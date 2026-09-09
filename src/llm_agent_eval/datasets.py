"""CSV/JSON/JSONL dataset import with mapping, provenance, and immutable versions."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from typing import Any

from .auth import Actor
from .contracts import NotFound, WorkflowError
from .ingestion import ImportService
from .storage import Storage, _now
from .versions import VersionStore, canonical_json

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ROWS = 5_000
MAX_INPUT_BYTES = 256 * 1024
MISSING = object()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _pointer(document: Any, pointer: str | None):
    if pointer is None:
        return MISSING
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise WorkflowError("JSON Pointer mappings must start with '/'")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return MISSING
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return MISSING
        else:
            return MISSING
    return current


def _clone(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _label_status(expected, declared) -> tuple[str, str | None]:
    if expected is MISSING or expected is None or expected == {}:
        return "missing", None
    if declared is MISSING or declared in (None, ""):
        return "inferred", "inferred"
    text = str(declared)
    if text in {"user_stated", "derived_from_trace", "approved"}:
        return "approved", "user_stated" if text == "approved" else text
    if text in {"inferred", "agent_output"}:
        return "inferred", "inferred"
    raise WorkflowError(f"Unsupported label_status {text!r}")


def _wrap_expected(expected, tag: str | None) -> dict[str, Any]:
    if expected is MISSING or expected is None:
        return {}
    provenance = tag or "inferred"
    if isinstance(expected, dict):
        return {key: {"tag": provenance, "value": value} for key, value in expected.items()}
    return {"value": {"tag": provenance, "value": expected}}


def _field_state(value) -> str:
    if value is MISSING:
        return "missing"
    if value is None:
        return "null"
    return "present"


def _csv_cell(row: dict[str, str], column: str | None):
    if not column:
        return MISSING
    if column not in row:
        return MISSING
    return row[column] if row[column] != "" else MISSING


class DatasetService:
    def __init__(self, storage: Storage, artifact_root):
        self.storage = storage
        self.importer = ImportService(storage, artifact_root)

    def validate_import(self, actor: Actor, project_id: str, upload_id: str, mapping: dict,
                        metric_requirements: list[dict], *, dataset_id: str | None = None) -> dict:
        actor.require(write=True)
        if self.storage.get_project(project_id, actor.workspace_id) is None:
            raise NotFound()
        if not isinstance(mapping, dict) or not isinstance(metric_requirements, list):
            raise WorkflowError("mapping and metric_requirements are required")
        data = self.importer._read_upload(actor, upload_id)
        if len(data) > MAX_UPLOAD_BYTES:
            raise WorkflowError("Dataset exceeds the 25 MiB import limit", code="quota_exceeded", status=413)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowError("Dataset must be UTF-8", code="invalid_utf8") from exc
        cases = self._parse(text, mapping)
        self._reject_duplicate_identities(cases)
        report = self._report(cases, metric_requirements)
        report_id, now = uuid.uuid4().hex, _now()
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            if dataset_id:
                found = conn.execute(
                    "SELECT dataset_id FROM datasets WHERE workspace_id=? AND dataset_id=? AND project_id=?",
                    (actor.workspace_id, dataset_id, project_id),
                ).fetchone()
                if found is None:
                    raise NotFound()
            else:
                dataset_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO datasets (workspace_id,dataset_id,project_id,created_at) VALUES (?,?,?,?)",
                    (actor.workspace_id, dataset_id, project_id, now),
                )
            conn.execute(
                "INSERT INTO dataset_import_reports (workspace_id,report_id,dataset_id,project_id,upload_id,"
                "mapping_json,requirements_json,report_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (actor.workspace_id, report_id, dataset_id, project_id, upload_id,
                 canonical_json(mapping), canonical_json(metric_requirements), canonical_json(report), now),
            )
        return {"state": "validated", "dataset_id": dataset_id, "report_id": report_id, **report}

    def commit_dataset(self, actor: Actor, dataset_id: str, report_id: str,
                       explicit_exclusions: list[str], expected_revision: int) -> dict:
        actor.require(write=True)
        if not isinstance(explicit_exclusions, list) or not all(isinstance(item, str) for item in explicit_exclusions):
            raise WorkflowError("explicit_exclusions must be a list of case IDs")
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            row = conn.execute(
                "SELECT * FROM dataset_import_reports WHERE workspace_id=? AND report_id=? AND dataset_id=?",
                (actor.workspace_id, report_id, dataset_id),
            ).fetchone()
            if row is None:
                raise NotFound()
            report = json.loads(row["report_json"])
            requirements = json.loads(row["requirements_json"])
        excluded = set(explicit_exclusions)
        known = {case["case_id"] for case in report["cases"]}
        if excluded - known:
            raise WorkflowError("Exclusion listed a case that is not in the report")
        kept = [case for case in report["cases"] if case["case_id"] not in excluded]
        exclusions = [{"case_id": case_id, "reason": "explicit"} for case_id in explicit_exclusions]
        content = self._report(kept, requirements)
        content["exclusions"] = exclusions
        version = VersionStore(self.storage).create("dataset", dataset_id, content, expected_revision, actor)
        return {"state": "validated", "dataset_id": dataset_id, "version_id": version.version_id,
                "revision": version.revision}

    def get_version(self, actor: Actor, dataset_id: str, version_id: str) -> dict:
        actor.require()
        version = VersionStore(self.storage).get(version_id, actor)
        if version.kind != "dataset" or version.parent_id != dataset_id:
            raise NotFound()
        return {
            "dataset_id": dataset_id,
            "version_id": version.version_id,
            "revision": version.revision,
            "content_digest": version.content_digest,
            **version.content,
        }

    def _parse(self, text: str, mapping: dict) -> list[dict]:
        fmt = mapping.get("format")
        if fmt == "jsonl":
            rows = list(self._iter_jsonl(text))
        elif fmt == "json":
            rows = list(self._iter_json(text))
        elif fmt == "csv":
            rows = list(self._iter_csv(text, mapping))
        else:
            raise WorkflowError("format must be csv, json, or jsonl")
        if len(rows) > MAX_ROWS:
            raise WorkflowError("Dataset exceeds 5,000 rows", code="row_limit")
        return [self._case(row, mapping) for row in rows]

    def _iter_jsonl(self, text: str):
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkflowError(f"Invalid JSONL at row {number}", code="row_parse_error") from exc
            if not isinstance(payload, dict):
                raise WorkflowError(f"JSONL row {number} must be an object")
            yield {"row_number": number, "document": payload}

    def _iter_json(self, text: str):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowError("Invalid JSON array", code="row_parse_error") from exc
        if not isinstance(payload, list):
            raise WorkflowError("JSON datasets must be an array of objects")
        for number, item in enumerate(payload, 1):
            if not isinstance(item, dict):
                raise WorkflowError(f"JSON row {number} must be an object")
            yield {"row_number": number, "document": item}

    def _iter_csv(self, text: str, mapping: dict):
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise WorkflowError("CSV is missing a header row")
        for number, row in enumerate(reader, 2):
            yield {"row_number": number, "document": row, "csv": True}

    def _case(self, row: dict, mapping: dict) -> dict:
        document = row["document"]
        csv_mode = row.get("csv") is True
        case_id = self._mapped(document, mapping.get("case_id"), csv_mode)
        if case_id is MISSING or case_id in (None, ""):
            raise WorkflowError(f"Row {row['row_number']} is missing case_id")
        case_id = str(case_id)
        if csv_mode:
            input_value = {
                key: value for key, column in (mapping.get("input_fields") or {}).items()
                if (value := _csv_cell(document, column)) is not MISSING
            }
            expected_raw = {
                key: value for key, column in (mapping.get("expected_fields") or {}).items()
                if (value := _csv_cell(document, column)) is not MISSING
            }
            if not expected_raw:
                expected_raw = MISSING
            declared = _csv_cell(document, mapping.get("label_status"))
            tags = []
            split = _csv_cell(document, mapping.get("split"))
            fixture = MISSING
        else:
            input_value = self._mapped(document, mapping.get("input"))
            expected_raw = self._mapped(document, mapping.get("expected"))
            declared = self._mapped(document, mapping.get("label_status"))
            tags = self._mapped(document, mapping.get("tags"))
            split = self._mapped(document, mapping.get("split"))
            fixture = self._mapped(document, mapping.get("fixture_state"))
        if input_value is MISSING or not isinstance(input_value, dict):
            raise WorkflowError(f"Row {row['row_number']} is missing a mapped input object")
        encoded_input = canonical_json(input_value).encode()
        if len(encoded_input) > MAX_INPUT_BYTES:
            raise WorkflowError(
                "A case input exceeds 256 KiB",
                code="case_input_limit",
                details={"case_id": case_id, "row_number": row["row_number"]},
            )
        status, provenance = _label_status(expected_raw, declared)
        expected = _wrap_expected(expected_raw, provenance)
        metadata = {
            "tags": [] if tags is MISSING or tags is None else tags,
            "split": None if split is MISSING else split,
            "fixture_state": None if fixture is MISSING else fixture,
            "row_number": row["row_number"],
        }
        return {
            "case_id": case_id,
            "case_key": _digest(input_value),
            "input_hash": _digest(input_value),
            "reference_hash": _digest(expected),
            "metadata_hash": _digest(metadata),
            "input": _clone(input_value),
            "expected": expected,
            "agent_request": _clone(input_value),
            "label_status": status,
            "label_provenance": provenance,
            "tags": metadata["tags"],
            "split": metadata["split"],
            "fixture_state": metadata["fixture_state"],
            "fields": {"expected": {"state": _field_state(expected_raw)}},
        }

    def _mapped(self, document, selector, csv_mode=False):
        if csv_mode:
            return _csv_cell(document, selector)
        if selector is None:
            return MISSING
        if isinstance(selector, str) and selector.startswith("/"):
            return _pointer(document, selector)
        raise WorkflowError("JSON/JSONL mappings must use JSON Pointers")

    def _reject_duplicate_identities(self, cases: list[dict]) -> None:
        seen_ids, seen_keys = {}, {}
        for case in cases:
            prior = seen_ids.get(case["case_id"]) or seen_keys.get(case["input_hash"])
            if prior:
                raise WorkflowError(
                    "Duplicate or contradictory case identity",
                    code="duplicate_identity",
                    details={"case_id": case["case_id"], "other_case_id": prior["case_id"]},
                )
            seen_ids[case["case_id"]] = case
            seen_keys[case["input_hash"]] = case

    def _report(self, cases: list[dict], metric_requirements: list[dict]) -> dict:
        approved = sum(1 for case in cases if case["label_status"] == "approved")
        accuracy = [item for item in metric_requirements if item.get("requires_label") or item.get("class") == "accuracy"]
        total = len(cases)
        accuracy_eligible = approved
        return {
            "cases": cases,
            "exclusions": [],
            "label_coverage": {
                "approved": approved,
                "inferred": sum(1 for case in cases if case["label_status"] == "inferred"),
                "missing": sum(1 for case in cases if case["label_status"] == "missing"),
                "total": total,
            },
            "eligibility": {
                "health": {"eligible_cases": total, "total_cases": total},
                "accuracy": {
                    "eligible_cases": accuracy_eligible if accuracy else 0,
                    "total_cases": total,
                    "coverage": f"{accuracy_eligible}/{total}" if total else "0/0",
                },
            },
        }
