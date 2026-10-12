"""P1.6 管理层、治理和资本配置台账子包。

按 P1 计划 §5 Task P1.6 覆盖 6 类内容：
1. 控股股东、实际控制人、董监高变更 (ShareholderChange / ManagementPerson)
2. 股权激励与考核条件 (EquityIncentive)
3. 关联交易、质押、担保、监管处罚 (RelatedPartyTransaction / RiskEvent)
4. 分红、回购、融资、并购、扩产和资产处置 (CapitalAction)
5. 管理层公开承诺及后续兑现情况 (ManagementCommitment)
6. 资本配置对 ROIC、每股价值和财务风险的影响 (CapitalAllocationImpact)

硬门禁：
- 管理层承诺按"原文—时间—目标—兑现—偏差"保存（5 项齐全）
- 人员评价不能由情绪新闻直接生成（is_from_emotional_news=True 阻断）
- 治理风险与财务预测影响分开表达（potential_financial_impact 仅描述，不参与预测）
"""
from .contracts import (
    CapitalAction,
    CapitalAllocationImpact,
    EquityIncentive,
    GovernanceGap,
    GovernanceLedger,
    GovernanceRisk,
    ManagementCommitment,
    ManagementPerson,
    PersonnelAssessment,
    RelatedPartyTransaction,
    RiskEvent,
    ShareholderChange,
)
from .repository import GovernanceRepository
from .service import GovernanceRequest, GovernanceService

__all__ = [
    "CapitalAction",
    "CapitalAllocationImpact",
    "EquityIncentive",
    "GovernanceGap",
    "GovernanceLedger",
    "GovernanceRepository",
    "GovernanceRequest",
    "GovernanceRisk",
    "GovernanceService",
    "ManagementCommitment",
    "ManagementPerson",
    "PersonnelAssessment",
    "RelatedPartyTransaction",
    "RiskEvent",
    "ShareholderChange",
]
