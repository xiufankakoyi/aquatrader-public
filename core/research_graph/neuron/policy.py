"""Neuron policy configuration.

证据权重与代谢阈值集中配置,可被环境全局覆盖或单神经元局部覆盖。
strong_counter_phrases 已移至 GrowthMiningEngine(neuron 只消费 stance,不做文本匹配)。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NeuronPolicyConfig:
    """神经元策略配置(任务第八节证据权重 + 代谢阈值)。"""

    evidence_weight: dict[str, float] = field(default_factory=lambda: {
        # 官方披露与定期报告(高可信,0.95-1.00)
        "official_disclosure": 1.00,
        "annual_report": 0.95,
        "semiannual_report": 0.95,      # 半年报,等同年报
        "quarterly_report": 0.95,       # 季报(一季报/三季报),等同年报
        "periodic_report": 0.95,        # AKShare 兜底泛化标签,等同年报
        "prospectus": 0.95,
        "contract": 0.95,
        "customer_certification": 0.95,
        # 日常公告(官方但非定期,略低于年报)
        "announcement": 0.90,
        # 董秘互动(高价值,但不等同年报/公告的确认强度)
        "company_ir_reply": 0.75,
        "investor_interaction": 0.75,   # 投资者互动(深市互动易/上证 e 互动),等同 company_ir_reply
        # 研究与行业材料
        "research_report": 0.70,
        "industry_report": 0.68,
        "authoritative_industry_material": 0.65,
        # 弱信号
        "news": 0.45,
        "local_structured_seed": 0.50,
        "search_snippet": 0.20,
        "forum_social": 0.15,
    })

    stance_weight: dict[str, float] = field(default_factory=lambda: {
        "support": 1.0,
        "counter": -1.0,
        "neutral": 0.0,
        "retracted": -0.5,
    })

    # 代谢阈值
    revival_threshold: float = 0.3
    # Bug 1 修复:复活迟滞阈值。从 WEAKENING/DORMANT/DECAYING 回升 ACTIVE 时,
    # staleness 必须低于此阈值,避免在 staleness 高位时与 ACTIVE→WEAKENING 反复横跳。
    # 形成迟滞带:staleness ∈ [revival_staleness_threshold, 0.7] 时状态稳定。
    revival_staleness_threshold: float = 0.3
    dormancy_threshold: float = 0.2
    decay_threshold: float = 0.1
    staleness_threshold_days: int = 30

    # 强反证规则:官方公告类 + 定期报告 + 董秘回复可作为强反证来源
    # (修复:evidence_weight 同步补齐 announcement/semiannual_report/quarterly_report,
    #  消除"strong_counter 命中但基础权重走 0.3 fallback"的逻辑断层)
    strong_counter_source_types: set[str] = field(default_factory=lambda: {
        "announcement",
        "annual_report", "semiannual_report", "quarterly_report",
        "company_ir_reply",
    })
    strong_counter_weight_multiplier: float = 3.0

    # nutrition / confidence 计算系数(任务第八节公式)
    nutrition_source_diversity_bonus: float = 0.3
    nutrition_counter_penalty: float = 0.8
    staleness_penalty_max: float = 0.3
