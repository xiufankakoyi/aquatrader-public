"""板块轮动经济有效性研究框架。

将旧 ICE/反转/加速规则降级为可验证的 feature/shadow，通过 walk-forward
样本外验证和人工审批后才可进入生产评分。每日 PIT 特征持久化，未来表现
标签只在 evaluation 层生成，不回写事实层。

子模块：
- contracts: PolicyVersion、SectorFeatureRecord、EvaluationLabel 等数据契约
- policy_store: PolicyVersion 配置化存储与查询
- feature_store: 每日 PIT 板块特征 SQLite 持久化
- label_store: 未来表现标签 SQLite 持久化（只在 evaluation 层）
- evaluator: walk-forward + 基准对比评价器
"""
from __future__ import annotations

from .contracts import (
    BASELINE_NAMES,
    EvaluationLabel,
    PolicyVersion,
    SectorFeatureRecord,
)
from .policy_store import PolicyVersionStore, ProductionPolicyAccessor
from .feature_store import SectorFeatureStore
from .label_store import EvaluationLabelStore
from .evaluator import SectorRotationEvaluator

__all__ = [
    "BASELINE_NAMES",
    "EvaluationLabel",
    "PolicyVersion",
    "SectorFeatureRecord",
    "PolicyVersionStore",
    "ProductionPolicyAccessor",
    "SectorFeatureStore",
    "EvaluationLabelStore",
    "SectorRotationEvaluator",
]
