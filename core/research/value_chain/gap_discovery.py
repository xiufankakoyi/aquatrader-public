from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.research.control_plane.contracts import (
    Claim, ClaimStatus, DocumentVersion, EvidenceRecord, ResearchTask,
    SourceTrace, TaskStatus, utc_now,
)
from core.research.control_plane.perilla_adapter import GroundedEvidenceExtractor
from core.research.control_plane.repository import ResearchRepository
from core.research_graph.retrieval_hypothesis import RetrievalHypothesis
from core.llm_processing.extraction_schema import LLMExtractionOutput
from core.utils.llm_usage import call_llm_with_budget, load_llm_providers


@dataclass
class FocusedRelationStats:
    llm_call_count: int = 0
    success: int = 0
    failure: int = 0


class FocusedRelationExtractor:
    """Small, strict LLM contract for discovering company causal relations."""

    def __init__(self, caller: Any = call_llm_with_budget) -> None:
        self.caller = caller
        self.available = caller is not call_llm_with_budget or bool(load_llm_providers())
        self.stats = FocusedRelationStats()

    def extract(
        self,
        chunk_text: str,
        source_text: str,
        metadata: dict[str, str],
    ) -> tuple[LLMExtractionOutput | None, list[Any]]:
        company = str(metadata.get("stock_name") or "")
        symbol = str(metadata.get("symbol") or "")
        rule_relations = self._rule_relations(source_text, company)
        if not self.available:
            return LLMExtractionOutput(extracted_relations=rule_relations), []
        system = (
            "你是产业研究关系抽取器。只抽取原文明确陈述、且公司为主语的有向关系。"
            "关系类型只允许 SUPPLIES_TO、PRODUCES、DEVELOPS、USES、REQUIRES、CUSTOMER_OF、BENEFITS_FROM。"
            "evidence_text 必须是输入中逐字连续原文，禁止改写、推断、行业泛化或使用搜索摘要。"
            "返回 JSON：{\"extracted_relations\":[{\"from_entity\":\"\",\"relation_type\":\"\","
            "\"to_entity\":\"\",\"evidence_text\":\"\",\"confidence\":\"high|medium|low\"}]}。"
            "没有合格关系时返回空数组。"
        )
        user = f"公司：{company}（{symbol}）\n标题：{metadata.get('title', '')}\n原文：\n{chunk_text}"
        self.stats.llm_call_count += 1
        try:
            raw = self.caller(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                source="research.value_chain.gap_discovery",
                temperature=0.0,
                max_completion_tokens=1200,
                response_format_json=True,
                timeout=45,
                tier="flash",
            )
            text = str(raw or "").strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
            parsed = json.loads(text)
            relations = parsed.get("extracted_relations", []) if isinstance(parsed, dict) else []
            if not isinstance(relations, list):
                relations = []
            relations.extend(rule_relations)
            deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
            for item in relations:
                if not isinstance(item, dict):
                    continue
                key = (
                    str(item.get("relation_type") or ""),
                    str(item.get("to_entity") or ""),
                    str(item.get("evidence_text") or ""),
                )
                deduped[key] = item
            self.stats.success += 1
            return LLMExtractionOutput(extracted_relations=list(deduped.values())), []
        except Exception:
            self.stats.failure += 1
            return LLMExtractionOutput(extracted_relations=rule_relations), []

    @staticmethod
    def _rule_relations(source_text: str, company: str) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        pattern = re.compile(
            r"公司主营业务为(?P<object>[^，。；\n]{2,60}?)的研发、生产及销售"
        )
        for match in pattern.finditer(str(source_text or "")):
            object_name = match.group("object").strip()
            quote = match.group(0)
            for relation in ("DEVELOPS", "PRODUCES"):
                relations.append({
                    "from_entity": company,
                    "relation_type": relation,
                    "to_entity": object_name,
                    "evidence_text": quote,
                    "confidence": "high",
                    "extraction_method": "explicit_annual_report_rule_v1",
                })
        downstream_pattern = re.compile(
            r"产品服务于(?P<object>[^。「」：:]{4,220}?领域的国内外客户)",
            flags=re.DOTALL,
        )
        for match in downstream_pattern.finditer(str(source_text or "")):
            raw_object = match.group("object").strip()
            relations.append({
                "from_entity": company,
                "relation_type": "SUPPLIES_TO",
                "to_entity": re.sub(r"\s+", "", raw_object),
                "object_aliases": [raw_object],
                "evidence_text": match.group(0),
                "confidence": "high",
                "extraction_method": "explicit_customer_domain_rule_v1",
            })
        demand_pattern = re.compile(
            r"受益于(?P<object>[^，。]{4,140}?投入)，公司产品出货较快增长",
            flags=re.DOTALL,
        )
        for match in demand_pattern.finditer(str(source_text or "")):
            raw_object = match.group("object").strip()
            relations.append({
                "from_entity": company,
                "relation_type": "BENEFITS_FROM",
                "to_entity": re.sub(r"\s+", "", raw_object),
                "object_aliases": [raw_object],
                "evidence_text": match.group(0),
                "confidence": "high",
                "extraction_method": "explicit_demand_driver_rule_v1",
            })
        upstream_pattern = re.compile(
            r"(?P<quote>光模块行业的上游主要(?:是|包括)(?P<objects>[^。]{4,120})。)"
        )
        for match in upstream_pattern.finditer(str(source_text or "")):
            objects = re.split(r"[、，,]|和|及", match.group("objects"))
            for object_name in objects:
                normalized = re.sub(r"\s+", "", object_name).strip("等")
                if not normalized:
                    continue
                relations.append({
                    "from_entity": "光模块",
                    "relation_type": "REQUIRES",
                    "to_entity": normalized,
                    "evidence_text": match.group("quote"),
                    "confidence": "high",
                    "industry_ontology": True,
                    "extraction_method": "explicit_industry_upstream_rule_v1",
                })
        return relations


class GapDiscoveryRetriever:
    """Use first-party reports first, then fill the bounded result set from web."""

    def __init__(self, first_party: Any, web: Any, context: dict[str, Any]) -> None:
        self.first_party = first_party
        self.web = web
        self.context = context

    def search(self, query: str, *, tier: str, limit: int) -> list[Any]:
        return self.search_with_context(query, tier=tier, limit=limit, context=self.context)

    def search_with_context(
        self, query: str, *, tier: str, limit: int, context: dict[str, Any]
    ) -> list[Any]:
        cninfo = getattr(self.first_party, "cninfo_pdf_retriever", None)
        if cninfo is not None:
            documents = list(cninfo.search(
                query, tier="first_party", limit=limit, context=context,
            ) or [])
        else:
            first_result = self.first_party.search(
                query, tier="first_party", limit=limit, context=context,
            )
            documents = list(getattr(first_result, "documents", first_result) or [])
        if len(documents) < limit:
            documents.extend(self.web.search(query, tier=tier, limit=limit - len(documents)) or [])
        seen: set[str] = set()
        output: list[Any] = []
        for document in documents:
            key = str(getattr(document, "url", "") or id(document))
            if key in seen:
                continue
            seen.add(key)
            output.append(document)
            if len(output) >= limit:
                break
        return output


class GapDiscoveryExecutor:
    """Discover concrete causal Claims before handing them to the evidence loop."""

    RELATIONS = {
        "SUPPLIES_TO": "SUPPLIES_TO", "供应": "SUPPLIES_TO", "供货": "SUPPLIES_TO",
        "PRODUCES": "PRODUCES", "生产": "PRODUCES", "制造": "PRODUCES",
        "DEVELOPS": "DEVELOPS", "研发": "DEVELOPS", "开发": "DEVELOPS",
        "USES": "USES", "使用": "USES", "采用": "USES",
        "REQUIRES": "REQUIRES", "需要": "REQUIRES", "依赖": "REQUIRES",
        "CUSTOMER_OF": "CUSTOMER_OF", "客户": "CUSTOMER_OF",
        "BENEFITS_FROM": "BENEFITS_FROM", "受益于": "BENEFITS_FROM",
    }

    def __init__(
        self,
        repository: ResearchRepository,
        retriever: Any,
        relation_extractor: Any,
        evidence_extractor: Any | None = None,
    ) -> None:
        self.repository = repository
        self.retriever = retriever
        self.relation_extractor = relation_extractor
        self.evidence_extractor = evidence_extractor or GroundedEvidenceExtractor(repository)

    @staticmethod
    def _key(value: Any) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fa5]+", "", str(value or "").lower())

    @classmethod
    def _relation(cls, value: Any) -> str:
        raw = str(value or "").strip()
        if raw.upper() in cls.RELATIONS:
            return cls.RELATIONS[raw.upper()]
        return next((mapped for token, mapped in cls.RELATIONS.items() if token in raw), "")

    @staticmethod
    def _visible(document: Any, as_of: str) -> bool:
        try:
            cutoff = datetime.fromisoformat(str(as_of)[:10]).date()
        except (TypeError, ValueError):
            return False
        raw = str(getattr(document, "published_at", "") or "")[:10]
        if not raw:
            return cutoff >= datetime.now().date()
        try:
            return datetime.fromisoformat(raw).date() <= cutoff
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _claim_id(symbol: str, relation: str, object_name: str) -> str:
        digest = hashlib.sha256(f"{symbol}|{relation}|{object_name}".encode("utf-8")).hexdigest()[:20]
        return f"claim_discovery_{digest}"

    def _save_ontology_relation(
        self,
        *,
        document: Any,
        task: ResearchTask,
        subject: str,
        relation: str,
        object_name: str,
        quote: str,
    ) -> str:
        text = str(getattr(document, "content_text", "") or getattr(document, "content", "") or "")
        url = str(getattr(document, "url", "") or "")
        lineage = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        trace = SourceTrace(
            source_trace_id=f"src_ontology_{lineage}",
            original_source=url,
            canonical_url=url,
            lineage_id=lineage,
            provider="cninfo.com.cn" if "cninfo.com.cn" in url else "company_report",
            source_type="financial_report",
            availability_status="ok",
            retrieved_at=str(getattr(document, "retrieved_at", "") or getattr(document, "fetched_at", "") or utc_now()),
            metadata=dict(getattr(document, "metadata", {}) or {}),
        )
        document_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        version = DocumentVersion(
            document_version_id=f"dv_ontology_{document_hash[:20]}",
            document_id=f"doc_ontology_{lineage}",
            title=str(getattr(document, "title", "") or ""),
            content_text=text,
            source_trace_id=trace.source_trace_id,
            published_at=str(getattr(document, "published_at", "") or ""),
            retrieved_at=trace.retrieved_at,
            parse_status="ok",
            metadata=dict(getattr(document, "metadata", {}) or {}),
        )
        self.repository.save_source_trace(trace)
        self.repository.save_document(version)
        claim_id = self._claim_id(f"ontology:{subject}", relation, object_name)
        claim = self.repository.get_claim(claim_id) or Claim(
            claim_id=claim_id,
            subject_id=f"ontology:{self._key(subject)}",
            subject_name=subject,
            relation=relation,
            object_name=object_name,
            direction="forward",
            research_status=ClaimStatus.DISCOVERED.value,
            attributes={
                "scope": "industry_ontology",
                "company_specific": False,
                "discovery_task_id": task.task_id,
            },
        )
        if self.repository.get_claim(claim_id) is None:
            self.repository.save_claim(claim)
        evidence = EvidenceRecord(
            evidence_id="ev_ontology_" + hashlib.sha1(
                f"{claim_id}|{lineage}|{quote}".encode("utf-8")
            ).hexdigest()[:16],
            claim_id=claim_id,
            document_version_id=version.document_version_id,
            source_trace_id=trace.source_trace_id,
            lineage_id=lineage,
            stance="support",
            evidence_kind="industry_ontology_statement",
            quote=quote,
            locator={
                "url": url,
                "char_start": text.find(quote),
                "char_end": text.find(quote) + len(quote),
            },
            source_quality=0.8,
            match_score=1.0,
            published_at=version.published_at,
            retrieved_at=version.retrieved_at,
            extraction_method="explicit_industry_ontology_rule_v1",
            grounding_status="exact",
            is_first_party=False,
            metadata={
                "claim_scope": "industry_ontology",
                "company_specific": False,
                "can_directly_score_company": False,
            },
        )
        if not self.repository.save_evidence(evidence):
            return ""
        current = self.repository.get_claim(claim_id)
        if current and current.research_status == ClaimStatus.DISCOVERED.value:
            current.research_status = ClaimStatus.CANDIDATE.value
            current.evidence_confidence = 0.8
            self.repository.save_claim(current)
        return claim_id

    def execute(self, task: ResearchTask) -> dict[str, Any]:
        gap = str(task.params.get("gap") or "")
        if gap not in {"verified_causal_chain_missing", "causal_chain_depth_insufficient"}:
            return {"task_id": task.task_id, "status": task.status, "claim_ids": [], "reason": "unsupported_gap"}
        symbol = str(task.params.get("target_entity") or "")
        company = str(task.params.get("company_name") or symbol)
        as_of = str(task.params.get("as_of") or datetime.now().date().isoformat())[:10]
        queries = [
            f"{company} {symbol.split('.')[0]} 主要产品 客户 供应 公告",
            f"{company} {symbol.split('.')[0]} 产能 生产 研发 年报",
            f"{company} {symbol.split('.')[0]} 受益于 算力基础设施 出货 年报",
        ]
        hypotheses = [
            RetrievalHypothesis(
                hypothesis_id="rh_" + hashlib.sha1(query.encode("utf-8")).hexdigest()[:16],
                candidate_id=symbol,
                query_prototype=query,
                reason="verified_causal_chain_missing",
                generated_by="gap_discovery_v1",
            ).to_slot_safe_dict()
            for query in queries
        ]
        task.params = {**task.params, "retrieval_hypotheses": hypotheses}
        task.status = TaskStatus.RUNNING.value
        task.started_at = task.started_at or utc_now()
        self.repository.save_task(task)

        max_documents = max(1, int((task.budget or {}).get("max_documents", 6)))
        documents: list[Any] = []
        seen: set[str] = set()
        default_requests = 3 if gap == "causal_chain_depth_insufficient" else 2
        for query in queries[: max(1, int((task.budget or {}).get("max_requests", default_requests)))]:
            search_with_context = getattr(self.retriever, "search_with_context", None)
            if callable(search_with_context) and "受益于" in query:
                rows = search_with_context(
                    query, tier="gap_discovery", limit=3,
                    context={
                        "symbol": symbol, "company_name": company,
                        "discovery_terms": ["受益于", "算力基础设施", "出货较快增长"],
                        "periodic_only": True,
                    },
                )
            else:
                rows = self.retriever.search(query, tier="gap_discovery", limit=3)
            for document in rows or []:
                content_key = str(
                    getattr(document, "content_text", "")
                    or getattr(document, "content", "") or ""
                )
                key = hashlib.sha1(
                    f"{getattr(document, 'url', '')}|{content_key}".encode("utf-8")
                ).hexdigest()
                if key in seen or not self._visible(document, as_of):
                    continue
                if bool((getattr(document, "metadata", {}) or {}).get("snippet_only")):
                    continue
                seen.add(key)
                documents.append(document)
                if len(documents) >= max_documents:
                    break
            if len(documents) >= max_documents:
                break

        claim_ids: list[str] = []
        evidence_count = 0
        relations_extracted = 0
        relations_rejected = 0
        relation_audit: list[dict[str, Any]] = []
        for document in documents:
            text = str(getattr(document, "content_text", "") or getattr(document, "content", "") or "")
            if not text.strip():
                continue
            extraction, _ = self.relation_extractor.extract(
                chunk_text=text[:8000], source_text=text,
                metadata={"symbol": symbol, "stock_name": company, "title": str(getattr(document, "title", "") or "")},
            )
            for relation_item in list(getattr(extraction, "extracted_relations", []) or []):
                relations_extracted += 1
                if not isinstance(relation_item, dict):
                    relations_rejected += 1
                    relation_audit.append({"decision": "rejected", "reason": "invalid_relation_shape"})
                    continue
                subject = str(relation_item.get("from_entity") or "").strip()
                object_name = str(relation_item.get("to_entity") or "").strip()
                relation = self._relation(relation_item.get("relation_type"))
                quote = str(relation_item.get("evidence_text") or "").strip()
                audit = {
                    "subject": subject,
                    "relation": str(relation_item.get("relation_type") or ""),
                    "normalized_relation": relation,
                    "object": object_name,
                    "quote": quote[:300],
                    "document_url": str(getattr(document, "url", "") or ""),
                }
                if not relation or not object_name or quote not in text:
                    relations_rejected += 1
                    reason = "unsupported_relation" if not relation else "missing_object" if not object_name else "quote_not_exact"
                    relation_audit.append({**audit, "decision": "rejected", "reason": reason})
                    continue
                if bool(relation_item.get("industry_ontology")):
                    claim_id = self._save_ontology_relation(
                        document=document, task=task, subject=subject,
                        relation=relation, object_name=object_name, quote=quote,
                    )
                    if not claim_id:
                        relations_rejected += 1
                        relation_audit.append({**audit, "decision": "rejected", "reason": "ontology_evidence_save_rejected"})
                        continue
                    evidence_count += 1
                    if claim_id not in claim_ids:
                        claim_ids.append(claim_id)
                    relation_audit.append({
                        **audit, "decision": "accepted", "reason": "grounded_industry_ontology_saved",
                        "claim_id": claim_id,
                    })
                    continue
                if self._key(company) not in self._key(subject) and self._key(subject) not in self._key(company):
                    relations_rejected += 1
                    relation_audit.append({**audit, "decision": "rejected", "reason": "company_subject_mismatch"})
                    continue
                claim_id = self._claim_id(symbol, relation, object_name)
                claim = self.repository.get_claim(claim_id) or Claim(
                    claim_id=claim_id,
                    subject_id=symbol,
                    subject_name=company,
                    relation=relation,
                    object_name=object_name,
                    direction="forward",
                    research_status=ClaimStatus.DISCOVERED.value,
                    support_terms=[f"{company} {object_name} {relation}", quote],
                    counter_terms=[f"{company} 不涉及 {object_name}", f"{company} 否认 {object_name}"],
                    attributes={
                        "discovery_task_id": task.task_id,
                        "retrieval_hypotheses": hypotheses,
                        "generated_claim_candidate": True,
                        "object_aliases": list(relation_item.get("object_aliases") or []),
                    },
                )
                evidence = self.evidence_extractor.extract(document, claim, query_purpose="support")
                if not evidence:
                    relations_rejected += 1
                    relation_audit.append({**audit, "decision": "rejected", "reason": "relationship_grounding_failed"})
                    continue
                if self.repository.get_claim(claim_id) is None:
                    self.repository.save_claim(claim)
                saved = [item for item in evidence if self.repository.save_evidence(item)]
                if not saved:
                    relations_rejected += 1
                    relation_audit.append({**audit, "decision": "rejected", "reason": "evidence_save_rejected"})
                    continue
                evidence_count += len(saved)
                if claim.research_status == ClaimStatus.DISCOVERED.value:
                    claim.research_status = ClaimStatus.CANDIDATE.value
                    claim.evidence_confidence = max(
                        float(item.source_quality) * float(item.match_score) for item in saved
                    )
                    self.repository.save_claim(claim)
                if claim_id not in claim_ids:
                    claim_ids.append(claim_id)
                relation_audit.append({**audit, "decision": "accepted", "reason": "grounded_evidence_saved", "claim_id": claim_id})

        task.status = TaskStatus.SUCCEEDED.value if claim_ids else TaskStatus.PARTIAL.value
        task.finished_at = utc_now()
        task.failure = {} if claim_ids else {
            "code": "NO_GROUNDED_RELATION",
            "message": "检索已执行，但未抽取到可定位产业关系",
        }
        task.progress = {
            "percent": 100,
            "message": "认知缺口发现完成",
            "documents_evaluated": len(documents),
            "claims_created": len(claim_ids),
            "evidence_saved": evidence_count,
            "relations_extracted": relations_extracted,
            "relations_rejected": relations_rejected,
            "relation_audit": relation_audit[:20],
            "documents": [{
                "title": str(getattr(document, "title", "") or "")[:200],
                "url": str(getattr(document, "url", "") or ""),
                "source_type": str(getattr(document, "source_type", "") or ""),
                "published_at": str(getattr(document, "published_at", "") or ""),
                "body_chars": len(str(getattr(document, "content_text", "") or "")),
            } for document in documents],
        }
        self.repository.save_task(task)
        return {
            "task_id": task.task_id,
            "status": task.status,
            "claim_ids": claim_ids,
            "documents_evaluated": len(documents),
            "evidence_saved": evidence_count,
            "relations_extracted": relations_extracted,
            "relations_rejected": relations_rejected,
        }
