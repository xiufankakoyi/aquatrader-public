"""P1.8 多方法估值与敏感性契约。

定义估值方法枚举、可计算性状态、估值假设、估值输入、敏感性点、
估值案例、估值报告与估值缺口等数据结构。

设计要点：
- 所有定量输出使用 Interval 区间，避免伪精确。
- 每个假设必须带 source_ref 和 as_of；缺 source_ref 时引擎返回 NOT_APPLICABLE。
- 方法不适用（如银行不用 EV/EBITDA）返回 NOT_APPLICABLE，不是失败或默认值。
- 输出区间和敏感性，不输出单点动作建议。
- 不输出买入/卖出/仓位/止盈/止损等投资建议。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from core.research.earnings.interval import Interval


def utc_now() -> str:
    """返回当前 UTC ISO 时间。

    Returns:
        ISO 8601 格式时间字符串
    """
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。

    Args:
        prefix: ID 前缀，如 "valc" / "valr" / "valg"

    Returns:
        形如 "valc_xxxxxxxxxxxxxxxx" 的唯一 ID
    """
    return f"{prefix}_{uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ValuationMethod(str, Enum):
    """估值方法枚举。

    覆盖 CFA 公司分析常用方法：
    - 历史倍数（PE/PB/PS）
    - Forward 倍数（PE/PEG/EV-EBITDA/FCF Yield）
    - DCF（FCFF/FCFE）
    - DDM 股利折现
    - SOTP 分部加总
    - 剩余收益
    - Reverse DCF 反推隐含增长率
    - 同业可比公司
    """

    HISTORICAL_PE = "historical_pe"
    HISTORICAL_PB = "historical_pb"
    HISTORICAL_PS = "historical_ps"
    FORWARD_PE = "forward_pe"
    PEG = "peg"
    EV_EBITDA = "ev_ebitda"
    FCF_YIELD = "fcf_yield"
    DCF_FCFF = "dcf_fcff"
    DCF_FCFE = "dcf_fcfe"
    DDM = "ddm"
    SOTP = "sotp"
    RESIDUAL_INCOME = "residual_income"
    REVERSE_DCF = "reverse_dcf"
    COMPARABLE_COMPANY = "comparable_company"

    @classmethod
    def all_values(cls) -> list[str]:
        """返回所有方法 value 列表。"""
        return [m.value for m in cls]


class Computability(str, Enum):
    """可计算性状态。

    COMPUTABLE: 方法可计算，输出区间有效
    NOT_APPLICABLE: 方法不适用（如银行不用 EV/EBITDA）
    INSUFFICIENT_INPUT: 缺关键输入
    METHOD_MISMATCH: 行业/商业模式不匹配
    """

    COMPUTABLE = "computable"
    NOT_APPLICABLE = "not_applicable"
    INSUFFICIENT_INPUT = "insufficient_input"
    METHOD_MISMATCH = "method_mismatch"


# 行业过滤规则：金融业不适用 EV/EBITDA / SOTP / FCF Yield
FINANCIAL_INDUSTRY_KEYWORDS: tuple[str, ...] = ("bank", "insurance", "securities", "financial", "金融", "银行", "保险", "证券")
METHODS_NOT_FOR_FINANCIAL: tuple[str, ...] = (
    ValuationMethod.EV_EBITDA.value,
    ValuationMethod.SOTP.value,
    ValuationMethod.FCF_YIELD.value,
    ValuationMethod.DCF_FCFF.value,
    ValuationMethod.DCF_FCFE.value,
    ValuationMethod.REVERSE_DCF.value,
)


# ---------------------------------------------------------------------------
# 假设与输入
# ---------------------------------------------------------------------------

@dataclass
class ValuationAssumption:
    """单个估值假设，必须带 source_ref 和 as_of。

    WACC、无风险利率、风险溢价、Beta、债务成本、终值增长率等关键假设
    均通过本结构保存来源与版本，缺 source_ref 时引擎返回 NOT_APPLICABLE。

    Attributes:
        name: 假设名，如 "wacc" / "risk_free_rate" / "beta" / "terminal_growth"
        value: 假设值
        unit: 单位，如 "ratio" / "percent"
        source_ref: 来源引用（claim_id 或 evidence_id）
        source_kind: 来源类型，如 "manual" / "consensus" / "statistical"
        as_of: 假设生效时点（ISO 8601）
        note: 备注
    """

    name: str
    value: float
    unit: str = "ratio"
    source_ref: str = ""
    source_kind: str = ""
    as_of: str = ""
    note: str = ""

    def has_source(self) -> bool:
        """检查是否带来源引用。"""
        return isinstance(self.source_ref, str) and bool(self.source_ref.strip())


@dataclass
class ValuationInput:
    """估值输入集合。

    Attributes:
        company_symbol: 公司代码
        as_of: PIT 时点
        scenario: 情景，如 "base" / "bull" / "bear"
        current_price: 当前股价
        shares_outstanding: 总股本
        market_cap: 市值
        total_debt: 有息负债
        cash_and_equivalents: 现金及等价物
        minority_interest: 少数股东权益
        associates_investments: 联营投资
        diluted_shares: 稀释股本
        historical_multiples: 历史倍数序列，如 {"historical_pe": [...], "historical_pb": [...]}
        trailing_eps: 最近可见每股收益
        book_value_per_share: 最近可见每股净资产
        revenue_per_share: 最近可见每股收入
        consensus_eps: 一致预期 EPS
        consensus_net_profit: 一致预期净利润
        consensus_revenue: 一致预期收入
        ebitda_ttm: TTM EBITDA
        fcf_ttm: TTM 自由现金流
        dividends_per_share: 每股股利
        fcff_series: FCFF 预测期序列
        fcfe_series: FCFE 预测期序列
        dividend_series: 股利预测期序列
        assumptions: CAPM 假设列表
        comparable_companies: 同业可比公司列表，每个 dict 含 symbol + pe/pb/ev_ebitda
        market_data_version: 市场数据版本
        financial_data_version: 财务数据版本
        model_version: 模型版本
        industry: 行业标签，用于行业过滤
    """

    company_symbol: str = ""
    as_of: str = ""
    scenario: str = "base"
    # 市场数据
    current_price: float = 0.0
    shares_outstanding: float = 0.0
    market_cap: float = 0.0
    # 资产负债表项
    total_debt: float = 0.0
    cash_and_equivalents: float = 0.0
    minority_interest: float = 0.0
    associates_investments: float = 0.0
    diluted_shares: float = 0.0
    # 财务输入
    historical_multiples: dict[str, list[float]] = field(default_factory=dict)
    trailing_eps: float = 0.0
    book_value_per_share: float = 0.0
    revenue_per_share: float = 0.0
    consensus_eps: float = 0.0
    consensus_net_profit: float = 0.0
    consensus_revenue: float = 0.0
    ebitda_ttm: float = 0.0
    fcf_ttm: float = 0.0
    dividends_per_share: float = 0.0
    # DCF 输入
    fcff_series: list[float] = field(default_factory=list)
    fcfe_series: list[float] = field(default_factory=list)
    dividend_series: list[float] = field(default_factory=list)
    # CAPM 假设
    assumptions: list[ValuationAssumption] = field(default_factory=list)
    # 可比公司
    comparable_companies: list[dict] = field(default_factory=list)
    # 行业标签（用于行业过滤）
    industry: str = ""
    # 数据版本
    market_data_version: str = ""
    financial_data_version: str = ""
    model_version: str = "v1"

    def assumption(self, name: str) -> ValuationAssumption | None:
        """按名称获取假设。

        Args:
            name: 假设名

        Returns:
            匹配的 ValuationAssumption；无匹配返回 None
        """
        assumptions = self.assumptions if isinstance(self.assumptions, (list, tuple)) else []
        for a in assumptions:
            if isinstance(a, ValuationAssumption) and a.name == name:
                return a
        return None

    def is_financial_industry(self) -> bool:
        """判断是否为金融业（用于方法适用性过滤）。"""
        if not self.industry:
            return False
        if not isinstance(self.industry, str):
            return False
        ind_lower = self.industry.lower()
        return any(kw in ind_lower for kw in FINANCIAL_INDUSTRY_KEYWORDS)


# ---------------------------------------------------------------------------
# 敏感性与结果
# ---------------------------------------------------------------------------

@dataclass
class SensitivityPoint:
    """敏感性矩阵单个点。

    Attributes:
        variable_name: 变量名，如 "wacc" / "growth" / "wacc_growth"
        variable_value: 变量取值
        output_low: 输出区间下界
        output_high: 输出区间上界
    """

    variable_name: str
    variable_value: float
    output_low: float
    output_high: float

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "SensitivityPoint":
        """反序列化。"""
        data = dict(raw) if isinstance(raw, Mapping) else {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ValuationCase:
    """单次估值结果。

    Attributes:
        case_id: 案例唯一 ID
        company_symbol: 公司代码
        method: 估值方法（ValuationMethod.value）
        as_of: PIT 时点
        scenario: 情景
        inputs_snapshot: 输入快照（用于审计）
        output_interval: 输出区间（企业价值或权益价值，取决于方法）
        output_per_share: 每股价值区间
        formula_trace: 公式轨迹，字符串列表
        sensitivity: 敏感性点列表
        source_refs: 来源引用列表
        assumptions: 假设列表
        computability: 可计算性状态
        limitations: 限制说明列表
        method_specific_metrics: 方法特有指标，如 terminal_value_pct / implied_growth
        model_version: 模型版本
        created_at: 创建时间
    """

    case_id: str = field(default_factory=lambda: new_id("valc"))
    company_symbol: str = ""
    method: str = ""
    as_of: str = ""
    scenario: str = "base"
    inputs_snapshot: dict[str, Any] = field(default_factory=dict)
    output_interval: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    output_per_share: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    formula_trace: list[str] = field(default_factory=list)
    sensitivity: list[SensitivityPoint] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    assumptions: list[ValuationAssumption] = field(default_factory=list)
    computability: str = Computability.COMPUTABLE.value
    limitations: list[str] = field(default_factory=list)
    method_specific_metrics: dict[str, Any] = field(default_factory=dict)
    model_version: str = "v1"
    created_at: str = field(default_factory=utc_now)

    def is_computable(self) -> bool:
        """是否可计算。"""
        return self.computability == Computability.COMPUTABLE.value

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（Interval 转为 {low, high}）。"""
        data = asdict(self)
        output_interval = self.output_interval if isinstance(self.output_interval, Interval) else Interval(0.0, 0.0)
        output_per_share = self.output_per_share if isinstance(self.output_per_share, Interval) else Interval(0.0, 0.0)
        data["output_interval"] = output_interval.to_dict()
        data["output_per_share"] = output_per_share.to_dict()
        sensitivity = self.sensitivity if isinstance(self.sensitivity, (list, tuple)) else []
        data["sensitivity"] = [s.to_dict() if hasattr(s, "to_dict") else s for s in sensitivity]
        assumptions = self.assumptions if isinstance(self.assumptions, (list, tuple)) else []
        data["assumptions"] = [a if isinstance(a, dict) else asdict(a) for a in assumptions if isinstance(a, (dict, ValuationAssumption))]
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ValuationCase":
        """从 dict 反序列化。"""
        data = dict(raw) if isinstance(raw, Mapping) else {}
        oi = Interval.from_dict(data.get("output_interval", {}))
        ops = Interval.from_dict(data.get("output_per_share", {}))
        data["output_interval"] = oi if oi is not None else Interval(0.0, 0.0)
        data["output_per_share"] = ops if ops is not None else Interval(0.0, 0.0)
        assumptions_raw = data.get("assumptions", []) or []
        assumptions: list[ValuationAssumption] = []
        for item in assumptions_raw:
            if not isinstance(item, Mapping):
                continue
            try:
                assumptions.append(ValuationAssumption(**dict(item)))
            except (TypeError, ValueError):
                continue
        data["assumptions"] = assumptions
        sens_raw = data.get("sensitivity", []) or []
        data["sensitivity"] = [
            SensitivityPoint.from_dict(s)
            for s in sens_raw
            if isinstance(s, Mapping)
        ]
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ValuationReport:
    """多方法估值报告。

    不同方法结果在 method_differences 中解释差异，不简单平均。
    overall_range 取所有可计算案例的 [min(low), max(high)]，标注为 "min_max"。

    Attributes:
        report_id: 报告唯一 ID
        company_symbol: 公司代码
        as_of: PIT 时点
        scenario: 情景
        cases: 估值案例列表（含可计算与不可计算）
        method_differences: 方法差异说明列表
        overall_range: 综合区间
        overall_range_method: 综合方式，默认 "min_max"
        limitations: 报告级限制说明
        notice: 固定提示「研究数据产物，不包含交易动作建议。」
        model_version: 模型版本
        created_at: 创建时间
    """

    report_id: str = field(default_factory=lambda: new_id("valr"))
    company_symbol: str = ""
    as_of: str = ""
    scenario: str = "base"
    cases: list[ValuationCase] = field(default_factory=list)
    method_differences: list[dict[str, Any]] = field(default_factory=list)
    overall_range: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    overall_range_method: str = "min_max"
    limitations: list[str] = field(default_factory=list)
    notice: str = "研究数据产物，不包含交易动作建议。"
    model_version: str = "v1"
    created_at: str = field(default_factory=utc_now)

    def computable_cases(self) -> list[ValuationCase]:
        """返回所有可计算案例。"""
        cases = self.cases if isinstance(self.cases, (list, tuple)) else []
        return [c for c in cases if isinstance(c, ValuationCase) and c.is_computable()]

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        data = asdict(self)
        overall_range = self.overall_range if isinstance(self.overall_range, Interval) else Interval(0.0, 0.0)
        data["overall_range"] = overall_range.to_dict()
        cases = self.cases if isinstance(self.cases, (list, tuple)) else []
        data["cases"] = [c.to_dict() if hasattr(c, "to_dict") else c for c in cases if isinstance(c, (ValuationCase, Mapping))]
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ValuationReport":
        """从 dict 反序列化。"""
        data = dict(raw) if isinstance(raw, Mapping) else {}
        orng = Interval.from_dict(data.get("overall_range", {}))
        data["overall_range"] = orng if orng is not None else Interval(0.0, 0.0)
        cases_raw = data.get("cases", []) or []
        data["cases"] = [
            ValuationCase.from_dict(c)
            for c in cases_raw
            if isinstance(c, Mapping)
        ]
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ValuationGap:
    """估值缺口（方法不可计算时的降级记录）。

    Attributes:
        gap_id: 缺口唯一 ID
        company_symbol: 公司代码
        method: 估值方法
        blocker_kind: 阻断类型，如 "missing_wacc" / "missing_beta" / "missing_fcf_series"
        reason: 阻断原因
        as_of: PIT 时点
    """

    gap_id: str = field(default_factory=lambda: new_id("valg"))
    company_symbol: str = ""
    method: str = ""
    blocker_kind: str = ""
    reason: str = ""
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ValuationGap":
        """反序列化。"""
        data = dict(raw) if isinstance(raw, Mapping) else {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
