"""Hosted JSON targets observe output only and never invent tool evidence."""


def test_remote_verification_does_not_invent_tool_evidence(platform):
    s = platform.seed("output_only_api")
    r = platform.post(f"/api/targets/{s['target_id']}/verify", s["smoke_request"])
    v = platform.drain(r["body"]["job_id"])
    assert v["state"] == "verified"
    assert v["capabilities"]["final_output"] == "observed"
    assert v["capabilities"]["tool_execution"] == "unavailable"
    assert v["capabilities"]["provider_cost"] == "unavailable"


def test_expected_labels_cannot_be_mapped_into_the_request(platform):
    s = platform.seed("output_only_api")
    created = platform.post(
        f"/api/projects/{s['project_id']}/connections",
        {
            **s["connection"],
            "output_mapping": {"final_response": "/answer", "expected": "/gold"},
        },
    )
    assert created["http_status"] == 422
    assert created["body"]["error"]["code"] == "golden_mapping_forbidden"


def test_stateful_and_streaming_contracts_are_rejected(platform):
    s = platform.seed("output_only_api")
    created = platform.post(
        f"/api/projects/{s['project_id']}/connections",
        {**s["connection"], "mode": "streaming"},
    )
    assert created["http_status"] == 422
    assert created["body"]["error"]["code"] == "unsupported_target_mode"


def test_health_get_is_not_verification_success(platform):
    s = platform.seed("output_only_api")
    assert platform.invocation_count(s["target_id"]) == 0
    r = platform.post(f"/api/targets/{s['target_id']}/verify", s["smoke_request"])
    v = platform.drain(r["body"]["job_id"])
    assert v["state"] == "verified"
    assert platform.invocation_count(s["target_id"]) == 1
    assert all(item["path"] != "/health" for item in platform.target_calls(s["target_id"]))


def test_inline_credentials_are_rejected(platform):
    s = platform.seed("output_only_api")
    created = platform.post(
        f"/api/projects/{s['project_id']}/connections",
        {**s["connection"], "auth": {"type": "bearer", "token": "sk-inline"}},
    )
    assert created["http_status"] == 422
    assert created["body"]["error"]["code"] == "secret_ref_required"


def test_openapi_rejects_external_refs_and_requires_operation(platform):
    s = platform.seed("empty_project")
    external = platform.post(
        f"/api/projects/{s['project_id']}/openapi-imports",
        {
            "document": {
                "openapi": "3.0.0",
                "paths": {"/invoke": {"post": {"$ref": "https://evil.example/ops.json"}}},
            },
            "operation_id": "run",
        },
    )
    assert external["http_status"] == 422
    assert external["body"]["error"]["code"] == "openapi_external_ref"
    missing = platform.post(
        f"/api/projects/{s['project_id']}/openapi-imports",
        {"document": {"openapi": "3.0.0", "paths": {"/invoke": {"post": {"operationId": "run"}}}}},
    )
    assert missing["http_status"] == 422
    assert missing["body"]["error"]["code"] == "openapi_operation_required"
    selected = platform.post(
        f"/api/projects/{s['project_id']}/openapi-imports",
        {
            "document": {
                "openapi": "3.0.0",
                "paths": {"/invoke": {"post": {"operationId": "run"}}},
                "components": {"schemas": {"Out": {"$ref": "#/components/schemas/Out"}}},
            },
            "operation_id": "run",
        },
    )
    assert selected["http_status"] == 201
    assert selected["body"]["method"] == "POST"
    assert selected["body"]["path"] == "/invoke"
