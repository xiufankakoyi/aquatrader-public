"""P1.5 产业经济与竞争格局子包。

按 P1 计划 §5 Task P1.5 至少覆盖 10 类内容：
1. TAM/SAM/SOM 口径 (MarketSizing)
2. 需求驱动和渗透率 (DemandDriver)
3. 供给、产能和扩产周期 (SupplyProfile)
4. 价格与成本曲线 (PriceCostCurve)
5. 库存和交付周期 (InventoryCycle)
6. 行业集中度、主要参与者和份额来源 (CompetitiveLandscape)
7. 技术路线、替代关系和认证壁垒 (TechnologyRoute)
8. 上下游议价能力 (BargainingPower)
9. 资本开支和自由现金流周期 (CapexCycle)
10. 赛道生命周期和失效条件 (LifecycleStage)

硬门禁：
- member_of_theme 不进入市场份额、产品和竞争结论（CompetitionEngine 过滤）
- 赛道阶段必须有时间和证据（LifecycleStage 缺一即 gap）
- 新闻热度不能单独证明需求兑现（DemandDriver.heat_only=True 降级为 warning）
- 公司映射证据为空显示"暂无本地证据"（CompanyMapping.has_local_evidence=False 返回常量）
"""
from .contracts import (
    BargainingPower,
    CapexCycle,
    CompanyMapping,
    CompetitiveLandscape,
    DemandDriver,
    IndustryGap,
    IndustryProfile,
    InventoryCycle,
    LifecycleStage,
    MarketSizing,
    NO_LOCAL_EVIDENCE,
    Participant,
    PriceCostCurve,
    SupplyProfile,
    TechnologyRoute,
)
from .competition_engine import CompetitionEngine
from .economics_engine import EconomicsEngine
from .repository import IndustryRepository

__all__ = [
    "BargainingPower",
    "CapexCycle",
    "CompanyMapping",
    "CompetitiveLandscape",
    "CompetitionEngine",
    "DemandDriver",
    "EconomicsEngine",
    "IndustryGap",
    "IndustryProfile",
    "IndustryRepository",
    "InventoryCycle",
    "LifecycleStage",
    "MarketSizing",
    "NO_LOCAL_EVIDENCE",
    "Participant",
    "PriceCostCurve",
    "SupplyProfile",
    "TechnologyRoute",
]
