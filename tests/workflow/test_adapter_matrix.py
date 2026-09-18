"""R2 adapter capability/conformance declarations fail closed."""

import pytest

from llm_agent_eval.targets.frameworks import adapter_capability, supported_adapters


def test_matrix_lists_only_explicitly_conformant_adapters():
    matrix = supported_adapters()
    assert matrix
    assert all(item.conformance_tests for item in matrix)
    assert all(item.framework != "*" for item in matrix)
    generic = adapter_capability("generic_http")
    assert generic.supported is True
    assert generic.capabilities["final_output"] == "observed"


def test_unknown_or_unverified_framework_is_honest_failure():
    unsupported = adapter_capability("langgraph", version="0.2")
    assert unsupported.supported is False
    assert unsupported.reason == "unsupported_framework"
    with pytest.raises(ValueError, match="unsupported_framework"):
        adapter_capability("crewai", version="1.0", require_supported=True)
