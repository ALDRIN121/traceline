"""Deterministic paired run comparisons with honest incomparability states."""

from __future__ import annotations

import hashlib
import json
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
        reference_reason = _reference_change(baseline, candidate, base_cases, candidate_cases)
        if reference_reason is not None:
            return {
                "state": "incomparable", "reason": reference_reason,
                "common_cases": len(common),
            }
        confounders = _confounders(baseline, candidate)
        base_scores = _authoritative_scores(self.storage, baseline_run_id, workspace_id, metric_id)
        candidate_scores = _authoritative_scores(self.storage, candidate_run_id, workspace_id, metric_id)
        deltas = [candidate_scores[candidate_cases[key]] - base_scores[base_cases[key]]
                  for key in common if base_cases[key] in base_scores and candidate_cases[key] in candidate_scores]
        if not deltas:
            return {"state": "incomparable", "reason": "no_common_scored_cases", "common_cases": len(common)}
        delta = sum(deltas) / len(deltas)
        no_ci = baseline.repeats < 3 or candidate.repeats < 3
        low, high = (None, None) if no_ci else _bootstrap(deltas, resamples, seed)
        return {
            "state": "comparable", "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id, "metric_id": metric_id,
            "cohort": {
                "baseline_cases": len(base_cases),
                "candidate_cases": len(candidate_cases),
                "common_cases": len(common),
                "scored_common_cases": len(deltas),
            },
            "common_cases": len(deltas), "delta": delta,
            "confidence_interval": None if no_ci else {"lower": low, "upper": high},
            "no_ci": no_ci,
            "confounders": confounders,
            "automation_status": "comparable_no_ci" if no_ci else "comparable",
            "seed": seed, "resamples": resamples,
        }


def _metric_key(metric) -> str:
    return hashlib.sha256(json.dumps(metric.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _version_refs(run) -> dict[str, Any]:
    world = run.world_config if isinstance(run.world_config, dict) else {}
    refs = world.get("version_refs") if isinstance(world.get("version_refs"), dict) else {}
    return refs


def _confounders(baseline, candidate) -> list[dict[str, Any]]:
    left, right = _version_refs(baseline), _version_refs(candidate)
    out = []
    for key in ("source_version_id", "target_version_id", "connection_version_id",
                "model_selection_version_id", "dashboard_version_id"):
        if left.get(key) != right.get(key) and (left.get(key) is not None or right.get(key) is not None):
            out.append({"kind": key, "baseline": left.get(key), "candidate": right.get(key)})
    if left.get("world_version_id") != right.get("world_version_id"):
        out.append({"kind": "world_version_id", "baseline": left.get("world_version_id"), "candidate": right.get("world_version_id")})
    if baseline.repeats != candidate.repeats:
        out.append({"kind": "repeat_count", "baseline": baseline.repeats, "candidate": candidate.repeats})
    return out


def _reference_change(baseline, candidate, base_cases, candidate_cases) -> str | None:
    """Reject a changed expected/reference value for a matched input cohort."""
    base_by_id = {case.case_id: case for case in baseline.spec.cases}
    candidate_by_id = {case.case_id: case for case in candidate.spec.cases}
    for key in set(base_cases) & set(candidate_cases):
        left = base_by_id.get(base_cases[key])
        right = candidate_by_id.get(candidate_cases[key])
        if left is None or right is None:
            continue
        left_expected = {name: value.model_dump(mode="json") for name, value in left.expected.items()}
        right_expected = {name: value.model_dump(mode="json") for name, value in right.expected.items()}
        if left_expected != right_expected:
            return "reference_version_changed"
    left_dataset = _version_refs(baseline).get("dataset_version_id")
    right_dataset = _version_refs(candidate).get("dataset_version_id")
    if left_dataset != right_dataset and (left_dataset is not None or right_dataset is not None):
        return "dataset_version_changed"
    left_world = _version_refs(baseline).get("world_version_id")
    right_world = _version_refs(candidate).get("world_version_id")
    if left_world != right_world and (left_world is not None or right_world is not None):
        return "world_version_changed"
    return None


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
