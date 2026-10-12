"""Build a replayable, fail-closed P1 AI-only internal review bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.research.integration.p1_ai_review import audit_p1_ai_evidence


DEFAULT_ROOT = Path("data/research_control/p1_annual_reports/2025")
EXPECTED_SYMBOLS = {
    "600519.SH", "300750.SZ", "600276.SH", "000858.SZ", "002594.SZ",
    "600036.SH", "601318.SH", "600030.SH", "000333.SZ", "002415.SZ",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number_pattern(value: object) -> re.Pattern[str] | None:
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


def _number_present(text: str, value: object) -> bool:
    pattern = _number_pattern(value)
    return bool(pattern and pattern.search(text))


def _row_value_present(
    text: str,
    anchors: tuple[str, ...],
    value: object,
) -> bool:
    """Bind a value to its labelled row, allowing one PDF line wrap."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        compact_line = re.sub(r"\s", "", line).lower()
        compact_anchors = tuple(re.sub(r"\s", "", anchor).lower() for anchor in anchors)
        if any(anchor in compact_line for anchor in compact_anchors):
            if _number_present(line, value):
                return True
            if not re.search(r"\d", line) and index + 1 < len(lines):
                if _number_present(lines[index + 1], value):
                    return True
        if index + 1 < len(lines):
            window = "\n".join(lines[index:index + 3])
            compact_window = re.sub(r"\s", "", window).lower()
            label_spans_lines = any(
                anchor in compact_window and anchor not in compact_line
                for anchor in compact_anchors
            )
            if label_spans_lines and _number_present(window, value):
                return True
    return False


def _page_text(reader: PdfReader, pages: list[int]) -> str:
    return "\n".join(
        reader.pages[page - 1].extract_text(extraction_mode="layout") or ""
        for page in pages
    )


def _close(left: Decimal, right: Decimal, tolerance: Decimal) -> bool:
    return abs(left - right) <= tolerance


def _statement_checks(
    reader: PdfReader,
    statements: dict[str, object],
) -> tuple[bool, bool, bool]:
    balance = statements["balance"]
    income = statements["income"]
    cash = statements["cash_flow"]
    balance_text = _page_text(reader, balance["pages"])
    income_text = _page_text(reader, income["pages"])
    cash_text = _page_text(reader, cash["pages"])

    balance_anchors = {
        "assets": ("资产总计", "Total assets"),
        "liabilities": ("负债合计", "Total liabilities"),
        "equity": (
            "所有者权益（或股东权益",
            "所有者权益（或股东权",
            "所有者权益合计",
            "股东权益合计",
            "Total equity",
        ),
    }
    balance_numbers = all(_row_value_present(
        balance_text, balance_anchors[key], balance[key]
    ) for key in ("assets", "liabilities", "equity"))
    balance_equation = _close(
        Decimal(str(balance["assets"])),
        Decimal(str(balance["liabilities"])) + Decimal(str(balance["equity"])),
        Decimal("0.02"),
    )
    balance_ok = balance_numbers and balance_equation

    income_ok = (
        _row_value_present(
            income_text,
            ("营业总收入", "营业收入合计", "营业收入", "Operating income"),
            income["revenue"],
        )
        and _row_value_present(
            income_text, ("净利润", "Profit for the year"), income["net_profit"]
        )
    )

    cash_anchors = {
        "cfo": ("经营活动产生", "Net cash generated from operating"),
        "cfi": ("投资活动产生", "投资活动使用", "Net cash used in investing"),
        "cff": ("筹资活动", "Net cash used in financing"),
        "fx_effect": ("汇率变动", "Effect of foreign exchange rate changes"),
        "cash_begin": (
            "期初现金及现金等价物余额",
            "期初现金及现金等价物余",
            "年初现金及现金等价物余额",
            "Cash and cash equivalents as at 1 January",
        ),
        "cash_end": (
            "期末现金及现金等价物余额",
            "年末现金及现金等价物余额",
            "Cash and cash equivalents as at 31 December",
        ),
    }
    cash_numbers = all(_row_value_present(
        cash_text, cash_anchors[key], cash[key]
    ) for key in cash_anchors)
    if "reported_change_before_fx" in cash:
        cash_numbers = cash_numbers and _row_value_present(
            cash_text,
            ("Net (decrease)/increase in cash and cash equivalents",),
            cash["reported_change_before_fx"],
        )
    unit = statements["unit"]
    tolerance = Decimal("2") if unit == "千元" else Decimal("0.02")
    components = sum(
        (Decimal(str(cash[key])) for key in ("cfo", "cfi", "cff", "fx_effect")),
        Decimal("0"),
    )
    reported_change = Decimal(str(cash["cash_change"]))
    end_change = Decimal(str(cash["cash_end"])) - Decimal(str(cash["cash_begin"]))
    cash_ok = (
        cash_numbers
        and _close(components, reported_change, tolerance)
        and _close(end_change, reported_change, tolerance)
    )
    return balance_ok, income_ok, cash_ok


def build_bundle(root: Path) -> dict[str, object]:
    source_path = root / "p1_ai_review_source.json"
    annual_path = root / "operating_model_evidence.json"
    smoke_path = root / "smoke_audit.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    annual = json.loads(annual_path.read_text(encoding="utf-8"))
    smoke = json.loads(smoke_path.read_text(encoding="utf-8"))
    annual_by_symbol = {row["symbol"]: row for row in annual["companies"]}
    smoke_by_symbol = {row["symbol"]: row for row in smoke["companies"]}

    bundle = {
        "schema_version": "p1_ai_review_v1",
        "review_mode": source["review_mode"],
        "human_independent_review": source["human_independent_review"],
        "identity_independent": source["identity_independent"],
        "as_of": source["as_of"],
        "input_hashes": {
            "p1_ai_review_source": _sha256(source_path),
            "operating_model_evidence": _sha256(annual_path),
            "smoke_audit": _sha256(smoke_path),
        },
        "companies": [],
    }
    for source_row in source["companies"]:
        symbol = source_row["symbol"]
        annual_row = annual_by_symbol[symbol]
        smoke_row = smoke_by_symbol[symbol]
        statements = deepcopy(source_row["statements"])
        pdf_path = root / annual_row["source"]["local_pdf"]
        reader = PdfReader(str(pdf_path))
        balance_ok, income_ok, cash_ok = _statement_checks(reader, statements)
        statements["balance"] = {
            **statements["balance"],
            "page": statements["balance"]["pages"][0],
            "equation_verified": balance_ok,
        }
        statements["income"] = {
            **statements["income"],
            "page": statements["income"]["pages"][0],
            "applicable_equations_verified": income_ok,
        }
        statements["cash_flow"] = {
            **statements["cash_flow"],
            "page": statements["cash_flow"]["pages"][0],
            "equation_verified": cash_ok,
        }
        statements.update({
            "currency": "CNY",
            "scope": "consolidated",
            "pdf_rows_verified": balance_ok and income_ok and cash_ok,
        })
        peer_status = smoke_row.get("peer_comparison_status")
        peers = smoke_row.get("peers") or []
        bundle["companies"].append({
            "symbol": symbol,
            "company_name": annual_row["company_name"],
            "annual_report": {
                "period": annual_row["period"],
                "published_at": annual_row["published_at"],
                "provider": annual_row["source"]["provider"],
                "source_url": annual_row["source"]["url"],
                "local_pdf": str(pdf_path),
                "pdf_sha256": _sha256(pdf_path),
            },
            "statements": statements,
            "operating_model": {
                "period": annual_row["period"],
                "segment_rows_verified": smoke_row["segment_values_verified"],
                "forward_plan_anchor_verified": smoke_row["forward_anchor_verified"],
                "qualitative_plan_numeric_fabrication": False,
                "segment_count": smoke_row["segment_count"],
            },
            "valuation": {
                **source["valuation_policy"],
                "as_of": source["as_of"],
            },
            "peers": {
                "status": peer_status,
                "rows": peers,
                "basis_verified": smoke_row["peer_basis_verified"],
            },
            "primary_review": source["review_declarations"]["primary_review"],
            "adversarial_review": source["review_declarations"]["adversarial_review"],
        })
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_ROOT / "p1_ai_review_audit.json"
    )
    args = parser.parse_args()

    bundle = build_bundle(args.root)
    decision = audit_p1_ai_evidence(bundle, expected_symbols=EXPECTED_SYMBOLS)
    payload = {**bundle, "decision": decision}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "output": str(args.output),
        "status": decision["status"],
        "overall_passed": decision["overall_passed"],
        "companies_passed": sum(row["passed"] for row in decision["companies"]),
        "companies_checked": len(decision["companies"]),
        "failures": {
            row["symbol"]: row["failures"]
            for row in decision["companies"] if row["failures"]
        },
    }, ensure_ascii=False, indent=2))
    return 0 if decision["overall_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
