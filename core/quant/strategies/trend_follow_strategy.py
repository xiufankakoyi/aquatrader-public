# strategies/trend_follow_strategy.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List
import time

import pandas as pd
import numpy as np


def _is_empty(data) -> bool:
    """检查数据是否为空，支持 DataFrame、Polars DataFrame 和 numpy array"""
    if data is None:
        return True
    if isinstance(data, np.ndarray):
        return data.size == 0
    if hasattr(data, 'is_empty'):
        return data.is_empty()
    if hasattr(data, 'empty'):
        return data.empty
    return False


def _ensure_dataframe(data):
    """确保数据是 DataFrame 格式，支持 numpy array 转换"""
    if data is None:
        return None
    if isinstance(data, np.ndarray):
        if data.size == 0:
            return pd.DataFrame()
        if len(data.shape) == 1:
            return pd.DataFrame({'value': data})
        return pd.DataFrame(data)
    return data

from core.quant.strategies.strategy_framework import StrategyBase
from config.config import Config


@dataclass(frozen=True)
class TrendFollowConfig:
    """
    主升浪趋势策略参数配置

    - 买：MA5 / MA10 多头，股价略高于 MA5，温和放量，不追暴涨大阳线
    - 卖：收盘价跌破 MA10（趋势破位离场），若无 MA10 列则退化为 MA5
    """

    # 市值过滤（这里用“亿元”逻辑：在 DB 里仍然是“万”为单位）
    market_cap_min: float = 50 * 10_000   # 50 亿
    market_cap_max: float = 500 * 10_000  # 500 亿

    # 量能相关
    volume_ratio_threshold: float = 1.2   # 轻微放量即可，不需要暴量
    max_upper_shadow: float = 0.03        # 上影线不超过 3%

    # 均线参数
    ma_short_days: int = 5
    ma_long_days: int = 10

    # 价格相对于短均线的乖离（避免太贴或太远）
    price_ma_gap_min: float = 0.005       # 收盘略高于 MA5：>= 0.5%
    price_ma_gap_max: float = 0.06        # 不追太远：<= 6%

    # MA5 相对 MA10 的最小“趋势强度”
    trend_strength_min: float = 0.005     # MA5 至少比 MA10 高 0.5%

    # 上市天数 & 预筛股票数量
    min_list_days: int = 60
    max_candidates: int = 1500

    # 仓位管理
    position_ratio: float = 0.25          # 单只股票最大仓位
    max_stocks_per_day: int = 5           # 单日最多买入股票数


class TrendFollowStrategy(StrategyBase):
    """
    主升浪趋势跟随策略：
    - 信号在昨日收盘生成，次日买入
    - 只参与市值中等、趋势明确、温和放量的个股
    - 收盘价跌破 MA10（或 MA5）视为趋势破位，全部卖出
    """

    strategy_id = "trend_follow_main_wave"
    strategy_name = "主升浪趋势跟随策略"
    needs_today_pool = True  # 策略需要当日股票池数据

    def __init__(self, config: TrendFollowConfig | None = None):
        super().__init__(name=self.strategy_name)
        self.config = config or TrendFollowConfig()

        self.required_days = self.config.min_list_days
        self.position_ratio = self.config.position_ratio
        self.max_stocks_per_day = self.config.max_stocks_per_day

        self._last_date = None
        self._yesterday_cache: Dict[str, pd.DataFrame] = {}
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

        # 昨日股票池快照：用于生成"次日买入信号"
        stock_pool_yesterday = self._get_previous_day_pool(previous_date, data_query)
        if _is_empty(stock_pool_yesterday):
            return {}

        # 预筛 + 趋势择股（在昨日 K 线上完成）
        pre_screened_stocks = self._pre_screen_stocks(stock_pool_yesterday, previous_date)
        if not pre_screened_stocks:
            return {}

        buy_candidates = self._evaluate_buy_candidates(pre_screened_stocks, stock_pool_yesterday)

        # 今日卖出信号：趋势破位就走
        if _is_empty(stock_pool_today):
            sell_signals: List[str] = []
        else:
            sell_signals = self._get_sell_signals(stock_pool_today, current_date, data_query)

        final_signals: Dict[str, str] = {code: "sell" for code in sell_signals}
        for stock in buy_candidates[: self.max_stocks_per_day]:
            final_signals.setdefault(stock, "buy")

        return final_signals

    # === 工具方法 ===

    def _get_previous_day_pool(self, date: str, data_query):
        """带缓存的昨日股票池，减少重复查询。"""
        if date in self._yesterday_cache:
            return self._yesterday_cache[date]

        try:
            pool = data_query.get_stock_pool(date, use_cache=True, filters={"min_mv": 0})
            if _is_empty(pool):
                return None
        except Exception as exc:
            print(f"[{date}] Trend策略: 获取股票池失败: {exc}")
            return None

        self._yesterday_cache[date] = pool
        if len(self._yesterday_cache) > 5:
            self._yesterday_cache.pop(next(iter(self._yesterday_cache)))
        return pool

    def _pre_screen_stocks(self, stock_pool: pd.DataFrame, date: str) -> List[str]:
        """
        预筛选：市值 + 板块 + 上市天数
        基本完全复用 JQ 策略的高性能写法。
        """
        t0 = time.perf_counter()

        stock_pool = _ensure_dataframe(stock_pool)
        if _is_empty(stock_pool):
            return []

        sort_col = "amount" if "amount" in stock_pool.columns else "total_mv"
        top_stocks = stock_pool.nlargest(self.config.max_candidates, sort_col)

        mask = (
            (top_stocks["total_mv"] >= self.config.market_cap_min)
            & (top_stocks["total_mv"] <= self.config.market_cap_max)
            & (top_stocks["is_st"] == 0)
        )
        if Config.EXCLUDE_KC and "is_kc" in top_stocks.columns:
            mask &= top_stocks["is_kc"] == 0
        if Config.EXCLUDE_CY and "is_cy" in top_stocks.columns:
            mask &= top_stocks["is_cy"] == 0

        filtered_stocks = top_stocks[mask].copy()
        if filtered_stocks.empty:
            return []

        current_dt = pd.Timestamp(date)

        try:
            if pd.api.types.is_datetime64_any_dtype(filtered_stocks["list_date"]):
                days_listed = (current_dt - filtered_stocks["list_date"]).dt.days
            else:
                list_dates = []
                for idx, stock_code in enumerate(filtered_stocks["stock_code"]):
                    if stock_code in self._list_date_cache:
                        list_dates.append(self._list_date_cache[stock_code])
                    else:
                        list_date_str = filtered_stocks.iloc[idx]["list_date"]
                        list_date_dt = pd.to_datetime(list_date_str, errors="coerce")
                        self._list_date_cache[stock_code] = list_date_dt
                        list_dates.append(list_date_dt)

                list_dates_series = pd.Series(list_dates, index=filtered_stocks.index)
                days_listed = (current_dt - list_dates_series).dt.days

            valid_days_mask = days_listed.notna() & (days_listed >= self.required_days)
            # 修复：确保索引一致，使用 filtered_stocks 而不是 top_stocks
            final_mask = valid_days_mask.reindex(filtered_stocks.index, fill_value=False)
            result = filtered_stocks.loc[final_mask, "stock_code"].tolist()

            dt_ms = (time.perf_counter() - t0) * 1000
            print(
                f"[PROFILE][Trend] _pre_screen_stocks {date} - "
                f"{len(stock_pool)} rows -> {len(result)} stocks, {dt_ms:.1f} ms"
            )
            return result
        except Exception as exc:
            print(f"[{date}] Trend策略: 过滤上市日期失败: {exc}")
            return []

    def _evaluate_buy_candidates(
        self, pre_screened_codes: List[str], stock_pool_snapshot: pd.DataFrame
    ) -> List[str]:
        """
        在“昨日收盘”这根 K 线上做趋势筛选：
        - MA5 / MA10 多头
        - 股价略高于 MA5，不追太远
        - 温和放量（量比略大于 1 即可）
        - 无明显长上影线
        """
        t0 = time.perf_counter()
        if not pre_screened_codes or _is_empty(stock_pool_snapshot):
            return []

        stock_pool_snapshot = _ensure_dataframe(stock_pool_snapshot)
        if _is_empty(stock_pool_snapshot):
            return []

        required_cols = {"stock_code", "close", "prev_close", "ma5"}
        missing_cols = required_cols - set(stock_pool_snapshot.columns)
        if missing_cols:
            print(f"[Trend策略] 股票池缺少列 {missing_cols}，无法完成筛选")
            return []

        candidates = stock_pool_snapshot[
            stock_pool_snapshot["stock_code"].isin(pre_screened_codes)
        ].copy()
        if candidates.empty:
            return []

        candidates = candidates.drop_duplicates(subset=["stock_code"])

        # 基本价格 & 均线
        candidates["close"] = candidates["close"].astype(float)
        candidates["ma5"] = candidates["ma5"].astype(float)

        has_ma10 = "ma10" in candidates.columns
        if has_ma10:
            candidates["ma10"] = candidates["ma10"].astype(float)

        # 价格相对 MA5 的乖离：略高于 MA5，避免又贴又飘
        price_ma_gap = (candidates["close"] / candidates["ma5"]) - 1
        price_gap_mask = (
            (price_ma_gap >= self.config.price_ma_gap_min)
            & (price_ma_gap <= self.config.price_ma_gap_max)
        )

        # MA5 > MA10，且略有乖离，说明趋势比较干净
        if has_ma10:
            ma_gap = (candidates["ma5"] / candidates["ma10"]) - 1
            trend_mask = ma_gap >= self.config.trend_strength_min
        else:
            # 没有 ma10 列时，只要求 close > ma5 即可
            trend_mask = candidates["close"] > candidates["ma5"]

        # 量比：轻微放量即可，不需要暴量
        if "volume_ratio" in candidates.columns:
            candidates["volume_ratio"] = candidates["volume_ratio"].fillna(0.0)
            volume_mask = candidates["volume_ratio"] >= self.config.volume_ratio_threshold
        else:
            volume_mask = pd.Series(True, index=candidates.index)

        # 无长上影线：(high - close)/close <= 3%
        if "high" in candidates.columns:
            upper = (candidates["high"] - candidates["close"]) / candidates["close"].replace(0, pd.NA)
            upper_mask = upper <= self.config.max_upper_shadow
            upper_mask = upper_mask.fillna(False)
        else:
            upper_mask = pd.Series(True, index=candidates.index)

        # 不买跌停/大阴线：要求收盘 >= 昨收
        basic_price_mask = candidates["close"] >= candidates["prev_close"]

        final_mask = (
            price_gap_mask
            & trend_mask
            & volume_mask
            & upper_mask
            & basic_price_mask
        )

        result = candidates.loc[final_mask, "stock_code"].tolist()
        dt_ms = (time.perf_counter() - t0) * 1000
        print(
            f"[PROFILE][Trend] _evaluate_buy_candidates "
            f"{len(pre_screened_codes)} pre-screened -> {len(result)} buys, {dt_ms:.1f} ms"
        )
        return result

    def _get_sell_signals(self, stock_pool, date, data_query):
        """
        趋势破位卖出逻辑：
        - 优先用 MA10：收盘价 < MA10 视为趋势破坏
        - 若无 MA10 列，则退化为 MA5
        """
        if _is_empty(stock_pool):
            return []

        stock_pool = _ensure_dataframe(stock_pool)
        if _is_empty(stock_pool):
            return []

        long_ma_col = f"ma{self.config.ma_long_days}"
        short_ma_col = f"ma{self.config.ma_short_days}"

        # 优先使用 MA10，如果没有则用 MA5（与 JQ 策略兼容）
        if long_ma_col not in stock_pool.columns and short_ma_col in stock_pool.columns:
            long_ma_col = short_ma_col

        if long_ma_col not in stock_pool.columns:
            # 使用回测引擎提供的 MA 计算接口兜底
            try:
                stock_list = stock_pool["stock_code"].tolist()
                snapshot = self.get_moving_average_snapshot(
                    data_query=data_query,
                    stock_codes=stock_list,
                    end_date=date,
                    column="close",
                    window=self.config.ma_long_days,
                    min_periods=self.config.ma_long_days,
                )
                if _is_empty(snapshot):
                    return []

                snapshot = snapshot.dropna(subset=["ma_value"])
                return snapshot.loc[snapshot["close"] < snapshot["ma_value"], "stock_code"].tolist()

            except Exception as exc:
                print(f"[{date}] Trend策略: 计算卖出信号失败: {exc}")
                return []

        sell_mask = stock_pool[long_ma_col].notna() & (stock_pool["close"] < stock_pool[long_ma_col])
        return stock_pool.loc[sell_mask, "stock_code"].tolist()
