"""默认日线筛选策略集合。

把原 DailyScreener 的六项检查拆成独立插件：
- PITCheckStrategy: PIT 准入（最先执行，失败短路）
- TrendCheckStrategy: MA 多头排列/突破
- PositionCheckStrategy: 距前高/前低相对位置
- AccelerationCheckStrategy: 加速赶顶检测
- LiquidityCheckStrategy: 成交额/成交量
- TradabilityCheckStrategy: ST/停牌/涨跌停

每个策略独立注册，DailyScreener 聚合结果。
外部策略大师可注册自己的策略替代或补充。
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from core.quant.contracts import QuantCandidate, RejectReason, SectorPhase
from core.quant.screening.strategy_base import (
    ScreenStrategyBase,
    StrategyContext,
    register_screen_strategy,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# PIT 准入检查（最高优先级，失败短路）
# ===========================================================================

@register_screen_strategy()
class PITCheckStrategy(ScreenStrategyBase):
    """PIT 准入检查：候选未过期且数据已可用。

    priority=10 确保最先执行；required=True 失败则整体拒绝。
    """

    strategy_id = "pit_check"
    strategy_name = "PIT 准入检查"
    description = "检查候选是否过期、数据是否在当前日期前可用"
    priority = 10
    required = True

    def check(
        self, ctx: StrategyContext
    ) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """执行 PIT 检查。

        Args:
            ctx: 策略上下文。

        Returns:
            (passed, score, details, reject_reasons)
        """
        candidate = ctx.candidate
        if candidate is None:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        # 过期检查
        if candidate.is_expired(ctx.current_date):
            return False, 0.0, {"expired_at": candidate.expires_at}, [RejectReason.EXPIRED]

        # PIT 可用性检查
        if not candidate.is_pit_valid(ctx.current_date):
            return False, 0.0, {"available_at": candidate.available_at}, [RejectReason.PIT_VIOLATION]

        return True, 1.0, {}, []


# ===========================================================================
# 趋势检查
# ===========================================================================

@register_screen_strategy()
class TrendCheckStrategy(ScreenStrategyBase):
    """趋势检查：MA 多头排列或突破中期均线。

    参数：
        ma_short: 短期均线天数（默认 5）
        ma_mid: 中期均线天数（默认 10）
        ma_long: 长期均线天数（默认 20）
    """

    strategy_id = "trend_check"
    strategy_name = "趋势检查"
    description = "MA 多头排列或突破中期均线"
    priority = 20
    required = True

    def check(
        self, ctx: StrategyContext
    ) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """执行趋势检查。"""
        df = ctx.daily_data
        if df is None or df.empty:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        ma_short_days = int(ctx.params.get("ma_short", 5))
        ma_mid_days = int(ctx.params.get("ma_mid", 10))
        ma_long_days = int(ctx.params.get("ma_long", 20))

        if "close" not in df.columns or len(df) < ma_long_days:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        try:
            close = df["close"]
            ma_short = close.rolling(ma_short_days).mean()
            ma_mid = close.rolling(ma_mid_days).mean()
            ma_long = close.rolling(ma_long_days).mean()

            latest_idx = len(close) - 1
            if pd.isna(ma_long.iloc[latest_idx]):
                return False, 0.0, {}, [RejectReason.MISSING_DATA]

            bullish_alignment = (
                ma_short.iloc[latest_idx] > ma_mid.iloc[latest_idx]
                and ma_mid.iloc[latest_idx] > ma_long.iloc[latest_idx]
            )
            breakout = close.iloc[latest_idx] > ma_mid.iloc[latest_idx]

            if bullish_alignment:
                score = 1.0
            elif breakout:
                score = 0.6
            else:
                score = 0.3

            passed = bool(bullish_alignment or breakout)
            details = {
                "ma_short": round(float(ma_short.iloc[latest_idx]), 4),
                "ma_mid": round(float(ma_mid.iloc[latest_idx]), 4),
                "ma_long": round(float(ma_long.iloc[latest_idx]), 4),
            }
            reject_reasons: list[RejectReason] = [] if passed else [RejectReason.TREND_FAIL]
            return passed, score, details, reject_reasons
        except Exception as exc:
            logger.warning("趋势检查异常: %s", exc)
            return False, 0.0, {"error": str(exc)}, [RejectReason.MISSING_DATA]


# ===========================================================================
# 位置检查
# ===========================================================================

@register_screen_strategy()
class PositionCheckStrategy(ScreenStrategyBase):
    """位置检查：距前高/前低的相对位置。

    参数：
        lookback_days: 回看天数（默认 60）
        high_position_ratio: 高位比例阈值（默认 0.85）
    """

    strategy_id = "position_check"
    strategy_name = "位置检查"
    description = "距前高/前低的相对位置，过高位拒绝"
    priority = 30
    required = True

    def check(
        self, ctx: StrategyContext
    ) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """执行位置检查。"""
        df = ctx.daily_data
        if df is None or df.empty:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        lookback = int(ctx.params.get("lookback_days", 60))
        high_ratio = float(ctx.params.get("high_position_ratio", 0.85))

        if "close" not in df.columns:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        try:
            close = df["close"]
            lookback_data = close.tail(lookback)
            if len(lookback_data) < 5:
                return False, 0.0, {}, [RejectReason.MISSING_DATA]

            high = float(lookback_data.max())
            low = float(lookback_data.min())
            current = float(close.iloc[-1])

            if high == low:
                score = 0.5
            else:
                score = (current - low) / (high - low)

            passed = bool(score < high_ratio)
            details = {
                "position_high": round(high, 4),
                "position_low": round(low, 4),
                "position_current": round(current, 4),
            }
            reject_reasons: list[RejectReason] = [] if passed else [RejectReason.POSITION_FAIL]
            return passed, float(score), details, reject_reasons
        except Exception as exc:
            logger.warning("位置检查异常: %s", exc)
            return False, 0.0, {"error": str(exc)}, [RejectReason.MISSING_DATA]


# ===========================================================================
# 加速赶顶检查
# ===========================================================================

@register_screen_strategy()
class AccelerationCheckStrategy(ScreenStrategyBase):
    """加速赶顶检测：短期涨幅过大为负面信号。

    参数：
        acceleration_threshold: 近 5 日涨幅阈值（默认 3.0%）
    """

    strategy_id = "acceleration_check"
    strategy_name = "加速赶顶检查"
    description = "近 5 日涨幅过大视为加速赶顶负面信号"
    priority = 40
    required = True

    def check(
        self, ctx: StrategyContext
    ) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """执行加速赶顶检查。"""
        df = ctx.daily_data
        if df is None or df.empty:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        threshold = float(ctx.params.get("acceleration_threshold", 3.0))

        if "close" not in df.columns or len(df) < 5:
            # 数据不足，安全放行（不阻断）
            return True, 1.0, {}, []

        try:
            close = df["close"]
            recent_return = (close.iloc[-1] / close.iloc[-5] - 1.0) * 100.0
            is_accelerating = float(recent_return) > threshold
            passed = not bool(is_accelerating)
            details = {"recent_5d_return": round(float(recent_return), 4)}
            reject_reasons: list[RejectReason] = [] if passed else [RejectReason.ACCELERATION_FAIL]
            return passed, float(recent_return), details, reject_reasons
        except Exception as exc:
            logger.warning("加速赶顶检查异常: %s", exc)
            # 安全降级：不阻断
            return True, 1.0, {"error": str(exc)}, []


# ===========================================================================
# 流动性检查
# ===========================================================================

@register_screen_strategy()
class LiquidityCheckStrategy(ScreenStrategyBase):
    """流动性检查：成交额/成交量达标。

    参数：
        min_turnover: 最低日成交额（默认 5e7，单位：元）
        min_volume: 最低日成交量（默认 1e5，单位：股）

    单位换算（LanceDB daily_ohlcv 存的是 Tushare 原始单位）：
        volume: 手（1手=100股）→ 换算为股
        amount: 千元 → 换算为元
    """

    strategy_id = "liquidity_check"
    strategy_name = "流动性检查"
    description = "近 5 日平均成交额和成交量达标"
    priority = 50
    required = True

    # LanceDB daily_ohlcv 字段单位（Tushare 原始）
    VOLUME_UNIT_FACTOR = 100.0       # volume: 手 → 股
    AMOUNT_UNIT_FACTOR = 1000.0      # amount: 千元 → 元

    def check(
        self, ctx: StrategyContext
    ) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """执行流动性检查。"""
        df = ctx.daily_data
        if df is None or df.empty:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        min_turnover = float(ctx.params.get("min_turnover", 5e7))
        min_volume = float(ctx.params.get("min_volume", 1e5))

        if "volume" not in df.columns:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        try:
            recent_volume = df["volume"].tail(5)
            # volume 单位换算：手 → 股
            avg_volume = float(recent_volume.mean()) * self.VOLUME_UNIT_FACTOR \
                if not recent_volume.empty else 0.0

            avg_turnover = 0.0
            if "amount" in df.columns:
                recent_turnover = df["amount"].tail(5)
                # amount 单位换算：千元 → 元
                avg_turnover = float(recent_turnover.mean()) * self.AMOUNT_UNIT_FACTOR \
                    if not recent_turnover.empty else 0.0
            elif "close" in df.columns:
                # amount 列缺失时用 volume(股) × close 估算
                avg_turnover = avg_volume * float(df["close"].iloc[-1])

            score = 0.0
            if avg_turnover >= min_turnover:
                score += 0.5
            if avg_volume >= min_volume:
                score += 0.5

            passed = bool(avg_turnover >= min_turnover and avg_volume >= min_volume)
            details = {
                "avg_volume_5d": round(avg_volume, 2),
                "avg_turnover_5d": round(avg_turnover, 2),
            }
            reject_reasons: list[RejectReason] = [] if passed else [RejectReason.LIQUIDITY_FAIL]
            return passed, score, details, reject_reasons
        except Exception as exc:
            logger.warning("流动性检查异常: %s", exc)
            return False, 0.0, {"error": str(exc)}, [RejectReason.MISSING_DATA]


# ===========================================================================
# 可交易性检查
# ===========================================================================

@register_screen_strategy()
class TradabilityCheckStrategy(ScreenStrategyBase):
    """可交易性检查：非 ST、非停牌、非涨跌停。

    依赖日线数据的 is_st/is_suspended/is_limit_up/is_limit_down 列，
    缺失时安全放行（不阻断）。
    """

    strategy_id = "tradability_check"
    strategy_name = "可交易性检查"
    description = "非 ST、非停牌、非涨跌停"
    priority = 60
    required = True

    def check(
        self, ctx: StrategyContext
    ) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """执行可交易性检查。"""
        df = ctx.daily_data
        if df is None or df.empty:
            return False, 0.0, {}, [RejectReason.TRADABILITY_FAIL]

        try:
            latest = df.iloc[-1]

            is_st = bool(latest.get("is_st", False)) if "is_st" in df.columns else False
            is_suspended = bool(latest.get("is_suspended", False)) if "is_suspended" in df.columns else False
            is_limit_up = bool(latest.get("is_limit_up", False)) if "is_limit_up" in df.columns else False
            is_limit_down = bool(latest.get("is_limit_down", False)) if "is_limit_down" in df.columns else False
            close = float(latest.get("close", 0.0))
            price_valid = close > 0

            passed = bool(
                not is_st and not is_suspended and not is_limit_up
                and not is_limit_down and price_valid
            )
            details = {
                "is_st": is_st,
                "is_suspended": is_suspended,
                "is_limit_up": is_limit_up,
                "is_limit_down": is_limit_down,
            }
            reject_reasons: list[RejectReason] = [] if passed else [RejectReason.TRADABILITY_FAIL]
            return passed, 1.0 if passed else 0.0, details, reject_reasons
        except Exception as exc:
            logger.warning("可交易性检查异常: %s", exc)
            return False, 0.0, {"error": str(exc)}, [RejectReason.TRADABILITY_FAIL]
