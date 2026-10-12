"""Stage 7 - 盈利预测与产业链传播 PIT 重验框架。

冻结 as_of 时点：所有输入文档、假设都必须在 as_of 时点已公开可见，
未来财报、修订版本由 pit.filter_inputs_as_of 拦截。

设计原则：
- 预测准确性 vs Alpha（SAG 传播）分开评价：ForecastRevalidator 只算预测侧，
  SAGPropagationRevalidator 单独评价事实传播正确性 vs 股价反应相关性。
- 样本不足返回 insufficient_sample=True，所有指标归零，禁止用合成数据宣称业务通过。
- 报告附 disclaimer="模拟评价，不构成投资建议"（遵循 AGENTS.md 不输出投资建议）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .contracts import ImpactAssumption, Serializable, new_id, utc_now
from .pit import filter_inputs_as_of, parse_time


# 最小样本量：低于此值返回 insufficient_sample=True，所有指标归零
# 不允许用极小样本宣称业务通过
MIN_SAMPLE_FOR_ACCURACY = 5
MIN_SAMPLE_FOR_PROPAGATION = 5

# 免责声明：所有报告必须附带，不输出投资建议
DISCLAIMER = "模拟评价，不构成投资建议"


class PredictionType(str, Enum):
    """预测类型。"""

    EARNINGS_FORECAST = "earnings_forecast"  # 盈利预测
    SAG_PROPAGATION = "sag_propagation"  # SAG 产业链传播


class CaseCategory(str, Enum):
    """案例分类（仅描述预测方向，不构成投资建议）。

    - positive: 预测认为指标向上（如 EPS 同比正增长）。
    - negative: 预测认为指标向下。
    - no_significant: 预测认为变化在噪声区间内，无显著变化。
    """

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NO_SIGNIFICANT = "no_significant"


# ---------------------------------------------------------------------------
# 输入文档
# ---------------------------------------------------------------------------


@dataclass
class InputDocument(Serializable):
    """输入文档及可见时间戳，用于 PIT 过滤。

    文档通过 pit.filter_inputs_as_of 过滤：published_at / retrieved_at /
    available_at 任一晚于 as_of 即被拦截。这与盈利映射层的假设过滤逻辑
    一致，避免在两个模块中重复实现 PIT 规则（DRY）。

    Attributes:
        document_id: 文档唯一标识。
        title: 文档标题或简要描述。
        published_at: 文档发布时间（ISO），PIT 用。
        retrieved_at: 系统采集时间（ISO），PIT 用。
        available_at: 数据生效时间（ISO），可选。
        expires_at: 文档过期时间（ISO），可选。
        source_kind: 来源类型（announcement / report 等）。
    """

    document_id: str = field(default_factory=lambda: new_id("doc"))
    title: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    available_at: str = ""
    expires_at: str = ""
    source_kind: str = "unknown"


# ---------------------------------------------------------------------------
# 案例 & 结果
# ---------------------------------------------------------------------------


@dataclass
class RevalidationCase(Serializable):
    """重验案例：在 as_of 冻结时点的预测，以及后续实际结果对比。

    案例创建时 PIT 过滤输入文档；actual_result 由 resolve_case 在
    后续真实结果公布后填入。预测准确性由 evaluate_accuracy 聚合评估。

    Attributes:
        case_id: 案例唯一 ID。
        symbol: 公司代码（如 600000.SH）。
        theme_id: 研究主题 ID。
        as_of: 冻结时点（ISO），未来文档在此处被拦截。
        prediction_type: 预测类型（earnings_forecast / sag_propagation）。
        input_documents: PIT 过滤后保留的可见输入文档。
        prediction_horizon: 预测区间标签（如 2026Q1、60d）。
        assumptions: 预测假设列表（标签或 ID）。
        prediction_value: 预测点值。
        prediction_interval_low: 预测区间下界。
        prediction_interval_high: 预测区间上界。
        actual_result: 实际结果（resolve_case 填入，初始为 None）。
        actual_result_date: 实际结果公布日期（ISO）。
        case_category: positive / negative / no_significant。
        selection_reason: 案例选择依据（不含投资建议）。
        feature_version: 特征管道版本。
        policy_version: 策略版本。
        pit_blocked_documents: 创建案例时被 PIT 拦截的文档数。
        created_at: 案例创建时间（ISO）。
    """

    case_id: str = field(default_factory=lambda: new_id("case"))
    symbol: str = ""
    theme_id: str = ""
    as_of: str = ""
    prediction_type: str = PredictionType.EARNINGS_FORECAST.value
    input_documents: list[InputDocument] = field(default_factory=list)
    prediction_horizon: str = ""
    assumptions: list[str] = field(default_factory=list)
    prediction_value: float = 0.0
    prediction_interval_low: float = 0.0
    prediction_interval_high: float = 0.0
    actual_result: float | None = None
    actual_result_date: str = ""
    case_category: str = CaseCategory.POSITIVE.value
    selection_reason: str = ""
    feature_version: str = "v1"
    policy_version: str = "v1"
    pit_blocked_documents: int = 0
    created_at: str = field(default_factory=utc_now)


@dataclass
class RevalidationResult(Serializable):
    """单案例重验结果。

    Attributes:
        case_id: 案例唯一 ID。
        direction_correct: 实际方向是否与预测分类一致。
        interval_covered: 实际值是否落在预测区间内。
        error_magnitude: 实际值与预测点值的绝对误差。
        is_failure: 是否为失败案例（方向错且区间未覆盖）。
    """

    case_id: str = ""
    direction_correct: bool = False
    interval_covered: bool = False
    error_magnitude: float = 0.0
    is_failure: bool = False


@dataclass
class AccuracyReport(Serializable):
    """预测准确性聚合报告。

    样本不足时 insufficient_sample=True，其余指标归零；
    禁止用合成数据宣称业务通过。

    Attributes:
        total_cases: 已 resolve 的案例数。
        direction_accuracy: 方向正确率。
        interval_coverage_rate: 区间覆盖率。
        mean_error: 平均绝对误差。
        failure_cases: 失败案例 case_id 列表。
        insufficient_sample: 样本不足标志。
        disclaimer: 免责声明（固定）。
    """

    total_cases: int = 0
    direction_accuracy: float = 0.0
    interval_coverage_rate: float = 0.0
    mean_error: float = 0.0
    failure_cases: list[str] = field(default_factory=list)
    insufficient_sample: bool = False
    disclaimer: str = DISCLAIMER


# ---------------------------------------------------------------------------
# SAG 传播相关
# ---------------------------------------------------------------------------


@dataclass
class SAGClaimReaction(Serializable):
    """SAG 事实声明 + 期望传播方向 + 后续验证状态。

    Attributes:
        claim_id: SAG claim ID。
        expected_direction: 期望方向（+1 利好 / -1 利空 / 0 中性）。
        propagation_verified: 后续事实是否验证了该 claim 的传播。
        verified_at: 验证时间（ISO）。
    """

    claim_id: str = ""
    expected_direction: int = 0
    propagation_verified: bool = False
    verified_at: str = ""


@dataclass
class PriceReaction(Serializable):
    """股价反应，按 claim_id 与 SAGClaimReaction 对齐。

    Attributes:
        claim_id: 对应的 SAG claim ID。
        price_change: 在事件窗口内的带符号价格变化。
        window: 时间窗口标签（如 20d / 60d）。
    """

    claim_id: str = ""
    price_change: float = 0.0
    window: str = "20d"


@dataclass
class PropagationReport(Serializable):
    """SAG 传播评价报告。

    传播准确率与股价反应相关性分开输出：claim 可能事实正确但市场
    未反应（已 priced in），或市场反应了错误事实（噪声/投机）；
    合并会掩盖任一失效模式。

    Attributes:
        total_claims: 评估的 claim 总数。
        propagation_accuracy: 传播验证通过率。
        price_reaction_correlation: 股价反应方向与期望方向一致的比例。
        failure_cases: 失败 claim_id 列表。
        insufficient_sample: 样本不足标志。
        disclaimer: 免责声明（固定）。
    """

    total_claims: int = 0
    propagation_accuracy: float = 0.0
    price_reaction_correlation: float = 0.0
    failure_cases: list[str] = field(default_factory=list)
    insufficient_sample: bool = False
    disclaimer: str = DISCLAIMER


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _filter_documents_as_of(
    documents: list[InputDocument], as_of: str
) -> tuple[list[InputDocument], int]:
    """对输入文档做 PIT 过滤，返回 (可见文档, 被拦截数)。

    将每个 InputDocument 包成最小 ImpactAssumption（只填可见性字段），
    复用 earnings.pit.filter_inputs_as_of 的可见性规则，避免在两处
    维护 PIT 逻辑（DRY）。

    Args:
        documents: 待过滤文档列表。
        as_of: PIT 截止时点（ISO）。

    Returns:
        (visible_documents, blocked_count) 二元组。
    """
    if not isinstance(documents, (list, tuple)):
        return [], 0
    valid_documents = [doc for doc in documents if isinstance(doc, InputDocument)]
    wrappers = [
        ImpactAssumption(
            assumption_id=doc.document_id,
            variable_name=doc.title or "input_doc",
            published_at=doc.published_at,
            retrieved_at=doc.retrieved_at,
            available_at=doc.available_at,
            expires_at=doc.expires_at,
        )
        for doc in valid_documents
    ]
    visible_wrappers, blocked = filter_inputs_as_of(wrappers, as_of, historical=True)
    visible_ids = {w.assumption_id for w in visible_wrappers}
    visible_docs = [doc for doc in valid_documents if doc.document_id in visible_ids]
    return visible_docs, blocked


def _normalize_interval(low: float, high: float) -> tuple[float, float]:
    """归一化区间，保证 low <= high。"""
    try:
        low_value = float(low)
        high_value = float(high)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("prediction interval endpoints must be finite") from exc
    if not math.isfinite(low_value) or not math.isfinite(high_value):
        raise ValueError("prediction interval endpoints must be finite")
    if low_value > high_value:
        return high_value, low_value
    return low_value, high_value


def _require_finite(value: float, field_name: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    return normalized


# ---------------------------------------------------------------------------
# 盈利预测重验器
# ---------------------------------------------------------------------------


class ForecastRevalidator:
    """盈利预测 PIT 重验器。

    负责：
    - create_case: 在 as_of 冻结时点创建案例，PIT 过滤输入文档。
    - resolve_case: 后续实际结果填入并计算单案例指标。
    - evaluate_accuracy: 跨案例聚合预测准确性。

    与 Alpha（SAG 传播）分开评价：Alpha 由 SAGPropagationRevalidator 处理。
    """

    def __init__(
        self,
        earnings_repo: Any = None,
        sag_kb: Any = None,
        expectations_engine: Any = None,
    ) -> None:
        """初始化重验器。

        Args:
            earnings_repo: 可选 EarningsRepository 实例，用于持久化案例。
            sag_kb: 可选 SAGKnowledgeBase，用于交叉验证。
            expectations_engine: 可选预期差引擎，用于预期差交叉检查。
        """
        self.earnings_repo = earnings_repo
        self.sag_kb = sag_kb
        self.expectations_engine = expectations_engine

    def create_case(
        self,
        *,
        symbol: str,
        theme_id: str,
        as_of: str,
        prediction_type: str = PredictionType.EARNINGS_FORECAST.value,
        input_documents: list[InputDocument] | None = None,
        prediction_horizon: str = "",
        assumptions: list[str] | None = None,
        prediction_value: float = 0.0,
        prediction_interval_low: float = 0.0,
        prediction_interval_high: float = 0.0,
        case_category: str = CaseCategory.POSITIVE.value,
        selection_reason: str = "",
        feature_version: str = "v1",
        policy_version: str = "v1",
    ) -> RevalidationCase:
        """创建一个在 as_of 冻结的重验案例。

        PIT 安全：只有 published_at/retrieved_at/available_at 均 <= as_of
        的文档保留在 input_documents；其余计入 pit_blocked_documents。

        Args:
            symbol: 公司代码。
            theme_id: 研究主题 ID。
            as_of: 冻结时点（ISO）。
            prediction_type: 预测类型（见 PredictionType）。
            input_documents: 输入文档列表，将被 PIT 过滤。
            prediction_horizon: 预测区间标签。
            assumptions: 预测假设列表。
            prediction_value: 预测点值。
            prediction_interval_low: 预测区间下界。
            prediction_interval_high: 预测区间上界。
            case_category: positive / negative / no_significant。
            selection_reason: 案例选择依据。
            feature_version: 特征版本。
            policy_version: 策略版本。

        Returns:
            冻结后的 RevalidationCase。

        Raises:
            ValueError: symbol 或 as_of 缺失 / as_of 不是合法 ISO 时间。
        """
        if not symbol:
            raise ValueError("symbol must be provided to create a case")
        if not as_of:
            raise ValueError("as_of must be provided to freeze case time")
        if parse_time(as_of) is None:
            raise ValueError(f"as_of is not a valid ISO timestamp: {as_of!r}")

        documents = list(input_documents) if isinstance(input_documents, (list, tuple)) else []
        visible_docs, blocked = _filter_documents_as_of(documents, as_of)

        prediction_value = _require_finite(prediction_value, "prediction_value")
        low, high = _normalize_interval(prediction_interval_low, prediction_interval_high)

        return RevalidationCase(
            symbol=symbol,
            theme_id=theme_id,
            as_of=as_of,
            prediction_type=prediction_type,
            input_documents=visible_docs,
            prediction_horizon=prediction_horizon,
            assumptions=[item for item in assumptions if isinstance(item, str)]
            if isinstance(assumptions, (list, tuple))
            else [],
            prediction_value=prediction_value,
            prediction_interval_low=low,
            prediction_interval_high=high,
            case_category=case_category,
            selection_reason=selection_reason,
            feature_version=feature_version,
            policy_version=policy_version,
            pit_blocked_documents=blocked,
        )

    def _compute_metrics(self, case: RevalidationCase) -> RevalidationResult:
        """从已 resolve 的案例计算指标，不修改案例本身。

        Args:
            case: 已设置 actual_result 的案例。

        Returns:
            RevalidationResult。

        Raises:
            ValueError: case.actual_result 未设置。
        """
        actual = case.actual_result
        if actual is None:
            raise ValueError("case.actual_result must be set before computing metrics")

        # 方向判定：no_significant 类以"是否落在区间内"作为方向正确性
        if case.case_category == CaseCategory.NO_SIGNIFICANT.value:
            direction_correct = (
                case.prediction_interval_low <= actual <= case.prediction_interval_high
            )
        elif case.case_category == CaseCategory.POSITIVE.value:
            direction_correct = actual > 0
        elif case.case_category == CaseCategory.NEGATIVE.value:
            direction_correct = actual < 0
        else:
            # 防御：未知分类不判方向，仅靠区间覆盖
            direction_correct = False

        interval_covered = (
            case.prediction_interval_low <= actual <= case.prediction_interval_high
        )
        error_magnitude = abs(actual - case.prediction_value)
        # 失败：方向错且区间未覆盖（两者皆错才算失败，单边错允许"部分正确"）
        is_failure = (not direction_correct) and (not interval_covered)

        return RevalidationResult(
            case_id=case.case_id,
            direction_correct=direction_correct,
            interval_covered=interval_covered,
            error_magnitude=error_magnitude,
            is_failure=is_failure,
        )

    def resolve_case(
        self,
        case: RevalidationCase,
        *,
        actual_result: float,
        actual_result_date: str,
    ) -> RevalidationResult:
        """用实际结果 resolve 案例，返回单案例指标。

        Args:
            case: 待 resolve 的案例（会被原地修改 actual_result / date）。
            actual_result: 实际结果数值。
            actual_result_date: 实际结果公布日期（ISO）。

        Returns:
            RevalidationResult。

        Raises:
            ValueError: actual_result 为 None / actual_result_date 空 /
                actual_result_date 早于 case.as_of（违反 PIT 时序）。
        """
        if actual_result is None:
            raise ValueError("actual_result must be provided to resolve case")
        actual_result = _require_finite(actual_result, "actual_result")
        if not actual_result_date:
            raise ValueError("actual_result_date must be provided")

        # PIT 防御：实际结果公布时间不得早于案例冻结时点
        case_time = parse_time(case.as_of)
        actual_time = parse_time(actual_result_date)
        if (
            case_time is not None
            and actual_time is not None
            and actual_time < case_time
        ):
            raise ValueError(
                "actual_result_date cannot precede case.as_of "
                f"(actual={actual_result_date}, as_of={case.as_of})"
            )

        case.actual_result = actual_result
        case.actual_result_date = actual_result_date
        return self._compute_metrics(case)

    def evaluate_accuracy(
        self, cases: list[RevalidationCase]
    ) -> AccuracyReport:
        """跨案例聚合评估预测准确性。

        样本不足（< MIN_SAMPLE_FOR_ACCURACY）时返回 insufficient_sample=True，
        所有指标归零——禁止用合成数据宣称业务通过。

        Args:
            cases: 案例列表（含未 resolve 的，会被跳过）。

        Returns:
            AccuracyReport。
        """
        resolved_cases: list[RevalidationCase] = []
        for case in cases if isinstance(cases, (list, tuple)) else []:
            if not isinstance(case, RevalidationCase) or case.actual_result is None:
                continue
            try:
                values = (
                    case.actual_result,
                    case.prediction_value,
                    case.prediction_interval_low,
                    case.prediction_interval_high,
                )
                if all(math.isfinite(float(value)) for value in values):
                    resolved_cases.append(case)
            except (TypeError, ValueError, OverflowError):
                continue
        if len(resolved_cases) < MIN_SAMPLE_FOR_ACCURACY:
            return AccuracyReport(
                total_cases=len(resolved_cases),
                direction_accuracy=0.0,
                interval_coverage_rate=0.0,
                mean_error=0.0,
                failure_cases=[],
                insufficient_sample=True,
            )

        results = [self._compute_metrics(c) for c in resolved_cases]
        n = len(results)
        direction_correct_count = sum(1 for r in results if r.direction_correct)
        interval_covered_count = sum(1 for r in results if r.interval_covered)
        total_error = sum(r.error_magnitude for r in results)
        failure_cases = [r.case_id for r in results if r.is_failure]

        return AccuracyReport(
            total_cases=n,
            direction_accuracy=direction_correct_count / n,
            interval_coverage_rate=interval_covered_count / n,
            mean_error=total_error / n,
            failure_cases=failure_cases,
            insufficient_sample=False,
        )


# ---------------------------------------------------------------------------
# SAG 传播重验器
# ---------------------------------------------------------------------------


class SAGPropagationRevalidator:
    """SAG 事实传播正确性 vs 股价反应重验器。

    分两条独立轴评价：
    1. propagation_accuracy: claim 是否被后续事实验证。
    2. price_reaction_correlation: 股价反应方向是否与 expected_direction 一致。

    两条轴分开报告，避免"事实对但市场没反应"与"市场反应了错误事实"
    被合并成一个数字而互相掩盖。
    """

    def __init__(self, sag_kb: Any = None) -> None:
        """初始化。

        Args:
            sag_kb: 可选 SAGKnowledgeBase，用于交叉验证。
        """
        self.sag_kb = sag_kb

    def evaluate_sag_vs_price(
        self,
        *,
        sag_claims: list[SAGClaimReaction],
        price_reactions: list[PriceReaction],
    ) -> PropagationReport:
        """评价 SAG 传播正确性 vs 股价反应相关性。

        Args:
            sag_claims: SAG claim 列表，含期望方向与传播验证状态。
            price_reactions: 按 claim_id 对齐的股价反应列表。

        Returns:
            PropagationReport。
        """
        raw_claims = sag_claims if isinstance(sag_claims, (list, tuple)) else []
        raw_reactions = price_reactions if isinstance(price_reactions, (list, tuple)) else []
        claims = [claim for claim in raw_claims if isinstance(claim, SAGClaimReaction)]
        reactions = [reaction for reaction in raw_reactions if isinstance(reaction, PriceReaction)]
        total = len(claims)
        # 样本不足：直接返回 insufficient_sample=True，指标归零
        if total < MIN_SAMPLE_FOR_PROPAGATION:
            return PropagationReport(
                total_claims=total,
                propagation_accuracy=0.0,
                price_reaction_correlation=0.0,
                failure_cases=[],
                insufficient_sample=True,
            )

        # 按 claim_id 索引股价反应
        reaction_by_claim_id: dict[str, PriceReaction] = {
            r.claim_id: r for r in reactions if r.claim_id
        }

        propagation_correct_count = 0
        price_correlated_count = 0
        matched_count = 0
        failure_cases: list[str] = []

        for claim in claims:
            propagation_correct = bool(claim.propagation_verified)
            if propagation_correct:
                propagation_correct_count += 1

            reaction = reaction_by_claim_id.get(claim.claim_id)
            if reaction is not None:
                matched_count += 1
                # 股价方向与期望方向一致即视为相关
                if claim.expected_direction > 0 and reaction.price_change > 0:
                    price_correlated_count += 1
                elif claim.expected_direction < 0 and reaction.price_change < 0:
                    price_correlated_count += 1
                elif claim.expected_direction == 0:
                    # 中性 claim：不期望股价显著反应，默认视为相关
                    price_correlated_count += 1

            # 失败判定：传播未通过 OR 传播通过但股价反向
            is_failure = False
            if not propagation_correct:
                is_failure = True
            elif reaction is not None and (
                (claim.expected_direction > 0 and reaction.price_change < 0)
                or (claim.expected_direction < 0 and reaction.price_change > 0)
            ):
                is_failure = True
            if is_failure:
                failure_cases.append(claim.claim_id)

        propagation_accuracy = propagation_correct_count / total
        price_reaction_correlation = (
            price_correlated_count / matched_count if matched_count > 0 else 0.0
        )

        return PropagationReport(
            total_claims=total,
            propagation_accuracy=propagation_accuracy,
            price_reaction_correlation=price_reaction_correlation,
            failure_cases=failure_cases,
            insufficient_sample=False,
        )
