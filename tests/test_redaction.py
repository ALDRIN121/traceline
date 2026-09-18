"""Trusted redaction is applied before a persistence boundary is reached."""

from llm_agent_eval.redaction import REDACTION_VERSION, redact


SENTINEL = "sk-test-do-not-persist-0001"


def test_redact_removes_canary_from_nested_payload_headers_and_error_channels():
    payload = {
        "headers": {"Authorization": f"Bearer {SENTINEL}", "X-Api-Key": SENTINEL},
        "request": {"body": {"messages": [{"content": f"import token={SENTINEL}"}]}},
        "exception": RuntimeError(f"remote error: {SENTINEL}"),
        "import_log": f"cloned with {SENTINEL}",
        "trace": [{"payload": f"tool saw {SENTINEL}"}],
    }

    result = redact(payload)

    assert SENTINEL not in str(result.content)
    assert result.version == REDACTION_VERSION
    assert "canary" in result.detector_flags
    assert "header" in result.detector_flags
    assert result.content["headers"]["Authorization"] == "[REDACTED]"
    assert result.content["exception"] != str(payload["exception"])


def test_redact_bounds_retained_strings_without_losing_detector_evidence():
    result = redact({"log": "x" * 20_000 + " sk-test-do-not-persist-0001"}, max_string_bytes=128)

    assert len(result.content["log"].encode("utf-8")) <= 128
    assert SENTINEL not in result.content["log"]
    assert result.truncated is True
    assert "canary" in result.detector_flags


def test_redact_removes_unstructured_provider_tokens():
    result = redact({"provider_error": "request used sk-live-actual-provider-token-1234567890"})

    assert "sk-live-actual-provider-token-1234567890" not in str(result.content)
    assert "token" in result.detector_flags


def test_redact_preserves_nonsecret_control_plane_references():
    result = redact({"authorization_id": "auth-1", "plan_id": "plan-1", "secret_ref": "vault-1"})
    assert result.content == {
        "authorization_id": "auth-1", "plan_id": "plan-1", "secret_ref": "vault-1",
    }
