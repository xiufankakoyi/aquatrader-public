"""P3.6 研究资源与预算优化模块。

将计算、检索和人工复核预算投向最可能减少关键不确定性的任务，
而不是新闻最多的主题。本包不修改现有 ``control_plane/budget_manager.py``，
仅按 P3.6 §「目标」与 §「验收」提供新的 ``resource_allocation`` 包。

公开契约：
- ``ResearchTask``：资源分配视角的研究任务，承载不确定性、阻断、
  证据质量、检索成本、历史命中、重要性、截止时间、冷却与失败计数。
- ``BudgetAllocation``：版本化、带指纹的预算分配决策。
- ``VOIResult``：单任务信息价值评估结果。
"""
from __future__ import annotations

from .research_budget import (
    AllocationDecision,
    BudgetAllocation,
    ResearchBudgetAllocator,
    ResearchTask,
)
from .value_of_information import (
    VOIResult,
    ValueOfInformationEstimator,
)

__all__ = [
    "AllocationDecision",
    "BudgetAllocation",
    "ResearchBudgetAllocator",
    "ResearchTask",
    "VOIResult",
    "ValueOfInformationEstimator",
]
