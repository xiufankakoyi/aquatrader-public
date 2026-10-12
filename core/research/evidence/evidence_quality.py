"""证据质量评分器 — 六维评分体系。

评分维度:
1. source_quality: 数据源质量
2. grounding_quality: 原文锚定质量
3. confidence_quality: 置信度质量
4. recency_quality: 时效性质量
5. evidence_text_quality: 证据文本质量
6. cross_validation_quality: 交叉验证质量（v1.2 新增）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 数据源质量权重
_SOURCE_QUALITY_MAP = {
    "announcement": 1.0,
    "report": 0.8,
    "market_data": 0.7,
    "quote_depth": 0.7,
    "kline": 0.7,
    "valuation": 0.7,
    "sector": 0.6,
    "theme": 0.6,
    "catalyst": 0.6,
    "risk": 0.6,
    "moat": 0.6,
    "news": 0.6,
    "extraction": 0.6,
}

# 置信度 → 分数
_CONFIDENCE_SCORE_MAP = {
    "high": 1.0,
    "medium": 0.7,
    "low": 0.4,
}

# 锚定状态 → 分数
_GROUNDING_SCORE_MAP = {
    "exact": 1.0,
    "partial": 0.7,
    "drop": 0.0,
    "": 0.5,  # 未做锚定校验（规则引擎结果）
}

# 交叉验证状态 → 分数
_CROSS_VALIDATION_SCORE_MAP = {
    "cross_validated": 1.0,
    "rule_only": 0.8,
    "llm_only": 0.5,
    "": 0.7,  # 未做交叉验证
}

# 六维权重
_WEIGHTS = {
    "source_quality": 0.20,
    "grounding_quality": 0.20,
    "confidence_quality": 0.15,
    "recency_quality": 0.15,
    "evidence_text_quality": 0.15,
    "cross_validation_quality": 0.15,
}


@dataclass
class QualityScore:
    """证据质量评分结果。

    Attributes:
        total: 加权总分 (0-1)
        source_quality: 数据源质量分
        grounding_quality: 锚定质量分
        confidence_quality: 置信度质量分
        recency_quality: 时效性质量分
        evidence_text_quality: 证据文本质量分
        cross_validation_quality: 交叉验证质量分
        strength: 强度标签 strong/medium/weak
    """

    total: float = 0.0
    source_quality: float = 0.0
    grounding_quality: float = 0.0
    confidence_quality: float = 0.0
    recency_quality: float = 0.0
    evidence_text_quality: float = 0.0
    cross_validation_quality: float = 0.0
    strength: str = "weak"


def score_evidence(
    evidence_type: Any = "",
    confidence: Any = "medium",
    grounding_status: Any = "",
    cross_validation_status: Any = "",
    data_date: Any = "",
    evidence_text: Any = "",
    reference_date: Optional[str] = None,
) -> QualityScore:
    """计算单条证据的六维质量评分。

    Args:
        evidence_type: 证据类型
        confidence: 置信度 high/medium/low
        grounding_status: 锚定状态 exact/partial/drop
        cross_validation_status: 交叉验证状态 cross_validated/rule_only/llm_only
        data_date: 数据日期 (YYYY-MM-DD)
        evidence_text: 证据文本
        reference_date: 参考日期（默认今天）

    Returns:
        QualityScore 评分结果
    """
    # 1. 数据源质量
    source_quality = _SOURCE_QUALITY_MAP.get(_normalize_label(evidence_type), 0.5)

    # 2. 锚定质量
    grounding_quality = _GROUNDING_SCORE_MAP.get(_normalize_label(grounding_status), 0.5)

    # 3. 置信度质量
    confidence_quality = _CONFIDENCE_SCORE_MAP.get(_normalize_label(confidence), 0.5)

    # 4. 时效性质量
    recency_quality = _compute_recency(data_date, reference_date)

    # 5. 证据文本质量
    evidence_text_quality = _compute_text_quality(evidence_text)

    # 6. 交叉验证质量
    cross_validation_quality = _CROSS_VALIDATION_SCORE_MAP.get(
        _normalize_label(cross_validation_status), 0.7
    )

    # 加权总分
    total = (
        source_quality * _WEIGHTS["source_quality"]
        + grounding_quality * _WEIGHTS["grounding_quality"]
        + confidence_quality * _WEIGHTS["confidence_quality"]
        + recency_quality * _WEIGHTS["recency_quality"]
        + evidence_text_quality * _WEIGHTS["evidence_text_quality"]
        + cross_validation_quality * _WEIGHTS["cross_validation_quality"]
    )

    # 强度标签
    if total >= 0.8:
        strength = "strong"
    elif total >= 0.55:
        strength = "medium"
    else:
        strength = "weak"

    return QualityScore(
        total=round(total, 4),
        source_quality=round(source_quality, 4),
        grounding_quality=round(grounding_quality, 4),
        confidence_quality=round(confidence_quality, 4),
        recency_quality=round(recency_quality, 4),
        evidence_text_quality=round(evidence_text_quality, 4),
        cross_validation_quality=round(cross_validation_quality, 4),
        strength=strength,
    )


def _normalize_label(value: Any) -> str:
    """Return a stable lookup key for external labels."""
    return value.strip().lower() if isinstance(value, str) else ""


def _compute_recency(data_date: Any, reference_date: Optional[str] = None) -> float:
    """计算时效性质量分。

    基准日为 data_date（公告/报告发布日期）:
    - ≤7天 1.0
    - ≤30天 0.8
    - ≤90天 0.6
    - >90天 0.4

    Args:
        data_date: 数据日期
        reference_date: 参考日期

    Returns:
        时效性质量分 (0-1)
    """
    if not isinstance(data_date, str) or len(data_date) < 10:
        return 0.5

    try:
        d_date = datetime.strptime(data_date[:10], "%Y-%m-%d")
        ref = (
            datetime.strptime(reference_date[:10], "%Y-%m-%d")
            if isinstance(reference_date, str) and reference_date
            else datetime.now()
        )
        delta_days = (ref - d_date).days

        if delta_days <= 7:
            return 1.0
        elif delta_days <= 30:
            return 0.8
        elif delta_days <= 90:
            return 0.6
        else:
            return 0.4
    except (ValueError, TypeError):
        return 0.5


def _compute_text_quality(evidence_text: Any) -> float:
    """计算证据文本质量分。

    长度合适且无推断 1.0，过短 0.4。

    Args:
        evidence_text: 证据文本

    Returns:
        文本质量分 (0-1)
    """
    if not isinstance(evidence_text, str) or not evidence_text.strip():
        return 0.0

    text = evidence_text.strip()
    # 中文字符数
    cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')

    # 过短
    if cn_chars < 4 and len(text) < 6:
        return 0.4

    # 合适长度
    if cn_chars >= 4 or len(text) >= 10:
        return 1.0

    return 0.7
