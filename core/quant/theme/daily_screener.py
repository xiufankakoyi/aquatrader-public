"""主题日线筛选器。

基于 ThemePacket 与候选列表执行日线 Top10 筛选，输出 READY/WATCH/
REJECTED/EXPIRED/UNKNOWN 五态结果。daily_data 缺失时降级为 UNKNOWN，
不抛异常；阻断性负面信息强制 REJECTED，不得进入 READY。

设计原则：
- KISS：分数计算直接基于 ThemePacket 已有字段与 daily_data 标量指标
- DRY：权重表、角色映射、technical_state 判定集中收口
- 防御性：daily_data 缺失/字段缺失/类型异常一律降级 UNKNOWN
- 不输出投资建议
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.quant.contracts import QuantCandidate
from core.quant.theme.contracts import ThemePacket
from core.quant.sector.evaluation.contracts import (
    LEGACY_DAILY_SCREENER_FEATURE_VERSION,
    LEGACY_DAILY_SCREENER_POLICY_VERSION,
)

logger = logging.getLogger(__name__)


# ======================================================================
# 契约
# ======================================================================

@dataclass
class ThemeScreeningResult:
    """主题日线筛选结果。

    旧规则降级为 feature/shadow：feature_version 和 policy_version 标记
    规则版本，未通过 walk-forward 样本外验证前不得进入生产评分。

    Attributes:
        candidate_id: 候选 ID（与 QuantCandidate.candidate_id 一致）。
        symbol: 证券代码。
        theme_id: 关联主题 ID。
        theme_run_id: 关联主题运行 ID。
        status: 状态枚举值字符串（READY/WATCH/REJECTED/EXPIRED/UNKNOWN）。
        final_score: 综合分数 [0, 1]。
        score_breakdown: 各分项分数映射。
        technical_state: 技术状态标签（MA10_RETEST / MA20_RETEST /
            TREND_PULLBACK / REACCELERATION / TREND_BROKEN /
            ACCELERATION_RISK / 空字符串）。
        degraded: 是否降级（UNKNOWN/数据缺失）。
        degradation_reasons: 降级原因列表。
        feature_version: 特征版本（如 daily_screener_v1_legacy）。
        policy_version: 策略版本（如 policy_screener_legacy_shadow_v1）。
    """

    candidate_id: str = ""
    symbol: str = ""
    theme_id: str = ""
    theme_run_id: str = ""
    status: str = ""  # READY / WATCH / REJECTED / EXPIRED / UNKNOWN
    final_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    technical_state: str = ""
    degraded: bool = False
    degradation_reasons: list[str] = field(default_factory=list)
    # 旧规则降级为 feature/shadow：标记规则版本
    feature_version: str = ""
    policy_version: str = ""


# ======================================================================
# 状态常量
# ======================================================================

STATUS_READY: str = "READY"
STATUS_WATCH: str = "WATCH"
STATUS_REJECTED: str = "REJECTED"
STATUS_EXPIRED: str = "EXPIRED"
STATUS_UNKNOWN: str = "UNKNOWN"

# 阻断性负面风险阈值（≥ 该值时强制 REJECTED）
BLOCKING_NEGATIVE_THRESHOLD: float = 0.7
# 警告级负面阈值（≥ 该值时阻止 READY）
WARNING_NEGATIVE_THRESHOLD: float = 0.4
# 拥挤度阈值（≥ 该值时阻止 READY）
CROWDING_BLOCK_THRESHOLD: float = 0.7
# 趋势质量阈值（< 该值时阻止 READY）
TREND_QUALITY_THRESHOLD: float = 0.7
# READY/WATCH 分数阈值
READY_SCORE_THRESHOLD: float = 0.6
WATCH_SCORE_THRESHOLD: float = 0.4

# 技术状态标签
TECH_TREND_BROKEN: str = "TREND_BROKEN"
TECH_ACCELERATION_RISK: str = "ACCELERATION_RISK"
TECH_MA5_HOLD: str = "MA5_HOLD"
TECH_MA10_RETEST: str = "MA10_RETEST"
TECH_MA20_RETEST: str = "MA20_RETEST"
TECH_MA30_RETEST: str = "MA30_RETEST"
TECH_TREND_PULLBACK: str = "TREND_PULLBACK"
TECH_REACCELERATION: str = "REACCELERATION"

# 加速赶顶阈值（近 5 日涨幅超过 15%）
ACCELERATION_RETURN_THRESHOLD: float = 0.15
# 回调阈值（近 5 日回调 < 5% 视为健康回调）
PULLBACK_HEALTHY_THRESHOLD: float = 0.05
# 流动性达标阈值（5 日均成交额 ≥ 5000 万）
LIQUIDITY_TURNOVER_THRESHOLD: float = 5e7


class ThemeDailyScreener:
    """基于 ThemePacket 的日线筛选器，输出最多 10 个状态结果。"""

    MAX_OUTPUT: int = 10

    FINAL_SCORE_WEIGHTS: dict[str, float] = {
        "research": 0.30,
        "persistence": 0.20,
        "role": 0.15,
        "trend": 0.15,
        "pullback": 0.10,
        "liquidity": 0.10,
    }

    # market_role → company_role_score 映射
    MARKET_ROLE_SCORES: dict[str, float] = {
        "core": 1.0,
        "leader": 0.8,
        "secondary": 0.5,
        "diffusion": 0.3,
    }

    # theme_type → 持续性分数字段映射
    PERSISTENCE_FIELD: dict[str, str] = {
        "news_persistence": "news_score",
        "ice_reversal": "persistence_score",
        "research_discovery": "persistence_score",
        "legacy_reentry": "persistence_score",
    }

    # 惩罚项系数
    CROWDING_PENALTY_WEIGHT: float = 0.3
    NEGATIVE_PENALTY_WEIGHT: float = 0.5

    def screen(
        self,
        candidates: list[QuantCandidate],
        theme_packets: list[ThemePacket],
        *,
        daily_data_map: dict[str, pd.DataFrame] | None = None,
        as_of: str = "",
    ) -> list[ThemeScreeningResult]:
        """对候选执行日线筛选，返回最多 MAX_OUTPUT 个结果。

        Args:
            candidates: QuantCandidate 列表（通常由 CandidateQuotaGenerator 产出）。
            theme_packets: 对应的 ThemePacket 列表（按 theme_id 索引）。
            daily_data_map: {symbol: DataFrame} 个股日线数据，可为 None。
            as_of: 当前日期（ISO 格式），用于过期判定。

        Returns:
            最多 10 个 ThemeScreeningResult，按 final_score 降序。
        """
        if not candidates:
            return []

        # 构建 theme_id → packet 索引（同 theme_id 取首个，配额生成器已确保唯一性）
        packet_index: dict[str, ThemePacket] = {}
        for packet in theme_packets:
            if packet.theme_id and packet.theme_id not in packet_index:
                packet_index[packet.theme_id] = packet

        results: list[ThemeScreeningResult] = []
        for candidate in candidates:
            packet = packet_index.get(candidate.theme_id)
            result = self._screen_one(
                candidate, packet,
                daily_data_map=daily_data_map,
                as_of=as_of,
            )
            results.append(result)

        # Top10：按 final_score 降序，分数相同按 candidate_id 升序保持稳定
        results.sort(key=lambda r: (-r.final_score, r.candidate_id))
        return results[: self.MAX_OUTPUT]

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    def _screen_one(
        self,
        candidate: QuantCandidate,
        packet: ThemePacket | None,
        *,
        daily_data_map: dict[str, pd.DataFrame] | None,
        as_of: str,
    ) -> ThemeScreeningResult:
        """对单个候选执行筛选。

        Args:
            candidate: 单个候选。
            packet: 对应的 ThemePacket（可为 None，降级为 UNKNOWN）。
            daily_data_map: 个股日线数据映射。
            as_of: 当前日期。

        Returns:
            ThemeScreeningResult 实例。
        """
        result = ThemeScreeningResult(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            theme_id=candidate.theme_id,
            theme_run_id=candidate.theme_run_id,
            # 旧规则降级为 feature/shadow：标记规则版本
            feature_version=LEGACY_DAILY_SCREENER_FEATURE_VERSION,
            policy_version=LEGACY_DAILY_SCREENER_POLICY_VERSION,
        )

        # 1. EXPIRED 判定（优先级最高，packet 缺失也按 expires_at 走 candidate）
        if self._is_expired(candidate, packet, as_of):
            result.status = STATUS_EXPIRED
            result.degraded = True
            result.degradation_reasons.append("expired")
            return result

        # 2. packet 缺失 → UNKNOWN
        if packet is None:
            result.status = STATUS_UNKNOWN
            result.degraded = True
            result.degradation_reasons.append("theme_packet_missing")
            return result

        # 3. 阻断性负面信息（优先级高于日线计算，避免无谓计算）
        if packet.negative_risk_score >= BLOCKING_NEGATIVE_THRESHOLD:
            result.status = STATUS_REJECTED
            result.final_score = 0.0
            result.score_breakdown = {"negative_risk_score": packet.negative_risk_score}
            result.technical_state = ""
            result.degraded = True
            result.degradation_reasons.append("blocking_negative_risk")
            return result

        # 4. 加载日线数据
        daily_data = None
        if daily_data_map is not None:
            daily_data = daily_data_map.get(candidate.symbol)
        daily_data = self._standardize(daily_data)

        # 5. daily_data 缺失 → UNKNOWN（不抛异常）
        if daily_data is None or daily_data.empty:
            result.status = STATUS_UNKNOWN
            result.degraded = True
            result.degradation_reasons.append("daily_data_missing")
            return result

        # 6. 计算各项分数 + technical_state
        breakdown, technical_state = self._compute_breakdown(
            candidate, packet, daily_data
        )
        result.score_breakdown = breakdown
        result.technical_state = technical_state

        # 7. final_score 加权求和 - 惩罚项
        final_score = self._compute_final_score(breakdown, packet)
        result.final_score = round(final_score, 4)

        # 8. 状态判定
        result.status = self._decide_status(
            final_score=final_score,
            technical_state=technical_state,
            breakdown=breakdown,
            packet=packet,
        )

        # 9. 降级标记：technical_state 为 TREND_BROKEN 或 ACCELERATION_RISK 时
        #    标记 degraded（不阻塞 REJECTED 判定，仅作为审计信息）
        if technical_state in (TECH_TREND_BROKEN, TECH_ACCELERATION_RISK):
            result.degraded = True
            result.degradation_reasons.append(technical_state.lower())

        return result

    def _is_expired(
        self,
        candidate: QuantCandidate,
        packet: ThemePacket | None,
        as_of: str,
    ) -> bool:
        """检查候选是否已过期。

        优先使用 packet.expires_at，回退到 candidate.expires_at。
        缺失 as_of 时不过期判定（防御性，返回 False）。

        Args:
            candidate: 候选实例。
            packet: 主题包（可 None）。
            as_of: 当前日期。

        Returns:
            True 表示已过期。
        """
        if not as_of:
            return False
        expires_at = ""
        if packet is not None and packet.expires_at:
            expires_at = packet.expires_at
        elif candidate.expires_at:
            expires_at = candidate.expires_at
        if not expires_at:
            return False
        # 统一取前 10 字符（YYYY-MM-DD）比较
        return as_of[:10] > expires_at[:10]

    def _standardize(self, df: pd.DataFrame | None) -> pd.DataFrame:
        """标准化日线数据列名。

        Args:
            df: 原始 DataFrame。

        Returns:
            标准化后的 DataFrame（含 close/amount 列），或空 DataFrame。
        """
        if df is None:
            return pd.DataFrame()
        if df.empty:
            return df
        result_df = df.copy()
        if hasattr(result_df, "to_pandas"):
            result_df = result_df.to_pandas()

        rename_map: dict[str, str] = {}
        for candidates, target in [
            (["close", "close_price"], "close"),
            (["amount", "turnover_value", "money"], "amount"),
            (["volume", "vol", "trade_volume"], "volume"),
            (["date", "trade_date", "datetime", "time"], "date"),
        ]:
            for c in candidates:
                if c in result_df.columns:
                    rename_map[c] = target
                    break
        if rename_map:
            result_df = result_df.rename(columns=rename_map)
        if "close" not in result_df.columns:
            return pd.DataFrame()
        return result_df

    def _compute_breakdown(
        self,
        candidate: QuantCandidate,
        packet: ThemePacket,
        daily_data: pd.DataFrame,
    ) -> tuple[dict[str, float], str]:
        """计算各分项分数与技术状态。

        Args:
            candidate: 候选实例。
            packet: 主题包。
            daily_data: 标准化后的日线数据。

        Returns:
            (score_breakdown, technical_state) 元组。
        """
        # research_score：直接取 candidate.research_score（来自 membership_confidence）
        research_score = float(candidate.research_score or 0.0)

        # theme_persistence：按 theme_type 分发到对应字段
        persistence_field = self.PERSISTENCE_FIELD.get(
            packet.theme_type, "persistence_score"
        )
        theme_persistence = float(getattr(packet, persistence_field, 0.0) or 0.0)

        # company_role_score：通过 packet.core_members 反查 market_role
        company_role_score = self._lookup_role_score(candidate.symbol, packet)

        # 技术指标
        trend_quality, technical_state, pullback_quality, liquidity = (
            self._compute_technical_scores(daily_data)
        )

        breakdown: dict[str, float] = {
            "research": round(research_score, 4),
            "persistence": round(theme_persistence, 4),
            "role": round(company_role_score, 4),
            "trend": round(trend_quality, 4),
            "pullback": round(pullback_quality, 4),
            "liquidity": round(liquidity, 4),
            "crowding_score": round(float(packet.crowding_score or 0.0), 4),
            "negative_risk_score": round(float(packet.negative_risk_score or 0.0), 4),
        }
        return breakdown, technical_state

    def _lookup_role_score(
        self, symbol: str, packet: ThemePacket
    ) -> float:
        """从 packet.core_members 查找 symbol 的 market_role 并映射为分数。

        找不到时默认 0.5（secondary，中性）。

        Args:
            symbol: 证券代码。
            packet: 主题包。

        Returns:
            role_score [0, 1]。
        """
        for member in packet.core_members:
            if member.symbol == symbol:
                role = member.market_role or "secondary"
                return self.MARKET_ROLE_SCORES.get(role, 0.5)
        # 回退到 members 列表（防御性：core_members 可能未填充）
        for member in packet.members:
            if member.symbol == symbol:
                role = member.market_role or "secondary"
                return self.MARKET_ROLE_SCORES.get(role, 0.5)
        return 0.5

    def _compute_technical_scores(
        self, daily_data: pd.DataFrame
    ) -> tuple[float, str, float, float]:
        """计算技术面分数：trend_quality / technical_state / pullback_quality / liquidity。

        Args:
            daily_data: 标准化后的日线数据（含 close 列，amount 列可选）。

        Returns:
            (trend_quality, technical_state, pullback_quality, liquidity) 元组。
        """
        try:
            ordered = daily_data.copy()
            if "trade_date" in ordered.columns:
                ordered = ordered.sort_values("trade_date", kind="stable")
            close = pd.to_numeric(ordered["close"], errors="coerce").dropna()
            if close.empty:
                return 0.3, TECH_TREND_BROKEN, 0.3, 0.3
            # 与主题市场特征层共用 MA5/10/20/30 观察口径。
            ma5 = close.rolling(window=5, min_periods=1).mean().iloc[-1]
            ma10 = close.rolling(window=10, min_periods=1).mean().iloc[-1]
            ma20 = close.rolling(window=20, min_periods=1).mean().iloc[-1]
            ma30 = close.rolling(window=30, min_periods=1).mean().iloc[-1]
            last_close = float(close.iloc[-1])
            ma5_val = float(ma5) if ma5 == ma5 else last_close  # noqa: PLR0124
            ma10_val = float(ma10) if ma10 == ma10 else last_close  # noqa: PLR0124
            ma20_val = float(ma20) if ma20 == ma20 else last_close  # noqa: PLR0124
            ma30_val = float(ma30) if ma30 == ma30 else last_close  # noqa: PLR0124

            # trend_quality：MA10/MA20 上方 = 1.0，下方 = 0.3，中间 = 0.7
            above_ma10 = last_close >= ma10_val
            above_ma20 = last_close >= ma20_val
            if above_ma10 and above_ma20:
                trend_quality = 1.0
            elif above_ma10 or above_ma20:
                trend_quality = 0.7
            else:
                trend_quality = 0.3

            # 近 5 日涨幅（用于 ACCELERATION_RISK 判定）
            recent_window = close.tail(6) if len(close) >= 6 else close
            recent_return = 0.0
            if len(recent_window) >= 2 and float(recent_window.iloc[0]) > 0:
                recent_return = (
                    float(recent_window.iloc[-1]) / float(recent_window.iloc[0]) - 1.0
                )

            # 近 5 日回调幅度（用于 pullback_quality）
            recent_5 = close.tail(6) if len(close) >= 6 else close
            max_high = float(recent_5.max()) if not recent_5.empty else last_close
            pullback_ratio = 0.0
            if max_high > 0:
                pullback_ratio = (max_high - last_close) / max_high

            # pullback_quality：近 5 日回调 < 5% = 0.8，否则 0.3
            if pullback_ratio < PULLBACK_HEALTHY_THRESHOLD:
                pullback_quality = 0.8
            else:
                pullback_quality = 0.3

            # liquidity：5 日均成交额 ≥ 5000万 = 1.0，否则按比例
            liquidity = 0.3
            if "amount" in ordered.columns:
                amount_series = pd.to_numeric(
                    ordered["amount"], errors="coerce"
                ).dropna()
                if not amount_series.empty:
                    avg_turnover = float(amount_series.tail(5).mean())
                    if avg_turnover >= LIQUIDITY_TURNOVER_THRESHOLD:
                        liquidity = 1.0
                    elif avg_turnover > 0:
                        liquidity = min(
                            1.0, avg_turnover / LIQUIDITY_TURNOVER_THRESHOLD
                        )
                    liquidity = round(liquidity, 4)

            # technical_state 判定（优先级：TREND_BROKEN > ACCELERATION_RISK >
            # MA10_RETEST > MA20_RETEST > TREND_PULLBACK > REACCELERATION）
            technical_state = ""
            if last_close < ma20_val and last_close < ma30_val:
                technical_state = TECH_TREND_BROKEN
            elif recent_return >= ACCELERATION_RETURN_THRESHOLD:
                technical_state = TECH_ACCELERATION_RISK
            elif (
                ma5_val > 0
                and abs(last_close - ma5_val) / ma5_val < 0.02
                and ma5_val >= ma10_val >= ma20_val
            ):
                technical_state = TECH_MA5_HOLD
            elif ma10_val > 0 and abs(last_close - ma10_val) / ma10_val < 0.02:
                technical_state = TECH_MA10_RETEST
            elif ma20_val > 0 and abs(last_close - ma20_val) / ma20_val < 0.02:
                technical_state = TECH_MA20_RETEST
            elif ma30_val > 0 and abs(last_close - ma30_val) / ma30_val < 0.025:
                technical_state = TECH_MA30_RETEST
            elif 0 < pullback_ratio <= PULLBACK_HEALTHY_THRESHOLD:
                technical_state = TECH_TREND_PULLBACK
            elif recent_return > 0.05:
                technical_state = TECH_REACCELERATION

            return trend_quality, technical_state, pullback_quality, liquidity
        except Exception as exc:  # noqa: BLE001 — 防御性：技术面计算异常降级
            logger.warning("技术面计算失败: %s", exc)
            return 0.3, TECH_TREND_BROKEN, 0.3, 0.3

    def _compute_final_score(
        self, breakdown: dict[str, float], packet: ThemePacket
    ) -> float:
        """计算 final_score 加权和减惩罚项。

        final_score =
          0.30 × research
        + 0.20 × persistence
        + 0.15 × role
        + 0.15 × trend
        + 0.10 × pullback
        + 0.10 × liquidity
        - 0.30 × crowding_score
        - 0.50 × negative_risk_score

        Args:
            breakdown: 各分项分数。
            packet: 主题包（提供惩罚项来源）。

        Returns:
            final_score 浮点数（不截断，由调用方四舍五入）。
        """
        weights = self.FINAL_SCORE_WEIGHTS
        weighted_sum = (
            weights["research"] * breakdown.get("research", 0.0)
            + weights["persistence"] * breakdown.get("persistence", 0.0)
            + weights["role"] * breakdown.get("role", 0.0)
            + weights["trend"] * breakdown.get("trend", 0.0)
            + weights["pullback"] * breakdown.get("pullback", 0.0)
            + weights["liquidity"] * breakdown.get("liquidity", 0.0)
        )
        crowding_penalty = (
            self.CROWDING_PENALTY_WEIGHT * float(packet.crowding_score or 0.0)
        )
        negative_penalty = (
            self.NEGATIVE_PENALTY_WEIGHT * float(packet.negative_risk_score or 0.0)
        )
        return weighted_sum - crowding_penalty - negative_penalty

    def _decide_status(
        self,
        *,
        final_score: float,
        technical_state: str,
        breakdown: dict[str, float],
        packet: ThemePacket,
    ) -> str:
        """根据 final_score、technical_state 与负面信息判定最终状态。

        Args:
            final_score: 综合分数。
            technical_state: 技术状态标签。
            breakdown: 分项分数（用于 trend_quality 阈值判定）。
            packet: 主题包（用于 negative_risk_score / crowding_score 阈值判定）。

        Returns:
            状态字符串（READY/WATCH/REJECTED）。
        """
        trend_quality = breakdown.get("trend", 0.0)
        negative_risk = float(packet.negative_risk_score or 0.0)
        crowding = float(packet.crowding_score or 0.0)

        # REJECTED：趋势破坏 / 加速赶顶 / 分数过低
        if technical_state in (TECH_TREND_BROKEN, TECH_ACCELERATION_RISK):
            return STATUS_REJECTED
        if final_score < WATCH_SCORE_THRESHOLD:
            return STATUS_REJECTED

        # READY：final_score ≥ 0.6 AND trend ≥ 0.7 AND negative < 0.4 AND crowding < 0.7
        if (
            final_score >= READY_SCORE_THRESHOLD
            and trend_quality >= TREND_QUALITY_THRESHOLD
            and negative_risk < WARNING_NEGATIVE_THRESHOLD
            and crowding < CROWDING_BLOCK_THRESHOLD
        ):
            return STATUS_READY

        # WATCH：final_score ≥ 0.4 但不满足 READY
        return STATUS_WATCH
