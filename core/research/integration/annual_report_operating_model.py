"""Build PIT-safe operating models from locally reviewed annual-report evidence."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.research.operating_model.contracts import (
    DRIVER_KIND_PRODUCT_REVENUE,
    ForwardAssumption,
    OperatingDriver,
    OperatingGap,
    OperatingModel,
    Segment,
)


class AnnualReportOperatingModelStore:
    """Read a structured evidence manifest and build ``OperatingModel`` objects.

    The manifest is deliberately separate from PDF extraction. Every numeric row has
    already been checked against a report page; this class only validates PIT, units,
    and segment reconciliation before admitting it to the domain model.
    """

    def __init__(self, manifest_path: str | Path) -> None:
        self.manifest_path = Path(manifest_path)
        self.load_errors: list[str] = []
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
            self.load_errors.append("manifest_unreadable")
        if not isinstance(payload, dict):
            payload = {}
            self.load_errors.append("manifest_root_invalid")
        self.schema_version = str(payload.get("schema_version") or "v1")
        companies = payload.get("companies") or []
        self._companies = list(companies) if isinstance(companies, list) else []

    @staticmethod
    def _source_ref(record: Mapping[str, Any], page: int) -> str:
        source = record.get("source") or {}
        if not isinstance(source, Mapping):
            source = {}
        provider = str(source.get("provider") or "annual_report")
        url = str(source.get("url") or "")
        return f"{provider}:{url}#pdf_page={page}"

    def build(self, symbol: str, as_of: str) -> OperatingModel | None:
        if not isinstance(as_of, str) or not as_of.strip():
            return None
        cutoff = as_of.strip()[:10]
        candidates = [
            row for row in self._companies
            if isinstance(row, Mapping)
            and row.get("symbol") == symbol
            and self._date_key(row.get("published_at"))
            and self._date_key(row.get("published_at")) <= cutoff
        ]
        if not candidates:
            return None
        record = max(
            candidates,
            key=lambda row: (
                self._date_key(row.get("period")),
                self._date_key(row.get("published_at")),
            ),
        )
        return self._build_record(record)

    @staticmethod
    def _date_key(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()[:10]

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _safe_page(value: Any) -> int:
        try:
            page = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return page if page > 0 else 0

    def _build_record(self, record: dict[str, Any]) -> OperatingModel:
        symbol = str(record.get("symbol") or "")
        period = str(record.get("period") or "")
        currency = str(record.get("currency") or "CNY")
        parsed_multiplier = self._safe_float(record.get("unit_multiplier", 1))
        multiplier = parsed_multiplier if parsed_multiplier and parsed_multiplier > 0 else 1.0
        reported_unit = str(record.get("reported_unit") or "元")
        source_page = self._safe_page(record.get("segments_page"))
        raw_segments = record.get("segments") or []
        segments_data = (
            list(raw_segments)
            if isinstance(raw_segments, (list, tuple))
            else []
        )
        source_ref = self._source_ref(record, source_page)
        trace_seed = f"{symbol}|{period}|{source_ref}|{self.schema_version}"
        model = OperatingModel(
            symbol=symbol,
            business_model=str(record.get("business_model") or "manufacturing"),
            period=period,
            currency=currency,
            unit="元",
            source_trace_id="ar_" + hashlib.sha256(trace_seed.encode("utf-8")).hexdigest()[:16],
            schema_version=f"operating_model_annual_report_{self.schema_version}",
        )

        parsed_segments: list[tuple[dict[str, Any], float]] = []
        for row in segments_data:
            if not isinstance(row, Mapping) or not str(row.get("name") or ""):
                continue
            reported_value = self._safe_float(row.get("revenue"))
            if reported_value is not None:
                parsed_segments.append((row, reported_value))
        total_value = self._safe_float(record.get("segment_total"))
        usable = bool(
            source_page
            and segments_data
            and len(parsed_segments) == len(segments_data)
            and total_value is not None
            and parsed_multiplier is not None
            and parsed_multiplier > 0
        )
        segment_sum = sum(value for _, value in parsed_segments)
        total_value = total_value if total_value is not None else 0.0
        relative_error = abs(segment_sum - total_value) / abs(total_value) if total_value else float("inf")
        if not usable or relative_error > 0.001:
            model.gaps.append(OperatingGap(
                name="年报分部收入",
                kind=DRIVER_KIND_PRODUCT_REVENUE,
                period=period,
                reason="annual_report_segment_evidence_unusable",
                blocker_kind="segment_reconciliation_failed",
                note=f"分部合计与披露总额差异 {relative_error:.4%}；未接入未勾稽数值。",
            ))
        else:
            for row, reported_value in parsed_segments:
                name = str(row.get("name") or "")
                driver = OperatingDriver(
                    name=f"{name}收入",
                    kind=DRIVER_KIND_PRODUCT_REVENUE,
                    value=reported_value * multiplier,
                    unit="元",
                    currency=currency,
                    period=period,
                    source_ref=source_ref,
                    observation_kind="historical_disclosed",
                    grounding_status="grounding_ok",
                    reported_value=reported_value,
                    segment_name=name,
                    note=f"原始披露单位：{reported_unit}；按 {multiplier:g} 倍换算为元。",
                )
                model.segments.append(Segment(
                    segment_name=name,
                    products=(
                        list(row.get("products") or [])
                        if isinstance(row.get("products") or [], list) else []
                    ),
                    drivers=[driver],
                ))

        forward = record.get("forward_disclosure") or {}
        if not isinstance(forward, Mapping):
            forward = {}
        forward_page = self._safe_page(forward.get("page"))
        if forward_page and forward.get("text"):
            model.assumptions.append(ForwardAssumption(
                name="管理层下一年度经营计划（定性）",
                kind=DRIVER_KIND_PRODUCT_REVENUE,
                value=None,
                unit="元",
                currency=currency,
                period=str(forward.get("period") or ""),
                source_ref=self._source_ref(record, forward_page),
                assumption_text=str(forward["text"]),
                basis="disclosed_forward_plan",
                note="仅保留披露来源；未将定性表述转换为数值增速。",
            ))
        else:
            model.gaps.append(OperatingGap(
                name="下一年度经营假设",
                kind=DRIVER_KIND_PRODUCT_REVENUE,
                period="",
                reason="no_forward_disclosure_source",
                blocker_kind="forward_evidence_missing",
                note="年报证据清单未登记可引用的经营计划页。",
            ))

        model.consistency_errors = model.validate_consistency()
        return model
