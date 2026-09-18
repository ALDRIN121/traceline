from llm_agent_eval.egress.decoders import normalize_usage


def test_usage_decoders_normalize_supported_provider_shapes():
    assert normalize_usage("openai", {"usage": {"prompt_tokens": 2, "completion_tokens": 3}}) == {
        "prompt_tokens": 2, "completion_tokens": 3,
    }
    assert normalize_usage("anthropic", {"usage": {"input_tokens": 4, "output_tokens": 5}}) == {
        "prompt_tokens": 4, "completion_tokens": 5,
    }
    assert normalize_usage("google", {"usageMetadata": {
        "promptTokenCount": 6, "candidatesTokenCount": 7,
    }}) == {"prompt_tokens": 6, "completion_tokens": 7}
