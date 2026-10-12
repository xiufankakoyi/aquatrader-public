"""研究观点(Opinion)模块。

把分散的 evidence、置信度、风险、反证、触发条件绑定为"当前研究观点"对象,
并提供置信度合成与不确定性拆解能力。
"""
from __future__ import annotations

from core.research.opinion.models import (
    ResearchOpinion,
    RiskFactor,
    UncertaintyBreakdown,
)
from core.research.opinion.synthesizer import OpinionConfidenceSynthesizer

__all__ = [
    "ResearchOpinion",
    "RiskFactor",
    "UncertaintyBreakdown",
    "OpinionConfidenceSynthesizer",
]
