"""Evidence-only score revisions.

Re-scoring is intentionally outside the execution service. It reads the
authoritative first-attempt trace and retained, redacted output payload, then
writes a new score revision. No target adapter is constructed or invoked.
"""

from __future__ import annotations

from typing import Any, Sequence

from .contracts import WorkflowError
from .evaluators import aggregate_metric, evaluate_gate, gate_safe, score_attempt, score_output
from .judge_gateway import GatewayJudge, JudgmentContext
from .rubric_store import RubricStore
from .spec import Metric


class RescoreService:
    def __init__(self, storage, *, gateway=None, judge_readiness=None):
        self.storage = storage
        self.gateway = gateway
        self.judge_readiness = judge_readiness

    def rescore(
        self,
        actor,
        run_id: str,
        metric_id: str,
        metric_payload: dict[str, Any],
        score_revision: int,
        *,
        case_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        actor.require(write=True)
        run = self.storage.get_run(run_id, actor.workspace_id)
        if run is None:
            raise WorkflowError("run not found", code="not_found", status=404)
        if run.status != "complete":
            raise WorkflowError(
                "only completed runs can be re-scored",
                code="rescore_run_incomplete", status=409,
            )
        if type(score_revision) is not int or score_revision <= 1:
            raise WorkflowError("score_revision must be greater than 1")
        try:
            metric = Metric.model_validate(metric_payload)
        except Exception as exc:
            raise WorkflowError("replacement metric is invalid", details={"error": str(exc)}) from exc
        if metric.metric_id != metric_id:
            raise WorkflowError("replacement metric_id must match the route metric_id")
        previous = self.storage.get_case_scores(
            run_id=run_id, workspace_id=actor.workspace_id, metric_id=metric_id,
        )
        if any(row.score_revision >= score_revision for row in previous):
            raise WorkflowError(
                "score revision already exists",
                code="score_revision_exists", status=409,
            )
        requested = set(case_ids or ())
        known = {case.case_id for case in run.spec.cases}
        if requested - known:
            raise WorkflowError(
                "unknown case in re-score request",
                details={"case_ids": sorted(requested - known)},
            )
        selected = [case for case in run.spec.cases if not requested or case.case_id in requested]
        replacement_spec = run.spec.model_copy(update={"metrics": [metric]})
        scores = []
        for case in selected:
            attempt = next(
                (
                    item for item in self.storage.list_attempts(
                        run_id, actor.workspace_id, case_id=case.case_id
                    )
                    if item.attempt == 0 and item.status == "completed"
                ),
                None,
            )
            if attempt is None:
                raise WorkflowError(
                    f"first-attempt evidence is unavailable for case {case.case_id!r}",
                    code="rescore_evidence_unavailable", status=409,
                )
            events = self.storage.get_trace_events(
                run_id=run_id, workspace_id=actor.workspace_id,
                attempt_id=attempt.attempt_id,
            )
            output = self.storage.get_attempt_output(attempt.attempt_id, actor.workspace_id)
            judge = self._judge(actor, metric, output, events)
            if metric.target.type in {"final_response", "state_change", "workflow_node", "external"}:
                if output is None:
                    raise WorkflowError(
                        f"output evidence is unavailable for case {case.case_id!r}",
                        code="rescore_evidence_unavailable", status=409,
                    )
                score = score_output(
                    replacement_spec, case.case_id, output, metric, judge=judge,
                )
            else:
                score = score_attempt(
                    replacement_spec, case.case_id, events, metric, judge=judge,
                )
            scores.append(score)

        for case, score in zip(selected, scores):
            self.storage.upsert_case_metric_results(
                run_id=run_id, case_id=case.case_id, workspace_id=actor.workspace_id,
                scores=[score], metric_by_id={metric.metric_id: metric},
                score_revision=score_revision,
            )
        aggregate = aggregate_metric(
            replacement_spec, metric, scores, expected_n=len(selected),
        )
        gate_status = "NOT_APPLICABLE"
        if metric in gate_safe([metric], readiness=self.judge_readiness):
            gate = evaluate_gate(metric, aggregate)
            gate_status = "PASS" if gate.passed else ("FAIL" if gate.active else "NOT_APPLICABLE")
        self.storage.upsert_run_metric_result(
            run_id=run_id, workspace_id=actor.workspace_id, metric=metric,
            aggregate=aggregate, score_revision=score_revision,
            gate_status=gate_status, no_ci=run.repeats < 3,
        )
        return {
            "state": "rescored",
            "run_id": run_id,
            "metric_id": metric_id,
            "score_revision": score_revision,
            "case_count": len(scores),
            "aggregate": {
                "value": aggregate.value,
                "pass_rate": aggregate.pass_rate,
                "sample_n": aggregate.n_cases,
                "aggregation_state": aggregate.aggregation_state.value,
            },
        }

    def _judge(self, actor, metric: Metric, output: Any, events):
        if metric.type != "judge":
            return None
        if self.gateway is None:
            raise WorkflowError("judge gateway is unavailable", code="judge_unavailable", status=409)
        rubric_store = RubricStore(self.storage)
        return GatewayJudge(
            self.gateway,
            rubric_version=metric.judge_binding.rubric_version,
            schema_version=metric.judge_binding.schema_version,
            readiness=self.judge_readiness.get(metric.judge_binding) if self.judge_readiness else None,
            rubric_resolver=lambda rubric_id: rubric_store.get(rubric_id, actor),
            context_resolver=lambda _metric, _case, refs: JudgmentContext(
                candidate_output=output,
                evidence={event.event_id: event.model_dump(mode="json") for event in events if event.event_id in refs},
            ),
        )
