"""日线筛选引擎（插件调度器）。

重构为插件式架构：
- DailyScreener 仅负责调度，不包含具体筛选逻辑
- 筛选逻辑由注册到 ScreenStrategyRegistry 的策略插件提供
- 默认注册六项策略（PIT/趋势/位置/加速赶顶/流动性/可交易性）
- 外部策略大师可注册自己的策略替代或补充

设计原则：
- 策略隔离：单个策略异常不阻塞其他策略
- PIT 安全：PIT 策略失败时短路，不执行后续策略
- 降级友好：策略异常时标记 MISSING_DATA 而非崩溃
- 向后兼容：保持原 screen() 方法签名
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import pandas as pd
from pandas import NaT

from core.quant.contracts import (
    QuantCandidate,
    RejectReason,
    ScreeningResult,
    SectorAssessment,
    SectorPhase,
)
from core.quant.screening.strategy_base import (
    ScreenStrategyBase,
    ScreenStrategyRegistry,
    StrategyContext,
    get_default_registry,
)

logger = logging.getLogger(__name__)

# 默认筛选参数（向后兼容旧 DailyScreener 构造函数）
DEFAULT_MA_SHORT = 5
DEFAULT_MA_MID = 10
DEFAULT_MA_LONG = 20
DEFAULT_MIN_TURNOVER = 5e7
DEFAULT_MIN_VOLUME = 1e5
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_ACCELERATION_THRESHOLD = 3.0
DEFAULT_HIGH_POSITION_RATIO = 0.85


def _normalize_symbol_variants(symbol: str) -> list[str]:
    """生成股票代码的多种表示形式，用于匹配不同数据源存储格式。

    A 股代码规则：
    - 6xxxxx → 上交所 .SH
    - 0xxxxx / 3xxxxx → 深交所 .SZ
    - 8xxxxx / 4xxxxx → 北交所 .BJ

    Args:
        symbol: 原始股票代码（纯数字或带后缀）。

    Returns:
        候选代码列表，按匹配优先级排序。例如 "600519" →
        ["600519.SH", "600519"]；"600519.SH" → ["600519.SH"]。
    """
    symbol = str(symbol).strip()
    if not symbol:
        return [symbol]

    # 已带后缀，直接返回
    if "." in symbol:
        return [symbol]

    # 纯 6 位数字：按规则补全后缀
    if symbol.isdigit() and len(symbol) == 6:
        first = symbol[0]
        if first == "6":
            return [f"{symbol}.SH", symbol]
        if first in ("0", "3"):
            return [f"{symbol}.SZ", symbol]
        if first in ("8", "4"):
            return [f"{symbol}.BJ", symbol]

    return [symbol]


def _ensure_default_strategies_registered() -> None:
    """确保默认六项策略已注册。

    多次调用安全（注册中心会去重）。
    """
    # 导入即触发 @register_screen_strategy 装饰器
    from core.quant.screening import default_strategies  # noqa: F401


class DailyScreener:
    """日线筛选引擎（插件调度器）。

    对单个 QuantCandidate 调用所有已注册策略，聚合结果为 ScreeningResult。
    不产生交易信号，仅标记通过/拒绝。

    向后兼容：保留原构造函数签名，旧代码无需修改。
    """

    def __init__(
        self,
        data_query: Any | None = None,
        *,
        ma_short: int = DEFAULT_MA_SHORT,
        ma_mid: int = DEFAULT_MA_MID,
        ma_long: int = DEFAULT_MA_LONG,
        min_turnover: float = DEFAULT_MIN_TURNOVER,
        min_volume: float = DEFAULT_MIN_VOLUME,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        acceleration_threshold: float = DEFAULT_ACCELERATION_THRESHOLD,
        high_position_ratio: float = DEFAULT_HIGH_POSITION_RATIO,
        registry: ScreenStrategyRegistry | None = None,
    ) -> None:
        """初始化日线筛选引擎。

        Args:
            data_query: 数据查询对象，用于获取个股历史行情。
            ma_short: 短期均线天数。
            ma_mid: 中期均线天数。
            ma_long: 长期均线天数。
            min_turnover: 最低日成交额。
            min_volume: 最低日成交量。
            lookback_days: 回看天数。
            acceleration_threshold: 加速赶顶涨幅阈值（%）。
            high_position_ratio: 高位比例阈值。
            registry: 自定义策略注册中心；None 时使用全局默认。
        """
        self._data_query = data_query
        # 注意：不能用 `registry or get_default_registry()`，
        # 因为 ScreenStrategyRegistry 实现了 __len__，空 registry 是 falsy。
        self._registry = registry if registry is not None else get_default_registry()
        # 确保默认策略已注册
        _ensure_default_strategies_registered()

        # 保存参数，构造策略上下文时传入
        self._params: dict[str, Any] = {
            "ma_short": ma_short,
            "ma_mid": ma_mid,
            "ma_long": ma_long,
            "min_turnover": min_turnover,
            "min_volume": min_volume,
            "lookback_days": lookback_days,
            "acceleration_threshold": acceleration_threshold,
            "high_position_ratio": high_position_ratio,
        }

    @property
    def registry(self) -> ScreenStrategyRegistry:
        """策略注册中心。"""
        return self._registry

    def screen(
        self,
        candidate: QuantCandidate,
        sector_assessment: SectorAssessment | None,
        current_date: str,
        *,
        daily_data: pd.DataFrame | None = None,
    ) -> ScreeningResult:
        """对单个候选执行完整日线筛选。

        P0-2 修复：日线数据按 current_date 截断，未来行情不得参与计算。
        P0-4 修复：设置 screening_id 和 candidate_id，避免同证券多候选覆盖。

        Args:
            candidate: QUANT 候选。
            sector_assessment: 板块评估结果（可 None，安全降级为 UNKNOWN）。
            current_date: 当前日期（ISO 格式）。
            daily_data: 预加载的日线数据；None 时通过 data_query 获取。

        Returns:
            ScreeningResult 完整筛选结果。
        """
        symbol = candidate.symbol
        # P0-3：板块评估缺失时用 UNKNOWN 而非 ICE
        sector_phase = (
            sector_assessment.phase
            if sector_assessment is not None
            else SectorPhase.UNKNOWN
        )

        result = ScreeningResult(
            screening_id=f"scr-{uuid.uuid4().hex[:12]}",
            candidate_id=candidate.candidate_id,
            symbol=symbol,
            as_of=current_date,
            sector_phase=sector_phase,
        )

        # 加载日线数据
        if daily_data is None:
            daily_data = self._load_daily_data(symbol, current_date)

        if daily_data is not None and not daily_data.empty:
            daily_data = self._standardize(daily_data)
            # P0-2：按 current_date 截断日线数据，防止未来行情参与计算
            # fail-closed：截断失败时标记 degraded
            daily_data, pit_degraded = self._truncate_by_date(daily_data, current_date)
            if pit_degraded:
                result.degraded = True
                result.reject_reasons.append(RejectReason.PIT_VIOLATION)

        # 构造上下文
        ctx = StrategyContext(
            current_date=current_date,
            candidate=candidate,
            sector_assessment=sector_assessment,
            daily_data=daily_data,
            data_query=self._data_query,
            params=dict(self._params),
        )

        # 按优先级执行所有策略
        strategies = self._registry.list_strategies(sorted_by_priority=True)
        pit_passed = False

        for strategy in strategies:
            passed, score, details, reject_reasons = strategy.safe_check(ctx)

            # 分发结果到 ScreeningResult 对应字段
            self._apply_strategy_result(strategy.strategy_id, passed, score, details,
                                         reject_reasons, result)

            # PIT 策略失败时短路
            if strategy.strategy_id == "pit_check":
                if not passed:
                    return result
                pit_passed = True

            # 数据缺失时标记降级
            if RejectReason.MISSING_DATA in reject_reasons:
                result.degraded = True

        result.pit_pass = pit_passed
        return result

    def _apply_strategy_result(
        self,
        strategy_id: str,
        passed: bool,
        score: float,
        details: dict[str, Any],
        reject_reasons: list[RejectReason],
        result: ScreeningResult,
    ) -> None:
        """把策略结果分发到 ScreeningResult 对应字段。

        Args:
            strategy_id: 策略 ID。
            passed: 是否通过。
            score: 分数。
            details: 详情。
            reject_reasons: 拒绝原因。
            result: 累积的筛选结果。
        """
        # 合并 details
        result.details.update(details)

        # 合并拒绝原因
        result.reject_reasons.extend(reject_reasons)

        # 分发到对应字段
        if strategy_id == "pit_check":
            result.pit_pass = passed
        elif strategy_id == "trend_check":
            result.trend_pass = passed
            result.trend_score = score
        elif strategy_id == "position_check":
            result.position_pass = passed
            result.position_score = score
        elif strategy_id == "acceleration_check":
            result.acceleration_pass = passed
        elif strategy_id == "liquidity_check":
            result.liquidity_pass = passed
            result.liquidity_score = score
        elif strategy_id == "tradability_check":
            result.tradability_pass = passed
        else:
            # 外部策略：无对应字段，仅通过 details 和 reject_reasons 记录
            result.details[f"{strategy_id}_passed"] = passed
            result.details[f"{strategy_id}_score"] = round(float(score), 4)

    # ==================== 数据加载与标准化（向后兼容） ====================

    def _load_daily_data(self, symbol: str, current_date: str) -> pd.DataFrame | None:
        """通过 data_query 加载个股历史日线数据。

        适配多种数据查询接口（UnifiedDataQuery / 旧版 data_query）：
        - get_stock_history(symbol, start, end): LanceDB UnifiedDataQuery
        - get_daily(symbol, start, end): LanceDBDataReader 兼容接口
        - get_daily_data(symbol, end_date, count): 旧版接口
        - get_stock_data(symbol, end_date, count): 旧版接口

        符号规范化：纯 6 位数字代码自动补全交易所后缀（.SH/.SZ/.BJ），
        匹配 LanceDB 中 stock_code 的存储格式（如 600519.SH）。

        Args:
            symbol: 股票代码（纯数字或带后缀）。
            current_date: 当前日期（ISO 格式，作为 end 日期）。

        Returns:
            日线 DataFrame 或 None。
        """
        if self._data_query is None:
            return None

        # 符号规范化：纯 6 位数字 → 带交易所后缀
        candidates = _normalize_symbol_variants(symbol)

        lookback = int(self._params["lookback_days"]) + 10
        # 反推 start 日期：lookback 天 + 缓冲，简单用 2*lookback 日历日
        try:
            end_dt = pd.to_datetime(current_date)
            start_dt = end_dt - pd.Timedelta(days=lookback * 2)
            start_str = start_dt.strftime("%Y-%m-%d")
        except Exception:
            start_str = ""

        try:
            # 优先：LanceDB UnifiedDataQuery（get_stock_history）
            if hasattr(self._data_query, "get_stock_history"):
                for sym in candidates:
                    df = self._data_query.get_stock_history(sym, start_str, current_date)
                    if df is not None and not df.empty:
                        return df
                return None
            # 次选：LanceDBDataReader 兼容接口（get_daily）
            if hasattr(self._data_query, "get_daily"):
                for sym in candidates:
                    df = self._data_query.get_daily(sym, start_str, current_date)
                    if df is not None and not df.empty:
                        return df
                return None
            # 旧版接口
            if hasattr(self._data_query, "get_daily_data"):
                for sym in candidates:
                    df = self._data_query.get_daily_data(
                        sym, end_date=current_date, count=lookback
                    )
                    if df is not None and not df.empty:
                        return df
                return None
            if hasattr(self._data_query, "get_stock_data"):
                for sym in candidates:
                    df = self._data_query.get_stock_data(
                        sym, end_date=current_date, count=lookback
                    )
                    if df is not None and not df.empty:
                        return df
                return None
        except Exception as exc:
            logger.warning("加载日线数据失败 %s: %s", symbol, exc)
        return None

    def _standardize(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化日线数据列名。

        Args:
            df: 原始 DataFrame。

        Returns:
            标准化后的 DataFrame，至少包含 close/volume 列。
        """
        if df is None or df.empty:
            return pd.DataFrame()

        result_df = df.copy()
        if hasattr(result_df, "to_pandas"):
            result_df = result_df.to_pandas()

        rename_map = {}
        for candidate_col, target in [
            (["stock_code", "symbol", "ts_code", "code"], "code"),
            (["open", "open_price"], "open"),
            (["high", "high_price"], "high"),
            (["low", "low_price"], "low"),
            (["close", "close_price"], "close"),
            (["volume", "vol", "trade_volume"], "volume"),
            (["amount", "turnover_value", "money"], "amount"),
        ]:
            for c in candidate_col:
                if c in result_df.columns:
                    rename_map[c] = target
                    break

        if rename_map:
            result_df = result_df.rename(columns=rename_map)

        if "close" not in result_df.columns:
            return pd.DataFrame()

        return result_df

    def _truncate_by_date(self, df: pd.DataFrame, current_date: str) -> tuple[pd.DataFrame, bool]:
        """按 current_date 截断日线数据，防止未来行情参与计算。

        P0-2 修复：确保均线和趋势计算只用 current_date 及之前的数据。
        fail-closed：无日期列或截断异常时返回空 DataFrame 并标记 degraded。

        Args:
            df: 标准化后的日线数据。
            current_date: 当前日期（ISO 格式）。

        Returns:
            (截断后的 DataFrame, degraded_flag)。
            degraded_flag=True 表示 PIT 截断失败，调用方应标记降级。
        """
        if df is None or df.empty or not current_date:
            return df, False

        # 尝试查找日期列
        date_col = None
        for candidate_col in ["date", "trade_date", "datetime", "time"]:
            if candidate_col in df.columns:
                date_col = candidate_col
                break

        if date_col is None:
            # fail-closed：无日期列无法保证 PIT 安全，返回空 DataFrame
            logger.warning("日线数据无日期列，PIT 截断失败，返回空: cols=%s", list(df.columns))
            return pd.DataFrame(), True

        try:
            # 用 pd.to_datetime 解析日期，支持多种格式（YYYYMMDD/ISO/带时区等）
            parsed_dates = pd.to_datetime(df[date_col], errors="coerce", utc=False)
            cutoff = pd.to_datetime(current_date, errors="coerce", utc=False)

            if cutoff is NaT:
                logger.warning("current_date 解析失败: %s", current_date)
                return pd.DataFrame(), True

            # NaT 的行（解析失败的日期）不参与比较，视为无效
            valid_mask = parsed_dates.notna()
            if not valid_mask.any():
                logger.warning("日线数据日期列全部解析失败")
                return pd.DataFrame(), True

            # 截断：保留 <= cutoff 的行
            mask = parsed_dates <= cutoff
            truncated = df[mask & valid_mask].copy()
            if truncated.empty:
                logger.warning(
                    "PIT 截断后数据为空: current_date=%s, date_range=[%s, %s]",
                    current_date,
                    parsed_dates.min(),
                    parsed_dates.max(),
                )
                # 截断后为空也标记 degraded（可能全部是未来数据）
                return truncated, True
            return truncated, False
        except Exception as exc:
            logger.warning("PIT 截断失败: %s", exc)
            return pd.DataFrame(), True
