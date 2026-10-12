"""Stable JSON CLI for AquaTrade's unified research-data router."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_svc.providers.source_router import SourceRouter


_QUERY_CAPABILITIES = {
    "news": "news",
    "report": "reports",
    "announce": "announcements",
    "valuation": "valuation",
    "finance": "finance_snapshot",
    "profile": "company_profile",
    "search": "semantic_search",
}


def _base_result(**overrides: Any) -> dict[str, Any]:
    return {
        "status": "empty",
        "selected_provider": "",
        "attempts": [],
        "events": [],
        "errors": [],
        **overrides,
    }


def _symbols(args: argparse.Namespace) -> list[str]:
    values = getattr(args, "symbols", []) or []
    return [
        symbol.strip()
        for value in values
        for symbol in str(value).split(",")
        if symbol.strip()
    ]


def execute(args: argparse.Namespace, router: SourceRouter) -> dict[str, Any]:
    """Execute one validated CLI request and return the stable result shape."""
    command = str(args.command)
    try:
        if command == "status":
            event = router.status(check_health=not bool(args.no_health))
            return _base_result(status="ok", events=[event])

        if command in {"kline", "quote"}:
            data_type = "daily" if command == "kline" else "realtime"
            attempt = (
                "unified_data_query.daily"
                if command == "kline"
                else "provider_registry.realtime"
            )
            routed = router.route(
                data_type,
                _symbols(args),
                start_date=getattr(args, "start_date", "") or None,
                end_date=getattr(args, "end_date", "") or None,
            )
            data = routed.get("data")
            if isinstance(data, pd.DataFrame):
                events = data.to_dict(orient="records")
            elif isinstance(data, list):
                events = [item for item in data if isinstance(item, dict)]
            elif isinstance(data, dict):
                events = [data]
            else:
                events = []
            status = "ok" if events else (
                "failed" if routed.get("status") == "error" else "empty"
            )
            errors = [str(routed.get("detail"))] if routed.get("detail") else []
            return _base_result(
                status=status,
                selected_provider=attempt.rsplit(".", 1)[0],
                attempts=[attempt],
                events=events,
                errors=errors,
            )

        capability = _QUERY_CAPABILITIES[command]
        queried = router.query(
            capability,
            symbols=_symbols(args),
            start_date=getattr(args, "start_date", "") or None,
            end_date=getattr(args, "end_date", "") or None,
            limit=int(getattr(args, "limit", 20) or 20),
            keyword=getattr(args, "keyword", "") or None,
            query=getattr(args, "query", "") or None,
        ).to_dict()
        status = queried.get("status", "empty")
        if status == "error":
            status = "failed"
        return _base_result(
            status=status,
            selected_provider=str(queried.get("selected_provider") or ""),
            attempts=list(queried.get("source_chain") or [capability]),
            events=list(queried.get("events") or []),
            errors=[str(item) for item in queried.get("errors") or []],
        )
    except Exception as exc:
        return _base_result(
            status="failed",
            attempts=[command],
            errors=[f"{type(exc).__name__}: {exc}"],
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AquaTrade 统一研究数据查询")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--no-health", action="store_true")

    for name in ("quote", "kline", "news", "report", "announce", "valuation", "finance", "profile"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--symbols", nargs="+", required=True)
        sub.add_argument("--start-date", default="")
        sub.add_argument("--end-date", default="")
        sub.add_argument("--limit", type=int, default=20)
        sub.add_argument("--keyword", default="")
        sub.add_argument("--query", default="")
    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--symbols", nargs="*", default=[])
    search.add_argument("--start-date", default="")
    search.add_argument("--end-date", default="")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--keyword", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = execute(args, SourceRouter())
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] in {"ok", "partial", "empty"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
