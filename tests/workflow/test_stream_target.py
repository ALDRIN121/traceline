from __future__ import annotations

import pytest

from llm_agent_eval.streaming import SSEParser, StreamProtocolError
from llm_agent_eval.targets.http_stream import HttpStreamAdapter
from llm_agent_eval.targets.network_policy import EndpointPolicy


def test_stream_adapter_emits_adapter_timing_evidence(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = b'event: token\ndata: hi\n\nevent: final\ndata: {"answer":"ok"}\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/stream"
        result = HttpStreamAdapter(
            policy=EndpointPolicy(allow_exact={f"127.0.0.1:{server.server_port}"})
        ).invoke(
            {"target": {"mode": "streaming", "url": url}},
            {"question": "hello"},
            {"run_id": "run", "workspace_id": "ws", "case_id": "case", "attempt_id": "attempt"},
        )
        assert result.outcome == "ok", result
        assert [event.type.value for event in result.trace_events] == [
            "stream_start", "first_token", "llm_response"
        ]
        assert all(event.source.value == "adapter" for event in result.trace_events)
        assert all(event.redaction_state.status == "clean" for event in result.trace_events)
    finally:
        server.shutdown()
        server.server_close()


def test_sse_parser_bounds_frames_and_handles_multiline_data():
    parser = SSEParser(max_frame_bytes=64, max_frames=2)
    frames = parser.feed(b"event: token\ndata: {\"x\":\n")
    assert frames == []
    frames = parser.feed(b"data: 1}\n\n")
    assert frames[0].event == "token"
    assert frames[0].data == '{"x":\n1}'


def test_sse_parser_rejects_oversized_and_malformed_frames():
    with pytest.raises(StreamProtocolError):
        SSEParser(max_frame_bytes=4).feed(b"data: too-long\n\n")
    with pytest.raises(StreamProtocolError):
        SSEParser().feed(b"not-a-field\n\n")
