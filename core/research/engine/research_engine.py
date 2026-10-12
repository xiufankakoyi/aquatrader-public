"""Research Engine — 统一入口，编排 providers → ingestion → events → extraction → evidence → graph → validation。

同时提供 REST API 所需的查询方法 (get_events / get_evidence / get_graph / etc.)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.events.event_bus import EventBus
from core.events.event_router import EventRouter
from core.research.evidence.evidence_center import Evidence, EvidenceCenter
from core.llm_processing.small_batch_processor import SmallBatchProcessor
from config.config import Config
from config.setting import Setting
from core.research.alpha import ActiveLearningPlanner, LearningPlanItem
from core.research.engine.fast_validation_loop import FastValidationLoop
from core.research.engine.models import EntityRegistry, EvidenceGraph, MoatDetector
from core.research.engine.single_stock_research import SingleStockResearch
from core.research.opinion import OpinionConfidenceSynthesizer
from core.research.perilla.discovery.search_discovery import SearchDiscovery
from core.research.review import FastMarketPriceProvider, ReviewEngine
from core.research_graph.scenario_simulator import ScenarioSimulator
from core.research_graph.valuation_bridge import ValuationBridge
from data_svc.ingestion.multisource_ingestion import MultiSourceIngestion
from data_svc.providers.provider_registry import ProviderRegistry
from data_svc.providers.source_router import DataSourceRouter

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ResearchEngine:
    """Research Data Engine 统一入口。

    编排完整的数据采集、事件处理、证据收集、图谱构建和验证流程。

    流程:
    providers → ingestion → events → extraction → evidence → graph → validation
    """

    def __init__(self) -> None:
        """初始化 Research Engine 及所有子组件。"""
        # 数据层
        self.registry = ProviderRegistry()
        self.event_bus = EventBus()
        self.event_router = EventRouter()
        self.source_router = DataSourceRouter(self.registry)
        self.ingestion = MultiSourceIngestion(
            registry=self.registry,
            event_bus=self.event_bus,
            source_router=self.source_router,
        )

        # 处理层
        self.processor = SmallBatchProcessor()

        # 证据层
        self.evidence_center = EvidenceCenter()

        # 知识图谱层
        self.entity_registry = EntityRegistry()
        self.evidence_graph = EvidenceGraph(self.entity_registry)
        self.moat_detector = MoatDetector(self.evidence_graph)

        # 验证层
        self.validation_loop = FastValidationLoop(
            evidence_center=self.evidence_center,
            entity_registry=self.entity_registry,
            evidence_graph=self.evidence_graph,
            event_bus=self.event_bus,
        )

        # 差距 5/7/8:主动学习、估值桥接、研究复盘
        self.active_learning_planner = ActiveLearningPlanner()
        self.valuation_bridge = ValuationBridge()
        self.review_engine = ReviewEngine(
            price_provider=FastMarketPriceProvider(db_path=Config.LANCEDB_PATH)
        )

        # 单股研报
        self.single_stock_research = SingleStockResearch(
            provider_registry=self.registry,
            evidence_center=self.evidence_center,
            entity_registry=self.entity_registry,
            evidence_graph=self.evidence_graph,
            small_batch_processor=self.processor,
            fast_validation_loop=self.validation_loop,
            event_bus=self.event_bus,
        )

        # 设置事件路由
        self._setup_routes()

        logger.info(
            "ResearchEngine 初始化完成: providers=%d, routes=%s",
            len(self.registry.get_all()),
            self.event_router.registered_types(),
        )

    def _setup_routes(self) -> None:
        """设置事件路由器默认路由。

        使用 lambda 包装避免循环依赖，每个路由处理器只做日志记录
        和简单转发，实际业务逻辑由各组件自行处理。
        """
        # raw_event -> 日志
        self.event_router.add_route(
            "raw_event",
            lambda env: logger.debug(
                "Route: raw_event -> %s", env.event_id
            ),
        )

        # normalized_event -> 日志
        self.event_router.add_route(
            "normalized_event",
            lambda env: logger.debug(
                "Route: normalized_event -> %s", env.event_id
            ),
        )

        # extraction_done -> 日志
        self.event_router.add_route(
            "extraction_done",
            lambda env: logger.debug(
                "Route: extraction_done -> %s", env.event_id
            ),
        )

        # evidence_created -> 日志
        self.event_router.add_route(
            "evidence_created",
            lambda env: logger.debug(
                "Route: evidence_created -> %s", env.event_id
            ),
        )

        # graph_updated -> 日志
        self.event_router.add_route(
            "graph_updated",
            lambda env: logger.debug(
                "Route: graph_updated -> %s", env.event_id
            ),
        )

        # candidate_created -> 日志
        self.event_router.add_route(
            "candidate_created",
            lambda env: logger.debug(
                "Route: candidate_created -> %s", env.event_id
            ),
        )

        # risk_alert -> 日志
        self.event_router.add_route(
            "risk_alert",
            lambda env: logger.warning(
                "Route: risk_alert -> %s payload=%s",
                env.event_id,
                str(env.payload)[:200],
            ),
        )

    def run_daily(
        self,
        trade_date: Optional[str] = None,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """运行完整的日常数据管线。

        流程:
        1. 从所有可用 Provider 采集数据
        2. 归一化、去重、发布事件
        3. 运行 SmallBatchProcessor 抽取
        4. 更新证据中心
        5. 构建实体注册表和证据图谱
        6. 运行 FastValidationLoop
        7. 输出报告

        Args:
            trade_date: 交易日期，默认今天
            symbols: 股票代码列表，默认全部

        Returns:
            运行结果摘要
        """
        start_time = datetime.now()
        if not trade_date:
            trade_date = start_time.strftime("%Y-%m-%d")

        logger.info("ResearchEngine daily run: trade_date=%s", trade_date)

        errors: List[str] = []

        # 1. 多源采集
        ingestion_result: Dict[str, Any] = {}
        try:
            ingestion_result = self.ingestion.collect_all(
                trade_date=trade_date, symbols=symbols
            )
            logger.info(
                "采集完成: raw=%d, normalized=%d",
                len(ingestion_result.get("raw_events", [])),
                len(ingestion_result.get("normalized_events", [])),
            )
        except Exception as exc:
            errors.append(f"采集失败: {exc}")
            logger.error("采集失败: %s", exc, exc_info=True)

        # 2. 运行 SmallBatchProcessor
        extraction_results: List[Any] = []
        try:
            normalized_events = ingestion_result.get("normalized_events", [])
            if normalized_events:
                extraction_results = self.processor.process_batch(normalized_events)
                logger.info("抽取完成: %d 条结果", len(extraction_results))
        except Exception as exc:
            errors.append(f"抽取失败: {exc}")
            logger.error("抽取失败: %s", exc, exc_info=True)

        # 3. 更新证据中心
        try:
            self._update_evidence_from_extraction(extraction_results, trade_date)
            # 扩容采集
            self.evidence_center.collect_from_industry_chain_if_available(trade_date)
            self.evidence_center.collect_from_finance_radar_if_available(trade_date)
            logger.info("证据更新完成: total=%d", len(self.evidence_center.evidences))
        except Exception as exc:
            errors.append(f"证据更新失败: {exc}")
            logger.error("证据更新失败: %s", exc, exc_info=True)

        # 4. 构建图谱
        try:
            self._build_graph_from_extraction(extraction_results)
            logger.info(
                "图谱构建完成: entities=%d, relations=%d",
                self.entity_registry.count(),
                self.evidence_graph.relation_count(),
            )
        except Exception as exc:
            errors.append(f"图谱构建失败: {exc}")
            logger.error("图谱构建失败: %s", exc, exc_info=True)

        # 5. 运行验证
        validation_results: List[Dict[str, Any]] = []
        try:
            if symbols:
                validation_results = self.validation_loop.validate_batch(
                    symbols, trade_date
                )
            else:
                # 从证据中心提取所有涉及的股票
                all_symbols = list({
                    sym
                    for ev in self.evidence_center.evidences
                    for sym in ev.symbols
                    if sym
                })
                if all_symbols:
                    validation_results = self.validation_loop.validate_batch(
                        all_symbols[:50], trade_date  # 最多验证 50 只
                    )
            logger.info("验证完成: %d 只股票", len(validation_results))
        except Exception as exc:
            errors.append(f"验证失败: {exc}")
            logger.error("验证失败: %s", exc, exc_info=True)

        # 6. 导出验证报告
        export_summary: Dict[str, Any] = {}
        try:
            export_summary = self.validation_loop.export_report(
                validation_results, trade_date
            )
        except Exception as exc:
            errors.append(f"导出报告失败: {exc}")
            logger.error("导出报告失败: %s", exc, exc_info=True)

        # 7. 生成研究观点(差距 1+2)
        research_opinions: List[Dict[str, Any]] = []
        try:
            research_opinions = self._build_research_opinions(trade_date)
        except Exception as exc:
            errors.append(f"生成研究观点失败: {exc}")
            logger.error("生成研究观点失败: %s", exc, exc_info=True)

        # 8. 情景压力测试(差距 3)
        scenario_reports: Dict[str, Any] = {}
        try:
            scenario_reports = self._run_scenario_analysis(research_opinions)
        except Exception as exc:
            errors.append(f"情景分析失败: {exc}")
            logger.error("情景分析失败: %s", exc, exc_info=True)

        # 9. 主动学习计划(差距 5)
        learning_plan: List[Dict[str, Any]] = []
        try:
            learning_plan = self._build_learning_plan(research_opinions, trade_date)
        except Exception as exc:
            errors.append(f"生成主动学习计划失败: {exc}")
            logger.error("生成主动学习计划失败: %s", exc, exc_info=True)

        # 9.5 执行主动学习计划（默认关闭，避免消耗搜索额度）
        learning_plan_execution: Dict[str, Any] = {}
        try:
            learning_plan_execution = self._execute_learning_plan(learning_plan, trade_date=trade_date)
        except Exception as exc:
            errors.append(f"执行主动学习计划失败: {exc}")
            logger.error("执行主动学习计划失败: %s", exc, exc_info=True)

        # 10. 估值桥接信号(差距 7)
        valuation_signals: Dict[str, Any] = {}
        try:
            valuation_signals = self._build_valuation_signals(research_opinions)
        except Exception as exc:
            errors.append(f"生成估值信号失败: {exc}")
            logger.error("生成估值信号失败: %s", exc, exc_info=True)

        # 11. 研究复盘记录(差距 8)
        review_summary: Dict[str, Any] = {}
        try:
            self._record_research_decisions(research_opinions, validation_results, trade_date)
            review_summary = self._run_review_evaluation()
        except Exception as exc:
            errors.append(f"记录复盘决策失败: {exc}")
            logger.error("记录复盘决策失败: %s", exc, exc_info=True)

        elapsed = (datetime.now() - start_time).total_seconds()
        result: Dict[str, Any] = {
            "success": len(errors) == 0,
            "trade_date": trade_date,
            "elapsed_seconds": round(elapsed, 1),
            "ingestion": {
                "raw_events": len(ingestion_result.get("raw_events", [])),
                "normalized_events": len(ingestion_result.get("normalized_events", [])),
                "documents": len(ingestion_result.get("documents", [])),
                "errors": ingestion_result.get("errors", []),
            },
            "extraction_count": len(extraction_results),
            "evidence_count": len(self.evidence_center.evidences),
            "entity_count": self.entity_registry.count(),
            "relation_count": self.evidence_graph.relation_count(),
            "validation_count": len(validation_results),
            "validation_export": export_summary,
            "research_opinions": research_opinions,
            "scenario_reports": scenario_reports,
            "learning_plan": learning_plan,
            "learning_plan_execution": learning_plan_execution,
            "valuation_signals": valuation_signals,
            "review_recorded": True,
            "review_summary": review_summary,
            "errors": errors,
        }

        logger.info(
            "ResearchEngine daily run 完成: elapsed=%.1fs, errors=%d",
            elapsed, len(errors),
        )

        return result

    def _build_research_opinions(self, trade_date: str) -> List[Dict[str, Any]]:
        """基于证据中心生成按主题聚合的研究观点。"""
        synthesizer = OpinionConfidenceSynthesizer()
        opinions: List[Dict[str, Any]] = []

        # 按 theme 聚合证据
        theme_evidence: Dict[str, List[Evidence]] = {}
        theme_counter: Dict[str, List[Evidence]] = {}
        for ev in self.evidence_center.evidences:
            if ev.is_stale:
                continue
            for theme in ev.themes or []:
                theme_evidence.setdefault(theme, []).append(ev)
            if ev.evidence_type == "risk" or ev.source_type == "risk":
                for theme in ev.themes or []:
                    theme_counter.setdefault(theme, []).append(ev)

        for theme, support_evs in theme_evidence.items():
            counter_evs = theme_counter.get(theme, [])
            support_evs = [ev for ev in support_evs if ev not in counter_evs]
            if not support_evs:
                continue
            opinion = synthesizer.synthesize(
                thesis=f"{theme} 具备研究价值,值得持续跟踪",
                theme=theme,
                evidence_list=support_evs,
                counter_evidence_list=counter_evs,
                reference_date=trade_date,
            )
            opinions.append(opinion.to_dict())
        return opinions

    def _run_scenario_analysis(
        self, research_opinions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """对重点主题构造内存神经元图并跑情景压力测试。"""
        if not research_opinions:
            return {}

        neurons: Dict[str, Any] = {}
        synapses: Dict[str, Any] = {}

        class _FakeNeuron:
            def __init__(
                self, neuron_id: str, canonical_name: str, node_type: str, confidence: float
            ):
                self.neuron_id = neuron_id
                self.canonical_name = canonical_name
                self.node_type = node_type
                self.confidence = confidence
                self.is_tombstone = False

        class _FakeSynapse:
            def __init__(
                self,
                synapse_id: str,
                source_neuron_id: str,
                target_neuron_id: str,
                strength: float = 0.5,
            ):
                self.synapse_id = synapse_id
                self.source_neuron_id = source_neuron_id
                self.target_neuron_id = target_neuron_id
                self.target_kind = "EXISTING_NEURON"
                self.strength = strength
                self.direction = "DOWNSTREAM_APPLICATION"
                self.relation = "MAPS_TO"
                self.status = "OPEN"

        # 收集涉及的股票
        symbol_to_themes: Dict[str, List[str]] = {}
        for ev in self.evidence_center.evidences:
            for sym in ev.symbols or []:
                symbol_to_themes.setdefault(sym, []).extend(ev.themes or [])

        for opinion in research_opinions[:3]:
            theme = opinion.get("theme", "")
            if not theme:
                continue
            nid = f"theme_{theme}"
            neurons[nid] = _FakeNeuron(
                neuron_id=nid,
                canonical_name=theme,
                node_type="THEME",
                confidence=opinion.get("confidence_score", 0.5),
            )
            for sym, themes in symbol_to_themes.items():
                if theme in themes:
                    sid = f"stock_{sym}"
                    if sid not in neurons:
                        neurons[sid] = _FakeNeuron(
                            neuron_id=sid,
                            canonical_name=sym,
                            node_type="STOCK",
                            confidence=0.5,
                        )
                    syn_id = f"syn_{nid}_{sid}"
                    synapses[syn_id] = _FakeSynapse(
                        synapse_id=syn_id,
                        source_neuron_id=nid,
                        target_neuron_id=sid,
                        strength=0.6,
                    )

        simulator = ScenarioSimulator.from_neurons_and_synapses(neurons, synapses)
        reports: Dict[str, Any] = {}
        scenarios = [
            ("base_upside", 0.15, 2),
            ("demand_miss", -0.20, 2),
            ("supply_constraint", -0.15, 2),
        ]
        for opinion in research_opinions[:3]:
            theme = opinion.get("theme", "")
            target_id = f"theme_{theme}"
            if simulator._get_neuron(target_id) is None:
                continue
            reports[theme] = {}
            for label, shock, steps in scenarios:
                report = simulator.simulate_intervention(target_id, shock, steps)
                reports[theme][label] = report.to_dict()
        return reports

    def _build_learning_plan(
        self,
        research_opinions: List[Dict[str, Any]],
        trade_date: str,
    ) -> List[Dict[str, Any]]:
        """基于研究观点生成主动学习计划(差距 5)。

        对低置信度/证据不足的主题构造 EvidenceGap,计算信息价值并排序。

        主动学习默认关闭(``Config.ACTIVE_LEARNING_ENABLED=False``)。关闭时
        直接返回空计划，避免搜索摘要绕过 LLM Claim 抽取直接成为证据。
        """
        if not Config.ACTIVE_LEARNING_ENABLED:
            logger.debug("ACTIVE_LEARNING_ENABLED=False，跳过主动学习计划生成")
            return []

        gaps: List[Dict[str, Any]] = []
        for opinion in research_opinions:
            theme = opinion.get("theme", "")
            confidence = opinion.get("confidence_score", 0.0)
            evidence_ids = opinion.get("evidence_ids", [])
            if not theme:
                continue
            if confidence >= 0.75 and len(evidence_ids) >= 3:
                continue
            gaps.append({
                "gap_id": f"research_{theme}_{trade_date}",
                "path": [theme],
                "missing_entity": theme,
                "missing_relation": "evidence",
                "missing_source_type": "研报",
                "priority": max(0.3, 0.75 - confidence),
            })

        if not gaps:
            return []

        plans = self.active_learning_planner.plan_from_gaps(
            gaps,
            root_theme="research_daily",
            current_confidence=0.5,
            path_alpha_score=0.6,
            reference_date=trade_date,
        )
        return [p.to_dict() for p in plans]

    def _execute_learning_plan(
        self,
        learning_plan: List[Dict[str, Any]],
        trade_date: str = "",
        top_n: int = 3,
    ) -> Dict[str, Any]:
        """执行主动学习计划的高价值搜索任务（差距 5 闭环）。

        执行前置条件:
        - ``Config.ACTIVE_LEARNING_ENABLED=True``：主动学习总开关，默认关闭。
          关闭时搜索摘要不会直接成为证据，必须经过 LLM Claim 抽取。
        - ``Config.LEARNING_PLAN_EXECUTION_ENABLED=True``：是否真正调用搜索 API，
          避免日常运行意外消耗搜索额度。

        执行结果保存到 learning_plan_execution/。

        Args:
            learning_plan: _build_learning_plan 生成的计划字典列表。
            top_n: 本次执行前 N 个计划。

        Returns:
            执行摘要，含 executed_count / result_count / skipped / results_path。
        """
        if not learning_plan:
            return {"executed_count": 0, "result_count": 0, "skipped": "empty_plan"}

        if not Config.ACTIVE_LEARNING_ENABLED:
            logger.info("ACTIVE_LEARNING_ENABLED=False，主动学习计划不执行")
            return {
                "executed_count": 0,
                "result_count": 0,
                "skipped": "active_learning_disabled",
            }

        if not Config.LEARNING_PLAN_EXECUTION_ENABLED:
            logger.info("LEARNING_PLAN_EXECUTION_ENABLED=False，学习计划仅生成不执行")
            return {
                "executed_count": 0,
                "result_count": 0,
                "skipped": "disabled_by_config",
            }

        # 还原为 LearningPlanItem 以便 mark_executed
        items = [LearningPlanItem(**p) for p in learning_plan[:top_n]]
        if not items:
            return {"executed_count": 0, "result_count": 0, "skipped": "no_valid_items"}

        queries: List[str] = []
        query_to_plan: Dict[str, LearningPlanItem] = {}
        for item in items:
            for q in item.suggested_queries:
                if q and q not in query_to_plan:
                    queries.append(q)
                    query_to_plan[q] = item

        if not queries:
            return {"executed_count": len(items), "result_count": 0, "skipped": "no_queries"}

        searcher = SearchDiscovery(
            searxng_base_url=Setting.SEARXNG_BASE_URL,
            engines=Setting.SEARXNG_ENGINES,
            max_results=5,
            enabled=True,
        )
        search_results = searcher.search_many(queries)

        # 收集结果并去重
        all_docs: List[Dict[str, Any]] = []
        seen_urls: set[str] = set()
        for q, docs in search_results.items():
            for doc in docs:
                url = getattr(doc, "url", "") or ""
                key = url.lower()
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                all_docs.append({
                    "query": q,
                    "plan_id": query_to_plan.get(q, LearningPlanItem(plan_id="")).plan_id,
                    "title": getattr(doc, "title", ""),
                    "url": url,
                    "content": str(getattr(doc, "content_text", "") or "")[:500],
                    "source_type": getattr(doc, "source_type", "unknown"),
                    "published_at": getattr(doc, "published_at", ""),
                })

        # 标记计划已执行（置信度变化留待下一轮运行时对比）
        for item in items:
            self.active_learning_planner.mark_executed(item, confidence_after=item.current_confidence)

        # 持久化执行结果
        run_dir = PROJECT_ROOT / "data" / "research_runs" / "learning_plan_execution"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_key = Path(str(trade_date or datetime.now().strftime("%Y%m%d"))).name
        result_path = run_dir / f"execution_{run_key}.json"
        result_path.write_text(
            json.dumps(
                {
                    "trade_date": trade_date,
                    "executed_at": datetime.now().isoformat(),
                    "plan_count": len(items),
                    "result_count": len(all_docs),
                    "results": all_docs,
                },
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )

        logger.info(
            "主动学习计划执行完成: plans=%d, queries=%d, results=%d",
            len(items), len(queries), len(all_docs)
        )

        # 将搜索结果作为弱证据注入 evidence_center，形成闭环
        ingested_count = 0
        evidence_center = getattr(self, "evidence_center", None)
        if evidence_center is not None:
            ingested_count = self._ingest_learning_results_as_evidence(all_docs, trade_date)
        else:
            logger.warning("evidence_center 未初始化，跳过主动学习结果证据注入")

        return {
            "executed_count": len(items),
            "result_count": len(all_docs),
            "ingested_as_evidence": ingested_count,
            "queries": queries,
            "results_path": str(result_path),
        }

    def _ingest_learning_results_as_evidence(
        self,
        search_docs: List[Dict[str, Any]],
        trade_date: str,
    ) -> int:
        """将主动学习搜索结果转化为弱证据注入 evidence_center。

        搜索结果置信度标为 weak，source_type 标记为 active_learning，
        以便后续 LLM 抽取时进行进一步验证，避免自动搜索的噪音直接
        影响高置信度判断。

        Args:
            search_docs: _execute_learning_plan 中收集的搜索文档列表。
            trade_date: 交易日期。

        Returns:
            成功注入的证据条数。
        """
        from core.research.evidence.evidence_center import Evidence

        ingested = 0
        existing_urls = {
            ev.source_url for ev in self.evidence_center.evidences if ev.source_url
        }
        existing_hashes = {
            ev.raw_hash for ev in self.evidence_center.evidences if ev.raw_hash
        }

        for doc in search_docs:
            url = doc.get("url", "")
            title = doc.get("title", "")
            content = (doc.get("content", "") or "")[:500]
            query = doc.get("query", "")
            source_type = doc.get("source_type", "web_search")
            plan_id = doc.get("plan_id", "")

            if not content or len(content) < 20:
                continue

            import hashlib
            raw_hash = hashlib.sha1(
                (url or title + content[:100]).encode("utf-8")
            ).hexdigest()[:16]

            if url and url in existing_urls:
                continue
            if raw_hash in existing_hashes:
                continue

            theme_name = query.replace("  ", " ").strip()
            evidence = Evidence(
                evidence_id=self.evidence_center._new_id(),
                trade_date=trade_date,
                entity_type="theme",
                entity_id=f"al_{plan_id or theme_name}",
                entity_name=theme_name,
                source_type="active_learning",
                source_name=f"web_search:{source_type}",
                title=title or f"主动学习搜索: {query}",
                summary=content[:200],
                symbols=[],
                themes=[theme_name] if theme_name else [],
                evidence_strength="weak",
                data_date=trade_date,
                is_stale=False,
                source_url=url,
                evidence_text=content,
                evidence_type="catalyst",
                extraction_method="active_learning_search",
                confidence="low",
                raw_hash=raw_hash,
                created_at=datetime.now().isoformat(),
            )
            self.evidence_center.add(evidence)
            existing_urls.add(url)
            existing_hashes.add(raw_hash)
            ingested += 1

        if ingested > 0:
            logger.info("主动学习搜索结果已注入 evidence_center: %d 条", ingested)
        return ingested

    def _build_valuation_signals(
        self,
        research_opinions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """基于研究观点生成估值桥接信号(差距 7)。"""
        class _FakeNeuron:
            def __init__(self, neuron_id: str, canonical_name: str, node_type: str, confidence: float):
                self.neuron_id = neuron_id
                self.canonical_name = canonical_name
                self.node_type = node_type
                self.confidence = confidence

        signals: Dict[str, Any] = {}
        for opinion in research_opinions[:3]:
            theme = opinion.get("theme", "")
            if not theme:
                continue

            # 收集与主题直接相关的真实证据，不再传空列表
            evidence_center = getattr(self, "evidence_center", None)
            if evidence_center is not None:
                theme_evidences = [
                    ev for ev in evidence_center.evidences
                    if not ev.is_stale and theme in (ev.themes or [])
                ]
            else:
                theme_evidences = []

            fake_neuron = _FakeNeuron(
                neuron_id=f"theme_{theme}",
                canonical_name=theme,
                node_type="THEME",
                confidence=opinion.get("confidence_score", 0.5),
            )
            report = self.valuation_bridge.analyze(fake_neuron, theme_evidences)
            signals[theme] = report.to_dict()
        return signals

    def _record_research_decisions(
        self,
        research_opinions: List[Dict[str, Any]],
        validation_results: List[Dict[str, Any]],
        trade_date: str,
    ) -> None:
        """记录研究判断到复盘日志(差距 8)。"""
        for opinion in research_opinions[:5]:
            confidence_score = opinion.get("confidence_score", 0.0)
            decision = "WATCH"
            if confidence_score >= 0.75:
                decision = "WATCH_CANDIDATE"
            elif confidence_score < 0.35:
                decision = "BLOCK"

            self.review_engine.record_judgment(
                agent="ResearchEngine",
                target_type="theme",
                target_key=opinion.get("theme", ""),
                decision=decision,
                confidence=opinion.get("confidence_level", "medium"),
                reasoning=opinion.get("thesis", ""),
                trade_date=trade_date,
                evidence_summary=", ".join(opinion.get("evidence_ids", [])[:5]),
            )

        for result in validation_results[:10]:
            symbol = result.get("symbol", "")
            if not symbol:
                continue
            self.review_engine.record_judgment(
                agent="FastValidationLoop",
                target_type="stock",
                target_key=symbol,
                decision=result.get("decision", "WATCH"),
                confidence="medium",
                reasoning=result.get("reason", ""),
                trade_date=trade_date,
            )

    def _run_review_evaluation(self) -> Dict[str, Any]:
        """自动评估已到期的判断记录，生成复盘报告。

        遍历 review_log 中所有 judgment，对 horizon_days 已过的记录计算
        实际收益，生成 PerformanceRecord 并保存复盘报告。

        Returns:
            复盘摘要字典，含 evaluated_count / weights / lessons / report_path。
        """
        all_judgments = self.review_engine.review_log.all_judgments()
        if not all_judgments:
            return {"evaluated_count": 0, "skipped": "no_judgments"}

        today_str = datetime.now().strftime("%Y-%m-%d")
        evaluable = [
            j for j in all_judgments
            if j.trade_date and self._is_evaluation_due(j.trade_date, today_str)
        ]
        if not evaluable:
            return {"evaluated_count": 0, "skipped": "none_due", "total_judgments": len(all_judgments)}

        performance_records = self.review_engine.evaluate_performance(evaluable)
        weights = self.review_engine.adjust_agent_weights(performance_records, evaluable)
        lessons = self.review_engine.generate_lessons(performance_records, evaluable)
        report_path = self.review_engine.save_review_report(performance_records, weights, lessons)

        hit_count = sum(1 for p in performance_records if p.hit)
        logger.info(
            "复盘评估完成: evaluated=%d, hit=%d, weights=%s",
            len(performance_records), hit_count, weights,
        )

        return {
            "evaluated_count": len(performance_records),
            "hit_count": hit_count,
            "agent_weights": weights,
            "lessons": lessons,
            "report_path": str(report_path),
        }

    @staticmethod
    def _is_evaluation_due(trade_date: str, today: str, horizon_days: int = 20) -> bool:
        """判断一个 trade_date 的判断是否已到复盘评估日。

        使用自然日近似：20 个交易日 ≈ 28 个自然日。
        """
        try:
            from datetime import datetime as _dt
            td = _dt.strptime(trade_date, "%Y-%m-%d")
            td_today = _dt.strptime(today, "%Y-%m-%d")
            return (td_today - td).days >= horizon_days * 7 // 5
        except Exception:
            return False

    def run_single_stock(self, symbol: str, days: int = 365) -> Dict[str, Any]:
        """运行单只股票的深度研报。

        Args:
            symbol: 股票代码
            days: 回看天数

        Returns:
            研报结果
        """
        logger.info("ResearchEngine single stock: %s, days=%d", symbol, days)

        try:
            report = self.single_stock_research.research(symbol, days)
            export = self.single_stock_research.export_report(symbol, report)

            return {
                "success": True,
                "symbol": symbol,
                "conclusion_level": report.get("conclusion_level", "UNKNOWN"),
                "validation_score": report.get("validation_score", 0.0),
                "export": export,
            }
        except Exception as exc:
            logger.error("单股研报失败: %s, %s", symbol, exc, exc_info=True)
            return {
                "success": False,
                "symbol": symbol,
                "error": str(exc),
            }

    def get_status(self) -> Dict[str, Any]:
        """获取当前引擎状态，包含 provider 健康信息。

        Returns:
            引擎状态字典，包含 engine / providers / graph_stats 等字段。
        """
        # Provider 健康检查
        providers: Dict[str, Any] = {}
        try:
            health = self.registry.health_check_all()
            for name, status in health.items():
                providers[name] = {
                    "enabled": status.enabled,
                    "status": status.status,
                    "capabilities": status.capabilities,
                    "last_error": status.last_error,
                    "sample_event_count": status.sample_event_count,
                }
        except Exception as exc:
            providers = {"error": str(exc)}

        return {
            "engine": "research_engine_v1",
            "initialized_at": getattr(self, "_initialized_at", ""),
            "provider_count": len(providers) if isinstance(providers, dict) else 0,
            "providers": providers,
            "source_routes": self.source_router.status(check_health=False),
            "graph_stats": {
                "entity_count": self.entity_registry.count(),
                "relation_count": self.evidence_graph.relation_count(),
            },
            "evidence_count": len(self.evidence_center.evidences),
            "event_bus_topics": self.event_bus.topics(),
            "event_router_types": self.event_router.registered_types(),
            "status": "ok",
        }

    def query_source(
        self,
        capability: str,
        symbols: Optional[List[str]] = None,
        **params: Any,
    ) -> Dict[str, Any]:
        """Query one logical capability through the provider fallback chain."""
        result = self.source_router.query(
            capability=capability,
            symbols=symbols,
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            limit=params.get("limit", 20),
            keyword=params.get("keyword"),
            query=params.get("query"),
        )
        return result.to_dict()

    def get_source_status(self, check_health: bool = True) -> Dict[str, Any]:
        """Return capability routes and optional provider health information."""
        return self.source_router.status(check_health=check_health)

    # ------------------------------------------------------------------
    # REST API 查询方法
    # ------------------------------------------------------------------

    def get_events(
        self,
        topic: Optional[str] = None,
        date: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """获取近期事件。

        从所有启用的 provider 获取行情/K线事件。

        Args:
            topic: 可选事件主题过滤。
            date: 可选日期过滤 (YYYY-MM-DD)。
            limit: 最大返回数量。

        Returns:
            包含 events 列表和 metadata 的字典。
        """
        events: List[Dict[str, Any]] = []
        errors: List[str] = []

        for name, provider in self.registry.get_all().items():
            if not provider.enabled:
                continue
            try:
                raw_events = provider.fetch_quotes(symbols=["000001.SZ"])
                if not raw_events:
                    raw_events = provider.fetch_kline(
                        symbols=["000001.SZ"],
                        start_date=date or "2024-01-01",
                        end_date=date or datetime.now().strftime("%Y-%m-%d"),
                    )
                for evt in raw_events[:limit]:
                    events.append(asdict(evt))
            except Exception as e:
                errors.append(f"{name}: {e}")

        events = events[:limit]

        return {
            "events": events,
            "total": len(events),
            "topic": topic,
            "date": date,
            "errors": errors if errors else None,
        }

    def get_evidence(
        self,
        symbol: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """获取证据列表。

        Args:
            symbol: 可选股票代码过滤。
            source_type: 可选来源类型过滤。
            limit: 最大返回数量。

        Returns:
            包含 evidence 列表和 metadata 的字典。
        """
        entities = self.entity_registry.all_entities()

        if symbol:
            entities = [
                e for e in entities
                if symbol in e.symbols or symbol in e.entity_name
            ]

        if source_type:
            entities = [e for e in entities if e.entity_type == source_type]

        evidence_list = []
        for entity in entities[:limit]:
            chains = self.evidence_graph.get_evidence_chain(entity.entity_id)
            evidence_list.append({
                "entity": asdict(entity),
                "evidence_chains": chains,
            })

        return {
            "evidence": evidence_list,
            "total": len(evidence_list),
            "symbol": symbol,
            "source_type": source_type,
        }

    def get_graph(
        self,
        symbol: Optional[str] = None,
        entity_id: Optional[str] = None,
        depth: int = 1,
    ) -> Dict[str, Any]:
        """获取知识图谱数据。

        Args:
            symbol: 可选股票代码，作为图谱中心。
            entity_id: 可选实体 ID，作为图谱中心。
            depth: 遍历深度。

        Returns:
            包含 nodes / edges / metadata 的字典。
        """
        center_entity = None

        if entity_id:
            center_entity = self.entity_registry.get(entity_id)
        elif symbol:
            center_entity = self.entity_registry.get_by_symbol(symbol)

        if center_entity:
            neighbor_distances = self.evidence_graph.get_neighbors(
                center_entity.entity_id, max_depth=depth
            )
            node_ids = set(neighbor_distances.keys())
            nodes = []
            for nid in node_ids:
                entity = self.entity_registry.get(nid)
                if entity:
                    nodes.append(asdict(entity))

            edges = []
            seen_rel_ids: set = set()
            for nid in node_ids:
                for rel in self.evidence_graph.get_relations(nid, direction="both"):
                    if (
                        rel.relation_id not in seen_rel_ids
                        and rel.source_entity_id in node_ids
                        and rel.target_entity_id in node_ids
                    ):
                        edges.append(asdict(rel))
                        seen_rel_ids.add(rel.relation_id)

            return {
                "nodes": nodes,
                "edges": edges,
                "center_entity_id": center_entity.entity_id,
                "depth": depth,
                "node_count": len(nodes),
                "edge_count": len(edges),
            }

        # 无中心实体 — 返回全图摘要
        all_entities = self.entity_registry.all_entities()
        all_relations = self.evidence_graph.all_relations()

        return {
            "nodes": [asdict(e) for e in all_entities],
            "edges": [asdict(r) for r in all_relations],
            "center_entity_id": None,
            "depth": depth,
            "node_count": len(all_entities),
            "edge_count": len(all_relations),
        }

    def get_candidates(self) -> Dict[str, Any]:
        """获取已验证的候选标的（有护城河信号的股票）。

        Returns:
            包含 candidates 列表的字典。
        """
        stock_entities = self.entity_registry.list_by_type("stock")
        candidates = []

        for entity in stock_entities:
            moat_summary = self.moat_detector.get_stock_moat_summary(
                entity.symbols[0] if entity.symbols else ""
            )
            if moat_summary.get("has_moat"):
                candidates.append({
                    "entity": asdict(entity),
                    "moat_summary": moat_summary,
                })

        return {
            "candidates": candidates,
            "total": len(candidates),
        }

    def get_brief(self) -> Dict[str, Any]:
        """获取最新研究简报。

        Returns:
            包含图谱摘要、provider 摘要等的字典。
        """
        # 统计实体类型
        entity_type_counts: Dict[str, int] = {}
        for entity in self.entity_registry.all_entities():
            entity_type_counts[entity.entity_type] = (
                entity_type_counts.get(entity.entity_type, 0) + 1
            )

        # Provider 摘要
        provider_summary: Dict[str, Any] = {}
        try:
            available = self.registry.get_available_providers()
            all_providers = list(self.registry.get_all().keys())
            provider_summary = {
                "available": available,
                "unavailable": [p for p in all_providers if p not in available],
                "total": len(all_providers),
            }
        except Exception as e:
            provider_summary = {"error": str(e)}

        return {
            "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "graph_summary": {
                "entity_count": self.entity_registry.count(),
                "relation_count": self.evidence_graph.relation_count(),
                "entity_types": entity_type_counts,
            },
            "provider_summary": provider_summary,
            "evidence_count": len(self.evidence_center.evidences),
        }

    def get_single_stock(self, symbol: str) -> Dict[str, Any]:
        """获取单只股票研究数据（只读查询，不触发流水线）。

        Args:
            symbol: 股票代码 (e.g. "000001.SZ")。

        Returns:
            包含 entity / evidence_chains / moat_summary / provider_data 的字典。
        """
        result: Dict[str, Any] = {
            "symbol": symbol,
            "entity": None,
            "evidence_chains": [],
            "moat_summary": {},
            "provider_data": {},
            "errors": [],
        }

        # 从图谱获取实体
        entity = self.entity_registry.get_by_symbol(symbol)
        if entity:
            result["entity"] = asdict(entity)
            result["evidence_chains"] = self.evidence_graph.get_evidence_chain(
                entity.entity_id
            )
            result["moat_summary"] = self.moat_detector.get_stock_moat_summary(symbol)

        # 从 provider 获取实时数据
        for name, provider in self.registry.get_all().items():
            if not provider.enabled:
                continue
            try:
                raw_events = provider.fetch_quotes(symbols=[symbol])
                if raw_events:
                    result["provider_data"][name] = [asdict(e) for e in raw_events[:5]]
            except Exception as e:
                result["errors"].append(f"{name}: {e}")

        return result

    def run_pipeline(self) -> Dict[str, Any]:
        """运行完整研究流水线（包装 run_daily）。

        Returns:
            流水线运行结果。
        """
        return self.run_daily()

    def probe_sources(self) -> Dict[str, Any]:
        """探测所有数据源并返回可用性报告。

        Returns:
            包含 provider 健康状态和样本数据的字典。
        """
        logger.info("Probing all data sources...")

        health = self.registry.health_check_all()
        sample_results: Dict[str, Any] = {}

        for name, provider in self.registry.get_all().items():
            if not provider.enabled:
                sample_results[name] = {
                    "status": "disabled",
                    "sample_event_count": 0,
                }
                continue

            try:
                events = provider.fetch_quotes(symbols=["000001.SZ"])
                if not events:
                    events = provider.fetch_kline(
                        symbols=["000001.SZ"],
                        start_date="2024-01-01",
                        end_date="2024-12-31",
                    )
                sample_results[name] = {
                    "status": "ok",
                    "sample_event_count": len(events),
                    "sample_events": [asdict(e) for e in events[:3]],
                }
            except Exception as e:
                sample_results[name] = {
                    "status": "error",
                    "sample_event_count": 0,
                    "error": str(e),
                }

        available_count = sum(
            1 for h in health.values() if h.status in ("ok", "partial")
        )

        return {
            "probed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "available_count": available_count,
            "total_count": len(health),
            "health": {
                name: {
                    "enabled": h.enabled,
                    "status": h.status,
                    "capabilities": h.capabilities,
                    "last_error": h.last_error,
                }
                for name, h in health.items()
            },
            "samples": sample_results,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _update_evidence_from_extraction(
        self,
        extraction_results: List[Any],
        trade_date: str,
    ) -> None:
        """从抽取结果更新证据中心。

        Args:
            extraction_results: ExtractionResult 列表
            trade_date: 交易日期
        """
        from core.events.models import ExtractionResult

        for result in extraction_results:
            if not isinstance(result, ExtractionResult):
                continue

            # 从 evidence_items 生成证据
            for item in result.evidence_items:
                self.evidence_center.add(Evidence(
                    evidence_id=EvidenceCenter._new_id(),
                    trade_date=trade_date,
                    entity_type="stock",
                    entity_id=result.symbol,
                    entity_name=result.stock_name,
                    source_type=result.processor,
                    source_name=f"extraction_{result.doc_type}",
                    title=item.get("label", "抽取证据"),
                    summary=item.get("evidence_text", "")[:200],
                    symbols=[result.symbol] if result.symbol else [],
                    themes=result.themes,
                    evidence_strength=(
                        "strong" if item.get("confidence", 0) >= 0.7
                        else "medium" if item.get("confidence", 0) >= 0.4
                        else "weak"
                    ),
                    data_date=trade_date,
                    created_at=EvidenceCenter._now(),
                ))

    def _build_graph_from_extraction(self, extraction_results: List[Any]) -> None:
        """从抽取结果构建图谱。

        Args:
            extraction_results: ExtractionResult 列表
        """
        from core.events.models import ExtractionResult
        from core.research.engine.models import Entity, Relation

        for result in extraction_results:
            if not isinstance(result, ExtractionResult):
                continue
            if not result.symbol:
                continue

            # 确保股票实体存在
            stock_entity = self.entity_registry.get_by_symbol(result.symbol)
            if not stock_entity:
                stock_entity = Entity(
                    entity_type="stock",
                    entity_name=result.stock_name or result.symbol,
                    symbols=[result.symbol],
                )
                self.entity_registry.add(stock_entity)

            # 题材实体和关系
            for theme in result.themes:
                if not theme:
                    continue
                theme_entity = self.entity_registry.get_by_name(theme)
                if not theme_entity:
                    theme_entity = Entity(
                        entity_type="theme",
                        entity_name=theme,
                    )
                    self.entity_registry.add(theme_entity)

                # 抽取结果仅能判定股票与主题存在关联，无法确定产业链具体语义
                # (供应商/客户/上下游等)，故使用 RELATED_TO；
                # BELONGS_TO_THEME 仅保留给纯主题分类场景。
                self.evidence_graph.add_relation(Relation(
                    source_entity_id=stock_entity.entity_id,
                    target_entity_id=theme_entity.entity_id,
                    relation_type="RELATED_TO",
                    weight=0.5,
                    confidence="medium",
                ))

            # 护城河检测
            if result.moat_claims or result.risks:
                self.moat_detector.detect_from_extraction(
                    result.moat_claims,
                    result.risks,
                    symbol=result.symbol,
                    stock_name=result.stock_name,
                )
