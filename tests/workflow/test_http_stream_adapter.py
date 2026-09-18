from __future__ import annotations

from llm_agent_eval.targets.http_stream import HttpStreamAdapter


class FakeResponse:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_bytes(self):
        yield b"event: token\ndata: hi\n\nevent: final\ndata: {\"answer\": \"ok\"}\n\n"


class FakeClient:
    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def stream(self, *args, **kwargs):
        return FakeResponse()


def test_stream_adapter_records_first_token_and_final_output(monkeypatch):
    monkeypatch.setattr("llm_agent_eval.targets.http_stream.httpx.Client", FakeClient)
    result = HttpStreamAdapter().invoke({"mode": "stream", "url": "https://provider/stream"}, {"q": "hi"}, {})
    assert result.outcome == "ok"
    assert result.output == {"answer": "ok"}
    assert result.connector_observations["first_token"] is not None


def test_stream_adapter_stops_with_uncertain_cancel_before_final_frame(monkeypatch):
    class CancellableResponse(FakeResponse):
        def iter_bytes(self):
            yield b"event: token\ndata: hi\n\n"
            yield b"event: final\ndata: {\"answer\": \"ok\"}\n\n"

    class CancellableClient(FakeClient):
        def stream(self, *args, **kwargs):
            return CancellableResponse()

    monkeypatch.setattr("llm_agent_eval.targets.http_stream.httpx.Client", CancellableClient)
    checks = 0

    def should_cancel():
        nonlocal checks
        checks += 1
        return checks >= 2

    result = HttpStreamAdapter().invoke(
        {"mode": "stream", "url": "https://provider/stream"}, {"q": "hi"},
        {"should_cancel": should_cancel},
    )
    assert result.outcome == "cancelled"
    assert result.remote_uncertainty == "cancelled"
