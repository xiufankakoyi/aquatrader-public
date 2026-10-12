"""Prompt 模板 — 防编造强化版 LLM 抽取 Prompt。

所有输出必须直接来自原文，不允许改写、总结、推断、评价。
空输出是专业且正确的表现。
"""

from __future__ import annotations

from typing import Dict

# Prompt 版本号，变更后缓存自动失效
PROMPT_VERSION = "v1.2"

# 合规禁用词：命中则整条丢弃
# 注意: "增持""减持"是券商评级，出现在 evidence_text 中是正常的摘录行为，
# 不应被拦截。合规扫描只检查 summary 和顶层字段，不检查 evidence_items。
COMPLIANCE_BLOCKED_PHRASES = [
    "建议买入", "建议卖出", "推荐买入", "推荐卖出", "强烈推荐",
    "投资建议", "应该买入", "应该卖出", "建议加仓", "建议减仓",
    "建议建仓", "建议清仓", "建议止损", "建议止盈",
]

# 仅在 summary 等顶层字段中扫描的禁用词（含券商评级词）
COMPLIANCE_SUMMARY_BLOCKED_WORDS = [
    "买入", "卖出", "目标价", "强烈推荐", "增持", "减持",
    "加仓", "减仓", "清仓", "建仓", "止损", "止盈",
    "投资建议", "推荐买入", "推荐卖出",
]

_SYSTEM_PROMPT = """你是一个严格的信息抽取器，仅负责从 A 股公告、研报、板块/题材文本中提取结构化事实。
你的所有输出都必须直接来自原文，不允许任何改写、总结、推断、评价。
如果要求你输出风险、催化剂等，但原文没有对应证据，你必须返回空数组。
空输出是专业且正确的表现，绝不会被视为失败。
严禁输出买卖建议、目标价、投资结论。"""

_TASK_INSTRUCTION = """只输出 JSON，不要 Markdown，不要解释。
每条 evidence_text 必须是原文中一段连续的、逐字复制的文本，不许缩写或改写。
同时输出 source_chunk_offset 标明该证据在 chunk 内的句子位置（如 "句子1"、"句子2-3"）。
如果证据是综合多句得到的，必须拆成多条，每条对应一个原文片段。
不确定时 confidence=low，但 evidence_text 仍必须是原文。
不要为了填空而编造，无证据则返回空数组 []。"""

_FEW_SHOT = """
【正例】
原文：2024年H1公司获得宁德时代5亿元订单，用于动力电池结构件。
正确输出：{ "evidence_text": "获得宁德时代5亿元订单", "source_chunk_offset": "句子1" }

【负例（会被系统丢弃）】
原文：2024年H1公司获得宁德时代5亿元订单，用于动力电池结构件。
错误输出：{ "evidence_text": "公司获得了重要客户大额订单" } ← 这是归纳，不允许！"""

_OUTPUT_SCHEMA = """输出 JSON Schema:
{
  "summary": "提取摘要（中性陈述，无推断）",
  "themes": ["匹配到的主题类别"],
  "risks": ["匹配到的风险关键词"],
  "catalysts": ["匹配到的催化剂关键词"],
  "moat_claims": ["匹配到的护城河关键词"],
  "financial_signals": ["匹配到的财务信号类别"],
  "industry_chain_relations": ["产业链关系描述"],
  "evidence_items": [
    {
      "category": "theme/risk/catalyst/moat/financial_signal/industry_relation",
      "label": "标签",
      "evidence_text": "原文证据片段（必须逐字复制）",
      "source_chunk_offset": "句子N 或 句子N-M",
      "confidence": "high/medium/low"
    }
  ],
  "extracted_relations": [
    {
      "from_entity": "实体A",
      "relation_type": "关系类型",
      "to_entity": "实体B",
      "evidence_text": "支持该关系的原文",
      "source_chunk_offset": "句子N",
      "confidence": "high/medium/low"
    }
  ],
  "sentiment": "positive/neutral/negative",
  "confidence": "high/medium/low"
}

主题类别: AI, 算力, 存储, 光模块, 电力, 机器人, 半导体, 低空经济, 医药, 消费, 数据中心, 消费电子, 汽车电子, 军工
风险关键词: 减持, 亏损, 诉讼, 处罚, 立案, 监管函, 退市风险, ST, 商誉减值, 毛利率下降, 现金流压力, 质押, 冻结, 爆雷
催化剂关键词: 中标, 合同, 订单, 产能, 扩产, 涨价, 并购, 回购, 增持, 业绩预增, 新产品, 量产, 突破, 签约, 合作
护城河关键词: 独家, 龙头, 领先, 高壁垒, 认证, 专利, 客户粘性, 长协, 稀缺, 技术优势, 国产替代, 垄断, 寡头, 核心供应商
财务信号类别: revenue_growth, profit_growth, margin_improve, cash_flow_positive"""


def build_extraction_prompt(chunk_text: str, metadata: Dict[str, str]) -> str:
    """构建 LLM 抽取 Prompt。

    Args:
        chunk_text: 待抽取的文本块
        metadata: 元数据字典，含 symbol/stock_name/provider/event_type/title 等

    Returns:
        完整的用户提示词
    """
    parts = []

    # 元数据
    if metadata.get("symbol"):
        parts.append(f"股票代码: {metadata['symbol']}")
    if metadata.get("stock_name"):
        parts.append(f"股票名称: {metadata['stock_name']}")
    if metadata.get("title"):
        parts.append(f"标题: {metadata['title']}")
    if metadata.get("chunk_id") is not None:
        parts.append(f"块编号: {metadata['chunk_id']}")

    parts.append(f"\n正文:\n{chunk_text}")

    return "\n".join(parts)


def get_system_prompt() -> str:
    """获取完整的系统提示词。

    Returns:
        系统提示词字符串
    """
    return "\n\n".join([
        _SYSTEM_PROMPT,
        _TASK_INSTRUCTION,
        _FEW_SHOT,
        _OUTPUT_SCHEMA,
    ])


def check_compliance(text: str) -> bool:
    """合规扫描：检测文本中是否包含交易建议禁用词。

    对整段文本检查建议性短语（如"建议买入"），
    对 summary 等顶层字段额外检查券商评级词（如"增持"）。

    Args:
        text: 待检测文本

    Returns:
        True 表示合规（无禁用词），False 表示不合规
    """
    # 检查建议性短语（全文）
    for phrase in COMPLIANCE_BLOCKED_PHRASES:
        if phrase in text:
            return False
    return True


def check_compliance_for_summary(text: str) -> bool:
    """合规扫描（严格版）：用于 summary 等顶层字段。

    除建议性短语外，还检查券商评级词（增持/减持等），
    因为 summary 不应包含任何交易相关术语。

    Args:
        text: 待检测文本（通常是 summary 字段）

    Returns:
        True 表示合规，False 表示不合规
    """
    if not check_compliance(text):
        return False
    for word in COMPLIANCE_SUMMARY_BLOCKED_WORDS:
        if word in text:
            return False
    return True


# =============================================================================
# AquaResearch P1 Prompt — 研报因果链抽取（claim + causal_chain）
# =============================================================================

# P1 Prompt 版本号，变更后缓存自动失效
P1_PROMPT_VERSION = "v2.0-aquaresearch-p1"

_P1_SYSTEM_PROMPT = """你是 AquaResearch 投研流水线的研报因果链抽取器。
你的任务是从 A 股公告、研报、新闻文本中抽取结构化观点（claim）和因果链（causal_chain）。
所有输出必须直接来自原文，不允许改写、总结、推断、评价。
如果原文没有明确的观点或因果关系，返回空数组。空输出是专业且正确的表现。
严禁输出买卖建议、目标价、投资结论。"""

_P1_TASK_INSTRUCTION = """只输出 JSON，不要 Markdown，不要解释。
每个 claim 的 evidence_quote 必须是原文中一段连续的、逐字复制的文本。
每个 causal_chain 的 evidence_quote 必须是原文中一段连续的、逐字复制的文本。
confidence 只能是 low 或 medium，禁止 high。
- 文本包含"公告、披露、已、确认、签订、客户认证"时 confidence = medium
- 文本包含"预计、有望、可能、建议关注"时 confidence = low
无法完整抽取 cause/mechanism/effect 时，对应字段写"未明确"，禁止编造。
无证据则返回空数组 []。"""

_P1_OUTPUT_SCHEMA = """输出 JSON Schema:
{
  "claims": [
    {
      "claim_text": "观点文本（来自原文的陈述）",
      "mentioned_terms": ["提及的术语，如 GB200/PCB/PTFE"],
      "mentioned_companies": ["提及的公司"],
      "signal_words": ["信号词，如 带动/推动/受益/利好/有望/预计/可能/建议关注/催化/验证/导入/扩产/涨价/国产替代/对日替代"],
      "evidence_quote": "原文证据片段（必须逐字复制）",
      "confidence": "low 或 medium"
    }
  ],
  "causal_chains": [
    {
      "claim_index": 0,
      "cause": "原因节点（如 GB200 放量）",
      "mechanism": "作用机制（如 带动/推动/提升）",
      "effect": "效果节点（如 高速 PCB 需求提升）",
      "beneficiary_node": "受益节点（如 高端铜箔）",
      "evidence_quote": "原文证据片段（必须逐字复制）",
      "confidence": "low 或 medium"
    }
  ]
}"""


def build_p1_extraction_prompt(chunk_text: str, metadata: Dict[str, str]) -> str:
    """构建 P1 研报因果链抽取 Prompt。

    Args:
        chunk_text: 待抽取的文本块
        metadata: 元数据字典，含 title/source_type/published_at 等

    Returns:
        完整的用户提示词
    """
    parts = []
    if metadata.get("title"):
        parts.append(f"标题: {metadata['title']}")
    if metadata.get("source_type"):
        parts.append(f"来源类型: {metadata['source_type']}")
    if metadata.get("document_id"):
        parts.append(f"文档ID: {metadata['document_id']}")
    parts.append(f"\n正文:\n{chunk_text}")
    return "\n".join(parts)


def get_p1_system_prompt() -> str:
    """获取 P1 完整的系统提示词。

    Returns:
        P1 系统提示词字符串
    """
    return "\n\n".join([
        _P1_SYSTEM_PROMPT,
        _P1_TASK_INSTRUCTION,
        _P1_OUTPUT_SCHEMA,
    ])


# =============================================================================
# AquaResearch P4A Prompt — 多智能体假设生成与反方审查
# =============================================================================

# P4A Prompt 版本号，变更后缓存自动失效
P4A_PROMPT_VERSION = "v2.0-aquaresearch-p4a"

_P4A_HYPOTHESIS_SYSTEM_PROMPT = """你是 AquaResearch 投研流水线的研究假设生成器。
你的任务是基于候选概念、因果链、产业链图谱和缺失边，生成可证伪的研究假设。
假设必须是可验证的、可证伪的，且基于已有证据。
严禁输出买卖建议、目标价、投资结论。假设状态只能是 born/pending/weak_signal/rejected/expired。"""

_P4A_HYPOTHESIS_TASK_INSTRUCTION = """只输出 JSON，不要 Markdown，不要解释。
每个假设的 hypothesis_text 必须是完整的陈述句，包含原因和预期效果。
evidence_quotes 必须来自原文，逐字复制。
novelty_score/evidence_score/crowding_score 范围 0-1。
status 只能是 born 或 pending。
无证据则返回空数组 []。"""

_P4A_HYPOTHESIS_SCHEMA = """输出 JSON Schema:
{
  "hypotheses": [
    {
      "hypothesis_text": "假设陈述（如：GB200 升级可能带动高速 PCB 需求）",
      "hypothesis_type": "product_platform_demand / tech_route_impact / investment_logic_signal",
      "source_nodes": ["GB200", "PCB"],
      "logic_chain": "GB200 放量 → PCB 需求提升",
      "evidence_quotes": ["原文证据片段"],
      "novelty_score": 0.0,
      "evidence_score": 0.0,
      "crowding_score": 0.0,
      "status": "born"
    }
  ]
}"""

_P4A_SKEPTIC_SYSTEM_PROMPT = """你是 AquaResearch 投研流水线的反方审查员（Skeptic）。
你的任务是对研究假设进行对抗式审查，指出其潜在风险和弱点。
必须攻击六类风险：单一来源风险、只有共现没有实质关系、没有公告官网客户认证、
市场可能已经交易、技术路线可能被替代、公司映射不明确。
严禁输出买卖建议、目标价、投资结论。"""

_P4A_SKEPTIC_TASK_INSTRUCTION = """只输出 JSON，不要 Markdown，不要解释。
每个审查的 challenge_text 必须具体、可操作。
severity 只能是 high/medium/low。
suggested_validation_task 必须是可执行的搜索任务描述。
无风险则返回空数组 []。"""

_P4A_SKEPTIC_SCHEMA = """输出 JSON Schema:
{
  "reviews": [
    {
      "hypothesis_id": "假设ID",
      "challenge_type": "single_source_risk / weak_relation / lack_official_evidence / crowding / tech_substitution / missing_company_mapping",
      "challenge_text": "具体风险描述",
      "severity": "high / medium / low",
      "suggested_validation_task": "建议的验证搜索任务"
    }
  ]
}"""

_P4A_PORTFOLIO_SYSTEM_PROMPT = """你是 AquaResearch 投研流水线的组合观察翻译器。
你的任务是把研究假设翻译成观察语言，而不是交易指令。
只能输出 ignore / monitor / research_required / watchlist_candidate 四类 watchlist_action。
严禁输出买卖建议、目标价、仓位、止盈止损或投资结论。"""

_P4A_PORTFOLIO_TASK_INSTRUCTION = """只输出 JSON，不要 Markdown，不要解释。
trade_meaning 必须是中性观察含义，不能包含买入、卖出、强烈推荐。
trigger_conditions / risk_notes / next_research_tasks 必须是研究动作或证据条件。
如果证据不足，watchlist_action 使用 research_required 或 ignore。"""

_P4A_PORTFOLIO_SCHEMA = """输出 JSON Schema:
{
  "portfolio_translation": {
    "watchlist_action": "ignore / monitor / research_required / watchlist_candidate",
    "trade_meaning": "中性观察含义",
    "trigger_conditions": "需要观察的证据条件",
    "risk_notes": "风险说明",
    "next_research_tasks": "后续研究任务"
  }
}"""


def build_p4a_hypothesis_prompt(
    candidate_concepts: str,
    causal_chains: str,
    missing_edges: str,
) -> str:
    """构建 P4A 假设生成 Prompt。

    Args:
        candidate_concepts: 候选概念 JSON 字符串
        causal_chains: 因果链 JSON 字符串
        missing_edges: 缺失边 JSON 字符串

    Returns:
        完整的用户提示词
    """
    return f"""基于以下输入生成研究假设：

候选概念:
{candidate_concepts}

因果链:
{causal_chains}

缺失边:
{missing_edges}
"""


def get_p4a_hypothesis_system_prompt() -> str:
    """获取 P4A 假设生成的系统提示词。"""
    return "\n\n".join([
        _P4A_HYPOTHESIS_SYSTEM_PROMPT,
        _P4A_HYPOTHESIS_TASK_INSTRUCTION,
        _P4A_HYPOTHESIS_SCHEMA,
    ])


def build_p4a_skeptic_prompt(
    hypothesis_text: str,
    evidence_quotes: str,
) -> str:
    """构建 P4A 反方审查 Prompt。

    Args:
        hypothesis_text: 假设文本
        evidence_quotes: 证据引用 JSON 字符串

    Returns:
        完整的用户提示词
    """
    return f"""对以下假设进行反方审查：

假设:
{hypothesis_text}

证据:
{evidence_quotes}
"""


def get_p4a_skeptic_system_prompt() -> str:
    """获取 P4A 反方审查的系统提示词。"""
    return "\n\n".join([
        _P4A_SKEPTIC_SYSTEM_PROMPT,
        _P4A_SKEPTIC_TASK_INSTRUCTION,
        _P4A_SKEPTIC_SCHEMA,
    ])


def build_p4a_portfolio_prompt(
    hypothesis_text: str,
    skepticism_score: str,
    evidence_score: str,
    novelty_score: str,
) -> str:
    """构建 P4A 组合观察翻译 Prompt。"""
    return f"""将以下研究假设翻译成组合观察语言：

假设:
{hypothesis_text}

skepticism_score:
{skepticism_score}

evidence_score:
{evidence_score}

novelty_score:
{novelty_score}
"""


def get_p4a_portfolio_system_prompt() -> str:
    """获取 P4A 组合观察翻译的系统提示词。"""
    return "\n\n".join([
        _P4A_PORTFOLIO_SYSTEM_PROMPT,
        _P4A_PORTFOLIO_TASK_INSTRUCTION,
        _P4A_PORTFOLIO_SCHEMA,
    ])


# =============================================================================
# AquaResearch P4B Prompt — 证据演化与市场共振（belief_state 评估辅助）
# =============================================================================

# P4B Prompt 版本号，变更后缓存自动失效
P4B_PROMPT_VERSION = "v2.0-aquaresearch-p4b"

_P4B_BELIEF_SYSTEM_PROMPT = """你是 AquaResearch 投研流水线的证据强度评估器。
你的任务是评估研究假设的证据强度（evidence_strength）和证据速度（evidence_velocity）。
evidence_strength 表示当前证据的总体强度（0-1）。
evidence_velocity 表示证据增长速度（0-1）。
严禁输出买卖建议、目标价、投资结论。"""

_P4B_BELIEF_TASK_INSTRUCTION = """只输出 JSON，不要 Markdown，不要解释。
evidence_strength 和 evidence_velocity 范围 0-1。
基于证据数量、来源多样性、时效性进行评估。
无证据则返回 evidence_strength=0, evidence_velocity=0。"""

_P4B_BELIEF_SCHEMA = """输出 JSON Schema:
{
  "evidence_strength": 0.0,
  "evidence_velocity": 0.0,
  "reason": "评估原因"
}"""


def build_p4b_belief_prompt(
    hypothesis_text: str,
    evidence_timeline: str,
) -> str:
    """构建 P4B belief_state 评估 Prompt。

    Args:
        hypothesis_text: 假设文本
        evidence_timeline: 证据时间线 JSON 字符串

    Returns:
        完整的用户提示词
    """
    return f"""评估以下假设的证据强度和速度：

假设:
{hypothesis_text}

证据时间线:
{evidence_timeline}
"""


def get_p4b_belief_system_prompt() -> str:
    """获取 P4B belief_state 评估的系统提示词。"""
    return "\n\n".join([
        _P4B_BELIEF_SYSTEM_PROMPT,
        _P4B_BELIEF_TASK_INSTRUCTION,
        _P4B_BELIEF_SCHEMA,
    ])
