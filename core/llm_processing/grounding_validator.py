"""Grounding Validator — 校验 LLM 输出证据是否来自原文。

优先使用 LLM 输出的 source_chunk_offset 直接定位原文句子进行比对，
无法定位时回退到全文搜索，相似度低于阈值则丢弃。
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 主观程度副词：若 evidence_text 中出现但原文中不存在，标记为 inferred
_SUBJECTIVE_ADVERBS = [
    "显著", "大幅", "前景广阔", "明显", "极为", "迅猛",
    "爆发式", "惊人", "极其", "超预期",
]


@dataclass
class GroundingResult:
    """单条证据的锚定校验结果。

    Attributes:
        status: 锚定状态 exact/partial/drop
        grounding_type: 锚定类型 text_exact/text_partial/text_missing_drop/taxonomy/structured_field/not_applicable
        original_evidence_text: LLM 原始输出
        grounded_evidence_text: 锚定后的原文片段（partial 时替换为原文）
        source_chunk_offset: 句子位置
        reason: 丢弃/降级原因
    """

    status: str = "drop"  # exact / partial / drop
    grounding_type: str = "text_missing_drop"  # text_exact / text_partial / text_missing_drop / taxonomy / structured_field / not_applicable
    original_evidence_text: str = ""
    grounded_evidence_text: str = ""
    source_chunk_offset: str = ""
    reason: str = ""


def split_into_sentences(text: str) -> List[str]:
    """将文本按句子边界分割。

    Args:
        text: 原始文本

    Returns:
        句子列表
    """
    if not text:
        return []
    # 按中英文句子边界分割，保留分隔符
    parts = re.split(r"(。|！|？|；|\n|\.|!|\?)", text)
    sentences: List[str] = []
    buffer = ""
    for part in parts:
        buffer += part
        if part in ("。", "！", "？", "；", "\n", ".", "!", "?"):
            if buffer.strip():
                sentences.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        sentences.append(buffer.strip())
    return sentences


def _normalize(text: str) -> str:
    """归一化文本：去除空格、标点差异，用于精确比较。

    Args:
        text: 原始文本

    Returns:
        归一化后的文本
    """
    # 去除空格
    result = re.sub(r"\s+", "", text)
    # 统一中英文标点
    replacements = {
        "，": ",", "。": ".", "：": ":", "；": ";",
        "！": "!", "？": "?", """: '"', """: '"',
        "（": "(", "）": ")", "【": "[", "】": "]",
    }
    for cn, en in replacements.items():
        result = result.replace(cn, en)
    return result.lower()


def _locate_by_offset(
    evidence_text: str,
    source_text: str,
    offset_hint: str,
) -> Optional[str]:
    """尝试用 source_chunk_offset 定位原文句子并比对。

    Args:
        evidence_text: LLM 输出的证据文本
        source_text: 原文
        offset_hint: 句子位置提示（如 "句子3" 或 "句子2-3"）

    Returns:
        匹配到的原文片段，未找到返回 None
    """
    sentences = split_into_sentences(source_text)
    if not sentences:
        return None

    # 解析 offset_hint
    match = re.match(r"句子(\d+)(?:-(\d+))?", offset_hint)
    if not match:
        return None

    start_idx = int(match.group(1)) - 1  # 转为 0-based
    end_idx = int(match.group(2)) - 1 if match.group(2) else start_idx

    if start_idx < 0 or start_idx >= len(sentences):
        return None
    end_idx = min(end_idx, len(sentences) - 1)

    # 拼接目标句子
    target_text = "".join(sentences[start_idx:end_idx + 1])
    norm_target = _normalize(target_text)
    norm_evidence = _normalize(evidence_text)

    if norm_evidence in norm_target or norm_target in norm_evidence:
        return target_text

    return None


def _search_in_source(evidence_text: str, source_text: str) -> Optional[str]:
    """在原文中搜索证据文本。

    Args:
        evidence_text: LLM 输出的证据文本
        source_text: 原文

    Returns:
        找到的原文片段，未找到返回 None
    """
    norm_ev = _normalize(evidence_text)
    norm_src = _normalize(source_text)

    # 直接包含
    if norm_ev in norm_src:
        # 在原文中找到对应位置，返回原文片段
        return _extract_original_span(evidence_text, source_text)

    # 滑动窗口搜索
    for window_size in [30, 20, 15, 10]:
        if window_size > len(norm_ev):
            continue
        for offset in range(0, len(norm_ev) - window_size + 1, max(1, window_size // 2)):
            fragment = norm_ev[offset:offset + window_size]
            if fragment in norm_src:
                return _extract_original_span(evidence_text, source_text)

    return None


def _extract_original_span(evidence_text: str, source_text: str) -> str:
    """从原文中提取包含证据文本的片段。

    Args:
        evidence_text: 证据文本
        source_text: 原文

    Returns:
        原文中的对应片段
    """
    # 尝试直接查找
    idx = source_text.find(evidence_text)
    if idx >= 0:
        start = max(0, idx - 20)
        end = min(len(source_text), idx + len(evidence_text) + 20)
        return source_text[start:end].strip()

    # 归一化后查找位置
    norm_ev = _normalize(evidence_text)
    norm_src = _normalize(source_text)
    idx = norm_src.find(norm_ev[:min(20, len(norm_ev))])
    if idx >= 0:
        # 近似映射回原文位置
        start = max(0, idx - 20)
        end = min(len(source_text), idx + len(evidence_text) + 40)
        return source_text[start:end].strip()

    return evidence_text


def _check_inferred_language(evidence_text: str, source_text: str) -> bool:
    """检测 evidence_text 中是否包含推断性语言。

    若主观程度副词在 evidence_text 中出现但原文中不存在，标记为推断。

    Args:
        evidence_text: 证据文本
        source_text: 原文

    Returns:
        True 表示检测到推断性语言
    """
    for adv in _SUBJECTIVE_ADVERBS:
        if adv in evidence_text and adv not in source_text:
            return True
    return False


def validate_evidence_item(
    item: Dict,
    source_text: str,
) -> GroundingResult:
    """校验单条证据是否锚定在原文中。

    流程:
    1. 优先用 source_chunk_offset 精确定位
    2. 精确定位失败则全文搜索
    3. 搜索失败则用相似度比对
    4. 中文字符数 < 4 直接丢弃
    5. 检测推断性语言

    Args:
        item: 证据条目字典
        source_text: 原文

    Returns:
        GroundingResult 锚定校验结果
    """
    evidence_text = item.get("evidence_text", "")
    offset_hint = item.get("source_chunk_offset", "")

    result = GroundingResult(
        original_evidence_text=evidence_text,
        source_chunk_offset=offset_hint,
    )

    # 空证据直接丢弃
    if not evidence_text or not evidence_text.strip():
        result.status = "drop"
        result.grounding_type = "text_missing_drop"
        result.reason = "evidence_text 为空"
        return result

    # 中文字符数 < 4 且总长度 < 6 直接丢弃
    cn_chars = sum(1 for c in evidence_text if '\u4e00' <= c <= '\u9fff')
    if cn_chars < 4 and len(evidence_text.strip()) < 6:
        result.status = "drop"
        result.grounding_type = "text_missing_drop"
        result.reason = "证据文本过短"
        return result

    # 1. 优先用 source_chunk_offset 精确定位
    if offset_hint:
        located = _locate_by_offset(evidence_text, source_text, offset_hint)
        if located:
            norm_ev = _normalize(evidence_text)
            norm_located = _normalize(located)
            if norm_ev in norm_located or norm_located in norm_ev:
                result.status = "exact"
                result.grounding_type = "text_exact"
                result.grounded_evidence_text = located
                return result

    # 2. 全文搜索
    found = _search_in_source(evidence_text, source_text)
    if found:
        result.status = "partial"
        result.grounding_type = "text_partial"
        result.grounded_evidence_text = found
        # 检测推断性语言
        if _check_inferred_language(evidence_text, source_text):
            result.reason = "检测到推断性语言，降级为 partial"
        return result

    # 3. 相似度比对
    norm_ev = _normalize(evidence_text)
    # 在原文的句子中寻找最相似的
    sentences = split_into_sentences(source_text)
    best_ratio = 0.0
    best_sentence = ""
    for sent in sentences:
        ratio = difflib.SequenceMatcher(None, norm_ev, _normalize(sent)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_sentence = sent

    if best_ratio >= 0.75:
        result.status = "partial"
        result.grounding_type = "text_partial"
        result.grounded_evidence_text = best_sentence
        result.reason = f"相似度 {best_ratio:.2f}，替换为原文片段"
        return result

    # 4. 完全找不到
    result.status = "drop"
    result.grounding_type = "text_missing_drop"
    result.reason = f"相似度 {best_ratio:.2f} < 0.75，无法锚定"
    return result


def validate_evidence_items(
    items: List[Dict],
    source_text: str,
) -> Tuple[List[Dict], List[GroundingResult]]:
    """批量校验证据条目。

    Args:
        items: 证据条目列表
        source_text: 原文

    Returns:
        (通过的证据列表, 全部校验结果列表)
        通过的证据列表中 partial 的已替换为原文片段
    """
    validated: List[Dict] = []
    results: List[GroundingResult] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        gr = validate_evidence_item(item, source_text)
        results.append(gr)

        if gr.status == "drop":
            logger.debug("证据被丢弃: %s", gr.reason)
            continue

        # 构建通过的证据条目
        passed = dict(item)
        if gr.status == "partial" and gr.grounded_evidence_text:
            passed["evidence_text"] = gr.grounded_evidence_text
        passed["grounding_status"] = gr.status
        passed["grounding_type"] = gr.grounding_type
        validated.append(passed)

    return validated, results
