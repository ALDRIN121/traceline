"""Static discovery: source is data, never execution evidence."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import io
from typing import Any
import zipfile


FRAMEWORKS = ("crewai", "langchain", "langgraph", "autogen", "openai")
ENTRYPOINT_FILES = ("__main__.py", "main.py", "entrypoint.sh", "agent.py")
ENTRYPOINT_CALLS = frozenset({"kickoff", "run", "invoke", "main"})
_INSTRUCTION_MARKERS = ("ignore previous", "system prompt", "you are an", "already verified execution")


@dataclass
class DiscoveryReport:
    source_version_id: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    pending_questions: list[dict[str, str]] = field(default_factory=list)
    needs_entrypoint_declaration: bool = True
    files: dict[str, str] = field(default_factory=dict)
    digest: str = ""


def _fact_id(kind: str, name: str) -> str:
    return hashlib.sha256(f"{kind}:{name}".encode()).hexdigest()[:16]


def _fact(kind, name, status, confidence, source_version_id, path, start, end) -> dict[str, Any]:
    return {
        "fact_id": _fact_id(kind, name),
        "kind": kind,
        "name": name,
        "status": status,
        "confidence": confidence,
        "evidence": [{
            "source_version_id": source_version_id,
            "path": path,
            "line_start": start,
            "line_end": end,
        }],
        "confirmed_by": None,
        "observed_in_attempt_id": None,
    }


def snapshot_files(data: bytes) -> dict[str, str]:
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            raw = archive.read(name)
            try:
                files[name] = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
    return files


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _runtime_tool_names(tree: ast.AST) -> list[tuple[str, int, int]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(value, ast.Call)
            ):
                found.append((key.value, node.lineno, getattr(node, "end_lineno", node.lineno)))
    return found


def discover(source_version_id: str, files: dict[str, str]) -> DiscoveryReport:
    facts: list[dict[str, Any]] = []
    has_entrypoint = False
    for path, text in sorted(files.items()):
        lowered = text.lower()
        if any(marker in lowered for marker in _INSTRUCTION_MARKERS) and not path.endswith(".py"):
            continue
        if path.rsplit("/", 1)[-1] in ENTRYPOINT_FILES and path.endswith(".py"):
            # A conventionally named file is a candidate, not a proven invocation.
            facts.append(_fact("entrypoint_candidate", path, "observed_static", 0.4, source_version_id, path, 1, 1))
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            facts.append(_fact("parse_error", path, "observed_static", 0.9, source_version_id, path, 1, 1))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in FRAMEWORKS:
                        facts.append(_fact("framework", root, "observed_static", 0.9, source_version_id, path, node.lineno, node.lineno))
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                if root in FRAMEWORKS:
                    facts.append(_fact("framework", root, "observed_static", 0.9, source_version_id, path, node.lineno, node.lineno))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and any(isinstance(target, ast.Name) and target.id.lower() in {"model", "model_id", "model_name", "llm_model"} for target in targets)
                ):
                    facts.append(_fact("model", value.value, "observed_static", 0.6, source_version_id, path, node.lineno, node.lineno))
            elif isinstance(node, ast.Call):
                pass
            elif isinstance(node, ast.ClassDef):
                bases = [_call_name(base) or "" for base in node.bases]
                if any(base == "BaseTool" or base.endswith("BaseTool") for base in bases):
                    facts.append(_fact("tool", node.name, "observed_static", 0.7, source_version_id, path, node.lineno, node.lineno))
        for name, start, end in _runtime_tool_names(tree):
            facts.append(_fact("tool", name, "inferred", 0.3, source_version_id, path, start, end))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and _call_name(node.value.func) in ENTRYPOINT_CALLS:
                facts.append(_fact("entrypoint", _call_name(node.value.func), "observed_static", 0.8, source_version_id, path, node.lineno, node.lineno))
                has_entrypoint = True
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                left = node.test.left
                if isinstance(left, ast.Name) and left.id == "__name__":
                    for child in node.body:
                        if isinstance(child, ast.Expr) and isinstance(child.value, ast.Call) and _call_name(child.value.func) in ENTRYPOINT_CALLS:
                            facts.append(_fact("entrypoint", _call_name(child.value.func), "observed_static", 0.8, source_version_id, path, child.lineno, child.lineno))
                            has_entrypoint = True

    unique: dict[str, dict[str, Any]] = {}
    for fact in facts:
        unique[fact["fact_id"]] = fact
    pending = []
    if not has_entrypoint:
        pending.append({
            "question_id": "entrypoint_declaration",
            "text": "Which command should invoke this agent?",
        })
    encoded = hashlib.sha256(repr(sorted((f["fact_id"], f["status"], f["name"]) for f in unique.values())).encode()).hexdigest()
    return DiscoveryReport(
        source_version_id=source_version_id,
        facts=list(unique.values()),
        pending_questions=pending,
        needs_entrypoint_declaration=not has_entrypoint,
        files={path: text for path, text in files.items() if path.endswith(".py")},
        digest=encoded,
    )


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Project understanding",
        "",
        "Runtime has not been verified. Static findings are not execution evidence.",
        "",
        f"- Entrypoint declaration needed: {report['needs_entrypoint_declaration']}",
        f"- Review needed: {report.get('review_needed', False)}",
        "",
        "## Facts",
    ]
    for fact in report["facts"]:
        evidence = fact["evidence"][0] if fact.get("evidence") else {}
        lines.append(
            f"- `{fact['name']}` ({fact['kind']}, {fact['status']}, confidence {fact['confidence']})"
            f" — {evidence.get('path', '?')}:{evidence.get('line_start', '?')}"
        )
    if report.get("pending_questions"):
        lines.extend(["", "## Pending questions"])
        for question in report["pending_questions"]:
            lines.append(f"- {question['text']}")
    return "\n".join(lines) + "\n"
