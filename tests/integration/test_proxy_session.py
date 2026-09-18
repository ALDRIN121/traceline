from llm_agent_eval.egress.recording import Budget, ProviderRoute
from llm_agent_eval.egress.session import ProxyRunSession
from llm_agent_eval.runtime.network import NetworkLease


class FakeNetwork:
    def create(self, run_id):
        return NetworkLease("llm-agent-eval-run-managed", run_id)

    def remove(self, lease):
        return {"state": "removed", "name": lease.name}


def test_proxy_session_owns_ca_listener_and_network(tmp_path):
    route = ProviderRoute(
        host="provider.example", path="/v1/chat/completions", model="fixture",
        secret_ref="secret", dummy_key="dummy", max_input_tokens=10,
        max_output_tokens=10, input_micros_per_token=1,
        output_micros_per_token=1, price_version="v1",
        input_token_bound=lambda body: 1,
    )
    session = ProxyRunSession(
        run_id="run-1", install_root=tmp_path / "install", routes=[route],
        budget=Budget(100), secret_resolver=lambda ref: "real",
        record=lambda item: None, network=FakeNetwork(),
    )

    info = session.start()
    assert info.proxy_endpoint.startswith("http://host.containers.internal:")
    assert info.ca_cert == tmp_path / "install" / "interception-ca.pem"
    assert info.ca_cert.is_file()
    result = session.stop()
    assert result["proxy"]["state"] == "stopped"
    assert result["network"]["state"] == "removed"
