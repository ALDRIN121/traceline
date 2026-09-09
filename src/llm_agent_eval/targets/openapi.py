"""Bounded OpenAPI assistance: bundled $ref only, no outbound fetches."""

from __future__ import annotations

from typing import Any

from ..contracts import WorkflowError

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _walk_refs(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.append(ref)
        for value in node.values():
            _walk_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_refs(item, found)


def import_openapi(document: dict, *, operation_id: str | None = None,
                   path: str | None = None, method: str | None = None) -> dict:
    if not isinstance(document, dict):
        raise WorkflowError("OpenAPI document must be a JSON object", code="openapi_invalid")
    refs: list[str] = []
    _walk_refs(document, refs)
    for ref in refs:
        if not ref.startswith("#/"):
            raise WorkflowError("External OpenAPI $ref fetches are disabled", code="openapi_external_ref")
    if not operation_id and not (path and method):
        raise WorkflowError("Select an OpenAPI operation before verification",
                            code="openapi_operation_required")
    paths = document.get("paths") or {}
    if not isinstance(paths, dict):
        raise WorkflowError("OpenAPI paths must be an object", code="openapi_invalid")
    selected = None
    for item_path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        for item_method, operation in operations.items():
            if item_method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            matches_id = operation_id and operation.get("operationId") == operation_id
            matches_path = path and method and item_path == path and item_method.lower() == method.lower()
            if matches_id or matches_path:
                selected = (item_path, item_method.upper(), operation)
                break
        if selected:
            break
    if selected is None:
        raise WorkflowError("Select an OpenAPI operation before verification",
                            code="openapi_operation_required")
    return {
        "state": "mapped",
        "path": selected[0],
        "method": selected[1],
        "operation_id": selected[2].get("operationId"),
        "external_refs": False,
    }
