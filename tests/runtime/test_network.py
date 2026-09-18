import json

import pytest

from llm_agent_eval.runtime.network import PodmanRunNetwork
from llm_agent_eval.runtime.sandbox import _Command, SandboxDenied


class FakeSandbox:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def _command(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["network", "inspect"]:
            return _Command(0, json.dumps(self.payload).encode(), b"")
        return _Command(0, b"network", b"")


def test_network_is_labelled_internal_and_cleanup_is_explicit():
    run_id = "run-42"
    label = __import__("hashlib").sha256(run_id.encode()).hexdigest()
    fake = FakeSandbox([{"Labels": {
        "llm-agent-eval.managed": "true",
        "llm-agent-eval.run": label,
    }, "Internal": True}])
    network = PodmanRunNetwork(fake)

    lease = network.create(run_id)

    assert lease.name == network.name_for(run_id)
    assert fake.calls[0][:3] == ["network", "create", "--internal"]
    assert network.remove(lease)["state"] == "removed"


def test_unlabelled_network_is_rejected():
    fake = FakeSandbox([{"Labels": {}, "Internal": True}])
    network = PodmanRunNetwork(fake)
    with pytest.raises(SandboxDenied):
        network.validate(network.name_for("run-1"), "run-1")
