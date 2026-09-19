from __future__ import annotations

import pytest

from llm_agent_eval.egress.cassettes import CassetteWorld, cassette_fingerprint
from llm_agent_eval.egress.transport import ProviderTransport


def test_cassette_record_replay_and_hybrid_are_bounded_and_deterministic(tmp_path):
    world = CassetteWorld(tmp_path / "cassettes", mode="record", version="v1")
    request = {"model": "fixture", "messages": [{"content": "hello"}]}
    fingerprint = cassette_fingerprint("openai", "/chat", request, "v1")
    assert world.exchange(fingerprint, request, lambda: (200, {"answer": "ok"})) == (200, {"answer": "ok"})

    replay = CassetteWorld(tmp_path / "cassettes", mode="replay", version="v1")
    assert replay.exchange(fingerprint, request, lambda: pytest.fail("replay billed upstream")) == (200, {"answer": "ok"})

    with pytest.raises(KeyError):
        replay.exchange("0" * 64, request, lambda: (200, {}))


def test_provider_transport_records_redacted_responses_and_replays_without_upstream(tmp_path):
    world = CassetteWorld(tmp_path / "cassettes", mode="record", version="v2")
    calls = []

    def send(_route, _body, _headers):
        calls.append("upstream")
        return 200, {
            "answer": "sk-test-do-not-persist-0001",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    transport = ProviderTransport(cassette=world, cassette_version="v2", sender=send)
    route = {"provider": "openai", "path": "/v1/chat/completions", "origin": "http://unused"}
    body = {"model": "fixture", "max_tokens": 1, "messages": []}

    assert transport(route, body, {"Authorization": "Bearer dummy"})[1]["usage"]
    assert calls == ["upstream"]
    cassette = next((tmp_path / "cassettes").glob("*.json"))
    saved = cassette.read_text()
    assert "sk-test-do-not-persist-0001" not in saved

    replay = ProviderTransport(
        cassette=CassetteWorld(tmp_path / "cassettes", mode="replay", version="v2"),
        cassette_version="v2", sender=lambda *_: pytest.fail("replay called upstream"),
    )
    status, payload = replay(route, body, {})
    assert status == 200
    assert payload["answer"] == "[REDACTED]"


def test_cassette_version_mismatch_fails_closed(tmp_path):
    world = CassetteWorld(tmp_path / "cassettes", mode="record", version="v1")
    fingerprint = cassette_fingerprint("openai", "/chat", {"q": "hi"}, "v1")
    world.exchange(fingerprint, {"q": "hi"}, lambda: (200, {"ok": True}))

    replay = CassetteWorld(tmp_path / "cassettes", mode="replay", version="v2")
    with pytest.raises(ValueError, match="version"):
        replay.exchange(fingerprint, {"q": "hi"}, lambda: (200, {}))


def test_provider_transport_stream_cassettes_redact_and_replay_without_upstream(tmp_path):
    world = CassetteWorld(tmp_path / "cassettes", mode="record", version="stream-v1")
    calls = []
    route = {"provider": "openai", "path": "/v1/chat/completions", "origin": "http://unused"}
    body = {"model": "fixture", "max_tokens": 1, "messages": [], "stream": True}
    chunks = [
        b'data: {"choices":[{"delta":{"content":"sk-test-do-not-persist-0001"}}]}\n\n',
        b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n',
    ]

    transport = ProviderTransport(
        cassette=world, cassette_version="stream-v1",
        sender=lambda *_: (calls.append("upstream") or (200, iter(chunks))),
    )
    status, stream = transport(route, body, {"Authorization": "Bearer dummy"})
    assert status == 200
    recorded = list(stream)
    assert b"[REDACTED]" in b"".join(recorded)
    assert calls == ["upstream"]
    cassette = next((tmp_path / "cassettes").glob("*.json"))
    assert "sk-test-do-not-persist-0001" not in cassette.read_text()

    replay = ProviderTransport(
        cassette=CassetteWorld(tmp_path / "cassettes", mode="replay", version="stream-v1"),
        cassette_version="stream-v1",
        sender=lambda *_: pytest.fail("stream replay called upstream"),
    )
    replay_status, replay_stream = replay(route, body, {})
    assert replay_status == 200
    assert b"[REDACTED]" in b"".join(replay_stream)
