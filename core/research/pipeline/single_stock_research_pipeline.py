"""单股深度研报 Pipeline 命令行入口。

Usage: python -m core.research.pipeline.single_stock_research_pipeline --symbol 000001.SZ --days 365
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _safe_symbol(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _safe_days(value: object, default: int = 365) -> int:
    try:
        return max(1, min(int(value), 3660))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def main() -> None:
    """单股深度研报 Pipeline 命令行入口。"""
    parser = argparse.ArgumentParser(
        description="Single Stock Deep Research Pipeline"
    )
    parser.add_argument(
        "--symbol",
        required=True,
        help="Stock symbol, e.g. 000001.SZ",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Lookback days (default: 365)",
    )
    args = parser.parse_args()

    symbol = _safe_symbol(args.symbol)
    days = _safe_days(args.days)
    if not symbol:
        parser.error("--symbol must be a non-empty string")

    print(f"[SingleStockResearch] 启动: symbol={symbol}, days={days}")

    from core.research.engine.research_engine import ResearchEngine

    engine = None
    try:
        engine = ResearchEngine()
        result = engine.run_single_stock(symbol, days)
        result = result if isinstance(result, Mapping) else {
            "success": False,
            "error": "invalid engine result",
        }
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    # 输出摘要
    print("\n" + "=" * 60)
    print(f"  深度研报: {symbol}")
    print("=" * 60)

    if result.get("success"):
        print(f"  结论等级: {result.get('conclusion_level', 'UNKNOWN')}")
        print(f"  验证得分: {_safe_float(result.get('validation_score', 0.0)):.1f}")
        export = result.get("export", {})
        export = export if isinstance(export, Mapping) else {}
        if export.get("json_path"):
            print(f"  JSON 报告: {export['json_path']}")
        if export.get("md_path"):
            print(f"  Markdown 报告: {export['md_path']}")
    else:
        print(f"  失败: {result.get('error', '未知错误')}")

    print("=" * 60)

    # 输出引擎状态
    try:
        if engine is None:
            raise RuntimeError("research engine unavailable")
        status = engine.get_status()
        status = status if isinstance(status, Mapping) else {}
        print(f"\n  引擎状态:")
        print(f"    证据数: {status.get('evidence_count', 0)}")
        print(f"    实体数: {status.get('entity_count', 0)}")
        print(f"    关系数: {status.get('relation_count', 0)}")
    except Exception:
        pass

    sys.exit(0 if bool(result.get("success")) else 1)


if __name__ == "__main__":
    main()
