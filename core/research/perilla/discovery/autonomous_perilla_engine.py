"""Perilla Discovery Engine - 自主编排引擎.

完整的紫苏发现流程编排器，串联概念雷达、搜索发现、RSS 采集、
变更监控、聚焦爬虫、概念聚类、供应链构建、瓶颈挖掘、公司映射、
证据评分、概念标签、紫苏分类、假设管理、审核队列和报告构建
等全部模块。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase
from core.research.perilla.bridge.perilla_sag_bridge import PerillaSAGBridge, PerillaSearchTask
from core.research.perilla.discovery.bottleneck_miner import BottleneckMiner
from core.research.perilla.discovery.chain_constructor import DynamicChainConstructor
from core.research.perilla.discovery.change_monitor import ChangeMonitor
from core.research.perilla.discovery.company_mapper import CompanyMapper
from core.research.perilla.discovery.concept_claim_tagger import ConceptClaimTagger
from core.research.perilla.discovery.concept_clusterer import ConceptClusterer
from core.research.perilla.discovery.concept_discovery_engine import ConceptDiscoveryEngine
from core.research.perilla.discovery.concept_radar import ConceptRadar
from core.research.perilla.discovery.focused_crawler import FocusedCrawler
from core.research.perilla.discovery.hypothesis_manager import HypothesisManager
from core.research.perilla.discovery.models import (
    BottleneckHypothesis,
    CompanyCandidate,
    ConceptSignal,
    DemandAnchor,
    DynamicSupplyChain,
    EarlySignal,
    PerillaDiscoveryItem,
    PerillaDiscoveryReport,
    SourceDocument,
    new_id,
    now_iso,
)
from core.research.perilla.discovery.perilla_classifier import PerillaClassifier
from core.research.perilla.discovery.perilla_leaf_screener import PerillaLeafCandidate, PerillaLeafScreener, render_perilla_leaf_report_md
from core.research.perilla.discovery.report_builder import ReportBuilder
from core.research.perilla.discovery.review_queue import ReviewQueue
from core.research.perilla.discovery.rss_collector import RSSCollector
from core.research.perilla.discovery.search_discovery import SearchDiscovery
from core.research.perilla.evidence.evidence_grader import EvidenceGrader
from core.research.perilla.evidence.source_registry import SourceRegistry

logger = logging.getLogger(__name__)

# PerillaSearchTask -> 质量阶梯映射
TIER_ORDER: tuple[str, ...] = ("high", "medium", "low")
PREFERRED_SOURCE_TO_TIER: dict[str, str] = {
    "announcement": "high",
    "annual_report": "high",
    "broker_report": "high",
    "industry_whitepaper": "high",
    "patent": "high",
    "standard": "high",
    "official_website": "high",
    "news": "medium",
    "wencai": "medium",
    "web_search_snippet": "low",
}
CAPABILITY_HINT_TO_TIER: dict[str, str] = {
    "announcements": "high",
    "reports": "high",
    "semantic_search": "low",
    "market_data": "low",
}


def _safe_nonnegative_int(value: object, default: int) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_text(value: object, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def _safe_path_text(value: object, default: str = "") -> str:
    try:
        text = os.fspath(value)
    except (TypeError, ValueError):
        return default
    return text if isinstance(text, str) and text else default


def _safe_filename_component(value: object, default: str = "concept") -> str:
    text = _safe_text(value).strip()
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", text).strip("._")
    return text[:80] or default


class AutonomousPerillaEngine:
    """自主紫苏发现引擎 —— 主编排器.

    串联所有子模块，执行完整的发现流程：
    概念发现 → 来源采集 → 供应链构建 → 瓶颈挖掘 →
    公司映射 → 证据评分 → 分类 → 报告生成。

    每个步骤均包裹 try/except，单步失败不中断整体流程。

    Args:
        mode: 运行模式 (auto / topic)。
        topic: 主题模式下的输入主题。
        lookback_days: 概念回溯天数。
        max_concepts: 最大概念数量。
        max_sources_per_concept: 每个概念最大来源数。
        max_candidates: 最大公司候选数量。
        enable_search: 是否启用搜索发现。
        enable_rss: 是否启用 RSS 采集。
        enable_change_monitor: 是否启用变更监控。
        enable_crawler: 是否启用聚焦爬虫。
        enable_playwright: 是否启用 Playwright（预留）。
        enable_firecrawl: 是否启用 Firecrawl（预留）。
        manual_signal_file: 手动信号文件路径。
        source_registry_path: 来源注册表 YAML 路径。
        no_llm: 是否禁用 LLM（仅用回退种子模式）。
        mock_web: 是否模拟 Web（跳过所有网络请求）。
        output_dir: 报告输出目录。
        kb: SAG 知识库实例，None 则按 storage_path 新建。
        storage_path: SAG 持久化路径，与 kb 二选一。
        bridge: Perilla-SAG 桥接层，None 则自动创建。
    """

    def __init__(
        self,
        mode: str = "auto",
        topic: Optional[str] = None,
        lookback_days: int = 30,
        max_concepts: int = 5,
        max_sources_per_concept: int = 10,
        max_candidates: int = 20,
        enable_search: bool = False,
        enable_rss: bool = False,
        enable_change_monitor: bool = False,
        enable_crawler: bool = False,
        enable_playwright: bool = False,
        enable_firecrawl: bool = False,
        manual_signal_file: str = "",
        source_registry_path: str = "",
        no_llm: bool = False,
        mock_web: bool = False,
        output_dir: str = "data/reports/perilla_discovery",
        kb: Optional[SAGKnowledgeBase] = None,
        storage_path: str = "",
        bridge: Optional[PerillaSAGBridge] = None,
    ) -> None:
        """初始化自主紫苏发现引擎."""
        # 参数校验
        if mode not in ("auto", "topic"):
            logger.warning("未知运行模式 '%s'，降级为 auto", mode)
            mode = "auto"

        self._mode = mode
        self._topic = topic.strip() if isinstance(topic, str) and topic.strip() else None
        self._lookback_days = _safe_nonnegative_int(lookback_days, 30)
        self._max_concepts = _safe_nonnegative_int(max_concepts, 5)
        self._max_sources_per_concept = _safe_nonnegative_int(max_sources_per_concept, 10)
        self._max_candidates = _safe_nonnegative_int(max_candidates, 20)
        self._enable_search = enable_search
        self._enable_rss = enable_rss
        self._enable_change_monitor = enable_change_monitor
        self._enable_crawler = enable_crawler
        self._enable_playwright = enable_playwright
        self._enable_firecrawl = enable_firecrawl
        self._manual_signal_file = _safe_path_text(manual_signal_file)
        self._source_registry_path = _safe_path_text(source_registry_path)
        self._no_llm = no_llm
        self._mock_web = mock_web
        self._output_dir = _safe_path_text(
            output_dir, "data/reports/perilla_discovery",
        )

        # SAG 与桥接层
        if kb is None and storage_path:
            kb = SAGKnowledgeBase(storage_path=storage_path)
        self._kb = kb
        self._bridge = bridge or (PerillaSAGBridge(kb=self._kb) if self._kb else None)

        # 初始化子模块
        self._radar = ConceptRadar()
        self._concept_discovery_engine = ConceptDiscoveryEngine(output_dir=self._output_dir)
        self._clusterer = ConceptClusterer()
        self._chain_constructor = DynamicChainConstructor()
        self._bottleneck_miner = BottleneckMiner()
        self._company_mapper = CompanyMapper()
        self._evidence_grader = EvidenceGrader()
        self._claim_tagger = ConceptClaimTagger()
        self._classifier = PerillaClassifier()
        self._hypothesis_manager = HypothesisManager()
        self._review_queue = ReviewQueue()
        self._report_builder = ReportBuilder()

        # 懒初始化的网络模块
        self._search: Optional[SearchDiscovery] = None
        self._rss_collector: Optional[RSSCollector] = None
        self._change_monitor: Optional[ChangeMonitor] = None
        self._crawler: Optional[FocusedCrawler] = None

        # 来源注册表
        self._registry = SourceRegistry()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(
        self,
        pending_search_tasks: Optional[List[PerillaSearchTask]] = None,
    ) -> PerillaDiscoveryReport:
        """执行完整的紫苏发现流程.

        流程步骤：
        1. 加载来源注册表
        2. 概念雷达（自动发现或主题发现）
        3. 搜索发现（如已启用）
        4. RSS/变更/手动信号采集
        5. 聚焦爬虫 + 文本提取（如已启用）
        6. 概念聚类
        7. 逐概念处理（供应链构建 → 瓶颈挖掘 → 公司映射 → 证据评分 → 概念标签）
        8. 紫苏分类
        9. 假设管理
        10. 审核队列
        11. 报告构建
        12. 将信号写入 SAG Event Graph（若配置了 bridge）
        13. 持久化 SAG

        Args:
            pending_search_tasks: 来自 AlphaDiscoveryEngine 的搜索任务。

        Returns:
            PerillaDiscoveryReport 实例。
        """
        run_id = new_id("run_")
        logger.info("===== Perilla Discovery Engine 启动 =====")
        logger.info("Run ID: %s, 模式: %s", run_id, self._mode)

        # 全局收集容器
        all_concept_signals: List[ConceptSignal] = []
        all_demand_anchors: List[DemandAnchor] = []
        all_source_docs: List[SourceDocument] = []
        all_early_signals: List[EarlySignal] = []
        all_chains: List[DynamicSupplyChain] = []
        all_bottlenecks: List[BottleneckHypothesis] = []
        all_candidates: List[CompanyCandidate] = []
        all_items: List[PerillaDiscoveryItem] = []

        # --- Step 1: 加载来源注册表 ---
        self._step_load_registry()

        # --- Step 2: 概念雷达 ---
        try:
            all_concept_signals = self._step_concept_radar()
            logger.info("概念雷达: 发现 %d 个概念信号", len(all_concept_signals))
        except Exception as exc:
            logger.error("概念雷达步骤失败: %s", exc, exc_info=True)

        # 限制概念数量
        if len(all_concept_signals) > self._max_concepts:
            logger.info(
                "概念信号数 %d 超过上限 %d，截取前 %d 个",
                len(all_concept_signals), self._max_concepts, self._max_concepts,
            )
            all_concept_signals = all_concept_signals[: self._max_concepts]

        # --- Step 3: 搜索发现 ---
        if self._enable_search and not self._mock_web:
            try:
                search_docs = self._step_search_discovery(all_concept_signals)
                all_source_docs.extend(search_docs)
                logger.info("搜索发现: 获取 %d 篇文档", len(search_docs))
            except Exception as exc:
                logger.error("搜索发现步骤失败: %s", exc, exc_info=True)

        # --- Step 3b: 执行来自 AlphaDiscoveryEngine 的搜索任务 ---
        if pending_search_tasks and not self._mock_web:
            try:
                try:
                    if os.getenv("RESEARCH_CONTROL_PLANE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
                        from core.research.control_plane.contracts import ResearchTask
                        from core.research.control_plane.repository import ResearchRepository
                        repository = ResearchRepository(os.getenv("RESEARCH_CONTROL_DB_PATH", "data/research_control/research_control.db"))
                        for legacy_task in pending_search_tasks:
                            payload = legacy_task.to_dict() if hasattr(legacy_task, "to_dict") else dict(legacy_task)
                            repository.save_task(ResearchTask(
                                task_id=str(payload.get("task_id") or ""),
                                task_type="evidence_completion",
                                trigger_source="alpha_gap",
                                target_claim_id=str(payload.get("claim_id") or payload.get("candidate_id") or ""),
                                priority=float(payload.get("priority_score") or payload.get("priority") or 0.0),
                                params={**payload, "evidence_kind": "retrieval_hypothesis"},
                            ))
                except Exception as control_exc:
                    logger.warning("统一研究任务登记失败，继续兼容搜索: %s", control_exc)
                task_docs = self._execute_pending_search_tasks(pending_search_tasks)
                all_source_docs.extend(task_docs)
                logger.info("Alpha 缺口任务搜索: 获取 %d 篇文档", len(task_docs))
            except Exception as exc:
                logger.error("Alpha 缺口任务搜索失败: %s", exc, exc_info=True)

        # --- Step 4: RSS/变更/手动信号采集 ---
        try:
            rss_docs, change_docs, manual_docs, manual_early_signals = self._step_signal_collection()
            all_source_docs.extend(rss_docs)
            all_source_docs.extend(change_docs)
            all_source_docs.extend(manual_docs)
            all_early_signals.extend(manual_early_signals)
            logger.info(
                "信号采集: RSS=%d, 变更=%d, 手动=%d, 手动信号=%d",
                len(rss_docs), len(change_docs), len(manual_docs), len(manual_early_signals),
            )
        except Exception as exc:
            logger.error("信号采集步骤失败: %s", exc, exc_info=True)

        # --- Step 5: 聚焦爬虫 ---
        if self._enable_crawler and not self._mock_web:
            try:
                crawl_docs = self._step_focused_crawl(all_source_docs)
                all_source_docs.extend(crawl_docs)
                logger.info("聚焦爬虫: 获取 %d 篇文档", len(crawl_docs))
            except Exception as exc:
                logger.error("聚焦爬虫步骤失败: %s", exc, exc_info=True)

        # --- Step 6: 概念聚类 ---
        try:
            all_concept_signals = self._step_concept_clustering(all_concept_signals)
            logger.info("概念聚类: 合并后 %d 个概念", len(all_concept_signals))
        except Exception as exc:
            logger.error("概念聚类步骤失败: %s", exc, exc_info=True)

        # --- Step 7: 逐概念处理 ---
        for concept in all_concept_signals:
            try:
                # 7a. 需求锚点识别
                anchors = self._clusterer.identify_demand_anchors(
                    concept, all_source_docs,
                )
                all_demand_anchors.extend(anchors)

                # 7b. 来源采集
                concept_docs, concept_signals = self._collect_sources(concept)
                all_source_docs.extend(concept_docs)
                all_early_signals.extend(concept_signals)

                # 7c-e. 供应链构建、瓶颈挖掘、公司映射
                for anchor in anchors:
                    chain, bottlenecks, candidates = self._process_concept(
                        concept, [anchor], concept_docs,
                        concept_signals + all_early_signals,
                    )
                    if chain:
                        all_chains.append(chain)
                    all_bottlenecks.extend(bottlenecks)

                # 7d. 证据评分
                for candidate in candidates:
                    try:
                        self._step_evidence_grading(candidate, concept_docs, concept_signals)
                    except Exception as exc:
                        logger.debug("证据评分失败 %s: %s", candidate.company_name, exc)

                # 7e. 概念声明标签
                try:
                    tagged_candidates = self._claim_tagger.tag_concept_claims(
                        candidates, concept_docs,
                    )
                    if isinstance(tagged_candidates, list):
                        candidates = tagged_candidates
                    all_candidates.extend(candidates)
                except Exception as exc:
                    if isinstance(candidates, list):
                        all_candidates.extend(candidates)
                    logger.debug("概念标签失败: %s", exc)

            except Exception as exc:
                logger.error(
                    "概念 '%s' 处理失败: %s", concept.name, exc, exc_info=True,
                )

        # 限制候选数量
        if len(all_candidates) > self._max_candidates:
            logger.info(
                "公司候选数 %d 超过上限 %d，截取前 %d 个",
                len(all_candidates), self._max_candidates, self._max_candidates,
            )
            all_candidates = all_candidates[: self._max_candidates]

        # --- Step 8: 紫苏分类 ---
        try:
            for chain in all_chains:
                chain_bottlenecks = [
                    b for b in all_bottlenecks if b.chain_id == chain.chain_id
                ]
                chain_candidates = [
                    c for c in all_candidates if c.concept_id == chain.concept_id
                ]
                items = self._classifier.classify(
                    chain_candidates, chain_bottlenecks, chain,
                )
                all_items.extend(items)
            logger.info("紫苏分类: 生成 %d 个发现条目", len(all_items))
        except Exception as exc:
            logger.error("紫苏分类步骤失败: %s", exc, exc_info=True)

        # --- Step 9: 假设管理 ---
        try:
            for chain in all_chains:
                chain_bottlenecks = [
                    b for b in all_bottlenecks if b.chain_id == chain.chain_id
                ]
                chain_candidates = [
                    c for c in all_candidates if c.concept_id == chain.concept_id
                ]
                self._hypothesis_manager.from_chain_and_bottlenecks(
                    chain, chain_bottlenecks, chain_candidates,
                )
            logger.info(
                "假设管理: 注册 %d 条假设",
                len(self._hypothesis_manager.get_hypotheses()),
            )
        except Exception as exc:
            logger.error("假设管理步骤失败: %s", exc, exc_info=True)

        # --- Step 10: 审核队列 ---
        review_data: List[Dict] = []
        try:
            review_items = self._review_queue.build_queue(all_items, all_candidates)
            # 将 ReviewItem 转为字典存入报告
            from dataclasses import asdict
            review_data = [asdict(r) for r in review_items]
            logger.info("审核队列: %d 条待审核", len(review_data))
        except Exception as exc:
            logger.error("审核队列步骤失败: %s", exc, exc_info=True)

        # --- Step 11: 报告构建 ---
        source_stats = self._compute_source_stats(all_source_docs)
        report = PerillaDiscoveryReport(
            run_id=run_id,
            mode=self._mode,
            input_topic=self._topic,
            concept_signals=all_concept_signals,
            demand_anchors=all_demand_anchors,
            dynamic_chains=all_chains,
            bottleneck_hypotheses=all_bottlenecks,
            company_candidates=all_candidates,
            discovery_items=all_items,
            review_queue=review_data,
            source_stats=source_stats,
            generated_at=now_iso(),
            disclaimer=self._build_disclaimer(),
        )

        try:
            md_path = self._report_builder.build_report(report, self._output_dir)
            logger.info("报告已生成: %s", md_path)
        except Exception as exc:
            logger.error("报告构建步骤失败: %s", exc, exc_info=True)

        # Step 12: 紫苏叶选股法（六步工作流）
        try:
            self._run_perilla_leaf_screener(
                report, all_concept_signals, all_demand_anchors,
                all_chains, all_bottlenecks, all_candidates,
                all_source_docs, all_early_signals,
            )
        except Exception as exc:
            logger.error("紫苏叶选股法步骤失败: %s", exc, exc_info=True)

        # Step 13: 将 Perilla 信号写入 SAG Event Graph
        if self._bridge is not None:
            try:
                self._ingest_signals_to_sag(
                    all_concept_signals,
                    all_bottlenecks,
                    all_early_signals,
                    all_source_docs,
                )
            except Exception as exc:
                logger.error("写入 SAG Event Graph 失败: %s", exc, exc_info=True)

        # Step 14: 持久化 SAG
        if self._kb is not None:
            if self._kb.save():
                logger.info("SAG 已持久化: %s", self._kb.storage_path)
            else:
                logger.error("SAG 持久化失败，数据可能未保存: %s", self._kb.storage_path)

        logger.info("===== Perilla Discovery Engine 完成 =====")
        return report

    # ------------------------------------------------------------------
    # 步骤方法
    # ------------------------------------------------------------------

    def _step_load_registry(self) -> None:
        """加载来源注册表.

        从 YAML 文件加载来源配置，文件不存在时使用空注册表。
        """
        if not self._source_registry_path:
            logger.debug("未指定来源注册表路径，使用空注册表")
            return

        try:
            self._registry = SourceRegistry.load_from_yaml(self._source_registry_path)
            logger.info("来源注册表加载完成: %s", self._registry)
        except Exception as exc:
            logger.warning("来源注册表加载失败: %s，使用空注册表", exc)
            self._registry = SourceRegistry()

    def _step_concept_radar(self) -> List[ConceptSignal]:
        """执行概念雷达发现.

        auto 模式调用 auto_discover_concepts，
        topic 模式调用 discover_from_topic。

        Returns:
            概念信号列表。
        """
        if self._mode == "topic" and self._topic:
            signal = self._radar.discover_from_topic(self._topic)
            return [signal]

        signals = self._radar.auto_discover_concepts(
            lookback_days=self._lookback_days,
        )
        if self._mock_web:
            return signals

        try:
            discovered = self._concept_discovery_engine.run()
            discovered_signals = self._concept_discovery_engine.to_concept_signals(
                discovered,
            )
            signals = self._merge_concept_signals(signals, discovered_signals)
            logger.info(
                "概念自发掘: 合并 discovered concepts 后共 %d 个概念信号",
                len(signals),
            )
        except Exception as exc:
            logger.error("概念自发掘步骤失败: %s", exc, exc_info=True)
        return signals

    @staticmethod
    def _merge_concept_signals(
        primary: List[ConceptSignal],
        discovered: List[ConceptSignal],
    ) -> List[ConceptSignal]:
        """Merge radar signals with inbox-discovered concept signals by name/alias."""
        merged: list[ConceptSignal] = []
        seen: set[str] = set()

        def keys_for(signal: ConceptSignal) -> set[str]:
            values = [signal.name] + list(signal.aliases or [])
            return {str(value).strip().upper() for value in values if str(value).strip()}

        for signal in primary + discovered:
            keys = keys_for(signal)
            if not keys:
                continue
            if seen.intersection(keys):
                for existing in merged:
                    if keys_for(existing).intersection(keys):
                        existing.aliases = sorted(set(existing.aliases + signal.aliases))
                        existing.detected_from = sorted(
                            set(existing.detected_from + signal.detected_from)
                        )
                        existing.source_types = sorted(
                            set(existing.source_types + signal.source_types)
                        )
                        existing.raw_evidence_ids = sorted(
                            set(existing.raw_evidence_ids + signal.raw_evidence_ids)
                        )
                        existing.heat_score = max(existing.heat_score, signal.heat_score)
                        existing.novelty_score = max(
                            existing.novelty_score,
                            signal.novelty_score,
                        )
                        if signal.notes and signal.notes not in existing.notes:
                            existing.notes = (
                                f"{existing.notes}; {signal.notes}"
                                if existing.notes else signal.notes
                            )
                        break
                seen.update(keys)
                continue
            merged.append(signal)
            seen.update(keys)

        merged.sort(key=lambda item: item.heat_score, reverse=True)
        return merged

    def _step_search_discovery(
        self, concepts: List[ConceptSignal],
    ) -> List[SourceDocument]:
        """执行搜索发现.

        为每个概念构建搜索查询并执行搜索。

        Args:
            concepts: 概念信号列表。

        Returns:
            搜索结果文档列表。
        """
        if self._search is None:
            try:
                from config.secrets import Secrets
                base_url = getattr(Secrets, "SEARXNG_BASE_URL", "")
            except ImportError:
                base_url = ""
            self._search = SearchDiscovery(
                searxng_base_url=base_url,
                max_results=self._max_sources_per_concept,
                enabled=self._enable_search,
            )

        all_docs: List[SourceDocument] = []
        seen_queries: set[str] = set()
        for concept in concepts:
            queries = self._search.build_queries_for_concept(concept.name)
            for query in queries:
                normalized_query = str(query).strip()
                if not normalized_query or normalized_query in seen_queries:
                    continue
                seen_queries.add(normalized_query)
                try:
                    results = self._search.search(normalized_query)
                    all_docs.extend(results)
                except Exception as exc:
                    logger.debug("搜索查询失败 '%s': %s", query, exc)

        # URL 去重
        return self._search.dedupe_urls(all_docs)

    def _execute_pending_search_tasks(
        self,
        tasks: List[PerillaSearchTask],
    ) -> List[SourceDocument]:
        """执行 AlphaDiscoveryEngine 下发的搜索任务，按质量阶梯分批调度.

        先执行 high tier（公告/财报/研报），若收集到足够文档则跳过 medium/low。

        Args:
            tasks: PerillaSearchTask 列表。

        Returns:
            新获取的 SourceDocument 列表。
        """
        if not tasks or self._mock_web:
            return []

        if self._search is None:
            try:
                from config.secrets import Secrets
                base_url = getattr(Secrets, "SEARXNG_BASE_URL", "")
            except ImportError:
                base_url = ""
            self._search = SearchDiscovery(
                searxng_base_url=base_url,
                max_results=self._max_sources_per_concept,
                enabled=True,
            )

        tiered = self._classify_pending_tasks_by_tier(tasks)
        from core.research.control_plane.repository import ResearchRepository
        from core.research.control_plane.tier_orchestrator import AdaptiveSearchBudgetPolicy, PerillaOrchestrator

        orchestrator = PerillaOrchestrator(AdaptiveSearchBudgetPolicy(ResearchRepository()))

        def run_tier(_tier: str, batch: List[PerillaSearchTask]) -> tuple[list[Any], List[SourceDocument], bool]:
            docs: List[SourceDocument] = []
            counter_attempted = False
            counter_succeeded = True
            for task in batch:
                docs.extend(self._execute_pending_search_tasks_tier([task]))
                keywords = [str(item) for item in task.query_keywords if item]
                if keywords:
                    counter_attempted = True
                    counter_query = " ".join(keywords[:4]) + " 否认 澄清 未供货 终止"
                    try:
                        counter_docs = self._search.search(counter_query)
                        for doc in counter_docs:
                            doc.metadata = dict(doc.metadata or {})
                            doc.metadata["query_purpose"] = "counter"
                        docs.extend(counter_docs)
                    except Exception as exc:
                        counter_succeeded = False
                        logger.debug("反证搜索失败 '%s': %s", counter_query, exc)
            return [], docs, counter_attempted and counter_succeeded

        execution = orchestrator.execute(
            tiered,
            run_tier,
            total_budget=max(1, len(tasks)),
        )
        logger.info(
            "Alpha 缺口统一分层检索: tiers=%s decision=%s",
            execution.tiers_executed,
            execution.decision.reason if execution.decision else "无证据",
        )
        return self._search.dedupe_urls(execution.documents)

    @staticmethod
    def _classify_pending_tasks_by_tier(
        tasks: List[PerillaSearchTask],
    ) -> Dict[str, List[PerillaSearchTask]]:
        """按 preferred_sources / capability_hint 把任务分组到质量阶梯."""
        grouped: Dict[str, List[PerillaSearchTask]] = {tier: [] for tier in TIER_ORDER}
        if not isinstance(tasks, (list, tuple)):
            return grouped
        for task in tasks:
            if not isinstance(task, PerillaSearchTask):
                continue
            # preferred_sources 比 capability_hint 更具体，优先使用
            tier = ""
            preferred_sources = task.preferred_sources
            if isinstance(preferred_sources, (list, tuple)) and preferred_sources:
                first_source = str(preferred_sources[0]).strip().lower()
                tier = PREFERRED_SOURCE_TO_TIER.get(first_source, "")
            if not tier:
                tier = CAPABILITY_HINT_TO_TIER.get(str(task.capability_hint or ""), "")
            if not tier:
                tier = "low"
            grouped[tier].append(task)
        return grouped

    def _execute_pending_search_tasks_tier(
        self,
        tasks: List[PerillaSearchTask],
    ) -> List[SourceDocument]:
        """执行单个 tier 的 pending 搜索任务."""
        all_docs: List[SourceDocument] = []
        seen: set[str] = set()
        for task in tasks:
            if task.status != "pending":
                continue
            keywords = [k for k in task.query_keywords if k]
            if not keywords:
                continue
            query = " ".join(keywords[:4])
            if query in seen:
                continue
            seen.add(query)
            try:
                results = self._search.search(query)
                all_docs.extend(results)
                task.status = "completed"
            except Exception as exc:
                logger.debug("Alpha 缺口任务搜索失败 '%s': %s", query, exc)
                task.status = "failed"
        return all_docs

    def _step_signal_collection(
        self,
    ) -> Tuple[List[SourceDocument], List[SourceDocument], List[SourceDocument], List[EarlySignal]]:
        """执行 RSS、变更监控和手动信号采集.

        Returns:
            (rss_docs, change_docs, manual_docs, manual_early_signals) 四元组。
            manual_early_signals 包含从手动信号文档中提取的早期信号。
        """
        rss_docs: List[SourceDocument] = []
        change_docs: List[SourceDocument] = []
        manual_docs: List[SourceDocument] = []
        manual_early_signals: List[EarlySignal] = []
        early_signals_from_manual: List[EarlySignal] = []

        # RSS 采集
        if self._enable_rss and not self._mock_web:
            try:
                if self._rss_collector is None:
                    self._rss_collector = RSSCollector()

                # 从注册表获取 RSS 源
                rss_sources = self._registry.filter_by_type("rss")
                # 也从 enabled sources 中筛选 base_url 非空的
                for entry in self._registry.get_enabled_sources():
                    if entry.crawl_method == "rss" and entry.base_url:
                        rss_sources.append(entry)

                feed_urls = list(dict.fromkeys(
                    s.base_url for s in rss_sources if s.base_url
                ))
                if feed_urls:
                    rss_docs = self._rss_collector.collect_multiple(feed_urls)
            except Exception as exc:
                logger.error("RSS 采集失败: %s", exc, exc_info=True)

        # 变更监控
        if self._enable_change_monitor and not self._mock_web:
            try:
                if self._change_monitor is None:
                    self._change_monitor = ChangeMonitor()

                # 从注册表获取变更检测源
                change_sources = [
                    e for e in self._registry.get_enabled_sources()
                    if e.crawl_method == "change_detection"
                ]
                for entry in change_sources:
                    if entry.base_url:
                        # 假设 base_url 指向 changedetection.io JSON 导出文件
                        docs = self._change_monitor.import_changes(entry.base_url)
                        change_docs.extend(docs)
            except Exception as exc:
                logger.error("变更监控失败: %s", exc, exc_info=True)

        # 手动信号（含 perilla_real_signals_*.json 自动加载）
        try:
            manual_docs, manual_file_signals = self._load_manual_signals()
            early_signals_from_manual.extend(manual_file_signals)
        except Exception as exc:
            logger.error("手动信号加载失败: %s", exc, exc_info=True)

        # 从手动信号文档中提取早期信号（不含公司信息的通用文档）
        for doc in manual_docs:
            # 含有公司提及的手动文档生成 EarlySignal
            if doc.source_type in ("manual", "announcement", "report", "concept_page", "sector_page"):
                manual_early_signals.append(EarlySignal(
                    signal_id=new_id("ms_"),
                    concept_id="",
                    source_document_id=doc.document_id,
                    signal_type=doc.source_type or "manual",
                    claim=doc.title or "",
                    evidence_text=(doc.content_text or "")[:200],
                    confidence="low",
                    evidence_level="EARLY_SIGNAL",
                ))

        # 合并手动信号文件生成的 EarlySignal
        manual_early_signals.extend(early_signals_from_manual)

        return rss_docs, change_docs, manual_docs, manual_early_signals

    def _step_focused_crawl(
        self, existing_docs: List[SourceDocument],
    ) -> List[SourceDocument]:
        """执行聚焦爬虫.

        从已有文档中提取 URL 进行深度爬取。

        Args:
            existing_docs: 已有文档列表（提取 URL 用）。

        Returns:
            新爬取的文档列表。
        """
        if not isinstance(existing_docs, (list, tuple)):
            return []
        if self._crawler is None:
            self._crawler = FocusedCrawler()

        # 从已有文档中收集未缓存的 URL
        urls_to_crawl: List[str] = []
        seen_urls: set[str] = set()
        for doc in existing_docs:
            if not isinstance(doc, SourceDocument) or not isinstance(doc.url, str):
                continue
            url = doc.url.strip()
            if url and url not in seen_urls and not self._crawler.is_url_cached(url):
                seen_urls.add(url)
                urls_to_crawl.append(url)

        # 限制爬取数量
        max_crawl = self._max_sources_per_concept * self._max_concepts
        urls_to_crawl = urls_to_crawl[:max_crawl]

        if not urls_to_crawl:
            logger.debug("聚焦爬虫: 无新 URL 需要爬取")
            return []

        return self._crawler.crawl_urls(urls_to_crawl)

    def _step_concept_clustering(
        self, signals: List[ConceptSignal],
    ) -> List[ConceptSignal]:
        """执行概念聚类.

        Args:
            signals: 原始概念信号列表。

        Returns:
            合并后的概念信号列表。
        """
        return self._clusterer.cluster_concepts(signals)

    def _step_evidence_grading(
        self,
        candidate: CompanyCandidate,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> None:
        """对公司候选执行证据评分，就地更新 candidate 字段.

        Args:
            candidate: 公司候选实例。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。
        """
        evidence_level, confidence, needs_review, why_not, upgrade_path = (
            self._evidence_grader.grade_evidence(candidate, source_docs, early_signals)
        )
        candidate.evidence_level = evidence_level
        candidate.needs_human_review = needs_review

    # ------------------------------------------------------------------
    # 概念级处理
    # ------------------------------------------------------------------

    def _collect_gateway_sources(
        self, concept: ConceptSignal,
    ) -> Tuple[List[SourceDocument], List[EarlySignal]]:
        """从 ChinaStockDataGateway 采集来源文档和早期信号.

        依次调用 Gateway 的 fetch_themes / fetch_sector / fetch_reports /
        fetch_announcements 接口，将结果转为 SourceDocument 和 EarlySignal。

        Args:
            concept: 概念信号实例。

        Returns:
            (source_docs, early_signals) 二元组。
        """
        source_docs: List[SourceDocument] = []
        early_signals: List[EarlySignal] = []

        try:
            from data_svc.gateways.china_stock_data_gateway import ChinaStockDataGateway
            gw = ChinaStockDataGateway()
            concept_name = concept.name

            # 1. fetch_themes - 获取热点题材（含领涨股）
            theme_result = gw.fetch_themes()
            if theme_result.success and theme_result.parsed_items:
                for itm in theme_result.parsed_items:
                    if not isinstance(itm, dict):
                        continue
                    board_name = itm.get("板块", "")
                    leading_stock = itm.get("领涨股", "")
                    heat = itm.get("涨跌幅(%)", "0")
                    if board_name:
                        doc = SourceDocument(
                            document_id=new_id("doc_"),
                            source_type="concept_page",
                            url="",
                            title=f"热点题材: {board_name}",
                            site_name="同花顺",
                            content_text=f"板块: {board_name}, 涨跌幅: {heat}%, 领涨股: {leading_stock}",
                            credibility_hint="high",
                        )
                        source_docs.append(doc)
                        # 领涨股作为早期信号
                        if leading_stock:
                            early_signals.append(EarlySignal(
                                signal_id=new_id("sig_"),
                                concept_id=concept.concept_id,
                                source_document_id=doc.document_id,
                                signal_type="concept_claim",
                                mentioned_companies=[leading_stock],
                                claim=f"{board_name}板块领涨股: {leading_stock}",
                                evidence_text=f"板块: {board_name}, 领涨股: {leading_stock}",
                                confidence="medium",
                                evidence_level="CONCEPT_CLAIM_ONLY",
                            ))

            # 2. fetch_sector - 获取行业板块
            sector_result = gw.fetch_sector()
            if sector_result.success and sector_result.parsed_items:
                for itm in sector_result.parsed_items:
                    if not isinstance(itm, dict):
                        continue
                    board_name = itm.get("板块", "")
                    leading_stock = itm.get("领涨股", "")
                    if board_name:
                        doc = SourceDocument(
                            document_id=new_id("doc_"),
                            source_type="sector_page",
                            url="",
                            title=f"行业板块: {board_name}",
                            site_name="同花顺",
                            content_text=f"板块: {board_name}, 领涨股: {leading_stock}",
                            credibility_hint="high",
                        )
                        source_docs.append(doc)

            # 3. fetch_reports - 搜索相关研报
            # 尝试用概念别名中的股票代码搜索研报
            for alias in (concept.aliases or []):
                if alias and len(alias) == 6 and alias.isdigit():
                    report_result = gw.fetch_reports(alias, limit=5)
                    if report_result.success and report_result.parsed_items:
                        for itm in report_result.parsed_items:
                            if not isinstance(itm, dict):
                                continue
                            title = itm.get("研报内容", itm.get("报告名称", ""))
                            institution = itm.get("机构名称", "")
                            date = itm.get("报告日期", "")
                            pdf_url = itm.get("PDF链接", "")
                            stock_name = itm.get("股票名称", "")
                            if title:
                                doc = SourceDocument(
                                    document_id=new_id("doc_"),
                                    source_type="report",
                                    url=pdf_url,
                                    title=title,
                                    site_name=institution,
                                    published_at=date,
                                    content_text=f"{title} ({institution}, {date})",
                                    credibility_hint="high",
                                )
                                source_docs.append(doc)
                                # 研报中提到的公司作为早期信号
                                if stock_name:
                                    early_signals.append(EarlySignal(
                                        signal_id=new_id("sig_"),
                                        concept_id=concept.concept_id,
                                        source_document_id=doc.document_id,
                                        signal_type="concept_claim",
                                        mentioned_companies=[stock_name],
                                        claim=f"研报覆盖: {title}",
                                        evidence_text=title,
                                        confidence="medium",
                                        evidence_level="PROBABLE_SUPPLIER",
                                    ))
                    break  # 只用第一个6位数字别名

            # 4. fetch_announcements - 搜索相关公告
            for alias in (concept.aliases or []):
                if alias and len(alias) == 6 and alias.isdigit():
                    ann_result = gw.fetch_announcements(alias, limit=5)
                    if ann_result.success and ann_result.parsed_items:
                        for itm in ann_result.parsed_items:
                            if not isinstance(itm, dict):
                                continue
                            title = itm.get("title", "")
                            date = itm.get("date", "")
                            pdf_url = itm.get("pdf_url", "")
                            company = itm.get("name", "")
                            if title:
                                doc = SourceDocument(
                                    document_id=new_id("doc_"),
                                    source_type="announcement",
                                    url=pdf_url,
                                    title=title,
                                    site_name="巨潮资讯",
                                    published_at=date,
                                    content_text=f"{title} ({company}, {date})",
                                    credibility_hint="high",
                                )
                                source_docs.append(doc)
                    break

        except Exception as exc:
            logger.warning("Gateway 来源采集失败: %s", exc)

        return source_docs, early_signals

    def _collect_sources(
        self, concept: ConceptSignal,
    ) -> Tuple[List[SourceDocument], List[EarlySignal]]:
        """为单个概念收集来源文档和早期信号.

        优先从 ChinaStockDataGateway 采集，再根据配置启用搜索、
        RSS、变更监控、爬虫等采集方式。mock_web 模式下返回空列表。

        Args:
            concept: 概念信号实例。

        Returns:
            (source_docs, early_signals) 二元组。
        """
        if self._mock_web:
            return [], []

        source_docs: List[SourceDocument] = []
        early_signals: List[EarlySignal] = []

        # 优先从 Gateway 采集
        gw_docs, gw_signals = self._collect_gateway_sources(concept)
        source_docs.extend(gw_docs)
        early_signals.extend(gw_signals)

        # 搜索发现
        if self._enable_search:
            try:
                search_docs = self._step_search_discovery([concept])
                source_docs.extend(search_docs)
            except Exception as exc:
                logger.debug("概念 '%s' 搜索失败: %s", concept.name, exc)

        # RSS 采集
        if self._enable_rss:
            try:
                if self._rss_collector is None:
                    self._rss_collector = RSSCollector()
                rss_sources = self._registry.filter_by_type("rss")
                feed_urls = list(dict.fromkeys(
                    s.base_url for s in rss_sources if s.base_url
                ))
                if feed_urls:
                    rss_docs = self._rss_collector.collect_multiple(feed_urls)
                    source_docs.extend(rss_docs)
            except Exception as exc:
                logger.debug("概念 '%s' RSS 采集失败: %s", concept.name, exc)

        # 变更监控
        if self._enable_change_monitor:
            try:
                if self._change_monitor is None:
                    self._change_monitor = ChangeMonitor()
                change_sources = [
                    e for e in self._registry.get_enabled_sources()
                    if e.crawl_method == "change_detection"
                ]
                for entry in change_sources:
                    if entry.base_url:
                        docs = self._change_monitor.import_changes(entry.base_url)
                        source_docs.extend(docs)
            except Exception as exc:
                logger.debug("概念 '%s' 变更监控失败: %s", concept.name, exc)

        # 聚焦爬虫
        if self._enable_crawler and source_docs:
            try:
                crawl_docs = self._step_focused_crawl(source_docs)
                source_docs.extend(crawl_docs)
            except Exception as exc:
                logger.debug("概念 '%s' 爬虫失败: %s", concept.name, exc)

        # 从来源文档中提取早期信号（简单规则：搜索结果视为早期信号）
        for doc in source_docs:
            if doc.source_type in ("recruitment", "expo", "tender", "certification"):
                early_signals.append(EarlySignal(
                    signal_id=new_id("es_"),
                    concept_id=concept.concept_id,
                    source_document_id=doc.document_id,
                    signal_type=doc.source_type,
                    claim=doc.title or "",
                    evidence_text=(doc.content_text or "")[:200],
                    confidence="low",
                    evidence_level="EARLY_SIGNAL",
                ))

        return source_docs, early_signals

    def _process_concept(
        self,
        concept: ConceptSignal,
        anchors: List[DemandAnchor],
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> Tuple[DynamicSupplyChain, List[BottleneckHypothesis], List[CompanyCandidate]]:
        """为单个概念执行供应链构建、瓶颈挖掘和公司映射.

        Args:
            concept: 概念信号实例。
            anchors: 需求锚点列表。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。

        Returns:
            (chain, bottlenecks, candidates) 三元组。
            chain 可能为 None（构建失败时）。
        """
        chain: Optional[DynamicSupplyChain] = None
        bottlenecks: List[BottleneckHypothesis] = []
        candidates: List[CompanyCandidate] = []

        # 使用第一个锚点构建供应链
        anchor = anchors[0] if anchors else DemandAnchor(
            anchor_id=new_id("da_"),
            concept_id=concept.concept_id,
            name=f"{concept.name}-unknown",
            anchor_type="unknown",
            description="自动生成的默认锚点",
            confidence="low",
        )

        # 供应链构建
        try:
            chain = self._chain_constructor.construct_chain(
                concept, anchor, source_docs, early_signals,
            )
        except Exception as exc:
            logger.error(
                "概念 '%s' 供应链构建失败: %s", concept.name, exc, exc_info=True,
            )
            return chain or DynamicSupplyChain(), bottlenecks, candidates

        if not chain or not chain.nodes:
            logger.warning("概念 '%s' 供应链为空", concept.name)
            return chain or DynamicSupplyChain(), bottlenecks, candidates

        # 瓶颈挖掘
        try:
            bottlenecks = self._bottleneck_miner.mine_bottlenecks(
                chain, source_docs, early_signals,
            )
        except Exception as exc:
            logger.error(
                "概念 '%s' 瓶颈挖掘失败: %s", concept.name, exc, exc_info=True,
            )

        # 公司映射
        try:
            candidates = self._company_mapper.map_companies(
                chain, bottlenecks, source_docs, early_signals, concept,
            )
        except Exception as exc:
            logger.error(
                "概念 '%s' 公司映射失败: %s", concept.name, exc, exc_info=True,
            )

        return chain, bottlenecks, candidates

    # ------------------------------------------------------------------
    # 手动信号加载
    # ------------------------------------------------------------------

    def _load_manual_signals(self) -> Tuple[List[SourceDocument], List[EarlySignal]]:
        """从手动信号文件加载来源文档和早期信号.

        支持两种来源：
        1. 配置指定的手动信号文件（JSON 列表或含 sources/signals 键的对象）
        2. data/input/ 目录下的 perilla_real_signals_*.json 文件（自动扫描）

        Returns:
            (来源文档列表, 早期信号列表) 二元组。
        """
        docs: List[SourceDocument] = []
        early_signals: List[EarlySignal] = []

        # 1. 加载配置指定的手动信号文件
        if self._manual_signal_file:
            if not os.path.isfile(self._manual_signal_file):
                logger.warning("手动信号文件不存在: %s", self._manual_signal_file)
            else:
                try:
                    with open(self._manual_signal_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except json.JSONDecodeError as exc:
                    logger.warning("手动信号文件 JSON 解析失败: %s", exc)
                    data = None
                except Exception as exc:
                    logger.warning("手动信号文件读取失败: %s", exc)
                    data = None

                if data is not None:
                    # 统一为列表
                    if isinstance(data, dict):
                        items = data.get("sources", data.get("signals", []))
                    elif isinstance(data, list):
                        items = data
                    else:
                        logger.warning("手动信号文件格式错误: 期望列表或字典")
                        items = []
                    if not isinstance(items, (list, tuple)):
                        items = []

                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        try:
                            doc = SourceDocument(
                                document_id=new_id("ms_"),
                                source_type=_safe_text(item.get("source_type"), "manual") or "manual",
                                url=_safe_text(item.get("url")),
                                title=_safe_text(item.get("title", item.get("name"))),
                                site_name=_safe_text(item.get("site_name")),
                                published_at=_safe_text(item.get("published_at")) or None,
                                fetched_at=now_iso(),
                                content_text=_safe_text(item.get("content", item.get("text"))),
                                credibility_hint=_safe_text(item.get("credibility_hint"), "medium") or "medium",
                            )
                            docs.append(doc)
                        except Exception as exc:
                            logger.debug("手动信号条目解析失败: %s", exc)

                    logger.info("手动信号加载完成: %d 条", len(docs))

        # 2. 自动加载 data/input/perilla_real_signals_*.json
        input_dir = Path("data/input")
        if input_dir.exists():
            for f in sorted(input_dir.glob("perilla_real_signals_*.json")):
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    signals = data.get("signals", []) if isinstance(data, dict) else []
                    if not isinstance(signals, (list, tuple)):
                        signals = []
                    for sig in signals:
                        if not isinstance(sig, dict):
                            continue
                        doc_id = new_id("doc_")
                        company_name = _safe_text(sig.get("company_name"))
                        symbol = _safe_text(sig.get("symbol"))
                        claim = _safe_text(sig.get("claim"))
                        evidence_text = _safe_text(sig.get("evidence_text", sig.get("claim")))
                        signal_type = _safe_text(sig.get("signal_type"), "concept_claim") or "concept_claim"

                        doc = SourceDocument(
                            document_id=doc_id,
                            source_type=_safe_text(sig.get("source_type"), "manual") or "manual",
                            url=_safe_text(sig.get("source_url")),
                            title=claim[:80] if claim else "",
                            site_name=_safe_text(sig.get("site_name")),
                            published_at=_safe_text(sig.get("date")) or None,
                            content_text=evidence_text,
                            credibility_hint=_safe_text(sig.get("credibility_hint"), "medium") or "medium",
                        )
                        docs.append(doc)

                        # 从手动信号生成 EarlySignal（包含公司名和代码）
                        if company_name:
                            # 将 symbol 附加到公司名后面便于 _resolve_company_code 识别
                            company_with_code = f"{company_name}({symbol})" if symbol else company_name
                            early_signals.append(EarlySignal(
                                signal_id=new_id("ms_"),
                                concept_id="",
                                source_document_id=doc_id,
                                signal_type=signal_type,
                                mentioned_companies=[company_with_code],
                                anchor_companies=[company_with_code] if signal_type == "supplier" else [],
                                claim=claim,
                                evidence_text=evidence_text[:200],
                                confidence=_safe_text(sig.get("confidence"), "medium") or "medium",
                                evidence_level="PROBABLE_SUPPLIER" if signal_type == "supplier" else "CONCEPT_CLAIM_ONLY",
                            ))
                    logger.info("加载手动信号文件: %s (%d 条)", f.name, len(signals))
                except Exception as exc:
                    logger.warning("加载手动信号文件 %s 失败: %s", f, exc)

        return docs, early_signals

    # ------------------------------------------------------------------
    # 免责声明
    # ------------------------------------------------------------------

    def _run_perilla_leaf_screener(
        self,
        report: PerillaDiscoveryReport,
        concept_signals: List[ConceptSignal],
        demand_anchors: List[DemandAnchor],
        chains: List[DynamicSupplyChain],
        bottlenecks: List[BottleneckHypothesis],
        candidates: List[CompanyCandidate],
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> None:
        """执行紫苏叶选股法六步工作流并输出报告.

        使用 LLM 驱动的 PerillaLeafScreener，支持两种模式：
        1. 有管道链数据时：run_from_pipeline（管道数据 + LLM 增强）
        2. 无链数据时：run（纯 LLM 独立模式）

        Args:
            report: 已生成的基础发现报告。
            concept_signals: 概念信号列表。
            demand_anchors: 需求锚点列表。
            chains: 动态供应链列表。
            bottlenecks: 瓶颈假设列表。
            candidates: 公司候选列表。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。
        """
        import json as _json

        screener = PerillaLeafScreener()
        output_dir = Path(self._output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        for concept in concept_signals:
            try:
                filename = _safe_filename_component(concept.name)
                concept_chains = [c for c in chains if c.concept_id == concept.concept_id]
                concept_anchors = [a for a in demand_anchors if a.concept_id == concept.concept_id]
                concept_bottlenecks = [b for b in bottlenecks if b.concept_id == concept.concept_id]
                concept_candidates = [c for c in candidates if c.concept_id == concept.concept_id]

                if concept_chains:
                    # 管道模式：利用已有链数据 + LLM 增强
                    # 合并所有链的节点（去重）
                    primary_chain = concept_chains[0]
                    leaf_report = screener.run_from_pipeline(
                        concept=concept,
                        anchors=concept_anchors,
                        chain=primary_chain,
                        bottlenecks=concept_bottlenecks,
                        company_candidates=concept_candidates,
                        source_docs=source_docs,
                        early_signals=early_signals,
                    )
                else:
                    # 独立模式：纯 LLM 驱动
                    leaf_report = screener.run(topic=concept.name)

                # 输出报告
                md_content = render_perilla_leaf_report_md(leaf_report)
                md_path = output_dir / f"perilla_leaf_{filename}_latest.md"
                with md_path.open("w", encoding="utf-8") as f:
                    f.write(md_content)

                # 输出 JSON
                json_path = output_dir / f"perilla_leaf_{filename}_latest.json"
                with json_path.open("w", encoding="utf-8") as f:
                    _json.dump(
                        leaf_report.__dict__,
                        f,
                        default=str,
                        ensure_ascii=False,
                        indent=2,
                    )

                in_pool = sum(1 for c in leaf_report.candidates if c.in_pool)
                logger.info(
                    "紫苏叶报告: %s | LLM=%s | 搜索=%s | 入池 %d | 淘汰 %d | Top: %s",
                    concept.name,
                    leaf_report.llm_used,
                    leaf_report.web_search_used,
                    in_pool,
                    len(leaf_report.eliminated),
                    [p.company_name for p in leaf_report.top_picks],
                )
            except Exception as exc:
                logger.error("紫苏叶选股法 %s 失败: %s", concept.name, exc, exc_info=True)

    def _ingest_signals_to_sag(
        self,
        concept_signals: List[ConceptSignal],
        bottlenecks: List[BottleneckHypothesis],
        early_signals: List[EarlySignal],
        source_docs: List[SourceDocument],
    ) -> dict[str, int]:
        """将 Perilla 发现的信号写入 SAG Event Graph.

        Args:
            concept_signals: 概念信号列表。
            bottlenecks: 瓶颈假设列表。
            early_signals: 早期信号列表。
            source_docs: 来源文档列表（用于关联来源负载）。

        Returns:
            摄入统计。
        """
        if self._bridge is None:
            return {"concept_signals": 0, "bottlenecks": 0, "early_signals": 0}

        valid_source_docs = [
            doc for doc in source_docs if isinstance(doc, SourceDocument)
        ]
        doc_by_id = {doc.document_id: doc for doc in valid_source_docs}
        stats = {"concept_signals": 0, "bottlenecks": 0, "early_signals": 0}

        for signal in concept_signals:
            if not isinstance(signal, ConceptSignal):
                continue
            source_doc = None
            if signal.raw_evidence_ids:
                doc_id = signal.raw_evidence_ids[0]
                source_doc = doc_by_id.get(doc_id)
                if source_doc is None:
                    source_doc = {"content_text": signal.notes or ""}
            claim = self._bridge.ingest_concept_signal(signal, source_doc)
            if claim is not None:
                stats["concept_signals"] += 1

        for hypothesis in bottlenecks:
            if not isinstance(hypothesis, BottleneckHypothesis):
                continue
            claim = self._bridge.ingest_bottleneck_hypothesis(hypothesis)
            if claim is not None:
                stats["bottlenecks"] += 1

        concept_name_by_id = {c.concept_id: c.name for c in concept_signals}
        for signal in early_signals:
            if not isinstance(signal, EarlySignal):
                continue
            concept_name = concept_name_by_id.get(signal.concept_id, "")
            claim = self._bridge.ingest_early_signal(signal, concept_name)
            if claim is not None:
                stats["early_signals"] += 1

        logger.info(
            "SAG Event Graph 摄入统计: concept_signals=%d, bottlenecks=%d, early_signals=%d",
            stats["concept_signals"],
            stats["bottlenecks"],
            stats["early_signals"],
        )
        return stats

    @staticmethod
    def _build_disclaimer() -> str:
        """构建标准免责声明文本.

        Returns:
            免责声明字符串。
        """
        return (
            "本报告由 Perilla Discovery Engine 自动生成，"
            "仅供研究参考，不构成任何投资建议。"
            "数据来源于公开信息，可能存在遗漏或错误，"
            "请以官方披露为准。"
            "报告中的供应链关系、瓶颈判断和公司映射均为算法推理结果，"
            "未经人工验证，不作为投资决策依据。"
            "紫苏概率仅表示供应链环节成为关键瓶颈的可能性，"
            "不代表公司投资价值。"
        )

    # ------------------------------------------------------------------
    # 来源统计
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_source_stats(
        source_docs: List[SourceDocument],
    ) -> Dict[str, int]:
        """计算来源文档统计信息.

        Args:
            source_docs: 来源文档列表。

        Returns:
            按来源类型统计的文档数量字典。
        """
        if not isinstance(source_docs, (list, tuple)):
            return {"total": 0}
        stats: Dict[str, int] = {}
        for doc in source_docs:
            if not isinstance(doc, SourceDocument):
                continue
            st = doc.source_type or "unknown"
            stats[st] = stats.get(st, 0) + 1
        stats["total"] = sum(stats.values())
        return stats
