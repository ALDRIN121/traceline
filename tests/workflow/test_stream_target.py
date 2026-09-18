from __future__ import annotations

import pytest

from llm_agent_eval.streaming import SSEParser, StreamProtocolError


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
