"""The broken and repaired reference fixtures must remain distinct."""

from __future__ import annotations

import ast
from pathlib import Path

from llm_agent_eval.discovery import discover


ROOT = Path(__file__).parents[1]
BROKEN = ROOT / "fixtures" / "reference-agent" / "multi_agent_system.py"
REPAIRED = ROOT / "fixtures" / "reference-agent-repaired" / "multi_agent_system.py"


def test_original_reference_fixture_is_still_the_adversarial_parse_failure():
    original = BROKEN.read_text(encoding="utf-8")
    try:
        ast.parse(original, filename=str(BROKEN))
    except SyntaxError:
        pass
    else:
        raise AssertionError("the deliberately broken reference fixture was repaired in place")


def test_repaired_reference_fixture_is_parseable_and_explicitly_runnable():
    repaired = REPAIRED.read_text(encoding="utf-8")
    ast.parse(repaired, filename=str(REPAIRED))
    assert 'model="gemini-2.0-flash"' in repaired
    assert "crew.kickoff()" in repaired

    report = discover("repaired-reference", {"multi_agent_system.py": repaired})
    assert report.needs_entrypoint_declaration is False
    assert any(item["kind"] == "framework" and item["name"] == "crewai" for item in report.facts)
    assert any(item["kind"] == "entrypoint" and item["name"] == "main" for item in report.facts)
