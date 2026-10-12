"""证据新颖度门控(Step 3)。

在 EvidenceLedger 写入前判定证据是否已被覆盖,避免跨运行重复抽取浪费 token。

设计:
- 复用 candidate_funnel.SimHashDeduper 做 64 位指纹去重(Hamming 距离阈值)。
- 维护两个指纹源:
  1. 内存指纹集合(本次进程内累积)。
  2. 向量记忆持久化指纹(Step 4 接入),重启后仍可识别历史重复。
- 输出 NoveltyDecision(is_novel / duplicate_of / similarity)。
- 不抛异常:任何解析失败都视为 novel,保证主流程不中断。

不引入新依赖,完全基于已有的 SimHash。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from core.research_graph.candidate_funnel import SimHashDeduper


class _FingerprintStore(Protocol):
    """指纹存储协议(Step 4 由 VectorMemoryFingerprintStore 实现)。"""

    def has_fingerprint(self, fingerprint: int) -> bool: ...
    def add_fingerprint(self, fingerprint: int, evidence_id: str) -> None: ...


@dataclass
class NoveltyDecision:
    """新颖度判定结果。"""

    is_novel: bool
    duplicate_of: str = ""
    similarity: float = 0.0
    reason: str = ""


@dataclass
class EvidenceNoveltyGate:
    """证据新颖度门控。

    Args:
        simhash_threshold: Hamming 距离阈值,小于等于视为重复。
        exact_min_length: 短于此长度的 quote 仅做精确匹配,不参与 SimHash。
    """

    simhash_threshold: int = 3
    exact_min_length: int = 12
    _deduper: SimHashDeduper = field(default_factory=lambda: SimHashDeduper(threshold=3))
    _fingerprints: list[tuple[int, str]] = field(default_factory=list)
    _exact_hashes: dict[str, str] = field(default_factory=dict)
    _persistent_store: Any = None

    def __init__(
        self,
        *,
        simhash_threshold: int = 3,
        exact_min_length: int = 12,
        persistent_store: Any = None,
    ) -> None:
        self.simhash_threshold = simhash_threshold
        self.exact_min_length = exact_min_length
        self._deduper = SimHashDeduper(threshold=simhash_threshold)
        self._fingerprints: list[tuple[int, str]] = []
        self._exact_hashes: dict[str, str] = {}
        self._persistent_store = persistent_store

    def evaluate(self, evidence: Any) -> NoveltyDecision:
        """判定证据是否新颖。

        Args:
            evidence: Evidence 实例或含 quote/source_id 的对象。

        Returns:
            NoveltyDecision。is_novel=True 表示应写入 ledger。
        """
        quote = str(getattr(evidence, "quote", "") or "")
        evidence_id = str(getattr(evidence, "evidence_id", "") or "")
        if not quote:
            return NoveltyDecision(is_novel=False, reason="empty_quote")

        # 精确哈希优先:相同 quote 一定重复。
        exact = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        if exact in self._exact_hashes:
            return NoveltyDecision(
                is_novel=False,
                duplicate_of=self._exact_hashes[exact],
                similarity=1.0,
                reason="exact_quote_match",
            )

        # SimHash 近似去重。
        fingerprint = self._deduper._fingerprint(quote)  # noqa: SLF001
        # 内存指纹。
        for existing_fp, existing_id in self._fingerprints:
            distance = (fingerprint ^ existing_fp).bit_count()
            if distance <= self.simhash_threshold:
                similarity = 1.0 - distance / 64.0
                return NoveltyDecision(
                    is_novel=False,
                    duplicate_of=existing_id,
                    similarity=similarity,
                    reason=f"simhash_distance_{distance}",
                )
        # 持久化指纹(Step 4 接入后)。
        if self._persistent_store is not None:
            try:
                if self._persistent_store.has_fingerprint(fingerprint):
                    return NoveltyDecision(
                        is_novel=False,
                        duplicate_of="persistent",
                        similarity=0.9,
                        reason="persistent_fingerprint_match",
                    )
            except Exception:
                pass

        return NoveltyDecision(is_novel=True, reason="novel")

    def register(self, evidence: Any) -> None:
        """把证据指纹登记到门控,后续相同/近似证据将被判为重复。

        Args:
            evidence: 已写入 ledger 的 Evidence 实例。
        """
        quote = str(getattr(evidence, "quote", "") or "")
        evidence_id = str(getattr(evidence, "evidence_id", "") or "")
        if not quote:
            return
        exact = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        self._exact_hashes[exact] = evidence_id
        fingerprint = self._deduper._fingerprint(quote)  # noqa: SLF001
        self._fingerprints.append((fingerprint, evidence_id))
        self._deduper.add(quote)
        if self._persistent_store is not None:
            try:
                self._persistent_store.add_fingerprint(fingerprint, evidence_id)
            except Exception:
                pass

    def register_many(self, evidences: Iterable[Any]) -> int:
        """批量登记已有证据指纹(用于冷启动加载历史 ledger)。

        Returns:
            成功登记的数量。
        """
        count = 0
        for ev in evidences:
            self.register(ev)
            count += 1
        return count

    def stats(self) -> dict[str, Any]:
        """返回门控当前状态摘要。"""
        return {
            "memory_fingerprints": len(self._fingerprints),
            "exact_hashes": len(self._exact_hashes),
            "simhash_threshold": self.simhash_threshold,
            "has_persistent_store": self._persistent_store is not None,
        }
