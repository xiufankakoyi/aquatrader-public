"""P2 新词雷达与概念发现增强。

在 P0 词表匹配基础上，增加：
1. 后缀模式约束的统计新词发现（防止垃圾词爆炸）
2. 最长匹配优先规则（避免 铜/铜箔/高端铜箔 同时污染）
3. 单字材料词降权（铜/钼/锗/钕 仅在与价格/资源/矿等共现时保留）
4. 突增评分 / 共现评分 / 概念评分 / 别名分组

输入：raw_documents_latest.json + raw_terms_latest.json + candidate_concepts_latest.json
输出：更新 raw_terms_latest.json / candidate_concepts_latest.json（补齐五字段）
      + concept_scores_latest.json + concept_aliases_latest.json
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from itertools import combinations
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .concept_taxonomy import ConceptTaxonomy

logger = logging.getLogger(__name__)

# ============================================================================
# 后缀模式定义（防止无约束 substring 枚举产生垃圾词）
# ============================================================================

# 材料后缀：XX树脂 / XX铜箔 / XX电子布 / XX玻纤布 / XX基材 / XX材料 / XX衬底
_MATERIAL_SUFFIXES: Tuple[str, ...] = (
    "树脂", "铜箔", "电子布", "玻纤布", "基材", "材料", "衬底",
)

# 技术路线后缀：XX互联 / XX封装 / XX交换 / XX网络 / XX光学 / XX连接
_TECH_ROUTE_SUFFIXES: Tuple[str, ...] = (
    "互联", "封装", "交换", "网络", "光学", "连接",
)

# 投资逻辑后缀：XX替代 / XX认证 / XX导入 / XX涨价 / XX扩产
_INVEST_LOGIC_SUFFIXES: Tuple[str, ...] = (
    "替代", "认证", "导入", "涨价", "扩产",
)

_ALL_SUFFIXES: Tuple[str, ...] = _MATERIAL_SUFFIXES + _TECH_ROUTE_SUFFIXES + _INVEST_LOGIC_SUFFIXES

# 后缀到 term_type 的映射
_SUFFIX_TO_TERM_TYPE: Dict[str, str] = {}
for suffix in _MATERIAL_SUFFIXES:
    _SUFFIX_TO_TERM_TYPE[suffix] = "material"
for suffix in _TECH_ROUTE_SUFFIXES:
    _SUFFIX_TO_TERM_TYPE[suffix] = "technology_route"
for suffix in _INVEST_LOGIC_SUFFIXES:
    _SUFFIX_TO_TERM_TYPE[suffix] = "investment_logic"

# 后缀模式正则：前缀 1-6 个中文/英文/数字字符 + 后缀
# 按后缀长度降序排列，确保 "玻纤布" 优先于 "布"（虽然这里没有单字后缀）
_SUFFIX_PATTERN = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{1,6})(" + "|".join(_ALL_SUFFIXES) + r")"
)

# ============================================================================
# 单字材料词降权规则
# ============================================================================

# 单字材料词集合（容易污染 top raw_terms 的短词）
_SINGLE_CHAR_MATERIALS: Set[str] = {
    "铜", "钼", "锗", "钕", "硅", "铟", "铂", "钯", "锂", "钴", "镍", "锰",
    "铝", "钛", "钨", "锌", "锡", "银", "金", "铁",
}

# 单字材料词保留的共现关键词（必须附近出现这些词才保留）
_MATERIAL_KEEP_KEYWORDS: Tuple[str, ...] = (
    "价格", "资源", "矿", "供给", "涨价", "出口管制", "材料",
)

# ============================================================================
# 别名分组规则
# ============================================================================

# 光互连家族：CPO / LPO / NPO / MPO 共现
_OPTICAL_INTERCONNECT_FAMILY: Set[str] = {"CPO", "LPO", "NPO", "MPO"}

# 高频高速材料家族：PTFE / 低介电 / 低损耗 / 高频高速 共现
_HIGH_FREQ_MATERIAL_KEYWORDS: Tuple[str, ...] = ("PTFE", "低介电", "低损耗", "高频高速")

# 铜箔家族：高端铜箔 / 铜箔 / HVLP铜箔 共现
_COPPER_FOIL_KEYWORDS: Tuple[str, ...] = ("高端铜箔", "铜箔", "HVLP")

# 磷化铟家族：磷化铟 / InP 共现
_INP_KEYWORDS: Tuple[str, ...] = ("磷化铟", "InP")

# ============================================================================
# 停用词 / 过滤词
# ============================================================================

# 统计新词的停用词前缀（避免 "的树脂" "是铜箔" 等垃圾）
_STATISTICAL_STOP_PREFIXES: Set[str] = {
    "的", "是", "在", "和", "与", "或", "了", "对", "为", "被", "把", "让",
    "该", "其", "这", "那", "本", "各", "某", "每", "另", "也", "又", "还",
    "都", "已", "将", "正", "在", "可", "能", "会", "应", "需", "要",
    "有", "无", "多", "少", "大", "小", "高", "低", "新", "旧",
    "随着", "由于", "因为", "所以", "但是", "不过", "然而", "虽然",
    "目前", "当前", "未来", "过去", "现在", "此前", "此后",
    "全球", "国内", "国外", "海外", "中国", "美国", "日本", "韩国",
}

# 普通词过滤（高频但无信息量的词）
_COMMON_WORD_FILTERS: Set[str] = {
    "公司", "企业", "行业", "产业", "市场", "产品", "业务", "收入", "利润",
    "增长", "下降", "提升", "降低", "变化", "发展", "趋势", "前景",
    "方面", "方面", "情况", "问题", "原因", "结果", "影响", "作用",
}

# P0 已知的种子词（用于 novelty_score 计算）
_P0_SEED_TERMS: Set[str] = set(ConceptTaxonomy().candidate_terms())


# ============================================================================
# 工具函数
# ============================================================================


def _short_hash(text: str) -> str:
    """计算短哈希，用于生成 ID。"""
    import hashlib
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def _is_valid_prefix(prefix: str) -> bool:
    """检查后缀模式的前缀是否有效。

    Args:
        prefix: 后缀模式匹配到的前缀部分

    Returns:
        前缀有效返回 True，否则 False
    """
    prefix = prefix.strip()
    if not prefix or len(prefix) < 1:
        return False
    # 纯数字前缀无效（如 "200树脂"）
    if prefix.isdigit():
        return False
    # 停用前缀过滤
    if prefix in _STATISTICAL_STOP_PREFIXES:
        return False
    # 纯标点前缀无效
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", prefix):
        return False
    return True


def _infer_term_type_by_suffix(suffix: str) -> str:
    """根据后缀推断 term_type。

    Args:
        suffix: 匹配到的后缀

    Returns:
        term_type 字符串
    """
    return _SUFFIX_TO_TERM_TYPE.get(suffix, "unclassified")


def _infer_root_theme(term: str, term_type: str, context: str) -> str:
    """推断 root_theme（光/存/算/电）。

    Args:
        term: 术语
        term_type: 术语类型
        context: 上下文文本

    Returns:
        root_theme 字符串
    """
    taxonomy = ConceptTaxonomy()
    root = taxonomy.root_theme_for(term, context)
    if root:
        return root
    # 按后缀类型推断
    if term_type == "material":
        if any(x in term for x in ("铜", "PCB", "连接器", "电源", "母线", "铜排")):
            return "电"
        if any(x in term for x in ("光", "磷化铟", "InP", "硅光")):
            return "光"
        if any(x in term for x in ("树脂", "电子布", "玻纤布", "基材", "衬底")):
            return "算"
    if term_type == "technology_route":
        if any(x in term for x in ("光", "互联", "光学", "连接")):
            return "光"
        if any(x in term for x in ("封装", "交换", "网络")):
            return "算"
    return ""


def _is_single_char_material_kept(term: str, doc_text: str) -> bool:
    """判断单字材料词是否应该保留。

    单字材料词（铜/钼/锗/钕等）仅在与"价格/资源/矿/供给/涨价/出口管制/材料"共现时才保留。

    Args:
        term: 单字材料词
        doc_text: 文档文本

    Returns:
        应保留返回 True，否则 False
    """
    if term not in _SINGLE_CHAR_MATERIALS:
        return True  # 非单字材料词，默认保留
    # 检查是否与共现关键词同时出现
    for keyword in _MATERIAL_KEEP_KEYWORDS:
        if keyword in doc_text:
            # 检查是否在附近（同一句子/50字窗口内）
            idx = doc_text.find(term)
            while idx >= 0:
                window = doc_text[max(0, idx - 30): idx + 30]
                if keyword in window:
                    return True
                idx = doc_text.find(term, idx + 1)
    return False


# ============================================================================
# 核心函数实现
# ============================================================================


def load_p2_inputs(input_dir: Optional[Path] = None) -> Dict[str, Any]:
    """加载 P2 输入数据。

    读取 raw_documents / raw_terms / candidate_concepts。

    Args:
        input_dir: 输入目录，默认为 data/reports/perilla_discovery/

    Returns:
        dict，含 raw_documents / raw_terms / candidate_concepts
    """
    base = Path(input_dir) if input_dir else Path("data/reports/perilla_discovery")
    result: Dict[str, Any] = {
        "raw_documents": [],
        "raw_terms": [],
        "candidate_concepts": [],
    }

    docs_path = base / "raw_documents_latest.json"
    if docs_path.exists():
        try:
            result["raw_documents"] = json.loads(docs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取 raw_documents_latest.json 失败: %s", exc)

    terms_path = base / "raw_terms_latest.json"
    if terms_path.exists():
        try:
            result["raw_terms"] = json.loads(terms_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取 raw_terms_latest.json 失败: %s", exc)

    concepts_path = base / "candidate_concepts_latest.json"
    if concepts_path.exists():
        try:
            result["candidate_concepts"] = json.loads(concepts_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取 candidate_concepts_latest.json 失败: %s", exc)

    return result


def extract_regex_terms(raw_documents: List[Dict[str, Any]]) -> List[str]:
    """抽取正则匹配的 term（保留 P0 英文缩写/英文数字组合/中文词表规则）。

    复用 P0 的 ConceptTaxonomy.candidate_terms() 和正则规则。

    Args:
        raw_documents: P0 的 raw_documents 列表

    Returns:
        去重后的 term 列表
    """
    taxonomy = ConceptTaxonomy()
    # P0 的英文缩写正则
    abbr_re = re.compile(r"\b[A-Z][A-Z0-9]{1,8}\b|\b\d(?:\.\d)?T\b|\b\d{3,4}[GV]\b")
    # P0 的中文技术词正则
    tech_term_re = re.compile(
        r"[\u4e00-\u9fffA-Za-z0-9]{2,14}"
        r"(?:模块|芯片|引擎|器件|电源|连接器|控制器|封装|材料|设备|"
        r"供电|液冷|光互连|高压直流|探测器|衬底|贴片机|铜箔|"
        r"树脂|磁性材料|浇筑设备)"
    )

    terms: Set[str] = set()
    for doc in raw_documents:
        text = " ".join([
            str(doc.get("title", "")),
            str(doc.get("summary", "")),
            str(doc.get("content_text", "")),
        ])
        # 词表匹配
        for seed in taxonomy.candidate_terms():
            if seed in text or seed.upper() in text.upper():
                terms.add(taxonomy.normalize(seed, text))
        # 英文缩写匹配
        terms.update(match.group(0) for match in abbr_re.finditer(text.upper()))
        # 中文技术词匹配
        terms.update(match.group(0) for match in tech_term_re.finditer(text))

    # 过滤空字符串
    return sorted({t for t in terms if t and len(t) >= 2})


def extract_statistical_terms(
    raw_documents: List[Dict[str, Any]],
    existing_terms: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """抽取统计新词（后缀模式约束 + 最长匹配 + 单字降权）。

    第一阶段只抽三类后缀模式：
    - 材料后缀：XX树脂 / XX铜箔 / XX电子布 / XX玻纤布 / XX基材 / XX材料 / XX衬底
    - 技术路线后缀：XX互联 / XX封装 / XX交换 / XX网络 / XX光学 / XX连接
    - 投资逻辑后缀：XX替代 / XX认证 / XX导入 / XX涨价 / XX扩产

    命中后缀后再计算 freq_score / cohesion_score / boundary_score / novelty_score。
    禁止无约束 substring 枚举。

    Args:
        raw_documents: P0 的 raw_documents 列表
        existing_terms: 已存在的 term 集合（用于 novelty_score 计算）

    Returns:
        统计新词列表，每条含 term / term_type / suffix / freq_score /
        cohesion_score / boundary_score / novelty_score / stat_score /
        source_count / related_document_ids / evidence_quotes
    """
    existing = existing_terms or set()
    # 收集所有候选片段（term, suffix, doc_id, position, doc_text）
    candidates: List[Dict[str, Any]] = []
    # 用于最长匹配：记录每个文档中每个位置已被占用的区间
    doc_occupied: Dict[str, List[Tuple[int, int]]] = defaultdict(list)

    for doc in raw_documents:
        doc_id = str(doc.get("document_id", ""))
        text = " ".join([
            str(doc.get("title", "")),
            str(doc.get("summary", "")),
            str(doc.get("content_text", "")),
        ])
        if not text:
            continue

        # 收集当前文档的所有匹配（含位置信息）
        matches: List[Tuple[int, str, str, str]] = []  # (start, full_term, prefix, suffix)
        for match in _SUFFIX_PATTERN.finditer(text):
            prefix = match.group(1)
            suffix = match.group(2)
            if not _is_valid_prefix(prefix):
                continue
            full_term = prefix + suffix
            start = match.start()
            matches.append((start, full_term, prefix, suffix))

        # 按长度降序排序，实现最长匹配优先
        matches.sort(key=lambda x: len(x[1]), reverse=True)

        occupied = doc_occupied[doc_id]
        accepted_in_doc: List[Tuple[int, str, str, str]] = []
        for start, full_term, prefix, suffix in matches:
            end = start + len(full_term)
            # 检查是否与已接受的长词位置重叠
            overlap = False
            for occ_start, occ_end in occupied:
                if start < occ_end and end > occ_start:
                    overlap = True
                    break
            if overlap:
                continue
            # 单字材料词降权检查
            if not _is_single_char_material_kept(full_term, text):
                continue
            accepted_in_doc.append((start, full_term, prefix, suffix))
            occupied.append((start, end))

        # 收集接受的片段
        for start, full_term, prefix, suffix in accepted_in_doc:
            # 提取上下文引用（前后 40 字）
            quote_start = max(0, start - 40)
            quote_end = min(len(text), start + len(full_term) + 40)
            quote = text[quote_start: quote_end].strip()
            candidates.append({
                "term": full_term,
                "suffix": suffix,
                "term_type": _infer_term_type_by_suffix(suffix),
                "doc_id": doc_id,
                "position": start,
                "doc_text": text,
                "evidence_quote": quote,
            })

    if not candidates:
        return []

    # 聚合统计：每个 term 的文档频次、位置信息
    term_docs: Dict[str, Set[str]] = defaultdict(set)
    term_quotes: Dict[str, List[str]] = defaultdict(list)
    term_suffix: Dict[str, str] = {}
    term_type: Dict[str, str] = {}
    term_positions: Dict[str, List[int]] = defaultdict(list)
    # 用于凝固度计算：term 内部字符的共现
    term_char_context: Dict[str, str] = defaultdict(str)

    for cand in candidates:
        term = cand["term"]
        term_docs[term].add(cand["doc_id"])
        if cand["evidence_quote"] and cand["evidence_quote"] not in term_quotes[term]:
            term_quotes[term].append(cand["evidence_quote"])
        term_suffix[term] = cand["suffix"]
        term_type[term] = cand["term_type"]
        term_positions[term].append(cand["position"])
        term_char_context[term] += cand["doc_text"]

    # 计算全局最大频次（用于归一化）
    max_freq = max(len(docs) for docs in term_docs.values()) if term_docs else 1

    results: List[Dict[str, Any]] = []
    for term, docs in term_docs.items():
        source_count = len(docs)
        freq = source_count
        # freq_score: 频次归一化（0-1）
        freq_score = round(min(1.0, freq / max(max_freq, 1)), 4)

        # cohesion_score: 凝固度（用 term 长度与后缀长度的比值近似）
        # 越长的 term 凝固度越高，纯后缀词（如 "树脂"）凝固度低
        suffix_len = len(term_suffix[term])
        term_len = len(term)
        cohesion_score = round(min(1.0, (term_len - suffix_len) / max(term_len, 1) + 0.3), 4)

        # boundary_score: 边界自由度（左右邻字多样性）
        # 统计 term 在所有文档中左边和右边的字符种类数
        left_chars: Set[str] = set()
        right_chars: Set[str] = set()
        context = term_char_context[term]
        idx = 0
        while True:
            idx = context.find(term, idx)
            if idx < 0:
                break
            if idx > 0:
                left_chars.add(context[idx - 1])
            right_end = idx + len(term)
            if right_end < len(context):
                right_chars.add(context[right_end])
            idx += 1
        boundary_score = round(min(1.0, (len(left_chars) + len(right_chars)) / 10.0), 4)

        # novelty_score: 新颖度（不在 P0 种子词表中得高分）
        novelty_score = 0.0 if term in existing or term in _P0_SEED_TERMS else 1.0
        novelty_score = round(novelty_score, 4)

        # stat_score: 综合统计分
        stat_score = round(freq_score + cohesion_score + boundary_score + novelty_score, 4)

        results.append({
            "term": term,
            "term_type": term_type[term],
            "suffix": term_suffix[term],
            "source_count": source_count,
            "freq_score": freq_score,
            "cohesion_score": cohesion_score,
            "boundary_score": boundary_score,
            "novelty_score": novelty_score,
            "stat_score": stat_score,
            "related_document_ids": sorted(docs),
            "evidence_quotes": term_quotes[term][:5],
        })

    # 按 stat_score 降序排序
    results.sort(key=lambda x: x["stat_score"], reverse=True)
    return results


def compute_burst_scores(
    raw_terms: List[Dict[str, Any]],
    statistical_terms: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """计算突增评分。

    无历史数据时 burst_score = source_count * 2 + recent_count。

    Args:
        raw_terms: P0 的 raw_terms 列表
        statistical_terms: P2 新发现的统计新词列表

    Returns:
        更新后的 raw_terms 列表（含 burst_score）
    """
    # 合并 P0 raw_terms 和统计新词
    merged: Dict[str, Dict[str, Any]] = {}
    for term in raw_terms:
        name = term.get("normalized_term") or term.get("term", "")
        if name:
            merged[name] = dict(term)

    for stat_term in (statistical_terms or []):
        name = stat_term.get("term", "")
        if not name:
            continue
        if name in merged:
            # 已存在，更新 source_count
            merged[name]["source_count"] = max(
                int(merged[name].get("source_count", 0)),
                int(stat_term.get("source_count", 0)),
            )
        else:
            # 新词，加入 merged
            merged[name] = {
                "term": name,
                "normalized_term": name,
                "term_type": stat_term.get("term_type", "unclassified"),
                "source_count": int(stat_term.get("source_count", 0)),
                "recent_count": int(stat_term.get("source_count", 0)),
                "related_document_ids": stat_term.get("related_document_ids", []),
                "related_titles": [],
                "related_companies": [],
                "evidence_quotes": stat_term.get("evidence_quotes", []),
                "co_terms": [],
                "first_seen_at": "",
                "last_seen_at": "",
                "status": "statistical_new",
            }

    # 计算 burst_score
    for name, term in merged.items():
        source_count = int(term.get("source_count", 0))
        recent_count = int(term.get("recent_count", source_count))
        burst_score = round(float(source_count * 2 + recent_count), 4)
        term["burst_score"] = burst_score

    return list(merged.values())


def compute_cooccurrence_scores(
    raw_terms: List[Dict[str, Any]],
    raw_documents: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """计算共现评分。

    同 document 共现的 term 互为 co_terms，每 term 最多保留 15 个。

    Args:
        raw_terms: raw_terms 列表
        raw_documents: raw_documents 列表

    Returns:
        更新后的 raw_terms 列表（含 cooccurrence_score 和更新后的 co_terms）
    """
    # 建立 term -> doc_ids 映射
    term_docs: Dict[str, Set[str]] = defaultdict(set)
    for term in raw_terms:
        name = term.get("normalized_term") or term.get("term", "")
        for doc_id in term.get("related_document_ids", []):
            term_docs[name].add(str(doc_id))

    # 计算共现矩阵
    doc_terms: Dict[str, Set[str]] = defaultdict(set)
    for term_name, doc_ids in term_docs.items():
        if not term_name:
            continue
        for doc_id in doc_ids:
            doc_terms[doc_id].add(term_name)

    cooccurrence: Dict[str, Counter] = defaultdict(Counter)
    for names in doc_terms.values():
        for name_a, name_b in combinations(sorted(names), 2):
            cooccurrence[name_a][name_b] += 1
            cooccurrence[name_b][name_a] += 1

    # 更新 raw_terms
    max_cooc = 1
    for term in raw_terms:
        name = term.get("normalized_term") or term.get("term", "")
        co_counter = cooccurrence.get(name, Counter())
        # 最多保留 15 个 co_terms
        top_co_terms = [item[0] for item in co_counter.most_common(15)]
        term["co_terms"] = top_co_terms
        # cooccurrence_score: 共现文档数归一化
        cooc_sum = sum(co_counter.values())
        if cooc_sum > max_cooc:
            max_cooc = cooc_sum

    for term in raw_terms:
        name = term.get("normalized_term") or term.get("term", "")
        co_counter = cooccurrence.get(name, Counter())
        cooc_sum = sum(co_counter.values())
        term["cooccurrence_score"] = round(min(1.0, cooc_sum / max(max_cooc, 1)), 4)

    return raw_terms


def build_concept_scores(
    raw_terms: List[Dict[str, Any]],
    candidate_concepts: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """构建概念评分。

    final_score = burst_score * 0.3 + cooccurrence_score * 0.25 +
                  novelty_score * 0.2 + evidence_score * 0.25

    同时为 candidate_concepts 补齐 burst_score / cooccurrence_score /
    novelty_score / evidence_score / final_score 五字段。

    Args:
        raw_terms: 含 burst_score / cooccurrence_score 的 raw_terms
        candidate_concepts: P0 的 candidate_concepts 列表

    Returns:
        (concept_scores 列表, 更新后的 candidate_concepts 列表)
    """
    # 建立 term -> raw_term 映射
    term_map: Dict[str, Dict[str, Any]] = {}
    for term in raw_terms:
        name = term.get("normalized_term") or term.get("term", "")
        if name:
            term_map[name] = term

    concept_scores: List[Dict[str, Any]] = []
    for concept in candidate_concepts:
        name = concept.get("normalized_name") or concept.get("concept_name", "")
        raw_term = term_map.get(name, {})

        # burst_score: 从 raw_term 取，归一化到 0-1
        burst_raw = float(raw_term.get("burst_score", 0) or 0)
        burst_score = round(min(1.0, burst_raw / 100.0), 4)  # 假设 burst_score 最大 100

        # cooccurrence_score: 从 raw_term 取
        cooccurrence_score = round(float(raw_term.get("cooccurrence_score", 0) or 0), 4)

        # novelty_score: 不在 P0 种子词表中为 1.0
        novelty_score = 0.0 if name in _P0_SEED_TERMS else 1.0
        novelty_score = round(novelty_score, 4)

        # evidence_score: 基于证据文档数归一化
        evidence_count = len(concept.get("evidence_document_ids", []))
        evidence_score = round(min(1.0, evidence_count / 20.0), 4)

        # final_score
        final_score = round(
            burst_score * 0.3 + cooccurrence_score * 0.25 +
            novelty_score * 0.2 + evidence_score * 0.25,
            4,
        )

        # 补齐 candidate_concepts 五字段
        concept["burst_score"] = burst_score
        concept["cooccurrence_score"] = cooccurrence_score
        concept["novelty_score"] = novelty_score
        concept["evidence_score"] = evidence_score
        concept["final_score"] = final_score

        # 构建 concept_scores 记录
        concept_scores.append({
            "concept_name": name,
            "concept_id": concept.get("concept_id", f"concept_{_short_hash(name)}"),
            "term_type": concept.get("term_type", "unclassified"),
            "root_theme": concept.get("root_theme", ""),
            "source_count": int(raw_term.get("source_count", 0) or 0),
            "burst_score": burst_score,
            "cooccurrence_score": cooccurrence_score,
            "novelty_score": novelty_score,
            "evidence_score": evidence_score,
            "final_score": final_score,
            "reason": concept.get("discovery_reason", ""),
        })

    # 按 final_score 降序排序
    concept_scores.sort(key=lambda x: x["final_score"], reverse=True)
    return concept_scores, candidate_concepts


def build_concept_aliases(
    raw_documents: List[Dict[str, Any]],
    candidate_concepts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """构建概念别名分组。

    规则：
    - optical_interconnect_family: CPO / LPO / NPO / MPO 共现
    - high_frequency_material_family: PTFE / 低介电 / 低损耗 / 高频高速 共现
    - copper_foil_family: 高端铜箔 / 铜箔 / HVLP铜箔 共现
    - inp_family: 磷化铟 / InP 共现

    Args:
        raw_documents: raw_documents 列表
        candidate_concepts: candidate_concepts 列表

    Returns:
        别名分组列表
    """
    # 统计每个关键词在哪些文档中出现
    concept_names = {
        concept.get("normalized_name", "")
        for concept in candidate_concepts
        if concept.get("normalized_name")
    }

    # 收集所有文档文本
    all_text = " ".join([
        " ".join([
            str(doc.get("title", "")),
            str(doc.get("summary", "")),
            str(doc.get("content_text", "")),
        ])
        for doc in raw_documents
    ])

    # 定义别名组规则
    alias_rules = [
        {
            "alias_group_id": "optical_interconnect_family",
            "canonical_name": "光互连家族",
            "keywords": list(_OPTICAL_INTERCONNECT_FAMILY),
            "reason": "CPO/LPO/NPO/MPO 同文档高频共现，归入光互连家族",
        },
        {
            "alias_group_id": "high_frequency_material_family",
            "canonical_name": "高频高速材料家族",
            "keywords": list(_HIGH_FREQ_MATERIAL_KEYWORDS),
            "reason": "PTFE/低介电/低损耗/高频高速 共现，归入高频高速材料家族",
        },
        {
            "alias_group_id": "copper_foil_family",
            "canonical_name": "铜箔家族",
            "keywords": list(_COPPER_FOIL_KEYWORDS),
            "reason": "高端铜箔/铜箔/HVLP铜箔 共现，归入铜箔家族",
        },
        {
            "alias_group_id": "inp_family",
            "canonical_name": "磷化铟家族",
            "keywords": list(_INP_KEYWORDS),
            "reason": "磷化铟/InP 共现，归入磷化铟家族",
        },
    ]

    results: List[Dict[str, Any]] = []
    for rule in alias_rules:
        keywords = rule["keywords"]
        # 检查哪些关键词在文档中出现
        found_keywords = [kw for kw in keywords if kw in all_text or kw in concept_names]
        if len(found_keywords) < 2:
            # 至少两个关键词共现才形成别名组
            continue

        # 收集相关文档 ID
        related_doc_ids: Set[str] = set()
        for doc in raw_documents:
            doc_text = " ".join([
                str(doc.get("title", "")),
                str(doc.get("summary", "")),
                str(doc.get("content_text", "")),
            ])
            if any(kw in doc_text for kw in found_keywords):
                related_doc_ids.add(str(doc.get("document_id", "")))

        results.append({
            "alias_group_id": rule["alias_group_id"],
            "canonical_name": rule["canonical_name"],
            "aliases": found_keywords,
            "reason": rule["reason"],
            "related_document_ids": sorted(related_doc_ids),
        })

    return results


def run_term_radar_enhancer(
    input_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """运行 P2 新词雷达增强全流程。

    串联：extract_regex_terms → extract_statistical_terms →
          compute_burst_scores → compute_cooccurrence_scores →
          build_concept_scores → build_concept_aliases

    输出：
    - 更新 raw_terms_latest.json（追加统计新词）
    - 更新 candidate_concepts_latest.json（补齐五字段）
    - concept_scores_latest.json
    - concept_aliases_latest.json

    Args:
        input_dir: 输入目录，默认为 data/reports/perilla_discovery/
        output_dir: 输出目录，默认同 input_dir

    Returns:
        dict summary，含 module / success / outputs / counts
    """
    base = Path(input_dir) if input_dir else Path("data/reports/perilla_discovery")
    out = Path(output_dir) if output_dir else base

    try:
        # 加载输入
        inputs = load_p2_inputs(base)
        raw_documents = inputs["raw_documents"]
        raw_terms = inputs["raw_terms"]
        candidate_concepts = inputs["candidate_concepts"]

        # 1. 抽取正则匹配 term（保留 P0 能力）
        regex_terms = extract_regex_terms(raw_documents)
        existing_terms = set(regex_terms)

        # 2. 抽取统计新词（后缀模式约束 + 最长匹配 + 单字降权）
        statistical_terms = extract_statistical_terms(raw_documents, existing_terms)

        # 3. 计算突增评分（合并 P0 raw_terms 和统计新词）
        merged_terms = compute_burst_scores(raw_terms, statistical_terms)

        # 4. 计算共现评分
        merged_terms = compute_cooccurrence_scores(merged_terms, raw_documents)

        # 5. 构建概念评分（补齐 candidate_concepts 五字段）
        concept_scores, updated_concepts = build_concept_scores(merged_terms, candidate_concepts)

        # 6. 构建别名分组
        concept_aliases = build_concept_aliases(raw_documents, updated_concepts)

        # 输出文件
        out.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        raw_terms_path = out / "raw_terms_latest.json"
        raw_terms_path.write_text(
            json.dumps(merged_terms, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(raw_terms_path))

        concepts_path = out / "candidate_concepts_latest.json"
        concepts_path.write_text(
            json.dumps(updated_concepts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(concepts_path))

        scores_path = out / "concept_scores_latest.json"
        scores_path.write_text(
            json.dumps(concept_scores, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(scores_path))

        aliases_path = out / "concept_aliases_latest.json"
        aliases_path.write_text(
            json.dumps(concept_aliases, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(aliases_path))

        return {
            "module": "term_radar_enhancer",
            "success": True,
            "outputs": outputs,
            "counts": {
                "raw_terms": len(merged_terms),
                "candidate_concepts": len(updated_concepts),
                "statistical_terms": len(statistical_terms),
                "concept_scores": len(concept_scores),
                "concept_aliases": len(concept_aliases),
            },
        }
    except Exception as exc:
        logger.error("run_term_radar_enhancer 失败: %s", exc, exc_info=True)
        return {
            "module": "term_radar_enhancer",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }
