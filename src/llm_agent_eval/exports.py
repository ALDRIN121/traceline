"""Stable, secret-free run export snapshots."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import uuid
from typing import Any


class ExportService:
    def __init__(self, storage):
        self.storage = storage

    def _manifest(
        self,
        workspace_id: str,
        run_id: str,
        *,
        case_ids: list[str] | None = None,
        metric_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        run = self.storage.get_run(run_id, workspace_id)
        if run is None:
            raise KeyError(run_id)
        case_filter = set(case_ids or ())
        metric_filter = set(metric_ids or ())
        if case_ids is not None and len(case_filter) != len(case_ids):
            raise ValueError("case_ids must not contain duplicates")
        if metric_ids is not None and len(metric_filter) != len(metric_ids):
            raise ValueError("metric_ids must not contain duplicates")
        metrics = [
            {
                "metric_id": row.metric_id,
                "value": row.value,
                "ci_lower": row.ci_lower,
                "ci_upper": row.ci_upper,
                "unit": row.unit,
                "gate_status": row.gate_status,
                "aggregation_state": row.aggregation_state,
                "method": row.method,
                "sample_n": row.sample_n,
                "error_n": row.error_n,
                "skipped_n": row.skipped_n,
                "no_ci": row.no_ci,
                "score_revision": row.score_revision,
            }
            for row in self.storage.get_run_metric_results(run_id, workspace_id)
            if not metric_filter or row.metric_id in metric_filter
        ]
        cases = [
            {
                "case_id": case.case_id,
                "case_key": case.case_key,
                "status": case.status,
                "classification": case.classification,
                "error_category": case.error_category,
            }
            for case in self.storage.list_cases(run_id, workspace_id)
            if not case_filter or case.case_id in case_filter
        ]
        case_metrics = [
            {
                "case_id": row.case_id,
                "metric_id": row.metric_id,
                "score_revision": row.score_revision,
                "status": row.status,
                "score": row.score,
                "raw_value": row.raw_value,
                "evidence_event_ids": list(row.evidence_event_ids),
                "evaluator_version": row.evaluator_version,
                "judge_binding": row.judge_binding,
                "is_authoritative": row.is_authoritative,
                "on_retry_override": row.on_retry_override,
                "overridden": row.overridden,
                "computed_at": row.computed_at,
            }
            for row in self.storage.get_case_scores(run_id=run_id, workspace_id=workspace_id)
            if (not case_filter or row.case_id in case_filter)
            and (not metric_filter or row.metric_id in metric_filter)
        ]
        payload = {
            "schema_version": 1,
            "run": {
                "run_id": run.run_id,
                "status": run.status,
                "tier": run.tier,
                "repeats": run.repeats,
                "retry_max": run.retry_max,
                "project_id": run.project_id,
                "created_at": run.created_at,
                "completed_at": run.run_completed_at,
            },
            "versions": {
                "spec_sha256": hashlib.sha256(run.spec_json.encode("utf-8")).hexdigest(),
                "source_digest": run.agent_version.get("source_digest"),
                "run_manifest_ref": run.run_manifest_ref,
            },
            "filters": {
                "case_ids": sorted(case_filter) if case_ids is not None else None,
                "metric_ids": sorted(metric_filter) if metric_ids is not None else None,
            },
            "metrics": metrics,
            "cases": cases,
            "case_metrics": case_metrics,
            "provenance": {
                "score_source": "engine",
                "trace_redaction": "capture_time",
                "evidence_state": "retained_evidence_is_run_scoped",
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return {"state": "snapshot", "manifest": payload, "sha256": hashlib.sha256(encoded).hexdigest()}

    def snapshot(self, workspace_id: str, run_id: str) -> dict[str, Any]:
        return self._manifest(workspace_id, run_id)

    def freeze(
        self,
        workspace_id: str,
        run_id: str,
        *,
        case_ids: list[str] | None = None,
        metric_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        snapshot = self._manifest(
            workspace_id, run_id, case_ids=case_ids, metric_ids=metric_ids
        )
        export_id = uuid.uuid4().hex
        stored = self.storage.create_export_snapshot(
            workspace_id=workspace_id,
            export_id=export_id,
            run_id=run_id,
            manifest=snapshot["manifest"],
            manifest_sha256=snapshot["sha256"],
        )
        return {**stored, "formats": self.format_hashes(stored["manifest"])}

    def frozen(self, workspace_id: str, export_id: str) -> dict[str, Any]:
        snapshot = self.storage.get_export_snapshot(export_id, workspace_id)
        if snapshot is None:
            raise KeyError(export_id)
        return {**snapshot, "formats": self.format_hashes(snapshot["manifest"])}

    @staticmethod
    def format_hashes(manifest: dict[str, Any]) -> dict[str, str]:
        return {
            "json": hashlib.sha256(ExportService._json_bytes(manifest)).hexdigest(),
            "csv": hashlib.sha256(ExportService._csv_bytes(manifest)).hexdigest(),
            "html": hashlib.sha256(ExportService._html_text(manifest).encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _json_bytes(manifest: dict[str, Any]) -> bytes:
        return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _csv_bytes(manifest: dict[str, Any]) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([
            "record_type", "case_id", "case_key", "metric_id", "status", "score",
            "raw_value", "evidence_event_ids", "classification", "error_category",
        ])
        for case in manifest["cases"]:
            writer.writerow([
                "case", _formula_safe(case["case_id"]), _formula_safe(case["case_key"]), "",
                _formula_safe(case["status"]), "", "", "",
                _formula_safe(case["classification"]), _formula_safe(case["error_category"]),
            ])
        case_keys = {case["case_id"]: case["case_key"] for case in manifest["cases"]}
        for metric in manifest.get("case_metrics", []):
            writer.writerow([
                "metric", _formula_safe(metric["case_id"]),
                _formula_safe(case_keys.get(metric["case_id"], "")),
                _formula_safe(metric["metric_id"]), _formula_safe(metric["status"]),
                _formula_safe(metric["score"]),
                _formula_safe(json.dumps(metric["raw_value"], sort_keys=True, ensure_ascii=False)),
                _formula_safe(";".join(metric["evidence_event_ids"])),
                "", "",
            ])
        return output.getvalue().encode("utf-8")

    @staticmethod
    def _html_text(manifest: dict[str, Any]) -> str:
        cases = "".join(
            f"<tr><td>{html.escape(str(case['case_id']))}</td>"
            f"<td>{html.escape(str(case['status']))}</td></tr>"
            for case in manifest["cases"]
        )
        metrics = "".join(
            f"<tr><td>{html.escape(str(metric['case_id']))}</td>"
            f"<td>{html.escape(str(metric['metric_id']))}</td>"
            f"<td>{html.escape(str(metric['status']))}</td>"
            f"<td>{html.escape(';'.join(metric['evidence_event_ids']))}</td></tr>"
            for metric in manifest.get("case_metrics", [])
        )
        return (
            "<table><tr><th>Case</th><th>Status</th></tr>" + cases +
            "</table><table><tr><th>Case</th><th>Metric</th><th>Status</th>"
            "<th>Evidence</th></tr>" + metrics + "</table>"
        )

    def frozen_bytes(self, workspace_id: str, export_id: str, format_name: str) -> tuple[bytes, str]:
        manifest = self.frozen(workspace_id, export_id)["manifest"]
        if format_name == "json":
            return self._json_bytes(manifest), "application/json"
        if format_name == "csv":
            return self._csv_bytes(manifest), "text/csv"
        if format_name == "html":
            return self._html_text(manifest).encode("utf-8"), "text/html"
        raise ValueError("unsupported export format")

    def csv(self, workspace_id: str, run_id: str) -> bytes:
        return self._csv_bytes(self.snapshot(workspace_id, run_id)["manifest"])

    def html(self, workspace_id: str, run_id: str) -> str:
        return self._html_text(self.snapshot(workspace_id, run_id)["manifest"])


def _formula_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@"} else text
