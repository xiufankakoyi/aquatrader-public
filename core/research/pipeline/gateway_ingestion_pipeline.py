"""Gateway Ingestion Pipeline — standalone pipeline for ChinaStockDataGateway data ingestion.

Does NOT replace the main ResearchDataEnginePipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

# Project root
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, _PROJECT_ROOT)

from core.events.event_bus import EventBus
from core.events.event_journal import LocalEventJournal
from core.events.models import EventEnvelope, RawEvent, RawDocument
from data_svc.gateways.china_stock_data_gateway import ChinaStockDataGateway
from data_svc.ingestion.multisource_ingestion import (
    MultiSourceIngestion, GatewayIngestionConfig, GatewayIngestionResult,
)
from data_svc.ingestion.normalizer import Normalizer
from data_svc.ingestion.event_deduper import EventDeduper
from data_svc.ingestion.document_store import DocumentStore
from core.llm_processing.small_batch_processor import SmallBatchProcessor
from core.llm_processing.prompt_templates import PROMPT_VERSION
from core.research.evidence.evidence_center import EvidenceCenter
from core.research.evidence.evidence_quality import score_evidence


def _field(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _label(value: Any, default: str = "unknown") -> str:
    if isinstance(value, str):
        return value.strip() or default
    if value is None or isinstance(value, (list, tuple, set, Mapping)):
        return default
    return str(value).strip() or default


def _record(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    try:
        payload = asdict(item)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_symbols(symbols: Any) -> list[str]:
    if not isinstance(symbols, (list, tuple, set)):
        return []
    return list(dict.fromkeys(
        item.strip() for item in symbols
        if isinstance(item, str) and item.strip()
    ))


def _safe_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 10000) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_filename(value: Any, default: str = "unknown") -> str:
    label = _label(value, default)
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in label)


def run_pipeline(
    symbols: List[str],
    days: int = 30,
    processor_mode: str = "rule_based",
    llm_enabled: bool = False,
    llm_api_base: str = "",
    llm_model_name: str = "",
    max_llm_items: int = 5,
    max_chars_per_chunk: int = 2000,
    disable_cache: bool = False,
    prompt_version: str = PROMPT_VERSION,
) -> Dict[str, Any]:
    """Run the Gateway Ingestion Pipeline.

    Args:
        symbols: Stock codes to process.
        days: Lookback days.
        processor_mode: Processing mode rule_based/local_llm/hybrid.
        llm_enabled: Whether to enable LLM extraction.
        llm_api_base: LLM API base URL override.
        llm_model_name: LLM model name override.
        max_llm_items: Max items per LLM batch.
        max_chars_per_chunk: Max chars per text chunk.
        disable_cache: Whether to disable LLM cache.
        prompt_version: Prompt version for cache invalidation.

    Returns:
        Pipeline result dictionary.
    """
    symbols = _safe_symbols(symbols)
    days = _safe_int(days, 30, maximum=3660)
    max_llm_items = _safe_int(max_llm_items, 5, minimum=1, maximum=1000)
    max_chars_per_chunk = _safe_int(max_chars_per_chunk, 2000, minimum=100, maximum=100000)
    if processor_mode not in {"rule_based", "local_llm", "hybrid"}:
        processor_mode = "rule_based"
    t0 = time.time()
    report: Dict[str, Any] = {
        "pipeline": "gateway_ingestion",
        "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "symbols": symbols,
        "days": days,
        "steps": {},
        "errors": [],
    }

    # Step 1: Init Gateway
    gateway = ChinaStockDataGateway()
    journal = LocalEventJournal()
    bus = EventBus(journal=journal)
    config = GatewayIngestionConfig(symbols=symbols)
    msi = MultiSourceIngestion(gateway=gateway, gateway_config=config, event_bus=bus)

    # Step 2: Gateway collect
    ingestion_result = msi.collect_from_gateway(
        symbols=symbols,
        include_capabilities=config.include_capabilities,
        max_items_per_capability=config.max_items_per_capability,
    )
    report["steps"]["gateway_collect"] = {
        "success": ingestion_result.success,
        "batch_trace_id": ingestion_result.batch_trace_id,
        "raw_event_count": len(ingestion_result.raw_events),
        "raw_document_count": len(ingestion_result.raw_documents),
        "published_event_count": ingestion_result.published_event_count,
        "saved_document_count": ingestion_result.saved_document_count,
        "elapsed_ms": ingestion_result.elapsed_ms,
        "warnings": ingestion_result.warnings,
    }

    # Step 3: Publish RawEvent to EventBus (already done in collect_from_gateway)
    report["steps"]["eventbus_publish"] = {
        "published": ingestion_result.published_event_count,
        "note": "Events published during collect_from_gateway",
    }

    # Step 4: Normalize RawEvent
    normalizer = Normalizer()
    raw_events = (
        ingestion_result.raw_events
        if isinstance(ingestion_result.raw_events, (list, tuple))
        else []
    )
    raw_documents = (
        ingestion_result.raw_documents
        if isinstance(ingestion_result.raw_documents, (list, tuple))
        else []
    )
    normalized_events = normalizer.normalize(raw_events) if raw_events else []
    report["steps"]["normalize"] = {
        "input_count": len(raw_events),
        "output_count": len(normalized_events),
        "needs_llm_count": sum(1 for e in normalized_events if getattr(e, 'needs_llm', False)),
    }

    # Step 5: Deduplicate
    deduper = EventDeduper()
    unique_events, dedup_stats = deduper.dedupe(normalized_events) if normalized_events else ([], {})
    report["steps"]["dedup"] = {
        "input_count": len(normalized_events),
        "unique_count": dedup_stats.get("unique_count", len(unique_events)),
        "duplicate_count": dedup_stats.get("duplicate_count", 0),
        "possible_duplicate_count": dedup_stats.get("possible_duplicate_count", 0),
    }

    # Step 6: Save RawDocument to DocumentStore
    doc_store = DocumentStore()
    saved_docs = []
    for doc in raw_documents:
        try:
            saved = doc_store.save_document_from_raw_document(doc)
            saved_docs.append(saved)
        except Exception as exc:
            report["errors"].append(f"doc save error: {exc}")
    report["steps"]["document_store"] = {
        "input_count": len(raw_documents),
        "saved_count": len(saved_docs),
    }

    # Step 7: SmallBatchProcessor for needs_llm events
    effective_processor = processor_mode
    if llm_enabled and processor_mode == "rule_based":
        effective_processor = "hybrid"

    processor = SmallBatchProcessor(
        provider=effective_processor,
        llm_api_base=llm_api_base or None,
        max_llm_items=max_llm_items,
        max_chars_per_chunk=max_chars_per_chunk,
    )
    extraction_results = processor.process_batch(unique_events) if unique_events else []

    # v1.2: LLM 统计
    llm_stats = processor.llm_stats
    cv_stats = processor.cv_stats

    report["steps"]["small_batch_processor"] = {
        "input_count": len(unique_events),
        "extraction_count": len(extraction_results),
        "processor": processor.provider,
        "llm_enabled": llm_enabled,
        "llm_api_base": bool(processor.llm_api_base),
        "llm_model_name": processor.llm_model_name,
        "prompt_version": prompt_version,
        "llm_stats": llm_stats,
        "cross_validation": {
            "cross_validated": cv_stats.cross_validated,
            "rule_only": cv_stats.rule_only,
            "llm_only": cv_stats.llm_only,
            "total_evidence": cv_stats.total_evidence,
        },
    }

    # Step 8: Evidence Center collect
    ec = EvidenceCenter()
    trade_date = datetime.now().strftime("%Y-%m-%d")

    # Collect from normalized events (structured)
    ec.collect_from_normalized_events(unique_events, trade_date)

    # Collect from extraction results (unstructured)
    ec.collect_from_extraction_results(extraction_results, trade_date)

    # Collect from event bus
    ec.collect_from_event_bus(bus)

    # v1.2: 对所有证据计算六维质量评分
    for ev in ec.evidences:
        qs = score_evidence(
            evidence_type=ev.evidence_type,
            confidence=ev.extraction_confidence or ev.confidence,
            grounding_status=ev.grounding_status,
            cross_validation_status=ev.cross_validation_status,
            data_date=ev.data_date,
            evidence_text=ev.evidence_text,
        )
        ev.quality_score = qs.total
        ev.quality_strength = qs.strength
        if qs.strength == "strong":
            ev.evidence_strength = "strong"
        elif qs.strength == "weak":
            ev.evidence_strength = "weak"

    # Export evidence
    evidence_report = ec.export("evidence_center_gateway_latest.json")
    total_evidence = evidence_report.get("total", len(ec.evidences))
    report["steps"]["evidence_center"] = {
        "evidence_count": total_evidence,
        "structured_evidence_count": sum(1 for e in ec.evidences if not e.is_derived),
        "extracted_evidence_count": sum(1 for e in ec.evidences if e.is_derived),
        "missing_trace_count": sum(1 for e in ec.evidences if not e.trace_id),
        "missing_raw_event_count": sum(1 for e in ec.evidences if not e.raw_event_id and not e.event_id),
        "missing_document_count": sum(1 for e in ec.evidences if e.is_derived and not e.document_id),
        "missing_evidence_text_count": sum(1 for e in ec.evidences if e.is_derived and not e.evidence_text),
    }

    # Step 9: Export graph seed
    graph_seed = ec.export_with_graph_seed("gateway_graph_seed_latest.json")
    report["steps"]["graph_seed"] = {
        "entity_count": graph_seed.get("entity_count", 0),
        "relation_count": len(graph_seed.get("relations", [])),
    }

    # Step 10: Generate single-stock evidence packs
    evidence_packs = {}
    for symbol in symbols:
        pack = _generate_evidence_pack(ec, symbol, unique_events, saved_docs, extraction_results)
        evidence_packs[symbol] = pack
        _write_evidence_pack(symbol, pack)
    report["steps"]["evidence_packs"] = {
        "symbols": symbols,
        "pack_count": len(evidence_packs),
    }

    # Step 11: Write final report
    elapsed_ms = round((time.time() - t0) * 1000)
    report["elapsed_ms"] = elapsed_ms
    report["success"] = True
    report["completed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    _write_final_report(report)

    return report


def _generate_evidence_pack(
    ec: EvidenceCenter,
    symbol: str,
    normalized_events: list,
    documents: list,
    extraction_results: list,
) -> Dict[str, Any]:
    """Generate a single-stock evidence pack.

    Args:
        ec: EvidenceCenter instance with collected evidence.
        symbol: Stock code to filter.
        normalized_events: Deduplicated normalized events.
        documents: Saved documents.
        extraction_results: LLM/rule extraction results.

    Returns:
        Evidence pack dictionary.
    """
    # Filter events for this symbol
    symbol_events = [e for e in normalized_events if _field(e, "symbol") == symbol]
    symbol_docs = [d for d in documents if _field(d, "symbol") == symbol]
    symbol_extractions = [r for r in extraction_results if _field(r, "symbol") == symbol]

    # Filter evidence for this symbol (include market-level sector/theme evidence)
    symbol_evidence = [
        e for e in ec.evidences
        if _field(e, "symbol") == symbol
        or (_field(e, "evidence_type") in ("sector", "theme") and not _field(e, "symbol"))
    ]

    # Categorize evidence
    quote_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") in ("market_data", "quote_depth", "valuation")]
    kline_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") == "kline"]
    announcement_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") == "announcement"]
    report_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") == "report"]
    theme_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") == "theme"]
    sector_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") == "sector"]
    risk_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") == "risk"]
    moat_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") == "moat"]
    catalyst_evidence = [e for e in symbol_evidence if _field(e, "evidence_type") == "catalyst"]

    # Data source stats
    source_stats = {}
    for e in symbol_evidence:
        src = _label(_field(e, "provider", "unknown"))
        source_stats[src] = source_stats.get(src, 0) + 1

    # v1.2: 证据质量分布
    quality_distribution = {"strong": 0, "medium": 0, "weak": 0}
    for e in symbol_evidence:
        strength = _label(_field(e, "quality_strength") or _field(e, "evidence_strength", "medium"), "medium")
        quality_distribution[strength] = quality_distribution.get(strength, 0) + 1

    # v1.2: 交叉验证分布
    cv_distribution = {"cross_validated": 0, "rule_only": 0, "llm_only": 0}
    for e in symbol_evidence:
        cv = _label(_field(e, "cross_validation_status"), "")
        if cv in cv_distribution:
            cv_distribution[cv] += 1

    # v1.2: 弱证据/待人工复核列表
    weak_evidence = [e for e in symbol_evidence if _field(e, "quality_strength") == "weak" or _field(e, "evidence_strength") == "weak"]
    review_needed = [e for e in symbol_evidence if _field(e, "review_status") == "pending_human_review"]

    # v1.2: grounding_type 分布
    grounding_type_distribution = {}
    for e in symbol_evidence:
        gt = _label(_field(e, "grounding_type"), "not_applicable")
        grounding_type_distribution[gt] = grounding_type_distribution.get(gt, 0) + 1

    # Missing items
    missing = []
    if not quote_evidence:
        missing.append("行情数据")
    if not kline_evidence:
        missing.append("K线数据")
    if not announcement_evidence:
        missing.append("公告")
    if not report_evidence:
        missing.append("研报")

    return {
        "symbol": symbol,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "version": "v2",
        "data_source_stats": source_stats,
        "quality_distribution": quality_distribution,
        "cross_validation_distribution": cv_distribution,
        "grounding_type_distribution": grounding_type_distribution,
        "quote_evidence": [_record(e) for e in quote_evidence],
        "kline_evidence": [_record(e) for e in kline_evidence],
        "valuation_evidence": [],
        "announcement_evidence": [_record(e) for e in announcement_evidence],
        "report_evidence": [_record(e) for e in report_evidence],
        "theme_evidence": [_record(e) for e in theme_evidence],
        "sector_evidence": [_record(e) for e in sector_evidence],
        "risk_clues": [_record(e) for e in risk_evidence],
        "moat_clues": [_record(e) for e in moat_evidence],
        "catalyst_clues": [_record(e) for e in catalyst_evidence],
        "weak_evidence": [_record(e) for e in weak_evidence],
        "review_needed": [_record(e) for e in review_needed],
        "raw_event_trace": [{"event_id": _field(e, "event_id"), "event_type": _field(e, "event_type")} for e in symbol_events],
        "document_trace": [{"document_id": _field(d, "document_id"), "title": _field(d, "title")} for d in symbol_docs],
        "missing_items": missing,
        "disclaimer": "本 Evidence Pack 不是投资研报，不提供买卖建议。",
    }


def _write_evidence_pack(symbol: str, pack: Dict[str, Any]) -> None:
    """Write evidence pack to JSON and Markdown files.

    Args:
        symbol: Stock code (used in filename).
        pack: Evidence pack dictionary.
    """
    pack_dir = os.path.join(_PROJECT_ROOT, "data", "reports", "single_stock_evidence")
    os.makedirs(pack_dir, exist_ok=True)

    # JSON
    safe_symbol = _safe_filename(symbol)
    json_path = os.path.join(pack_dir, f"{safe_symbol}_evidence_pack.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=2, default=str)

    # MD
    md_path = os.path.join(pack_dir, f"{safe_symbol}_evidence_pack.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {symbol} Evidence Pack v2\n\n")
        f.write(f"**生成时间**: {pack['generated_at']}\n\n")
        f.write(f"**免责声明**: {pack['disclaimer']}\n\n")

        f.write("## 数据来源统计\n\n")
        for src, cnt in pack.get("data_source_stats", {}).items():
            f.write(f"- {src}: {cnt} 条\n")

        # v1.2: 质量分布
        qd = pack.get("quality_distribution", {})
        if qd:
            f.write("\n## 证据质量分布\n\n")
            for strength, cnt in qd.items():
                f.write(f"- {strength}: {cnt} 条\n")

        # v1.2: 交叉验证分布
        cvd = pack.get("cross_validation_distribution", {})
        if cvd and any(v > 0 for v in cvd.values()):
            f.write("\n## 交叉验证分布\n\n")
            for status, cnt in cvd.items():
                f.write(f"- {status}: {cnt} 条\n")

        # v1.2: 锚定类型分布
        gtd = pack.get("grounding_type_distribution", {})
        if gtd:
            f.write("\n## 锚定类型分布\n\n")
            for gt, cnt in gtd.items():
                f.write(f"- {gt}: {cnt} 条\n")

        for section_name, key in [
            ("行情证据", "quote_evidence"),
            ("K线证据", "kline_evidence"),
            ("公告证据", "announcement_evidence"),
            ("研报证据", "report_evidence"),
            ("题材证据", "theme_evidence"),
            ("板块证据", "sector_evidence"),
            ("风险线索", "risk_clues"),
            ("护城河线索", "moat_clues"),
            ("催化剂线索", "catalyst_clues"),
        ]:
            items = pack.get(key, [])
            f.write(f"\n## {section_name} ({len(items)})\n\n")
            for item in items[:5]:
                title = item.get("title", "")
                cv = item.get("cross_validation_status", "")
                gs = item.get("grounding_status", "")
                gt = item.get("grounding_type", "")
                qs = item.get("quality_strength", "")
                rs = item.get("review_status", "")
                suffix = ""
                if cv:
                    suffix += f" [{cv}]"
                if gt:
                    suffix += f" grounding_type={gt}"
                elif gs:
                    suffix += f" grounding={gs}"
                if qs:
                    suffix += f" quality={qs}"
                if rs:
                    suffix += f" review={rs}"
                f.write(f"- {title}{suffix}\n")

        # v1.2: 待人工复核
        review = pack.get("review_needed", [])
        if review:
            f.write(f"\n## 待人工复核 ({len(review)})\n\n")
            for item in review[:10]:
                gt = item.get("grounding_type", "")
                gt_note = f" (grounding_type={gt})" if gt else ""
                f.write(f"- {item.get('title', '')} (llm_only, confidence=low{gt_note})\n")

        if pack.get("missing_items"):
            f.write(f"\n## 缺失项\n\n")
            for m in pack["missing_items"]:
                f.write(f"- {m}\n")


def _write_final_report(report: Dict[str, Any]) -> None:
    """Write pipeline final report to JSON and Markdown files.

    Args:
        report: Pipeline result dictionary.
    """
    reports_dir = os.path.join(_PROJECT_ROOT, "data", "reports")
    os.makedirs(reports_dir, exist_ok=True)

    # JSON
    json_path = os.path.join(reports_dir, "gateway_ingestion_latest.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    # MD
    md_path = os.path.join(reports_dir, "gateway_ingestion_latest.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Gateway Ingestion Pipeline Report\n\n")
        f.write(f"**时间**: {report.get('started_at', '')}\n\n")
        f.write(f"**Symbols**: {', '.join(report.get('symbols', []))}\n\n")
        f.write(f"**耗时**: {report.get('elapsed_ms', 0)} ms\n\n")

        for step_name, step_data in report.get("steps", {}).items():
            f.write(f"## {step_name}\n\n")
            if isinstance(step_data, dict):
                for k, v in step_data.items():
                    if k != "warnings":
                        f.write(f"- {k}: {v}\n")
                if step_data.get("warnings"):
                    f.write(f"\n### Warnings\n\n")
                    for w in step_data["warnings"][:10]:
                        f.write(f"- {w}\n")
            f.write("\n")

        if report.get("errors"):
            f.write("## Errors\n\n")
            for e in report["errors"]:
                f.write(f"- {e}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gateway Ingestion Pipeline v1.2")
    parser.add_argument("--symbols", required=True, help="Comma-separated stock codes")
    parser.add_argument("--days", type=int, default=30, help="Lookback days")
    parser.add_argument("--processor", default="rule_based", choices=["rule_based", "local_llm", "hybrid"], help="Processing mode")
    parser.add_argument("--llm-enabled", action="store_true", help="Enable LLM extraction")
    parser.add_argument("--llm-api-base", default="", help="LLM API base URL override")
    parser.add_argument("--llm-model-name", default="", help="LLM model name override")
    parser.add_argument("--max-llm-items", type=int, default=5, help="Max items per LLM batch")
    parser.add_argument("--max-chars-per-chunk", type=int, default=2000, help="Max chars per text chunk")
    parser.add_argument("--disable-cache", action="store_true", help="Disable LLM cache")
    parser.add_argument("--prompt-version", default=PROMPT_VERSION, help="Prompt version")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    result = run_pipeline(
        symbols=symbols,
        days=args.days,
        processor_mode=args.processor,
        llm_enabled=args.llm_enabled,
        llm_api_base=args.llm_api_base,
        llm_model_name=args.llm_model_name,
        max_llm_items=args.max_llm_items,
        max_chars_per_chunk=args.max_chars_per_chunk,
        disable_cache=args.disable_cache,
        prompt_version=args.prompt_version,
    )

    print(f"\nPipeline completed: {'SUCCESS' if result.get('success') else 'FAILED'}")
    print(f"Elapsed: {result.get('elapsed_ms', 0)} ms")
