"""Smoke test the ten manually reviewed 2025 annual-report operating models."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.research.integration.annual_report_operating_model import AnnualReportOperatingModelStore
from core.research.integration.p1_data_loader import COMPANY_SAMPLE, P1DataLoader
from core.research.operating_model.repository import OperatingModelRepository
from core.research.valuation.repository import ValuationRepository


DEFAULT_ROOT = Path("data/research_control/p1_annual_reports/2025")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_pdf_text(text: str) -> str:
    return re.sub(r"[\s,]", "", text)


def _reported_number_pattern(value: object) -> re.Pattern[str] | None:
    """Build a numeric-token pattern tolerant of PDF grouping whitespace."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    negative = number < 0
    raw = format(abs(number), "f")
    integer, separator, fraction = raw.partition(".")
    integer_pattern = r"[,\s]*".join(re.escape(char) for char in integer)
    if separator and fraction:
        fraction_pattern = r"\s*".join(re.escape(char) for char in fraction)
        numeric = rf"{integer_pattern}\s*\.\s*{fraction_pattern}0*"
        end_boundary = r"(?![^\W\d_]|[_.])"
    else:
        numeric = rf"{integer_pattern}(?:\s*\.\s*0+)?"
        end_boundary = r"(?![\w.])"
    if negative:
        numeric = rf"(?:-\s*{numeric}|\(\s*{numeric}\s*\))"
    return re.compile(rf"(?<![\w.]){numeric}{end_boundary}")


def _find_reported_number(text: str, value: object, start: int = 0) -> re.Match[str] | None:
    pattern = _reported_number_pattern(value)
    return pattern.search(text, pos=start) if pattern else None


def _reported_number_is_present(text: str, value: object) -> bool:
    return _find_reported_number(text, value) is not None


def _normalise_label(value: object) -> str:
    return re.sub(r"\s", "", str(value or ""))


def _segment_evidence_checks(page_text: str, evidence: dict[str, object]) -> list[bool]:
    """Bind every reported segment value to its PDF row or column evidence."""
    segments = list(evidence.get("segments") or [])
    lines = page_text.splitlines()
    if evidence.get("segment_evidence_mode") == "columnar_row":
        anchor = _normalise_label(evidence.get("segment_value_row_anchor"))
        row = next((line for line in lines if anchor and anchor in _normalise_label(line)), "")
        page_compact = _normalise_label(page_text)
        labels_present = True
        for segment in segments:
            label_parts = segment.get("pdf_label_parts") or [
                segment.get("pdf_label") or segment.get("name")
            ]
            labels_present = labels_present and all(
                bool(_normalise_label(part)) and _normalise_label(part) in page_compact
                for part in label_parts
            )
        position = 0
        values_in_order = bool(row)
        for segment in segments:
            match = _find_reported_number(row, segment.get("revenue"), start=position)
            if match is None:
                values_in_order = False
                break
            position = match.end()
        passed = bool(segments) and labels_present and values_in_order
        return [passed for _ in segments]

    checks: list[bool] = []
    for segment in segments:
        label = _normalise_label(segment.get("pdf_label") or segment.get("name"))
        checks.append(any(
            label and label in _normalise_label(line)
            and _reported_number_is_present(line, segment.get("revenue"))
            for line in lines
        ))
    return checks


def _peer_basis_audit(
    peers: list[dict[str, object]],
    target: dict[str, object],
    as_of: str,
    *,
    requested: bool,
) -> dict[str, object]:
    """Verify comparable rows or an explicit no-conclusion degradation."""
    if not requested:
        return {"peer_basis_verified": True, "peer_comparison_status": "not_requested"}
    target_structure = str(target.get("business_structure") or "")
    target_structure_ref = str(target.get("business_structure_source_ref") or "")
    if not target_structure or not target_structure_ref:
        return {
            "peer_basis_verified": not peers,
            "peer_comparison_status": (
                "not_generated_missing_business_structure_evidence"
                if not peers else "invalid_unsourced_peers"
            ),
        }
    if not peers:
        return {"peer_basis_verified": True, "peer_comparison_status": "no_qualified_peer"}

    required_basis = "same_industry_business_structure_and_market_cap_0.25x_to_4x"
    verified = True
    for peer in peers:
        try:
            scale_ratio = float(peer.get("scale_ratio") or 0)
        except (TypeError, ValueError, OverflowError):
            scale_ratio = 0.0
        verified = verified and (
            peer.get("industry") == target.get("industry")
            and peer.get("business_structure") == target_structure
            and bool(peer.get("business_structure_source_ref"))
            and peer.get("target_business_structure_source_ref") == target_structure_ref
            and 0.25 <= scale_ratio <= 4.0
            and bool(str(peer.get("as_of") or ""))
            and str(peer.get("as_of") or "") <= as_of[:10]
            and peer.get("multiple_type") == "pe_ttm"
            and peer.get("currency") == "CNY"
            and peer.get("accounting_standard") == "PRC_ASBE"
            and peer.get("selection_basis") == required_basis
            and bool(peer.get("source_ref"))
        )
    return {
        "peer_basis_verified": verified,
        "peer_comparison_status": "qualified_peers" if verified else "invalid_peer_basis",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--as-of", default="2026-07-22")
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT / "smoke_audit.json")
    parser.add_argument("--valuation-db", type=Path, default=DEFAULT_ROOT / "smoke_valuation.db")
    parser.add_argument("--with-market-peers", action="store_true")
    args = parser.parse_args()

    store = AnnualReportOperatingModelStore(args.root / "operating_model_evidence.json")
    operating_repo = OperatingModelRepository()
    valuation_repo = ValuationRepository(args.valuation_db) if args.with_market_peers else None
    loader = P1DataLoader(
        operating_repo=operating_repo,
        annual_report_store=store,
        valuation_repo=valuation_repo,
        peer_universe=COMPANY_SAMPLE if args.with_market_peers else None,
    )

    rows = []
    manifest_errors = list(store.load_errors)
    if any(not isinstance(item, dict) for item in store._companies):
        manifest_errors.append("manifest_company_row_invalid")
    if not store._companies and not manifest_errors:
        manifest_errors.append("manifest_empty")
    failures = list(manifest_errors)
    for evidence in (item for item in store._companies if isinstance(item, dict)):
        symbol = evidence["symbol"]
        result = loader.load_company(symbol, args.as_of)
        model = operating_repo.get_latest_model(symbol, as_of=args.as_of)
        local_pdf = args.root / evidence["source"]["local_pdf"]
        pdf_reader = PdfReader(str(local_pdf)) if local_pdf.is_file() else None
        segment_page_text = ""
        forward_page_text = ""
        if pdf_reader is not None:
            page = pdf_reader.pages[int(evidence["segments_page"]) - 1]
            segment_page_text = page.extract_text(extraction_mode="layout") or ""
            forward_page_text = pdf_reader.pages[
                int(evidence["forward_disclosure"]["page"]) - 1
            ].extract_text() or ""
        segment_value_checks = _segment_evidence_checks(segment_page_text, evidence)
        forward_anchor = str(evidence["forward_disclosure"].get("anchor") or "")
        peers = []
        if valuation_repo is not None:
            cases = valuation_repo.list_cases(company_symbol=symbol, as_of=args.as_of)
            if cases:
                peers = cases[0].get("inputs_snapshot", {}).get("comparable_companies", []) or []
        target_peer_metadata = next(
            (item for item in COMPANY_SAMPLE if item.get("symbol") == symbol), {}
        )
        peer_audit = _peer_basis_audit(
            peers, target_peer_metadata, args.as_of, requested=args.with_market_peers
        )
        row = {
            "symbol": symbol,
            "company_name": evidence.get("company_name", ""),
            "period": evidence["period"],
            "pdf": str(local_pdf),
            "pdf_exists": local_pdf.is_file(),
            "pdf_sha256": _sha256(local_pdf) if local_pdf.is_file() else "",
            "segments_page": evidence["segments_page"],
            "segment_value_checks": segment_value_checks,
            "segment_values_verified": bool(segment_value_checks) and all(segment_value_checks),
            "segment_count": len(model.segments) if model else 0,
            "segment_source_refs": sorted({
                driver.source_ref for segment in (model.segments if model else [])
                for driver in segment.drivers
            }),
            "assumption_source_refs": [item.source_ref for item in (model.assumptions if model else [])],
            "forward_anchor": forward_anchor,
            "forward_anchor_verified": bool(forward_anchor and forward_anchor in forward_page_text),
            "gaps": [gap.to_dict() for gap in (model.gaps if model else [])],
            "consistency_errors": list(model.consistency_errors if model else ["model_missing"]),
            "peer_count": len(peers),
            "peers": peers,
            **peer_audit,
            "load_result": result.to_dict(),
        }
        if (
            not row["pdf_exists"] or not model or not row["segment_count"]
            or not row["segment_values_verified"] or not row["forward_anchor_verified"]
            or row["consistency_errors"]
            or not row["peer_basis_verified"]
        ):
            failures.append(symbol)
        rows.append(row)

    payload = {
        "as_of": args.as_of,
        "market_peers_requested": args.with_market_peers,
        "manifest_errors": manifest_errors,
        "companies_checked": len(rows),
        "companies_passed": sum(row["symbol"] not in failures for row in rows),
        "failures": failures,
        "companies": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "companies_checked": payload["companies_checked"],
        "companies_passed": payload["companies_passed"],
        "peer_counts": {row["symbol"]: row["peer_count"] for row in rows},
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
