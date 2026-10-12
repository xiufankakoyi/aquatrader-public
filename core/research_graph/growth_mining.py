from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from core.research_graph.neuron.models import GrowthPulse


ALLOWED_RUN_STATUSES = {"CANDIDATE", "REJECTED", "BLOCKED", "NEEDS_MORE_EVIDENCE", "NEEDS_REVIEW", "CONFLICT_RISK"}
FINAL_STATUSES = {"ACTIVE", "PROVEN_FINAL", "VERIFIED_FINAL"}
CONCRETE_NODE_TYPES = {"PRODUCT", "PROCESS", "MATERIAL", "EQUIPMENT"}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class GrowthMiningRequest:
    topic: str = ""
    root_theme: str = ""
    node: str = ""
    company: str = ""
    symbol: str = ""
    min_evidence: int = 1
    live_mode: bool = False
    use_system_search: bool = False
    allow_llm_assist: bool = False
    use_vector_memory: bool = True
    vector_memory_path: Path = Path("data/vector_store/research_chunks.lance")
    apply_candidate: bool = False
    node_aliases: list[str] = field(default_factory=list)
    acceptance_profile_path: Path = Path("data/config/research_graph_growth_acceptance.json")
    run_root: Path = Path("data/research_runs")
    run_name: str = ""
    max_search_results: int = 8

    @classmethod
    def from_pulse(cls, pulse: "GrowthPulse", **overrides: Any) -> "GrowthMiningRequest":
        """从 GrowthPulse 构造 GrowthMiningRequest。

        字段优先级:overrides > pulse.payload 显式覆盖 > pulse 顶层字段 > 默认值。
        Plan 4 集成阶段会扩展 expected_node_types → 节点候选等高级映射。

        Args:
            pulse: ResearchNeuron.growth_cycle() 发出的 GrowthPulse。
            **overrides: 覆盖任意 GrowthMiningRequest 字段。

        Returns:
            GrowthMiningRequest 实例。
        """
        payload = dict(pulse.payload or {})
        return cls(
            topic=overrides.get("topic", payload.get("topic", "")),
            root_theme=overrides.get("root_theme", pulse.root_theme or payload.get("root_theme", "")),
            node=overrides.get("node", payload.get("node", "")),
            company=overrides.get("company", payload.get("company", "")),
            symbol=overrides.get("symbol", payload.get("symbol", "")),
            min_evidence=overrides.get("min_evidence", payload.get("min_evidence", 1)),
            live_mode=overrides.get("live_mode", payload.get("live_mode", False)),
            use_system_search=overrides.get("use_system_search", payload.get("use_system_search", False)),
            allow_llm_assist=overrides.get("allow_llm_assist", payload.get("allow_llm_assist", False)),
            use_vector_memory=overrides.get("use_vector_memory", payload.get("use_vector_memory", True)),
            vector_memory_path=Path(overrides.get(
                "vector_memory_path",
                payload.get("vector_memory_path", "data/vector_store/research_chunks.lance"),
            )),
            apply_candidate=overrides.get("apply_candidate", payload.get("apply_candidate", False)),
            node_aliases=overrides.get("node_aliases", payload.get("node_aliases", [])),
            run_name=overrides.get("run_name", payload.get("run_name", "")),
            max_search_results=overrides.get("max_search_results", payload.get("max_search_results", 8)),
        )


@dataclass
class GrowthMiningResult:
    run_dir: Path
    validation_summary: dict[str, Any]
    run_status: str
    support_evidence_count: int


@dataclass
class SourceCandidate:
    provider_name: str
    provider_type: str
    query_or_request: str
    request_id: str
    retrieved_at: str
    title: str = ""
    url: str = ""
    publisher: str = ""
    raw_text: str = ""
    local_path: str = ""
    record_id: str = ""
    row_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    is_live: bool = False


class GrowthMiningEngine:
    """Build staging-only Growth Agent run artifacts from system sources."""

    def __init__(self, request: GrowthMiningRequest) -> None:
        self.request = request
        self.profile = self._load_selected_profile(request)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = request.run_name or f"growth_mining_{timestamp}"
        self.run_dir = request.run_root / run_name
        self.raw_dir = self.run_dir / "raw_documents"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> GrowthMiningResult:
        sources: list[dict[str, Any]] = []
        failed_sources: list[dict[str, Any]] = []
        extracted_evidence: list[dict[str, Any]] = []

        candidates: list[SourceCandidate] = []
        candidates.extend(self._collect_vector_memory_candidates(failed_sources))
        if self.request.use_system_search:
            candidates.extend(self._collect_live_candidates(failed_sources))
        candidates.extend(self._collect_local_candidates(failed_sources))

        # 候选漏斗:SimHash 去重 + 朴素贝叶斯分类 + 规则排序
        # 把候选分为 must_read / skim / skip,LLM 只处理 must_read
        from core.research_graph.candidate_funnel import funnel_candidates

        node_aliases = self.request.node_aliases or [
            self.request.node, self.request.topic, self.request.root_theme,
        ]
        node_aliases = [a for a in node_aliases if a]
        bucket = funnel_candidates(
            candidates,
            theme=self.request.topic or self.request.root_theme or "research",
            neuron_aliases=node_aliases,
        )
        filtered_candidates = bucket.must_read + bucket.skim

        seen_hashes: set[str] = set()
        must_read_source_rows: list[dict[str, Any]] = []
        skim_source_rows: list[dict[str, Any]] = []
        for candidate in filtered_candidates:
            source_row = self._persist_source(candidate)
            if source_row["content_hash"] in seen_hashes:
                failed_sources.append({
                    "provider_name": candidate.provider_name,
                    "provider_type": candidate.provider_type,
                    "query_or_request": candidate.query_or_request,
                    "reason": "duplicate_content_hash",
                    "url": candidate.url,
                    "retrieved_at": utc_now_text(),
                })
                continue
            seen_hashes.add(source_row["content_hash"])
            sources.append(source_row)
            self._upsert_vector_memory_source(source_row)
            if candidate in bucket.must_read:
                must_read_source_rows.append(source_row)
            else:
                skim_source_rows.append(source_row)
                # skim 先走规则提取
                evidence = self._extract_evidence(source_row)
                if evidence:
                    extracted_evidence.append(evidence)

        # 介入点 1:LLM 证据抽取增强,只对 must_read 调用
        llm_assist_used = False
        if self.request.allow_llm_assist and must_read_source_rows:
            llm_evidence = self._extract_evidence_llm(must_read_source_rows)
            if llm_evidence:
                extracted_evidence.extend(llm_evidence)
                llm_assist_used = True

        # "must read" is a priority tier, not an LLM-only tier.  When LLM
        # assistance is disabled or unavailable, these highest-value local
        # documents still have to pass through the deterministic extractor;
        # otherwise a strong structured company mapping is silently ignored.
        if must_read_source_rows and not llm_assist_used:
            for source_row in must_read_source_rows:
                evidence = self._extract_evidence(source_row)
                if evidence:
                    extracted_evidence.append(evidence)

        support_evidence = [item for item in extracted_evidence if item.get("stance") == "support"]
        counter_evidence = [item for item in extracted_evidence if item.get("stance") == "counter"]
        raw_evidence = [item for item in extracted_evidence if item.get("stance") != "counter"]
        if support_evidence and counter_evidence:
            run_status = "NEEDS_REVIEW"
        elif len(support_evidence) >= self.request.min_evidence:
            run_status = "CANDIDATE"
        else:
            run_status = "BLOCKED"
        candidate_claims = self._build_candidate_claims(support_evidence, run_status, sources)
        graph_hypotheses = self._build_graph_hypotheses(candidate_claims, run_status, counter_evidence)

        run_options = {
            "topic": self.request.topic,
            "root_theme": self.request.root_theme,
            "node": self.request.node,
            "company": self.request.company,
            "symbol": self.request.symbol,
            "min_evidence": self.request.min_evidence,
            "live_mode": self.request.live_mode,
            "use_system_search": self.request.use_system_search,
            "use_vector_memory": self.request.use_vector_memory,
            "vector_memory_path": str(self.request.vector_memory_path),
            "allow_llm_assist": self.request.allow_llm_assist,
            "llm_assist_used": llm_assist_used,
            "llm_trace_path": "",
            "apply_candidate": self.request.apply_candidate,
            "run_status": run_status,
            "staging_only": not self.request.apply_candidate,
            "selected_profile": self.profile.get("profile_id", ""),
        }

        write_json(self.run_dir / "source_manifest.json", sources)
        write_json(self.run_dir / "raw_evidence.json", raw_evidence)
        write_json(self.run_dir / "candidate_claims.json", candidate_claims)
        write_json(self.run_dir / "counter_evidence.json", counter_evidence)
        write_json(self.run_dir / "graph_hypotheses.json", graph_hypotheses)
        write_json(self.run_dir / "failed_sources.json", failed_sources)
        write_json(self.run_dir / "run_options.json", run_options)
        self._write_readme(run_status, len(support_evidence))

        validator = GrowthArtifactValidator(
            run_dir=self.run_dir,
            request=self.request,
            profile=self.profile,
        )
        validation_summary = validator.validate()
        write_json(self.run_dir / "validation_summary.json", validation_summary)
        return GrowthMiningResult(
            run_dir=self.run_dir,
            validation_summary=validation_summary,
            run_status=run_status,
            support_evidence_count=len(support_evidence),
        )

    @staticmethod
    def _load_selected_profile(request: GrowthMiningRequest) -> dict[str, Any]:
        payload = read_json(request.acceptance_profile_path, {})
        selected: dict[str, Any] = {}
        for profile in payload.get("profiles", []):
            match = profile.get("match", {})
            if not match:
                continue
            checks = {
                "topic": request.topic,
                "root_theme": request.root_theme,
                "node": request.node,
                "company": request.company,
                "symbol": request.symbol,
            }
            if all(not expected or checks.get(key) == expected for key, expected in match.items()):
                selected = profile
                break
        base = payload.get("defaults", {}).copy()
        base.update(selected)
        base["all_forbidden_fixture_terms"] = payload.get("all_forbidden_fixture_terms", [])
        base["forbidden_trading_terms"] = payload.get("forbidden_trading_terms", [])
        return base

    def _build_queries(self) -> list[str]:
        values = {
            "topic": self.request.topic,
            "root_theme": self.request.root_theme,
            "node": self.request.node,
            "company": self.request.company,
            "symbol": self.request.symbol,
        }
        templates = self.profile.get("query_templates") or [
            "{node} {topic}",
            "{node} {root_theme}",
            "{node} {company}",
        ]
        queries: list[str] = []
        for template in templates:
            try:
                query = template.format(**values).strip()
            except KeyError:
                continue
            if query and query not in queries:
                queries.append(query)
        return queries

    def _collect_live_candidates(self, failed_sources: list[dict[str, Any]]) -> list[SourceCandidate]:
        results: list[SourceCandidate] = []
        for query in self._build_queries():
            request_id = short_id("req")
            results.extend(self._baidu_search_candidates(query, request_id, failed_sources))
            results.extend(self._search_discovery_candidates(query, request_id, failed_sources))
            if not self.profile.get("skip_provider_registry_search", False):
                results.extend(self._provider_registry_candidates(query, request_id, failed_sources))

        crawled: list[SourceCandidate] = []
        unique_results: list[SourceCandidate] = []
        seen_locations: set[str] = set()
        for candidate in results:
            location = candidate.url or f"{candidate.provider_name}:{candidate.title}:{candidate.raw_text[:80]}"
            if not location or location in seen_locations:
                continue
            seen_locations.add(location)
            unique_results.append(candidate)
        crawl_limit = max(
            self.request.max_search_results * max(1, len(self._build_queries())),
            self.request.min_evidence * 4,
        )
        for candidate in unique_results[:crawl_limit]:
            if candidate.url:
                crawled_doc = self._crawl_candidate(candidate, failed_sources)
                if crawled_doc:
                    crawled.append(crawled_doc)
                    continue
            if self.profile.get("allow_search_snippet_evidence", False) and candidate.raw_text:
                crawled.append(candidate)
        return crawled

    def _search_discovery_candidates(
        self,
        query: str,
        request_id: str,
        failed_sources: list[dict[str, Any]],
    ) -> list[SourceCandidate]:
        # 优先读环境变量,降级到 Secrets 配置
        base_url = (
            os.getenv("SEARCH_SEARXNG_BASE_URL", "")
            or os.getenv("SEARXNG_BASE_URL", "")
        )
        if not base_url:
            try:
                from config.secrets import Secrets
                base_url = getattr(Secrets, "SEARXNG_BASE_URL", "") \
                    or getattr(Secrets, "SEARCH_SEARXNG_BASE_URL", "")
            except Exception:
                base_url = ""
        if not base_url:
            failed_sources.append({
                "provider_name": "SearchDiscovery",
                "provider_type": "web_search",
                "query_or_request": query,
                "request_id": request_id,
                "retrieved_at": utc_now_text(),
                "reason": "search_base_url_not_configured",
            })
            return []
        try:
            from core.research.perilla.discovery.search_discovery import SearchDiscovery

            searcher = SearchDiscovery(
                searxng_base_url=base_url,
                max_results=self.request.max_search_results,
                enabled=True,
            )
            docs = searcher.search(query)
        except Exception as exc:  # noqa: BLE001
            failed_sources.append({
                "provider_name": "SearchDiscovery",
                "provider_type": "web_search",
                "query_or_request": query,
                "request_id": request_id,
                "retrieved_at": utc_now_text(),
                "reason": f"search_failed: {exc}",
            })
            return []

        candidates: list[SourceCandidate] = []
        for doc in docs:
            candidates.append(SourceCandidate(
                provider_name="SearchDiscovery",
                provider_type="web_search",
                query_or_request=query,
                request_id=request_id,
                retrieved_at=doc.fetched_at or utc_now_text(),
                title=doc.title,
                url=doc.url,
                publisher=doc.site_name,
                raw_text=doc.content_text,
                is_live=True,
                metadata={
                    "source_type": doc.source_type,
                    "credibility_hint": doc.credibility_hint,
                    "content_origin": "search_discovery_document",
                },
            ))
        return candidates

    def _provider_registry_candidates(
        self,
        query: str,
        request_id: str,
        failed_sources: list[dict[str, Any]],
    ) -> list[SourceCandidate]:
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry

            result = ProviderRegistry().semantic_search(query=query, count=self.request.max_search_results)
        except Exception as exc:  # noqa: BLE001
            failed_sources.append({
                "provider_name": "ProviderRegistry",
                "provider_type": "api",
                "query_or_request": query,
                "request_id": request_id,
                "retrieved_at": utc_now_text(),
                "reason": f"semantic_search_failed: {exc}",
            })
            return []
        rows = result.get("results") if isinstance(result, dict) else []
        if not rows:
            failed_sources.append({
                "provider_name": "ProviderRegistry",
                "provider_type": "api",
                "query_or_request": query,
                "request_id": request_id,
                "retrieved_at": utc_now_text(),
                "reason": "empty_search_results",
            })
            return []

        provider_name = str(result.get("source", "provider_registry")) if isinstance(result, dict) else "provider_registry"
        candidates: list[SourceCandidate] = []
        for row in rows[: self.request.max_search_results]:
            title = str(row.get("title") or row.get("name") or row.get("股票简称") or "")
            url = str(row.get("url") or row.get("link") or "")
            snippet = str(row.get("snippet") or row.get("content") or row.get("body") or row)
            candidates.append(SourceCandidate(
                provider_name=provider_name,
                provider_type="api",
                query_or_request=query,
                request_id=request_id,
                retrieved_at=utc_now_text(),
                title=title,
                url=url,
                publisher=urlparse(url).netloc,
                raw_text=snippet,
                is_live=True,
                metadata={"api_row": row, "content_origin": "search_snippet"},
            ))
        return candidates

    def _baidu_search_candidates(
        self,
        query: str,
        request_id: str,
        failed_sources: list[dict[str, Any]],
    ) -> list[SourceCandidate]:
        try:
            from infrastructure.data_providers.research.baidu_search_provider import BaiduSearchProvider

            rows = BaiduSearchProvider().search(query=query, count=self.request.max_search_results)
        except Exception as exc:  # noqa: BLE001
            failed_sources.append({
                "provider_name": "BaiduSearchProvider",
                "provider_type": "api",
                "query_or_request": query,
                "request_id": request_id,
                "retrieved_at": utc_now_text(),
                "reason": f"search_failed: {exc}",
            })
            return []
        if not rows:
            failed_sources.append({
                "provider_name": "BaiduSearchProvider",
                "provider_type": "api",
                "query_or_request": query,
                "request_id": request_id,
                "retrieved_at": utc_now_text(),
                "reason": "empty_search_results",
            })
            return []

        candidates: list[SourceCandidate] = []
        for row in rows[: self.request.max_search_results]:
            url = str(row.get("url") or row.get("href") or "")
            candidates.append(SourceCandidate(
                provider_name=str(row.get("source") or "baidu_search"),
                provider_type="api",
                query_or_request=query,
                request_id=request_id,
                retrieved_at=utc_now_text(),
                title=str(row.get("title") or ""),
                url=url,
                publisher=urlparse(url).netloc,
                raw_text=str(row.get("snippet") or row.get("content") or row),
                is_live=True,
                metadata={"api_row": row, "content_origin": "search_snippet"},
            ))
        return candidates

    def _crawl_candidate(
        self,
        candidate: SourceCandidate,
        failed_sources: list[dict[str, Any]],
    ) -> SourceCandidate | None:
        try:
            from core.research.perilla.discovery.focused_crawler import FocusedCrawler

            crawler = FocusedCrawler(cache_dir=str(self.run_dir / "crawler_cache"))
            doc = crawler.crawl_url(candidate.url)
        except Exception as exc:  # noqa: BLE001
            failed_sources.append({
                "provider_name": "FocusedCrawler",
                "provider_type": "crawler",
                "query_or_request": candidate.query_or_request,
                "request_id": candidate.request_id,
                "retrieved_at": utc_now_text(),
                "url": candidate.url,
                "reason": f"crawl_failed: {exc}",
            })
            return None
        if not doc or not doc.content_text:
            failed_sources.append({
                "provider_name": "FocusedCrawler",
                "provider_type": "crawler",
                "query_or_request": candidate.query_or_request,
                "request_id": candidate.request_id,
                "retrieved_at": utc_now_text(),
                "url": candidate.url,
                "reason": "empty_crawl_result",
            })
            return None
        return SourceCandidate(
            provider_name="FocusedCrawler",
            provider_type="crawler",
            query_or_request=candidate.query_or_request,
            request_id=candidate.request_id,
            retrieved_at=doc.fetched_at or utc_now_text(),
            title=doc.title or candidate.title,
            url=doc.url,
            publisher=doc.site_name or candidate.publisher,
            raw_text=doc.content_text,
            is_live=True,
            metadata={
                "source_type": doc.source_type,
                "credibility_hint": doc.credibility_hint,
                "search_provider": candidate.provider_name,
                "search_request_id": candidate.request_id,
                "content_origin": "crawler_text",
            },
        )

    def _collect_local_candidates(self, failed_sources: list[dict[str, Any]]) -> list[SourceCandidate]:
        if not (self.request.company or self.request.symbol):
            return []
        candidates: list[SourceCandidate] = []
        parquet_files = [
            Path("data/industry/industry_node_candidates.parquet"),
            Path("data/industry/concept_members.parquet"),
            Path("data/industry/stock_basic_info.parquet"),
        ]
        aliases = self.profile.get("node_aliases", []) or [self.request.node]
        company_terms = self.profile.get("company_aliases", []) or [self.request.company, self.request.symbol]
        for path in parquet_files:
            if not path.exists():
                continue
            try:
                import pandas as pd

                df = pd.read_parquet(path)
            except Exception as exc:  # noqa: BLE001
                failed_sources.append({
                    "provider_name": "local_cache",
                    "provider_type": "local_cache",
                    "query_or_request": str(path),
                    "request_id": short_id("req"),
                    "retrieved_at": utc_now_text(),
                    "reason": f"local_read_failed: {exc}",
                })
                continue
            for idx, row in df.iterrows():
                row_payload = {str(col): self._jsonable(row[col]) for col in df.columns}
                row_text = json.dumps(row_payload, ensure_ascii=False, sort_keys=True)
                if not any(term and term in row_text for term in company_terms):
                    continue
                if aliases and not any(term and term in row_text for term in aliases):
                    continue
                candidates.append(SourceCandidate(
                    provider_name="local_cache",
                    provider_type="local_cache",
                    query_or_request=f"{path}:{idx}",
                    request_id=short_id("req"),
                    retrieved_at=utc_now_text(),
                    title=path.name,
                    publisher="local",
                    raw_text=row_text,
                    local_path=str(path),
                    row_id=str(idx),
                    record_id=f"{path.name}:{idx}",
                    is_live=False,
                    metadata={"columns": list(df.columns), "row": row_payload, "content_origin": "local_structured_record"},
                ))
        return candidates

    def _collect_vector_memory_candidates(self, failed_sources: list[dict[str, Any]]) -> list[SourceCandidate]:
        if not self.request.use_vector_memory:
            return []
        query = " ".join(
            item
            for item in [
                self.request.node,
                self.request.topic,
                self.request.root_theme,
                self.request.company,
                self.request.symbol,
            ]
            if item
        ).strip()
        if not query:
            return []
        try:
            from core.research_graph.vector_memory import DocumentChunkStore, HybridRetriever

            store = DocumentChunkStore(db_path=self.request.vector_memory_path)
            retrieved = HybridRetriever(store).retrieve(query, limit=self.request.max_search_results)
        except Exception as exc:  # noqa: BLE001
            failed_sources.append({
                "provider_name": "vector_memory",
                "provider_type": "local_vector_memory",
                "query_or_request": query,
                "request_id": short_id("req"),
                "retrieved_at": utc_now_text(),
                "reason": f"vector_memory_failed: {exc}",
            })
            return []
        candidates: list[SourceCandidate] = []
        for item in retrieved:
            if item.score < 0.15:
                continue
            chunk = item.chunk
            candidates.append(SourceCandidate(
                provider_name="vector_memory",
                provider_type="local_vector_memory",
                query_or_request=query,
                request_id=short_id("req"),
                retrieved_at=utc_now_text(),
                title=chunk.topic or chunk.document_id,
                publisher="local_vector_memory",
                raw_text=chunk.text,
                record_id=chunk.chunk_id,
                is_live=False,
                metadata={
                    "content_origin": "vector_memory_chunk",
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "source_id": chunk.source_id,
                    "source_type": chunk.source_type,
                    "score": item.score,
                    "vector_score": item.vector_score,
                    "keyword_score": item.keyword_score,
                },
            ))
        return candidates

    @staticmethod
    def _jsonable(value: Any) -> Any:
        try:
            if hasattr(value, "item"):
                return value.item()
        except Exception:
            pass
        if value is None:
            return ""
        return str(value)

    def _persist_source(self, candidate: SourceCandidate) -> dict[str, Any]:
        source_id = short_id("src")
        document_id = short_id("doc")
        raw_text = candidate.raw_text or ""
        normalized = normalize_text(raw_text)
        raw_path = self.raw_dir / f"{source_id}.txt"
        raw_path.write_text(raw_text, encoding="utf-8")
        normalized_path = self.raw_dir / f"{source_id}.normalized.txt"
        normalized_path.write_text(normalized, encoding="utf-8")
        domain = urlparse(candidate.url).netloc if candidate.url else ""
        return {
            "source_id": source_id,
            "document_id": document_id,
            "provider_name": candidate.provider_name,
            "provider_type": candidate.provider_type,
            "query_or_request": candidate.query_or_request,
            "request_id": candidate.request_id,
            "retrieved_at": candidate.retrieved_at or utc_now_text(),
            "title": candidate.title,
            "url": candidate.url,
            "domain": domain,
            "publisher": candidate.publisher or domain or "local",
            "local_path": candidate.local_path,
            "record_id": candidate.record_id,
            "row_id": candidate.row_id,
            "raw_document_path": str(raw_path),
            "normalized_document_path": str(normalized_path),
            "content_hash": sha256_text(raw_text),
            "is_live": candidate.is_live,
            "metadata": candidate.metadata,
        }

    def _upsert_vector_memory_source(self, source: dict[str, Any]) -> None:
        if not self.request.use_vector_memory:
            return
        try:
            from core.research_graph.vector_memory import DocumentChunk, DocumentChunkStore

            text = Path(source["normalized_document_path"]).read_text(encoding="utf-8")
            if not text:
                return
            chunk = DocumentChunk(
                chunk_id=str(source.get("document_id") or source.get("source_id")),
                document_id=str(source.get("document_id", "")),
                source_id=str(source.get("source_id", "")),
                source_type=str(source.get("provider_type", "")),
                topic=self.request.topic or self.request.root_theme,
                text=text,
                quote=text[:240],
                quote_span=[0, min(240, len(text))],
                metadata={
                    "provider_name": source.get("provider_name", ""),
                    "query_or_request": source.get("query_or_request", ""),
                    "retrieved_at": source.get("retrieved_at", ""),
                    "content_hash": source.get("content_hash", ""),
                },
            )
            DocumentChunkStore(db_path=self.request.vector_memory_path).upsert_chunks([chunk])
        except Exception:
            return

    def _extract_evidence(self, source: dict[str, Any]) -> dict[str, Any] | None:
        raw_text = Path(source["raw_document_path"]).read_text(encoding="utf-8")
        # Step 2:政策事件类来源优先走 policy_event_parser,产出事件化证据。
        from core.research_graph.policy_event_parser import parse_policy_event

        policy_evidence = parse_policy_event(source, raw_text)
        if policy_evidence:
            # 返回首个政策事件 evidence,后续 evidence 由 _extract_evidence_llm 或
            # 主流程的 evidence 列表承载。这里只覆盖单个证据以保持 schema 一致。
            ev = policy_evidence[0]
            return {
                "evidence_id": ev.evidence_id,
                "source_id": ev.source_id,
                "document_id": ev.document_id,
                "raw_document_path": source["raw_document_path"],
                "normalized_document_path": source["normalized_document_path"],
                "quote": ev.quote,
                "span_start": ev.metadata.get("span_start", 0),
                "span_end": ev.metadata.get("span_end", len(ev.quote)),
                "stance": "support",
                "status": "CANDIDATE",
                "extraction_reason": "policy_event_parser",
                "anchor_window": {"primary_anchor": ev.event_date, "secondary_anchor": ev.metadata.get("shock_type", "")},
                "counter_anchor": "",
                "provider_name": source.get("provider_name", ""),
                "provider_type": source.get("provider_type", ""),
                "content_origin": ev.metadata.get("content_origin", ""),
                "evidence_strength": "LIKELY",
                "evidence_kind": "policy_event",
                "path_hint": [
                    item for item in [
                        self.request.root_theme or self.request.topic,
                        ev.event_date,
                        ev.metadata.get("shock_type", ""),
                    ] if item
                ],
                "path_hint_source": self._normalization_source(source),
                "event_date": ev.event_date,
                "causal_status": ev.causal_status,
            }
        primary_terms = [term for term in (self.profile.get("primary_anchors") or [self.request.node]) if term]
        secondary_terms = [term for term in (self.profile.get("secondary_anchors") or [self.request.topic]) if term]
        match = find_anchor_window(
            raw_text,
            primary_terms=primary_terms,
            secondary_terms=secondary_terms,
            max_distance=int(self.profile.get("anchor_max_distance", 500)),
        )
        if not match and self.profile.get("fallback_full_document_match", False):
            match = find_any_anchor_window(raw_text, primary_terms + secondary_terms)
        if not match:
            return None
        anchor_window = match["anchor_window"]
        max_distance = int(self.profile.get("anchor_max_distance", 500))
        max_same_paragraph_chars = int(self.profile.get("max_same_paragraph_chars", 1200))
        valid_window = (
            int(anchor_window.get("distance_chars", 10**9)) <= max_distance
            or (
                anchor_window.get("same_paragraph")
                and int(anchor_window.get("paragraph_chars", 10**9)) <= max_same_paragraph_chars
            )
        )
        if not valid_window:
            return None
        quote = raw_text[match["span_start"]:match["span_end"]]
        counter_anchor = first_term_match(quote, self.profile.get("counter_anchors", []))
        stance = "counter" if counter_anchor else "support"
        evidence_strength, evidence_kind = self._evidence_grade(source)
        evidence_id = short_id("ev")
        return {
            "evidence_id": evidence_id,
            "source_id": source["source_id"],
            "document_id": source["document_id"],
            "raw_document_path": source["raw_document_path"],
            "normalized_document_path": source["normalized_document_path"],
            "quote": quote,
            "span_start": match["span_start"],
            "span_end": match["span_end"],
            "stance": stance,
            "status": "CANDIDATE",
            "extraction_reason": "counter_anchor_window_match" if counter_anchor else "anchor_window_match",
            "anchor_window": match["anchor_window"],
            "counter_anchor": counter_anchor,
            "provider_name": source["provider_name"],
            "provider_type": source["provider_type"],
            "content_origin": source.get("metadata", {}).get("content_origin", ""),
            "evidence_strength": evidence_strength,
            "evidence_kind": evidence_kind,
            "path_hint": self._path_hint_from_evidence(source, match["anchor_window"]),
            "path_hint_source": self._normalization_source(source),
        }

    def _extract_evidence_llm(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """介入点 1:LLM 证据抽取增强。

        把 sources 转换为 enhanced_evidence_extractor 所需格式,
        调用 LLM 4 阶段抽取(高召回预筛 → chunk 召回 → LLM 精排 → LLM 四元组抽取),
        把产出的 Evidence 对象转换为与 _extract_evidence 同 schema 的 dict。

        Args:
            sources: source_row 列表(含 raw_document_path / title / url 等)。

        Returns:
            evidence dict 列表(同 _extract_evidence schema),失败返回空列表。
        """
        from core.research_graph.llm_evidence_extractor import enhanced_evidence_extraction

        # 收集 node_aliases(canonical_name + topic + root_theme)
        node_aliases = [
            self.request.node, self.request.topic,
            self.request.root_theme, self.request.company,
        ]
        node_aliases = [a for a in node_aliases if a]
        node_name = self.request.node or self.request.topic or self.request.root_theme

        # 把 source_row 转换为 extractor 所需 doc 格式
        docs: list[dict[str, Any]] = []
        for src in sources:
            try:
                raw_text = Path(src["raw_document_path"]).read_text(encoding="utf-8")
            except Exception:
                continue
            docs.append({
                "doc_id": src.get("document_id", ""),
                "source_id": src.get("source_id", ""),
                "title": src.get("title", ""),
                "url": src.get("url", ""),
                "full_text": raw_text,
                "snippet": raw_text[:200],
                "source_type": src.get("metadata", {}).get("content_origin", "")
                                or src.get("provider_type", ""),
                "node_ids": [],  # 由 evidence_index 路由时填充
            })

        if not docs:
            return []

        try:
            evidence_list = enhanced_evidence_extraction(
                search_results=docs,
                root_theme=self.request.root_theme or self.request.topic,
                node_aliases=node_aliases,
                node_name=node_name,
                llm_providers=None,  # 从 config 加载
                source_type_default="search_snippet",
            )
        except Exception as exc:
            # LLM 增强失败不影响主流程,已有正则 evidence 兜底
            return []

        # 转 Evidence 对象为同 schema dict
        result: list[dict[str, Any]] = []
        source_by_id = {s.get("source_id"): s for s in sources}
        for ev in evidence_list:
            src = source_by_id.get(ev.source_id, {})
            evidence_strength, evidence_kind = self._evidence_grade(src)
            result.append({
                "evidence_id": ev.evidence_id,
                "source_id": ev.source_id,
                "document_id": ev.document_id,
                "raw_document_path": src.get("raw_document_path", ""),
                "normalized_document_path": src.get("normalized_document_path", ""),
                "quote": ev.quote,
                "span_start": ev.metadata.get("span_start", 0),
                "span_end": ev.metadata.get("span_end", len(ev.quote)),
                "stance": ev.metadata.get("stance", "support"),
                "status": "CANDIDATE",
                "extraction_reason": f"llm_quad_extract:{ev.metadata.get('relationship', '')}",
                "anchor_window": {"primary_anchor": ev.metadata.get("subject", ""),
                                  "secondary_anchor": ev.metadata.get("object", "")},
                "counter_anchor": ev.metadata.get("stance") == "counter",
                "provider_name": src.get("provider_name", ""),
                "provider_type": src.get("provider_type", ""),
                "content_origin": src.get("metadata", {}).get("content_origin", ""),
                "evidence_strength": evidence_strength,
                "evidence_kind": evidence_kind,
                "path_hint": [
                    item for item in [
                        self.request.root_theme or self.request.topic,
                        ev.metadata.get("subject", ""),
                        ev.metadata.get("object", ""),
                    ] if item
                ],
                "path_hint_source": self._normalization_source(src),
                "llm_extracted": True,
                "llm_confidence": ev.metadata.get("confidence", 0.5),
                "llm_relationship": ev.metadata.get("relationship", ""),
            })
        return result

    @staticmethod
    def _normalization_source(source: dict[str, Any]) -> str:
        origin = str(source.get("metadata", {}).get("content_origin") or "")
        if origin:
            return origin
        return str(source.get("provider_type") or "source_document")

    @staticmethod
    def _evidence_grade(source: dict[str, Any]) -> tuple[str, str]:
        origin = str(source.get("metadata", {}).get("content_origin") or "")
        provider_type = str(source.get("provider_type") or "")
        if provider_type == "api" and origin == "search_snippet":
            return "SPECULATIVE", "snippet_evidence"
        if origin == "local_structured_record" or provider_type == "local_cache":
            return "STRUCTURED", "local_structured_record"
        if origin == "crawler_text" or provider_type == "crawler":
            return "STRONG", "crawler_text"
        return "OBSERVED", "document_evidence"

    @staticmethod
    def _structured_row_from_source(source: dict[str, Any]) -> dict[str, Any]:
        """Recover a locally generated structured row, including via vector memory.

        Vector retrieval preserves the JSON text but may not carry the original
        ``metadata.row`` wrapper.  Only rows explicitly marked as local structured
        seeds are accepted; arbitrary JSON documents must not become mappings.
        """
        row = source.get("metadata", {}).get("row", {})
        if isinstance(row, dict) and row:
            return row
        try:
            raw_path = Path(str(source.get("raw_document_path") or ""))
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        origin = str(payload.get("candidate_source") or payload.get("provider") or "")
        if origin != "local_structured_seed":
            return {}
        if not payload.get("stock_name") or not payload.get("node_id"):
            return {}
        return payload

    def _path_hint_from_evidence(self, source: dict[str, Any], anchor_window: dict[str, Any]) -> list[str]:
        root = self.request.root_theme or self.request.topic
        row = self._structured_row_from_source(source)
        if row:
            chain_id = str(row.get("chain_id") or "").strip()
            node_id = str(row.get("node_id") or "").strip()
            path = [item for item in [root, chain_id, node_id] if item]
            if path:
                return path
        return [
            item
            for item in [
                root,
                str(anchor_window.get("primary_anchor") or self.request.node).strip(),
                str(anchor_window.get("secondary_anchor") or "").strip(),
            ]
            if item
        ]

    def _build_candidate_claims(
        self,
        support_evidence: list[dict[str, Any]],
        run_status: str,
        sources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        source_by_id = {item.get("source_id"): item for item in sources}
        for evidence in support_evidence:
            source = source_by_id.get(evidence.get("source_id"), {})
            claim_context = self._claim_context_from_evidence(evidence, source)
            claim_id = short_id("claim")
            claims.append({
                "claim_id": claim_id,
                "status": "CANDIDATE" if run_status == "CANDIDATE" else "NEEDS_MORE_EVIDENCE",
                "subject": claim_context["subject"],
                "relation": claim_context["relation"],
                "object": claim_context["object"],
                "source_id": evidence["source_id"],
                "document_id": evidence["document_id"],
                "evidence_ids": [evidence["evidence_id"]],
                "quote": evidence["quote"],
                "span_start": evidence["span_start"],
                "span_end": evidence["span_end"],
                "extraction_reason": evidence["extraction_reason"],
                "path_hint": claim_context["path_hint"],
                "path_hint_source": claim_context["normalization_source"],
                "normalization_source": claim_context["normalization_source"],
                "normalization_evidence_id": evidence["evidence_id"],
                "evidence_strength": evidence.get("evidence_strength", ""),
                "evidence_kind": evidence.get("evidence_kind", ""),
                "generated_by": "growth_agent_mining",
                "llm_assist_allowed": self.request.allow_llm_assist,
            })
        return claims

    def _claim_context_from_evidence(self, evidence: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        row = self._structured_row_from_source(source)
        if row:
            subject_name = str(row.get("stock_name") or self.request.company or self.request.node).strip()
            subject_symbol = str(row.get("symbol") or self.request.symbol).strip()
            object_name = str(row.get("matched_board_name") or row.get("matched_keyword") or row.get("node_id") or self.request.node).strip()
            object_id = str(row.get("node_id") or object_name).strip()
            return {
                "subject": {"name": subject_name, "type": "COMPANY", "symbol": subject_symbol},
                "relation": "MAPS_TO",
                "object": {"name": object_name, "type": infer_node_type(object_id or object_name)},
                "path_hint": evidence.get("path_hint", []),
                "normalization_source": "local_structured_record",
            }

        anchor_window = evidence.get("anchor_window", {})
        primary = str(anchor_window.get("primary_anchor") or self.request.node or self.request.company).strip()
        secondary = str(anchor_window.get("secondary_anchor") or self.request.topic or self.request.node).strip()
        subject_name = self.request.company or primary
        subject_type = "COMPANY" if self.request.company else infer_node_type(primary)
        subject_symbol = self.request.symbol if self.request.company else ""
        return {
            "subject": {"name": subject_name, "type": subject_type, "symbol": subject_symbol},
            "relation": "EVIDENCE_CO_WINDOW",
            "object": {"name": secondary or self.request.node or self.request.topic, "type": infer_node_type(secondary)},
            "path_hint": evidence.get("path_hint", []),
            "normalization_source": self._normalization_source(source),
        }

    def _build_graph_hypotheses(
        self,
        claims: list[dict[str, Any]],
        run_status: str,
        counter_evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        hypotheses: list[dict[str, Any]] = []
        has_conflict = bool(claims and counter_evidence)
        status = "CONFLICT_RISK" if has_conflict else ("CANDIDATE" if run_status == "CANDIDATE" else "NEEDS_MORE_EVIDENCE")
        for claim in claims:
            hypotheses.append({
                "hypothesis_id": short_id("gh"),
                "status": status,
                "claim_ids": [claim["claim_id"]],
                "evidence_ids": list(claim.get("evidence_ids", [])),
                "counter_evidence_ids": [item["evidence_id"] for item in counter_evidence],
                "subject": claim["subject"],
                "relation": claim["relation"],
                "object": claim["object"],
                "path_hint": claim.get("path_hint", []),
                "path_hint_source": claim.get("path_hint_source", ""),
                "normalization_source": claim.get("normalization_source", ""),
                "apply_candidate": self.request.apply_candidate,
            })
        return hypotheses

    def _write_readme(self, run_status: str, support_count: int) -> None:
        lines = [
            "# Growth Agent Mining Run",
            "",
            f"- status: `{run_status}`",
            f"- support_evidence_count: `{support_count}`",
            f"- min_evidence: `{self.request.min_evidence}`",
            f"- staging_only: `{not self.request.apply_candidate}`",
        ]
        (self.run_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def find_anchor_window(
    text: str,
    *,
    primary_terms: list[str],
    secondary_terms: list[str],
    max_distance: int,
) -> dict[str, Any] | None:
    if not text:
        return None
    for primary in primary_terms:
        if not primary:
            continue
        for p_match in re.finditer(re.escape(primary), text, flags=re.IGNORECASE):
            p_start = p_match.start()
            for secondary in secondary_terms:
                if not secondary:
                    continue
                for s_match in re.finditer(re.escape(secondary), text, flags=re.IGNORECASE):
                    s_start = s_match.start()
                    distance = abs(p_start - s_start)
                    same_paragraph = paragraph_index(text, p_start) == paragraph_index(text, s_start)
                    paragraph_chars = paragraph_length(text, p_start) if same_paragraph else 0
                    if same_paragraph or distance <= max_distance:
                        start = max(0, min(p_start, s_start) - 160)
                        end = min(len(text), max(p_match.end(), s_match.end()) + 260)
                        return {
                            "span_start": start,
                            "span_end": end,
                            "anchor_window": {
                                "primary_anchor": text[p_match.start():p_match.end()],
                                "secondary_anchor": text[s_match.start():s_match.end()],
                                "distance_chars": distance,
                                "same_paragraph": same_paragraph,
                                "paragraph_chars": paragraph_chars,
                            },
                        }
    return None


def find_any_anchor_window(text: str, terms: list[str]) -> dict[str, Any] | None:
    for term in terms:
        if not term:
            continue
        match = re.search(re.escape(term), text, flags=re.IGNORECASE)
        if match:
            start = max(0, match.start() - 160)
            end = min(len(text), match.end() + 260)
            return {
                "span_start": start,
                "span_end": end,
                "anchor_window": {
                    "primary_anchor": text[match.start():match.end()],
                    "secondary_anchor": "",
                    "distance_chars": 0,
                    "same_paragraph": True,
                },
            }
    return None


def first_term_match(text: str, terms: list[str]) -> str:
    haystack = text or ""
    for term in terms or []:
        term_text = str(term or "")
        if term_text and re.search(re.escape(term_text), haystack, flags=re.IGNORECASE):
            return term_text
    return ""


def infer_node_type(text: str) -> str:
    value = (text or "").lower()
    material_terms = ["material", "phosphide", "inp", "substrate", "wafer", "compound"]
    equipment_terms = ["tool", "equipment", "machine", "system", "reactor"]
    product_terms = ["memory", "dram", "nand", "hbm", "chip", "device", "module", "product"]
    process_terms = ["pvd", "deposition", "process", "manufacturing", "etch", "lithography", "sputter"]
    if any(term in value for term in material_terms):
        return "MATERIAL"
    if any(term in value for term in equipment_terms):
        return "EQUIPMENT"
    if any(term in value for term in product_terms):
        return "PRODUCT"
    if any(term in value for term in process_terms):
        return "PROCESS"
    return "PROCESS"


def paragraph_index(text: str, offset: int) -> int:
    return text[:offset].count("\n\n")


def paragraph_length(text: str, offset: int) -> int:
    start = text.rfind("\n\n", 0, offset)
    end = text.find("\n\n", offset)
    if start == -1:
        start = 0
    else:
        start += 2
    if end == -1:
        end = len(text)
    return max(0, end - start)


class GrowthArtifactValidator:
    def __init__(self, run_dir: Path, request: GrowthMiningRequest, profile: dict[str, Any]) -> None:
        self.run_dir = run_dir
        self.request = request
        self.profile = profile
        self.source_manifest = read_json(run_dir / "source_manifest.json", [])
        self.raw_evidence = read_json(run_dir / "raw_evidence.json", [])
        self.counter_evidence = read_json(run_dir / "counter_evidence.json", [])
        self.candidate_claims = read_json(run_dir / "candidate_claims.json", [])
        self.graph_hypotheses = read_json(run_dir / "graph_hypotheses.json", [])
        self.run_options = read_json(run_dir / "run_options.json", {})

    def validate(self) -> dict[str, Any]:
        checks = [
            self._anti_hardcode_check(),
            self._live_mode_check(),
            self._provider_trace_check(),
            self._raw_document_persistence_check(),
            self._quote_grounding_check(),
            self._source_diversity_check(),
            self._anchor_window_check(),
            self._evidence_bound_claim_check(),
            self._profile_answer_leak_check(),
            self._expected_profile_validation_check(),
            self._counter_evidence_routing_check(),
            self._conflict_risk_check(),
            self._llm_trace_check(),
            self._snippet_evidence_grading_check(),
            self._candidate_only_graph_check(),
            self._no_company_theme_direct_check(),
            self._staging_graph_check(),
            self._forbidden_trading_text_check(),
            self._blocked_not_faked_check(),
        ]
        passed = all(item["passed"] for item in checks)
        return {
            "status": "PASSED" if passed else "FAILED",
            "run_status": self.run_options.get("run_status", "BLOCKED"),
            "checks": checks,
            "support_evidence_count": len([item for item in self.raw_evidence if item.get("stance") == "support"]),
            "counter_evidence_count": len([item for item in self.counter_evidence if item.get("stance") == "counter"]),
            "source_count": len(self.source_manifest),
            "validated_at": utc_now_text(),
        }

    def _check(self, name: str, passed: bool, details: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"name": name, "passed": bool(passed), "details": details}
        if extra:
            payload.update(extra)
        return payload

    def _anti_hardcode_check(self) -> dict[str, Any]:
        terms = [term for term in self.profile.get("all_forbidden_fixture_terms", []) if term]
        files = [
            Path("core/research_graph/growth_agent.py"),
            Path("core/research_graph/growth_mining.py"),
            Path("core/research_graph/evidence_miner.py"),
            Path("scripts/run_growth_agent_mining.py"),
        ]
        hits: list[str] = []
        for path in files:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for term in terms:
                if term in text:
                    hits.append(f"{path}:{term}")
        return self._check("anti_hardcode_check", not hits, "fixture terms absent from core logic", {"hits": hits})

    def _live_mode_check(self) -> dict[str, Any]:
        if not self.request.live_mode:
            return self._check("live_mode_check", True, "live mode not requested")
        required_live = int(self.profile.get("min_live_evidence", 0))
        source_by_id = {item.get("source_id"): item for item in self.source_manifest}
        live_count = 0
        for evidence in self.raw_evidence:
            source = source_by_id.get(evidence.get("source_id"), {})
            if evidence.get("stance") == "support" and source.get("is_live"):
                live_count += 1
        return self._check(
            "live_mode_check",
            live_count >= required_live,
            f"live support evidence {live_count}/{required_live}",
            {"live_support_evidence_count": live_count},
        )

    def _provider_trace_check(self) -> dict[str, Any]:
        missing: list[str] = []
        required = ["provider_name", "provider_type", "query_or_request", "request_id", "retrieved_at", "content_hash", "raw_document_path"]
        for source in self.source_manifest:
            source_id = source.get("source_id", "")
            for field_name in required:
                if not source.get(field_name):
                    missing.append(f"{source_id}:{field_name}")
            provider_type = source.get("provider_type")
            if provider_type in {"web_search", "api", "crawler"} and not source.get("url"):
                missing.append(f"{source_id}:url")
            if provider_type == "local_cache" and not (source.get("local_path") and (source.get("record_id") or source.get("row_id"))):
                missing.append(f"{source_id}:local_trace")
        return self._check("provider_trace_check", not missing, "provider trace complete", {"missing": missing})

    def _raw_document_persistence_check(self) -> dict[str, Any]:
        missing: list[str] = []
        raw_dir_ok = (self.run_dir / "raw_documents").is_dir()
        for source in self.source_manifest:
            path = Path(str(source.get("raw_document_path", "")))
            if not path.exists() or not path.read_text(encoding="utf-8", errors="ignore"):
                missing.append(source.get("source_id", "unknown"))
        for evidence in self.raw_evidence + self.counter_evidence:
            path = evidence.get("raw_document_path", "")
            if not path or not Path(path).exists():
                missing.append(f"{evidence.get('evidence_id')}:raw_document_path")
        return self._check(
            "raw_document_persistence_check",
            raw_dir_ok and not missing,
            "raw documents persisted",
            {"missing": missing, "raw_dir_ok": raw_dir_ok},
        )

    def _quote_grounding_check(self) -> dict[str, Any]:
        failures: list[str] = []
        for evidence in self.raw_evidence + self.counter_evidence:
            quote = evidence.get("quote", "")
            raw_path = Path(str(evidence.get("raw_document_path", "")))
            if not quote or not raw_path.exists():
                failures.append(f"{evidence.get('evidence_id')}:missing_quote_or_raw")
                continue
            raw_text = raw_path.read_text(encoding="utf-8", errors="ignore")
            span_start = int(evidence.get("span_start", -1))
            span_end = int(evidence.get("span_end", -1))
            exact_span = 0 <= span_start <= span_end <= len(raw_text) and raw_text[span_start:span_end] == quote
            exact_contains = quote in raw_text
            if not (exact_span or exact_contains):
                normalized_path = Path(str(evidence.get("normalized_document_path", "")))
                normalized_ok = normalized_path.exists() and quote in normalized_path.read_text(encoding="utf-8", errors="ignore")
                if normalized_ok and evidence.get("normalization_trace"):
                    continue
                failures.append(f"{evidence.get('evidence_id')}:quote_not_found_in_source")
        return self._check("quote_grounding_check", not failures, "quotes grounded in source text", {"failures": failures})

    def _source_diversity_check(self) -> dict[str, Any]:
        if self.request.min_evidence < 3:
            return self._check("source_diversity_check", True, "diversity threshold not requested")
        support_sources = []
        source_by_id = {item.get("source_id"): item for item in self.source_manifest}
        for evidence in self.raw_evidence:
            if evidence.get("stance") == "support":
                source = source_by_id.get(evidence.get("source_id"), {})
                if source:
                    support_sources.append(source)
        source_ids = {item.get("source_id") for item in support_sources}
        hashes = {item.get("content_hash") for item in support_sources}
        publishers = {item.get("domain") or item.get("publisher") or item.get("provider_name") for item in support_sources}
        passed = len(source_ids) >= self.request.min_evidence and len(hashes) >= self.request.min_evidence and len(publishers) >= self.request.min_evidence
        return self._check(
            "source_diversity_check",
            passed,
            "support evidence source diversity",
            {"source_ids": len(source_ids), "content_hashes": len(hashes), "publishers": len(publishers)},
        )

    def _anchor_window_check(self) -> dict[str, Any]:
        if not self.profile.get("require_anchor_window", False):
            return self._check("anchor_window_check", True, "anchor window not required")
        max_same_paragraph_chars = int(self.profile.get("max_same_paragraph_chars", 1200))
        failures = [
            item.get("evidence_id", "")
            for item in self.raw_evidence
            if item.get("stance") == "support"
            and not (
                item.get("anchor_window", {}).get("same_paragraph")
                and int(item.get("anchor_window", {}).get("paragraph_chars", 10**9)) <= max_same_paragraph_chars
                or int(item.get("anchor_window", {}).get("distance_chars", 10**9)) <= int(self.profile.get("anchor_max_distance", 500))
            )
        ]
        return self._check("anchor_window_check", not failures, "anchors are local to quote window", {"failures": failures})

    def _evidence_bound_claim_check(self) -> dict[str, Any]:
        failures: list[str] = []
        required = ["source_id", "document_id", "quote", "span_start", "span_end", "extraction_reason", "evidence_ids"]
        for claim in self.candidate_claims:
            for field_name in required:
                if claim.get(field_name) in (None, "", []):
                    failures.append(f"{claim.get('claim_id')}:missing_{field_name}")
            for field_name in ["normalization_source", "normalization_evidence_id", "path_hint_source"]:
                if not claim.get(field_name):
                    failures.append(f"{claim.get('claim_id')}:missing_{field_name}")
        return self._check("evidence_bound_claim_check", not failures, "claims are evidence-bound", {"failures": failures})

    def _profile_answer_leak_check(self) -> dict[str, Any]:
        failures: list[str] = []
        forbidden_sources = {"acceptance_profile", "profile", "expected_profile"}
        for group_name, rows in (("candidate_claims", self.candidate_claims), ("graph_hypotheses", self.graph_hypotheses)):
            for row in rows:
                source = str(row.get("normalization_source") or row.get("path_hint_source") or "")
                row_id = row.get("claim_id") or row.get("hypothesis_id")
                if not source:
                    failures.append(f"{group_name}:{row_id}:missing_generation_source")
                if source in forbidden_sources:
                    failures.append(f"{group_name}:{row_id}:profile_generation_source")
        return self._check("profile_answer_leak_check", not failures, "generated graph fields cite evidence-bound normalization source", {"failures": failures})

    def _expected_profile_validation_check(self) -> dict[str, Any]:
        if not self.profile.get("require_expected_mapping", False):
            return self._check("expected_profile_validation_check", True, "no expected mapping gate configured")
        expected_path = self.profile.get("expected_path_hint", [])
        expected_object_name = self.profile.get("expected_object_node_name", "")
        expected_object_type = self.profile.get("expected_object_node_type", "")
        expected_relation = self.profile.get("expected_relation", "")
        matched = False
        for hypothesis in self.graph_hypotheses:
            if expected_path and hypothesis.get("path_hint") != expected_path:
                continue
            if expected_object_name and hypothesis.get("object", {}).get("name") != expected_object_name:
                continue
            if expected_object_type and hypothesis.get("object", {}).get("type") != expected_object_type:
                continue
            if expected_relation and hypothesis.get("relation") != expected_relation:
                continue
            if not hypothesis.get("evidence_ids"):
                continue
            matched = True
            break
        details = "expected mapping appears in evidence-bound hypotheses"
        return self._check(
            "expected_profile_validation_check",
            matched,
            details,
            {
                "expected_path_hint": expected_path,
                "expected_object_node_name": expected_object_name,
                "expected_relation": expected_relation,
            },
        )

    def _counter_evidence_routing_check(self) -> dict[str, Any]:
        failures: list[str] = []
        for item in self.raw_evidence:
            if item.get("stance") == "counter":
                failures.append(f"raw_evidence:{item.get('evidence_id')}:counter_not_routed")
        for item in self.counter_evidence:
            if item.get("stance") != "counter":
                failures.append(f"counter_evidence:{item.get('evidence_id')}:not_counter")
            if not item.get("counter_anchor"):
                failures.append(f"counter_evidence:{item.get('evidence_id')}:missing_counter_anchor")
        return self._check("counter_evidence_routing_check", not failures, "counter evidence is routed separately", {"failures": failures})

    def _conflict_risk_check(self) -> dict[str, Any]:
        if not self.raw_evidence or not self.counter_evidence:
            return self._check("conflict_risk_check", True, "no support/counter conflict")
        allowed = {"CONFLICT_RISK", "NEEDS_REVIEW"}
        failures = [
            f"{item.get('hypothesis_id')}:{item.get('status')}"
            for item in self.graph_hypotheses
            if item.get("status") not in allowed
        ]
        return self._check("conflict_risk_check", not failures, "conflicting support/counter evidence requires review", {"failures": failures})

    def _llm_trace_check(self) -> dict[str, Any]:
        allow_llm = bool(self.run_options.get("allow_llm_assist"))
        llm_used = bool(self.run_options.get("llm_assist_used"))
        if allow_llm and not llm_used:
            return self._check("llm_trace_check", True, "LLM assist allowed but not used")
        if not llm_used:
            return self._check("llm_trace_check", True, "LLM assist not used")
        trace_path = Path(str(self.run_options.get("llm_trace_path", "")))
        required = ["input_document_ids", "input_quote_ids", "output_claim_ids", "model_name", "prompt_hash", "response_hash"]
        failures: list[str] = []
        # 防御:空字符串或目录路径跳过读取(Path("") 解析为 . 目录)
        if not trace_path.is_file():
            failures.append("missing_llm_trace_path")
        else:
            for idx, line in enumerate(trace_path.read_text(encoding="utf-8", errors="ignore").splitlines()):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    failures.append(f"line_{idx}:invalid_json")
                    continue
                for field_name in required:
                    if row.get(field_name) in (None, "", []):
                        failures.append(f"line_{idx}:missing_{field_name}")
                for forbidden in ["source_id", "url", "provider_name", "provider_type"]:
                    if forbidden in row:
                        failures.append(f"line_{idx}:llm_generated_{forbidden}")
        return self._check("llm_trace_check", not failures, "LLM traces are explicit when used", {"failures": failures})

    def _snippet_evidence_grading_check(self) -> dict[str, Any]:
        source_by_id = {item.get("source_id"): item for item in self.source_manifest}
        failures: list[str] = []
        for evidence in self.raw_evidence + self.counter_evidence:
            source = source_by_id.get(evidence.get("source_id"), {})
            origin = source.get("metadata", {}).get("content_origin") or evidence.get("content_origin")
            if source.get("provider_type") == "api" and origin == "search_snippet":
                if evidence.get("evidence_strength") != "SPECULATIVE" or evidence.get("evidence_kind") != "snippet_evidence":
                    failures.append(f"{evidence.get('evidence_id')}:api_snippet_not_speculative")
        return self._check("snippet_evidence_grading_check", not failures, "API snippets are speculative evidence only", {"failures": failures})

    def _candidate_only_graph_check(self) -> dict[str, Any]:
        failures: list[str] = []
        for group_name, rows in (("candidate_claims", self.candidate_claims), ("graph_hypotheses", self.graph_hypotheses)):
            for row in rows:
                status = str(row.get("status", ""))
                if status not in ALLOWED_RUN_STATUSES or status in FINAL_STATUSES:
                    failures.append(f"{group_name}:{row.get('claim_id') or row.get('hypothesis_id')}:{status}")
        artifact_text = json.dumps({
            "candidate_claims": self.candidate_claims,
            "graph_hypotheses": self.graph_hypotheses,
        }, ensure_ascii=False)
        for status in FINAL_STATUSES:
            if status in artifact_text:
                failures.append(f"artifact_contains:{status}")
        return self._check("candidate_only_graph_check", not failures, "no final graph statuses", {"failures": failures})

    def _no_company_theme_direct_check(self) -> dict[str, Any]:
        failures: list[str] = []
        for hypothesis in self.graph_hypotheses:
            subject_type = hypothesis.get("subject", {}).get("type")
            object_type = hypothesis.get("object", {}).get("type")
            path_hint = hypothesis.get("path_hint", [])
            evidence_ids = hypothesis.get("evidence_ids", [])
            if subject_type in {"COMPANY", "SUBSIDIARY"} and object_type == "THEME":
                failures.append(f"{hypothesis.get('hypothesis_id')}:company_theme_direct")
            if subject_type in {"COMPANY", "SUBSIDIARY"} and object_type not in CONCRETE_NODE_TYPES:
                failures.append(f"{hypothesis.get('hypothesis_id')}:not_concrete_object")
            if subject_type in {"COMPANY", "SUBSIDIARY"} and (not path_hint or not evidence_ids):
                failures.append(f"{hypothesis.get('hypothesis_id')}:missing_path_or_evidence")
        return self._check("no_company_theme_direct_check", not failures, "company mappings are concrete", {"failures": failures})

    def _staging_graph_check(self) -> dict[str, Any]:
        apply_candidate = bool(self.run_options.get("apply_candidate"))
        graph_path = self.run_dir / "graph_store.json"
        if not apply_candidate:
            return self._check("staging_graph_check", not graph_path.exists(), "default staging-only run")
        failures: list[str] = []
        if graph_path.exists() and any(status in graph_path.read_text(encoding="utf-8", errors="ignore") for status in FINAL_STATUSES):
            failures.append("graph_store_final_status")
        return self._check("staging_graph_check", not failures, "candidate apply did not write final status", {"failures": failures})

    def _forbidden_trading_text_check(self) -> dict[str, Any]:
        terms = [term for term in self.profile.get("forbidden_trading_terms", []) if term]
        files = [
            self.run_dir / "source_manifest.json",
            self.run_dir / "raw_evidence.json",
            self.run_dir / "counter_evidence.json",
            self.run_dir / "candidate_claims.json",
            self.run_dir / "graph_hypotheses.json",
            self.run_dir / "failed_sources.json",
            self.run_dir / "run_options.json",
            self.run_dir / "README.md",
        ]
        hits: list[str] = []
        for path in files:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for term in terms:
                if term in text:
                    hits.append(f"{path.name}:{term}")
        return self._check("forbidden_trading_text_check", not hits, "no restricted action text", {"hits": hits})

    def _blocked_not_faked_check(self) -> dict[str, Any]:
        support_count = len([item for item in self.raw_evidence if item.get("stance") == "support"])
        run_status = self.run_options.get("run_status")
        passed = support_count >= self.request.min_evidence or run_status == "BLOCKED"
        return self._check(
            "blocked_not_faked_check",
            passed,
            "insufficient evidence remains blocked",
            {"support_evidence_count": support_count, "run_status": run_status},
        )
