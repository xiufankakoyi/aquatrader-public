"""主升浪回踩反转策略。

识别"主升浪启动→加速→回踩均线→地量反转放量中阳线"形态：
1. 历史存在沿 5 日线上升的主升浪段（MA5>MA10、启动阶段乖离度受控、期间高位放量）
2. 主升浪后回踩 MA10/MA20/MA30 之一
3. 反转日（今日）中阳线涨幅>5%，昨日地量，今日放量≥昨日 1.2 倍

导入即注册为日线筛选策略插件（@register_screen_strategy 装饰器触发）。
DailyScreener 会在导入本模块后自动调度本策略。

设计原则：
- 形态识别用位置索引（iloc/.values），避免标签索引在不同数据源下的坑
- PIT 安全：仅用 ctx.daily_data（已被 screener 截断到 current_date 之前）
- 降级友好：数据不足或异常时返回 MISSING_DATA，不崩溃
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from core.quant.contracts import RejectReason
from core.quant.screening.strategy_base import (
    ScreenStrategyBase,
    StrategyContext,
    register_screen_strategy,
)

logger = logging.getLogger(__name__)


@register_screen_strategy()
class MainWavePullbackStrategy(ScreenStrategyBase):
    """主升浪回踩反转策略。

    检测流程（在反转日=今日触发）：
    1. 今日反转信号：中阳线涨幅>surge_pct + 昨日地量 + 今日放量≥昨日×vol_surge_ratio
    2. 前期主升浪：lookback 窗口内存在一段 uptrend_min_days 天的上升段，
       期间 MA5>MA10、启动阶段乖离度≤bias_max_startup、期间出现高位放量
    3. 回踩确认：主升浪高点后股价回踩 MA10/MA20/MA30 之一（容差±pullback_tol%）

    参数（均从 ctx.params 读取，支持外部覆盖）：
        ma_short: 短期均线天数（默认 5）
        ma_mid: 中期均线天数（默认 10）
        ma_long: 长期均线天数（默认 20）
        ma_longest: 超长期均线天数（默认 30）
        lookback_days: 主升浪回看窗口（默认 60）
        surge_pct: 中阳线最低涨幅%（默认 5.0）
        vol_surge_ratio: 今日放量倍数（默认 1.2）
        low_vol_ratio: 地量相对 5 日均量比例（默认 0.6）
        bias_max_startup: 启动阶段最大乖离度%（默认 8.0）
        pullback_tol: 回踩触及均线容差%（默认 3.0）
        uptrend_min_days: 主升浪最少持续天数（默认 10）
    """

    strategy_id = "main_wave_pullback"
    strategy_name = "主升浪回踩反转"
    description = "主升浪后回踩均线，地量反转放量中阳线"
    priority = 35
    required = True

    def check(
        self, ctx: StrategyContext
    ) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """执行主升浪回踩反转形态检测。

        Args:
            ctx: 策略执行上下文。

        Returns:
            (passed, score, details, reject_reasons)：
            - passed: 是否满足主升浪回踩反转形态
            - score: 形态匹配度 [0, 1]
            - details: 形态关键参数（涨幅/乖离/回踩均线等）
            - reject_reasons: 未通过时的拒绝原因
        """
        df = ctx.daily_data
        if df is None or df.empty:
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        # 参数读取（支持外部覆盖）
        ma_short = int(ctx.params.get("ma_short", 5))
        ma_mid = int(ctx.params.get("ma_mid", 10))
        ma_long = int(ctx.params.get("ma_long", 20))
        ma_longest = int(ctx.params.get("ma_longest", 30))
        lookback = int(ctx.params.get("lookback_days", 60))
        surge_pct = float(ctx.params.get("surge_pct", 5.0))
        vol_surge_ratio = float(ctx.params.get("vol_surge_ratio", 1.2))
        low_vol_ratio = float(ctx.params.get("low_vol_ratio", 0.7))
        bias_max = float(ctx.params.get("bias_max_startup", 8.0))
        pullback_tol = float(ctx.params.get("pullback_tol", 3.0))
        uptrend_min_days = int(ctx.params.get("uptrend_min_days", 10))

        # 数据充分性：需要 lookback + ma_longest 天
        required_len = lookback + ma_longest
        if (
            "close" not in df.columns
            or "volume" not in df.columns
            or len(df) < required_len
        ):
            return False, 0.0, {}, [RejectReason.MISSING_DATA]

        try:
            close = df["close"].astype(float).reset_index(drop=True)
            volume = df["volume"].astype(float).reset_index(drop=True)
            low_col = (
                df["low"].astype(float).reset_index(drop=True)
                if "low" in df.columns
                else close
            )

            # 均线
            ma5 = close.rolling(ma_short).mean()
            ma10 = close.rolling(ma_mid).mean()
            ma20 = close.rolling(ma_long).mean()
            ma30 = close.rolling(ma_longest).mean()
            vol_ma5 = volume.rolling(ma_short).mean()

            today_idx = len(df) - 1
            yest_idx = today_idx - 1
            if yest_idx < ma_longest:
                return False, 0.0, {}, [RejectReason.MISSING_DATA]

            today_close = float(close.iloc[today_idx])
            prev_close = float(close.iloc[yest_idx])
            today_vol = float(volume.iloc[today_idx])
            yest_vol = float(volume.iloc[yest_idx])

            # ===== 步骤1: 今日反转信号 =====
            # 1.1 中阳线涨幅（相对昨日收盘）
            if prev_close <= 0:
                return False, 0.0, {}, [RejectReason.MISSING_DATA]
            today_return = (today_close / prev_close - 1.0) * 100.0
            if today_return < surge_pct:
                return False, 0.0, {
                    "today_return": round(today_return, 4),
                    "surge_required": surge_pct,
                }, [RejectReason.TREND_FAIL]

            # 1.2 昨日地量：昨日量 < 昨日 5 日均量 × low_vol_ratio
            yest_vol_ma5 = vol_ma5.iloc[yest_idx]
            if pd.isna(yest_vol_ma5) or yest_vol_ma5 <= 0:
                return False, 0.0, {}, [RejectReason.MISSING_DATA]
            is_low_vol = yest_vol < float(yest_vol_ma5) * low_vol_ratio
            if not is_low_vol:
                return False, 0.0, {
                    "yest_vol": round(yest_vol, 2),
                    "yest_vol_ma5": round(float(yest_vol_ma5), 2),
                    "low_vol_ratio_required": low_vol_ratio,
                }, [RejectReason.LIQUIDITY_FAIL]

            # 1.3 今日放量 ≥ 昨日 × vol_surge_ratio
            if yest_vol <= 0:
                return False, 0.0, {}, [RejectReason.MISSING_DATA]
            today_vol_ratio = today_vol / yest_vol
            if today_vol_ratio < vol_surge_ratio:
                return False, 0.0, {
                    "today_vol": round(today_vol, 2),
                    "yest_vol": round(yest_vol, 2),
                    "vol_surge_required": vol_surge_ratio,
                }, [RejectReason.LIQUIDITY_FAIL]

            # ===== 步骤2: 前期主升浪识别 =====
            # 在 [window_start, yest_idx] 范围内找最高点（不含今日）
            window_start = max(0, today_idx - lookback + 1)
            if yest_idx - window_start + 1 < uptrend_min_days:
                return False, 0.0, {}, [RejectReason.MISSING_DATA]

            window_close_vals = close.iloc[window_start:yest_idx + 1].to_numpy()
            high_pos_in_window = int(np.argmax(window_close_vals))
            high_idx = window_start + high_pos_in_window

            # 主升浪段：从 high_idx 往前找连续 MA5>MA10 的区间起点
            up_start = high_idx
            for i in range(high_idx, max(-1, high_idx - lookback - 1), -1):
                if i < ma_longest:
                    break
                ma5_val = ma5.iloc[i]
                ma10_val = ma10.iloc[i]
                if pd.isna(ma5_val) or pd.isna(ma10_val):
                    break
                if float(ma5_val) <= float(ma10_val):
                    break
                up_start = i

            up_days = high_idx - up_start + 1
            if up_days < uptrend_min_days:
                return False, 0.0, {
                    "uptrend_days": up_days,
                    "uptrend_required": uptrend_min_days,
                }, [RejectReason.TREND_FAIL]

            # 启动阶段乖离度（前半段）：|close - ma5| / ma5
            half = up_start + up_days // 2
            startup_close = close.iloc[up_start:half + 1]
            startup_ma5 = ma5.iloc[up_start:half + 1]
            valid = startup_ma5.notna() & (startup_ma5 > 0)
            if valid.sum() == 0:
                return False, 0.0, {}, [RejectReason.MISSING_DATA]
            bias = (
                (startup_close[valid] - startup_ma5[valid]) / startup_ma5[valid]
            ).abs() * 100.0
            max_bias = float(bias.max())
            if max_bias > bias_max:
                return False, 0.0, {
                    "max_startup_bias": round(max_bias, 4),
                    "bias_max_allowed": bias_max,
                }, [RejectReason.TREND_FAIL]

            # 期间高位放量：主升浪段内存在 volume > vol_ma5 的日
            seg_vol = volume.iloc[up_start:high_idx + 1]
            seg_vol_ma5 = vol_ma5.iloc[up_start:high_idx + 1]
            seg_valid = seg_vol_ma5.notna() & (seg_vol_ma5 > 0)
            if seg_valid.sum() == 0:
                return False, 0.0, {}, [RejectReason.MISSING_DATA]
            has_volume_surge = bool(
                (seg_vol[seg_valid] > seg_vol_ma5[seg_valid]).any()
            )
            if not has_volume_surge:
                return False, 0.0, {
                    "uptrend_days": up_days,
                    "note": "no_high_volume_in_uptrend",
                }, [RejectReason.LIQUIDITY_FAIL]

            # ===== 步骤3: 回踩确认 =====
            # high_idx+1 到 yest_idx 之间，最低价触及 MA10/MA20/MA30 之一
            if yest_idx <= high_idx:
                # 今日即窗口最高点，无回踩段
                return False, 0.0, {
                    "note": "no_pullback_window",
                }, [RejectReason.POSITION_FAIL]

            pullback_low = low_col.iloc[high_idx + 1:yest_idx + 1]
            pullback_ma10 = ma10.iloc[high_idx + 1:yest_idx + 1]
            pullback_ma20 = ma20.iloc[high_idx + 1:yest_idx + 1]
            pullback_ma30 = ma30.iloc[high_idx + 1:yest_idx + 1]

            touched_line = ""
            min_touch_pct = float("inf")
            for ma_name, ma_series in [
                ("MA10", pullback_ma10),
                ("MA20", pullback_ma20),
                ("MA30", pullback_ma30),
            ]:
                valid_ma = ma_series.notna() & (ma_series > 0)
                if valid_ma.sum() == 0:
                    continue
                diff = (
                    (pullback_low[valid_ma] - ma_series[valid_ma]).abs()
                    / ma_series[valid_ma]
                    * 100.0
                )
                diff_min = float(diff.min())
                if diff_min < min_touch_pct:
                    min_touch_pct = diff_min
                if diff_min <= pullback_tol:
                    touched_line = ma_name
                    break

            if not touched_line:
                return False, 0.0, {
                    "min_pullback_touch_pct": round(min_touch_pct, 4),
                    "pullback_tol": pullback_tol,
                }, [RejectReason.POSITION_FAIL]

            # ===== 全部通过 =====
            details = {
                "today_return": round(today_return, 4),
                "uptrend_days": up_days,
                "max_startup_bias": round(max_bias, 4),
                "pullback_touched": touched_line,
                "min_pullback_touch_pct": round(min_touch_pct, 4),
                "yest_vol_vs_ma5": round(yest_vol / float(yest_vol_ma5), 4),
                "today_vol_vs_yest": round(today_vol_ratio, 4),
            }
            return True, 1.0, details, []

        except Exception as exc:
            logger.warning("主升浪回踩策略异常: %s", exc)
            return False, 0.0, {"error": str(exc)}, [RejectReason.MISSING_DATA]
