from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.us_options.config import load_scan_config
from core.us_options.scanner import scan_us_options


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US Option Candidate Scanner v0")
    parser.add_argument("--config", default="config/us_options_universe.json", help="JSON 配置文件路径")
    parser.add_argument("--budget", type=float, default=None, help="单张合约最大预算，默认 50")
    parser.add_argument("--direction", choices=["call", "put", "both"], default=None, help="扫描方向")
    parser.add_argument("--min-dte", type=int, default=None, help="最小 DTE")
    parser.add_argument("--max-dte", type=int, default=None, help="最大 DTE")
    parser.add_argument("--top-n", type=int, default=None, help="最多输出候选数量")
    parser.add_argument("--risk-free-rate", type=float, default=None, help="无风险利率，例如 0.045")
    parser.add_argument("--dividend-yield", type=float, default=None, help="股息率，第一版默认 0")
    parser.add_argument("--output-root", default=None, help="扫描输出根目录")
    parser.add_argument("--data-source", choices=["auto", "yfinance", "cboe", "cboe_delayed"], default=None, help="数据源，默认 auto")
    parser.add_argument("--push-feishu", dest="push_feishu", action="store_true", default=None, help="扫描完成后推送到飞书")
    parser.add_argument("--no-push-feishu", dest="push_feishu", action="store_false", help="扫描完成后不推送到飞书")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_scan_config(
        args.config,
        budget=args.budget,
        direction=args.direction,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        top_n=args.top_n,
        risk_free_rate=args.risk_free_rate,
        dividend_yield=args.dividend_yield,
        output_root=args.output_root,
        data_source=args.data_source,
        push_feishu=args.push_feishu,
    )
    result = scan_us_options(config)
    print(f"扫描完成：{result['run_dir']}")
    print(
        "合约={raw_count} 候选={candidate_count} 拒绝={rejected_count} 成功symbol={successful_symbols} 失败symbol={failed_symbols}".format(
            **result
        )
    )
    if "feishu_push" in result:
        status = result["feishu_push"]
        print(f"飞书推送：{'成功' if status.get('success') else '失败'} {status.get('reason', '')}")
    print("提示：下单前必须用复星国际证券 App 核对实时 bid/ask、价差、成交量和可交易性。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
