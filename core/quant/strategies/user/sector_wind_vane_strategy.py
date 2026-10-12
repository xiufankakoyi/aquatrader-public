"""
板块风向标策略（Sector Wind Vane Strategy）

基于市场相对强弱，用 A 股申万/通达信行业字段把全市场股票聚合成"虚拟板块"，
通过板块内部个股的相对表现识别板块所处周期阶段：

    加速 → 一致 → 分歧 → 冷漠 → （下一轮）一致

并在板块状态转换的关键节点识别三类核心品种：
    · 领涨品种：板块向上攻击时涨幅前列、带动跟风
    · 抗跌品种：板块调整/冷漠时跌得少甚至逆市
    · 扛旗品种：板块分歧日承受抛压后仍有强承接

交易逻辑：
    · 冷漠转一致：买入提前止跌的抗跌核心（潜伏下一轮一致）
    · 一致延续：持有或加仓领涨核心
    · 加速：不新开仓，持有观察
    · 分歧：未出现扛旗品种则卖出；出现扛旗品种则继续持有

数据依赖：
    · data_query.get_stock_basic() 中的 industry 字段（必填）
    · stock_pool_today 中的 stock_code / symbol_code、open/high/low/close/volume
    · data_query.get_stock_history() 获取个股近期 K 线

注意：
    1. 本策略不读取任何外部文件、不联网，所有计算在内存完成。
    2. 为了避免过拟合，状态判断尽量使用排名/分位等相对指标，而非固定阈值。
    3. 当前版本用本地 stock_info.industry 作为板块代理；若后续接入通达信/同花顺
       板块数据，只需替换 _load_sector_map 中的行业映射来源即可。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.quant.strategies.strategy_framework import StrategyBase


class SectorWindVaneStrategy(StrategyBase):
    """
    板块风向标策略

    通过板块相对强弱识别周期位置，并在关键转换点交易核心品种。
    """

    strategy_id = "user_sector_wind_vane_strategy"
    strategy_name = "SectorWindVaneStrategy"

    # -------------------------- 可配置参数 --------------------------
    LOOKBACK_DAYS = 20           # 个股历史回看天数
    SECTOR_HISTORY_DAYS = 10     # 板块状态历史缓存天数
    MIN_STOCKS_PER_SECTOR = 5    # 板块最小成分股数量
    TOP_SECTOR_COUNT = 5         # 每日重点关注的强势板块数量
    MAX_HOLDINGS = 5             # 最大持仓数量
    POSITION_WEIGHT = 0.15       # 单票目标仓位（相对总资产）

    def __init__(self, name=None):
        super().__init__(name)

        # 板块状态历史：{sector_name: [{date, state, metrics}]}
        self._sector_history: Dict[str, List[Dict[str, Any]]] = {}

        # 个股近期表现缓存：{stock_code: [{date, sector, return_pct, volume_ratio}]}
        self._stock_history: Dict[str, List[Dict[str, Any]]] = {}

        # 最近一次交易记录（用于避免当日重复开平仓）
        self._last_trade_date: Optional[str] = None

        # 已识别出的当前核心品种：{
        #   code: {"role": "leader"/"resilient"/"flag_bearer", "sector": str}
        # }
        self._core_stocks: Dict[str, Dict[str, str]] = {}

        # 板块映射缓存（industry -> [stock_code]）
        self._sector_map: Optional[Dict[str, List[str]]] = None

    # ========================== 对外入口 ==========================

    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        每日生成交易信号。

        参数:
            current_date (str): 当前回测日期，格式 YYYY-MM-DD
            stock_pool_today (pd.DataFrame): 当日全市场股票行情
            data_query (UnifiedDataQuery): 数据查询对象

        返回:
            Dict[str, str]: {stock_code: 'buy'/'sell'/'hold'}
        """
        signals: Dict[str, str] = {}

        # 1. 数据完整性检查
        if stock_pool_today is None or stock_pool_today.empty:
            return signals

        # 2. 加载行业映射并构造板块结构
        sector_map = self._load_sector_map(data_query)
        if not sector_map:
            return signals

        # 3. 清洗当日行情，计算日内指标
        daily_df = self._prepare_daily_frame(stock_pool_today)
        if daily_df.empty:
            return signals

        # 4. 按行业聚合，计算板块层面指标
        sector_stats = self._compute_sector_stats(daily_df, sector_map)
        if not sector_stats:
            return signals

        # 5. 判断每个板块当前状态
        sector_states = self._classify_sector_states(sector_stats, current_date)

        # 6. 识别三类核心品种
        core_stocks = self._identify_core_stocks(
            daily_df, sector_map, sector_states, current_date
        )
        self._core_stocks = core_stocks

        # 7. 更新历史缓存
        self._update_histories(sector_states, daily_df, sector_map, current_date)

        # 8. 基于状态转换生成交易信号
        signals = self._generate_trade_signals(
            current_date, daily_df, sector_states, core_stocks
        )

        self._last_trade_date = current_date
        return signals

    # ========================== 数据准备 ==========================

    def _load_sector_map(self, data_query) -> Dict[str, List[str]]:
        """
        从 stock_basic 加载行业映射。

        返回:
            Dict[str, List[str]]: {行业名: [股票代码列表]}
        """
        if self._sector_map is not None:
            return self._sector_map

        try:
            basic_df = data_query.get_stock_basic()
        except Exception:
            return {}

        if basic_df is None or basic_df.empty:
            return {}

        # 兼容可能的列名
        code_col = self._find_column(basic_df, ["stock_code", "symbol_code", "symbol", "ts_code", "code"])
        industry_col = self._find_column(basic_df, ["industry", "sector", "sw_industry", "industry_name"])

        if not code_col or not industry_col:
            return {}

        basic_df = basic_df[[code_col, industry_col]].dropna()
        basic_df[industry_col] = basic_df[industry_col].astype(str).str.strip()
        basic_df = basic_df[basic_df[industry_col].replace("", np.nan).notna()]

        sector_map: Dict[str, List[str]] = {}
        for _, row in basic_df.iterrows():
            code = str(row[code_col]).strip()
            sector = str(row[industry_col]).strip()
            if not code or not sector:
                continue
            sector_map.setdefault(sector, []).append(code)

        # 过滤成分股太少的行业
        sector_map = {
            s: codes for s, codes in sector_map.items()
            if len(codes) >= self.MIN_STOCKS_PER_SECTOR
        }

        self._sector_map = sector_map
        return sector_map

    def _prepare_daily_frame(self, stock_pool_today: pd.DataFrame) -> pd.DataFrame:
        """
        把 stock_pool_today 标准化成统一列名，并计算日内涨跌幅、振幅等指标。
        """
        df = stock_pool_today.copy()

        # 识别代码列和 OHLCV 列
        code_col = self._find_column(df, ["stock_code", "symbol_code", "symbol", "ts_code", "code"])
        open_col = self._find_column(df, ["open", "open_price"])
        high_col = self._find_column(df, ["high", "high_price"])
        low_col = self._find_column(df, ["low", "low_price"])
        close_col = self._find_column(df, ["close", "close_price"])
        pre_close_col = self._find_column(df, ["pre_close", "prev_close", "pre_close_price"])
        volume_col = self._find_column(df, ["volume", "vol", "trade_volume"])

        if not code_col or not close_col:
            return pd.DataFrame()

        df = df.rename(columns={
            code_col: "code",
            open_col: "open",
            high_col: "high",
            low_col: "low",
            close_col: "close",
            volume_col: "volume",
        })

        # 计算涨跌幅
        if pre_close_col and pre_close_col in df.columns:
            df["return_pct"] = (df["close"] - df[pre_close_col]) / df[pre_close_col] * 100
        elif "open" in df.columns:
            # 没有昨收时用 open 近似
            df["return_pct"] = (df["close"] - df["open"]) / df["open"] * 100
        else:
            df["return_pct"] = 0.0

        # 计算振幅
        if all(c in df.columns for c in ["high", "low", "close"]):
            df["amplitude"] = (df["high"] - df["low"]) / df["close"] * 100
        else:
            df["amplitude"] = df["return_pct"].abs()

        # 实体比例
        if "open" in df.columns:
            df["body_pct"] = (df["close"] - df["open"]) / df["open"] * 100
        else:
            df["body_pct"] = df["return_pct"]

        # 下影线比例（相对振幅）
        if all(c in df.columns for c in ["open", "high", "low", "close"]):
            lower_shadow = np.minimum(df["open"], df["close"]) - df["low"]
            upper_shadow = df["high"] - np.maximum(df["open"], df["close"])
            df["lower_shadow_ratio"] = lower_shadow / (df["high"] - df["low"]).replace(0, np.nan) * 100
            df["upper_shadow_ratio"] = upper_shadow / (df["high"] - df["low"]).replace(0, np.nan) * 100
        else:
            df["lower_shadow_ratio"] = 0.0
            df["upper_shadow_ratio"] = 0.0

        # 成交额近似（close * volume），便于板块内比较
        if "volume" in df.columns:
            df["turnover"] = df["close"] * df["volume"]
        else:
            df["turnover"] = 0.0

        return df[[
            "code", "open", "high", "low", "close", "volume", "turnover",
            "return_pct", "amplitude", "body_pct",
            "lower_shadow_ratio", "upper_shadow_ratio"
        ]].dropna(subset=["code", "close"])

    # ========================== 板块指标 ==========================

    def _compute_sector_stats(
        self,
        daily_df: pd.DataFrame,
        sector_map: Dict[str, List[str]],
    ) -> Dict[str, Dict[str, Any]]:
        """
        计算每个板块当日的相对强弱指标。

        返回:
            Dict[str, Dict]: {
                sector_name: {
                    "stocks_count", "up_count", "down_count",
                    "avg_return", "max_return", "min_return",
                    "return_std", "avg_amplitude", "avg_volume",
                    "top_return", "up_ratio", ...
                }
            }
        """
        stats: Dict[str, Dict[str, Any]] = {}

        for sector, codes in sector_map.items():
            sector_df = daily_df[daily_df["code"].isin(codes)]
            if sector_df.empty or len(sector_df) < self.MIN_STOCKS_PER_SECTOR:
                continue

            returns = sector_df["return_pct"].dropna()
            amplitudes = sector_df["amplitude"].dropna()
            volumes = sector_df["volume"].dropna().replace(0, np.nan)
            turnovers = sector_df["turnover"].dropna().replace(0, np.nan)

            up_count = int((returns > 0).sum())
            down_count = int((returns < 0).sum())
            flat_count = len(returns) - up_count - down_count

            stats[sector] = {
                "stocks_count": len(sector_df),
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
                "up_ratio": up_count / len(returns) if len(returns) else 0.0,
                "avg_return": float(returns.mean()),
                "median_return": float(returns.median()),
                "max_return": float(returns.max()),
                "min_return": float(returns.min()),
                "return_std": float(returns.std()) if len(returns) > 1 else 0.0,
                "avg_amplitude": float(amplitudes.mean()),
                "max_amplitude": float(amplitudes.max()),
                "avg_volume": float(volumes.mean()) if not volumes.empty else 0.0,
                "total_turnover": float(turnovers.sum()) if not turnovers.empty else 0.0,
                "top_return": float(returns.quantile(0.9)),
                "bottom_return": float(returns.quantile(0.1)),
            }

        return stats

    # ========================== 状态机 ==========================

    def _classify_sector_states(
        self,
        sector_stats: Dict[str, Dict[str, Any]],
        current_date: str,
    ) -> Dict[str, Dict[str, Any]]:
        """
        基于当日板块统计和近期历史，判断每个板块处于加速/一致/分歧/冷漠之一。

        判断原则（相对强弱，非绝对阈值）：
            · 加速：短期领涨 + 内部极度一致 + 量价齐升
            · 一致：整体向上 + 内部结构良性 + 无极端爆量
            · 分歧：上涨后爆量震荡 + 内部涨跌分化严重
            · 冷漠：无人问津 + 缩量 + 涨跌均衡/阴跌
        """
        states: Dict[str, Dict[str, Any]] = {}

        if not sector_stats:
            return states

        # 为了相对判断，计算全市场板块的分位数
        all_up_ratios = np.array([s["up_ratio"] for s in sector_stats.values()])
        all_avg_returns = np.array([s["avg_return"] for s in sector_stats.values()])
        all_amplitudes = np.array([s["avg_amplitude"] for s in sector_stats.values()])
        all_stds = np.array([s["return_std"] for s in sector_stats.values()])
        all_turnovers = np.array([s["total_turnover"] for s in sector_stats.values()])

        def pct(value, arr):
            if len(arr) == 0 or np.all(np.isnan(arr)):
                return 0.5
            return float(np.mean(arr <= value))

        for sector, stat in sector_stats.items():
            up_ratio_pct = pct(stat["up_ratio"], all_up_ratios)
            avg_return_pct = pct(stat["avg_return"], all_avg_returns)
            amplitude_pct = pct(stat["avg_amplitude"], all_amplitudes)
            std_pct = pct(stat["return_std"], all_stds)
            turnover_pct = pct(stat["total_turnover"], all_turnovers)

            # 获取近期历史
            history = self._sector_history.get(sector, [])
            recent = history[-5:] if len(history) >= 5 else history

            # 辅助：是否前期上涨过
            had_recent_rally = any(h.get("avg_return", 0) > 1.5 for h in recent)

            # 辅助：近期成交量分位
            recent_volumes = [h.get("total_turnover", 0) for h in history[-10:]]
            volume_spike = False
            if len(recent_volumes) >= 3 and stat["total_turnover"] > 0:
                baseline = np.median(recent_volumes[:-1]) if len(recent_volumes) > 1 else stat["total_turnover"]
                if baseline > 0 and stat["total_turnover"] / baseline > 1.5:
                    volume_spike = True

            # 辅助：近期平均涨幅
            recent_avg_return = np.mean([h.get("avg_return", 0) for h in recent]) if recent else 0.0

            # ---------------- 状态判断 ----------------
            # 默认状态：根据当日方向给一个中性归类
            state = "整理"
            confidence = 0.5

            # 加速：板块集体冲锋，内部极度一致，有领涨大旗
            if (
                up_ratio_pct > 0.85
                and avg_return_pct > 0.75
                and stat["max_return"] > 4.0
                and std_pct < 0.80
            ):
                state = "加速"
                confidence = 0.5 + 0.25 * up_ratio_pct + 0.25 * avg_return_pct

            # 一致：整体向上，结构良性（允许放量，放量一致是健康的）
            # 一致不苛求内部标准差最小，只要绝大多数个股同向即可
            elif (
                stat["avg_return"] > 0.5
                and up_ratio_pct > 0.60
                and stat["up_ratio"] > 0.55
            ):
                state = "一致"
                confidence = 0.5 + 0.3 * up_ratio_pct + 0.2 * avg_return_pct

            # 分歧：上涨后的爆量震荡 + 内部分化（必须有前期上涨）
            elif (
                had_recent_rally
                and volume_spike
                and std_pct > 0.70
                and amplitude_pct > 0.65
            ):
                state = "分歧"
                confidence = 0.5 + 0.25 * std_pct + 0.25 * amplitude_pct

            # 冷漠：成交萎缩、涨跌均衡、缺乏方向
            elif (
                turnover_pct < 0.35
                and up_ratio_pct < 0.60
                and abs(stat["avg_return"]) < 1.0
            ):
                state = "冷漠"
                confidence = 0.5 + 0.25 * (1 - turnover_pct) + 0.25 * (1 - abs(stat["avg_return"]))

            states[sector] = {
                "date": current_date,
                "state": state,
                "confidence": min(confidence, 0.95),
                "metrics": stat,
                "recent_avg_return": float(recent_avg_return),
                "volume_spike": volume_spike,
                "had_recent_rally": had_recent_rally,
            }

        return states

    # ========================== 核心品种识别 ==========================

    def _identify_core_stocks(
        self,
        daily_df: pd.DataFrame,
        sector_map: Dict[str, List[str]],
        sector_states: Dict[str, Dict[str, Any]],
        current_date: str,
    ) -> Dict[str, Dict[str, str]]:
        """
        识别三类核心品种：领涨、抗跌、扛旗。

        返回:
            {stock_code: {"role": str, "sector": str, "score": float}}
        """
        core: Dict[str, Dict[str, str]] = {}

        for sector, state_info in sector_states.items():
            sector_codes = sector_map.get(sector, [])
            sector_df = daily_df[daily_df["code"].isin(sector_codes)].copy()
            if sector_df.empty:
                continue

            state = state_info["state"]

            # 1. 领涨品种：板块向上攻击时涨幅前列且带量
            if state in ("一致", "加速"):
                leaders = self._find_leaders(sector_df)
                for code, score in leaders:
                    core[code] = {
                        "role": "leader",
                        "sector": sector,
                        "score": f"{score:.3f}",
                    }

            # 2. 扛旗品种：分歧日承受抛压后仍强承接
            if state == "分歧":
                flag_bearers = self._find_flag_bearers(sector_df)
                for code, score in flag_bearers:
                    core[code] = {
                        "role": "flag_bearer",
                        "sector": sector,
                        "score": f"{score:.3f}",
                    }

            # 3. 抗跌品种：冷漠/整理/分歧期跌得少、甚至逆市
            if state in ("冷漠", "整理", "分歧"):
                resilient = self._find_resilient_stocks(sector, sector_df)
                for code, score in resilient:
                    if code not in core:
                        core[code] = {
                            "role": "resilient",
                            "sector": sector,
                            "score": f"{score:.3f}",
                        }

        return core

    def _find_leaders(self, sector_df: pd.DataFrame, top_n: int = 3) -> List[Tuple[str, float]]:
        """领涨品种：涨幅排名前列，且成交额不低于行业中位数（排除无量一字）。"""
        if sector_df.empty or len(sector_df) < 3:
            return []

        median_turnover = sector_df["turnover"].median()
        df = sector_df[sector_df["turnover"] >= median_turnover * 0.3].copy()
        if df.empty:
            df = sector_df.copy()

        df["leader_score"] = df["return_pct"] * 0.6 + df["body_pct"] * 0.3 + df["volume"] / df["volume"].max() * 5 * 0.1
        df = df.sort_values("leader_score", ascending=False)

        return [(row["code"], float(row["leader_score"])) for _, row in df.head(top_n).iterrows()]

    def _find_flag_bearers(self, sector_df: pd.DataFrame, top_n: int = 3) -> List[Tuple[str, float]]:
        """扛旗品种：分歧日长下影、相对抗跌、收盘收复失地。"""
        if sector_df.empty or len(sector_df) < 3:
            return []

        df = sector_df.copy()
        # 下影线 + 收盘相对高点回落不大 + 绝对跌幅不深
        df["flag_score"] = (
            df["lower_shadow_ratio"].fillna(0) * 0.4
            + (100 - df["upper_shadow_ratio"].fillna(0)) * 0.2
            - df["return_pct"].abs() * 0.3
            + df["volume"] / df["volume"].max() * 10 * 0.1
        )
        df = df.sort_values("flag_score", ascending=False)

        return [(row["code"], float(row["flag_score"])) for _, row in df.head(top_n).iterrows()]

    def _find_resilient_stocks(
        self,
        sector: str,
        sector_df: pd.DataFrame,
        top_n: int = 3,
    ) -> List[Tuple[str, float]]:
        """抗跌品种：当日跌幅小或逆市，且历史缓存中相对板块跌得少。"""
        if sector_df.empty or len(sector_df) < 3:
            return []

        df = sector_df.copy()
        df["today_score"] = -df["return_pct"]  # 跌得越少越好

        # 加入历史相对强弱
        history_scores = {}
        for code in df["code"]:
            hist = self._stock_history.get(code, [])
            if len(hist) >= 3:
                # 统计该股票近几日相对其板块的平均超额收益
                excess_returns = [
                    h["return_pct"] - h.get("sector_avg_return", 0)
                    for h in hist[-10:]
                    if h.get("sector") == sector
                ]
                history_scores[code] = np.mean(excess_returns) if excess_returns else 0.0
            else:
                history_scores[code] = 0.0

        df["hist_score"] = df["code"].map(history_scores)
        df["resilient_score"] = df["today_score"] * 0.6 + df["hist_score"] * 0.4
        df = df.sort_values("resilient_score", ascending=False)

        return [(row["code"], float(row["resilient_score"])) for _, row in df.head(top_n).iterrows()]

    # ========================== 交易信号 ==========================

    def _generate_trade_signals(
        self,
        current_date: str,
        daily_df: pd.DataFrame,
        sector_states: Dict[str, Dict[str, Any]],
        core_stocks: Dict[str, Dict[str, str]],
    ) -> Dict[str, str]:
        """
        根据板块状态转换和核心品种生成 buy/sell/hold 信号。

        规则：
            · 冷漠转一致：买入抗跌核心（潜伏）
            · 一致延续/加速：持有领涨核心；空仓时轻仓追领涨
            · 分歧：未出现扛旗品种则卖出；出现扛旗则继续持有
            · 持仓票所在板块进入冷漠：卖出
        """
        signals: Dict[str, str] = {}
        all_codes = set(daily_df["code"])

        # 初始化所有股票为 hold
        for code in all_codes:
            signals[code] = "hold"

        # 当前持仓
        holdings = set(self.current_portfolio.keys())

        # 板块状态历史，用于检测"转换"
        transitions = self._detect_transitions(sector_states)

        # ---------------- 买入逻辑 ----------------
        buy_candidates: List[Tuple[str, float, str]] = []

        # 1. 冷漠/整理转一致：买入抗跌核心
        for sector, transition in transitions.items():
            if (
                transition.get("from_state") in ("冷漠", "整理")
                and transition.get("to_state") == "一致"
            ):
                candidates = [
                    (code, info) for code, info in core_stocks.items()
                    if info["sector"] == sector and info["role"] == "resilient"
                ]
                for code, info in candidates:
                    score = float(info.get("score", 0))
                    buy_candidates.append((code, score, "cold_to_consensus"))

        # 2. 一致延续/加速：若空仓可轻仓追领涨
        for sector, state_info in sector_states.items():
            if state_info["state"] in ("一致", "加速"):
                candidates = [
                    (code, info) for code, info in core_stocks.items()
                    if info["sector"] == sector and info["role"] == "leader"
                ]
                for code, info in candidates:
                    score = float(info.get("score", 0))
                    buy_candidates.append((code, score, "leader_follow"))

        # 3. 分歧转一致（次日反包）：买入扛旗品种
        for sector, transition in transitions.items():
            if transition.get("from_state") == "分歧" and transition.get("to_state") == "一致":
                candidates = [
                    (code, info) for code, info in core_stocks.items()
                    if info["sector"] == sector and info["role"] == "flag_bearer"
                ]
                for code, info in candidates:
                    score = float(info.get("score", 0))
                    buy_candidates.append((code, score, "flag_bounce"))

        # 按分数排序，控制仓位数量
        buy_candidates.sort(key=lambda x: x[1], reverse=True)
        slots_left = max(0, self.MAX_HOLDINGS - len(holdings))
        selected_buys = [c[0] for c in buy_candidates[:slots_left]]

        for code in selected_buys:
            signals[code] = "buy"

        # ---------------- 卖出逻辑 ----------------
        for code in holdings:
            sector = self._get_stock_sector(code, sector_states)
            if not sector:
                continue

            state_info = sector_states.get(sector, {})
            state = state_info.get("state", "冷漠")
            role = core_stocks.get(code, {}).get("role", "")

            should_sell = False

            # 1. 板块进入冷漠：资金撤离
            if state == "冷漠":
                should_sell = True

            # 2. 分歧日没有扛旗：行情可能结束
            if state == "分歧" and role != "flag_bearer":
                should_sell = True

            # 3. 分歧日虽有扛旗但本票不是核心：减仓
            if state == "分歧" and code not in core_stocks:
                should_sell = True

            if should_sell:
                signals[code] = "sell"

        return signals

    def _detect_transitions(
        self,
        sector_states: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """
        检测板块状态转换。

        返回:
            {sector: {"from_state": str, "to_state": str, "days_in_prev": int}}
        """
        transitions: Dict[str, Dict[str, Any]] = {}

        for sector, state_info in sector_states.items():
            history = self._sector_history.get(sector, [])
            if not history:
                continue

            prev = history[-1]
            from_state = prev.get("state")
            to_state = state_info["state"]

            if from_state and from_state != to_state:
                transitions[sector] = {
                    "from_state": from_state,
                    "to_state": to_state,
                    "days_in_prev": 1,
                }

        return transitions

    # ========================== 历史缓存 ==========================

    def _update_histories(
        self,
        sector_states: Dict[str, Dict[str, Any]],
        daily_df: pd.DataFrame,
        sector_map: Dict[str, List[str]],
        current_date: str,
    ) -> None:
        """更新板块和个股历史缓存，用于后续判断状态延续和转换。"""
        # 板块历史
        for sector, state_info in sector_states.items():
            self._sector_history.setdefault(sector, []).append({
                "date": current_date,
                "state": state_info["state"],
                "avg_return": state_info["metrics"]["avg_return"],
                "up_ratio": state_info["metrics"]["up_ratio"],
                "total_turnover": state_info["metrics"]["total_turnover"],
                "return_std": state_info["metrics"]["return_std"],
                "confidence": state_info["confidence"],
            })
            # 限制长度
            if len(self._sector_history[sector]) > self.SECTOR_HISTORY_DAYS:
                self._sector_history[sector] = self._sector_history[sector][-self.SECTOR_HISTORY_DAYS:]

        # 个股历史
        for sector, codes in sector_map.items():
            sector_avg = sector_states.get(sector, {}).get("metrics", {}).get("avg_return", 0.0)
            sector_df = daily_df[daily_df["code"].isin(codes)]
            for _, row in sector_df.iterrows():
                code = row["code"]
                self._stock_history.setdefault(code, []).append({
                    "date": current_date,
                    "sector": sector,
                    "return_pct": float(row["return_pct"]),
                    "sector_avg_return": float(sector_avg),
                    "volume": float(row["volume"]) if pd.notna(row["volume"]) else 0.0,
                })
                if len(self._stock_history[code]) > self.LOOKBACK_DAYS:
                    self._stock_history[code] = self._stock_history[code][-self.LOOKBACK_DAYS:]

    def _get_stock_sector(
        self,
        code: str,
        sector_states: Dict[str, Dict[str, Any]],
    ) -> Optional[str]:
        """根据当前板块状态和缓存推断股票所属板块。"""
        # 先看缓存
        hist = self._stock_history.get(code, [])
        if hist:
            return hist[-1].get("sector")

        # 再看当日 core_stocks
        for c, info in self._core_stocks.items():
            if c == code:
                return info.get("sector")

        return None

    # ========================== 工具方法 ==========================

    @staticmethod
    def _find_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
        """在 DataFrame 中查找候选列名，返回第一个存在的列。"""
        cols = set(df.columns)
        for c in candidates:
            if c in cols:
                return c
        return None
