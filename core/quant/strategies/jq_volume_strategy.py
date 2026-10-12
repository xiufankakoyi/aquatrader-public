# strategies/jq_volume_strategy.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List
import time

import pandas as pd

from core.quant.strategies.strategy_framework import StrategyBase
from config.config import Config


@dataclass(frozen=True)
class JQVolumeConfig:
    """集中管理策略阈值，便于调参或复用。"""
    
    # 使用 field(metadata={...}) 定义前端参数元数据
    market_cap_min: float = field(
        default=60 * 10_000,
        metadata={
            "label": "最小市值",
            "group": "市值筛选",
            "type": "float",
            "min": 0,
            "max": 50000000,  # 5000亿（单位：万元）
            "step": 10000,
            "description": "股票最小市值（万元）",
            "optimize": True,
        }
    )
    market_cap_max: float = field(
        default=70 * 10_000,
        metadata={
            "label": "最大市值",
            "group": "市值筛选",
            "type": "float",
            "min": 0,
            "max": 50000000,  # 5000亿（单位：万元）
            "step": 10000,
            "description": "股票最大市值（万元）",
            "optimize": True,
        }
    )
    volume_ratio_threshold: float = field(
        default=2.0,
        metadata={
            "label": "量比阈值",
            "group": "量能筛选",
            "type": "float",
            "min": 0.5,
            "max": 10.0,
            "step": 0.1,
            "description": "量比最低要求",
            "optimize": True,
        }
    )
    turnover_rate_threshold: float = field(
        default=1.5,
        metadata={
            "label": "换手率阈值",
            "group": "量能筛选",
            "type": "float",
            "min": 0.1,
            "max": 20.0,
            "step": 0.1,
            "description": "换手率最低要求（%）",
            "optimize": True,
        }
    )
    momentum_lookback: int = field(
        default=3,
        metadata={
            "label": "动量回看天数",
            "group": "动量筛选",
            "type": "int",
            "min": 1,
            "max": 30,
            "step": 1,
            "description": "计算动量的回看天数",
            "optimize": False,  # 通常不优化此参数
        }
    )
    momentum_threshold: float = field(
        default=0.04,
        metadata={
            "label": "动量阈值",
            "group": "动量筛选",
            "type": "float",
            "min": 0.01,
            "max": 0.2,
            "step": 0.01,
            "description": "动量最低要求（4% = 0.04）",
            "optimize": True,
        }
    )
    ma_days: int = field(
        default=5,
        metadata={
            "label": "均线天数",
            "group": "技术指标",
            "type": "int",
            "min": 3,
            "max": 60,
            "step": 1,
            "description": "移动平均线天数（MA5/MA10等）",
            "optimize": True,
        }
    )
    min_list_days: int = field(
        default=60,
        metadata={
            "label": "最小上市天数",
            "group": "股票筛选",
            "type": "int",
            "min": 30,
            "max": 365,
            "step": 10,
            "description": "股票最小上市天数",
            "optimize": False,  # 通常不优化此参数
        }
    )
    max_candidates: int = field(
        default=1500,
        metadata={
            "label": "最大候选数",
            "group": "股票筛选",
            "type": "int",
            "min": 100,
            "max": 5000,
            "step": 100,
            "description": "预筛选后的最大候选股票数",
            "optimize": False,  # 通常不优化此参数
        }
    )
    position_ratio: float = field(
        default=0.2,
        metadata={
            "label": "仓位比例",
            "group": "仓位管理",
            "type": "float",
            "min": 0.05,
            "max": 1.0,
            "step": 0.05,
            "description": "单只股票仓位比例",
            "optimize": True,
        }
    )
    max_stocks_per_day: int = field(
        default=5,
        metadata={
            "label": "每日最大买入数",
            "group": "仓位管理",
            "type": "int",
            "min": 1,
            "max": 20,
            "step": 1,
            "description": "每日最多买入的股票数量",
            "optimize": False,  # 通常不优化此参数
        }
    )


class JQVolumeStrategy(StrategyBase):
    """
    聚宽移植策略：量比 + 市值筛选，在昨日收盘信号上做买入，今日 MA5 跌破卖出。
    """
    # CHANGED: 内部定义策略名称
    strategy_id = "jq_volume_v1"
    strategy_name = "聚宽量比市值策略"
    needs_today_pool = True  # 策略需要当日股票池数据

    def __init__(self, config: JQVolumeConfig | None = None):
        super().__init__(name=self.strategy_name)
        self.config = config or JQVolumeConfig()

        self.required_days = self.config.min_list_days
        self.position_ratio = self.config.position_ratio
        self.max_stocks_per_day = self.config.max_stocks_per_day

        self._last_date = None
        self._yesterday_cache: Dict[str, pd.DataFrame] = {}
        # 新增：缓存上市日期的 datetime 对象 {stock_code: pd.Timestamp}
        self._list_date_cache: Dict[str, pd.Timestamp] = {}

    # === 引擎入口：每天调用一次 ===
    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        current_date：当前回测日期（引擎传入）
        stock_pool_today：当前日期的股票池
        data_query：数据库查询对象
        """
        if self._last_date is None:
            self._last_date = current_date
            return {}

        previous_date = self._last_date
        self._last_date = current_date

        stock_pool_yesterday = self._get_previous_day_pool(previous_date, data_query)
        if stock_pool_yesterday is None or stock_pool_yesterday.empty:
            return {}

        pre_screened_stocks = self._pre_screen_stocks(stock_pool_yesterday, previous_date)
        if not pre_screened_stocks:
            return {}

        buy_candidates = self._evaluate_buy_candidates(pre_screened_stocks, stock_pool_yesterday)

        if stock_pool_today is None or stock_pool_today.empty:
            sell_signals: List[str] = []
        else:
            sell_signals = self._get_sell_signals(stock_pool_today, current_date, data_query)

        final_signals: Dict[str, str] = {code: "sell" for code in sell_signals}
        for stock in buy_candidates:
            final_signals.setdefault(stock, "buy")

        return final_signals

    # === 以下方法对应聚宽原策略的核心步骤 ===

    def _get_previous_day_pool(self, date: str, data_query):
        """带缓存的昨日股票池，减少重复查询。"""
        if date in self._yesterday_cache:
            return self._yesterday_cache[date]

        try:
            pool = data_query.get_stock_pool(date, use_cache=True, filters={"min_mv": 0})
            if pool is None or pool.empty:
                return None
        except Exception as exc:
            print(f"[{date}] JQ策略: 获取股票池失败: {exc}")
            return None

        self._yesterday_cache[date] = pool
        if len(self._yesterday_cache) > 5:
            self._yesterday_cache.pop(next(iter(self._yesterday_cache)))
        return pool

    def _pre_screen_stocks(self, stock_pool: pd.DataFrame, date: str) -> List[str]:
        """模拟 JQ 的预筛选（市值 + 上市天数 + 板块过滤）。优化版本：避免重复的 pd.to_datetime 计算。"""

        t0 = time.perf_counter()

        # 优化：使用 nlargest 替代 sort_values + head，性能提升显著
        sort_col = "amount" if "amount" in stock_pool.columns else "total_mv"
        top_stocks = stock_pool.nlargest(self.config.max_candidates, sort_col)

        # 优化：先做市值和板块筛选，减少后续计算量
        # 【防御性检查】使用 .get() 或列存在性检查
        is_st_col = top_stocks.get("is_st", pd.Series([False] * len(top_stocks), index=top_stocks.index))
        mask = (
            (top_stocks["total_mv"] >= self.config.market_cap_min)
            & (top_stocks["total_mv"] <= self.config.market_cap_max)
            & (is_st_col == 0)
        )
        if Config.EXCLUDE_KC and "is_kc" in top_stocks.columns:
            mask &= (top_stocks["is_kc"] == 0)
        if Config.EXCLUDE_CY and "is_cy" in top_stocks.columns:
            mask &= (top_stocks["is_cy"] == 0)

        # 先筛选，减少后续处理的数据量
        filtered_stocks = top_stocks[mask]

        if filtered_stocks.empty:
            return []

        # 优化：避免重复的 pd.to_datetime 计算
        current_dt = pd.Timestamp(date)
        
        try:
            # 检查 list_date 是否已经是 datetime 类型
            if pd.api.types.is_datetime64_any_dtype(filtered_stocks["list_date"]):
                days_listed = (current_dt - filtered_stocks["list_date"]).dt.days
            else:
                # 使用缓存避免重复转换：只转换未缓存的股票
                list_dates = []
                for idx, stock_code in enumerate(filtered_stocks["stock_code"]):
                    if stock_code in self._list_date_cache:
                        list_dates.append(self._list_date_cache[stock_code])
                    else:
                        # 只转换一次，存入缓存
                        list_date_str = filtered_stocks.iloc[idx]["list_date"]
                        list_date_dt = pd.to_datetime(list_date_str, errors="coerce")
                        self._list_date_cache[stock_code] = list_date_dt
                        list_dates.append(list_date_dt)
                
                # 向量化计算天数
                list_dates_series = pd.Series(list_dates, index=filtered_stocks.index)
                days_listed = (current_dt - list_dates_series).dt.days
            
            # 过滤上市天数，只保留有效的日期
            valid_days_mask = days_listed.notna() & (days_listed >= self.required_days)
            final_mask = mask & valid_days_mask
            result = top_stocks.loc[final_mask, "stock_code"].tolist()
            dt = (time.perf_counter() - t0) * 1000
            print(f"[PROFILE][JQ] _pre_screen_stocks {date} - {len(stock_pool)} rows -> {len(result)} stocks, {dt:.1f} ms")
            return result
        except Exception as exc:
            print(f"[{date}] JQ策略: 过滤上市日期失败: {exc}")
            return []

    def _evaluate_buy_candidates(
        self, pre_screened_codes: List[str], stock_pool_snapshot: pd.DataFrame
    ) -> List[str]:
        """直接使用数据库字段完成量比/动量筛选。优化版本：减少 copy() 和内存分配。"""
        t0 = time.perf_counter()
        if not pre_screened_codes or stock_pool_snapshot is None or stock_pool_snapshot.empty:
            return []

        required_cols = {"stock_code", "volume_ratio", "turnover_rate", "close", "open", "prev_close", "ma5"}
        missing_cols = required_cols - set(stock_pool_snapshot.columns)
        if missing_cols:
            print(f"[JQ策略] 股票池缺少列 {missing_cols}，无法完成筛选")
            return []
        
        # 【防御性检查】确保关键字段存在，如果缺失则使用默认值
        if "is_limit_up" not in stock_pool_snapshot.columns:
            stock_pool_snapshot["is_limit_up"] = False
        if "is_limit_down" not in stock_pool_snapshot.columns:
            stock_pool_snapshot["is_limit_down"] = False
        if "ma60" not in stock_pool_snapshot.columns:
            # 尝试用 ma20 或 ma10 或 ma5 填充
            if "ma20" in stock_pool_snapshot.columns:
                stock_pool_snapshot["ma60"] = stock_pool_snapshot["ma20"]
            elif "ma10" in stock_pool_snapshot.columns:
                stock_pool_snapshot["ma60"] = stock_pool_snapshot["ma10"]
            elif "ma5" in stock_pool_snapshot.columns:
                stock_pool_snapshot["ma60"] = stock_pool_snapshot["ma5"]
            else:
                stock_pool_snapshot["ma60"] = stock_pool_snapshot.get("close", 0.0)

        # 优化：只取子集，避免 copy 整个大表
        candidates = stock_pool_snapshot[stock_pool_snapshot["stock_code"].isin(pre_screened_codes)].copy()
        
        if candidates.empty:
            return []

        # 去重
        candidates = candidates.drop_duplicates(subset=["stock_code"])

        # 优化：使用 inplace=True 避免创建新对象
        # NOTE: 为避免 Pandas 3.0 链式赋值 FutureWarning，这里改为显式赋值形式
        candidates["volume_ratio"] = candidates["volume_ratio"].fillna(0)
        candidates["turnover_rate"] = candidates["turnover_rate"].fillna(0)
        
        # 优化：向量化计算动量，直接用于 mask
        momentum_mask = ((candidates["close"] / candidates["ma5"]) - 1) >= self.config.momentum_threshold

        # CHANGED: 尾盘买入 - 大阳线条件：close >= open * 1.03（优先使用 open/close，若无则用 change_pct）
        has_open_close = "open" in candidates.columns and "close" in candidates.columns
        has_change_pct = "change_pct" in candidates.columns
        
        if has_open_close:
            # 优先使用 open/close 判定大阳线
            big_yang_mask = candidates["close"] >= candidates["open"] * 1.03
            # 防御：如果 open 为 0 或缺失，回退到 change_pct
            if has_change_pct:
                big_yang_mask = big_yang_mask | (
                    (candidates["open"].isna() | (candidates["open"] <= 0)) & 
                    (candidates["change_pct"] >= 3.0)
                )
        elif has_change_pct:
            # 只有 change_pct 时，使用 change_pct >= 3%
            big_yang_mask = candidates["change_pct"] >= 3.0
        else:
            # 防御：两者都没有时，跳过此条件（TODO: 需要补充数据源）
            print("[JQ策略] 警告：缺少 open/close 或 change_pct，无法判定大阳线，跳过该条件")
            big_yang_mask = pd.Series(True, index=candidates.index)

        # CHANGED: 尾盘买入 - 排除冲高回落：(high - close) / close <= 0.03
        if "high" in candidates.columns and "close" in candidates.columns:
            pullback_mask = (
                (candidates["high"] - candidates["close"]) / candidates["close"].replace(0, pd.NA)
            ) <= 0.03
            pullback_mask = pullback_mask.fillna(False)  # 处理除零或缺失值
        else:
            # TODO: 若无 high 列，保留注释但不影响其他逻辑
            pullback_mask = pd.Series(True, index=candidates.index)

        # 【防御性检查】排除涨停股票（如果字段存在）
        limit_up_mask = pd.Series(True, index=candidates.index)
        if "is_limit_up" in candidates.columns:
            limit_up_mask = ~candidates["is_limit_up"].fillna(False)
        elif "limit_up" in candidates.columns:
            # 兼容 limit_up 字段（可能是数值类型）
            limit_up_mask = (candidates["limit_up"].fillna(0) == 0)
        
        # 优化：合并所有条件，避免创建中间变量
        # 【防御性检查】使用 try-except 保护，防止数据缺失导致回测中断
        try:
            # 【防御性检查】确保关键字段存在，使用 .get() 或 fillna 保护
            volume_ratio = candidates.get("volume_ratio", pd.Series([0.0] * len(candidates), index=candidates.index)).fillna(0)
            turnover_rate = candidates.get("turnover_rate", pd.Series([0.0] * len(candidates), index=candidates.index)).fillna(0)
            close = candidates.get("close", pd.Series([0.0] * len(candidates), index=candidates.index))
            prev_close = candidates.get("prev_close", close)  # 如果没有 prev_close，使用 close
            
            final_mask = (
                (volume_ratio >= self.config.volume_ratio_threshold)
                & (turnover_rate >= self.config.turnover_rate_threshold)
                & big_yang_mask  # CHANGED: 尾盘买入 - 大阳线条件
                & (close > prev_close)
                & momentum_mask  # 优化：使用预计算的动量 mask
                & pullback_mask  # CHANGED: 尾盘买入 - 排除冲高回落
                & limit_up_mask  # 【防御性检查】排除涨停
            )
            result = candidates.loc[final_mask, "stock_code"].tolist()
        except Exception as exc:
            print(f"[JQ策略] _evaluate_buy_candidates 计算失败: {exc}")
            import traceback
            traceback.print_exc()
            result = []  # 返回空列表，不中断回测
        
        dt = (time.perf_counter() - t0) * 1000
        print(f"[PROFILE][JQ] _evaluate_buy_candidates {len(pre_screened_codes)} pre-screened -> {len(result)} buys, {dt:.1f} ms")
        return result

    def _get_sell_signals(self, stock_pool, date, data_query):
        """向量化计算：当日 MA5 与收盘价，收盘价跌破 MA5 则卖出。"""
        if stock_pool is None or stock_pool.empty:
            return []

        if "ma5" not in stock_pool.columns:
            try:
                stock_list = stock_pool["stock_code"].tolist()
                snapshot = self.get_moving_average_snapshot(
                    data_query=data_query,
                    stock_codes=stock_list,
                    end_date=date,
                    column="close",
                    window=self.config.ma_days,
                    min_periods=self.config.ma_days,
                )
                if snapshot.empty:
                    return []

                snapshot = snapshot.dropna(subset=["ma_value"])
                return snapshot.loc[snapshot["close"] < snapshot["ma_value"], "stock_code"].tolist()

            except Exception as exc:
                print(f"[{date}] JQ策略: 计算卖出信号失败: {exc}")
                return []

        # 【防御性检查】使用 .get() 和 try-except 保护
        try:
            ma5 = stock_pool.get("ma5", pd.Series([0.0] * len(stock_pool), index=stock_pool.index))
            close = stock_pool.get("close", pd.Series([0.0] * len(stock_pool), index=stock_pool.index))
            sell_mask = ma5.notna() & (close < ma5)
            return stock_pool.loc[sell_mask, "stock_code"].tolist()
        except Exception as exc:
            print(f"[{date}] JQ策略: 计算卖出信号失败（防御性检查）: {exc}")
            return []
