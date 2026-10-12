"""板块轮动引擎（已降级为行情特征计算器）。

本模块已降级为行情特征计算器，只输出 phase/广度/成交等特征。
主题身份和成分由 core/quant/theme/service.py 的 DynamicThemeEngine 决定。

历史背景：
- 从现有 sector_wind_vane_strategy.py 抽取计算能力（_prepare_daily_frame、
  _compute_sector_stats），重建为七阶段状态机：
  ICE → RECOVERY → STARTUP → ACCELERATION → CLIMAX → DIVERGENCE → RETREAT

设计原则：
- 旧策略不动，本引擎是独立 QUANT 控制面组件
- 不产生 buy/sell，仅输出 SectorAssessment
- 缺数据安全降级
- 支持历史回放（phase_history）
- get_sector_for_symbol_with_fallback() 已降级，仅供向后兼容；
  新代码应使用 ThemeSourceRouter + DynamicThemeEngine 获取主题身份和成分
"""
from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.quant.contracts import SectorAssessment, SectorPhase
from core.quant.sector.sector_data import (
    SECTOR_TYPE_CONCEPT,
    load_sector_map,
    load_sw_sector_map,
)
from core.quant.sector.evaluation.contracts import (
    LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
    LEGACY_SECTOR_ENGINE_POLICY_VERSION,
)

logger = logging.getLogger(__name__)

# 历史窗口配置
DEFAULT_HISTORY_DAYS = 20
DEFAULT_RALLY_THRESHOLD = 1.5       # 近期涨幅阈值
DEFAULT_VOLUME_SPIKE_RATIO = 1.5    # 放量倍数
DEFAULT_MIN_STOCKS_PER_SECTOR = 5

# 本地预设板块映射文件路径（实验功能）
_CUSTOM_SECTOR_MAPPING_PATH = Path(__file__).resolve().parents[3] / "config" / "custom_sector_mapping.yaml"

# 预设映射缓存（进程内）
_custom_sector_mapping_cache: dict[str, list[str]] | None = None


def _normalize_code(symbol: str) -> str:
    """规范化股票代码：去后缀、补 6 位。

    Args:
        symbol: 原始代码（如 "300308.SZ" 或 "300308"）。

    Returns:
        规范化后的 6 位数字代码（如 "300308"）。
    """
    s = str(symbol).strip()
    if "." in s:
        s = s.split(".")[0]
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def _load_custom_sector_mapping() -> dict[str, list[str]]:
    """加载本地预设板块映射（实验功能）。

    用户可在 config/custom_sector_mapping.yaml 中手动新增尚未在行情数据源
    建好板块的新概念（如 CPO、铜缆高速连接），格式：

        CPO:
          - "300308"
          - "002281"
        铜缆高速连接:
          - "002873"
          - "300682"

    用于实验性趋同验证：如果预设的股票集合和引擎识别的板块走势趋同，
    说明预设合理；否则需要调整。

    Returns:
        {板块名: [股票代码列表]} 映射，文件不存在或解析失败返回空 dict。
    """
    global _custom_sector_mapping_cache
    if _custom_sector_mapping_cache is not None:
        return _custom_sector_mapping_cache

    if not _CUSTOM_SECTOR_MAPPING_PATH.exists():
        _custom_sector_mapping_cache = {}
        return _custom_sector_mapping_cache

    try:
        import yaml
        with open(_CUSTOM_SECTOR_MAPPING_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            result: dict[str, list[str]] = {}
            for sector, codes in data.items():
                if not isinstance(codes, list):
                    continue
                normalized = [_normalize_code(str(c)) for c in codes if c]
                normalized = [c for c in normalized if c]
                if normalized:
                    result[str(sector)] = normalized
            _custom_sector_mapping_cache = result
            logger.info("加载本地预设板块映射 %d 个板块", len(result))
            return result
    except Exception as exc:
        logger.warning("加载本地预设板块映射失败: %s", exc)

    _custom_sector_mapping_cache = {}
    return _custom_sector_mapping_cache


class SectorRotationEngine:
    """七阶段板块轮动引擎。

    维护每个板块的历史统计，判断当前所处阶段，输出 SectorAssessment。
    不产生交易信号，仅提供板块上下文供日线筛选引擎使用。
    """

    def __init__(
        self,
        data_query: Any | None = None,
        *,
        sector_map: dict[str, list[str]] | None = None,
        history_days: int = DEFAULT_HISTORY_DAYS,
        min_stocks_per_sector: int = DEFAULT_MIN_STOCKS_PER_SECTOR,
        sector_type: str = SECTOR_TYPE_CONCEPT,
        keyword_filter: list[str] | None = None,
        max_boards: int = 200,
    ) -> None:
        """初始化板块轮动引擎。

        Args:
            data_query: 数据查询对象，用于加载板块映射。
            sector_map: 预加载的板块映射；非 None 时优先使用。
            history_days: 历史窗口天数。
            min_stocks_per_sector: 板块最少成分股数。
            sector_type: 板块类型："concept"(东财概念，默认) / "sw"(申万) /
                "basic"(stock_basic) / "auto"(自动回退)。
            keyword_filter: 概念板块名称关键词过滤（仅 concept 模式生效）。
            max_boards: 概念板块最大加载数（仅 concept 模式生效）。
        """
        self._data_query = data_query
        self._sector_map: dict[str, list[str]] | None = sector_map
        self._history_days = max(history_days, 5)
        self._min_stocks = min_stocks_per_sector
        self._sector_type = sector_type
        self._keyword_filter = keyword_filter
        self._max_boards = max_boards
        # 板块历史统计：{sector_name: deque([stat_dict, ...])}
        self._sector_history: dict[str, deque[dict[str, Any]]] = {}

    def _ensure_sector_map(self) -> dict[str, list[str]]:
        """懒加载板块映射。"""
        if self._sector_map is not None:
            return self._sector_map
        # 按板块类型加载
        self._sector_map = load_sector_map(
            self._data_query,
            sector_type=self._sector_type,
            min_stocks=self._min_stocks,
            max_boards=self._max_boards,
            keyword_filter=self._keyword_filter,
        )
        return self._sector_map

    def assess(
        self,
        stock_pool_today: pd.DataFrame,
        current_date: str,
    ) -> dict[str, SectorAssessment]:
        """评估所有板块的当前阶段。

        Args:
            stock_pool_today: 当日全市场个股 OHLCV 数据。
            current_date: 当前日期（ISO 格式）。

        Returns:
            {sector_name: SectorAssessment} 映射。
        """
        sector_map = self._ensure_sector_map()
        if not sector_map:
            logger.warning("板块映射为空，无法评估")
            return {}

        daily_df = self._prepare_daily_frame(stock_pool_today)
        if daily_df.empty:
            logger.warning("日线数据为空，无法评估板块")
            return {}

        sector_stats = self._compute_sector_stats(daily_df, sector_map)
        if not sector_stats:
            return {}

        # 更新历史
        self._update_histories(sector_stats, current_date)

        # 阶段判断
        assessments: dict[str, SectorAssessment] = {}
        for sector_name, stat in sector_stats.items():
            assessment = self._classify_phase(sector_name, stat, current_date)
            assessments[sector_name] = assessment

        return assessments

    def assess_single(
        self,
        sector_name: str,
        stock_pool_today: pd.DataFrame,
        current_date: str,
    ) -> SectorAssessment | None:
        """评估单个板块的阶段。

        Args:
            sector_name: 板块名称。
            stock_pool_today: 当日全市场个股数据。
            current_date: 当前日期。

        Returns:
            SectorAssessment 或 None（板块不存在时）。
        """
        sector_map = self._ensure_sector_map()
        if sector_name not in sector_map:
            return None
        results = self.assess(stock_pool_today, current_date)
        return results.get(sector_name)

    # ==================== 计算能力（从现有策略抽取） ====================

    def _prepare_daily_frame(self, stock_pool_today: pd.DataFrame) -> pd.DataFrame:
        """标准化 OHLCV 列名并计算日内指标。

        从 sector_wind_vane_strategy._prepare_daily_frame 抽取，
        不依赖策略实例。

        Args:
            stock_pool_today: 原始行情 DataFrame。

        Returns:
            标准化后的 DataFrame，包含 code/open/high/low/close/volume/
            return_pct/amplitude/body_pct/turnover 等列。
        """
        if stock_pool_today is None or stock_pool_today.empty:
            return pd.DataFrame()

        df = stock_pool_today.copy()

        # 兼容 Polars
        if hasattr(df, "to_pandas"):
            df = df.to_pandas()

        code_col = self._find_column(df, ["stock_code", "symbol_code", "symbol", "ts_code", "code"])
        open_col = self._find_column(df, ["open", "open_price"])
        high_col = self._find_column(df, ["high", "high_price"])
        low_col = self._find_column(df, ["low", "low_price"])
        close_col = self._find_column(df, ["close", "close_price"])
        pre_close_col = self._find_column(df, ["pre_close", "prev_close", "pre_close_price"])
        volume_col = self._find_column(df, ["volume", "vol", "trade_volume"])

        if not code_col or not close_col:
            return pd.DataFrame()

        rename_map = {code_col: "code", close_col: "close"}
        if open_col:
            rename_map[open_col] = "open"
        if high_col:
            rename_map[high_col] = "high"
        if low_col:
            rename_map[low_col] = "low"
        if volume_col:
            rename_map[volume_col] = "volume"
        if pre_close_col:
            rename_map[pre_close_col] = "pre_close"
        df = df.rename(columns=rename_map)

        # 计算涨跌幅
        if "pre_close" in df.columns and df["pre_close"].notna().any():
            df["return_pct"] = (df["close"] / df["pre_close"] - 1.0) * 100.0
        elif "open" in df.columns:
            df["return_pct"] = (df["close"] / df["open"] - 1.0) * 100.0
        else:
            df["return_pct"] = 0.0

        # 振幅
        if "high" in df.columns and "low" in df.columns and "pre_close" in df.columns:
            df["amplitude"] = (
                (df["high"] - df["low"]) / df["pre_close"].replace(0, np.nan) * 100.0
            )
        else:
            df["amplitude"] = 0.0

        # 实体占比
        if "open" in df.columns:
            body = (df["close"] - df["open"]).abs()
            range_val = (df.get("high", df["close"]) - df.get("low", df["close"])).replace(0, np.nan)
            df["body_pct"] = (body / range_val * 100.0).fillna(0.0)
        else:
            df["body_pct"] = 0.0

        # 换手（使用 volume 代理）
        if "volume" in df.columns:
            df["turnover"] = df["volume"].astype(float)
        else:
            df["turnover"] = 0.0

        return df

    def _compute_sector_stats(
        self,
        daily_df: pd.DataFrame,
        sector_map: dict[str, list[str]],
    ) -> dict[str, dict[str, Any]]:
        """计算每个板块当日的相对强弱指标。

        从 sector_wind_vane_strategy._compute_sector_stats 抽取。

        Args:
            daily_df: 标准化后的日线数据。
            sector_map: 板块映射。

        Returns:
            {sector_name: {stats_dict}} 映射。
        """
        stats: dict[str, dict[str, Any]] = {}

        for sector, codes in sector_map.items():
            sector_df = daily_df[daily_df["code"].isin(codes)]
            if sector_df.empty or len(sector_df) < self._min_stocks:
                continue

            returns = sector_df["return_pct"].dropna()
            amplitudes = sector_df["amplitude"].dropna()
            volumes = sector_df["volume"].dropna().replace(0, np.nan) if "volume" in sector_df else pd.Series(dtype=float)
            turnovers = sector_df["turnover"].dropna().replace(0, np.nan) if "turnover" in sector_df else pd.Series(dtype=float)

            up_count = int((returns > 0).sum())
            down_count = int((returns < 0).sum())
            flat_count = len(returns) - up_count - down_count

            stats[sector] = {
                "stocks_count": len(sector_df),
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
                "up_ratio": up_count / len(returns) if len(returns) else 0.0,
                "avg_return": float(returns.mean()) if len(returns) else 0.0,
                "median_return": float(returns.median()) if len(returns) else 0.0,
                "max_return": float(returns.max()) if len(returns) else 0.0,
                "min_return": float(returns.min()) if len(returns) else 0.0,
                "return_std": float(returns.std()) if len(returns) > 1 else 0.0,
                "avg_amplitude": float(amplitudes.mean()) if len(amplitudes) else 0.0,
                "max_amplitude": float(amplitudes.max()) if len(amplitudes) else 0.0,
                "avg_volume": float(volumes.mean()) if not volumes.empty else 0.0,
                "total_turnover": float(turnovers.sum()) if not turnovers.empty else 0.0,
                "top_return": float(returns.quantile(0.9)) if len(returns) else 0.0,
                "bottom_return": float(returns.quantile(0.1)) if len(returns) else 0.0,
            }

        return stats

    def _update_histories(
        self,
        sector_stats: dict[str, dict[str, Any]],
        current_date: str,
    ) -> None:
        """更新板块历史统计。

        Args:
            sector_stats: 当日板块统计。
            current_date: 当前日期。
        """
        for sector, stat in sector_stats.items():
            if sector not in self._sector_history:
                self._sector_history[sector] = deque(maxlen=self._history_days)
            self._sector_history[sector].append({
                "date": current_date,
                "avg_return": stat["avg_return"],
                "total_turnover": stat["total_turnover"],
                "up_ratio": stat["up_ratio"],
                "return_std": stat["return_std"],
            })

    # ==================== 七阶段状态机 ====================

    def _classify_phase(
        self,
        sector_name: str,
        stat: dict[str, Any],
        current_date: str,
    ) -> SectorAssessment:
        """判断单个板块的七阶段。

        七阶段定义：
        - ICE：冰点，板块持续低迷，成交萎缩，涨跌均衡或阴跌
        - RECOVERY：复苏，底部放量，领涨品种出现，avg_return 转正
        - STARTUP：启动，板块共振，多品种齐涨，up_ratio > 0.6
        - ACCELERATION：加速，涨幅扩大，成交激增，max_return > 4%
        - CLIMAX：高潮，顶部放量滞涨，分歧加大，std 升高
        - DIVERGENCE：分歧，品种分化，领涨滞涨，up_ratio 下降
        - RETREAT：撤退，板块回调，资金流出，avg_return < 0

        Args:
            sector_name: 板块名称。
            stat: 当日统计。
            current_date: 当前日期。

        Returns:
            SectorAssessment 实例。
        """
        degraded_fields: list[str] = []

        # 获取历史
        history = list(self._sector_history.get(sector_name, []))
        recent_5 = history[-5:] if len(history) >= 5 else history
        recent_10 = history[-10:] if len(history) >= 10 else history

        # 辅助指标
        had_recent_rally = any(h.get("avg_return", 0) > DEFAULT_RALLY_THRESHOLD for h in recent_5)
        recent_avg_return = float(np.mean([h.get("avg_return", 0) for h in recent_5])) if recent_5 else 0.0
        recent_avg_turnover = float(np.mean([h.get("total_turnover", 0) for h in recent_10])) if recent_10 else 0.0

        # 放量检测
        volume_spike = False
        if recent_avg_turnover > 0 and stat["total_turnover"] > 0:
            if stat["total_turnover"] / recent_avg_turnover > DEFAULT_VOLUME_SPIKE_RATIO:
                volume_spike = True

        # 缩量检测
        volume_shrink = False
        if recent_avg_turnover > 0 and stat["total_turnover"] > 0:
            if stat["total_turnover"] / recent_avg_turnover < 0.6:
                volume_shrink = True

        # 趋势方向
        is_uptrend = recent_avg_return > 0.3 and stat["avg_return"] > 0
        is_downtrend = recent_avg_return < -0.3 and stat["avg_return"] < 0

        # ---- 七阶段判断 ----
        phase = SectorPhase.ICE
        confidence = 0.5

        if stat["avg_return"] < -0.5 and (volume_shrink or is_downtrend):
            # RETREAT：撤退，板块回调
            phase = SectorPhase.RETREAT
            confidence = 0.5 + 0.3 * min(abs(stat["avg_return"]) / 3.0, 1.0)

        elif had_recent_rally and stat["return_std"] > 2.0 and stat["up_ratio"] < 0.55:
            # DIVERGENCE：分歧，前期上涨后品种分化
            phase = SectorPhase.DIVERGENCE
            confidence = 0.5 + 0.2 * min(stat["return_std"] / 4.0, 1.0)

        elif had_recent_rally and volume_spike and abs(stat["avg_return"]) < 0.8:
            # CLIMAX：高潮，放量但滞涨
            phase = SectorPhase.CLIMAX
            confidence = 0.5 + 0.2 * (1.0 - min(abs(stat["avg_return"]) / 2.0, 1.0))

        elif (
            stat["up_ratio"] > 0.75
            and stat["avg_return"] > 1.5
            and stat["max_return"] > 4.0
            and volume_spike
        ):
            # ACCELERATION：加速，齐涨 + 涨幅大 + 放量
            phase = SectorPhase.ACCELERATION
            confidence = 0.6 + 0.2 * min(stat["avg_return"] / 3.0, 1.0)

        elif stat["up_ratio"] > 0.6 and stat["avg_return"] > 0.5 and is_uptrend:
            # STARTUP：启动，板块共振齐涨
            phase = SectorPhase.STARTUP
            confidence = 0.55 + 0.2 * min(stat["up_ratio"], 1.0)

        elif stat["avg_return"] > 0.3 and volume_spike and not had_recent_rally:
            # RECOVERY：复苏，底部放量转正
            phase = SectorPhase.RECOVERY
            confidence = 0.5 + 0.2 * min(stat["avg_return"] / 2.0, 1.0)

        else:
            # ICE：冰点，默认状态
            phase = SectorPhase.ICE
            confidence = 0.5 + 0.1 * (1.0 - min(stat["up_ratio"], 1.0))

        confidence = min(confidence, 0.95)

        # 构建阶段历史
        phase_history: list[tuple[str, SectorPhase]] = []
        for h in recent_5:
            h_date = h.get("date", "")
            # 历史阶段简化标记（不重新计算，仅保留日期占位）
            phase_history.append((h_date, SectorPhase.ICE))

        # 分数计算
        momentum_score = min(max(stat["avg_return"] / 3.0, 0.0), 1.0)
        breadth_score = stat["up_ratio"]
        turnover_score = min(stat["total_turnover"] / 1e10, 1.0) if stat["total_turnover"] > 0 else 0.0
        dispersion_score = min(stat["return_std"] / 4.0, 1.0) if stat["return_std"] > 0 else 0.0

        if not history:
            degraded_fields.append("history")

        return SectorAssessment(
            sector_id=sector_name,
            sector_name=sector_name,
            phase=phase,
            phase_confidence=round(confidence, 4),
            momentum_score=round(momentum_score, 4),
            breadth_score=round(breadth_score, 4),
            turnover_score=round(turnover_score, 4),
            dispersion_score=round(dispersion_score, 4),
            phase_history=phase_history,
            assessed_at=current_date,
            degraded=bool(degraded_fields),
            degraded_fields=degraded_fields,
            # 旧规则降级为 feature/shadow：标记规则版本
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
            policy_version=LEGACY_SECTOR_ENGINE_POLICY_VERSION,
        )

    # ==================== 工具方法 ====================

    @staticmethod
    def _find_column(df: pd.DataFrame, candidates: list[str]) -> str:
        """在 DataFrame 中查找匹配的列名。"""
        columns = set(df.columns)
        for c in candidates:
            if c in columns:
                return c
        return ""

    def get_sector_for_symbol(self, symbol: str) -> str:
        """查询股票所属板块。

        Args:
            symbol: 股票代码。

        Returns:
            板块名称，未找到返回空串。
        """
        sector_map = self._ensure_sector_map()
        for sector, codes in sector_map.items():
            if symbol in codes:
                return sector
        return ""

    def get_sector_for_symbol_with_fallback(
        self,
        symbol: str,
        sector_id_hint: str = "",
    ) -> tuple[str, str]:
        """获取股票所属板块（已降级，仅供向后兼容）。

        ⚠️ 此方法已降级，主题身份和成分应由 DynamicThemeEngine 决定。
        新代码应使用 ThemeSourceRouter + DynamicThemeEngine。

        历史查找优先级（保留原有逻辑，不删除）：
        1. symbol 反查 sector_map（主路径，不依赖 sector_id 命名）
        2. 本地预设板块映射 config/custom_sector_mapping.yaml（实验功能）
           —— 用于新增尚未建好板块的股票（如 CPO 新概念）
        3. sector_id_hint 软提示（实验性趋同验证，不直接信任）

        Args:
            symbol: 股票代码（纯数字或带后缀）。
            sector_id_hint: Research 侧的 sector_id 提示（英文 ID 或中文名），
                仅用于实验性趋同验证，不作为主查找路径。

        Returns:
            (板块名称, 来源) 元组。来源取值：
            - "sector_map"：从板块引擎映射查到
            - "custom_preset"：从本地预设映射查到
            - "hint"：从 sector_id_hint 查到（实验性）
            - ""：未查到，板块名称为空串
        """
        # 降级标记：发出 DeprecationWarning，提示使用 DynamicThemeEngine
        import warnings
        warnings.warn(
            "sector_engine.get_sector_for_symbol_with_fallback() 已降级，"
            "请使用 DynamicThemeEngine 获取主题身份和成分。",
            DeprecationWarning,
            stacklevel=2,
        )

        # 规范化 symbol（去掉 .SH/.SZ/.BJ 后缀，补 6 位）
        normalized = _normalize_code(symbol)

        # 1. symbol 反查 sector_map（主路径）
        # sector_map 里的 code 格式可能是 "300308" 或 "300308.SZ"，两种都匹配
        #
        # 【备用 TODO】当前 sector_map 数据源受网络环境限制：
        #   - 东财概念：代理拦截，返回空
        #   - TDX 板块：服务器不提供下载，本机无客户端
        #   - 同花顺概念：akshare 无成分股接口
        #   - 申万一级：可用但分类粗（"通信"不区分光模块/IDC）
        #   - stock_basic：当前默认，全覆盖但粗（"通信设备"）
        # 待更细粒度数据源接入后（见 sector_data.py 头部注释），此处自动生效
        sector_map = self._ensure_sector_map()
        for sector, codes in sector_map.items():
            for code in codes:
                code_normalized = _normalize_code(code)
                if code == symbol or code == normalized or code_normalized == normalized:
                    return sector, "sector_map"

        # 2. 本地预设板块映射（实验功能）
        # 【备用 TODO】当更细粒度的板块数据源接入后，预设映射主要用于新概念补充
        custom_map = _load_custom_sector_mapping()
        if custom_map:
            for sector, codes in custom_map.items():
                for code in codes:
                    code_normalized = _normalize_code(code)
                    if code == symbol or code == normalized or code_normalized == normalized:
                        return sector, "custom_preset"

        # 3. sector_id_hint 软提示（实验性趋同验证）
        # 不直接信任 hint，但若 hint 恰好是 sector_map 里的板块名，则使用
        if sector_id_hint:
            hint_normalized = sector_id_hint.strip()
            if hint_normalized in sector_map:
                return hint_normalized, "hint"

        return "", ""
