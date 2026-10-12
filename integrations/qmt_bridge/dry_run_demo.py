"""Run the QMT/QNT bridge without any external broker connection."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.qmt_bridge.mock_broker import MockBrokerBridge
from integrations.qmt_bridge.risk_guard import RiskGuard
from integrations.qmt_bridge.trade_plan import TradePlan


def run_demo() -> dict:
    output = ROOT / "data" / "reports" / "qmt_bridge_dry_run_latest.json"
    signal = {
        "symbol": "000001.SZ",
        "side": "buy",
        "quantity": 100,
        "price_type": "limit",
        "reason": "接口联调样例，仅用于 dry-run 验证",
        "strategy_id": "dry_run_validation",
    }
    plan = TradePlan(**signal)
    context = {
        "single_position_ratio": 0.05,
        "total_position_ratio": 0.30,
        "is_st": False,
        "is_suspended": False,
        "is_limit_up": False,
        "is_limit_down": False,
        "data_complete": True,
        "is_trading_day": True,
    }
    approved, checks = RiskGuard().evaluate(plan, context)
    broker = MockBrokerBridge(output)
    order = broker.place_order(plan.to_dict()) if approved else None
    result = {
        "dry_run": True,
        "real_broker_connected": False,
        "signal": signal,
        "trade_plan": plan.to_dict(),
        "risk_checks": checks,
        "mock_order": order,
        "log_path": str(output),
    }
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
