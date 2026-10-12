"""P3.2 行业专用模型框架基类。

按 P3 计划 Task P3.2：
- 统一接口：supports / required_inputs / build_forecast / build_risks / validate
- 行业模型只处理自身特有逻辑，通用三表和估值复用 P1
- 不适用公司明确返回 ``not_applicable``
- 输出财务、现金流、估值和风险变化，不输出交易动作
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Any


class DomainModelError(ValueError):
    """行业模型错误。"""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """把外部快照收敛为只读映射，坏输入按空快照处理。"""
    return value if isinstance(value, Mapping) else {}


def _finite_float(value: Any) -> float | None:
    """解析有限浮点数；避免 NaN/Infinity 污染预测结果。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if isfinite(parsed) else None


def _safe_float(value: Any, default: float = 0.0) -> float:
    """解析有限浮点数，失败时使用明确的业务默认值。"""
    parsed = _finite_float(value)
    return default if parsed is None else parsed


def _safe_text(value: Any, default: str = "") -> str:
    """仅接受非空文本，避免把 None/容器变成错误标识。"""
    return value.strip() if isinstance(value, str) and value.strip() else default


@dataclass(frozen=True)
class CompanyProfile:
    """公司画像；行业模型用此判断适用性。

    Attributes:
        symbol: 公司代码
        industry: 行业名称
        business_model: 商业模式（growth/cyclical/stable_cashflow/financial）
        sub_industry: 子行业（如「白酒」「半导体设备」）
        listed_at: 上市日期 YYYY-MM-DD
        operating_metrics: 经营指标快照（容量、销量、价格等）
    """

    symbol: str
    industry: str
    business_model: str
    sub_industry: str = ""
    listed_at: str = ""
    operating_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InputRequirement:
    """模型输入要求。

    Attributes:
        required_fields: 必填字段 -> 类型描述
        optional_fields: 可选字段 -> 类型描述
        data_source_refs: 依赖的数据源 ID
    """

    required_fields: dict[str, str]
    optional_fields: dict[str, str]
    data_source_refs: list[str]


@dataclass(frozen=True)
class IntegratedForecast:
    """整合预测结果；含三表联动、估值和置信度。

    Attributes:
        symbol: 公司代码
        forecast_period: 预测期间（如 ``2024Q4``）
        revenue: 营收预测
        net_profit: 净利润预测
        cash_flow: 经营现金流预测
        valuation_range: 估值区间（low/mid/high）
        confidence: 置信度 [0,1]
        drivers: 关键驱动假设
        assumptions: 显式假设清单
    """

    symbol: str
    forecast_period: str
    revenue: float
    net_profit: float
    cash_flow: float
    valuation_range: dict[str, float]
    confidence: float
    drivers: dict[str, Any]
    assumptions: list[str]


@dataclass(frozen=True)
class ResearchRisk:
    """研究风险；不含交易建议。

    Attributes:
        symbol: 公司代码
        risk_factors: 风险因子 -> 影响描述
        risk_level: 风险等级（low/medium/high）
        mitigation: 缓解措施
        invalidation_conditions: 观点失效条件
    """

    symbol: str
    risk_factors: dict[str, str]
    risk_level: str
    mitigation: list[str]
    invalidation_conditions: list[str]


@dataclass(frozen=True)
class ValidationReport:
    """模型输出验证报告。

    Attributes:
        passed: 是否通过验证
        errors: 错误清单
        warnings: 警告清单
        metrics: 验证指标（如 mape、coverage）
    """

    passed: bool
    errors: list[str]
    warnings: list[str]
    metrics: dict[str, float]


class DomainModelBase(ABC):
    """行业专用模型抽象基类。

    子类必须实现：
    - ``supports(profile)`` 判断适用性
    - ``required_inputs()`` 返回输入要求
    - ``build_forecast(snapshot, assumptions)`` 构建整合预测
    - ``build_risks(snapshot)`` 构建研究风险
    - ``validate(output)`` 验证输出
    """

    #: 行业模型 ID（子类覆盖）
    model_id: str = ""
    #: 适用行业（子类覆盖）
    applicable_industries: tuple[str, ...] = ()

    @abstractmethod
    def supports(self, profile: CompanyProfile) -> bool:
        """判断公司画像是否适用于本行业模型。

        Args:
            profile: 公司画像

        Returns:
            bool: True 表示适用；False 表示 ``not_applicable``
        """

    @abstractmethod
    def required_inputs(self) -> InputRequirement:
        """返回模型输入要求。"""

    @abstractmethod
    def build_forecast(
        self,
        snapshot: dict[str, Any],
        assumptions: dict[str, Any],
    ) -> IntegratedForecast:
        """构建整合预测；不输出交易动作。

        Args:
            snapshot: 公司当前快照（三表、经营指标）
            assumptions: 显式假设

        Returns:
            IntegratedForecast: 整合预测结果
        """

    @abstractmethod
    def build_risks(self, snapshot: dict[str, Any]) -> ResearchRisk:
        """构建研究风险；不含交易建议。"""

    @abstractmethod
    def validate(self, output: IntegratedForecast) -> ValidationReport:
        """验证模型输出。"""

    def not_applicable_reason(self, profile: CompanyProfile) -> str:
        """返回不适用原因（默认实现）。"""
        industry = _safe_text(getattr(profile, "industry", None))
        if industry not in self.applicable_industries:
            return (
                f"行业不适用：{industry} 不在 "
                f"{self.applicable_industries} 中"
            )
        return ""
