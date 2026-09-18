"""Deterministic retrieval evidence metrics."""

from __future__ import annotations


def recall_at_k(relevant: list[str], ranked: list[str], k: int) -> float:
    if type(k) is not int or k < 1:
        raise ValueError("k must be positive")
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    return len(relevant_set & set(ranked[:k])) / len(relevant_set)


def citation_correctness(citations: list[str], evidence: dict[str, bool]) -> float | None:
    if not citations:
        return None
    if any(citation not in evidence for citation in citations):
        return None
    return sum(bool(evidence[citation]) for citation in citations) / len(citations)
