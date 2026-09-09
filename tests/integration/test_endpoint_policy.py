"""Endpoint policy rejects private, metadata, and rebinding destinations."""

import pytest

from llm_agent_eval.targets.network_policy import EndpointPolicy, PolicyDenied


def test_production_policy_rejects_loopback_and_metadata():
    policy = EndpointPolicy()
    with pytest.raises(PolicyDenied) as denied:
        policy.authorize("http://127.0.0.1:9/invoke")
    assert denied.value.code == "disallowed_destination"
    with pytest.raises(PolicyDenied):
        policy.authorize("http://169.254.169.254/latest/meta-data/")


def test_exact_test_allowlist_permits_only_that_loopback_port():
    policy = EndpointPolicy(allow_exact={"127.0.0.1:9"})
    assert policy.authorize("http://127.0.0.1:9/invoke")["pinned_host"] == "127.0.0.1"
    with pytest.raises(PolicyDenied):
        policy.authorize("http://127.0.0.1:10/invoke")


def test_dns_rebinding_to_loopback_is_blocked(monkeypatch):
    policy = EndpointPolicy()

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(None, None, None, None, ("127.0.0.1", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(PolicyDenied) as denied:
        policy.authorize("http://agent.example/invoke")
    assert denied.value.code == "dns_rebinding"
