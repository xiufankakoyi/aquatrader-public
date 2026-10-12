from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.research_graph.evidence_corpus import read_jsonl
from core.research_graph.models import Evidence
from core.research_graph.raw_document import RawDocument
from core.research_graph.schema import EvidenceLevel, OFFICIAL_SOURCE_TYPES
from core.research_graph.store import GraphStore

# 向后兼容别名：外部调用方可继续使用 SourceDocument 名称
SourceDocument = RawDocument


DEFAULT_COUNTER_TERMS = [
    "明确否认",
    "公司否认",
    "予以否认",
    "不存在",
    "不涉及",
    "无关联",
    "无相关",
    "业务剥离",
    "终止合作",
    "暂停项目",
    "不及预期",
    "低于预期",
    "下调预期",
    "需求疲软",
    "产能过剩",
    "价格下滑",
    "取消订单",
]


@dataclass
class Claim:
    claim_id: str
    edge_id: str
    claim_text: str
    support_terms: list[str] = field(default_factory=list)
    counter_terms: list[str] = field(default_factory=list)
    topic: str = ""
    subject: str = ""
    relation: str = ""
    object: str = ""
    path_hint: list[str] = field(default_factory=list)


@dataclass
class RetrievedDocument:
    document: SourceDocument
    score: float
    matched_terms: list[str]
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document.document_id,
            "source_id": self.document.source_id,
            "source_type": self.document.source_type,
            "title": self.document.title,
            "url": self.document.url,
            "topic": self.document.topic,
            "score": round(self.score, 4),
            "matched_terms": list(self.matched_terms),
            "reasons": list(self.reasons),
        }


@dataclass
class EvidenceMiningResult:
    claim_id: str
    edge_id: str
    support_evidence: list[Evidence]
    counter_evidence: list[Evidence]
    suggested_confidence: str
    should_update_graph: bool
    rationale: str
    retrieved_documents: list[RetrievedDocument] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "edge_id": self.edge_id,
            "support_evidence": [item.to_dict() for item in self.support_evidence],
            "counter_evidence": [item.to_dict() for item in self.counter_evidence],
            "suggested_confidence": self.suggested_confidence,
            "should_update_graph": self.should_update_graph,
            "rationale": self.rationale,
            "retrieved_documents": [item.to_dict() for item in self.retrieved_documents],
        }


class EvidenceMiner:
    """Extract traceable support and counter evidence for a claim."""

    def mine(
        self,
        claim: Claim,
        documents: list[SourceDocument],
        retrieved_documents: list[RetrievedDocument] | None = None,
    ) -> EvidenceMiningResult:
        support: list[Evidence] = []
        counter: list[Evidence] = []
        support_terms = _dedupe_terms(claim.support_terms or self._terms_from_claim(claim.claim_text))
        counter_terms = _dedupe_terms(claim.counter_terms or DEFAULT_COUNTER_TERMS)

        for document in documents:
            content = document.searchable_text()
            support_hit = self._first_hit(content, support_terms)
            counter_hit = self._first_hit(content, counter_terms)
            if support_hit:
                support.append(self._make_evidence(claim, document, support_hit, contradicts=False))
            if counter_hit:
                counter.append(self._make_evidence(claim, document, counter_hit, contradicts=True))

        suggested = self._suggest_confidence(support, counter)
        should_update = bool(support or counter)
        rationale = (
            "抽取到支持证据或反证，建议交由 GraphVerifier 更新图谱"
            if should_update else "未抽取到可用证据，不建议更新图谱"
        )
        return EvidenceMiningResult(
            claim_id=claim.claim_id,
            edge_id=claim.edge_id,
            support_evidence=support,
            counter_evidence=counter,
            suggested_confidence=suggested,
            should_update_graph=should_update,
            rationale=rationale,
            retrieved_documents=retrieved_documents or [],
        )

    def apply_to_store(
        self,
        store: GraphStore,
        result: EvidenceMiningResult,
        operator: str = "evidence_miner",
    ) -> None:
        for evidence in result.support_evidence:
            store.add_evidence(evidence, operator=operator)
            if result.edge_id in store.edges:
                store.attach_evidence(result.edge_id, evidence.evidence_id, operator=operator)
        for evidence in result.counter_evidence:
            store.add_evidence(evidence, operator=operator)
            if result.edge_id in store.edges:
                store.attach_evidence(result.edge_id, evidence.evidence_id, as_counter=True, operator=operator)

    @staticmethod
    def _terms_from_claim(text: str) -> list[str]:
        return _dedupe_terms(re.split(r"[\s,，。；;、:：/()（）\[\]【】]+", text))

    @staticmethod
    def _is_boundary(char: str) -> bool:
        """判断字符是否为中文词边界（非汉字或空）。"""
        if not char:
            return True
        return not ("\u4e00" <= char <= "\u9fff")

    @classmethod
    def _first_hit(cls, content: str, terms: list[str]) -> str:
        """在内容中查找第一个以独立语义单元出现的术语，返回上下文片段。"""
        for term in terms:
            if not term:
                continue
            # Chinese prose has no whitespace word boundaries. An exact
            # multi-character Chinese phrase is specific enough to match inside
            # a sentence; keep boundary checks for Latin and one-character terms.
            is_chinese_phrase = len(term) >= 2 and all(
                "\u4e00" <= char <= "\u9fff" for char in term
            )
            idx = content.find(term)
            while idx != -1:
                before = content[idx - 1] if idx > 0 else ""
                after = content[idx + len(term)] if idx + len(term) < len(content) else ""
                if is_chinese_phrase or (
                    cls._is_boundary(before) and cls._is_boundary(after)
                ):
                    start = max(0, idx - 24)
                    end = min(len(content), idx + len(term) + 72)
                    return content[start:end].strip()
                idx = content.find(term, idx + 1)
        return ""

    @staticmethod
    def _make_evidence(
        claim: Claim,
        document: SourceDocument,
        quote: str,
        *,
        contradicts: bool,
    ) -> Evidence:
        level = EvidenceLevel.DISPROVEN.value if contradicts else EvidenceLevel.SPECULATIVE.value
        if not contradicts and document.source_type in OFFICIAL_SOURCE_TYPES:
            level = EvidenceLevel.PROVEN.value
        elif not contradicts and document.source_type in {
            "company_website",
            "technical_doc",
            "research_report",
            "industry_report",
            "news",
            "investor_interaction",
        }:
            level = EvidenceLevel.LIKELY.value
        return Evidence(
            evidence_id=f"ev_{claim.claim_id}_{document.document_id}_{'counter' if contradicts else 'support'}",
            source_type=document.source_type,
            source_title=document.title,
            source_id=document.source_id,
            document_id=document.document_id,
            source_url=document.url,
            publish_date=document.publish_date,
            quote=quote,
            summary=(
                "该证据反驳 claim，需要降级或标记冲突"
                if contradicts else "该证据支持 claim，可交由验证器评估置信度"
            ),
            evidence_level=level,
            supports=[] if contradicts else [claim.edge_id],
            contradicts=[claim.edge_id] if contradicts else [],
            extracted_by="evidence_miner",
            verified_by="rule_verifier",
            metadata={
                "claim_id": claim.claim_id,
                "topic": claim.topic or document.topic,
                "matched_document_topic": document.topic,
                "source_id": document.source_id,
                "document_id": document.document_id,
            },
        )

    @staticmethod
    def _suggest_confidence(support: list[Evidence], counter: list[Evidence]) -> str:
        if support and counter:
            return EvidenceLevel.CONFLICTED.value
        if counter:
            return EvidenceLevel.DISPROVEN.value
        if any(item.evidence_level == EvidenceLevel.PROVEN.value for item in support):
            return EvidenceLevel.PROVEN.value
        if len(support) >= 2 or any(item.evidence_level == EvidenceLevel.LIKELY.value for item in support):
            return EvidenceLevel.LIKELY.value
        if support:
            return EvidenceLevel.SPECULATIVE.value
        return EvidenceLevel.SPECULATIVE.value


class EvidenceCorpusIndex:
    """Small deterministic retrieval index for local research corpus documents."""

    def __init__(self, documents: list[SourceDocument]) -> None:
        self.documents = _dedupe_documents(documents)

    @classmethod
    def from_overnight_run(cls, run_dir: Path) -> "EvidenceCorpusIndex":
        rows = read_jsonl(run_dir / "raw_evidence.jsonl")
        return cls(documents_from_raw_evidence(rows))

    def retrieve(self, claim: Claim, *, limit: int = 5, min_score: float = 1.0) -> list[RetrievedDocument]:
        query_terms = _claim_query_terms(claim)
        retrieved: list[RetrievedDocument] = []
        for document in self.documents:
            score, matches, reasons = _score_document(claim, document, query_terms)
            if score >= min_score:
                retrieved.append(RetrievedDocument(document, score, matches, reasons))
        retrieved.sort(key=lambda item: (-item.score, item.document.document_id))
        return retrieved[:limit]


class CorpusEvidenceMiner:
    """Retrieval-augmented Evidence Miner over a local corpus."""

    def __init__(self, index: EvidenceCorpusIndex, miner: EvidenceMiner | None = None) -> None:
        self.index = index
        self.miner = miner or EvidenceMiner()

    @classmethod
    def from_overnight_run(cls, run_dir: Path) -> "CorpusEvidenceMiner":
        return cls(EvidenceCorpusIndex.from_overnight_run(run_dir))

    def mine_claim(self, claim: Claim, *, limit: int = 5, min_score: float = 1.0) -> EvidenceMiningResult:
        retrieved = self.index.retrieve(claim, limit=limit, min_score=min_score)
        documents = [item.document for item in retrieved]
        return self.miner.mine(claim, documents, retrieved_documents=retrieved)

    def mine_many(
        self,
        claims: list[Claim],
        *,
        limit: int = 5,
        min_score: float = 1.0,
        store: GraphStore | None = None,
    ) -> list[EvidenceMiningResult]:
        results = [self.mine_claim(claim, limit=limit, min_score=min_score) for claim in claims]
        if store is not None:
            for result in results:
                self.miner.apply_to_store(store, result)
        return results


def documents_from_raw_evidence(rows: list[dict[str, Any]]) -> list[SourceDocument]:
    documents: list[SourceDocument] = []
    for row in rows:
        content_parts = [
            str(row.get("extracted_text") or ""),
            str(row.get("summary") or ""),
            str(row.get("why_strength") or ""),
        ]
        documents.append(
            SourceDocument(
                document_id=str(row.get("evidence_id") or row.get("source_id") or ""),
                source_id=str(row.get("source_id") or ""),
                source_type=str(row.get("source_type") or ""),
                title=str(row.get("title") or ""),
                content="\n".join(part for part in content_parts if part),
                publish_date=str(row.get("publish_date") or ""),
                url=str(row.get("url") or ""),
                topic=str(row.get("topic") or ""),
                summary=str(row.get("summary") or ""),
                metadata={
                    "evidence_id": row.get("evidence_id"),
                    "mentioned_entities": list(row.get("mentioned_entities") or []),
                    "mentioned_chain_nodes": list(row.get("mentioned_chain_nodes") or []),
                    "possible_relation_types": list(row.get("possible_relation_types") or []),
                    "evidence_strength_hint": row.get("evidence_strength_hint"),
                    "needs_human_review": row.get("needs_human_review"),
                },
            )
        )
    return documents


def claim_from_candidate(row: dict[str, Any]) -> Claim:
    subject = row.get("subject") or {}
    obj = row.get("object") or {}
    path_hint = [str(item) for item in row.get("path_hint") or [] if item]
    support_terms = _dedupe_terms([
        str(subject.get("name") or ""),
        str(obj.get("name") or ""),
        str(row.get("relation") or ""),
        *path_hint,
        str(row.get("reason") or ""),
    ])
    counter_terms = _dedupe_terms([*DEFAULT_COUNTER_TERMS, *[str(item) for item in row.get("missing_verification") or []]])
    return Claim(
        claim_id=str(row.get("claim_id") or ""),
        edge_id=str(row.get("claim_id") or ""),
        claim_text=_claim_text_from_candidate(row),
        support_terms=support_terms,
        counter_terms=counter_terms,
        topic=str(row.get("topic") or ""),
        subject=str(subject.get("name") or ""),
        relation=str(row.get("relation") or ""),
        object=str(obj.get("name") or ""),
        path_hint=path_hint,
    )


def load_candidate_claims(path: Path) -> list[Claim]:
    return [claim_from_candidate(row) for row in read_jsonl(path)]


def write_mining_results(path: Path, results: list[EvidenceMiningResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True) for result in results)
        + ("\n" if results else ""),
        encoding="utf-8",
    )


def summarize_mining_results(results: list[EvidenceMiningResult]) -> dict[str, Any]:
    return {
        "claims": len(results),
        "claims_with_retrieved_documents": sum(1 for item in results if item.retrieved_documents),
        "claims_with_support_evidence": sum(1 for item in results if item.support_evidence),
        "claims_with_counter_evidence": sum(1 for item in results if item.counter_evidence),
        "support_evidence": sum(len(item.support_evidence) for item in results),
        "counter_evidence": sum(len(item.counter_evidence) for item in results),
        "confidence_distribution": dict(Counter(item.suggested_confidence for item in results)),
        "untraceable_evidence": [
            evidence.evidence_id
            for result in results
            for evidence in result.support_evidence + result.counter_evidence
            if not evidence.source_id or not evidence.document_id
        ],
        "non_candidate_updates": [],
        "result_type": "evidence_mining_smoke_summary",
    }


def _claim_text_from_candidate(row: dict[str, Any]) -> str:
    subject = (row.get("subject") or {}).get("name") or ""
    obj = (row.get("object") or {}).get("name") or ""
    parts = [str(row.get("topic") or ""), str(subject), str(row.get("relation") or ""), str(obj), str(row.get("reason") or "")]
    return " ".join(part for part in parts if part)


def _claim_query_terms(claim: Claim) -> list[str]:
    return _dedupe_terms([
        claim.topic,
        claim.subject,
        claim.object,
        claim.relation,
        claim.claim_text,
        *claim.path_hint,
        *claim.support_terms,
    ])


def _score_document(
    claim: Claim,
    document: SourceDocument,
    query_terms: list[str],
) -> tuple[float, list[str], list[str]]:
    text = document.searchable_text()
    matched: list[str] = []
    score = 0.0
    reasons: list[str] = []
    if claim.topic and document.topic == claim.topic:
        score += 2.0
        reasons.append("topic_match")
    for term in query_terms:
        if not term or term not in text:
            continue
        matched.append(term)
        if term in claim.support_terms:
            score += 3.0
            reasons.append("support_term_match")
        elif term in {claim.subject, claim.object}:
            score += 2.0
            reasons.append("endpoint_match")
        else:
            score += 1.0
            reasons.append("query_term_match")
    if document.source_type in OFFICIAL_SOURCE_TYPES | {"company_website", "technical_doc"}:
        score += 0.5
        reasons.append("source_quality_hint")
    return score, _dedupe_terms(matched), sorted(set(reasons))


def _dedupe_terms(terms: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = str(term).strip()
        if len(cleaned) < 2 or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _dedupe_documents(documents: list[SourceDocument]) -> list[SourceDocument]:
    result: list[SourceDocument] = []
    seen: set[tuple[str, str]] = set()
    for document in documents:
        key = (document.source_id or document.document_id, document.url or document.document_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(document)
    return result
