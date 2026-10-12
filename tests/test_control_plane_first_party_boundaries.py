from __future__ import annotations

import time

from core.research.control_plane.first_party_retriever import (
    CompanyIRSiteProvider,
    FirstPartyRetriever,
)
from core.research.perilla.discovery.models import SourceDocument


class _SlowProvider:
    name = "slow"

    def search(self, query: str, *, limit: int = 5) -> list[SourceDocument]:
        time.sleep(0.2)
        return [SourceDocument(content_text="late result")]


class _StringHealthProvider:
    name = "string_health"

    def health(self) -> dict[str, str]:
        return {"available": "false", "error_code": "offline"}

    def search(self, query: str, *, limit: int = 5) -> list[SourceDocument]:
        raise AssertionError("unavailable provider must not be called")


def test_provider_timeout_is_enforced_without_blocking_search() -> None:
    retriever = FirstPartyRetriever(interactive_providers=[_SlowProvider()])
    retriever.timeout = 0.01

    started = time.monotonic()
    result = retriever.search("600000 测试")

    assert time.monotonic() - started < 0.15
    assert result.reason_code == "first_party_unavailable"
    assert result.source_status["slow"].error_code == "provider_timeout"


def test_health_string_false_skips_provider() -> None:
    result = FirstPartyRetriever(
        interactive_providers=[_StringHealthProvider()]
    ).search("600000 测试")

    assert result.documents == []
    assert result.skipped_providers == ["string_health"]
    assert result.source_status["string_health"].status == "unavailable"


def test_malformed_document_and_duplicate_provider_are_ignored() -> None:
    class Provider:
        name = ["invalid"]

        def search(self, query: str, *, limit: int = 5) -> list[object]:
            return [SourceDocument(), SourceDocument(content_text="valid")]

    provider = Provider()
    result = FirstPartyRetriever(
        interactive_providers=[provider, provider]
    ).search("600000 测试")

    assert len(result.documents) == 1
    assert result.documents[0].content_text == "valid"
    assert list(result.source_status) == ["Provider"]


def test_company_resolver_failure_degrades_to_empty() -> None:
    provider = CompanyIRSiteProvider(resolver=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bad")))

    assert provider.search("600000 测试") == []
