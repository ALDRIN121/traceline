"""R2 recurring-evaluation due slots and in-process lock contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as utc_timezone
import hashlib
import uuid
from zoneinfo import ZoneInfo

from .auth import Actor
from .contracts import NotFound, WorkflowError
from .run_plans import RunPlanService


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    timezone: str
    local_time: time
    dst_policy: str = "first"


def due_daily_slots(start: date, end: date, local_time: time, timezone: str,
                    *, dst_policy: str = "first") -> list[datetime]:
    if end < start:
        return []
    zone = ZoneInfo(timezone)
    if dst_policy not in {"first", "skip"}:
        raise ValueError("unsupported DST policy")
    current, result = start, []
    while current <= end:
        naive = datetime.combine(current, local_time)
        local = naive.replace(tzinfo=zone, fold=0)
        roundtrip = local.astimezone(utc_timezone.utc).astimezone(zone).replace(tzinfo=None)
        if roundtrip != naive and dst_policy == "skip":
            current += timedelta(days=1)
            continue
        result.append(local)
        current += timedelta(days=1)
    return result


class ScheduleStore:
    def __init__(self):
        self._locks: dict[str, str] = {}

    def enqueue_key(self, schedule: Schedule, slot: datetime) -> str:
        return hashlib.sha256(f"{schedule.schedule_id}:{slot.isoformat()}".encode()).hexdigest()

    def acquire(self, schedule: Schedule, slot: datetime, owner: str) -> bool:
        key = self.enqueue_key(schedule, slot)
        if key in self._locks:
            return False
        self._locks[key] = owner
        return True


class DurableScheduleService:
    """Durable recurring synthetic-run scheduling.

    The scheduler only claims a local-time slot and enqueues the already
    authorized frozen run plan. It never re-plans, refreshes verification, or
    bypasses authorization on behalf of a schedule.
    """

    def __init__(self, storage):
        self.storage = storage

    def create(
        self, actor: Actor, *, plan_id: str, plan_hash: str, authorization_id: str,
        timezone_name: str, local_time: str, dst_policy: str = "first",
        version_policy: str = "frozen", daily_request_limit: int = 1,
        daily_budget_usd_micros: int = 0,
    ):
        actor.require(write=True)
        try:
            zone = ZoneInfo(timezone_name)
            parsed_time = time.fromisoformat(local_time)
        except (ValueError, TypeError):
            raise WorkflowError("timezone and local_time are invalid", code="schedule_invalid") from None
        if parsed_time.tzinfo is not None or parsed_time.second or parsed_time.microsecond:
            raise WorkflowError("local_time must be an HH:MM value", code="schedule_invalid")
        if dst_policy not in {"first", "skip"}:
            raise WorkflowError("unsupported DST policy", code="schedule_invalid")
        if version_policy != "frozen":
            # A schedule must never accept a policy it cannot execute. The
            # current plan/authorization contract freezes exact version refs;
            # silently treating ``new_versions`` as frozen would make the
            # schedule's visible policy dishonest.
            raise WorkflowError(
                "only frozen version policy is currently supported",
                code="schedule_version_policy_unsupported",
            )
        if type(daily_request_limit) is not int or not 1 <= daily_request_limit <= 10_000:
            raise WorkflowError("daily_request_limit is invalid", code="schedule_invalid")
        if type(daily_budget_usd_micros) is not int or daily_budget_usd_micros < 0:
            raise WorkflowError("daily_budget_usd_micros is invalid", code="schedule_invalid")
        plan = RunPlanService(self.storage).get_plan(actor, plan_id)
        if plan.content_digest != plan_hash or plan.state != "validated":
            raise WorkflowError("schedule requires the exact validated plan", code="authorization_hash_mismatch", status=409)
        authorization = self.storage.get_run_authorization(authorization_id, actor.workspace_id)
        if authorization is None or authorization.plan_id != plan_id or authorization.plan_hash != plan_hash:
            raise WorkflowError("schedule authorization is unavailable", code="authorization_unavailable", status=409)
        if authorization.state != "authorized" or datetime.fromisoformat(authorization.expires_at) <= datetime.now(utc_timezone.utc):
            raise WorkflowError("schedule authorization has expired", code="authorization_expired", status=409)
        project_id = plan.content["version_refs"].get("project_id")
        if not project_id or self.storage.get_project(project_id, actor.workspace_id) is None:
            raise NotFound()
        # Constructing ZoneInfo above is itself the validation; retain the
        # explicit object use so a missing system timezone database fails here.
        del zone
        return self.storage.create_schedule(
            workspace_id=actor.workspace_id,
            schedule_id=uuid.uuid4().hex,
            project_id=project_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            authorization_id=authorization_id,
            timezone=timezone_name,
            local_time=parsed_time.isoformat(timespec="minutes"),
            state="active",
            dst_policy=dst_policy,
            version_policy=version_policy,
            daily_request_limit=daily_request_limit,
            daily_budget_usd_micros=daily_budget_usd_micros,
        )

    def sweep(self, actor: Actor, worker, schedule_id: str, start: date, end: date, *, owner: str):
        actor.require(write=True)
        schedule = self.storage.get_schedule(schedule_id, actor.workspace_id)
        if schedule is None:
            raise NotFound()
        if schedule.state != "active":
            return []
        plan = RunPlanService(self.storage).get_plan(actor, schedule.plan_id)
        authorization = self.storage.get_run_authorization(schedule.authorization_id, actor.workspace_id)
        if (plan.content_digest != schedule.plan_hash or plan.state != "validated"
                or authorization is None or authorization.state != "authorized"
                or datetime.fromisoformat(authorization.expires_at) <= datetime.now(utc_timezone.utc)):
            return []
        local = time.fromisoformat(schedule.local_time)
        schedule_shape = Schedule(schedule.schedule_id, schedule.timezone, local, schedule.dst_policy)
        slots = due_daily_slots(
            start, end, local, schedule.timezone, dst_policy=schedule.dst_policy,
        )
        existing = self.storage.list_schedule_slots(schedule_id, actor.workspace_id)
        daily_counts: dict[str, int] = {}
        for slot in existing:
            if slot.state in {"claimed", "queued"}:
                day = slot.slot_at[:10]
                daily_counts[day] = daily_counts.get(day, 0) + 1
        plan_budget = int((plan.content.get("limits") or {}).get("budget_usd_micros", 0))
        daily_usage = self.storage.get_schedule_daily_cost_usage(
            schedule_id, actor.workspace_id, reservation_usd_micros=plan_budget,
        )
        queued = []
        for slot in slots:
            slot_key = ScheduleStore().enqueue_key(schedule_shape, slot)
            day = slot.strftime("%Y-%m-%d")
            if daily_counts.get(day, 0) >= schedule.daily_request_limit:
                continue
            if schedule.daily_budget_usd_micros:
                usage = daily_usage.get(day, {
                    "actual_usd_micros": 0,
                    "reserved_usd_micros": 0,
                    "unknown": False,
                })
                if usage["unknown"] or (
                    int(usage["actual_usd_micros"])
                    + int(usage["reserved_usd_micros"])
                    + plan_budget > schedule.daily_budget_usd_micros
                ):
                    continue
            # A single atomic insert is the distributed lock and idempotency
            # record; a second worker gets False and does no enqueue.
            if not self.storage.claim_schedule_slot(
                workspace_id=actor.workspace_id, schedule_id=schedule_id,
                slot_key=slot_key, slot_at=slot.isoformat(), owner=owner,
            ):
                continue
            try:
                job = worker.queue(actor).enqueue(
                    {
                        "kind": "evaluation_run",
                        "plan_id": schedule.plan_id,
                        "plan_hash": schedule.plan_hash,
                        "authorization_id": schedule.authorization_id,
                        "schedule_id": schedule.schedule_id,
                        "schedule_slot": slot_key,
                    },
                    f"schedule:{schedule.schedule_id}:{slot_key}",
                )
            except Exception:
                self.storage.set_schedule_slot_job(
                    workspace_id=actor.workspace_id, schedule_id=schedule_id,
                    slot_key=slot_key, state="failed",
                )
                raise
            self.storage.set_schedule_slot_job(
                workspace_id=actor.workspace_id, schedule_id=schedule_id,
                slot_key=slot_key, state="queued", job_id=job.job_id,
            )
            queued.append(job)
            daily_counts[day] = daily_counts.get(day, 0) + 1
            usage = daily_usage.setdefault(day, {
                "actual_usd_micros": 0,
                "reserved_usd_micros": 0,
                "unknown": False,
            })
            usage["reserved_usd_micros"] = int(usage["reserved_usd_micros"]) + plan_budget
        return queued
