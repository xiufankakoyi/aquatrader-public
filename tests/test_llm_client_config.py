import pytest

from core.utils.llm_client import _provider_api_key
from core.utils.llm_usage import LLMProviderConfig


def _provider(*, api_base: str, api_key: str = "") -> LLMProviderConfig:
    return LLMProviderConfig(
        name="test",
        api_base=api_base,
        api_key=api_key,
        model_name="test-model",
    )


def test_local_provider_without_key_uses_explicit_sdk_value() -> None:
    assert _provider_api_key(_provider(api_base="http://127.0.0.1:1234/v1")) == "not-needed"


def test_remote_provider_without_key_fails_fast() -> None:
    with pytest.raises(ValueError, match="no API key"):
        _provider_api_key(_provider(api_base="https://llm.example.com/v1"))


def test_configured_provider_key_is_preserved() -> None:
    assert _provider_api_key(
        _provider(api_base="https://llm.example.com/v1", api_key="test-configured-api-key")
    ) == "test-configured-api-key"
