# strategies/simple_volume_v5.py
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List

import pandas as pd

from core.quant.strategies.simple_volume_v3 import SimpleVolumeConfig, SimpleVolumeStrategyV3


class SimpleVolumeStrategyV5(SimpleVolumeStrategyV3):
    """
    聚宽量比市值策略 V5 (增强严格主升浪版)
    
    V3版本的进阶改进：
    1. 加入趋势强度过滤，主升浪判断更精准
    2. 增加大阴线卖点保护
    3. 优化止盈止损逻辑
    4. 增加量价结构过滤
    """
    strategy_id = "simple_volume_v5"
    strategy_name = "聚宽量比市值策略V5_趋势增强"

    # V5版本的参数规范（继承并扩展V3的参数）
    PARAM_SPEC = SimpleVolumeStrategyV3.PARAM_SPEC.copy()
    PARAM_SPEC.extend([
        {
            "key": "trend_confirmation_threshold",
            "label": "趋势确认阈值",
            "group": "趋势过滤",
            "type": "float",
            "min": 1.0,
            "max": 1.5,
            "step": 0.01,
            "default": 1.03,
            "optimize": True,
            "description": "趋势强度确认系数，值越大要求趋势越强",
        },
        {
            "key": "bear_candle_threshold",
            "label": "大阴线阈值(%)",
            "group": "卖点保护",
            "type": "float",
            "min": -10.0,
            "max": 0.0,
            "step": -0.1,
            "default": -2.5,
            "optimize": True,
            "description": "当日跌幅超过该阈值时强制卖出",
        },
        {
            "key": "ma_distance_ratio",
            "label": "均线远离系数",
            "group": "卖点保护",
            "type": "float",
            "min": 0.85,
            "max": 0.99,
            "step": 0.01,
            "default": 0.95,
            "optimize": True,
            "description": "当价格/MA5低于该值时卖出（用于大阴线保护）",
        },
    ])

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
        # V5特有参数
        trend_confirmation_threshold: float = 1.03,
        bear_candle_threshold: float = -2.5,
        ma_distance_ratio: float = 0.95,
    ):
        # 初始化父类
        super().__init__(
            config=config,
            market_cap_min=market_cap_min,
            market_cap_max=market_cap_max,
            volume_ratio_threshold=volume_ratio_threshold,
            turnover_rate_threshold=turnover_rate_threshold,
            ma_days=ma_days,
            min_list_days=min_list_days,
            max_candidates=max_candidates,
            position_ratio=position_ratio,
            max_stocks_per_day=max_stocks_per_day,
            bank_codes=bank_codes,
            bank_position_ratio=bank_position_ratio,
            bank_safe_ma=bank_safe_ma,
        )
        
        # V5特有参数
        self.trend_confirmation_threshold = trend_confirmation_threshold
        self.bear_candle_threshold = bear_candle_threshold
        self.ma_distance_ratio = ma_distance_ratio

    def _pre_screen_stocks(self, stock_pool: pd.DataFrame, date: str, data_query) -> List[str]:
        """
        V5增强版初筛：
        在V3的基础上增加趋势强度和量价结构过滤
        """
        # 调用V3的基础筛选
        base_candidates = super()._pre_screen_stocks(stock_pool, date, data_query)
        
        if not base_candidates or stock_pool.empty:
            return []
        
        # 进一步过滤候选股
        candidates_df = stock_pool[stock_pool["stock_code"].isin(base_candidates)].copy()
        
        # 1. 趋势强度过滤
        trend_mask = pd.Series(True, index=candidates_df.index)
        
        # 检查MA5与MA20的关系，要求趋势强度
        if "ma5" in candidates_df.columns and "ma20" in candidates_df.columns:
            ma5 = candidates_df["ma5"]
            ma20 = candidates_df["ma20"]
            valid_ma = ma5.notna() & ma20.notna() & (ma20 > 0)
            trend_mask &= ~valid_ma | (ma5 / ma20 >= self.trend_confirmation_threshold)
        
        # 2. 量价结构过滤：要求当日收盘价高于开盘价
        price_mask = candidates_df["close"] > candidates_df["open"]
        
        # 3. 量能持续性检查：要求当日量比大于前一日
        if "volume_ratio" in candidates_df.columns:
            # 注：由于没有前一日数据，这里简化处理，确保量比足够大
            vol_continue_mask = candidates_df["volume_ratio"] > self.config.volume_ratio_threshold + 0.5
        else:
            vol_continue_mask = pd.Series(True, index=candidates_df.index)
        
        # 综合过滤
        final_mask = trend_mask & price_mask & vol_continue_mask
        enhanced_candidates = candidates_df.loc[final_mask, "stock_code"].tolist()
        
        return enhanced_candidates

    def _evaluate_buy_candidates_strict(
        self,
        pre_screened_codes: List[str],
        stock_pool_snapshot: pd.DataFrame,
        current_date,
        data_query,
    ) -> List[str]:
        """
        V5增强版主升浪筛选：
        在V3的严格基础上增加趋势强度确认
        """
        # 调用V3的基础评估
        base_candidates = super()._evaluate_buy_candidates_strict(
            pre_screened_codes, stock_pool_snapshot, current_date, data_query
        )
        
        if not base_candidates or stock_pool_snapshot.empty:
            return []
        
        # 进一步筛选
        candidates = (
            stock_pool_snapshot[stock_pool_snapshot["stock_code"].isin(base_candidates)]
            .copy()
            .drop_duplicates(subset=["stock_code"])
        )
        
        # 趋势强度增强过滤
        strength_mask = pd.Series(True, index=candidates.index)
        
        # 1. 更严格的均线关系检查
        if "ma5" in candidates.columns and "ma10" in candidates.columns and "ma20" in candidates.columns:
            ma5 = candidates["ma5"]
            ma10 = candidates["ma10"]
            ma20 = candidates["ma20"]
            
            valid_ma = ma5.notna() & ma10.notna() & ma20.notna()
            
            # 检查MA5与MA10的强度关系
            ma5_ma10_relation = (ma5 / ma10) >= self.trend_confirmation_threshold
            
            # 检查MA10与MA20的强度关系
            ma10_ma20_relation = (ma10 / ma20) >= self.trend_confirmation_threshold
            
            strength_mask &= ~valid_ma | (ma5_ma10_relation & ma10_ma20_relation)
        
        # 2. 检查股价与均线的距离，确保股价强势
        if "close" in candidates.columns and "ma5" in candidates.columns:
            close = candidates["close"]
            ma5 = candidates["ma5"]
            valid_price = close.notna() & ma5.notna() & (ma5 > 0)
            strength_mask &= ~valid_price | (close / ma5 > 1.01)  # 要求收盘价高于MA5至少1%
        
        # 3. 成交量与MA5量的关系，要求放量
        if "volume" in candidates.columns and "volume_ma5" in candidates.columns:
            volume = candidates["volume"]
            volume_ma5 = candidates["volume_ma5"]
            valid_volume = volume.notna() & volume_ma5.notna() & (volume_ma5 > 0)
            strength_mask &= ~valid_volume | (volume / volume_ma5 > 1.5)  # 要求成交量大于MA5量1.5倍
        
        # 应用过滤
        final_candidates = candidates.loc[strength_mask, "stock_code"].tolist()
        
        return final_candidates

    def _get_sell_signals(self, stock_pool, current_date, data_query):
        """
        V5增强版卖出逻辑：
        在V3的基础上增加大阴线强制卖出保护
        """
        if stock_pool is None or stock_pool.empty:
            return []
        
        sell_signals = []
        
        # 1. 收集基础卖出信号（MA5跌破）
        ma5_sell_candidates = super()._get_sell_signals(stock_pool, current_date, data_query)
        sell_signals.extend(ma5_sell_candidates)
        
        # 2. 大阴线强制卖出保护
        bear_mask = stock_pool["close"] / stock_pool["open"] - 1 <= self.bear_candle_threshold / 100
        bear_candidates = stock_pool.loc[bear_mask, "stock_code"].tolist()
        
        # 3. 远离均线卖出保护
        ma_distance_mask = pd.Series(False, index=stock_pool.index)
        if "close" in stock_pool.columns and "ma5" in stock_pool.columns:
            close = stock_pool["close"]
            ma5 = stock_pool["ma5"]
            valid_distance = close.notna() & ma5.notna() & (ma5 > 0)
            ma_distance_mask = valid_distance & (close / ma5 < self.ma_distance_ratio)
        
        distance_candidates = stock_pool.loc[ma_distance_mask, "stock_code"].tolist()
        
        # 合并卖出信号，避免重复
        all_sell_candidates = set(sell_signals + bear_candidates + distance_candidates)
        
        # 确保只处理持仓中的股票
        current_positions = set(getattr(self, "current_portfolio", {}).keys())
        final_sell_signals = list(current_positions & all_sell_candidates)
        
        # 记录卖出原因
        for code in final_sell_signals:
            if code in bear_candidates:
                print(f"[Simple策略V5] {current_date} 大阴线卖出: {code}")
            elif code in distance_candidates:
                print(f"[Simple策略V5] {current_date} 远离均线卖出: {code}")
            elif code in ma5_sell_candidates:
                print(f"[Simple策略V5] {current_date} 跌破MA5卖出: {code}")
        
        return final_sell_signals