from __future__ import annotations

import json

from core.research.integration.annual_report_operating_model import (
    AnnualReportOperatingModelStore,
)
from core.research.integration.p1_data_loader import P1DataLoader
from core.research.operating_model.repository import OperatingModelRepository


def _write_manifest(tmp_path, companies):
    path = tmp_path / "annual_report_evidence.json"
    path.write_text(json.dumps({"schema_version": "v1", "companies": companies}), encoding="utf-8")
    return path


def test_builds_real_segments_and_preserves_source_basis(tmp_path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "symbol": "600519.SH",
        "period": "2025-12-31",
        "published_at": "2026-04-17",
        "business_model": "manufacturing",
        "currency": "CNY",
        "reported_unit": "千元",
        "unit_multiplier": 1000,
        "source": {"provider": "cninfo", "url": "https://example/report.pdf"},
        "segment_total": 300,
        "segments_page": 10,
        "segments": [
            {"name": "甲", "revenue": 100},
            {"name": "乙", "revenue": 200},
        ],
        "forward_disclosure": {
            "page": 22,
            "text": "下一年度经营计划为定性披露，未披露可直接建模的数值幅度。",
        },
    }])

    model = AnnualReportOperatingModelStore(manifest).build("600519.SH", "2026-07-22")

    assert model is not None
    assert [segment.segment_name for segment in model.segments] == ["甲", "乙"]
    assert [segment.drivers[0].value for segment in model.segments] == [100_000, 200_000]
    assert model.segments[0].drivers[0].reported_value == 100
    assert model.segments[0].drivers[0].unit == "元"
    assert "pdf_page=10" in model.segments[0].drivers[0].source_ref
    assert model.assumptions[0].value is None
    assert "pdf_page=22" in model.assumptions[0].source_ref
    assert model.assumptions[0].basis == "disclosed_forward_plan"
    assert model.consistency_errors == []


def test_missing_or_unreconciled_segment_evidence_degrades_to_gap(tmp_path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "symbol": "TEST.SH",
        "period": "2025-12-31",
        "published_at": "2026-04-01",
        "business_model": "manufacturing",
        "currency": "CNY",
        "reported_unit": "元",
        "unit_multiplier": 1,
        "source": {"provider": "cninfo", "url": "https://example/report.pdf"},
        "segment_total": 100,
        "segments_page": 8,
        "segments": [{"name": "唯一分部", "revenue": 80}],
    }])

    model = AnnualReportOperatingModelStore(manifest).build("TEST.SH", "2026-07-22")

    assert model is not None
    assert model.segments == []
    assert model.gaps[0].blocker_kind == "segment_reconciliation_failed"
    assert "20.0000%" in model.gaps[0].note


def test_pit_rejects_report_published_after_as_of(tmp_path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "symbol": "TEST.SH",
        "period": "2025-12-31",
        "published_at": "2026-04-01",
        "business_model": "manufacturing",
        "currency": "CNY",
        "reported_unit": "元",
        "unit_multiplier": 1,
        "source": {"provider": "cninfo", "url": "https://example/report.pdf"},
        "segment_total": 100,
        "segments_page": 8,
        "segments": [{"name": "唯一分部", "revenue": 100}],
    }])

    assert AnnualReportOperatingModelStore(manifest).build("TEST.SH", "2026-03-31") is None


def test_pit_rejects_report_without_publication_date(tmp_path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "symbol": "TEST.SH",
        "period": "2025-12-31",
        "published_at": "",
        "segments": [],
    }])

    assert AnnualReportOperatingModelStore(manifest).build("TEST.SH", "2026-07-22") is None


def test_invalid_as_of_is_treated_as_unavailable_pit(tmp_path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "symbol": "TEST.SH",
        "period": "2025-12-31",
        "published_at": "2026-04-01",
    }])
    store = AnnualReportOperatingModelStore(manifest)

    assert store.build("TEST.SH", None) is None  # type: ignore[arg-type]
    assert store.build("TEST.SH", "") is None


def test_p1_loader_persists_local_annual_report_when_market_provider_is_down(tmp_path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "symbol": "TEST.SH",
        "period": "2025-12-31",
        "published_at": "2026-04-01",
        "business_model": "manufacturing",
        "currency": "CNY",
        "reported_unit": "元",
        "unit_multiplier": 1,
        "source": {"provider": "cninfo", "url": "https://example/report.pdf"},
        "segment_total": 100,
        "segments_page": 8,
        "segments": [{"name": "唯一分部", "revenue": 100}],
        "forward_disclosure": {"page": 9, "text": "仅有定性经营计划。"},
    }])

    class DownProvider:
        @staticmethod
        def is_available() -> bool:
            return False

    repo = OperatingModelRepository()
    result = P1DataLoader(
        operating_repo=repo,
        annual_report_store=AnnualReportOperatingModelStore(manifest),
        provider=DownProvider(),
    ).load_company("TEST.SH", "2026-07-22")

    assert result.success is True
    assert result.operating_models_loaded == 1
    assert result.data_source == "annual_report"
    assert repo.get_latest_model("TEST.SH").segments[0].segment_name == "唯一分部"


def test_corrupt_manifest_degrades_to_empty_store(tmp_path) -> None:
    manifest = tmp_path / "annual_report_evidence.json"
    manifest.write_text("{not-json", encoding="utf-8")

    store = AnnualReportOperatingModelStore(manifest)

    assert store.build("TEST.SH", "2026-07-22") is None
    assert store.load_errors == ["manifest_unreadable"]


def test_malformed_numeric_evidence_degrades_to_gaps(tmp_path) -> None:
    manifest = _write_manifest(tmp_path, [
        "invalid-row",
        {
            "symbol": "TEST.SH",
            "period": "2025-12-31",
            "published_at": "2026-04-01",
            "unit_multiplier": "not-a-number",
            "segments_page": "not-a-page",
            "segment_total": 100,
            "segments": [{"name": "唯一分部", "revenue": "not-a-number"}],
            "forward_disclosure": {"page": "not-a-page", "text": "定性计划"},
        },
    ])

    model = AnnualReportOperatingModelStore(manifest).build("TEST.SH", "2026-07-22")

    assert model is not None
    assert model.segments == []
    assert {gap.blocker_kind for gap in model.gaps} == {
        "segment_reconciliation_failed",
        "forward_evidence_missing",
    }
