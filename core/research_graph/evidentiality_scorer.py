"""证据性评分器(Step 6)。

把"语义相似"提升为"证据相关":对 HybridRetriever 召回的 RetrievedChunk
按是否真正支持/反驳当前主张做二次打分。

设计(KISS,纯规则):
- 实体命中:chunk 是否包含 hypothesis 中的关键实体。
- 立场 cue:chunk 文本是否含支持/反驳 cue 词。
- 时间窗口:publish_date 与 event_date 越近越可信。
- 引语窗口:chunk 是否含数字/百分比等量化证据。

输出 EvidentialityScore,HybridRetriever 用其重排结果。
不引入新依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# 支持 cue 词:出现时倾向"支持"。
SUPPORT_CUES = (
    "支持", "证实", "验证", "印证", "确认", "证据表明", "数据显示", "同比增长",
    "环比增长", "提升", "扩张", "放量", "突破", "符合预期", "超预期",
    "supports", "confirmed", "evidence shows", "data indicates",
)

# 反驳 cue 词:出现时倾向"反驳"。
COUNTER_CUES = (
    "反驳", "证伪", "否定", "否认", "质疑", "失败", "下滑", "收缩",
    "不及预期", "亏损", "违约", "停工", "停产", "contradicts", "denied",
    "disputed", "failed", "declined",
)

# 量化证据正则:百分比、金额、倍数。
_QUANT_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*亿|\d+(?:\.\d+)?\s*万|\d+(?:\.\d+)?\s*倍)"
)


@dataclass
class EvidentialityScore:
    """证据性评分结果。"""

    total: float
    entity_hit: float
    stance_alignment: float
    temporal_proximity: float
    quantification: float
    stance_label: str  # support / counter / neutral
    reasons: list[str]


def _extract_entities(hypothesis: str) -> list[str]:
    """从假设文本中抽取关键实体(简单分词)。

    策略:按标点切分,保留长度 >= 2 的片段。
    """
    if not hypothesis:
        return []
    parts = re.split(r"[,，。;；、\s]+", hypothesis)
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def _parse_date(text: str) -> str | None:
    """从文本中解析首个日期(YYYY-MM-DD)。"""
    if not text:
        return None
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if not m:
        return None
    try:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    except ValueError:
        return None


def _temporal_closeness(chunk_date: str | None, event_date: str | None) -> float:
    """计算 chunk 发布日期与事件日期的接近度,0~1。

    同日=1.0,30 日内线性衰减,超过 30 日=0。
    """
    if not chunk_date or not event_date:
        return 0.0
    try:
        d1 = datetime.strptime(chunk_date[:10], "%Y-%m-%d")
        d2 = datetime.strptime(event_date[:10], "%Y-%m-%d")
    except ValueError:
        return 0.0
    delta_days = abs((d1 - d2).days)
    if delta_days == 0:
        return 1.0
    if delta_days > 30:
        return 0.0
    return 1.0 - delta_days / 30.0


def score_chunk(
    chunk: Any,
    hypothesis: str,
    *,
    stance: str = "support",
    event_date: str = "",
) -> EvidentialityScore:
    """对单个 RetrievedChunk 做证据性打分。

    Args:
        chunk: RetrievedChunk 实例(需有 text/content、publish_date 等字段)。
        hypothesis: 当前主张文本,用于实体匹配。
        stance: 期望立场,support/counter。
        event_date: 事件日期,用于时间窗口打分。

    Returns:
        EvidentialityScore,total 越高越相关。
    """
    text = str(getattr(chunk, "text", "") or getattr(chunk, "content", "") or "")
    reasons: list[str] = []

    # 1. 实体命中:假设中的实体在 chunk 出现比例。
    entities = _extract_entities(hypothesis)
    if entities:
        hits = sum(1 for e in entities if e in text)
        entity_hit = hits / len(entities)
        if entity_hit > 0:
            reasons.append(f"entity_hit={hits}/{len(entities)}")
    else:
        entity_hit = 0.0

    # 2. 立场对齐:统计 support/counter cue 出现次数。
    support_count = sum(1 for cue in SUPPORT_CUES if cue in text)
    counter_count = sum(1 for cue in COUNTER_CUES if cue in text)
    if stance == "counter":
        stance_alignment = min(counter_count / 2.0, 1.0) - min(support_count / 4.0, 0.5)
        stance_label = "counter" if counter_count > support_count else "neutral"
    else:
        stance_alignment = min(support_count / 2.0, 1.0) - min(counter_count / 4.0, 0.5)
        stance_label = "support" if support_count > counter_count else "neutral"
    stance_alignment = max(0.0, stance_alignment)
    if stance_alignment > 0:
        reasons.append(f"stance={stance_label}(s={support_count},c={counter_count})")

    # 3. 时间接近度。
    chunk_date = (
        getattr(chunk, "publish_date", None)
        or (getattr(chunk, "metadata", {}) or {}).get("publish_date")
        or _parse_date(text)
    )
    temporal_proximity = _temporal_closeness(chunk_date, event_date)
    if temporal_proximity > 0:
        reasons.append(f"temporal={temporal_proximity:.2f}")

    # 4. 量化证据:数字/百分比/金额提升可信度。
    quants = _QUANT_PATTERN.findall(text)
    quantification = min(len(quants) / 3.0, 1.0)
    if quantification > 0:
        reasons.append(f"quant={len(quants)}")

    # 加权汇总:实体命中权重最高,其次立场、时间、量化。
    total = (
        0.40 * entity_hit
        + 0.30 * stance_alignment
        + 0.15 * temporal_proximity
        + 0.15 * quantification
    )
    return EvidentialityScore(
        total=round(total, 4),
        entity_hit=round(entity_hit, 4),
        stance_alignment=round(stance_alignment, 4),
        temporal_proximity=round(temporal_proximity, 4),
        quantification=round(quantification, 4),
        stance_label=stance_label,
        reasons=reasons,
    )


def rerank_chunks(
    chunks: list[Any],
    hypothesis: str,
    *,
    stance: str = "support",
    event_date: str = "",
    min_total: float = 0.0,
) -> list[tuple[Any, EvidentialityScore]]:
    """对 chunk 列表做证据性重排。

    Args:
        chunks: RetrievedChunk 列表。
        hypothesis: 当前主张。
        stance: 期望立场。
        event_date: 事件日期。
        min_total: 最低总分阈值,低于则过滤。

    Returns:
        (chunk, score) 列表,按 total 降序。
    """
    scored = [
        (chunk, score_chunk(chunk, hypothesis, stance=stance, event_date=event_date))
        for chunk in chunks
    ]
    scored = [item for item in scored if item[1].total >= min_total]
    scored.sort(key=lambda x: x[1].total, reverse=True)
    return scored
