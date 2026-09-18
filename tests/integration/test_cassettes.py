from __future__ import annotations

import pytest

from llm_agent_eval.egress.cassettes import CassetteWorld, cassette_fingerprint


def test_cassette_record_replay_and_hybrid_are_bounded_and_deterministic(tmp_path):
    world = CassetteWorld(tmp_path / "cassettes", mode="record", version="v1")
    request = {"model": "fixture", "messages": [{"content": "hello"}]}
    fingerprint = cassette_fingerprint("openai", "/chat", request, "v1")
    assert world.exchange(fingerprint, request, lambda: (200, {"answer": "ok"})) == (200, {"answer": "ok"})

    replay = CassetteWorld(tmp_path / "cassettes", mode="replay", version="v1")
    assert replay.exchange(fingerprint, request, lambda: pytest.fail("replay billed upstream")) == (200, {"answer": "ok"})

    with pytest.raises(KeyError):
        replay.exchange("0" * 64, request, lambda: (200, {}))
