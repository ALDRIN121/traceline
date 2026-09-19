"""Confirmed project knowledge versions, invalidation, and lexical retrieval."""

from __future__ import annotations

from typing import Any

from .artifacts import ArtifactStore
from .auth import Actor
from .contracts import NotFound, WorkflowError
from .discovery import discover, markdown_report, snapshot_files
from .versions import VersionStore


class KnowledgeStore:
    def __init__(self, storage, artifact_root):
        self.storage, self.artifact_root = storage, artifact_root

    def _versions(self) -> VersionStore:
        return VersionStore(self.storage)

    def _source(self, project_id: str, actor: Actor):
        listing = self._versions().list("source", project_id, actor)
        if not listing["active_version_id"]:
            raise NotFound()
        return self._versions().get(listing["active_version_id"], actor)

    def _files(self, source, actor: Actor) -> dict[str, str]:
        artifact_ids = source.content.get("artifact_ids") or []
        if not artifact_ids:
            raise WorkflowError("Source snapshot is missing")
        data = ArtifactStore(self.storage, self.artifact_root, actor).get(actor.workspace_id, artifact_ids[0])
        return snapshot_files(data)

    def _merge(self, discovered, previous) -> dict[str, Any]:
        prior_facts = {fact["fact_id"]: fact for fact in (previous or {}).get("facts") or []}
        facts = []
        missing_confirmed = False
        for fact in discovered.facts:
            prior = prior_facts.get(fact["fact_id"])
            if prior and prior.get("status") == "user_confirmed":
                merged = dict(fact)
                merged["status"] = "user_confirmed"
                merged["confirmed_by"] = prior.get("confirmed_by")
                for field in ("summary", "original_summary", "correction_note"):
                    if field in prior:
                        merged[field] = prior[field]
                facts.append(merged)
            else:
                facts.append(dict(fact))
        discovered_ids = {fact["fact_id"] for fact in discovered.facts}
        for fact_id, prior in prior_facts.items():
            if fact_id not in discovered_ids and prior.get("status") in {"user_confirmed", "review_needed"}:
                kept = dict(prior)
                kept["status"] = "review_needed"
                kept["observed_in_attempt_id"] = None
                facts.append(kept)
                missing_confirmed = True
        pending = list(discovered.pending_questions)
        if previous and previous.get("pending_questions"):
            by_id = {item["question_id"]: item for item in pending}
            for item in previous["pending_questions"]:
                by_id.setdefault(item["question_id"], item)
            pending = list(by_id.values())
            if not discovered.needs_entrypoint_declaration:
                pending = [item for item in pending if item["question_id"] != "entrypoint_declaration"]
        report = {
            "source_version_id": discovered.source_version_id,
            "discovery_digest": discovered.digest,
            "verification_id": None,
            "needs_entrypoint_declaration": discovered.needs_entrypoint_declaration,
            "review_needed": missing_confirmed,
            "facts": facts,
            "pending_questions": pending,
            "source_files": discovered.files,
        }
        report["markdown_report"] = markdown_report(report)
        return report

    def current(self, project_id: str, actor: Actor) -> dict[str, Any]:
        actor.require()
        source = self._source(project_id, actor)
        discovered = discover(source.version_id, self._files(source, actor))
        listing = self._versions().list("knowledge", project_id, actor)
        previous = None
        if listing["active_version_id"]:
            previous = self._versions().get(listing["active_version_id"], actor).content
        merged = self._merge(discovered, previous)
        if previous and previous.get("discovery_digest") == merged["discovery_digest"] and previous.get("source_version_id") == source.version_id:
            return self._view(listing["active_version_id"], listing["active_revision"], previous)
        actor.require(write=True)
        stored = dict(merged)
        stored.pop("source_files", None)
        record = self._versions().create("knowledge", project_id, stored, listing["active_revision"], actor)
        merged["report_id"] = record.version_id
        return self._view(record.version_id, record.revision, {**stored, "source_files": discovered.files})

    def confirm(self, project_id: str, report_id: str, corrections: list[dict], expected_revision: int, actor: Actor) -> dict[str, Any]:
        actor.require(write=True)
        current = self.current(project_id, actor)
        if current["report_id"] != report_id:
            raise WorkflowError("Knowledge report is stale; reload before confirming", code="conflict", status=409)
        facts = {fact["fact_id"]: dict(fact) for fact in current["facts"]}
        for correction in corrections:
            if not isinstance(correction, dict) or correction.get("status") != "user_confirmed":
                raise WorkflowError("Corrections must confirm an existing fact")
            fact = facts.get(correction.get("fact_id"))
            if fact is None:
                raise WorkflowError("Unknown fact cannot be confirmed")
            fact["status"] = "user_confirmed"
            fact["confirmed_by"] = actor.actor_id
            fact["observed_in_attempt_id"] = None
            if "summary" in correction:
                summary = correction["summary"]
                if not isinstance(summary, str) or not summary.strip():
                    raise WorkflowError("A correction summary must be non-empty text")
                if "original_summary" not in fact:
                    fact["original_summary"] = fact.get("summary", "")
                fact["summary"] = summary.strip()
                fact["correction_note"] = summary.strip()
        content = {
            "source_version_id": current["source_version_id"],
            "discovery_digest": self._versions().get(report_id, actor).content.get("discovery_digest"),
            "verification_id": None,
            "needs_entrypoint_declaration": current["needs_entrypoint_declaration"],
            "review_needed": False,
            "facts": list(facts.values()),
            "pending_questions": current["pending_questions"],
        }
        content["markdown_report"] = markdown_report(content)
        record = self._versions().create("knowledge", project_id, content, expected_revision, actor)
        return self._view(record.version_id, record.revision, content)

    def retrieve(self, project_id: str, query: str, budget: int, actor: Actor) -> dict[str, Any]:
        actor.require()
        if not isinstance(query, str) or not query.strip():
            raise WorkflowError("A retrieval query is required")
        if type(budget) is not int or budget < 1 or budget > 20:
            raise WorkflowError("budget must be an integer from 1 to 20")
        source = self._source(project_id, actor)
        files = self._files(source, actor)
        terms = [token.lower() for token in query.split() if token]
        scored = []
        for path, text in files.items():
            if not path.endswith(".py"):
                continue
            lines = text.splitlines()
            haystack = text.lower()
            score = sum(haystack.count(term) for term in terms)
            if score <= 0:
                continue
            match_index = next((i for i, line in enumerate(lines) if any(term in line.lower() for term in terms)), 0)
            start = max(0, match_index)
            end = min(len(lines), start + 8)
            scored.append({
                "path": path,
                "line_start": start + 1,
                "line_end": end,
                "text": "\n".join(lines[start:end]),
                "score": score,
                "source_version_id": source.version_id,
            })
        scored.sort(key=lambda item: (-item["score"], item["path"]))
        return {"snippets": scored[:budget]}

    @staticmethod
    def _view(version_id: str, revision: int, content: dict[str, Any]) -> dict[str, Any]:
        return {
            "report_id": version_id,
            "knowledge_version_id": version_id,
            "revision": revision,
            "source_version_id": content["source_version_id"],
            "verification_id": content.get("verification_id"),
            "needs_entrypoint_declaration": content["needs_entrypoint_declaration"],
            "review_needed": content.get("review_needed", False),
            "facts": content["facts"],
            "pending_questions": content["pending_questions"],
            "markdown_report": content.get("markdown_report") or markdown_report(content),
        }
