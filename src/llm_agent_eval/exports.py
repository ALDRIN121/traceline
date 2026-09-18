"""Stable, secret-free run export snapshots."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from typing import Any


class ExportService:
    def __init__(self, storage):
        self.storage = storage

    def snapshot(self, workspace_id: str, run_id: str) -> dict[str, Any]:
        run = self.storage.get_run(run_id, workspace_id)
        if run is None:
            raise KeyError(run_id)
        payload = {
            "run": {"run_id": run.run_id, "status": run.status, "tier": run.tier,
                    "repeats": run.repeats, "retry_max": run.retry_max},
            "metrics": [
                {"metric_id": row.metric_id, "value": row.value, "gate_status": row.gate_status,
                 "aggregation_state": row.aggregation_state, "no_ci": row.no_ci}
                for row in self.storage.get_run_metric_results(run_id, workspace_id)
            ],
            "cases": [
                {"case_id": case.case_id, "case_key": case.case_key,
                 "status": case.status, "classification": case.classification}
                for case in self.storage.list_cases(run_id, workspace_id)
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return {"state": "snapshot", "manifest": payload, "sha256": hashlib.sha256(encoded).hexdigest()}

    def csv(self, workspace_id: str, run_id: str) -> bytes:
        snapshot = self.snapshot(workspace_id, run_id)["manifest"]
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["case_id", "case_key", "status", "classification"])
        for case in snapshot["cases"]:
            writer.writerow([_formula_safe(case[key]) for key in ("case_id", "case_key", "status", "classification")])
        return output.getvalue().encode("utf-8")

    def html(self, workspace_id: str, run_id: str) -> str:
        snapshot = self.snapshot(workspace_id, run_id)["manifest"]
        return "<table><tr><th>Case</th><th>Status</th></tr>" + "".join(
            f"<tr><td>{html.escape(str(case['case_id']))}</td><td>{html.escape(str(case['status']))}</td></tr>"
            for case in snapshot["cases"]
        ) + "</table>"


def _formula_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@"} else text
