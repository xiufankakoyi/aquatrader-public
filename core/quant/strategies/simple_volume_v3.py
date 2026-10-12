# strategies/simple_volume_v3.py
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List

import pandas as pd

from core.quant.strategies.strategy_framework import StrategyBase


@dataclass(frozen=True)
class SimpleVolumeConfig:
    """集中管理策略阈值"""

    market_cap_min: float = 20 * 10_000
    market_cap_max: float = 60 * 10_000
    volume_ratio_threshold: float = 2.0
    turnover_rate_threshold: float = 1.5
    ma_days: int = 5
    min_list_days: int = 60
    max_candidates: int = 1500
    position_ratio: float = 0.2
    max_stocks_per_day: int = 5
    
    # 银行股防守仓配置
    bank_codes: List[str] = field(default_factory=lambda: [
        "600000", "601998", "601818", "600015", "600016", 
        "600036", "601166", "000001", "601328", "601398", 
        "601939", "601988", "601288", "601658"
    ])
    bank_position_ratio: float = 1.0  
    bank_safe_ma: int = 20 


class SimpleVolumeStrategyV3(StrategyBase):
    """
    聚宽量比市值策略 V3 (严格主升浪版)
    
    核心改进：
    1. 极其严格的"主升浪"定义：必须 MA5 > MA10 > MA20，且位于 MA60 之上。
    2. 任何趋势不明、数据缺失的股票，一律视为弱势，不予买入。
    3. 只有真正的强势股才会被选中，否则强制进入银行防守或空仓。
    """
    strategy_id = "simple_volume_v3"
    strategy_name = "聚宽量比市值策略V3_严格趋势"
    needs_today_pool = True  # 策略需要当日股票池数据

    # ===== 新增：这一段是给前端 + GA 用的参数规范 =====
    PARAM_SPEC = [
        # --- 量能核心参数 ---
        {
            "key": "volume_ratio_threshold",
            "label": "量比阈值",
            "group": "量能过滤",
            "type": "float",
            "min": 1.0,
            "max": 10.0,
            "step": 0.1,
            "default": SimpleVolumeConfig().volume_ratio_threshold,
            "optimize": True,
            "description": "当日量比 > 该值才视为放量（核心买入条件）",
        },
        {
            "key": "turnover_rate_threshold",
            "label": "换手率下限(%)",
            "group": "量能过滤",
            "type": "float",
            "min": 0.5,
            "max": 20.0,
            "step": 0.5,
            "default": SimpleVolumeConfig().turnover_rate_threshold,
            "optimize": True,
            "description": "当日换手率 >= 该阈值，过滤缩量假阳线",
        },

        # --- 趋势相关（均线为主） ---
        {
            "key": "ma_days",
            "label": "主趋势均线天数",
            "group": "趋势过滤",
            "type": "int",
            "min": 5,
            "max": 60,
            "step": 1,
            "default": SimpleVolumeConfig().ma_days,
            "optimize": True,
            "description": "用来定义 MA 序列，例如 MA5>MA10>MA20>MA60 的主升浪结构",
        },
        {
            "key": "min_list_days",
            "label": "最短上市天数",
            "group": "趋势过滤",
            "type": "int",
            "min": 30,
            "max": 365,
            "step": 5,
            "default": SimpleVolumeConfig().min_list_days,
            "optimize": False,
            "description": "过滤刚上市的新股，防止数据不稳定",
        },

        # --- 仓位/持仓控制 ---
        {
            "key": "max_candidates",
            "label": "候选池最大数量",
            "group": "仓位控制",
            "type": "int",
            "min": 10,
            "max": 2000,
            "step": 10,
            "default": SimpleVolumeConfig().max_candidates,
            "optimize": True,
            "description": "从全市场先按成交额/市值挑出最多 N 只股票做进一步筛选",
        },
        {
            "key": "position_ratio",
            "label": "单次建仓仓位比例",
            "group": "仓位控制",
            "type": "float",
            "min": 0.05,
            "max": 1.0,
            "step": 0.05,
            "default": SimpleVolumeConfig().position_ratio,
            "optimize": True,
            "description": "整体资金中，用于本策略仓位的占比（非单票）",
        },
        {
            "key": "max_stocks_per_day",
            "label": "单日最多买入只数",
            "group": "仓位控制",
            "type": "int",
            "min": 1,
            "max": 10,
            "step": 1,
            "default": SimpleVolumeConfig().max_stocks_per_day,
            "optimize": True,
            "description": "控制每天分散程度，防止频繁换仓",
        },

        # --- 银行防守仓 ---
        {
            "key": "bank_position_ratio",
            "label": "银行防守仓比例",
            "group": "防守策略",
            "type": "float",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "default": SimpleVolumeConfig().bank_position_ratio,
            "optimize": True,
            "description": "当没有标的可买时，最多多少仓位拿去买银行股防守",
        },
        {
            "key": "bank_safe_ma",
            "label": "银行股安全均线天数",
            "group": "防守策略",
            "type": "int",
            "min": 10,
            "max": 120,
            "step": 5,
            "default": SimpleVolumeConfig().bank_safe_ma,
            "optimize": True,
            "description": "例如要求银行股收盘价站在 MA20/60 之上才买入",
        },
    ]

    @classmethod
    def get_param_spec(cls):
        return cls.PARAM_SPEC

    def __init__(
        self,
        config: SimpleVolumeConfig | None = None,
        market_cap_min: float | None = None,
        market_cap_max: float | None = None,
        volume_ratio_threshold: float | None = None,
        turnover_rate_threshold: float | None = None,
        ma_days: int | None = None,
        min_list_days: int | None = None,
        max_candidates: int | None = None,
        position_ratio: float | None = None,
        max_stocks_per_day: int | None = None,
        bank_codes: List[str] | None = None,
        bank_position_ratio: float | None = None,
        bank_safe_ma: int | None = None,
    ):
        super().__init__(name=self.strategy_name)
        base_config = config or SimpleVolumeConfig()

        overrides = {}
        if market_cap_min is not None:
            overrides["market_cap_min"] = float(market_cap_min)
        if market_cap_max is not None:
            overrides["market_cap_max"] = float(market_cap_max)
        if volume_ratio_threshold is not None:
            overrides["volume_ratio_threshold"] = float(volume_ratio_threshold)
        if turnover_rate_threshold is not None:
            overrides["turnover_rate_threshold"] = float(turnover_rate_threshold)
        if ma_days is not None:
            overrides["ma_days"] = int(ma_days)
        if min_list_days is not None:
            overrides["min_list_days"] = int(min_list_days)
        if max_candidates is not None:
            overrides["max_candidates"] = int(max_candidates)
        if position_ratio is not None:
            overrides["position_ratio"] = float(position_ratio)
        if max_stocks_per_day is not None:
            overrides["max_stocks_per_day"] = int(max_stocks_per_day)
        if bank_codes is not None:
            overrides["bank_codes"] = list(bank_codes)
        if bank_position_ratio is not None:
            overrides["bank_position_ratio"] = float(bank_position_ratio)
        if bank_safe_ma is not None:
            overrides["bank_safe_ma"] = int(bank_safe_ma)

        self.config = replace(base_config, **overrides) if overrides else base_config

        self.required_days = self.config.min_list_days
        self.position_ratio = self.config.position_ratio
        self.max_stocks_per_day = self.config.max_stocks_per_day
        self._bank_set = frozenset(self.config.bank_codes)

    def generate_signals(self, current_date, stock_pool_today, data_query):
        final_signals: Dict[str, str] = {}

        if stock_pool_today is None or stock_pool_today.empty:
            return {}

        prev_date = self._get_previous_trading_date(current_date, data_query)
        if prev_date is None:
            return {}
        
        stock_pool_prev = data_query.get_stock_pool(prev_date)
        if stock_pool_prev is None or stock_pool_prev.empty:
            return {}

        sell_codes = self._get_sell_signals(stock_pool_prev, prev_date, data_query)
        for code in sell_codes:
            final_signals[code] = "sell"

        pre_screened = self._pre_screen_stocks(stock_pool_prev, prev_date, data_query)
        
        buy_candidates = []
        if pre_screened:
            buy_candidates = self._evaluate_buy_candidates_strict(
                pre_screened, stock_pool_prev, prev_date, data_query
            )

        if buy_candidates:
            limited = buy_candidates[: self.config.max_stocks_per_day]
            for code in limited:
                if final_signals.get(code) != "sell":
                    final_signals.setdefault(code, "buy")
            print(f"[Simple策略] {current_date} 基于{prev_date}数据发现主升浪，买入: {limited}")
        else:
            self._apply_bank_defense_optimized(current_date, final_signals, stock_pool_prev)

        return final_signals
    
    def _get_previous_trading_date(self, current_date: str, data_query) -> str | None:
        """获取前一个交易日"""
        import pandas as pd
        from datetime import datetime, timedelta
        
        try:
            current = pd.to_datetime(current_date)
            for i in range(1, 10):
                prev = current - timedelta(days=i)
                prev_str = prev.strftime("%Y-%m-%d")
                df = data_query.get_stock_pool(prev_str)
                if df is not None and not df.empty:
                    return prev_str
            return None
        except Exception as e:
            print(f"[Simple策略] 获取前一交易日失败: {e}")
            return None

    def _evaluate_buy_candidates_strict(
        self,
        pre_screened_codes: List[str],
        stock_pool_snapshot: pd.DataFrame,
        current_date,
        data_query,
    ) -> List[str]:
        """
        严格版主升浪筛选：
        宁缺毋滥。如果均线没有多头排列，或者缺少数据无法验证，直接剔除。
        """
        if not pre_screened_codes or stock_pool_snapshot.empty:
            return []

        # 提取候选池
        candidates = (
            stock_pool_snapshot[stock_pool_snapshot["stock_code"].isin(pre_screened_codes)]
            .copy()
            .drop_duplicates(subset=["stock_code"])
        )
        if candidates.empty:
            return []

        # 1. 基础量能过滤
        candidates["volume_ratio"] = candidates["volume_ratio"].fillna(0)
        candidates["turnover_rate"] = candidates["turnover_rate"].fillna(0)
        
        vol_mask = (candidates["volume_ratio"] >= self.config.volume_ratio_threshold) & \
                   (candidates["turnover_rate"] >= self.config.turnover_rate_threshold)
        
        # 2. K线形态：放量大阳 + 非冲高回落
        bull_gt3 = candidates["close"] >= candidates["open"] * 1.03
        # 计算上影线比例：(High - Close) / Close
        upper_shadow = (candidates["high"] - candidates["close"]) / candidates["close"].replace(0, 1)
        no_fade = upper_shadow <= 0.03 # 上影线不超过3%
        
        # --- 3. 关键修正：严格趋势过滤 (STRICT TREND) ---
        # 检查是否有多头均线数据
        has_ma5 = "ma5" in candidates.columns
        has_ma10 = "ma10" in candidates.columns
        has_ma20 = "ma20" in candidates.columns
        has_ma60 = "ma60" in candidates.columns

        # 必须有 MA5/10/20 数据，否则视为非主升浪（宁可错过，不可做错）
        if not (has_ma5 and has_ma10 and has_ma20):
            print(f"[Simple策略] {current_date} 警告：缺少MA5/10/20数据，无法确认主升浪，全部放弃。")
            return []

        # 严格多头排列: Close > MA5 > MA10 > MA20
        # 这能有效过滤掉类似"得润电子"那种反弹但MA20还在头顶压制的股票
        trend_strict = (
            (candidates["close"] > candidates["ma5"]) &
            (candidates["ma5"] > candidates["ma10"]) &
            (candidates["ma10"] > candidates["ma20"])
        )
        
        # 均线向上：MA20 必须是向上的 (简单判定：当前MA20 > 昨天的MA20，这里简化为 > MA20*0.99 或者使用 ma20_slope 如果有)
        # 这里我们使用更简单的：Close 必须显著高于 MA20 (例如高出 3% 以上)，证明非常强势
        trend_strong = (candidates["close"] / candidates["ma20"]) > 1.03

        # 4. 生命线过滤 (如果有MA60)
        # 主升浪一定是在60日线之上的
        if has_ma60:
            life_line_ok = candidates["close"] > candidates["ma60"]
            trend_strict &= life_line_ok

        # 5. 综合掩码
        final_mask = vol_mask & bull_gt3 & no_fade & trend_strict & trend_strong
        
        strong_candidates = candidates.loc[final_mask, "stock_code"].tolist()
        
        if not strong_candidates and len(pre_screened_codes) > 0:
            # Debug info: let user know why stocks were rejected
            pass 
            # print(f"[Simple策略] {current_date} 预选 {len(pre_screened_codes)} 只，但无一满足严格主升浪条件 -> 转入防守")
            
        return strong_candidates

    def _apply_bank_defense_optimized(
        self,
        current_date: str,
        final_signals: Dict[str, str],
        stock_pool_today: pd.DataFrame,
    ) -> None:
        """
        优化后的银行兜底：
        只有当银行股本身处于安全区域（如 > MA20 或 > MA5）时才买入。
        否则空仓。
        """
        # 1. 清除非银行仓位
        current_positions = set(getattr(self, "current_portfolio", {}).keys())
        non_bank_positions = current_positions - self._bank_set
        for code in non_bank_positions:
            final_signals[code] = "sell"

        # 2. 获取当日银行股数据
        if stock_pool_today is None or stock_pool_today.empty:
            return
        
        bank_pool = stock_pool_today[stock_pool_today["stock_code"].isin(self._bank_set)].copy()
        if bank_pool.empty:
            # print(f"[Simple策略] {current_date} 防守失败：无银行股数据 -> 空仓")
            return

        # --- 银行股趋势过滤 ---
        safe_ma = self.config.bank_safe_ma
        
        close = bank_pool["close"]
        needs_check = pd.Series(True, index=bank_pool.index, dtype=bool)
        safe_mask = pd.Series(False, index=bank_pool.index, dtype=bool)

        if safe_ma == 20 and "ma20" in bank_pool.columns:
            ma20 = bank_pool["ma20"]
            ma20_available = needs_check & ma20.notna()
            safe_mask |= ma20_available & (close > ma20)
            needs_check &= ~ma20_available

        if "ma10" in bank_pool.columns:
            ma10 = bank_pool["ma10"]
            ma10_available = needs_check & ma10.notna()
            safe_mask |= ma10_available & (close > ma10)
            needs_check &= ~ma10_available

        if "ma5" in bank_pool.columns:
            ma5 = bank_pool["ma5"]
            ma5_available = needs_check & ma5.notna()
            safe_mask |= ma5_available & (close > ma5)

        bank_pool["is_safe"] = safe_mask
        safe_banks = bank_pool[bank_pool["is_safe"]]

        if safe_banks.empty:
            print(f"[Simple策略] {current_date} 市场极度弱势：银行股也破位 -> 空仓避险")
            return

        # --- 正常的买入分配逻辑 ---
        sort_col = "amount" if "amount" in safe_banks.columns else "total_mv"
        safe_banks = safe_banks.sort_values(sort_col, ascending=False)

        bank_ratio = max(0.0, min(1.0, float(self.config.bank_position_ratio or 0)))
        per_stock = max(0.0, min(1.0, float(self.position_ratio or 0)))
        if per_stock <= 0:
            per_stock = bank_ratio if bank_ratio > 0 else 1.0
        remaining = bank_ratio if bank_ratio > 0 else per_stock

        buys = []
        for _, row in safe_banks.iterrows():
            if remaining <= 0:
                break
            bank_code = row["stock_code"]
            
            if final_signals.get(bank_code) == "sell":
                continue
                
            weight = min(per_stock, remaining)
            final_signals[bank_code] = ("buy", weight)
            remaining -= weight
            buys.append(f"{bank_code}")

        if buys:
            print(f"[Simple策略] {current_date} 启动银行防守，买入: {', '.join(buys)}")

    def _pre_screen_stocks(self, stock_pool: pd.DataFrame, date: str, data_query) -> List[str]:
        if stock_pool is None or stock_pool.empty:
            return []
        
        sort_col = "amount" if "amount" in stock_pool.columns else "total_mv"
        if len(stock_pool) > self.config.max_candidates:
            stock_pool = stock_pool.nlargest(self.config.max_candidates, sort_col)
        else:
            stock_pool = stock_pool.sort_values(sort_col, ascending=False)
        
        mask = ((stock_pool["total_mv"] >= self.config.market_cap_min) & 
                (stock_pool["total_mv"] <= self.config.market_cap_max))
        
        if "is_st" in stock_pool.columns:
            mask &= stock_pool["is_st"].eq(0)
        
        df = stock_pool[mask].copy()
        if df.empty: return []
        
        if "volume" in df.columns:
            df = df[df["volume"].fillna(0) > 0]
        
        if "ma5" in df.columns:
            df = df[df["close"] > df["ma5"]]
            
        return df["stock_code"].tolist()

    def _get_sell_signals(self, stock_pool, date, data_query):
        if stock_pool is None or stock_pool.empty: return []
        # 跌破MA5即卖出
        sell_mask = stock_pool["ma5"].notna() & (stock_pool["close"] < stock_pool["ma5"])
        return stock_pool.loc[sell_mask, "stock_code"].tolist()