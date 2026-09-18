"""Small provider usage decoders; unsupported shapes fail closed."""

from __future__ import annotations

from collections.abc import Mapping


def normalize_usage(provider: str, payload: Mapping) -> dict[str, int]:
    usage = payload.get("usage") if provider in {"openai", "anthropic"} else payload.get("usageMetadata")
    if not isinstance(usage, Mapping):
        raise ValueError("usage_unavailable")
    if provider == "openai":
        fields = ("prompt_tokens", "completion_tokens")
    elif provider == "anthropic":
        fields = ("input_tokens", "output_tokens")
    elif provider == "google":
        fields = ("promptTokenCount", "candidatesTokenCount")
    else:
        raise ValueError("provider_decoder_unsupported")
    values = [usage.get(field) for field in fields]
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("usage_unavailable")
    return {"prompt_tokens": values[0], "completion_tokens": values[1]}
