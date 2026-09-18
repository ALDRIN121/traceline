"""R2 recurring-evaluation due slots and in-process lock contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    timezone: str
    local_time: time


def due_daily_slots(start: date, end: date, local_time: time, timezone: str) -> list[datetime]:
    if end < start:
        return []
    zone = ZoneInfo(timezone)
    current, result = start, []
    while current <= end:
        local = datetime.combine(current, local_time).replace(tzinfo=zone, fold=0)
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
