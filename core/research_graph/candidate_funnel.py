"""候选文档漏斗:去重、相关性分类、优先级排序。

接入 GrowthMiningEngine 主流程,在 LLM 深度阅读之前把候选文档分到三档:
  - must_read: 必读,走 LLM 4 阶段证据抽取
  - skim: 略读,走规则提取或丢弃
  - skip: 跳过,不进入后续处理

只引入两种算法:
  - SimHash 去重
  - 朴素贝叶斯相关性分类
其余全部用规则和统计,避免重 ML 模型带来的开发和维护成本。
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol


def _tokenize(text: str) -> list[str]:
    """轻量分词:中文按字符,英文按单词,过滤标点。"""
    if not text:
        return []
    # 保留中文字符、英文单词、数字
    tokens: list[str] = re.findall(r"[\u4e00-\u9fa5]|[a-zA-Z0-9]+", text.lower())
    return tokens


def _tokenize_ngrams(text: str, n: int = 2) -> list[str]:
    """生成字符 n-gram,用于 SimHash。"""
    tokens = _tokenize(text)
    if len(tokens) < n:
        return tokens
    return ["".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _jaccard_similarity(a: str, b: str) -> float:
    """计算两段文本的 Jaccard 相似度。"""
    tokens_a = set(_tokenize(a))
    tokens_b = set(_tokenize(b))
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union else 0.0


class _CandidateLike(Protocol):
    """候选对象协议,duck typing 避免循环导入。"""

    title: str
    raw_text: str
    url: str
    publisher: str
    provider_type: str
    retrieved_at: str


@dataclass
class FunnelScores:
    """候选文档漏斗评分。"""

    authority: float
    novelty: float
    graph_match: float
    freshness: float
    total: float


@dataclass
class CandidateBucket:
    """漏斗分桶结果。"""

    must_read: list[_CandidateLike] = field(default_factory=list)
    skim: list[_CandidateLike] = field(default_factory=list)
    skip: list[_CandidateLike] = field(default_factory=list)


class SimHashDeduper:
    """基于 SimHash 的文本去重器。

    对标题+正文生成 64 位指纹,Hamming 距离小于等于阈值视为重复。
    """

    def __init__(self, threshold: int = 3) -> None:
        """初始化。

        Args:
            threshold: Hamming 距离阈值,小于等于视为重复。
        """
        self._threshold = threshold
        self._fingerprints: list[int] = []

    @staticmethod
    def _hash_token(token: str) -> int:
        """对 token 生成 64 位 hash。"""
        digest = hashlib.sha256(token.encode("utf-8")).digest()[:8]
        return int.from_bytes(digest, "big", signed=True)

    def _fingerprint(self, text: str) -> int:
        """生成文本的 64 位 SimHash 指纹。"""
        tokens = _tokenize_ngrams(text, n=2)
        if not tokens:
            return 0
        counter = Counter(tokens)
        vector = [0.0] * 64
        for token, weight in counter.items():
            token_hash = self._hash_token(token)
            for i in range(64):
                bit = (token_hash >> i) & 1
                vector[i] += weight if bit else -weight
        fingerprint = 0
        for i in range(64):
            if vector[i] > 0:
                fingerprint |= 1 << i
        return fingerprint

    def is_duplicate(self, text: str) -> bool:
        """判断文本是否与已有指纹重复。"""
        fingerprint = self._fingerprint(text)
        for existing in self._fingerprints:
            distance = (fingerprint ^ existing).bit_count()
            if distance <= self._threshold:
                return True
        return False

    def add(self, text: str) -> None:
        """把文本指纹加入索引。"""
        self._fingerprints.append(self._fingerprint(text))


class NaiveBayesRelevanceClassifier:
    """朴素贝叶斯相关性分类器。

    没有人工标注数据时,用主题词和 neuron aliases 构造正类词汇,
    用通用停用词构造负类词汇,输出候选文本与研究方向相关的概率。
    """

    _STOP_WORDS: set[str] = {
        "的", "了", "是", "在", "和", "与", "或", "有", "我", "你", "他",
        "这", "那", "为", "之", "及", "等", "对", "也", "就", "都", "而",
        "但", "要", "会", "能", "可以", "一个", "没有", "进行", "表示",
        "已经", "开始", "目前", "公司", "企业", "市场", "产品", "业务",
    }

    def __init__(self, theme: str, neuron_aliases: Iterable[str]) -> None:
        """初始化分类器。

        Args:
            theme: 研究主题,如"HBM"。
            neuron_aliases: 神经元别名列表,用于构造正类词汇。
        """
        self._pos_phrases: set[str] = {
            a.lower().strip() for a in neuron_aliases if a
        }
        self._pos_phrases.add(theme.lower().strip())
        self._pos_vocab: set[str] = set(_tokenize(theme))
        for alias in neuron_aliases:
            self._pos_vocab.update(_tokenize(alias))
        self._neg_vocab = self._STOP_WORDS.copy()

    def predict(self, text: str) -> float:
        """预测候选文本与研究主题相关的概率。

        朴素贝叶斯特征为"主题/alias 完整短语是否命中"。
        单字容易被误匹配(如"电镀液"的"电"命中"电影"),
        所以这里只用完整短语作为正类特征,命中越多概率越高。

        Args:
            text: 待预测文本。

        Returns:
            相关概率,范围 [0, 1]。
        """
        text_lower = text.lower()
        phrase_hits = sum(1 for p in self._pos_phrases if p in text_lower)
        if phrase_hits == 0:
            return 0.0
        # 命中 1 个短语给 0.7,命中越多越高,封顶 0.95
        return min(0.95, 0.7 + 0.05 * phrase_hits)


class RuleBasedRanker:
    """基于规则的候选文档排序器。"""

    _AUTHORITY_SCORES: dict[str, float] = {
        "announcement": 1.0,
        "financial_report": 1.0,
        "official": 0.9,
        "news": 0.7,
        "crawler": 0.6,
        "api": 0.5,
        "blog": 0.4,
        "forum": 0.3,
        "local_cache": 0.8,
        "unknown": 0.5,
    }

    def score(
        self,
        candidate: _CandidateLike,
        neuron_aliases: Iterable[str],
        existing_quotes: Iterable[str] | None,
        now: datetime | None = None,
    ) -> FunnelScores:
        """对单个候选打分。

        Args:
            candidate: 候选文档。
            neuron_aliases: 当前研究方向的 neuron aliases。
            existing_quotes: 已有 evidence quote,用于计算新颖度。
            now: 当前时间,默认 UTC now。

        Returns:
            FunnelScores 评分对象。
        """
        text = f"{candidate.title} {candidate.raw_text}"
        tokens = set(_tokenize(text))

        # 来源权威性
        provider_type = (candidate.provider_type or "unknown").lower()
        authority = self._AUTHORITY_SCORES.get(provider_type, 0.5)

        # 图谱匹配度:完整 alias 作为子串命中数量 / 总 alias 数量
        text_lower = text.lower()
        aliases = [a.lower() for a in neuron_aliases if a]
        matched = sum(1 for a in aliases if a in text_lower) / max(1, len(aliases))

        # 新颖度:与已有 quote 的最大相似度越低越新
        novelty = 1.0
        if existing_quotes:
            similarities = [_jaccard_similarity(text, q) for q in existing_quotes if q]
            if similarities:
                novelty = 1.0 - max(similarities)

        # 新鲜度:越近越高,指数衰减,半衰期 7 天
        freshness = self._freshness(candidate.retrieved_at, now)

        total = (
            authority * 0.35
            + matched * 0.25
            + novelty * 0.25
            + freshness * 0.15
        )
        return FunnelScores(
            authority=round(authority, 4),
            novelty=round(novelty, 4),
            graph_match=round(matched, 4),
            freshness=round(freshness, 4),
            total=round(total, 4),
        )

    @staticmethod
    def _freshness(retrieved_at: str, now: datetime | None = None) -> float:
        """计算新鲜度分数。"""
        if not retrieved_at:
            return 0.5
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            # 支持 ISO 格式和常见格式
            ts = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return 0.5
        age_days = (now - ts).total_seconds() / 86400.0
        half_life = 7.0
        import math

        return math.exp(-age_days / half_life)


def funnel_candidates(
    candidates: Iterable[_CandidateLike],
    theme: str,
    neuron_aliases: Iterable[str],
    *,
    existing_quotes: Iterable[str] | None = None,
    now: datetime | None = None,
    must_read_ratio: float = 0.2,
    must_read_threshold: float = 0.6,
    relevance_threshold: float = 0.25,
    skim_threshold: float = 0.4,
) -> CandidateBucket:
    """候选文档漏斗主入口。

    流程:
      1. SimHash 去重
      2. 朴素贝叶斯相关性分类
      3. 规则排序
      4. 分桶:must_read / skim / skip

    Args:
        candidates: 候选文档列表。
        theme: 研究主题。
        neuron_aliases: neuron aliases,用于相关性和图谱匹配。
        existing_quotes: 已有 evidence quote,用于新颖度计算。
        now: 当前时间。
        must_read_ratio: must_read 桶占相关候选的比例上限,默认 20%。
        must_read_threshold: must_read 桶的最低总分门槛,默认 0.6。
        relevance_threshold: 相关性概率阈值,低于则 skip。
        skim_threshold: skim 桶的最低总分门槛,默认 0.4。

    Returns:
        CandidateBucket 分桶结果。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    deduper = SimHashDeduper(threshold=3)
    classifier = NaiveBayesRelevanceClassifier(theme, neuron_aliases)
    ranker = RuleBasedRanker()

    # 1. 去重 + 2. 相关性分类
    relevant: list[_CandidateLike] = []
    for candidate in candidates:
        text = f"{candidate.title} {candidate.raw_text}"
        if deduper.is_duplicate(text):
            continue
        deduper.add(text)
        if classifier.predict(text) < relevance_threshold:
            continue
        relevant.append(candidate)

    if not relevant:
        return CandidateBucket()

    # 3. 规则排序
    scored: list[tuple[_CandidateLike, FunnelScores]] = [
        (c, ranker.score(c, neuron_aliases, existing_quotes, now)) for c in relevant
    ]
    scored.sort(key=lambda x: x[1].total, reverse=True)

    # 4. 分桶
    bucket = CandidateBucket()
    max_must_read = max(1, int(len(scored) * must_read_ratio))
    for idx, (candidate, scores) in enumerate(scored):
        if idx < max_must_read and scores.total >= must_read_threshold:
            bucket.must_read.append(candidate)
        elif scores.total >= skim_threshold:
            bucket.skim.append(candidate)
        else:
            bucket.skip.append(candidate)
    return bucket
