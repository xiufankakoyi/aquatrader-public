"""P3.4 宏观传导路径注册表与传导引擎。

按 P3 计划 Task P3.4：
- 宏观变量通过显式传导路径影响行业和公司驱动
- 每条传导路径包含方向、时滞、适用范围和证据
- 相关性不能自动声明因果；缺乏证据的路径默认 correlation_only
- 输出财务、现金流、估值和风险变化，不输出交易动作

设计要点：
- ``MacroTransmissionRegistry`` 注册 / 查询传导路径；支持按 source / target / 行业过滤
- ``build_default_registry`` 提供 5 类宏观变量的标准传导路径模板
  （模板路径 ``evidence_refs`` 为空，默认 ``correlation_only=True``；
  调用方必须通过 ``attach_evidence`` 显式补证据后才能用于因果传导）
- ``MacroTransmissionEngine`` 计算一组宏观冲击下的 driver impacts
  - impact_pct = shock_fractional_change × sign × sensitivity × confidence × lag_factor
  - lag_factor = max(0, (horizon_months - lag_months) / horizon_months)
"""
from __future__ import annotations

import math
from typing import Iterable

from .contracts import (
    CorrelationCausationError,
    DIRECTION_NEGATIVE,
    DIRECTION_NEUTRAL,
    DIRECTION_POSITIVE,
    DriverImpact,
    MacroShock,
    ScenarioContractError,
    TARGET_DRIVER_DISCOUNT_RATE,
    TARGET_DRIVER_FCF_CONVERSION,
    TARGET_DRIVER_GROSS_MARGIN,
    TARGET_DRIVER_OPEX_RATIO,
    TARGET_DRIVER_REVENUE,
    TARGET_DRIVER_VALUATION_MULTIPLE,
    TARGET_DRIVER_CAPEX,
    TransmissionPath,
    assert_causal_path,
)


# ---------------------------------------------------------------------------
# 注册表错误
# ---------------------------------------------------------------------------


class TransmissionRegistryError(ValueError):
    """传导路径注册表错误。"""


# ---------------------------------------------------------------------------
# 默认传导路径模板
# ---------------------------------------------------------------------------

# 说明：以下路径为「结构化模板」，evidence_refs 默认为空。
# 调用方必须通过 attach_evidence 显式补证据并通过 assert_causal_path 校验后，
# 才能用于因果压力测试。模板本身只描述方向、时滞、适用范围与灵敏度，
# 不构成对任何具体公司业务或证据来源的断言。

def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_nonnegative_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


_DEFAULT_PATH_TEMPLATES: tuple[dict, ...] = (
    # ---- 1. 利率 / 汇率 / 通胀 / 信用 ----
    {
        "source_variable": "policy_rate",
        "target_driver": TARGET_DRIVER_DISCOUNT_RATE,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 0,
        "applicable_industries": (),
        "confidence": 0.80,
        "sensitivity": 1.00,
        "description": "政策利率上升推高折现率",
    },
    {
        "source_variable": "policy_rate",
        "target_driver": TARGET_DRIVER_VALUATION_MULTIPLE,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 0,
        "applicable_industries": (),
        "confidence": 0.75,
        "sensitivity": 1.50,
        "description": "政策利率上升压缩估值倍数",
    },
    {
        "source_variable": "policy_rate",
        "target_driver": TARGET_DRIVER_FCF_CONVERSION,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 3,
        "applicable_industries": (),
        "confidence": 0.55,
        "sensitivity": 0.30,
        "description": "利率上升抬升利息费用，降低 FCF 转化率",
    },
    {
        "source_variable": "cny_usd",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 1,
        "applicable_industries": ("电子", "家电", "纺织服装", "机械"),
        "confidence": 0.60,
        "sensitivity": 0.50,
        "description": "人民币贬值利好出口导向行业营收",
    },
    {
        "source_variable": "cpi",
        "target_driver": TARGET_DRIVER_OPEX_RATIO,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 2,
        "applicable_industries": ("消费", "零售", "食品饮料"),
        "confidence": 0.50,
        "sensitivity": 0.40,
        "description": "CPI 上升推高消费行业运营费用率",
    },
    {
        "source_variable": "credit_spread",
        "target_driver": TARGET_DRIVER_DISCOUNT_RATE,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 1,
        "applicable_industries": (),
        "confidence": 0.65,
        "sensitivity": 0.80,
        "description": "信用利差走阔抬升折现率",
    },
    # ---- 2. 商品价格 / 能源成本 ----
    {
        "source_variable": "oil_price",
        "target_driver": TARGET_DRIVER_GROSS_MARGIN,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 1,
        "applicable_industries": ("航空", "运输", "化工", "塑料"),
        "confidence": 0.70,
        "sensitivity": 0.60,
        "description": "油价上升侵蚀能源消耗行业毛利率",
    },
    {
        "source_variable": "oil_price",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 1,
        "applicable_industries": ("石油石化", "煤炭", "油气开采"),
        "confidence": 0.75,
        "sensitivity": 0.80,
        "description": "油价上升利好能源开采行业营收",
    },
    {
        "source_variable": "steel_price",
        "target_driver": TARGET_DRIVER_GROSS_MARGIN,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 1,
        "applicable_industries": ("汽车", "家电", "机械", "建筑"),
        "confidence": 0.65,
        "sensitivity": 0.50,
        "description": "钢价上升压缩下游制造业毛利率",
    },
    {
        "source_variable": "lithium_price",
        "target_driver": TARGET_DRIVER_GROSS_MARGIN,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 2,
        "applicable_industries": ("电池", "新能源汽车"),
        "confidence": 0.60,
        "sensitivity": 0.70,
        "description": "锂价上升压缩电池及新能源车毛利率",
    },
    # ---- 3. 终端需求 / 资本开支 ----
    {
        "source_variable": "pmi",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 0,
        "applicable_industries": (),
        "confidence": 0.70,
        "sensitivity": 1.20,
        "description": "PMI 上行提振全行业营收",
    },
    {
        "source_variable": "consumer_confidence",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 1,
        "applicable_industries": ("消费", "零售", "家电", "食品饮料"),
        "confidence": 0.65,
        "sensitivity": 0.80,
        "description": "消费者信心上升利好可选消费营收",
    },
    {
        "source_variable": "fixed_asset_investment",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 2,
        "applicable_industries": ("机械", "建材", "钢铁", "电气设备"),
        "confidence": 0.65,
        "sensitivity": 0.60,
        "description": "固定资产投资上行提振中上游制造业营收",
    },
    {
        "source_variable": "capex_index",
        "target_driver": TARGET_DRIVER_CAPEX,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 0,
        "applicable_industries": (),
        "confidence": 0.55,
        "sensitivity": 1.00,
        "description": "宏观资本开支指数同步驱动公司资本开支",
    },
    # ---- 4. 贸易 / 关税 / 政策 / 供应链 ----
    {
        "source_variable": "tariff_rate",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 1,
        "applicable_industries": ("电子", "家电", "纺织服装", "机械"),
        "confidence": 0.65,
        "sensitivity": 0.70,
        "description": "关税上升压缩出口导向行业营收",
    },
    {
        "source_variable": "supply_chain_index",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 0,
        "applicable_industries": ("电子", "半导体", "汽车"),
        "confidence": 0.60,
        "sensitivity": 0.60,
        "description": "供应链压力指数上升拖累制造业营收",
    },
    {
        "source_variable": "export_control",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 1,
        "applicable_industries": ("半导体", "电子"),
        "confidence": 0.55,
        "sensitivity": 0.50,
        "description": "出口管制收紧影响半导体及电子行业营收",
    },
    # ---- 5. 行业价格 / 库存 / 利用率 ----
    {
        "source_variable": "industry_inventory_days",
        "target_driver": TARGET_DRIVER_GROSS_MARGIN,
        "direction": DIRECTION_NEGATIVE,
        "lag_months": 0,
        "applicable_industries": (),
        "confidence": 0.60,
        "sensitivity": 0.40,
        "description": "行业库存天数上升压低毛利率",
    },
    {
        "source_variable": "industry_capacity_util",
        "target_driver": TARGET_DRIVER_GROSS_MARGIN,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 1,
        "applicable_industries": (),
        "confidence": 0.60,
        "sensitivity": 0.50,
        "description": "行业产能利用率上升支撑毛利率",
    },
    {
        "source_variable": "industry_price_index",
        "target_driver": TARGET_DRIVER_REVENUE,
        "direction": DIRECTION_POSITIVE,
        "lag_months": 0,
        "applicable_industries": (),
        "confidence": 0.70,
        "sensitivity": 1.00,
        "description": "行业价格指数上升直接传导至营收",
    },
)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class MacroTransmissionRegistry:
    """宏观传导路径注册表。

    支持注册、查询、行业过滤和证据升级。

    Usage::

        registry = build_default_registry()
        # 默认路径 correlation_only=True，需补证据才能用于因果传导
        registry.attach_evidence(path_id, evidence_refs=("ev_001",), confidence=0.75)
        paths = registry.paths_for_industry("电子")
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._paths: dict[str, TransmissionPath] = {}

    # ------------------ 注册 / 查询 ------------------

    def register(self, path: TransmissionPath) -> TransmissionPath:
        """注册一条传导路径。

        Args:
            path: 待注册路径

        Returns:
            TransmissionPath: 已注册路径

        Raises:
            TransmissionRegistryError: 路径 ID 重复
        """
        if not isinstance(path, TransmissionPath):
            raise TransmissionRegistryError("path 必须是 TransmissionPath")
        if path.path_id in self._paths:
            raise TransmissionRegistryError(
                f"传导路径已存在：path_id={path.path_id}"
                f"（source={path.source_variable}, target={path.target_driver}）"
            )
        self._paths[path.path_id] = path
        return path

    def get(self, path_id: str) -> TransmissionPath | None:
        """按 path_id 查询。"""
        return self._paths.get(path_id)

    def all_paths(self) -> tuple[TransmissionPath, ...]:
        """返回所有路径。"""
        return tuple(self._paths.values())

    def paths_for_source(self, source_variable: str) -> tuple[TransmissionPath, ...]:
        """按源宏观变量查询路径。"""
        return tuple(
            p for p in self._paths.values() if p.source_variable == source_variable
        )

    def paths_for_target(self, target_driver: str) -> tuple[TransmissionPath, ...]:
        """按目标驱动查询路径。"""
        return tuple(
            p for p in self._paths.values() if p.target_driver == target_driver
        )

    def paths_for_industry(self, industry: str) -> tuple[TransmissionPath, ...]:
        """查询适用于某行业的所有路径。

        Args:
            industry: 行业名称

        Returns:
            适用路径元组（含 applicable_industries 为空的「全行业」路径）
        """
        return tuple(
            p for p in self._paths.values() if p.applies_to_industry(industry)
        )

    def paths_for_shock(
        self, shock: MacroShock, industry: str
    ) -> tuple[TransmissionPath, ...]:
        """查询某冲击在某行业下适用的传导路径。

        Args:
            shock: 宏观冲击
            industry: 公司行业

        Returns:
            适用路径元组
        """
        return tuple(
            p
            for p in self._paths.values()
            if p.source_variable == shock.variable.name
            and p.applies_to_industry(industry)
        )

    # ------------------ 证据升级 ------------------

    def attach_evidence(
        self,
        path_id: str,
        evidence_refs: Iterable[str],
        confidence: float | None = None,
    ) -> TransmissionPath:
        """为路径附加证据，从相关性观察升级为因果路径。

        Args:
            path_id: 路径 ID
            evidence_refs: 证据 ID 列表（至少一条）
            confidence: 可选；覆盖原置信度

        Returns:
            TransmissionPath: 升级后的新路径（path_id 也会重新派生）

        Raises:
            TransmissionRegistryError: 路径不存在
            ScenarioContractError: 证据为空或置信度仍低于阈值
        """
        old = self._paths.get(path_id)
        if old is None:
            raise TransmissionRegistryError(f"路径不存在：{path_id}")
        try:
            raw_evidence = tuple(evidence_refs)
        except TypeError:
            raw_evidence = ()
        evs = tuple(e for e in raw_evidence if isinstance(e, str) and e.strip())
        if not evs:
            raise ScenarioContractError(
                "attach_evidence 要求至少一条证据；空证据不能升级为因果路径"
            )
        new_conf = old.confidence if confidence is None else _safe_float(confidence, default=math.nan)
        new_path = TransmissionPath(
            source_variable=old.source_variable,
            target_driver=old.target_driver,
            direction=old.direction,
            lag_months=old.lag_months,
            applicable_industries=old.applicable_industries,
            evidence_refs=evs,
            confidence=new_conf,
            description=old.description,
            sensitivity=old.sensitivity,
        )
        # path_id 派生不包含 evidence_refs，但包含 confidence；若 confidence
        # 发生变化则新 path_id 与原 path_id 不同，需显式移除旧条目避免残留。
        old_path_id = old.path_id
        new_path_id = new_path.path_id
        if old_path_id in self._paths:
            del self._paths[old_path_id]
        if new_path_id in self._paths and new_path_id != old_path_id:
            raise TransmissionRegistryError(
                f"升级后的路径 ID 与已存在路径冲突：{new_path_id}"
            )
        self._paths[new_path_id] = new_path
        return new_path

    def __len__(self) -> int:
        """返回路径总数。"""
        return len(self._paths)


# ---------------------------------------------------------------------------
# 默认注册表工厂
# ---------------------------------------------------------------------------


def build_default_registry() -> MacroTransmissionRegistry:
    """构造包含 5 类宏观变量标准传导路径模板的注册表。

    模板路径 ``evidence_refs`` 默认为空，因此 ``correlation_only=True``。
    调用方必须通过 ``attach_evidence`` 显式补证据并通过 ``assert_causal_path``
    校验后才能用于因果压力测试。

    Returns:
        MacroTransmissionRegistry: 含 20 条标准模板路径的注册表
    """
    registry = MacroTransmissionRegistry()
    for template in _DEFAULT_PATH_TEMPLATES:
        path = TransmissionPath(
            source_variable=template["source_variable"],
            target_driver=template["target_driver"],
            direction=template["direction"],
            lag_months=template["lag_months"],
            applicable_industries=template["applicable_industries"],
            evidence_refs=(),  # 模板默认无证据 → correlation_only=True
            confidence=template["confidence"],
            description=template["description"],
            sensitivity=template["sensitivity"],
        )
        registry.register(path)
    return registry


# ---------------------------------------------------------------------------
# 传导引擎
# ---------------------------------------------------------------------------


class MacroTransmissionEngine:
    """宏观传导引擎；计算一组宏观冲击下的 driver impacts。

    计算公式::

        impact_pct = shock_fractional_change × sign × sensitivity × confidence × lag_factor

    其中 lag_factor 反映路径时滞在评估期内的兑现比例::

        lag_factor = max(0, (horizon_months - lag_months) / horizon_months)

    - ``horizon_months = 0`` 时视作当期不兑现；返回空 impact 列表
    - ``allow_correlation_only = False`` 时跳过 ``correlation_only=True`` 的路径
      并把其 path_id 收集到 ``correlation_only_paths`` 中
    """

    def __init__(
        self,
        registry: MacroTransmissionRegistry,
        *,
        horizon_months: int = 12,
        allow_correlation_only: bool = False,
    ) -> None:
        """初始化传导引擎。

        Args:
            registry: 传导路径注册表
            horizon_months: 评估期（月）；默认 12
            allow_correlation_only: 是否允许相关性观察路径参与传导；
                                    默认 False（相关性 ≠ 因果）
        """
        if horizon_months < 0:
            raise ScenarioContractError(
                f"horizon_months 不能为负：{horizon_months}"
            )
        safe_horizon = _safe_nonnegative_int(horizon_months, default=-1)
        if safe_horizon < 0:
            raise ScenarioContractError("horizon_months 必须是非负整数")
        if not isinstance(registry, MacroTransmissionRegistry):
            raise ScenarioContractError("registry 必须是 MacroTransmissionRegistry")
        self._registry = registry
        self._horizon_months = safe_horizon
        self._allow_correlation_only = bool(allow_correlation_only)

    @property
    def registry(self) -> MacroTransmissionRegistry:
        """返回关联的注册表。"""
        return self._registry

    @property
    def horizon_months(self) -> int:
        """返回评估期。"""
        return self._horizon_months

    @property
    def allow_correlation_only(self) -> bool:
        """是否允许相关性观察路径参与传导。"""
        return self._allow_correlation_only

    def lag_factor(self, lag_months: int) -> float:
        """计算时滞兑现因子。

        Args:
            lag_months: 路径时滞（月）

        Returns:
            float: [0, 1]；horizon=0 时返回 0
        """
        if self._horizon_months <= 0:
            return 0.0
        if lag_months >= self._horizon_months:
            return 0.0
        return (self._horizon_months - lag_months) / self._horizon_months

    def compute_driver_impacts(
        self,
        shocks: Iterable[MacroShock],
        industry: str,
    ) -> tuple[tuple[DriverImpact, ...], tuple[str, ...], tuple[str, ...]]:
        """计算一组宏观冲击在某行业下的 driver impacts。

        Args:
            shocks: 宏观冲击列表
            industry: 公司行业

        Returns:
            tuple:
                - driver_impacts: 驱动影响列表（已按 driver 聚合）
                - correlation_only_paths: 被跳过的相关性观察路径 ID
                - skipped_no_path: 未匹配到传导路径的宏观变量名
        """
        impacts_by_driver: dict[str, list[DriverImpact]] = {}
        correlation_only: list[str] = []
        skipped: list[str] = []

        safe_shocks = shocks if isinstance(shocks, (list, tuple, set)) else ()
        for shock in safe_shocks:
            if not isinstance(shock, MacroShock):
                continue
            paths = self._registry.paths_for_shock(shock, industry)
            if not paths:
                skipped.append(shock.variable.name)
                continue
            fractional = shock.to_fractional_change()
            for path in paths:
                # 相关性观察路径过滤
                if path.correlation_only and not self._allow_correlation_only:
                    correlation_only.append(path.path_id)
                    continue
                # 显式断言：用于因果传导的路径必须满足 causal 条件
                if not path.correlation_only:
                    assert_causal_path(path)
                factor = self.lag_factor(path.lag_months)
                if factor <= 0:
                    # 时滞未到，不产生当期影响（但仍记录路径以保留审计痕迹）
                    correlation_only.append(path.path_id)
                    continue
                impact_pct = (
                    fractional
                    * path.sign()
                    * path.sensitivity
                    * path.confidence
                    * factor
                    * 100.0  # 转为百分比
                )
                di = DriverImpact(
                    driver=path.target_driver,
                    path_id=path.path_id,
                    source_variable=path.source_variable,
                    shock_fractional_change=fractional,
                    impact_pct=impact_pct,
                    lag_months=path.lag_months,
                    confidence=path.confidence,
                    evidence_refs=path.evidence_refs,
                    note=path.description,
                )
                impacts_by_driver.setdefault(path.target_driver, []).append(di)

        # 按 driver 聚合（求和）
        aggregated: list[DriverImpact] = []
        for driver, items in impacts_by_driver.items():
            total_impact = sum(i.impact_pct for i in items)
            # 取置信度最低的路径作为聚合置信度（保守估计）
            min_conf = min(i.confidence for i in items) if items else 0.0
            max_lag = max(i.lag_months for i in items) if items else 0
            ev_refs = tuple(
                sorted({e for i in items for e in i.evidence_refs})
            )
            sources = sorted({i.source_variable for i in items})
            aggregated.append(
                DriverImpact(
                    driver=driver,
                    path_id=",".join(i.path_id for i in items),
                    source_variable="+".join(sources),
                    shock_fractional_change=sum(
                        i.shock_fractional_change for i in items
                    ),
                    impact_pct=total_impact,
                    lag_months=max_lag,
                    confidence=min_conf,
                    evidence_refs=ev_refs,
                    note=f"聚合自 {len(items)} 条路径",
                )
            )

        return (
            tuple(aggregated),
            tuple(sorted(set(correlation_only))),
            tuple(sorted(set(skipped))),
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_default_engine(
    *,
    horizon_months: int = 12,
    allow_correlation_only: bool = False,
) -> MacroTransmissionEngine:
    """构造默认传导引擎（含 5 类标准模板路径）。

    Args:
        horizon_months: 评估期（月）
        allow_correlation_only: 是否允许相关性观察路径参与传导

    Returns:
        MacroTransmissionEngine
    """
    registry = build_default_registry()
    return MacroTransmissionEngine(
        registry=registry,
        horizon_months=horizon_months,
        allow_correlation_only=allow_correlation_only,
    )


__all__ = [
    "TransmissionRegistryError",
    "MacroTransmissionRegistry",
    "MacroTransmissionEngine",
    "build_default_registry",
    "build_default_engine",
    "CorrelationCausationError",
]
