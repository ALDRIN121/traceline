"""Recording egress uses synthetic traffic, never real provider credentials."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from llm_agent_eval.egress.ca import InstallCA
from llm_agent_eval.egress.recording import Budget, EgressDenied, ProviderRoute, RecordingEgress


def route(**changes):
    values = dict(input_token_bound=lambda body: 100)
    values.update(changes)
    return ProviderRoute(host="provider.example", path="/v1/chat/completions", model="fixture-model",
                         secret_ref="fixture-ref", dummy_key="dummy-case-key", max_input_tokens=100,
                         max_output_tokens=20, input_micros_per_token=2, output_micros_per_token=3,
                         price_version="fixture-v1", **values)


def test_ca_persists_identity_and_private_permissions(tmp_path):
    ca = InstallCA.load_or_create(tmp_path / "install")
    again = InstallCA.load_or_create(tmp_path / "install")
    assert ca.certificate_pem == again.certificate_pem
    assert (tmp_path / "install" / "interception-ca.pem").stat().st_mode & 0o777 == 0o600
    assert ca.context_for("provider.example").minimum_version.name == "TLSv1_2"


def test_ca_rejects_repository_and_corruption(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="repository"):
        InstallCA.load_or_create(tmp_path / "repo" / "state")
    state = tmp_path / "install"
    InstallCA.load_or_create(state)
    (state / "interception-ca.pem").write_bytes(b"invalid")
    with pytest.raises(ValueError, match="invalid"):
        InstallCA.load_or_create(state)


def test_budget_reservation_is_atomic_and_unknown_usage_closes_budget():
    budget = Budget(260)
    def reserve(_):
        try:
            return budget.reserve(260)
        except EgressDenied:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(pool.map(reserve, range(8)))
    assert sum(value is not None for value in reservations) == 1
    reservation = next(value for value in reservations if value is not None)
    budget.uncertain(reservation)
    with pytest.raises(EgressDenied, match="unavailable"):
        budget.reserve(1)


def test_proxy_substitutes_credentials_and_records_only_redacted_metadata():
    records, outbound = [], []
    def send(config, body, headers):
        outbound.append((config, body, headers))
        return 200, {"choices": [{"message": {"content": "person@example.com real-fixture-secret"}}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    proxy = RecordingEgress(route(), Budget(500), lambda ref: "real-fixture-secret", records.append, send=send)
    response = proxy.forward("/v1/chat/completions", {"Authorization": "Bearer dummy-case-key"},
                             {"model": "fixture-model", "messages": [{"role": "user", "content": "PII person@example.com"}],
                              "max_tokens": 20})
    assert response[0] == 200
    assert outbound[0][2]["Authorization"] == "Bearer real-fixture-secret"
    saved = json.dumps(records)
    assert "person@example.com" not in saved
    assert "real-fixture-secret" not in saved
    assert "dummy-case-key" not in saved
    assert records[-1]["source"] == "proxy"
    assert records[-1]["cost_usd_micros"] == 32
    assert proxy.budget.spent == 32


@pytest.mark.parametrize("change", [{"stream": True}, {"model": "other"}, {"max_tokens": 21}, {"max_tokens": 0}])
def test_unsupported_traffic_denied_before_secret_resolution(change):
    def never(*args):
        pytest.fail("No secret or network access for denied traffic")
    proxy = RecordingEgress(route(), Budget(500), never, lambda record: None, send=never)
    body = {"model": "fixture-model", "max_tokens": 20, **change}
    with pytest.raises(EgressDenied):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"}, body)


def test_unknown_usage_stops_subsequent_provider_requests():
    proxy = RecordingEgress(route(), Budget(1000), lambda ref: "fixture", lambda record: None,
                             send=lambda *args: (200, {"choices": []}))
    with pytest.raises(EgressDenied, match="usage"):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"}, {"model": "fixture-model", "max_tokens": 2})
    with pytest.raises(EgressDenied, match="unavailable"):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"}, {"model": "fixture-model", "max_tokens": 2})


def test_adapter_evidence_cannot_supply_provider_cost():
    records = []
    proxy = RecordingEgress(route(), Budget(500), lambda ref: "fixture", records.append)
    proxy.record_adapter({"type": "tool_call", "tool_name": "lookup", "cost_usd_micros": 0,
                          "payload": {"name": "private person@example.com"}, "source": "proxy"})
    assert records[0]["source"] == "adapter"
    assert records[0]["cost_usd_micros"] is None
    assert records[0]["type"] == "tool_call"
    assert "person@example.com" not in json.dumps(records)


def test_oversize_input_declared_in_tokens_is_denied_before_reservation():
    def never(*args):
        pytest.fail("denied input must not reserve or resolve anything")
    # Route limit is 100; the trusted provider counter bounds this request at 101.
    proxy = RecordingEgress(route(input_token_bound=lambda body: 101), Budget(10_000_000),
                            never, lambda record: None, send=never)
    with pytest.raises(EgressDenied, match="input_tokens"):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"},
                      {"model": "fixture-model", "max_tokens": 20})


def test_worst_case_reservation_uses_input_bound_not_byte_quotient():
    records, budget = [], Budget(258)
    captured = {}
    def send(config, body, headers):
        with pytest.raises(EgressDenied, match="exceeded"):
            budget.reserve(1)
        return 200, {"usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    def bound(body):
        captured["called"] = True
        return 99
    proxy = RecordingEgress(route(input_token_bound=bound), budget,
                            lambda ref: "fixture", records.append, send=send)
    proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"},
                  {"model": "fixture-model", "messages": ["x" * 8000], "max_tokens": 20})
    assert captured.get("called")
    # The whole 258-micro bound was reserved during send, then reconciled.
    assert proxy.budget.spent == 32


def test_unsupported_input_bound_fails_closed():
    with pytest.raises(ValueError, match="input_token_bound"):
        route(input_token_bound="bytes//4")


@pytest.mark.parametrize("counter", [None, lambda body: None, lambda body: True,
                                    lambda body: -1, lambda body: 1.5])
def test_unavailable_or_invalid_bound_never_resolves_credentials(counter):
    def never(*args):
        pytest.fail("unsupported requests cannot resolve credentials or leave the proxy")
    proxy = RecordingEgress(route(input_token_bound=counter), Budget(1000), never,
                            lambda record: None, send=never)
    with pytest.raises(EgressDenied, match="input_token_bound_unavailable"):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"},
                      {"model": "fixture-model", "max_tokens": 20})


@pytest.mark.parametrize("payload", [[], "not JSON object",
    {"usage": {"prompt_tokens": 1.5, "completion_tokens": 1}},
    {"usage": {"prompt_tokens": True, "completion_tokens": 1}}])
def test_unverifiable_usage_closes_budget(payload):
    budget = Budget(1000)
    records = []
    proxy = RecordingEgress(route(), budget, lambda ref: "fixture", records.append,
                            send=lambda *args: (200, payload))
    with pytest.raises(EgressDenied, match="usage"):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"},
                      {"model": "fixture-model", "max_tokens": 20})
    with pytest.raises(EgressDenied, match="unavailable"):
        budget.reserve(1)
    assert records[0]["cost_usd_micros"] is None


@pytest.mark.parametrize("usage", [{"prompt_tokens": 101, "completion_tokens": 1},
                                   {"prompt_tokens": 1, "completion_tokens": 21}])
def test_usage_above_route_maxima_closes_budget_and_preserves_observed_cost(usage):
    """A violated bound stops forwarding; observed billed usage is still recorded."""
    budget = Budget(10_000)
    records = []
    proxy = RecordingEgress(route(), budget, lambda ref: "fixture", records.append,
                            send=lambda *args: (200, {"usage": usage}))
    with pytest.raises(EgressDenied, match="usage"):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"},
                      {"model": "fixture-model", "max_tokens": 20})
    with pytest.raises(EgressDenied, match="unavailable"):
        budget.reserve(1)
    expected_cost = usage["prompt_tokens"] * 2 + usage["completion_tokens"] * 3
    assert records[0]["cost_usd_micros"] == expected_cost
    assert budget.spent == expected_cost


@pytest.mark.parametrize("error", [OSError("connection reset"), EgressDenied("late transport denial")])
def test_transport_exception_treats_spend_as_uncertain(error):
    records = []
    budget = Budget(1000)
    def send(config, body, headers):
        raise error
    proxy = RecordingEgress(route(), budget, lambda ref: "fixture", records.append, send=send)
    with pytest.raises(EgressDenied, match="unreachable"):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"},
                      {"model": "fixture-model", "max_tokens": 20})
    assert budget.spent == 260  # conservatively charged reservation, not observed usage
    assert records[0]["cost_usd_micros"] is None
    with pytest.raises(EgressDenied, match="unavailable"):
        proxy.forward(route().path, {"Authorization": "Bearer dummy-case-key"},
                      {"model": "fixture-model", "max_tokens": 20})
