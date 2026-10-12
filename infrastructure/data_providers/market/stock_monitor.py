"""Stock Monitor - 股票监控/预警工具

功能：价格预警、自选异动检查、自动盯盘
数据源：通过 ProviderRegistry 智能降级获取行情
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd

from infrastructure.data_providers.provider_registry import ProviderRegistry

logger = logging.getLogger(__name__)

# 默认自选股列表
_DEFAULT_WATCHLIST = [
    "600519",  # 贵州茅台
    "300750",  # 宁德时代
    "000001",  # 平安银行
    "601318",  # 中国平安
    "000858",  # 五粮液
    "002415",  # 海康威视
    "600036",  # 招商银行
    "601166",  # 兴业银行
]

# 同一 API 调用间隔（秒）
_RATE_LIMIT_SECONDS = 0.3


class StockMonitor:
    """股票监控/预警工具，基于 ProviderRegistry 智能降级获取行情数据。"""

    def __init__(self, registry: ProviderRegistry) -> None:
        """初始化监控器。

        Args:
            registry: 数据提供者注册表，用于获取行情数据
        """
        self.registry = registry
        self._alerts: list[dict[str, Any]] = []
        self._last_call_time: float = 0.0

    def _rate_limit(self) -> None:
        """限速：确保连续调用之间至少间隔 _RATE_LIMIT_SECONDS 秒。"""
        elapsed = time.time() - self._last_call_time
        if elapsed < _RATE_LIMIT_SECONDS:
            time.sleep(_RATE_LIMIT_SECONDS - elapsed)
        self._last_call_time = time.time()

    def _get_quote_by_code(self, code: str) -> dict[str, Any] | None:
        """从 registry 获取单只股票实时行情（使用 smart_quote 智能降级）。"""
        self._rate_limit()
        try:
            result = self.registry.smart_quote(code)
            if "error" in result:
                return None
            return {
                "code": code,
                "name": result.get("name", ""),
                "price": float(result.get("price", 0) or 0),
                "change_pct": float(result.get("change_pct", result.get("pct_chg", 0)) or 0),
                "time": str(result.get("time", result.get("updated_at", ""))),
            }
        except Exception as exc:
            logger.warning("获取 %s 行情失败: %s", code, exc)
            return None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def check_price(
        self,
        code: str,
        target: float | None = None,
        direction: str = "above",
    ) -> dict[str, Any]:
        """检查股票价格是否触发预警条件。

        Args:
            code: 股票代码
            target: 目标价格，为 None 时仅返回当前行情
            direction: 触发方向，'above' 突破 / 'below' 跌破

        Returns:
            包含 code/name/price/change_pct/time/triggered/alert 的字典
        """
        q = self._get_quote_by_code(code)
        if q is None:
            return {"code": code, "error": "获取行情失败"}

        result: dict[str, Any] = {
            "code": code,
            "name": q["name"],
            "price": q["price"],
            "change_pct": q["change_pct"],
            "time": q["time"],
            "triggered": False,
            "alert": "",
        }

        if target is not None:
            target = float(target)
            if direction == "above" and q["price"] >= target:
                result["triggered"] = True
                result["alert"] = f"📈 {q['name']} 已突破 {target}元，现价{q['price']}元"
            elif direction == "below" and q["price"] <= target:
                result["triggered"] = True
                result["alert"] = f"📉 {q['name']} 已跌破 {target}元，现价{q['price']}元"

        return result

    def check_watchlist(self, watchlist: list[str] | None = None) -> list[dict[str, Any]]:
        """检查自选股列表的异动（涨跌幅>3%标记为异动）。

        Args:
            watchlist: 股票代码列表，为 None 时使用默认列表

        Returns:
            异动列表，按涨跌幅绝对值降序排列
        """
        codes = watchlist or _DEFAULT_WATCHLIST
        results: list[dict[str, Any]] = []

        for code in codes:
            try:
                q = self._get_quote_by_code(code)
                if q is None:
                    continue
                item: dict[str, Any] = {
                    "code": code,
                    "name": q["name"],
                    "price": q["price"],
                    "change_pct": q["change_pct"],
                    "time": q["time"],
                }
                c = q["change_pct"]
                if abs(c) >= 3:
                    item["flag"] = "⚠️" if c > 0 else "🔻"
                elif abs(c) >= 1:
                    item["flag"] = "↑" if c > 0 else "↓"
                else:
                    item["flag"] = "→"
                results.append(item)
            except Exception as exc:
                logger.warning("检查自选股 %s 异常: %s", code, exc)
                continue

        # 按涨跌幅绝对值降序
        results.sort(key=lambda x: abs(x.get("change_pct", 0)), reverse=True)
        return results

    def set_alert(self, code: str, condition: str, value: float) -> dict[str, Any]:
        """设置价格预警。

        Args:
            code: 股票代码
            condition: 触发条件，'above' 突破 / 'below' 跌破
            value: 目标价格

        Returns:
            已设置的预警信息
        """
        alert: dict[str, Any] = {
            "code": code,
            "condition": condition,
            "value": float(value),
            "active": True,
        }
        self._alerts.append(alert)
        return alert

    def check_alerts(self) -> list[dict[str, Any]]:
        """检查所有已设置的预警是否触发。

        Returns:
            触发的预警列表，每项包含 code/name/price/triggered/alert 信息
        """
        triggered_list: list[dict[str, Any]] = []
        for alert in self._alerts:
            if not alert.get("active", True):
                continue
            try:
                result = self.check_price(
                    code=alert["code"],
                    target=alert["value"],
                    direction=alert["condition"],
                )
                if result.get("triggered"):
                    # 标记已触发，避免重复提醒
                    alert["active"] = False
                    triggered_list.append(result)
            except Exception as exc:
                logger.warning("检查预警 %s 失败: %s", alert["code"], exc)
                continue
        return triggered_list
