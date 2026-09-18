"""Deterministic paired run comparisons with honest incomparability states."""

from __future__ import annotations

import hashlib
import random
from typing import Any

from .contracts import WorkflowError


class ComparisonService:
    def __init__(self, storage):
        self.storage = storage

    def compare(self, workspace_id: str, baseline_run_id: str, candidate_run_id: str,
                metric_id: str, *, resamples: int = 10_000, seed: int = 0) -> dict[str, Any]:
        if type(resamples) is not int or not 1 <= resamples <= 10_000:
            raise WorkflowError("resamples must be between 1 and 10000")
        baseline = self.storage.get_run(baseline_run_id, workspace_id)
        candidate = self.storage.get_run(candidate_run_id, workspace_id)
        if baseline is None or candidate is None:
            raise WorkflowError("comparison run not found", code="not_found", status=404)
        if baseline.status != "complete" or candidate.status != "complete":
            return {"state": "incomparable", "reason": "runs_must_be_complete"}
        base_metric = next((m for m in baseline.spec.metrics if m.metric_id == metric_id), None)
        candidate_metric = next((m for m in candidate.spec.metrics if m.metric_id == metric_id), None)
        if base_metric is None or candidate_metric is None:
            return {"state": "incomparable", "reason": "metric_missing"}
        if _metric_key(base_metric) != _metric_key(candidate_metric):
            return {"state": "incomparable", "reason": "metric_definition_changed"}
        base_cases = {case.case_key: case.case_id for case in self.storage.list_cases(baseline_run_id, workspace_id)}
        candidate_cases = {case.case_key: case.case_id for case in self.storage.list_cases(candidate_run_id, workspace_id)}
        common = sorted(set(base_cases) & set(candidate_cases))
        base_scores = _authoritative_scores(self.storage, baseline_run_id, workspace_id, metric_id)
        candidate_scores = _authoritative_scores(self.storage, candidate_run_id, workspace_id, metric_id)
        deltas = [candidate_scores[candidate_cases[key]] - base_scores[base_cases[key]]
                  for key in common if base_cases[key] in base_scores and candidate_cases[key] in candidate_scores]
        if not deltas:
            return {"state": "incomparable", "reason": "no_common_scored_cases", "common_cases": len(common)}
        delta = sum(deltas) / len(deltas)
        low, high = _bootstrap(deltas, resamples, seed)
        return {
            "state": "comparable", "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id, "metric_id": metric_id,
            "common_cases": len(deltas), "delta": delta,
            "confidence_interval": {"lower": low, "upper": high},
            "no_ci": baseline.repeats < 3 or candidate.repeats < 3,
            "seed": seed, "resamples": resamples,
        }


def _metric_key(metric) -> str:
    import json
    return hashlib.sha256(json.dumps(metric.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _authoritative_scores(storage, run_id, workspace_id, metric_id):
    rows = storage.get_case_scores(run_id=run_id, workspace_id=workspace_id, metric_id=metric_id)
    return {row.case_id: row.score for row in rows
            if row.is_authoritative and row.score is not None and row.status in {"PASS", "FAIL"}}


def _bootstrap(values: list[float], resamples: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    samples = []
    for _ in range(resamples):
        samples.append(sum(values[rng.randrange(len(values))] for _ in values) / len(values))
    samples.sort()
    return samples[int(0.025 * (len(samples) - 1))], samples[int(0.975 * (len(samples) - 1))]
