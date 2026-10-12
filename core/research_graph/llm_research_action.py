"""LLM 驱动的研究动作：实体/关系/证据抽取 + 合并回统一记忆。

工作流：
    1. 从记忆召回与任务节点相关的文档
    2. 调用 LLM 抽取实体、关系、证据（结构化 JSON）
    3. 将抽取结果合并回 UnifiedResearchMemory
    4. 返回 ResearchResult（新增节点/边/证据/召回数）

LLM 调用：
    - 使用 call_llm_with_budget（统一入口）
    - tier="flash"（经济任务）
    - response_format_json=True（强制 JSON 输出）
    - 失败时降级返回空结果，不阻塞主循环

合规：
    - Evidence.extracted_by="llm" 时 metadata 带 llm_trace_path
    - 不输出投资建议（买入/卖出/仓位/止盈/止损）
    - 不凭空编造公司业务/概念归属/收入占比
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from core.research_graph.autonomous_research_engine import ResearchResult
from core.research_graph.cognitive_gap_scheduler import ResearchTask
from core.research_graph.unified_memory import UnifiedResearchMemory

logger = logging.getLogger(__name__)


class LLMResearchAction:
    """LLM 驱动的研究动作：召回 → 抽取 → 合并。

    Args:
        recall_limit: 从记忆召回的最大文档数。
        llm_timeout: LLM 调用超时秒数。
        llm_tier: LLM 层级（"flash" 经济 / "pro" 高智商）。
        max_nodes_per_task: 单次研究最多新增节点数。
        max_edges_per_task: 单次研究最多新增边数。
        trace_dir: LLM 抽取轨迹保存目录（满足 EvidenceLedger 校验）。
        external_provider: 外部观察数据源（ProviderRegistry 兼容对象）。
            None 时惰性加载 data_svc ProviderRegistry；不可用时降级为仅本地文档。
    """

    def __init__(
        self,
        recall_limit: int = 5,
        llm_timeout: int = 30,
        llm_tier: str = "flash",
        max_nodes_per_task: int = 10,
        max_edges_per_task: int = 15,
        trace_dir: str = "data/llm_traces/research_engine",
        external_provider: Any = None,
    ) -> None:
        self._recall_limit = recall_limit
        self._llm_timeout = llm_timeout
        self._llm_tier = llm_tier
        self._max_nodes = max_nodes_per_task
        self._max_edges = max_edges_per_task
        self._trace_dir = trace_dir

        # 惰性加载 LLM providers
        self._providers_loaded = False
        self._llm_available = False

        # 外部观察数据源（ProviderRegistry 兼容对象）
        # 惰性加载：首次需要时才实例化 data_svc ProviderRegistry
        self._external_provider = external_provider
        self._external_provider_loaded = external_provider is not None

    def execute(
        self, task: ResearchTask, memory: UnifiedResearchMemory
    ) -> ResearchResult:
        """执行 LLM 驱动的研究动作。

        Args:
            task: 研究任务（node_name 为研究目标）。
            memory: 统一记忆层。

        Returns:
            研究结果。
        """
        target = task.node_name
        if not target:
            return ResearchResult(
                success=False, reason="任务无 node_name",
                source_status="unavailable",
            )

        # 1. 从记忆召回相关文档
        try:
            docs = memory.recall_documents(target, limit=self._recall_limit)
        except Exception as exc:
            logger.warning("召回文档失败: %s", exc)
            docs = []

        # 1.5 外部观察补充：本地召回不足时调用外部 provider 获取新闻/研报/公告
        # 写入 UnifiedResearchMemory.upsert_document，并构造 RetrievedChunk 追加到 docs
        # 修复 1：记录外部 provider 可用性，用于 source_status 判定
        external_provider_failed = False
        if len(docs) < self._recall_limit:
            remaining = self._recall_limit - len(docs)
            external_docs = self._fetch_external_documents(target, remaining)
            # _fetch_external_documents 在 provider 不可用时返回 []，
            # 需要单独检查 provider 是否真的不可用（区分"provider 不可用" vs "查询无结果"）
            if not external_docs:
                provider = self._get_external_provider()
                if provider is None:
                    external_provider_failed = True
            for doc in external_docs:
                try:
                    memory.upsert_document(
                        doc_id=doc["doc_id"],
                        content=doc["content"],
                        source_type=doc["source_type"],
                        title=doc["title"],
                        url=doc["url"],
                        topic=target,
                    )
                    # 直接构造 RetrievedChunk 追加，避免 recall_documents 命中缓存
                    retrieved = self._build_external_retrieved_chunk(doc, target)
                    if retrieved is not None:
                        docs.append(retrieved)
                except Exception as exc:
                    logger.warning("写入外部文档失败 doc_id=%s: %s", doc.get("doc_id"), exc)

            logger.info(
                "外部观察补充: target=%s, 外部获取=%d, 合计文档=%d",
                target, len(external_docs), len(docs),
            )

        logger.info(
            "LLM 研究: target=%s, 召回文档=%d, source=%s",
            target, len(docs), task.source,
        )

        # 2. 无可用文档时不推断，直接返回失败（避免生成式关系进入主图）
        # 修复 1：来源状态判定——无文档 = 来源不可用
        if not docs:
            source_status = "unavailable" if external_provider_failed else "degraded"
            return ResearchResult(
                success=False,
                reason="无可用文档，不执行 LLM 推断",
                source_status=source_status,
            )

        # 3. 构建 LLM 提示（_build_prompt 在 doc_context 为空时返回失败结果）
        prompt_messages = self._build_prompt(target, docs, task)
        if isinstance(prompt_messages, ResearchResult):
            prompt_messages.source_status = (
                "degraded" if external_provider_failed else "ok"
            )
            return prompt_messages

        # 4. 调用 LLM 抽取
        try:
            raw_response = self._call_llm(prompt_messages)
        except Exception as exc:
            logger.error("LLM 调用失败: %s", exc)
            # 修复 1：LLM 不可用 = 来源不可用
            return ResearchResult(
                success=False,
                documents_recalled=len(docs),
                reason=f"LLM 调用失败: {exc}",
                source_status="unavailable",
            )

        # 4.5 空响应检查：LLM 返回空字符串或纯空白时判定为失败
        # 场景：有文档但 LLM 未产出（如 provider 降级返回空），
        # 此时不应把空抽取当成成功研究，否则节点会被误标记为已完成。
        # 修复 1：LLM 空响应 = 来源不可用（provider 降级到无产出）
        if not raw_response or not raw_response.strip():
            logger.warning("LLM 返回空，无新知识: target=%s", target)
            return ResearchResult(
                success=False,
                documents_recalled=len(docs),
                reason="LLM 返回空，无新知识",
                source_status="unavailable",
            )

        # 5. 解析 LLM 输出
        try:
            extraction = self._parse_extraction(raw_response)
        except Exception as exc:
            logger.error("LLM 输出解析失败: %s", exc)
            return ResearchResult(
                success=False,
                documents_recalled=len(docs),
                reason=f"解析失败: {exc}",
                source_status="ok",
            )

        # 6. 保存 LLM 轨迹（满足 EvidenceLedger 校验）
        trace_path = self._save_trace(task, extraction, raw_response)

        # 7. 合并回统一记忆
        new_nodes, new_edges, new_evidence = self._merge_to_memory(
            target, extraction, memory, trace_path, docs
        )

        progressed = bool(new_nodes or new_edges or new_evidence)
        # 修复 1：来源可用但无新知识仍算 "ok"（来源本身没问题）
        return ResearchResult(
            success=progressed,
            new_nodes=new_nodes,
            new_edges=new_edges,
            new_evidence=new_evidence,
            documents_recalled=len(docs),
            reason=(
                f"抽取: {len(new_nodes)} 节点, {len(new_edges)} 边, {len(new_evidence)} 证据"
                if progressed
                else "抽取完成但无新知识，不标记为成功"
            ),
            source_status="degraded" if external_provider_failed else "ok",
        )

    # ------------------------------------------------------------------
    # LLM 提示构建
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        target: str,
        docs: list[Any],
        task: ResearchTask,
    ) -> list[dict[str, Any]] | ResearchResult:
        """构建 LLM 提示消息。

        Args:
            target: 研究目标节点名称。
            docs: 召回的文档列表。
            task: 研究任务。

        Returns:
            OpenAI 格式的消息列表；当无可用文档时返回失败的 ResearchResult，
            由 execute 统一返回，避免 LLM 基于常识推断产生生成式关系。
        """
        # 构建上下文文档
        doc_context = self._format_docs(docs)

        # 无可用文档时不推断（防御性：execute 已拦截空 docs，这里再次保险）
        if not doc_context:
            return ResearchResult(
                success=False,
                reason="无可用文档，不执行 LLM 推断",
            )

        system_prompt = (
            "你是产业链研究助手。基于给定目标和参考文档，抽取实体、关系和证据。\n"
            "严格遵守：\n"
            "1. 不输出任何投资建议（买入/卖出/仓位/止盈/止损）\n"
            "2. 不凭空编造公司业务、概念归属、收入占比或证据来源\n"
            "3. 只基于参考文档内容抽取，无足够信息时返回空列表\n"
            "4. 必须只返回纯 JSON，禁止任何解释、markdown fence 或前后缀文字\n"
            "5. JSON 格式：\n"
            '{"nodes": [{"name": "", "type": "", "aliases": []}], '
            '"edges": [{"subject": "", "relation": "", "object": "", "qualifiers": {}}], '
            '"evidence": [{"quote": "", "source_title": "", "source_type": "", "summary": "", "supports": []}]}\n'
            "6. node type 只能是：PRODUCT / MATERIAL / EQUIPMENT / COMPANY / PROCESS / APPLICATION / CONCEPT\n"
            "7. relation 只能是：UPSTREAM_OF / DOWNSTREAM_OF / PART_OF / REQUIRES / SUPPLIES_TO / "
            "BENEFITS_FROM / CONSTRAINED_BY / SUBSTITUTES / PRODUCES\n"
            "8. evidence 的 quote 必须是参考文档中的原文片段，不得编造\n"
            "9. evidence 的 source_type 只能是：research_report / news / announcement / annual_report / "
            "industry_data / expert_interview / patent / other"
        )

        user_prompt = (
            f"研究目标：{target}\n"
            f"缺口类型：{task.gap_type}\n"
            f"评分理由：{task.reason}\n\n"
            f"参考文档：\n{doc_context}\n\n"
            "请抽取实体、关系和证据："
        )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _format_docs(self, docs: list[Any]) -> str:
        """格式化召回文档为 LLM 上下文。

        Args:
            docs: RetrievedChunk 列表。

        Returns:
            格式化的文档文本。
        """
        if not docs:
            return ""
        parts: list[str] = []
        for i, doc in enumerate(docs[:5], 1):  # 最多 5 篇
            chunk = doc.chunk if hasattr(doc, "chunk") else doc
            text = getattr(chunk, "text", "") or ""
            title = getattr(chunk, "metadata", {}).get("title", "")
            source_type = getattr(chunk, "source_type", "unknown")
            # 截断过长文本
            if len(text) > 800:
                text = text[:800] + "..."
            parts.append(f"【文档{i}】标题={title} | 来源={source_type}\n{text}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 外部观察：调用 data_svc provider 获取新闻/研报/公告
    # ------------------------------------------------------------------

    def _get_external_provider(self) -> Any:
        """惰性加载外部数据源 provider（ProviderRegistry 兼容对象）。

        Returns:
            ProviderRegistry 实例；不可用时返回 None（降级为仅本地文档）。
        """
        if self._external_provider is not None:
            return self._external_provider
        if self._external_provider_loaded:
            return None
        self._external_provider_loaded = True
        try:
            from data_svc.providers.provider_registry import ProviderRegistry

            self._external_provider = ProviderRegistry()
            return self._external_provider
        except Exception as exc:
            logger.warning("外部 provider 不可用，降级为仅本地文档: %s", exc)
            return None

    def _fetch_external_documents(
        self, target: str, limit: int
    ) -> list[dict[str, Any]]:
        """从外部数据源获取文档（新闻/研报/公告）。

        优先调用 akshare 体系：semantic_search（新闻语义搜索）、
        get_research_reports（券商研报）、get_announcements（公告）。
        每个数据源独立 try-except，失败时降级跳过。

        Args:
            target: 研究目标（节点名称或股票代码）。
            limit: 最大返回数。

        Returns:
            标准化文档列表，每项含 doc_id/content/source_type/title/url。
        """
        provider = self._get_external_provider()
        if provider is None or limit <= 0:
            return []

        documents: list[dict[str, Any]] = []

        # 1. 语义搜索（对应 fetch_stock_news：问财/iFinD/AKShare/百度/DuckDuckGo 多级降级）
        try:
            result = provider.semantic_search(query=target, count=limit)
            for item in (result.get("results", []) or [])[:limit]:
                title = str(item.get("title", "")).strip()
                if not title:
                    continue
                snippet = str(
                    item.get("snippet")
                    or item.get("digest")
                    or item.get("summary")
                    or ""
                )
                documents.append({
                    "doc_id": f"ext_news_{hashlib.sha1(title.encode('utf-8')).hexdigest()[:12]}",
                    "content": snippet or title,
                    "source_type": "news",
                    "title": title,
                    "url": str(item.get("url") or item.get("link") or ""),
                })
        except Exception as exc:
            logger.warning("外部语义搜索失败 target=%s: %s", target, exc)

        # 2. 券商研报（对应 fetch_research_reports：AKShare stock_research_report_em）
        try:
            reports = provider.get_research_reports(code=target, count=limit)
            for item in (reports or [])[:limit]:
                title = str(item.get("title", "")).strip()
                if not title:
                    continue
                institution = str(item.get("institution", "")).strip()
                content_parts = [title]
                if institution:
                    content_parts.append(f"机构: {institution}")
                documents.append({
                    "doc_id": f"ext_report_{hashlib.sha1(title.encode('utf-8')).hexdigest()[:12]}",
                    "content": " | ".join(content_parts),
                    "source_type": "research_report",
                    "title": title,
                    "url": str(item.get("pdf_link") or ""),
                })
        except Exception as exc:
            logger.warning("外部研报获取失败 target=%s: %s", target, exc)

        # 3. 公告（对应 fetch_cls_news：CNINFO/AKShare 公告查询）
        try:
            announcements = provider.get_announcements(code=target, count=limit)
            for item in (announcements or [])[:limit]:
                title = str(item.get("title", "")).strip()
                if not title:
                    continue
                documents.append({
                    "doc_id": f"ext_ann_{hashlib.sha1(title.encode('utf-8')).hexdigest()[:12]}",
                    "content": title,
                    "source_type": "announcement",
                    "title": title,
                    "url": str(item.get("pdf_url") or item.get("link") or ""),
                })
        except Exception as exc:
            logger.warning("外部公告获取失败 target=%s: %s", target, exc)

        # 按 doc_id 去重（不同数据源可能返回相同标题）
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for doc in documents:
            if doc["doc_id"] in seen:
                continue
            seen.add(doc["doc_id"])
            unique.append(doc)

        return unique[:limit]

    def _build_external_retrieved_chunk(
        self, doc: dict[str, Any], topic: str
    ) -> Any:
        """将外部文档构造为 RetrievedChunk，便于 _format_docs/_merge_to_memory 复用。

        Args:
            doc: 标准化文档 dict（doc_id/content/source_type/title/url）。
            topic: 主题（研究目标）。

        Returns:
            RetrievedChunk 实例；构造失败时返回 None。
        """
        try:
            from core.research_graph.vector_memory import DocumentChunk, RetrievedChunk

            metadata: dict[str, Any] = {}
            if doc.get("title"):
                metadata["title"] = doc["title"]
            if doc.get("url"):
                metadata["url"] = doc["url"]

            chunk = DocumentChunk(
                chunk_id=doc["doc_id"],
                document_id=doc["doc_id"],
                text=doc.get("content", ""),
                source_type=doc.get("source_type", "unknown"),
                topic=topic,
                metadata=metadata,
            )
            return RetrievedChunk(
                chunk=chunk,
                score=0.0,
                vector_score=0.0,
                keyword_score=0.0,
                source_authority_score=0.0,
            )
        except Exception as exc:
            logger.warning("构造外部 RetrievedChunk 失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    def _call_llm(self, messages: list[dict[str, Any]]) -> str:
        """调用 LLM（带降级处理）。

        Args:
            messages: OpenAI 格式消息列表。

        Returns:
            LLM 生成的文本。

        Raises:
            RuntimeError: LLM 不可用或调用失败。
        """
        from core.utils.llm_usage import call_llm_with_budget

        return call_llm_with_budget(
            messages,
            source="autonomous_research_engine",
            temperature=0.0,
            max_completion_tokens=2048,
            response_format_json=True,
            timeout=self._llm_timeout,
            allow_no_provider=True,  # 无 provider 时返回空，不阻塞
            tier=self._llm_tier,
        )

    # ------------------------------------------------------------------
    # 输出解析
    # ------------------------------------------------------------------

    def _parse_extraction(self, raw: str) -> dict[str, Any]:
        """解析 LLM 输出为结构化抽取结果。

        Args:
            raw: LLM 原始输出文本。

        Returns:
            {"nodes": [...], "edges": [...], "evidence": [...]}
        """
        if not raw or not raw.strip():
            return {"nodes": [], "edges": [], "evidence": []}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 尝试提取 JSON 片段
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(raw[start:end])
            else:
                logger.warning("LLM 输出非 JSON: %s", raw[:200])
                return {"nodes": [], "edges": [], "evidence": []}

        return {
            "nodes": data.get("nodes", []) or [],
            "edges": data.get("edges", []) or [],
            "evidence": data.get("evidence", []) or [],
        }

    # ------------------------------------------------------------------
    # LLM 轨迹保存（满足 EvidenceLedger.extracted_by="llm" 校验）
    # ------------------------------------------------------------------

    def _save_trace(
        self,
        task: ResearchTask,
        extraction: dict[str, Any],
        raw_response: str,
    ) -> str:
        """保存 LLM 抽取轨迹到文件。

        Args:
            task: 研究任务。
            extraction: 解析后的抽取结果。
            raw_response: LLM 原始响应。

        Returns:
            轨迹文件路径。
        """
        from pathlib import Path

        trace_dir = Path(self._trace_dir)
        trace_dir.mkdir(parents=True, exist_ok=True)

        trace_id = hashlib.sha1(
            f"{task.task_id}|{time.time()}".encode("utf-8")
        ).hexdigest()[:16]
        trace_path = trace_dir / f"trace_{trace_id}.json"

        trace_data = {
            "task_id": task.task_id,
            "node_name": task.node_name,
            "gap_type": task.gap_type,
            "research_value": task.research_value,
            "timestamp": time.time(),
            "extraction": extraction,
            "raw_response": raw_response[:2000],  # 截断防止过大
        }

        try:
            trace_path.write_text(
                json.dumps(trace_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("保存 LLM 轨迹失败: %s", exc)
            return ""

        return str(trace_path)

    # ------------------------------------------------------------------
    # 合并回统一记忆
    # ------------------------------------------------------------------

    def _merge_to_memory(
        self,
        target: str,
        extraction: dict[str, Any],
        memory: UnifiedResearchMemory,
        trace_path: str,
        docs: list[Any],
    ) -> tuple[list[str], list[str], list[str]]:
        """将 LLM 抽取结果合并回统一记忆。

        Args:
            target: 研究目标名称。
            extraction: LLM 抽取结果。
            memory: 统一记忆层。
            trace_path: LLM 轨迹路径。
            docs: 召回的文档列表。

        Returns:
            (new_node_names, new_edge_ids, new_evidence_ids)
        """
        from core.research_graph.models import Evidence
        from core.research_graph.schema import EvidenceLevel

        new_nodes: list[str] = []
        new_edges: list[str] = []
        edge_context: dict[str, tuple[str, str, str]] = {}
        new_evidence: list[str] = []

        # 1. 确保目标节点存在
        target_node_id = self._ensure_node(
            memory, target, "CONCEPT", target, source="llm"
        )

        # 2. 添加抽取的节点
        node_name_to_id: dict[str, str] = {target: target_node_id}
        for node_data in extraction.get("nodes", [])[:self._max_nodes]:
            name = str(node_data.get("name", "")).strip()
            if not name or name == target:
                continue
            node_type = str(node_data.get("type", "CONCEPT")).strip()
            aliases = node_data.get("aliases", []) or []
            existed = memory.find_node_by_name(name) is not None
            node_id = self._ensure_node(
                memory, name, node_type, target, aliases, source="llm"
            )
            node_name_to_id[name] = node_id
            if node_id and not existed and name not in new_nodes:
                new_nodes.append(name)

        # 3. 添加抽取的边
        for edge_data in extraction.get("edges", [])[:self._max_edges]:
            subject_name = str(edge_data.get("subject", "")).strip()
            relation = str(edge_data.get("relation", "")).strip()
            object_name = str(edge_data.get("object", "")).strip()
            if not subject_name or not relation or not object_name:
                continue

            subject_id = node_name_to_id.get(subject_name) or self._ensure_node(
                memory, subject_name, "CONCEPT", target, source="llm"
            )
            object_id = node_name_to_id.get(object_name) or self._ensure_node(
                memory, object_name, "CONCEPT", target, source="llm"
            )

            if not subject_id or not object_id:
                continue

            qualifiers = edge_data.get("qualifiers", {}) or {}
            qualifiers.setdefault("source_topic", target)
            existed_edge = any(
                edge.relation == relation
                and edge.subjects == [subject_id]
                and edge.objects == [object_id]
                for edge in memory.graph_store.edges.values()
            )
            edge_id = memory.add_edge(
                subject_id,
                object_id,
                relation,
                qualifiers=qualifiers,
                topic=target,
                source="llm",
            )
            if edge_id and not existed_edge:
                new_edges.append(edge_id)
            if edge_id:
                edge_context[edge_id] = (subject_name, relation, object_name)

        # 4. 添加抽取的证据（按 quote 内容哈希绑定文档，无文档匹配则拒绝）
        from core.research_graph.evidence_grounding import find_grounded_document

        rejected_evidence_count = 0
        for i, ev_data in enumerate(extraction.get("evidence", [])):
            quote = str(ev_data.get("quote", "")).strip()
            if not quote:
                continue  # EvidenceLedger 要求 quote 非空

            source_title = str(ev_data.get("source_title", f"LLM 抽取证据_{i}"))
            source_type = str(ev_data.get("source_type", "research_report"))
            summary = str(ev_data.get("summary", ""))
            supports = ev_data.get("supports", []) or []

            # 4.1 按 quote 内容哈希绑定文档：遍历所有召回文档，找到 grounding 通过的文档
            # 不再按数组序号 i < len(docs) 绑定，避免错误绑定
            grounded = find_grounded_document(quote, docs)
            if grounded is None:
                # 无文档支撑：拒绝该 Evidence（不创建虚拟 document_id）
                rejected_evidence_count += 1
                logger.warning(
                    "证据被拒绝（无文档支撑）: target=%s, quote=%s",
                    target, quote[:80],
                )
                continue

            _, matched_doc = grounded
            chunk = getattr(matched_doc, "chunk", matched_doc)
            document_id = getattr(chunk, "document_id", "") or ""
            source_id = getattr(chunk, "source_id", "") or ""

            evidence_id = hashlib.sha1(
                f"{quote[:100]}|{target}|{i}|{time.time()}".encode("utf-8")
            ).hexdigest()[:16]

            metadata: dict[str, Any] = {
                "extracted_by": "llm",
                "target_node": target,
            }
            if trace_path:
                metadata["llm_trace_path"] = trace_path

            evidence = Evidence(
                evidence_id=evidence_id,
                source_type=source_type,
                source_title=source_title,
                source_id=source_id,
                document_id=document_id,
                quote=quote,
                summary=summary,
                evidence_level=EvidenceLevel.SPECULATIVE.value,
                supports=supports[:5],  # 限制长度
                extracted_by="llm_research_engine",
                metadata=metadata,
            )

            try:
                memory.add_evidence(evidence)
                new_evidence.append(evidence_id)
                # 写入主图后同步路由到神经元证据池，形成“吸收→状态更新”的闭环。
                try:
                    memory.route_evidence(evidence, topic=target)
                except Exception as route_exc:
                    logger.warning("证据路由到神经元失败: %s", route_exc)
            except Exception as exc:
                logger.warning("添加证据失败: %s", exc)
                continue

            # 4.2 Evidence 自动绑定到边：本次抽取创建的边均与该 Evidence 相关
            # 绑定后触发 verifier 重新评估边 confidence/status
            linked_edges: set[str] = set()
            for field_name, as_counter in (("supports", False), ("contradicts", True)):
                references = ev_data.get(field_name, []) or []
                if not isinstance(references, list):
                    references = [references]
                for reference in references:
                    ref = str(reference).strip()
                    if not ref:
                        continue
                    for edge_id, context in edge_context.items():
                        subject_name, relation, object_name = context
                        candidates = {
                            edge_id,
                            subject_name,
                            object_name,
                            f"{subject_name}|{relation}|{object_name}",
                        }
                        if ref not in candidates:
                            continue
                        memory.attach_evidence_to_edge(
                            edge_id,
                            evidence_id,
                            as_counter=as_counter,
                            operator="llm_research_engine",
                        )
                        linked_edges.add(edge_id)
            if not linked_edges:
                # Fallback: supports 为空时，自动绑定到本次创建的所有边
                # LLM 抽取了边和证据但未显式声明 supports 时，假设证据支撑所有本次创建的边
                for fallback_edge_id in edge_context:
                    memory.attach_evidence_to_edge(
                        fallback_edge_id,
                        evidence_id,
                        as_counter=False,
                        operator="llm_research_engine",
                    )
                    linked_edges.add(fallback_edge_id)

        if rejected_evidence_count > 0:
            logger.info(
                "证据 grounding 校验: target=%s, 拒绝=%d, 接受=%d",
                target, rejected_evidence_count, len(new_evidence),
            )

        return new_nodes, new_edges, new_evidence

    def _ensure_node(
        self,
        memory: UnifiedResearchMemory,
        name: str,
        node_type: str,
        topic: str,
        aliases: list[str] | None = None,
        source: str = "llm",
    ) -> str:
        """确保节点存在，返回 node_id。

        Args:
            memory: 统一记忆层。
            name: 节点名称。
            node_type: 节点类型。
            topic: 主题。
            aliases: 别名列表。

        Returns:
            节点 ID。
        """
        try:
            return memory.add_node(
                name,
                node_type=node_type,
                topic=topic,
                aliases=aliases,
                source=source,
            )
        except Exception as exc:
            logger.warning("添加节点失败 name=%s: %s", name, exc)
            return ""
