"""P2.9 预测记分卡引擎。

按 P2 计划 §301-323 Task P2.9：
- 收入 / 利润 / EPS 预测误差
- 预测区间覆盖率
- 共识 vs 独立预测相对误差
- 5 类错误归因（model / mapping / data / timing / assumption）
- 行业与市场环境稳定性
- 3 条强制规则

设计要点：
- evaluate(): 主入口，校验 PIT、计算误差、区间覆盖、稳定性
- 冻结输入防篡改校验
- actual.reported_at > forecast.as_of（无未来泄漏）
- 不以股价涨跌单独定义正确性（仅评估基本面指标）
"""
from __future__ import annotations

from math import isfinite
from typing import Any

from .contracts import (
    ACTUAL_OUTCOME_STATUSES,
    ERROR_ATTRIBUTIONS,
    FORECAST_SOURCES,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    MARKET_REGIMES,
    METRICS,
    ActualOutcome,
    ErrorAttribution,
    ForecastScorecard,
    FrozenForecastInput,
    IndustryStabilityScore,
    utc_now,
)


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------

ERR_MISSING_FORECAST: str = "missing_forecast"
ERR_MISSING_ACTUAL: str = "missing_actual"
ERR_NOT_REPORTED: str = "actual_not_reported"
ERR_INVALID_METRIC: str = "invalid_metric"
ERR_INVALID_SOURCE: str = "invalid_forecast_source"
ERR_INVALID_REGIME: str = "invalid_market_regime"
ERR_INVALID_ATTRIBUTION: str = "invalid_attribution_key"
ERR_INVALID_INTERVAL: str = "invalid_interval"
ERR_ATTRIBUTION_TOTAL_NOT_100: str = "attribution_total_not_100"
ERR_ACTUAL_BEFORE_FORECAST: str = "actual_before_forecast"


# ---------------------------------------------------------------------------
# 预测记分卡引擎
# ---------------------------------------------------------------------------


class ForecastScorecardEngine:
    """预测记分卡引擎。

    评估某次预测的误差、区间覆盖、归因和稳定性。
    """

    def evaluate(
        self,
        *,
        forecast: FrozenForecastInput,
        actual: ActualOutcome,
        attribution: ErrorAttribution,
        industry: str = "",
        market_regime: str = "range",
        consensus_forecast_value: float | None = None,
        peer_scorecards: list[ForecastScorecard] | None = None,
        evaluation_as_of: str = "",
        evaluator: str = "",
    ) -> ForecastScorecard:
        """评估预测误差并生成记分卡。

        Args:
            forecast: 冻结的预测输入
            actual: 实际结果
            attribution: 错误归因（5 类占比总和必须为 100）
            industry: 行业
            market_regime: 市场环境
            consensus_forecast_value: 同期共识预测值（None 表示无共识对比）
            peer_scorecards: 同行业 / 同 regime 的历史记分卡（用于稳定性计算）
            evaluation_as_of: 评估时点（默认 utc_now）
            evaluator: 评估人

        Returns:
            ForecastScorecard

        Raises:
            ValueError: 任一校验失败
        """
        # 1. 校验 forecast
        if not isinstance(forecast, FrozenForecastInput):
            raise TypeError("forecast must be a FrozenForecastInput")
        if not isinstance(actual, ActualOutcome):
            raise TypeError("actual must be an ActualOutcome")
        if not isinstance(attribution, ErrorAttribution):
            raise TypeError("attribution must be an ErrorAttribution")
        if not forecast.forecast_id or not forecast.company_symbol or not forecast.as_of:
            raise ValueError("forecast 缺少必填字段（forecast_id/company_symbol/as_of）")
        if forecast.metric not in METRICS:
            raise ValueError(
                f"非法 metric：{forecast.metric}（合法：{', '.join(METRICS)})"
            )
        if forecast.forecast_source not in FORECAST_SOURCES:
            raise ValueError(
                f"非法 forecast_source：{forecast.forecast_source}"
                f"（合法：{', '.join(FORECAST_SOURCES)}）"
            )
        # 冻结输入完整性
        if not forecast.is_intact():
            raise ValueError(
                "冻结输入已被篡改（input_hash 不匹配）"
            )
        # 区间合法性
        if (
            forecast.interval_lower is not None
            and forecast.interval_upper is not None
            and forecast.interval_lower > forecast.interval_upper
        ):
            raise ValueError(
                f"非法区间：lower({forecast.interval_lower}) > upper({forecast.interval_upper})"
            )

        # 2. 校验 actual
        if not actual.company_symbol or not actual.period:
            raise ValueError("actual 缺少必填字段（company_symbol/period）")
        if not actual.is_reported():
            raise ValueError(
                f"actual 未公布（status={actual.status}, reported_at={actual.reported_at}）"
            )
        if actual.metric != forecast.metric:
            raise ValueError(
                f"metric 不匹配：forecast={forecast.metric}, actual={actual.metric}"
            )
        # PIT：actual.reported_at > forecast.as_of
        if actual.reported_at <= forecast.as_of:
            raise ValueError(
                f"未来泄漏：actual.reported_at({actual.reported_at}) "
                f"<= forecast.as_of({forecast.as_of})"
            )

        # 3. 校验 attribution
        if not attribution.is_total_100():
            raise ValueError(
                f"错误归因占比总和必须为 100，实际 {attribution.total():.2f}"
            )
        if attribution.contains_trade_action():
            raise ValueError(
                f"归因备注含交易动作关键词（{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）"
            )

        # 4. 校验 market_regime
        if market_regime not in MARKET_REGIMES:
            raise ValueError(
                f"非法 market_regime：{market_regime}（合法：{', '.join(MARKET_REGIMES)}）"
            )

        # 5. 计算误差
        signed_error = self._compute_signed_error_pct(
            forecast.forecast_value, actual.actual_value
        )
        abs_error = abs(signed_error)

        # 6. 区间覆盖
        interval_covered = self._compute_interval_coverage(
            forecast.interval_lower,
            forecast.interval_upper,
            actual.actual_value,
        )

        # 7. 共识对比
        consensus_error_pct: float | None = None
        if consensus_forecast_value is not None:
            consensus_error_pct = self._compute_signed_error_pct(
                consensus_forecast_value, actual.actual_value
            )

        # 8. 稳定性分数
        peers = [item for item in (peer_scorecards or []) if isinstance(item, ForecastScorecard)]
        stability = self._compute_stability_score(peers)

        # 9. 评估时点
        if not evaluation_as_of:
            evaluation_as_of = utc_now()

        return ForecastScorecard(
            forecast=forecast,
            actual=actual,
            signed_error_pct=signed_error,
            abs_error_pct=abs_error,
            interval_covered=interval_covered,
            attribution=attribution,
            industry=industry,
            market_regime=market_regime,
            stability_score=stability,
            consensus_error_pct=consensus_error_pct,
            evaluation_as_of=evaluation_as_of,
            evaluator=evaluator,
        )

    # ------------------------------------------------------------------
    # 误差计算
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_signed_error_pct(
        forecast_value: float, actual_value: float
    ) -> float:
        """计算有符号误差百分比。

        error = (actual - forecast) / |forecast| × 100

        - 正值：实际高于预测（低估）
        - 负值：实际低于预测（高估）
        - forecast 为 0 时返回 0（防御性）

        Args:
            forecast_value: 预测值
            actual_value: 实际值

        Returns:
            有符号误差百分比
        """
        try:
            forecast_value = float(forecast_value)
            actual_value = float(actual_value)
        except (TypeError, ValueError):
            return 0.0
        if (
            not isfinite(forecast_value)
            or not isfinite(actual_value)
            or abs(forecast_value) < 1e-9
        ):
            return 0.0
        return (actual_value - forecast_value) / abs(forecast_value) * 100.0

    @staticmethod
    def _compute_interval_coverage(
        lower: float | None,
        upper: float | None,
        actual: float,
    ) -> bool:
        """计算区间覆盖。

        Args:
            lower: 区间下限（None 表示无区间 → False）
            upper: 区间上限
            actual: 实际值

        Returns:
            是否覆盖
        """
        if lower is None or upper is None:
            return False
        try:
            lower_value, upper_value, actual_value = (
                float(lower),
                float(upper),
                float(actual),
            )
        except (TypeError, ValueError):
            return False
        return (
            isfinite(lower_value)
            and isfinite(upper_value)
            and isfinite(actual_value)
            and lower_value <= actual_value <= upper_value
        )

    @staticmethod
    def _compute_stability_score(
        peer_scorecards: list[ForecastScorecard],
    ) -> float:
        """计算稳定性分数。

        基于同行业 / 同 regime 的历史记分卡的 abs_error_pct 方差。
        方差越小 → 越稳定 → score 越高。

        Args:
            peer_scorecards: 同类历史记分卡

        Returns:
            稳定性分数（0-1，越高越稳定）
        """
        if not peer_scorecards:
            return 0.5  # 默认中等

        errors = []
        for scorecard in peer_scorecards:
            try:
                value = float(scorecard.abs_error_pct)
            except (TypeError, ValueError):
                continue
            if isfinite(value):
                errors.append(value)
        if not errors:
            return 0.5

        mean = sum(errors) / len(errors)
        variance = sum((e - mean) ** 2 for e in errors) / len(errors)

        # 归一化：variance 单位是 (%²)，常见范围 [0, 10000]
        # 0 → score 1.0；1000 → score 0.0
        normalized = min(1.0, variance / 1000.0)
        return max(0.0, 1.0 - normalized)

    # ------------------------------------------------------------------
    # 行业稳定性聚合
    # ------------------------------------------------------------------

    def aggregate_industry_stability(
        self,
        scorecards: list[ForecastScorecard],
    ) -> list[IndustryStabilityScore]:
        """按 (industry, market_regime) 聚合稳定性分数。

        Args:
            scorecards: 全部记分卡

        Returns:
            IndustryStabilityScore 列表
        """
        groups: dict[tuple[str, str], list[ForecastScorecard]] = {}
        for sc in scorecards:
            if not isinstance(sc, ForecastScorecard):
                continue
            key = (sc.industry, sc.market_regime)
            groups.setdefault(key, []).append(sc)

        result: list[IndustryStabilityScore] = []
        for (industry, regime), cards in groups.items():
            if not cards:
                continue
            errors = [c.abs_error_pct for c in cards]
            n = len(errors)
            mean_err = sum(errors) / n
            variance = sum((e - mean_err) ** 2 for e in errors) / n if n > 0 else 0.0
            normalized = min(1.0, variance / 1000.0)
            score = max(0.0, 1.0 - normalized)
            result.append(
                IndustryStabilityScore(
                    industry=industry,
                    market_regime=regime,
                    score=score,
                    sample_count=n,
                    variance=variance,
                    mean_abs_error_pct=mean_err,
                )
            )
        return result


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_forecast_scorecard_engine() -> ForecastScorecardEngine:
    """构造默认 ForecastScorecardEngine。"""
    return ForecastScorecardEngine()


__all__ = [
    # 错误码
    "ERR_MISSING_FORECAST",
    "ERR_MISSING_ACTUAL",
    "ERR_NOT_REPORTED",
    "ERR_INVALID_METRIC",
    "ERR_INVALID_SOURCE",
    "ERR_INVALID_REGIME",
    "ERR_INVALID_ATTRIBUTION",
    "ERR_INVALID_INTERVAL",
    "ERR_ATTRIBUTION_TOTAL_NOT_100",
    "ERR_ACTUAL_BEFORE_FORECAST",
    # 引擎
    "ForecastScorecardEngine",
    "build_forecast_scorecard_engine",
]
