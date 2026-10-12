from __future__ import annotations

import threading
import time

from infrastructure.data_providers.base import BaseMarketDataProvider
from infrastructure.data_providers.market.akshare_provider import (
    _periodic_report_categories,
    _report_type_from_category,
)
from infrastructure.data_providers.provider_registry import ProviderRegistry
from infrastructure.data_providers.research.cninfo_provider import _classify_periodic_report


class _FakeProvider(BaseMarketDataProvider):
    name = "fake"

    def __init__(
        self,
        *,
        periodic_reports: list[dict] | None = None,
        interactions: list[dict] | None = None,
    ) -> None:
        self._periodic_reports = periodic_reports or []
        self._interactions = interactions or []

    def is_available(self) -> bool:
        return True

    def get_periodic_reports(
        self,
        code: str,
        report_types: list[str] | None = None,
        count: int = 10,
    ) -> list[dict]:
        return self._periodic_reports[:count]

    def get_investor_interactions(self, code: str, count: int = 10) -> list[dict]:
        return self._interactions[:count]


def test_periodic_report_type_helpers() -> None:
    assert _classify_periodic_report("公司2025年年度报告") == "annual_report"
    assert _classify_periodic_report("公司2025年半年度报告") == "semiannual_report"
    assert _classify_periodic_report("公司2026年第一季度报告") == "quarterly_report"
    assert _classify_periodic_report("公司2025年年度报告摘要") == ""

    assert _periodic_report_categories(["annual_report"]) == ["年报"]
    assert _periodic_report_categories(["quarterly_report"]) == ["一季报", "三季报"]
    assert _report_type_from_category("半年报") == "semiannual_report"


def test_provider_registry_periodic_reports_failover() -> None:
    empty = _FakeProvider()
    empty.name = "cninfo"
    fallback = _FakeProvider(
        periodic_reports=[
            {
                "title": "公司2025年年度报告",
                "report_type": "annual_report",
                "pdf_url": "http://example.test/report.pdf",
            }
        ]
    )
    fallback.name = "akshare"

    registry = ProviderRegistry(providers=[empty, fallback])

    reports = registry.get_periodic_reports("000001", count=3)

    assert reports == fallback._periodic_reports


def test_provider_registry_investor_interactions() -> None:
    provider = _FakeProvider(
        interactions=[
            {
                "question": "公司相关产品是否用于数据中心？",
                "answer": "请以公告和定期报告披露为准。",
                "source": "cninfo_irm",
            }
        ]
    )
    provider.name = "akshare"
    registry = ProviderRegistry(providers=[provider])

    interactions = registry.get_investor_interactions("000001", count=3)

    assert len(interactions) == 1
    assert interactions[0]["source"] == "cninfo_irm"


def test_provider_registry_investor_interactions_times_out_and_opens_circuit() -> None:
    release = threading.Event()

    class _SlowProvider(_FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def get_investor_interactions(self, code: str, count: int = 10) -> list[dict]:
            self.calls += 1
            release.wait(1.0)
            return []

    provider = _SlowProvider()
    provider.name = "akshare"
    registry = ProviderRegistry(
        providers=[provider],
        interaction_timeout_seconds=0.05,
        interaction_circuit_seconds=1.0,
    )

    started_at = time.monotonic()
    assert registry.get_investor_interactions("000001", count=3) == []
    elapsed = time.monotonic() - started_at
    assert elapsed < 0.5

    assert registry.get_investor_interactions("000001", count=3) == []
    assert provider.calls == 1
    release.set()
