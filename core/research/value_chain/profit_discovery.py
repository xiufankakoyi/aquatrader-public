from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.research.control_plane.contracts import (
    Claim,
    ClaimStatus,
    DocumentVersion,
    EvidenceRecord,
    ResearchTask,
    SourceTrace,
    TaskStatus,
    utc_now,
)
from core.research.control_plane.repository import ResearchRepository

from .operational_facts import (
    flatten_operating_tables,
    parse_forward_plan_indicators,
    parse_operating_tables,
)


def _digest(*parts: Any, length: int = 20) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:length]


def _canonical_url(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_of_date(value: str):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


def load_observed_operating_facts(
    repository: ResearchRepository,
    *,
    symbol: str,
    as_of: str,
    limit: int = 10_000,
) -> list[dict[str, Any]]:
    """Read point-in-time operating facts from the transactional research memory."""
    output: list[dict[str, Any]] = []
    visible_date = _as_of_date(as_of)
    if visible_date is None:
        return output
    allowed = {
        ClaimStatus.CANDIDATE.value,
        ClaimStatus.SUPPORTED.value,
        ClaimStatus.ACTIVE.value,
        ClaimStatus.TRUSTED.value,
    }
    for claim in repository.list_claims(limit=limit) or []:
        if claim is None or getattr(claim, "subject_id", "") != symbol or getattr(claim, "research_status", "") not in allowed:
            continue
        attributes = _mapping(getattr(claim, "attributes", None))
        if attributes.get("observation_kind") != "historical_disclosed":
            continue
        evidences = repository.list_evidence(claim.claim_id, limit=20) or []
        visible = []
        for evidence in evidences:
            if evidence is None:
                continue
            raw = str(getattr(evidence, "published_at", "") or "")[:10]
            try:
                if raw and datetime.fromisoformat(raw).date() <= visible_date:
                    visible.append(evidence)
            except ValueError:
                continue
        if not visible:
            continue
        evidence = visible[0]
        locator = _mapping(getattr(evidence, "locator", None))
        output.append({
            "claim_id": claim.claim_id,
            "source_evidence_id": evidence.evidence_id,
            "product_name": getattr(claim, "object_name", ""),
            "period": str(attributes.get("period") or ""),
            "metric": str(attributes.get("metric") or ""),
            "value": attributes.get("value"),
            "unit": str(attributes.get("unit") or "unknown"),
            "published_at": evidence.published_at,
            "source_url": str(locator.get("url") or ""),
            "observation_kind": "historical_disclosed",
            "eligible_as_forward_assumption": False,
        })
    return sorted(output, key=lambda item: (item["period"], item["product_name"], item["metric"]))


def load_forward_disclosed_indicators(
    repository: ResearchRepository, *, symbol: str, as_of: str, limit: int = 10_000
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    visible_date = _as_of_date(as_of)
    if visible_date is None:
        return output
    for claim in repository.list_claims(limit=limit) or []:
        if claim is None:
            continue
        attributes = _mapping(getattr(claim, "attributes", None))
        if (
            getattr(claim, "subject_id", "") != symbol
            or attributes.get("observation_kind") != "company_forward_plan_qualitative"
        ):
            continue
        visible = []
        for evidence in repository.list_evidence(claim.claim_id, limit=20) or []:
            if evidence is None:
                continue
            try:
                if evidence.published_at and datetime.fromisoformat(
                    evidence.published_at[:10]
                ).date() <= visible_date:
                    visible.append(evidence)
            except ValueError:
                continue
        if not visible:
            continue
        evidence = visible[0]
        locator = _mapping(getattr(evidence, "locator", None))
        output.append({
            **attributes,
            "claim_id": claim.claim_id,
            "source_evidence_id": evidence.evidence_id,
            "published_at": evidence.published_at,
            "source_url": str(locator.get("url") or ""),
        })
    return sorted(output, key=lambda item: (str(item.get("period") or ""), str(item.get("metric") or "")))


class ProfitBridgeDiscoveryExecutor:
    """Extract disclosed historical operating facts without inventing a forecast."""

    def __init__(self, repository: ResearchRepository, retriever: Any) -> None:
        self.repository = repository
        self.retriever = retriever

    def _persist_document(self, document: Any) -> tuple[DocumentVersion, SourceTrace]:
        url = _canonical_url(str(getattr(document, "url", "") or ""))
        content = str(getattr(document, "content_text", "") or getattr(document, "content", "") or "")
        published_at = str(getattr(document, "published_at", "") or "")
        retrieved_at = str(getattr(document, "retrieved_at", "") or utc_now())
        lineage = _digest(url, length=20)
        trace = SourceTrace(
            source_trace_id=f"src_profit_{lineage}",
            original_source=url,
            canonical_url=url,
            lineage_id=lineage,
            provider=urlsplit(url).netloc or "cninfo",
            source_type="financial_report",
            availability_status="ok",
            retrieved_at=retrieved_at,
            metadata=dict(getattr(document, "metadata", {}) or {}),
        )
        document_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        version = DocumentVersion(
            document_version_id=f"dv_profit_{document_hash[:20]}",
            document_id=f"doc_profit_{lineage}",
            title=str(getattr(document, "title", "") or ""),
            content_text=content,
            source_trace_id=trace.source_trace_id,
            published_at=published_at,
            retrieved_at=retrieved_at,
            parse_status="ok",
            metadata=dict(getattr(document, "metadata", {}) or {}),
        )
        self.repository.save_source_trace(trace)
        self.repository.save_document(version)
        return version, trace

    def execute(self, task: ResearchTask) -> dict[str, Any]:
        if str(task.params.get("gap") or "") not in {
            "forward_profit_bridge_missing", "company_forward_driver_evidence_missing",
        }:
            return {"task_id": task.task_id, "status": task.status, "reason": "unsupported_gap"}
        symbol = str(task.params.get("target_entity") or "")
        company = str(task.params.get("company_name") or symbol)
        as_of = str(task.params.get("as_of") or datetime.now().date().isoformat())[:10]
        task.status = TaskStatus.RUNNING.value
        task.started_at = task.started_at or utc_now()
        self.repository.save_task(task)
        historical_queries = [
            f"{company} {symbol.split('.')[0]} 产能 产量 销量 毛利率 年报",
            f"{company} {symbol.split('.')[0]} 生产经营 主要产品 年报",
        ]
        forward_queries = [f"{company} {symbol.split('.')[0]} 年度工作计划 交付能力 出货量"]
        documents: list[Any] = []
        seen: set[str] = set()
        searches = [
            (
                query,
                {"symbol": symbol, "company_name": company, "discovery_terms": [
                    "产能", "产量", "销量", "毛利率",
                ], "periodic_only": True},
            )
            for query in historical_queries
        ] + [
            (
                query,
                {"symbol": symbol, "company_name": company, "discovery_terms": [
                    "年度工作计划",
                ], "periodic_only": True},
            )
            for query in forward_queries
        ]
        for query, context in searches:
            search_with_context = getattr(self.retriever, "search_with_context", None)
            rows = (
                search_with_context(query, tier="profit_bridge", limit=3, context=context)
                if callable(search_with_context)
                else self.retriever.search(query, tier="profit_bridge", limit=3)
            )
            for document in rows or []:
                content = str(getattr(document, "content_text", "") or "")
                key = _digest(getattr(document, "url", ""), content)
                if key in seen:
                    continue
                seen.add(key)
                published = str(getattr(document, "published_at", "") or "")[:10]
                try:
                    if not published or datetime.fromisoformat(published).date() > datetime.fromisoformat(as_of).date():
                        continue
                except ValueError:
                    continue
                documents.append(document)

        saved_claims: list[str] = []
        rejected_documents = 0
        for document in documents:
            content = str(getattr(document, "content_text", "") or getattr(document, "content", "") or "")
            tables = parse_operating_tables(content)
            plans = parse_forward_plan_indicators(content)
            if not tables and not plans:
                rejected_documents += 1
                continue
            version, trace = self._persist_document(document)
            published_at = str(getattr(document, "published_at", "") or "")
            for fact in flatten_operating_tables(tables):
                claim_id = "claim_operating_" + _digest(
                    symbol, fact["product_name"], fact["period"], fact["metric"]
                )
                claim = self.repository.get_claim(claim_id) or Claim(
                    claim_id=claim_id,
                    subject_id=symbol,
                    subject_name=company,
                    relation=f"OBSERVED_{str(fact['metric']).upper()}",
                    object_name=str(fact["product_name"]),
                    valid_from=str(fact["period"]),
                    valid_to=str(fact["period"]),
                    research_status=ClaimStatus.DISCOVERED.value,
                    attributes={
                        "period": fact["period"],
                        "metric": fact["metric"],
                        "value": fact["value"],
                        "unit": fact["unit"],
                        "observation_kind": "historical_disclosed",
                        "eligible_as_forward_assumption": False,
                        "discovery_task_id": task.task_id,
                    },
                )
                if self.repository.get_claim(claim_id) is None:
                    self.repository.save_claim(claim)
                quote = str(fact["quote"])
                evidence_id = "ev_operating_" + _digest(claim_id, trace.lineage_id, quote, length=16)
                evidence = EvidenceRecord(
                    evidence_id=evidence_id,
                    claim_id=claim_id,
                    document_version_id=version.document_version_id,
                    source_trace_id=trace.source_trace_id,
                    lineage_id=trace.lineage_id,
                    stance="support",
                    evidence_kind="disclosed_operating_fact",
                    quote=quote,
                    locator={
                        "url": trace.canonical_url,
                        "char_start": content.find(quote),
                        "char_end": content.find(quote) + len(quote),
                    },
                    source_quality=1.0,
                    match_score=1.0,
                    published_at=published_at,
                    retrieved_at=version.retrieved_at,
                    extraction_method="structured_annual_report_table_v1",
                    grounding_status="exact",
                    is_first_party=True,
                    metadata={
                        "metric": fact["metric"],
                        "period": fact["period"],
                        "eligible_as_forward_assumption": False,
                    },
                )
                if not self.repository.save_evidence(evidence):
                    continue
                current = self.repository.get_claim(claim_id)
                if current and current.research_status == ClaimStatus.DISCOVERED.value:
                    current.research_status = ClaimStatus.CANDIDATE.value
                    current.evidence_confidence = 1.0
                    self.repository.save_claim(current)
                saved_claims.append(claim_id)

            for indicator in plans:
                object_name = "、".join(indicator.get("products") or []) or "高端产品"
                claim_id = "claim_forward_plan_" + _digest(
                    symbol, indicator["period"], indicator["metric"], object_name
                )
                claim = self.repository.get_claim(claim_id) or Claim(
                    claim_id=claim_id,
                    subject_id=symbol,
                    subject_name=company,
                    relation="FORWARD_DELIVERY_AND_SHIPMENT_PLAN",
                    object_name=object_name,
                    valid_from=str(indicator["period"]),
                    valid_to=str(indicator["period"]),
                    research_status=ClaimStatus.DISCOVERED.value,
                    attributes={
                        key: value for key, value in indicator.items() if key != "quote"
                    } | {"discovery_task_id": task.task_id},
                )
                if self.repository.get_claim(claim_id) is None:
                    self.repository.save_claim(claim)
                quote = str(indicator["quote"])
                evidence_id = "ev_forward_plan_" + _digest(
                    claim_id, trace.lineage_id, quote, length=16
                )
                evidence = EvidenceRecord(
                    evidence_id=evidence_id,
                    claim_id=claim_id,
                    document_version_id=version.document_version_id,
                    source_trace_id=trace.source_trace_id,
                    lineage_id=trace.lineage_id,
                    stance="support",
                    evidence_kind="disclosed_forward_plan_qualitative",
                    quote=quote,
                    locator={
                        "url": trace.canonical_url,
                        "char_start": content.find(quote),
                        "char_end": content.find(quote) + len(quote),
                    },
                    source_quality=1.0,
                    match_score=1.0,
                    published_at=published_at,
                    retrieved_at=version.retrieved_at,
                    extraction_method="explicit_management_plan_rule_v1",
                    grounding_status="exact",
                    is_first_party=True,
                    metadata={
                        "metric": indicator["metric"],
                        "period": indicator["period"],
                        "eligible_as_forward_assumption": False,
                    },
                )
                if not self.repository.save_evidence(evidence):
                    continue
                current = self.repository.get_claim(claim_id)
                if current and current.research_status == ClaimStatus.DISCOVERED.value:
                    current.research_status = ClaimStatus.CANDIDATE.value
                    current.evidence_confidence = 1.0
                    self.repository.save_claim(current)
                saved_claims.append(claim_id)

        facts = load_observed_operating_facts(self.repository, symbol=symbol, as_of=as_of)
        disclosed_indicators = load_forward_disclosed_indicators(
            self.repository, symbol=symbol, as_of=as_of
        )
        task.status = TaskStatus.PARTIAL.value
        task.finished_at = utc_now()
        task.failure = {
            "code": "FORWARD_ASSUMPTIONS_STILL_MISSING",
            "message": "已保存可定位的历史经营事实；未来产能、价格、销量/份额和成本假设仍需新证据。",
        }
        task.progress = {
            "percent": 100,
            "message": "历史经营事实已入记忆，前瞻利润桥保持阻断",
            "documents_evaluated": len(documents),
            "documents_rejected": rejected_documents,
            "claims_saved": len(set(saved_claims)),
            "observed_facts_visible": len(facts),
            "forward_assumptions_saved": 0,
            "forward_disclosed_indicators": len(disclosed_indicators),
        }
        self.repository.save_task(task)
        return {
            "task_id": task.task_id,
            "status": task.status,
            "claim_ids": sorted(set(saved_claims)),
            "observed_fact_count": len(facts),
            "forward_assumption_count": 0,
            "forward_disclosed_indicator_count": len(disclosed_indicators),
            "reason": "forward_assumptions_still_missing",
        }


# ---------------------------------------------------------------------------
# P1.3 桥接：从 observed_facts / forward_disclosed 构建经营驱动模型
# ---------------------------------------------------------------------------

# P1.3 引入新引擎（延迟导入避免循环依赖）
try:
    from core.research.operating_model.contracts import (
        OperatingModel as _P13OperatingModel,
    )
    from core.research.operating_model.driver_builder import (
        build_operating_model as _p13_build_operating_model,
    )
    _P13_AVAILABLE = True
except ImportError:  # pragma: no cover
    _P13_AVAILABLE = False


def build_operating_model_from_research_memory(
    repository: ResearchRepository,
    *,
    symbol: str,
    period: str,
    business_model: str,
    as_of: str,
    company_revenue: float | None = None,
    currency: str = "CNY",
    unit: str = "元",
) -> "_P13OperatingModel":
    """从研究记忆中的 observed_facts / forward_disclosed 构建经营驱动模型。

    按 P1.3 规则：
    - 历史事实只能入 drivers（observation_kind=historical_disclosed），
      不得自动成为未来假设
    - 前瞻披露需 source_ref，定性词转数值阻断为 gap
    - 多产品共享收入基数检测
    - 单位/币种/期间一致性校验

    Args:
        repository: 研究仓储
        symbol: 公司股票代码
        period: 模型基准期间 YYYY-MM-DD
        business_model: 商业模式（manufacturing / software_service / cyclical）
        as_of: PIT 过滤时点
        company_revenue: 公司总收入（用于检测重复收入基数）
        currency: 统一币种
        unit: 统一单位

    Returns:
        OperatingModel 实例

    Raises:
        RuntimeError: 当 P1.3 引擎不可用
    """
    if not _P13_AVAILABLE:
        raise RuntimeError("P1.3 引擎不可用，请检查 core/research/operating_model 包")
    observed_facts = load_observed_operating_facts(
        repository, symbol=symbol, as_of=as_of,
    )
    forward_disclosed = load_forward_disclosed_indicators(
        repository, symbol=symbol, as_of=as_of,
    )
    return _p13_build_operating_model(
        symbol=symbol,
        period=period,
        business_model=business_model,
        observed_facts=observed_facts,
        forward_disclosed=forward_disclosed,
        company_revenue=company_revenue,
        currency=currency,
        unit=unit,
        source_trace_id=f"profit_discovery:{symbol}:{as_of}",
    )
