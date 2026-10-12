"""超图神经元环境(任务第三节:环境不是大脑)。

职责:
- 持有 NeuronStateStore / EvidenceIndex / EventRouter / LifecycleManager
- 把 GraphStore 节点包装为 ResearchNeuron(cold_start)
- 把新 evidence 路由给相关神经元(push)
- 接收 GrowthPulse,按预算执行外部动作(consume_pulse)
- 控制资源预算
- 记录 mutation log
- 提供一致性快照给神经元

不负责:
- 不替神经元决定何时发出 pulse
- 不中央扫描生成 frontier task
- 不替神经元更新 belief
- 不绕过 evidence gate 写 ACTIVE
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.research.perilla.event.cross_theme_surprise_miner import CrossThemeSurpriseMiner
from core.research_graph.neuron.events import EvidenceIndex, EventRouter
from core.research_graph.neuron.lifecycle import LifecycleManager
from core.research_graph.neuron.state_store import NeuronStateStore

logger = logging.getLogger(__name__)


class SynapseStateReaderAdapter:
    """SynapseStateReader in-process adapter(Plan 6 修复二)。

    从 NeuronStateStore 查询 incident synapse,供 neuron.growth_cycle 使用。
    """

    def __init__(self, state_store: NeuronStateStore) -> None:
        self._store = state_store

    def get_synapse(self, synapse_id: str) -> Any:
        """按 synapse_id 查询突触。"""
        return self._store.get_synapse(synapse_id)


class EvidenceLedgerReaderAdapter:
    """EvidenceLedgerReader in-process adapter(Plan 6 修复二)。

    从 environment 维护的 evidence_pool / neuron_evidence_map / evidence_stance
    读取证据,按 stance 过滤。route_evidence 时存入 pool。
    """

    def __init__(
        self,
        evidence_pool: dict[str, Any],
        neuron_evidence_map: dict[str, list[str]],
        evidence_stance: dict[str, str],
    ) -> None:
        self._pool = evidence_pool
        self._map = neuron_evidence_map
        self._stance = evidence_stance

    def query_by_neuron(self, neuron_id: str, stance: str = "") -> list[Any]:
        """按 neuron_id 查询关联证据,可选 stance 过滤。

        Args:
            neuron_id: 神经元 ID。
            stance: 可选立场过滤(support/counter/stale/retracted),空返回全部。

        Returns:
            Evidence 列表。
        """
        ids = self._map.get(neuron_id, [])
        evs = [self._pool[eid] for eid in ids if eid in self._pool]
        if stance:
            evs = [ev for ev in evs if self._stance.get(ev.evidence_id, "support") == stance]
        return evs


class MutationLogWriterAdapter:
    """MutationLogWriter in-process adapter(Plan 6 修复二)。

    把 MutationEntry 直接写入 state_store.mutations 表(append-only)。
    """

    def __init__(self, state_store: NeuronStateStore) -> None:
        self._store = state_store

    def append_cycle_mutation(self, mutation: Any) -> None:
        """追加 mutation 到 state_store.mutations 表。"""
        self._store.save_mutation(mutation)


@dataclass
class EnvironmentBudgetConfig:
    """环境层预算配置(任务第十七节)。"""
    max_active_neurons_per_tick: int = 50
    max_pulses_per_neuron: int = 3
    max_total_pulses_per_tick: int = 100
    max_pulse_age_hours: int = 24
    tick_interval_seconds: int = 60
    max_graph_depth: int = 1


class ResearchGraphEnvironment:
    """超图神经元环境。

    环境不是大脑:只负责投递/调用/收集/执行/持久化,不替神经元决策。
    """

    # consume_pulse 白名单(spec 6.4),非白名单 pulse_type 一律拒绝
    PULSE_WHITELIST: set[str] = {
        "REQUEST_GROWTH_MINING", "EXPAND_COMPANY_MAPPING",
        "CHECK_COUNTER_EVIDENCE", "MERGE_ALIAS", "SPLIT_TOO_COARSE",
        "PRUNE_DISPROVEN", "REQUEST_HUMAN_REVIEW",
        # 阶段 3 新增:扩展方向脉冲接入 consume_pulse,由 _handle_expand_direction 处理
        "EXPAND_UPSTREAM_MATERIAL", "EXPAND_DOWNSTREAM_APPLICATION",
        "EXPAND_SYNTHESIS_CHILDREN", "EXPAND_TARGET_MATERIAL", "EXPAND_EQUIPMENT",
        # Step 8:时序因果筛选脉冲
        "REQUEST_CAUSAL_SCREENING",
        # P0 Sprint:事件研究脉冲
        "REQUEST_EVENT_STUDY",
    }

    # pulse_type → (direction, relation, dimension, default_node_type) 映射
    # 用于 _handle_expand_direction 构造 TARGET_DRAFT synapse
    # direction 必须是 SynapseDirection 合法枚举值
    _EXPAND_DIRECTION_MAP: dict[str, tuple[str, str, str, str]] = {
        "EXPAND_UPSTREAM_MATERIAL": ("UPSTREAM_MATERIAL", "UPSTREAM_OF", "industry_flow", "MATERIAL"),
        "EXPAND_DOWNSTREAM_APPLICATION": ("DOWNSTREAM_APPLICATION", "DOWNSTREAM_OF", "industry_flow", "APPLICATION"),
        "EXPAND_SYNTHESIS_CHILDREN": ("SYNTHESIS_CHILD", "PART_OF", "synthesis_tree", "MATERIAL"),
        # TARGET_MATERIAL / UP_EQUIPMENT 无对应 SynapseDirection 枚举,复用 UP_COMPANY
        "EXPAND_TARGET_MATERIAL": ("UP_COMPANY", "MAPS_TO", "company_chain", "MATERIAL"),
        "EXPAND_EQUIPMENT": ("UP_COMPANY", "REQUIRES", "company_chain", "EQUIPMENT"),
    }

    def __init__(
        self,
        state_store: NeuronStateStore,
        budget_config: EnvironmentBudgetConfig | None = None,
        graph_store: Any = None,
        evidence_ledger: Any = None,
        entity_resolver: Any | None = None,
        retriever: Any = None,
        fingerprint_store_path: str | None = None,
        regime_detector: Any = None,
        event_study_service: Any = None,
    ) -> None:
        self.state_store = state_store
        self.budget_config = budget_config or EnvironmentBudgetConfig()
        self.graph_store = graph_store
        self.evidence_ledger = evidence_ledger
        # P0 Sprint:体制检测器 + 事件研究服务(可选)。
        # 注入时 _handle_event_study 可调用其 run_event_study + placebo_test,
        # event_loop_tick 末尾输出 regime_report;为 None 时两者均不触发。
        self.regime_detector = regime_detector
        self.event_study_service = event_study_service
        # Step 7:HybridRetriever 注入(可选)。提供时反证搜索先查历史反证文档,
        # 缺口分析 pulse 附带 hypothesis 字段驱动后续证据性检索。
        self.retriever = retriever
        self.evidence_index = EvidenceIndex()
        self.event_router = EventRouter()
        self.lifecycle_manager = LifecycleManager()
        self._active_neuron_ids: set[str] = set()
        self._tick_count: int = 0
        self._last_mutation_tick: int = 0
        # factory 模式:接受 GrowthMiningRequest 返回 GrowthMiningResult,
        # 真实注入 lambda req: GrowthMiningEngine(req).run(),mock 注入 MagicMock(return_value=...)
        self.growth_mining_engine_factory: Any = None
        # 阶段 2 新增:网络调用总开关。True 时 _handle_growth_mining / _handle_counter_evidence
        # 会把 use_system_search=True 注入 GrowthMiningRequest,触发 BaiduSearch +
        # ProviderRegistry.semantic_search 真实网络调用。False(默认)走本地 parquet。
        self.enable_system_search: bool = False
        # 阶段 3 新增:LLM 辅助 target_draft 生成开关。
        # True 时 _handle_expand_direction 会调 call_llm_with_budget 生成候选目标节点;
        # False 时退化为规则模板(基于 pulse_type 推断 node_type,proposed_name 留空)。
        self.llm_target_draft_enabled: bool = False
        # 阶段 5 新增:#4 自我质疑与反向验证开关。
        # True 时 _handle_counter_evidence 会先调 LLM 基于神经元 support evidence
        # 生成反证问题(如"主营业务是否真的包含电镀液?收入占比多少?"),
        # 再用问题驱动 growth_mining 搜索,产出的 counter evidence 回写神经元。
        # False 时退化为原行为(直接用 pulse.payload.topic 搜索)。
        self.llm_counter_question_enabled: bool = False
        # 介入点 1 新增:LLM 证据抽取增强开关。
        # True 时 _handle_growth_mining / _handle_counter_evidence 会把
        # allow_llm_assist=True 注入 GrowthMiningRequest,GrowthMiningEngine
        # 会调 llm_evidence_extractor 做 4 阶段抽取(替代纯正则 anchor window)。
        # False 时只走本地正则。
        self.llm_evidence_extraction_enabled: bool = False
        # LLM provider 列表(可选);None 时从 config 加载
        self.llm_providers: Any = None
        # Plan 6 修复二:evidence pool 供 EvidenceLedgerReaderAdapter 查询
        self._evidence_pool: dict[str, Any] = {}
        self._neuron_evidence_map: dict[str, list[str]] = {}
        self._evidence_stance: dict[str, str] = {}
        # 假设-证据覆盖矩阵分析器
        from core.research_graph.gap_analyzer import GapAnalyzer

        self._gap_analyzer = GapAnalyzer()
        # 边际变化跟踪器
        from core.research_graph.delta_tracker import DeltaTracker

        self._delta_tracker = DeltaTracker()
        from core.research_graph.neuron.tick_scheduler import TickScheduler

        self._tick_scheduler = TickScheduler()
        # 阶段 5:跨主题/跨行业动态事件传播
        from core.research_graph.dynamic_event_propagation import (
            DynamicEventPropagation,
        )

        self._cross_theme_propagation = DynamicEventPropagation()
        # 差距 6:跨主题意外发现,基于实体词表和桥接公司挖掘未研究主题
        self._cross_theme_surprise_miner = CrossThemeSurpriseMiner(
            entity_resolver=entity_resolver,
            max_candidates=10,
        )
        # Step 5:证据新颖度门控(跨运行去重)。lazy 初始化,首次 evaluate 时创建。
        # 默认不接持久化指纹存储,通过 attach_fingerprint_store 注入。
        self._evidence_novelty_gate: Any = None
        self._novelty_skipped_count: int = 0
        # P1d:若提供了 fingerprint_store_path,默认 attach VectorMemoryFingerprintStore,
        # 启用跨运行去重,无需外部手动 attach。
        if fingerprint_store_path:
            try:
                from core.research_graph.vector_memory import (
                    VectorMemoryFingerprintStore,
                )
                self._fingerprint_store = VectorMemoryFingerprintStore(
                    db_path=fingerprint_store_path,
                )
            except Exception:
                self._fingerprint_store = None
        else:
            self._fingerprint_store = None

    def active_neuron_ids(self) -> list[str]:
        """返回当前激活神经元 ID 列表。"""
        return list(self._active_neuron_ids)

    def attach_fingerprint_store(self, store: Any) -> None:
        """Step 5:注入持久化指纹存储,启用跨运行证据去重。

        Args:
            store: 实现 has_fingerprint/add_fingerprint 协议的对象,
                   通常为 VectorMemoryFingerprintStore。
        """
        from core.research_graph.evidence_novelty_gate import EvidenceNoveltyGate

        if self._evidence_novelty_gate is None:
            self._evidence_novelty_gate = EvidenceNoveltyGate(persistent_store=store)
        else:
            self._evidence_novelty_gate._persistent_store = store  # noqa: SLF001

    def _ensure_novelty_gate(self) -> Any:
        """lazy 创建 EvidenceNoveltyGate(有持久化 store 时自动启用跨运行去重)。"""
        if self._evidence_novelty_gate is None:
            from core.research_graph.evidence_novelty_gate import EvidenceNoveltyGate

            self._evidence_novelty_gate = EvidenceNoveltyGate(
                persistent_store=self._fingerprint_store,
            )
        return self._evidence_novelty_gate

    def _activate_neuron(self, neuron_id: str) -> None:
        """激活神经元加入队列。"""
        self._active_neuron_ids.add(neuron_id)

    def cold_start(self, graph_store: Any = None) -> dict[str, Any]:
        """冷启动:从 GraphStore 节点初始化神经元(spec 9)。

        Args:
            graph_store: 可选 GraphStore,提供初始节点;为 None 时使用 self.graph_store。

        Returns:
            摘要 dict,含 neurons_created/neurons_activated。
        """
        from core.research_graph.neuron.enums import VerificationStatus
        from core.research_graph.neuron.models import ResearchNeuron

        gs = graph_store or self.graph_store
        created = 0
        if gs is not None:
            for node_id, node in gs.nodes.items():
                existing = self.state_store.get_neuron(node_id)
                if existing is not None:
                    # 冷启动是恢复，不是重置；保留已学习的 confidence、营养、
                    # 证据计数和生命周期状态，只补齐后来写入的主题/别名元数据。
                    topic = str((node.attributes or {}).get("topic", ""))
                    changed = False
                    if topic and not existing.root_theme:
                        existing.root_theme = topic
                        changed = True
                    if node.aliases:
                        merged_aliases = list(dict.fromkeys(existing.aliases + list(node.aliases)))
                        if merged_aliases != existing.aliases:
                            existing.aliases = merged_aliases
                            changed = True
                    if changed:
                        self.state_store.save_neuron(existing)
                    self.evidence_index.index_neuron(existing)
                    self._activate_neuron(existing.neuron_id)
                    continue
                neuron = ResearchNeuron(
                    neuron_id=node_id,
                    node_id=node_id,
                    node_type=node.type,
                    canonical_name=node.name,
                    aliases=list(node.aliases),
                    root_theme=str((node.attributes or {}).get("topic", "")),
                    created_from=str(getattr(node, "source", "") or "graph_store"),
                    verification_status="UNVERIFIED_SEED"
                        if node.source == "seed_graph"
                        else VerificationStatus.CANDIDATE.value,
                )
                self.state_store.save_neuron(neuron)
                self.evidence_index.index_neuron(neuron)
                self._activate_neuron(neuron.neuron_id)
                created += 1

        # 修复缺口2/3: 重建证据池。cold_start 创建神经元后，神经元已被
        # evidence_index.index_neuron 索引，此时从 GraphStore.evidence
        # 幂等补充 _evidence_pool/_neuron_evidence_map，避免重启后
        # EvidenceLedgerReaderAdapter 查询为空。同时也修复缺口3：
        # load_graph_store 后 _ensure_environment 触发 cold_start，
        # 即便没有 checkpoint 也能重建证据池。
        rebuilt = self._rebuild_evidence_pool_from_graph_store()
        if rebuilt:
            logger.info(
                "cold_start 重建证据池: 从 GraphStore 补充 %d 条证据", rebuilt,
            )

        return {
            "neurons_created": created,
            "neurons_activated": len(self._active_neuron_ids),
        }

    def cold_start_from_topic(
        self, topic: str, *, max_nodes: int = 8,
    ) -> dict[str, Any]:
        """#3 种子图扩展:用 LLM 生成产业链骨架,初始化 UNVERIFIED_SEED neuron。

        解决冷启动知识空白:让 LLM 生成一个初步的产业链骨架
        (产品→工艺→材料→设备),节点标注 UNVERIFIED_SEED,synapse 强度极低。
        然后让神经元网络去吸收证据、自我修正。

        Args:
            topic: 主题(如"HBM"/"存储"/"光通信")。
            max_nodes: 最大生成节点数,默认 8。

        Returns:
            摘要 dict,含 neurons_created/synapses_created/llm_source。
        """
        import json as _json
        import uuid
        from core.research_graph.neuron.enums import (
            MetabolismState, SynapseDirection, SynapseRelation,
            SynapseStatus, SynapseTargetKind, VerificationStatus,
        )
        from core.research_graph.neuron.models import (
            ResearchNeuron, ResearchSynapse,
        )
        from core.utils.llm_usage import call_llm_with_budget

        system_prompt = (
            "你是产业链分析助手。基于给定的主题,生成一个初步的产业链骨架。"
            "严格遵守:\n"
            "1. 只能基于常识推断,不允许编造具体公司业务或供应关系\n"
            "2. 不输出任何投资建议(买入/卖出/仓位/止盈/止损)\n"
            "3. 必须只返回纯 JSON,禁止任何解释、思考过程、markdown fence 或前后缀文字\n"
            "4. JSON 格式:{\"nodes\": [{\"name\": \"\", \"type\": \"\"}], "
            "\"edges\": [{\"source\": \"\", \"relation\": \"\", \"target\": \"\"}]}\n"
            "5. type 字段只能是:PRODUCT / PROCESS / MATERIAL / EQUIPMENT / COMPANY / APPLICATION\n"
            "6. relation 字段只能是:UPSTREAM_OF / DOWNSTREAM_OF / PART_OF / REQUIRES / MAPS_TO\n"
            f"7. nodes 最多 {max_nodes} 个,edges 最多 {max_nodes * 2} 个\n"
            "8. name 字段填中文具体名称"
        )
        user_prompt = f"主题: {topic}\n请生成产业链骨架:"

        # 调 LLM(失败则退化为空骨架)
        nodes_data: list[dict[str, str]] = []
        edges_data: list[dict[str, str]] = []
        llm_source = "llm"
        try:
            content = call_llm_with_budget(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                source="neuron.cold_start_from_topic",
                providers=self.llm_providers,
                temperature=0.1,
                max_completion_tokens=1024,
                timeout=30,
                allow_no_provider=True,
                tier="pro",
            )
            if content:
                from core.research_graph.json_parse_utils import (
                    ParseFailure, robust_json_parse,
                )
                skeleton = robust_json_parse(content, "cold_start")
                if isinstance(skeleton, ParseFailure):
                    # 第一次未输出合法 JSON,用严格 prompt 重试一次
                    retry_prompt = (
                        "把下面的产业链分析内容转换成严格 JSON。要求:\n"
                        "1. 只输出 JSON,不要任何解释、思考过程或 markdown fence\n"
                        "2. 格式:{\"nodes\":[{\"name\":\"\",\"type\":\"\"}],"
                        "\"edges\":[{\"source\":\"\",\"relation\":\"\",\"target\":\"\"}]}\n"
                        "3. type 只能是:PRODUCT/PROCESS/MATERIAL/EQUIPMENT/COMPANY/APPLICATION\n"
                        "4. relation 只能是:UPSTREAM_OF/DOWNSTREAM_OF/PART_OF/REQUIRES/MAPS_TO\n"
                        "待转换内容:\n" + content[:1500]
                    )
                    retry_content = call_llm_with_budget(
                        messages=[
                            {"role": "system", "content": retry_prompt},
                            {"role": "user", "content": "只返回 JSON:"},
                        ],
                        source="neuron.cold_start_from_topic_retry",
                        providers=self.llm_providers,
                        temperature=0.0,
                        max_completion_tokens=1024,
                        timeout=30,
                        allow_no_provider=True,
                        tier="pro",
                    )
                    if retry_content:
                        skeleton = robust_json_parse(retry_content, "cold_start_retry")
                if not isinstance(skeleton, ParseFailure):
                    nodes_data = skeleton.get("nodes", [])[:max_nodes]
                    edges_data = skeleton.get("edges", [])[:max_nodes * 2]
            else:
                llm_source = "empty_llm"
        except Exception:
            llm_source = "llm_failed"

        # 创建 neuron(UNVERIFIED_SEED + ACTIVE metabolism,等待证据验证)
        created_neurons = 0
        name_to_neuron_id: dict[str, str] = {}
        for node in nodes_data:
            name = str(node.get("name", "")).strip()
            if not name:
                continue
            node_type = str(node.get("type", "CONCEPT")).upper()
            # 限制 node_type 合法性
            if node_type not in {"PRODUCT", "PROCESS", "MATERIAL", "EQUIPMENT",
                                  "COMPANY", "APPLICATION", "CONCEPT"}:
                node_type = "CONCEPT"
            neuron_id = f"n_{topic}_{name}_{uuid.uuid4().hex[:6]}"
            neuron = ResearchNeuron(
                neuron_id=neuron_id,
                node_id=f"node_{name}",
                node_type=node_type,
                canonical_name=name,
                root_theme=topic,
                verification_status=VerificationStatus.UNVERIFIED_SEED.value,
                metabolism_state=MetabolismState.ACTIVE.value,
            )
            self.state_store.save_neuron(neuron)
            self.evidence_index.index_neuron(neuron)
            self._activate_neuron(neuron_id)
            name_to_neuron_id[name] = neuron_id
            created_neurons += 1

        # 创建 synapse(强度 0.1,极低,等待证据增强)
        created_synapses = 0
        valid_directions = {
            "UPSTREAM_OF": SynapseDirection.UPSTREAM_MATERIAL.value,
            "DOWNSTREAM_OF": SynapseDirection.DOWNSTREAM_APPLICATION.value,
            "PART_OF": SynapseDirection.SYNTHESIS_CHILD.value,
            "REQUIRES": SynapseDirection.UP_COMPANY.value,
            "MAPS_TO": SynapseDirection.UP_COMPANY.value,
        }
        for edge in edges_data:
            src_name = str(edge.get("source", "")).strip()
            tgt_name = str(edge.get("target", "")).strip()
            relation = str(edge.get("relation", "UPSTREAM_OF")).upper()
            src_id = name_to_neuron_id.get(src_name)
            tgt_id = name_to_neuron_id.get(tgt_name)
            if not src_id or not tgt_id:
                continue
            direction = valid_directions.get(relation, SynapseDirection.UPSTREAM_MATERIAL.value)
            syn = ResearchSynapse(
                synapse_id=f"syn_seed_{uuid.uuid4().hex[:10]}",
                source_neuron_id=src_id,
                target_kind=SynapseTargetKind.EXISTING_NEURON.value,
                target_neuron_id=tgt_id,
                direction=direction,
                relation=relation,
                dimension="industry_flow",
                strength=0.1,  # 极低,等待证据增强
                status=SynapseStatus.OPEN.value,
            )
            try:
                syn.validate()
                self.state_store.save_synapse(syn)
                created_synapses += 1
            except Exception:
                continue

        return {
            "neurons_created": created_neurons,
            "synapses_created": created_synapses,
            "llm_source": llm_source,
            "topic": topic,
        }

    def dispatch_event(self, event: Any) -> None:
        """投递事件,激活相关神经元加入 active_neuron_queue(spec 6.3)。

        Args:
            event: NeuronEvent 实例(任意子类)。
        """
        self.event_router.dispatch(event)
        self._activate_neuron(event.target_neuron_id)

    def route_evidence(self, evidence: Any, stance: str = "support") -> list[str]:
        """新证据写入后,通过 EvidenceIndex 路由给相关神经元(spec 6.3 push)。

        Args:
            evidence: Evidence 实例。
            stance: 证据立场,support/counter,决定事件类型。

        Returns:
            命中的 neuron_id 列表。
        """
        from core.research_graph.neuron.events import (
            CounterEvidenceArrivedEvent,
            EvidenceArrivedEvent,
        )

        hit_ids = self.evidence_index.lookup(evidence)
        # Plan 6 修复二:存入 evidence_pool 供 EvidenceLedgerReaderAdapter 查询
        self._evidence_pool[evidence.evidence_id] = evidence
        self._evidence_stance[evidence.evidence_id] = stance
        for nid in hit_ids:
            self._neuron_evidence_map.setdefault(nid, []).append(evidence.evidence_id)
        for nid in hit_ids:
            if stance == "counter":
                event = CounterEvidenceArrivedEvent(
                    event_id=f"route_{evidence.evidence_id}_{nid}",
                    target_neuron_id=nid,
                    payload={"counter_evidence_ids": [evidence.evidence_id]},
                )
            else:
                event = EvidenceArrivedEvent(
                    event_id=f"route_{evidence.evidence_id}_{nid}",
                    target_neuron_id=nid,
                    payload={"new_evidence_ids": [evidence.evidence_id]},
                )
            self.dispatch_event(event)
        return list(hit_ids)

    def ingest_policy_event(self, source_row: dict[str, Any], text: str) -> list[str]:
        """Step 2:把政策类 source_row 解析为规范 Evidence 并写入 ledger。

        若 evidence_ledger 未注入则只路由不持久化,保持向后兼容。

        Args:
            source_row: GrowthMiningEngine._persist_source 产出的字典。
            text: 该 source 对应的原始正文。

        Returns:
            已路由的 evidence_id 列表。
        """
        from core.research_graph.policy_event_parser import parse_policy_event

        routed: list[str] = []
        for evidence in parse_policy_event(source_row, text):
            if self.evidence_ledger is not None:
                try:
                    self.evidence_ledger.add(evidence, operator="policy_event_parser")
                except ValueError:
                    continue
            self.route_evidence(evidence, stance="support")
            routed.append(evidence.evidence_id)
        return routed

    def _build_neuron_context(
        self, neuron_id: str, snapshot_id: str
    ) -> Any:
        """为 neuron 构建 NeuronContext(spec 6.6,禁止万能上帝对象)。

        Args:
            neuron_id: 神经元 ID。
            snapshot_id: 一致性快照 ID。

        Returns:
            NeuronContext 实例,neuron 不存在返回 None。
        """
        from core.research_graph.neuron.enums import EventType
        from core.research_graph.neuron.models import (
            NeighborRef,
            NeuronContext,
            ResourceBudget,
        )

        neuron = self.state_store.get_neuron(neuron_id)
        if neuron is None:
            return None

        # incident synapses + neighbor refs(1 跳)
        synapses = self.state_store.get_synapses_by_neuron(neuron_id)
        incident_synapse_ids = [s.synapse_id for s in synapses
                                if s.source_neuron_id == neuron_id]
        neighbor_refs: list[NeighborRef] = []
        for syn in synapses:
            if syn.source_neuron_id != neuron_id:
                continue
            if syn.target_kind != "EXISTING_NEURON" or not syn.target_neuron_id:
                continue
            neighbor = self.state_store.get_neuron(syn.target_neuron_id)
            if neighbor is None or neighbor.is_tombstone:
                continue
            neighbor_refs.append(NeighborRef(
                neuron_id=neighbor.neuron_id, node_id=neighbor.node_id,
                node_type=neighbor.node_type, canonical_name=neighbor.canonical_name,
                verification_status=neighbor.verification_status,
                metabolism_state=neighbor.metabolism_state,
                confidence=neighbor.confidence,
                relation=syn.relation, direction=syn.direction, dimension=syn.dimension,
            ))

        # 从 pending events 提取 evidence_refs(消费式 drain)
        events = self.event_router.drain(neuron_id)
        new_evidence_refs: list[str] = []
        stale_evidence_refs: list[str] = []
        manual_mutation_refs: list[str] = []
        # Plan 6 修复三/四:TIME_TICK / COUNTER_EVIDENCE payload 提取
        counter_evidence_refs: list[str] = []
        staleness_delta: float = 0.0
        tick_reason: str = ""
        trigger_event_type: str = ""
        for ev in events:
            if ev.event_type == EventType.EVIDENCE_ARRIVED.value:
                new_evidence_refs.extend(ev.payload.get("new_evidence_ids", []))
                if not trigger_event_type:
                    trigger_event_type = "EvidenceArrivedEvent"
            elif ev.event_type == EventType.COUNTER_EVIDENCE_ARRIVED.value:
                counter_evidence_refs.extend(ev.payload.get("counter_evidence_ids", []))
                trigger_event_type = "CounterEvidenceArrivedEvent"
            elif ev.event_type == EventType.EVIDENCE_RETRACTED.value:
                stale_evidence_refs.extend(ev.payload.get("retracted_evidence_ids", []))
                trigger_event_type = "EvidenceRetractedEvent"
            elif ev.event_type == EventType.MANUAL_MUTATION.value:
                manual_mutation_refs.extend(ev.payload.get("mutation_refs", []))
                if not trigger_event_type:
                    trigger_event_type = "ManualMutationEvent"
            elif ev.event_type == EventType.TIME_TICK_METABOLISM.value:
                staleness_delta = float(ev.payload.get("staleness_delta", 0.0))
                tick_reason = ev.payload.get("tick_reason", "")
                trigger_event_type = "TimeTickMetabolismEvent"

        # incoming_pulses:邻居发出且 related_node_ids 含本 neuron node_id 的 EMITTED pulse
        # 排除自己发出的 pulse(spec 6.6)
        emitted_pulses = self.state_store.get_emitted_pulses(limit=200)
        incoming_pulses = [
            p for p in emitted_pulses
            if p.emitter_neuron_id != neuron_id
            and neuron.node_id in (p.related_node_ids or [])
        ]

        # Plan 6 修复二:注入真实 reader/writer adapter
        synapse_reader = SynapseStateReaderAdapter(self.state_store)
        evidence_reader = EvidenceLedgerReaderAdapter(
            self._evidence_pool, self._neuron_evidence_map, self._evidence_stance,
        )
        mutation_writer = MutationLogWriterAdapter(self.state_store)

        # current_time:TimeTick 携带则用,否则用 utc_now
        from core.research_graph.models import utc_now
        current_time = ""
        if staleness_delta > 0:
            current_time = events[0].payload.get("current_time", "") if events else ""
        if not current_time:
            current_time = utc_now()

        return NeuronContext(
            graph_snapshot_id=snapshot_id,
            current_time=current_time,
            cycle_id=f"cycle_{neuron_id}_{snapshot_id}",
            neighbor_refs=neighbor_refs,
            incident_synapse_ids=incident_synapse_ids,
            new_evidence_refs=new_evidence_refs,
            stale_evidence_refs=stale_evidence_refs,
            manual_mutation_refs=manual_mutation_refs,
            incoming_pulses=incoming_pulses,
            resource_budget=ResourceBudget(),
            policy_config=None,
            synapse_state_reader=synapse_reader,
            evidence_ledger_reader=evidence_reader,
            mutation_log_writer=mutation_writer,
            trigger_event_type=trigger_event_type,
            counter_evidence_refs=counter_evidence_refs,
            staleness_delta=staleness_delta,
            tick_reason=tick_reason,
        )

    def consume_pulse(self, pulse: Any) -> dict[str, Any]:
        """消费 GrowthPulse(spec 6.4,环境消费,不生成)。

        consumed=True 时自动将 pulse.status 置为 ABSORBED 并持久化,避免
        check_termination 的 queue_empty 分支被遗留的 EMITTED pulse 误导。

        Args:
            pulse: GrowthPulse 实例。

        Returns:
            结果 dict,含 consumed/reason,consumed=True 表示已处理。
        """
        from core.research_graph.neuron.enums import PulseStatus

        if pulse.pulse_type not in self.PULSE_WHITELIST:
            return {"consumed": False, "reason": f"not_in_whitelist: {pulse.pulse_type}"}

        handler = {
            "REQUEST_GROWTH_MINING": self._handle_growth_mining,
            "MERGE_ALIAS": self._handle_merge_alias,
            "SPLIT_TOO_COARSE": self._handle_split,
            "PRUNE_DISPROVEN": self._handle_prune,
            "REQUEST_HUMAN_REVIEW": self._handle_human_review,
            "EXPAND_COMPANY_MAPPING": self._handle_expand_company,
            "CHECK_COUNTER_EVIDENCE": self._handle_counter_evidence,
            # 阶段 3:5 个扩展方向脉冲统一走 _handle_expand_direction
            "EXPAND_UPSTREAM_MATERIAL": self._handle_expand_direction,
            "EXPAND_DOWNSTREAM_APPLICATION": self._handle_expand_direction,
            "EXPAND_SYNTHESIS_CHILDREN": self._handle_expand_direction,
            "EXPAND_TARGET_MATERIAL": self._handle_expand_direction,
            "EXPAND_EQUIPMENT": self._handle_expand_direction,
            # Step 8:时序因果筛选脉冲
            "REQUEST_CAUSAL_SCREENING": self._handle_causal_screening,
            # P0 Sprint:事件研究脉冲
            "REQUEST_EVENT_STUDY": self._handle_event_study,
        }.get(pulse.pulse_type)

        if handler is None:
            return {"consumed": False, "reason": f"no_handler: {pulse.pulse_type}"}

        result = handler(pulse)
        if result.get("consumed"):
            pulse.status = PulseStatus.ABSORBED.value
            self.state_store.save_pulse(pulse)
        return result

    def _handle_merge_alias(self, pulse: Any) -> dict[str, Any]:
        """执行 MERGE_ALIAS(spec 6.4):从 payload 提取参数调用 lifecycle.merge_neuron。

        Args:
            pulse: MERGE_ALIAS 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed/merged_id。
        """
        payload = pulse.payload or {}
        source_ids = payload.get("source_neuron_ids", [])
        merged_spec = payload.get("merged_spec", {})
        reason = payload.get("reason", "alias_overlap")
        if len(source_ids) < 2 or not merged_spec:
            return {"consumed": False, "reason": "invalid_merge_payload"}
        try:
            merged_id = self.lifecycle_manager.merge_neuron(
                self.state_store, source_ids, merged_spec, reason,
                cycle_id=f"merge_{pulse.pulse_id}",
            )
        except Exception as exc:
            return {"consumed": False, "reason": f"merge_failed: {exc}"}
        return {"consumed": True, "reason": "merged", "merged_id": merged_id}

    def _handle_split(self, pulse: Any) -> dict[str, Any]:
        """执行 SPLIT_TOO_COARSE(spec 6.4):从 payload 提取参数调用 lifecycle.split_neuron。

        Args:
            pulse: SPLIT_TOO_COARSE 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed/child_ids。
        """
        payload = pulse.payload or {}
        split_spec = payload.get("split_spec", [])
        evidence_partition = payload.get("evidence_partition", {})
        reason = payload.get("reason", "too_coarse")
        if not split_spec:
            return {"consumed": False, "reason": "invalid_split_payload"}
        try:
            child_ids = self.lifecycle_manager.split_neuron(
                self.state_store, pulse.emitter_neuron_id,
                split_spec, evidence_partition, reason,
                cycle_id=f"split_{pulse.pulse_id}",
            )
        except Exception as exc:
            return {"consumed": False, "reason": f"split_failed: {exc}"}
        return {"consumed": True, "reason": "split", "child_ids": child_ids}

    def _handle_prune(self, pulse: Any) -> dict[str, Any]:
        """执行 PRUNE_DISPROVEN(spec 6.4):调用 lifecycle.prune_neuron 标记 tombstone。

        Args:
            pulse: PRUNE_DISPROVEN 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed。
        """
        payload = pulse.payload or {}
        reason = payload.get("reason", "disproven")
        evidence_ids = payload.get("evidence_ids", [])
        try:
            self.lifecycle_manager.prune_neuron(
                self.state_store, pulse.emitter_neuron_id, reason,
                evidence_ids=evidence_ids, cycle_id=f"prune_{pulse.pulse_id}",
            )
        except Exception as exc:
            return {"consumed": False, "reason": f"prune_failed: {exc}"}
        return {"consumed": True, "reason": "pruned"}

    def _handle_human_review(self, pulse: Any) -> dict[str, Any]:
        """执行 REQUEST_HUMAN_REVIEW(spec 6.4):标记 emitter attention_required=True。

        Args:
            pulse: REQUEST_HUMAN_REVIEW 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed。
        """
        neuron = self.state_store.get_neuron(pulse.emitter_neuron_id)
        if neuron is None:
            return {"consumed": False, "reason": "emitter_not_found"}
        neuron.attention_required = True
        self.state_store.save_neuron(neuron)
        return {"consumed": True, "reason": "attention_required_set"}

    def _handle_expand_company(self, pulse: Any) -> dict[str, Any]:
        """执行 EXPAND_COMPANY_MAPPING(spec 6.4):创建 TARGET_DRAFT synapse 等待验证。

        Args:
            pulse: EXPAND_COMPANY_MAPPING 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed/synapse_id。
        """
        import uuid
        from core.research_graph.neuron.enums import (
            SynapseDirection, SynapseRelation, SynapseStatus, SynapseTargetKind,
        )
        from core.research_graph.neuron.models import (
            MutationEntry, ResearchSynapse, TargetDraft,
        )

        payload = pulse.payload or {}
        draft_payload = payload.get("target_draft", {})
        if not draft_payload:
            return {"consumed": False, "reason": "no_target_draft"}
        dimension = payload.get("dimension", "company_chain")
        synapse_id = f"syn_expand_{uuid.uuid4().hex[:12]}"
        target_draft = TargetDraft(
            proposed_node_type=draft_payload.get("proposed_node_type", "COMPANY"),
            proposed_name=draft_payload.get("proposed_name", ""),
            reason=draft_payload.get("reason", ""),
            evidence_ids=list(draft_payload.get("evidence_ids", [])),
        )
        syn = ResearchSynapse(
            synapse_id=synapse_id,
            source_neuron_id=pulse.emitter_neuron_id,
            target_kind=SynapseTargetKind.TARGET_DRAFT.value,
            target_draft=target_draft,
            direction=SynapseDirection.UP_COMPANY.value,
            relation=SynapseRelation.MAPS_TO.value,
            dimension=dimension,
            status=SynapseStatus.OPEN.value,
        )
        syn.validate()
        self.state_store.save_synapse(syn)
        self.state_store.save_mutation(MutationEntry(
            mutation_id=f"mut_{uuid.uuid4().hex[:12]}",
            cycle_id=f"expand_{pulse.pulse_id}",
            action="SYNAPSE_CREATED",
            target_type="synapse",
            target_id=synapse_id,
            before={},
            after={"target_kind": syn.target_kind, "proposed_name": target_draft.proposed_name},
            reason=f"expand_company_mapping_from_{pulse.pulse_id}",
            evidence_ids=list(target_draft.evidence_ids),
        ))
        return {"consumed": True, "reason": "target_draft_created", "synapse_id": synapse_id}

    def _handle_expand_direction(self, pulse: Any) -> dict[str, Any]:
        """处理 EXPAND_UPSTREAM_MATERIAL / EXPAND_SYNTHESIS_CHILDREN 等方向脉冲(#2)。

        当神经元发出这类脉冲但 target_draft 为空时,环境介入:
          - llm_target_draft_enabled=True: 调 LLM 生成候选目标节点
          - llm_target_draft_enabled=False: 退化为规则模板(node_type 推断,name 留空)

        生成的 synapse 状态为 OPEN(待证据验证),target_kind=TARGET_DRAFT。

        Args:
            pulse: EXPAND_* 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed/synapse_id/target_draft_source。
        """
        import json as _json
        import uuid
        from core.research_graph.neuron.enums import (
            SynapseDirection, SynapseRelation, SynapseStatus, SynapseTargetKind,
        )
        from core.research_graph.neuron.models import (
            MutationEntry, ResearchSynapse, TargetDraft,
        )

        pulse_type = pulse.pulse_type
        direction_info = self._EXPAND_DIRECTION_MAP.get(pulse_type)
        if direction_info is None:
            return {"consumed": False, "reason": f"unknown_direction: {pulse_type}"}
        direction, relation, dimension, default_node_type = direction_info

        # 优先用 pulse.payload 里已有的 target_draft(若 neuron 已填充)
        payload = pulse.payload or {}
        draft_payload = payload.get("target_draft", {})
        target_draft: TargetDraft
        source = "preset"

        if draft_payload:
            target_draft = TargetDraft(
                proposed_node_type=draft_payload.get("proposed_node_type", default_node_type),
                proposed_name=draft_payload.get("proposed_name", ""),
                reason=draft_payload.get("reason", ""),
                evidence_ids=list(draft_payload.get("evidence_ids", [])),
            )
        elif self.llm_target_draft_enabled:
            # LLM 生成候选目标节点
            llm_result = self._generate_target_draft_via_llm(pulse, direction, default_node_type)
            target_draft = TargetDraft(
                proposed_node_type=llm_result.get("node_type", default_node_type),
                proposed_name=llm_result.get("name", ""),
                reason=llm_result.get("reason", f"LLM 建议({pulse_type})"),
                evidence_ids=[],
            )
            source = "llm"
        else:
            # 规则模板退化:name 留空,等待后续证据挖掘补全
            target_draft = TargetDraft(
                proposed_node_type=default_node_type,
                proposed_name="",
                reason=f"规则模板({pulse_type}),待证据补全",
                evidence_ids=[],
            )
            source = "rule_fallback"

        # 构造 TARGET_DRAFT synapse
        synapse_id = f"syn_expand_{uuid.uuid4().hex[:12]}"
        syn = ResearchSynapse(
            synapse_id=synapse_id,
            source_neuron_id=pulse.emitter_neuron_id,
            target_kind=SynapseTargetKind.TARGET_DRAFT.value,
            target_draft=target_draft,
            direction=direction,
            relation=relation,
            dimension=dimension,
            status=SynapseStatus.OPEN.value,
        )
        syn.validate()
        self.state_store.save_synapse(syn)
        self.state_store.save_mutation(MutationEntry(
            mutation_id=f"mut_{uuid.uuid4().hex[:12]}",
            cycle_id=f"expand_{pulse.pulse_id}",
            action="SYNAPSE_CREATED",
            target_type="synapse",
            target_id=synapse_id,
            before={},
            after={"target_kind": syn.target_kind,
                   "proposed_name": target_draft.proposed_name,
                   "direction": direction},
            reason=f"{pulse_type}_from_{pulse.pulse_id}_via_{source}",
            evidence_ids=[],
        ))
        return {
            "consumed": True,
            "reason": f"target_draft_created_via_{source}",
            "synapse_id": synapse_id,
            "target_draft_source": source,
            "proposed_name": target_draft.proposed_name,
        }

    def _generate_target_draft_via_llm(
        self, pulse: Any, direction: str, default_node_type: str,
    ) -> dict[str, str]:
        """调 LLM 生成候选目标节点(#2 核心)。

        把神经元当前状态(节点类型/名称/已有证据摘要/邻居结构)喂给 LLM,
        让它提出候选目标节点(如"PVD 的上游材料可能是高纯铝靶材、铜靶材")。

        Args:
            pulse: EXPAND_* 类型的 GrowthPulse。
            direction: 扩展方向(UPSTREAM_MATERIAL / SYNTHESIS_CHILD 等)。
            default_node_type: 默认节点类型。

        Returns:
            dict 含 node_type / name / reason。
        """
        from core.utils.llm_usage import call_llm_with_budget

        neuron = self.state_store.get_neuron(pulse.emitter_neuron_id)
        if neuron is None:
            return {"node_type": default_node_type, "name": "", "reason": "neuron_not_found"}

        # 收集邻居结构(1 跳)
        synapses = self.state_store.get_synapses_by_neuron(neuron.neuron_id)
        neighbors: list[str] = []
        for syn in synapses[:5]:  # 限制 5 个避免 prompt 过长
            tgt = syn.target_neuron_id or syn.target_draft.proposed_name if syn.target_draft else ""
            if tgt:
                neighbors.append(f"{syn.direction}→{tgt}")

        system_prompt = (
            "你是产业链分析助手。基于给定的神经元信息,提出 1 个候选目标节点。"
            "严格遵守:\n"
            "1. 只能基于提供的神经元信息推断,不允许编造公司业务或供应关系\n"
            "2. 不输出任何投资建议(买入/卖出/仓位/止盈/止损)\n"
            "3. 必须返回 JSON 格式:{\"node_type\": \"\", \"name\": \"\", \"reason\": \"\"}\n"
            "4. name 字段填具体材料/工艺/公司名(中文),reason 填推断依据(中文,≤50字)"
        )
        user_prompt = (
            f"当前神经元:node_id={neuron.node_id}, type={neuron.node_type}, "
            f"name={neuron.canonical_name}, root_theme={neuron.root_theme}\n"
            f"扩展方向: {direction} (寻找 {default_node_type} 类型的候选节点)\n"
            f"已有邻居: {', '.join(neighbors) if neighbors else '无'}\n"
            f"脉冲原因: {pulse.reason}\n"
            f"请提出 1 个候选 {default_node_type} 节点:"
        )

        try:
            content = call_llm_with_budget(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                source="neuron.target_draft",
                providers=self.llm_providers,
                temperature=0.0,
                max_completion_tokens=256,
                response_format_json=True,
                timeout=20,
                allow_no_provider=True,
                tier="pro",
            )
            if content:
                from core.research_graph.json_parse_utils import (
                    ParseFailure, robust_json_parse,
                )
                result = robust_json_parse(content, "target_draft")
                if not isinstance(result, ParseFailure):
                    return {
                        "node_type": str(result.get("node_type", default_node_type))[:50],
                        "name": str(result.get("name", ""))[:100],
                        "reason": str(result.get("reason", ""))[:200],
                    }
        except Exception as exc:
            # LLM 失败不阻塞,退化为空 name
            pass
        return {"node_type": default_node_type, "name": "", "reason": "llm_failed"}

    def _handle_counter_evidence(self, pulse: Any) -> dict[str, Any]:
        """执行 CHECK_COUNTER_EVIDENCE(spec 6.4):调用 growth_mining_engine_factory 搜索反证。

        通过 factory 模式注入真实 engine(GrowthMiningEngine(request).run())或 mock factory,
        消除"engine 是可重用实例"的错误假设。复用 _handle_growth_mining 的 factory 调用逻辑,
        但语义为反证搜索,触发 CounterEvidenceArrivedEvent 而非 EvidenceArrivedEvent。

        阶段 5 增强(#4 自我质疑与反向验证):
        若 llm_counter_question_enabled=True 且能查到 emitter 神经元的 support evidence,
        先调 LLM 生成反证问题(如"主营业务是否真的包含电镀液?收入占比多少?"),
        再把首个问题注入 GrowthMiningRequest.topic 驱动搜索,让系统主动找反证
        而非被动等待。LLM 失败时退化为原行为(用 pulse.payload.topic)。

        Args:
            pulse: CHECK_COUNTER_EVIDENCE 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed/run_status/support_evidence_count/counter_questions。
        """
        from core.research_graph.growth_mining import GrowthMiningRequest

        if self.growth_mining_engine_factory is None:
            return {"consumed": False, "reason": "no_growth_mining_engine_factory"}

        # 阶段 5:LLM 生成反证问题(主动找反证)
        counter_questions: list[str] = []
        topic_override = ""
        if self.llm_counter_question_enabled:
            counter_questions = self._generate_counter_questions_via_llm(pulse.emitter_neuron_id)
            if counter_questions:
                # 用首个反证问题覆盖 topic,驱动 growth_mining 搜索
                topic_override = counter_questions[0]

        # 阶段 2:注入 use_system_search 开关;阶段 5:topic_override 覆盖搜索查询;
        # 介入点 1:llm_evidence_extraction_enabled 控制证据抽取增强
        overrides: dict[str, Any] = {
            "use_system_search": self.enable_system_search,
            "allow_llm_assist": self.llm_evidence_extraction_enabled,
        }
        if topic_override:
            overrides["topic"] = topic_override
        request = GrowthMiningRequest.from_pulse(pulse, **overrides)
        try:
            result = self.growth_mining_engine_factory(request)
        except Exception as exc:
            return {"consumed": False, "reason": f"engine_failed: {exc}"}

        emitter_neuron = self.state_store.get_neuron(pulse.emitter_neuron_id)
        routed_evidence_ids: list[str] = []
        counter_ingest_stats: dict[str, Any] = {
            "routed_evidence_ids": [],
            "novelty_skipped": 0,
            "ledger_rejected": 0,
            "total_attempted": 0,
        }
        if emitter_neuron is not None and result.support_evidence_count > 0:
            counter_ingest_stats = self._ingest_growth_run_evidence(
                result,
                emitter_neuron=emitter_neuron,
                stance="counter",
            )
            routed_evidence_ids = counter_ingest_stats["routed_evidence_ids"]

        # Step 7:若注入了 retriever,先从向量记忆召回历史反证文档作为补充。
        # 让反证搜索从"被动等待 growth_mining"升级为"先查本地历史 + 再触发新搜索"。
        historic_counter_chunks: list[dict[str, Any]] = []
        if self.retriever is not None:
            try:
                hypothesis = topic_override or str(getattr(pulse.payload, "topic", "") or "")
                chunks = self.retriever.retrieve(
                    hypothesis or emitter_neuron.node_id if emitter_neuron else "",
                    limit=5,
                    hypothesis=hypothesis or None,
                    stance="counter",
                )
                for chunk in chunks:
                    historic_counter_chunks.append({
                        "text": str(getattr(chunk, "text", "") or getattr(chunk, "content", "") or ""),
                        "source": str(getattr(chunk, "source", "") or ""),
                        "publish_date": str(getattr(chunk, "publish_date", "") or ""),
                    })
            except Exception:
                pass

        return {
            "consumed": True,
            "reason": "counter_evidence_searched",
            "run_status": result.run_status,
            "support_evidence_count": result.support_evidence_count,
            "counter_questions": counter_questions,
            "routed_evidence_ids": routed_evidence_ids,
            "novelty_skipped": counter_ingest_stats["novelty_skipped"],
            "ledger_rejected": counter_ingest_stats["ledger_rejected"],
            "total_attempted": counter_ingest_stats["total_attempted"],
            "historic_counter_chunks": historic_counter_chunks,
            "run_dir": str(getattr(result, "run_dir", "")),
        }

    def _handle_causal_screening(self, pulse: Any) -> dict[str, Any]:
        """Step 8:执行 REQUEST_CAUSAL_SCREENING 脉冲。

        通过 TemporalCausalService 对序列做 Granger/BSTS 归因,
        结果以 CANDIDATE evidence 写入 ledger,不直接修改图谱。
        pulse.payload 期望字段:
            cause_symbol/effect_symbol:序列标签
            cause_series/effect_series:数值序列
            event_date:干预事件日期(可选)
            intervention_point:BSTS 干预时点索引(可选)

        依赖未安装时 graceful 降级为 TEMPORAL_PRECEDENCE evidence。

        Args:
            pulse: REQUEST_CAUSAL_SCREENING 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed/causal_status/evidence_id。
        """
        from core.research_graph.temporal_causal_service import TemporalCausalService

        payload = getattr(pulse, "payload", {}) or {}
        cause_symbol = str(payload.get("cause_symbol", ""))
        effect_symbol = str(payload.get("effect_symbol", ""))
        cause_series = list(payload.get("cause_series", []) or [])
        effect_series = list(payload.get("effect_series", []) or [])
        event_date = str(payload.get("event_date", ""))
        intervention_point = payload.get("intervention_point")

        if not cause_symbol or not effect_symbol:
            return {"consumed": False, "reason": "missing_symbols"}
        if not effect_series:
            return {"consumed": False, "reason": "missing_effect_series"}

        service = TemporalCausalService()
        if intervention_point is not None:
            candidate = service.bsts_attribution(
                effect_series,
                intervention_point=int(intervention_point),
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                event_date=event_date,
            )
        else:
            if not cause_series:
                return {"consumed": False, "reason": "missing_cause_series"}
            candidate = service.granger_screen(
                cause_series,
                effect_series,
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
            )

        evidence_id = f"tcs_{pulse.emitter_neuron_id}_{cause_symbol}_{effect_symbol}"
        evidence = service.screen_to_evidence(
            candidate,
            evidence_id=evidence_id,
            event_date=event_date,
        )
        # P0 修复:canonical evidence 入账一致性 + novelty gate。
        # 重复 causal evidence 应被 novelty gate 拦截,而非覆盖已有记录。
        gate = self._ensure_novelty_gate()
        decision = gate.evaluate(evidence)
        if not decision.is_novel:
            return {
                "consumed": False,
                "reason": f"novelty_gate_rejected: {decision.reason}",
                "causal_status": candidate.causal_status,
                "evidence_id": evidence_id,
                "backends_available": service.is_available(),
                "novelty_skipped": 1,
            }
        if self.evidence_ledger is None:
            return {
                "consumed": False,
                "reason": "no_evidence_ledger",
                "causal_status": candidate.causal_status,
                "evidence_id": evidence_id,
                "backends_available": service.is_available(),
            }
        try:
            self.evidence_ledger.add(evidence, operator="temporal_causal_service")
        except ValueError as exc:
            return {
                "consumed": False,
                "reason": f"ledger_rejected: {exc}",
                "causal_status": candidate.causal_status,
                "evidence_id": evidence_id,
                "backends_available": service.is_available(),
                "ledger_rejected": 1,
            }
        gate.register(evidence)
        self.route_evidence(evidence, stance="support")
        routed = [evidence.evidence_id]
        return {
            "consumed": True,
            "reason": "causal_screened",
            "causal_status": candidate.causal_status,
            "lag": candidate.lag,
            "stability_score": candidate.stability_score,
            "p_value": candidate.p_value,
            "evidence_id": evidence_id,
            "routed_evidence_ids": routed,
            "novelty_skipped": 0,
            "ledger_rejected": 0,
            "total_attempted": 1,
            "backends_available": service.is_available(),
        }

    def _handle_event_study(self, pulse: Any) -> dict[str, Any]:
        """P0 Sprint:执行 REQUEST_EVENT_STUDY 脉冲。

        通过 EventStudyService 对 stock/market 序列做 CAR/BHAR + 安慰剂检验,
        结果以 QUASI_EXPERIMENTAL_EFFECT evidence 写入 ledger(显著 + 安慰剂通过时)。

        pulse.payload 期望字段:
            stock_returns/market_returns:收益率序列
            event_date_idx:事件日索引(0-based)
            window:事件窗口偏移(默认 (-5, 5))
            regime_label:体制标签(可选,透传到 evidence.metadata)
            cause_symbol/effect_symbol:序列标签(用于 evidence_id 构造)

        Args:
            pulse: REQUEST_EVENT_STUDY 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed/causal_status/evidence_id/placebo_p_value。
        """
        import math
        import uuid

        from core.research_graph.models import Evidence, utc_now
        from core.research_graph.schema import (
            CausalStatus,
            EvidenceLevel,
        )

        if self.event_study_service is None:
            return {"consumed": False, "reason": "no_event_study_service"}

        payload = getattr(pulse, "payload", {}) or {}
        stock_returns = list(payload.get("stock_returns", []) or [])
        market_returns = list(payload.get("market_returns", []) or [])
        event_date_idx = payload.get("event_date_idx")
        window = payload.get("window", [-5, 5])
        regime_label = str(payload.get("regime_label", "") or "")
        cause_symbol = str(payload.get("cause_symbol", "event") or "event")
        effect_symbol = str(payload.get("effect_symbol", "stock") or "stock")

        if not stock_returns or not market_returns:
            return {"consumed": False, "reason": "missing_returns"}
        if event_date_idx is None:
            return {"consumed": False, "reason": "missing_event_date_idx"}

        event_date_idx = int(event_date_idx)
        window_tuple = (int(window[0]), int(window[1])) if isinstance(window, (list, tuple)) else (-5, 5)

        service = self.event_study_service

        # 1. 计算事件窗口 + CAR 统计量(含 sigma_ar)
        start = max(0, event_date_idx + window_tuple[0])
        end = min(len(stock_returns) - 1, len(market_returns) - 1, event_date_idx + window_tuple[1])
        car_result = service.compute_car_with_stats(
            stock_returns, market_returns, (start, end)
        )
        bhar = service.compute_bhar(stock_returns, market_returns, (start, end))

        # 2. 安慰剂检验
        placebo = service.placebo_test(
            stock_returns,
            market_returns,
            event_date_idx,
            window=window_tuple,
            n_placebo=50,
        )

        # 3. 决定 causal_status:
        # - 原事件显著 AND 安慰剂通过 → QUASI_EXPERIMENTAL_EFFECT
        # - 否则 → OBSERVATIONAL_ASSOCIATION
        is_significant = (
            car_result.p_value is not None
            and car_result.p_value < 0.05
            and placebo.is_significant
        )
        causal_status = (
            CausalStatus.QUASI_EXPERIMENTAL_EFFECT.value
            if is_significant
            else CausalStatus.OBSERVATIONAL_ASSOCIATION.value
        )

        # 4. 构造 pre_period_rmse 代理值(让 Verifier QUASI 门控通过)
        # 策略:sigma_AR * sqrt(estimation_window),作为干预前 RMSE 的合理代理。
        pre_period_rmse: float | None = None
        if car_result.sigma_ar is not None and car_result.sigma_ar > 0:
            pre_period_rmse = float(
                car_result.sigma_ar * math.sqrt(service.estimation_window)
            )

        # 5. 构造 evidence_id
        evidence_id = (
            f"es_{pulse.emitter_neuron_id}_{cause_symbol}_{effect_symbol}_"
            f"{uuid.uuid4().hex[:8]}"
        )

        # 6. 构造 Evidence
        emitter = self.state_store.get_neuron(pulse.emitter_neuron_id)
        support_node_ids = [emitter.node_id] if emitter is not None else []
        quote = (
            f"{cause_symbol} -> {effect_symbol} "
            f"(car={car_result.car:.4f}, bhar={bhar:.4f}, "
            f"p={car_result.p_value}, placebo_p={placebo.placebo_p_value:.3f}, "
            f"status={causal_status})"
        )
        metadata = {
            "cause_symbol": cause_symbol,
            "effect_symbol": effect_symbol,
            "car": car_result.car,
            "bhar": bhar,
            "t_stat": car_result.t_stat,
            "p_value": car_result.p_value,
            "placebo_p_value": placebo.placebo_p_value,
            "placebo_is_significant": placebo.is_significant,
            "placebo_n": placebo.n_placebo,
            "pre_period_rmse": pre_period_rmse,
            "regime_label": regime_label,
            "event_date_idx": event_date_idx,
            "event_window": list(window_tuple),
            "estimation_window": service.estimation_window,
            "method": "ols" if service.is_statsmodels_available() else "simple_diff",
            "content_origin": "event_study_service",
            "generated_at": utc_now(),
        }
        evidence = Evidence(
            evidence_id=evidence_id,
            source_type="event_study_service",
            source_title=f"event_study_{cause_symbol}_{effect_symbol}",
            source_id=f"es_{cause_symbol}_{effect_symbol}",
            quote=quote,
            summary=f"事件研究法:{quote}",
            evidence_level=EvidenceLevel.LIKELY.value,
            supports=support_node_ids,
            extracted_by="event_study_service",
            verified_by="rule_verifier",
            causal_status=causal_status,
            metadata=metadata,
        )

        # 7. novelty gate → ledger.add → route_evidence(复用 _handle_causal_screening 模式)
        gate = self._ensure_novelty_gate()
        decision = gate.evaluate(evidence)
        if not decision.is_novel:
            return {
                "consumed": False,
                "reason": f"novelty_gate_rejected: {decision.reason}",
                "causal_status": causal_status,
                "evidence_id": evidence_id,
                "placebo_p_value": placebo.placebo_p_value,
                "novelty_skipped": 1,
            }
        if self.evidence_ledger is None:
            return {
                "consumed": False,
                "reason": "no_evidence_ledger",
                "causal_status": causal_status,
                "evidence_id": evidence_id,
                "placebo_p_value": placebo.placebo_p_value,
            }
        try:
            self.evidence_ledger.add(evidence, operator="event_study_service")
        except ValueError as exc:
            return {
                "consumed": False,
                "reason": f"ledger_rejected: {exc}",
                "causal_status": causal_status,
                "evidence_id": evidence_id,
                "placebo_p_value": placebo.placebo_p_value,
                "ledger_rejected": 1,
            }
        gate.register(evidence)
        self.route_evidence(evidence, stance="support")
        routed = [evidence.evidence_id]
        return {
            "consumed": True,
            "reason": "event_study_completed",
            "causal_status": causal_status,
            "car": car_result.car,
            "bhar": bhar,
            "t_stat": car_result.t_stat,
            "p_value": car_result.p_value,
            "placebo_p_value": placebo.placebo_p_value,
            "placebo_is_significant": placebo.is_significant,
            "evidence_id": evidence_id,
            "routed_evidence_ids": routed,
            "novelty_skipped": 0,
            "ledger_rejected": 0,
            "total_attempted": 1,
        }

    def _generate_counter_questions_via_llm(self, neuron_id: str) -> list[str]:
        """#4 自我质疑:用 LLM 基于神经元 support evidence 生成反证问题。

        流程:
        1. 从 EvidenceLedgerReaderAdapter 查询神经元 support evidence
        2. 取 top 3 evidence quote 拼上下文,连同神经元名称/类型喂 LLM
        3. LLM 返回 JSON 数组(2-3 个反证问题)
        4. 任何异常都返回空列表,调用方退化为原行为

        Args:
            neuron_id: 发出 CHECK_COUNTER_EVIDENCE 脉冲的神经元 ID。

        Returns:
            反证问题列表(失败返回空列表,不抛异常)。
        """
        import json as _json
        from core.utils.llm_usage import call_llm_with_budget

        neuron = self.state_store.get_neuron(neuron_id)
        if neuron is None:
            return []

        # 查询 support evidence(复用 environment 内部 adapter)
        reader = EvidenceLedgerReaderAdapter(
            self._evidence_pool, self._neuron_evidence_map, self._evidence_stance,
        )
        support_evs = reader.query_by_neuron(neuron_id, stance="support")
        if not support_evs:
            return []

        # 取 top 3 evidence quote(按 weight 降序)
        sorted_evs = sorted(support_evs, key=lambda e: getattr(e, "weight", 0.5), reverse=True)
        evidence_summary = "\n".join(
            f"- {ev.quote[:120]} (来源: {ev.source_type})"
            for ev in sorted_evs[:3]
        )

        system_prompt = (
            "你是产业链研究的反证提问助手。基于给定神经元的已有 support evidence,"
            "提出 2-3 个可能的反证问题,用于检验该神经元主张是否站得住脚。\n"
            "严格遵守:\n"
            "1. 不输出任何投资建议(买入/卖出/仓位/止盈/止损)\n"
            "2. 不凭空编造公司业务或证据,只基于给定上下文提出质疑方向\n"
            "3. 必须返回 JSON 数组格式:[\"问题1\", \"问题2\", \"问题3\"]\n"
            "4. 问题应聚焦:主营业务匹配度、收入占比、供应关系真实性、替代品/竞品\n"
            "5. 问题应是可搜索验证的具体问题,不是泛泛质疑"
        )
        user_prompt = (
            f"神经元名称: {neuron.canonical_name}\n"
            f"节点类型: {neuron.node_type}\n"
            f"根主题: {neuron.root_theme}\n"
            f"已有 support evidence:\n{evidence_summary}\n\n"
            f"请生成反证问题:"
        )

        try:
            content = call_llm_with_budget(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                source="neuron.counter_evidence_question",
                providers=self.llm_providers,
                temperature=0.2,
                max_completion_tokens=512,
                response_format_json=True,
                timeout=30,
                allow_no_provider=True,
                tier="pro",
            )
            if not content:
                return []
            from core.research_graph.json_parse_utils import (
                ParseFailure, robust_json_parse,
            )
            questions = robust_json_parse(content, "counter_questions")
            if isinstance(questions, ParseFailure):
                return []
            if isinstance(questions, list):
                return [str(q).strip() for q in questions if str(q).strip()][:3]
            return []
        except Exception:
            return []

    def _handle_growth_mining(self, pulse: Any) -> dict[str, Any]:
        """执行 Growth Mining 闭环(spec 6.4.1)。

        通过 growth_mining_engine_factory 注入真实 engine(GrowthMiningEngine(request).run())
        或 mock factory,消除"engine 是可重用实例"的错误假设。

        Args:
            pulse: REQUEST_GROWTH_MINING 类型的 GrowthPulse。

        Returns:
            结果 dict,含 consumed/reason/run_status/support_evidence_count。
        """
        from core.research_graph.growth_mining import GrowthMiningRequest

        if self.growth_mining_engine_factory is None:
            return {"consumed": False, "reason": "no_growth_mining_engine_factory"}

        # 阶段 2:注入 use_system_search 开关;介入点 1:llm_evidence_extraction_enabled
        request = GrowthMiningRequest.from_pulse(
            pulse,
            use_system_search=self.enable_system_search,
            allow_llm_assist=self.llm_evidence_extraction_enabled,
        )
        try:
            result = self.growth_mining_engine_factory(request)
        except Exception as exc:
            return {"consumed": False, "reason": f"engine_failed: {exc}"}

        emitter_neuron = self.state_store.get_neuron(pulse.emitter_neuron_id)
        ingest_stats: dict[str, Any] = {
            "routed_evidence_ids": [],
            "novelty_skipped": 0,
            "ledger_rejected": 0,
            "total_attempted": 0,
        }
        if emitter_neuron is not None and result.support_evidence_count > 0:
            ingest_stats = self._ingest_growth_run_evidence(
                result,
                emitter_neuron=emitter_neuron,
                stance="support",
            )

        return {
            "consumed": True,
            "reason": "growth_mining_executed",
            "run_status": result.run_status,
            "support_evidence_count": result.support_evidence_count,
            "routed_evidence_ids": ingest_stats["routed_evidence_ids"],
            "novelty_skipped": ingest_stats["novelty_skipped"],
            "ledger_rejected": ingest_stats["ledger_rejected"],
            "total_attempted": ingest_stats["total_attempted"],
            "run_dir": str(getattr(result, "run_dir", "")),
        }

    def _ingest_growth_run_evidence(
        self,
        result: Any,
        *,
        emitter_neuron: Any,
        stance: str,
    ) -> dict[str, Any]:
        """Convert growth mining artifacts to canonical Evidence and route them.

        No event is dispatched when there is no EvidenceLedger, because a growth
        artifact is only staging data until it becomes canonical evidence.

        Returns:
            dict 含 routed_evidence_ids / novelty_skipped / ledger_rejected /
            total_attempted。
        """
        if self.evidence_ledger is None:
            return {"routed_evidence_ids": [], "novelty_skipped": 0, "ledger_rejected": 0, "total_attempted": 0}

        import json
        from pathlib import Path

        from core.research_graph.models import Evidence
        from core.research_graph.schema import EvidenceLevel

        run_dir = Path(getattr(result, "run_dir", ""))
        artifact_name = "counter_evidence.json" if stance == "counter" else "raw_evidence.json"
        artifact_path = run_dir / artifact_name
        if not artifact_path.exists():
            return {"routed_evidence_ids": [], "novelty_skipped": 0, "ledger_rejected": 0, "total_attempted": 0}
        try:
            rows = json.loads(artifact_path.read_text(encoding="utf-8"))
        except Exception:
            return {"routed_evidence_ids": [], "novelty_skipped": 0, "ledger_rejected": 0, "total_attempted": 0}
        if not isinstance(rows, list):
            return {"routed_evidence_ids": [], "novelty_skipped": 0, "ledger_rejected": 0, "total_attempted": 0}

        routed: list[str] = []
        novelty_skipped = 0
        ledger_rejected = 0
        total_attempted = 0
        node_id = getattr(emitter_neuron, "node_id", "") or getattr(emitter_neuron, "neuron_id", "")
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_stance = str(row.get("stance", "support"))
            if stance == "counter" and row_stance != "counter":
                continue
            if stance == "support" and row_stance == "counter":
                continue
            total_attempted += 1
            strength = str(row.get("evidence_strength", "")).upper()
            evidence_level = (
                EvidenceLevel.LIKELY.value
                if strength in {"STRONG", "STRUCTURED", "PROVEN", "LIKELY"}
                else EvidenceLevel.SPECULATIVE.value
            )
            source_type = str(
                row.get("provider_type")
                or row.get("source_type")
                or row.get("evidence_kind")
                or "growth_mining"
            )
            row_extracted_by = str(row.get("extracted_by", "growth_mining"))
            is_llm_extracted = row.get("llm_extracted") is True or "llm" in row_extracted_by.lower()
            metadata = {
                "run_dir": str(run_dir),
                "raw_document_path": row.get("raw_document_path", ""),
                "normalized_document_path": row.get("normalized_document_path", ""),
                "path_hint": row.get("path_hint", []),
                "stance": stance,
            }
            if is_llm_extracted:
                metadata.update(
                    {
                        "llm_extracted": row.get("llm_extracted"),
                        "llm_confidence": row.get("llm_confidence"),
                        "llm_relationship": row.get("llm_relationship"),
                        "llm_trace_path": row.get("llm_trace_path", ""),
                        "extraction_trace_path": row.get("extraction_trace_path", ""),
                    }
                )
            evidence = Evidence(
                evidence_id=str(row.get("evidence_id", "")),
                source_type=source_type,
                source_title=str(row.get("provider_name", "")) or str(row.get("source_id", "")),
                source_id=str(row.get("source_id", "")),
                document_id=str(row.get("document_id", "")),
                quote=str(row.get("quote", "")),
                summary=str(row.get("extraction_reason", "")),
                evidence_level=evidence_level,
                supports=[node_id] if stance == "support" else [],
                contradicts=[node_id] if stance == "counter" else [],
                extracted_by="llm_extractor" if is_llm_extracted else row_extracted_by,
                verified_by="growth_artifact_validator",
                metadata=metadata,
                # Step 5:承载 Step 2 政策事件字段(growth_mining 已写入 row)。
                event_date=str(row.get("event_date", "")),
                causal_status=str(row.get("causal_status", "")),
            )
            # Step 5:写入前做新颖度门控,跨运行重复证据直接跳过。
            gate = self._ensure_novelty_gate()
            decision = gate.evaluate(evidence)
            if not decision.is_novel:
                self._novelty_skipped_count += 1
                novelty_skipped += 1
                continue
            try:
                self.evidence_ledger.add(evidence, operator="growth_mining")
            except ValueError:
                ledger_rejected += 1
                continue
            gate.register(evidence)
            self.route_evidence(evidence, stance=stance)
            routed.append(evidence.evidence_id)
        return {
            "routed_evidence_ids": routed,
            "novelty_skipped": novelty_skipped,
            "ledger_rejected": ledger_rejected,
            "total_attempted": total_attempted,
        }

    def event_loop_tick(self) -> dict[str, Any]:
        """event_loop 单次 tick(spec 6.2)。

        按预算处理 active_neuron_queue 中的神经元,执行 growth_cycle,
        持久化 emitted pulse 并尝试 consume。任何神经元异常都跳过不中断 tick。

        Returns:
            摘要 dict,含 neurons_processed/pulses_emitted/pulses_consumed/tick_count/snapshot_id。
        """
        from core.research_graph.models import utc_now
        from core.research_graph.neuron.enums import PulseStatus

        self._tick_count += 1
        snapshot_id = f"snap_{self._tick_count}"
        current_time = utc_now()

        # Bug 4 修复:非白名单脉冲自动 EXPIRE,避免永久 EMITTED 累积。
        # EXPAND_UPSTREAM_MATERIAL / EXPAND_SYNTHESIS_CHILDREN 等脉冲不在 PULSE_WHITELIST,
        # 永远不会被 consume_pulse 处理,会导致状态表累积大量 EMITTED 记录。
        # 修复策略:tick 开始时扫描所有 EMITTED pulse,非白名单的直接标记为 EXPIRED。
        try:
            emitted_pulses = self.state_store.get_emitted_pulses(limit=1000)
            for p in emitted_pulses:
                if p.pulse_type not in self.PULSE_WHITELIST:
                    p.status = PulseStatus.EXPIRED.value
                    self.state_store.save_pulse(p)
        except Exception:
            pass

        max_n = self.budget_config.max_active_neurons_per_tick
        active_neurons = []
        for neuron_id in sorted(self._active_neuron_ids):
            neuron = self.state_store.get_neuron(neuron_id)
            if neuron is not None and not neuron.is_tombstone:
                active_neurons.append(neuron)
        scheduled = self._tick_scheduler.select(active_neurons, limit=max_n)
        pending = [item.neuron_id for item in scheduled]
        neurons_processed = 0
        pulses_emitted_total = 0
        pulses_consumed_total = 0
        mutations_this_tick = 0
        processed_source_ids: list[str] = []
        scheduler_feedback: list[dict[str, Any]] = []
        # P1c:tick 级累积统计,用于判断系统是否"学过就少学"。
        tick_novelty_skipped = 0
        tick_ledger_rejected = 0
        tick_routed_evidence = 0
        tick_total_attempted = 0

        # Consume already-persisted environment pulses before running neuron growth.
        # These pulses may be injected by upstream schedulers or tests, so limiting
        # consumption to result.emitted_pulses would leave valid work stuck forever.
        try:
            persisted_pulses = [
                p for p in self.state_store.get_emitted_pulses(limit=1000)
                if p.pulse_type in self.PULSE_WHITELIST
            ]
        except Exception:
            persisted_pulses = []
        for pulse in persisted_pulses:
            if pulses_consumed_total >= self.budget_config.max_total_pulses_per_tick:
                break
            consume_result = self.consume_pulse(pulse)
            if consume_result.get("consumed"):
                pulses_consumed_total += 1
                mutations_this_tick += 1
                routed_ids = consume_result.get("routed_evidence_ids") or []
                novelty_skipped = int(consume_result.get("novelty_skipped", 0))
                ledger_rejected = int(consume_result.get("ledger_rejected", 0))
                total_attempted = int(consume_result.get("total_attempted", 0))
                if routed_ids or novelty_skipped or ledger_rejected or total_attempted:
                    tick_novelty_skipped += novelty_skipped
                    tick_ledger_rejected += ledger_rejected
                    tick_routed_evidence += len(routed_ids)
                    tick_total_attempted += total_attempted
                    self._tick_scheduler.record_feedback(
                        selected_neuron_id=pulse.emitter_neuron_id,
                        allocated_budget=0,
                        new_evidence_count=total_attempted,
                        accepted_evidence_count=len(routed_ids),
                        rejected_evidence_count=novelty_skipped + ledger_rejected,
                        token_cost=float(consume_result.get("token_cost", 0.0)),
                    )

        for neuron_id in pending:
            self._active_neuron_ids.discard(neuron_id)
            neuron = self.state_store.get_neuron(neuron_id)
            if neuron is None or neuron.is_tombstone:
                continue
            confidence_before = float(getattr(neuron, "confidence", 0.0) or 0.0)
            evidence_before = set(self._neuron_evidence_map.get(neuron_id, []))

            context = self._build_neuron_context(neuron_id, snapshot_id)
            if context is None:
                continue
            context.current_time = current_time

            try:
                result = neuron.growth_cycle(context)
            except Exception:
                continue

            neurons_processed += 1
            processed_source_ids.append(neuron_id)
            self.state_store.save_neuron(neuron)
            confidence_after = float(getattr(neuron, "confidence", 0.0) or 0.0)
            evidence_after = set(self._neuron_evidence_map.get(neuron_id, []))
            scheduler_feedback.append(
                self._tick_scheduler.record_feedback(
                    selected_neuron_id=neuron_id,
                    allocated_budget=self.budget_config.max_pulses_per_neuron,
                    new_evidence_count=len(evidence_after - evidence_before),
                    accepted_evidence_count=len(evidence_after - evidence_before),
                    rejected_evidence_count=0,
                    confidence_delta=confidence_after - confidence_before,
                )
            )

            # Plan 6 修复一:应用 synapse_updates 到 state_store 并持久化
            for update in result.synapse_updates:
                syn = self.state_store.get_synapse(update.synapse_id)
                if syn is None:
                    continue
                if update.new_status is not None:
                    syn.status = update.new_status
                if update.new_strength is not None:
                    syn.strength = update.new_strength
                if update.new_inhibition is not None:
                    syn.inhibition = update.new_inhibition
                if update.new_confidence is not None:
                    syn.confidence = update.new_confidence
                if update.new_attention_required is not None:
                    syn.attention_required = update.new_attention_required
                if update.added_counter_evidence:
                    for eid in update.added_counter_evidence:
                        if eid not in syn.counter_evidence_ids:
                            syn.counter_evidence_ids.append(eid)
                if update.added_support_evidence:
                    for eid in update.added_support_evidence:
                        if eid not in syn.support_evidence_ids:
                            syn.support_evidence_ids.append(eid)
                self.state_store.save_synapse(syn)
                mutations_this_tick += 1

            for pulse in result.emitted_pulses:
                if pulses_emitted_total >= self.budget_config.max_total_pulses_per_tick:
                    break
                self.state_store.save_pulse(pulse)
                pulses_emitted_total += 1
                mutations_this_tick += 1

            for pulse in result.emitted_pulses:
                if pulses_consumed_total >= self.budget_config.max_total_pulses_per_tick:
                    break
                consume_result = self.consume_pulse(pulse)
                if consume_result.get("consumed"):
                    pulses_consumed_total += 1
                    # P1b:把 pulse consume 结果回灌 scheduler feedback。
                    # 只对携带 evidence 统计的 consume_result 调用,避免对 EXPAND_*/MERGE_ALIAS
                    # 等纯拓扑脉冲重复 record_feedback(growth_cycle 已调用一次)。
                    routed_ids = consume_result.get("routed_evidence_ids") or []
                    novelty_skipped = int(consume_result.get("novelty_skipped", 0))
                    ledger_rejected = int(consume_result.get("ledger_rejected", 0))
                    total_attempted = int(consume_result.get("total_attempted", 0))
                    if routed_ids or novelty_skipped or ledger_rejected or total_attempted:
                        tick_novelty_skipped += novelty_skipped
                        tick_ledger_rejected += ledger_rejected
                        tick_routed_evidence += len(routed_ids)
                        tick_total_attempted += total_attempted
                        self._tick_scheduler.record_feedback(
                            selected_neuron_id=pulse.emitter_neuron_id,
                            allocated_budget=0,
                            new_evidence_count=total_attempted,
                            accepted_evidence_count=len(routed_ids),
                            rejected_evidence_count=novelty_skipped + ledger_rejected,
                            token_cost=float(consume_result.get("token_cost", 0.0)),
                        )

        if mutations_this_tick > 0:
            self._last_mutation_tick = self._tick_count

        # 阶段 1:假设-证据覆盖矩阵 + 置信度校准
        gap_report = self._evaluate_confidence_and_gaps(current_time)

        # 阶段 5:跨主题/跨行业联动(在 confidence 更新后执行)
        cross_theme_report = self._propagate_cross_theme(processed_source_ids)

        # 差距 6:跨主题意外发现,触发新主题冷启动
        surprise_report = self._mine_cross_theme_surprises(processed_source_ids)

        # 阶段 4:边际变化跟踪
        delta_report = self._track_delta_changes()

        # P0 Sprint:若 regime_detector 已注入,输出 regime_report 标志。
        # 完整实现需从外部数据源读取收益率序列,本轮简化为"检测器已注入"摘要。
        regime_report: dict[str, Any] = {}
        if self.regime_detector is not None:
            try:
                regime_report = {
                    "method": "available",
                    "detector_injected": True,
                    "n_states": getattr(self.regime_detector, "n_states", None),
                    "is_hmm_available": getattr(
                        self.regime_detector, "is_hmm_available", lambda: False
                    )(),
                }
            except Exception:
                regime_report = {}

        return {
            "neurons_processed": neurons_processed,
            "pulses_emitted": pulses_emitted_total,
            "pulses_consumed": pulses_consumed_total,
            "tick_count": self._tick_count,
            "snapshot_id": snapshot_id,
            "gap_report": gap_report,
            "cross_theme_report": cross_theme_report,
            "surprise_report": surprise_report,
            "delta_report": delta_report,
            "regime_report": regime_report,
            # P1c:evidence 入账统计,让外部能判断"学过就少学"还是"没搜到东西"。
            "evidence_stats": {
                "routed": tick_routed_evidence,
                "novelty_skipped": tick_novelty_skipped,
                "ledger_rejected": tick_ledger_rejected,
                "total_attempted": tick_total_attempted,
                "cumulative_novelty_skipped": self._novelty_skipped_count,
            },
            "scheduler_report": {
                "candidate_count": len(active_neurons),
                "selected": [item.to_dict() for item in scheduled],
                "feedback": scheduler_feedback,
            },
        }

    def _track_delta_changes(self) -> dict[str, Any]:
        """阶段 4:跟踪置信度与证据集合的边际变化。

        Returns:
            DeltaReport dict。
        """
        import uuid

        from core.research_graph.neuron.models import GrowthPulse

        neurons = self._load_all_neurons()
        evidence_map: dict[str, list[Any]] = {}
        for neuron in neurons:
            ev_ids = self._neuron_evidence_map.get(neuron.neuron_id, [])
            evidence_map[neuron.neuron_id] = [
                self._evidence_pool[eid] for eid in ev_ids if eid in self._evidence_pool
            ]

        report = self._delta_tracker.update(self._tick_count, neurons, evidence_map)

        # 对显著变化生成人工审查 pulse
        for nid in report.review_triggered:
            neuron = next((n for n in neurons if n.neuron_id == nid), None)
            if neuron is None:
                continue
            delta = next(
                (d for d in report.significant_deltas if d.neuron_id == nid), None
            )
            reason = f"confidence delta {delta.confidence_delta:+}"
            if delta and delta.bucket_before != delta.bucket_after:
                reason += f", bucket {delta.bucket_before}->{delta.bucket_after}"
            pulse = GrowthPulse(
                pulse_id=f"pulse_review_{uuid.uuid4().hex[:8]}",
                emitter_neuron_id=nid,
                pulse_type="REQUEST_HUMAN_REVIEW",
                root_theme=neuron.root_theme,
                reason=reason,
                priority=80,
                suggested_queries=delta.new_evidence_ids if delta else [],
                payload={
                    "neuron_id": nid,
                    "confidence_delta": delta.confidence_delta if delta else 0.0,
                    "bucket_before": delta.bucket_before if delta else "",
                    "bucket_after": delta.bucket_after if delta else "",
                },
            )
            self.state_store.save_pulse(pulse)

        return report.to_dict()

    def _load_all_neurons(self) -> list[Any]:
        """加载 state_store 中所有非 tombstone 神经元。"""
        import json

        from core.research_graph.neuron.models import ResearchNeuron

        try:
            table = self.state_store.db.open_table("neurons")
            rows = table.to_pandas().to_dict("records")
            neurons: list[Any] = []
            for row in rows:
                payload = json.loads(row["extra_payload"])
                neuron = ResearchNeuron.from_dict(payload)
                if not neuron.is_tombstone:
                    neurons.append(neuron)
            return neurons
        except Exception:
            return []

    def export_learning_observability(self, output_dir: str | Any) -> dict[str, Any]:
        """显式导出学习可观测化 artifacts,不改变 tick 默认行为。"""
        from pathlib import Path

        from core.research_graph.learning_observability import (
            LearningObservabilityReporter,
        )

        neurons = self._load_all_neurons()
        reporter = LearningObservabilityReporter(
            graph_store=self.graph_store,
            neurons=neurons,
            tick_count=self._tick_count,
        )
        snapshot = reporter.build_snapshot()
        artifacts = reporter.write_artifacts(output_dir)
        return {
            "output_dir": str(output_dir),
            "artifacts": {key: str(path) for key, path in artifacts.items()},
            "history_path": str(Path(output_dir) / "learning_curve_history.jsonl"),
            "learning_metrics": snapshot["learning_metrics"],
            "breakthrough_report": snapshot["breakthrough_report"],
        }

    def _evaluate_confidence_and_gaps(self, current_time: str) -> dict[str, Any]:
        """阶段 1:扫描所有神经元,更新置信度并生成搜索缺口 pulse。

        Args:
            current_time: 当前时间 ISO 字符串。

        Returns:
            摘要 dict,含 neurons_evaluated/gaps_found/pulses_emitted。
        """
        import uuid

        from core.research_graph.neuron.models import GrowthPulse

        neurons = self._load_all_neurons()
        evidence_map: dict[str, list[Any]] = {}
        for neuron in neurons:
            ev_ids = self._neuron_evidence_map.get(neuron.neuron_id, [])
            evidence_map[neuron.neuron_id] = [
                self._evidence_pool[eid] for eid in ev_ids if eid in self._evidence_pool
            ]

        # 更新每个神经元的置信度
        for neuron in neurons:
            score, _ = self._gap_analyzer.analyze_neuron(
                neuron, evidence_map.get(neuron.neuron_id, []), current_time
            )
            neuron.confidence = score.raw_score
            neuron.uncertainty = 1.0 - score.raw_score
            neuron.evidence_gap = 1.0 - score.raw_score
            if not neuron.source_node_ref:
                neuron.source_node_ref = {}
            neuron.source_node_ref["confidence_bucket"] = score.bucket
            neuron.source_node_ref["uncertainty_sources"] = score.uncertainty_sources
            self.state_store.save_neuron(neuron)

        # 生成 top gaps 对应的搜索 pulse
        top_gaps = self._gap_analyzer.find_top_gaps(
            neurons, evidence_map, top_k=5, now_iso=current_time
        )
        neuron_by_id = {n.neuron_id: n for n in neurons}
        emitted = 0
        for gap in top_gaps:
            if gap.severity < 0.5:
                continue
            target_neuron = neuron_by_id.get(gap.target_id)
            pulse = GrowthPulse(
                pulse_id=f"pulse_gap_{uuid.uuid4().hex[:8]}",
                emitter_neuron_id=gap.target_id,
                pulse_type="REQUEST_GROWTH_MINING",
                root_theme=target_neuron.root_theme if target_neuron else "",
                reason=f"[{gap.dimension}] {gap.reason}",
                priority=int(50 + gap.severity * 40),
                suggested_queries=[gap.suggested_query],
                payload={
                    "topic": gap.suggested_query,
                    "node": gap.target_id,
                    "gap_dimension": gap.dimension,
                    "expected_gain": gap.expected_gain,
                    "allow_llm_assist": self.llm_evidence_extraction_enabled,
                    "use_system_search": self.enable_system_search,
                    # Step 7:附带 hypothesis,供后续检索做证据性重排。
                    "hypothesis": gap.suggested_query,
                    "stance": "support",
                },
            )
            self.state_store.save_pulse(pulse)
            emitted += 1

        return {
            "neurons_evaluated": len(neurons),
            "gaps_found": len(top_gaps),
            "pulses_emitted": emitted,
        }

    def _propagate_cross_theme(self, source_neuron_ids: list[str]) -> dict[str, Any]:
        """阶段 5:基于主题关联图传播事件,激活相邻主题的低置信度神经元。

        Args:
            source_neuron_ids: 本次 tick 中处理过的神经元 ID 列表。

        Returns:
            传播报告 dict,含 activated_neuron_count/emitted_pulse_count/links_used。
        """
        neurons = self._load_all_neurons()
        synapses = self.state_store.get_all_synapses()
        result = self._cross_theme_propagation.propagate(
            source_neuron_ids, neurons, synapses
        )
        for neuron in result.activated_neurons:
            self._activate_neuron(neuron.neuron_id)
            self.state_store.save_neuron(neuron)
        for pulse in result.emitted_pulses:
            self.state_store.save_pulse(pulse)
        return result.to_dict()

    def _mine_cross_theme_surprises(
        self,
        source_neuron_ids: list[str],
        max_pulses: int = 3,
    ) -> dict[str, Any]:
        """差距 6:跨主题意外发现,从源神经元挖掘未研究主题并生成冷启动脉冲。

        对高置信度源神经元,利用本地实体词表和桥接公司发现意想不到的跨主题连接,
        生成 REQUEST_GROWTH_MINING pulse 驱动新主题冷启动。

        Args:
            source_neuron_ids: 本次 tick 中处理过的神经元 ID 列表。
            max_pulses: 每 tick 最多生成的冷启动脉冲数。

        Returns:
            意外发现报告 dict,含 candidate_count/emitted_pulse_count/candidates。
        """
        import uuid

        from core.research_graph.neuron.models import GrowthPulse

        neurons = self._load_all_neurons()
        synapses = self.state_store.get_all_synapses()
        known_themes = {
            getattr(n, "root_theme", "") for n in neurons if getattr(n, "root_theme", "")
        }

        candidates = self._cross_theme_surprise_miner.mine(
            source_neuron_ids=source_neuron_ids,
            neurons=neurons,
            synapses=synapses,
            known_themes=known_themes,
        )

        emitted: list[Any] = []
        for candidate in candidates[:max_pulses]:
            pulse = GrowthPulse(
                pulse_id=f"pulse_surprise_{uuid.uuid4().hex[:8]}",
                emitter_neuron_id=candidate.source_neuron_id,
                pulse_type="REQUEST_GROWTH_MINING",
                root_theme=candidate.suggested_theme,
                reason=f"跨主题意外发现:{candidate.reason}",
                priority=int(50 + candidate.strength * 30),
                suggested_queries=list(candidate.suggested_queries),
                payload={
                    "topic": candidate.suggested_theme,
                    "seed_node": candidate.suggested_seed_node,
                    "cross_theme_source": candidate.source_theme,
                    "link_type": candidate.link_type,
                    "strength": round(candidate.strength, 4),
                    "cold_start": True,
                },
            )
            self.state_store.save_pulse(pulse)
            emitted.append(pulse)

        return {
            "candidate_count": len(candidates),
            "emitted_pulse_count": len(emitted),
            "candidates": [c.to_dict() for c in candidates[:max_pulses]],
        }

    def _passes_evidence_gate(self, pulse: Any) -> bool:
        """evidence gate:无证据的 pulse 不通过(spec 6.5)。

        Args:
            pulse: GrowthPulse 实例。

        Returns:
            payload 中含非空 evidence_ids 返回 True,否则 False。
        """
        evidence_ids = (pulse.payload or {}).get("evidence_ids", [])
        return len(evidence_ids) > 0

    def tree_projection(
        self, root_neuron_id: str, dimension: str | None = None, max_depth: int = 3
    ) -> dict[str, Any]:
        """从指定神经元出发,按维度投影为树结构(spec 10)。

        Args:
            root_neuron_id: 根神经元 ID。
            dimension: 可选维度过滤,仅投影该维度的突触。
            max_depth: 最大递归深度。

        Returns:
            树形 dict,根不存在返回空 dict。
        """
        root = self.state_store.get_neuron(root_neuron_id)
        if root is None:
            return {}

        visited: set[str] = set()

        def _build(neuron_id: str, depth: int) -> dict[str, Any]:
            if neuron_id in visited or depth > max_depth:
                return {"neuron_id": neuron_id, "children": [], "truncated": True}
            visited.add(neuron_id)
            neuron = self.state_store.get_neuron(neuron_id)
            if neuron is None:
                return {"neuron_id": neuron_id, "children": []}
            children: list[dict[str, Any]] = []
            if depth < max_depth:
                synapses = self.state_store.get_synapses_by_neuron(neuron_id)
                for syn in synapses:
                    if syn.source_neuron_id != neuron_id:
                        continue
                    if dimension and syn.dimension != dimension:
                        continue
                    if syn.status == "PRUNED":
                        continue
                    if syn.target_kind == "EXISTING_NEURON" and syn.target_neuron_id:
                        children.append(_build(syn.target_neuron_id, depth + 1))
                    else:
                        children.append({
                            "neuron_id": "",
                            "target_kind": syn.target_kind,
                            "direction": syn.direction,
                            "is_leaf": True,
                        })
            return {
                "neuron_id": neuron_id,
                "canonical_name": neuron.canonical_name,
                "verification_status": neuron.verification_status,
                "children": children,
            }

        return _build(root_neuron_id, 0)

    def check_termination(self, convergence_threshold: int = 10) -> dict[str, Any]:
        """检查终止条件(spec 11)。

        优先级:convergence(连续 N tick 无 mutation)> queue_empty(队列空且无 pulse)。
        convergence 优先是因为即使队列暂时空,若已收敛也应终止而非等待。

        Args:
            convergence_threshold: 连续无 mutation 的 tick 阈值。

        Returns:
            dict 含 terminated/reason。terminated=True 表示应停止 event_loop。
        """
        if self._tick_count - self._last_mutation_tick >= convergence_threshold:
            return {
                "terminated": True,
                "reason": f"converged:{convergence_threshold}_ticks_no_mutation",
            }

        if not self._active_neuron_ids:
            emitted_pulses = self.state_store.get_emitted_pulses(limit=1)
            if not emitted_pulses:
                return {"terminated": True, "reason": "queue_empty_and_no_pulses"}

        return {"terminated": False, "reason": "active"}

    def manual_add_synapse(
        self,
        source_neuron_id: str,
        target_neuron_id: str | None,
        direction: str,
        relation: str,
        dimension: str,
        target_draft: Any = None,
        operator: str = "human",
    ) -> str:
        """用户手动添加 synapse(spec 12)。无证据 → CONFLICTED + attention_required。

        Args:
            source_neuron_id: 源神经元 ID。
            target_neuron_id: 目标神经元 ID(EXISTING_NEURON 时必填)。
            direction: 突触方向(SynapseDirection 枚举值)。
            relation: 突触关系(SynapseRelation 枚举值)。
            dimension: 维度。
            target_draft: TARGET_DRAFT 时的候选目标描述。
            operator: 操作者标识,默认 "human"。

        Returns:
            新建的 synapse_id。
        """
        import uuid
        from core.research_graph.neuron.enums import SynapseStatus, SynapseTargetKind
        from core.research_graph.neuron.models import MutationEntry, ResearchSynapse

        synapse_id = f"syn_manual_{uuid.uuid4().hex[:12]}"
        target_kind = (SynapseTargetKind.TARGET_DRAFT.value if target_draft is not None
                       else SynapseTargetKind.EXISTING_NEURON.value)
        syn = ResearchSynapse(
            synapse_id=synapse_id,
            source_neuron_id=source_neuron_id,
            target_kind=target_kind,
            target_neuron_id=target_neuron_id,
            target_draft=target_draft,
            direction=direction,
            relation=relation,
            dimension=dimension,
            status=SynapseStatus.CONFLICTED.value,
            attention_required=True,
        )
        syn.validate()
        self.state_store.save_synapse(syn)
        mutation_id = f"mut_{uuid.uuid4().hex[:12]}"
        self.state_store.save_mutation(MutationEntry(
            mutation_id=mutation_id,
            cycle_id=f"manual_{synapse_id}",
            action="MANUAL_ADD_SYNAPSE",
            target_type="synapse",
            target_id=synapse_id,
            before={},
            after={"status": syn.status, "attention_required": True},
            reason=f"manual_add_by_{operator}",
        ))
        # dispatch ManualMutationEvent 让源 neuron 在下次 growth_cycle 感知(spec 4.7)
        from core.research_graph.neuron.events import ManualMutationEvent
        self.dispatch_event(ManualMutationEvent(
            event_id=f"manual_evt_{synapse_id}",
            target_neuron_id=source_neuron_id,
            payload={
                "mutation_refs": [mutation_id],
                "manual_action": "MANUAL_ADD_SYNAPSE",
                "synapse_id": synapse_id,
            },
        ))
        return synapse_id

    def manual_remove_synapse(self, synapse_id: str, operator: str = "human") -> None:
        """用户手动删除 synapse(spec 12:PRUNED 不物理删除)。

        Args:
            synapse_id: 待删除的突触 ID。
            operator: 操作者标识,默认 "human"。

        Raises:
            ValueError: synapse_id 不存在。
        """
        import uuid
        from core.research_graph.neuron.enums import SynapseStatus
        from core.research_graph.neuron.models import MutationEntry

        syn = self.state_store.get_synapse(synapse_id)
        if syn is None:
            raise ValueError(f"synapse_not_found: {synapse_id}")
        before = {"status": syn.status}
        syn.status = SynapseStatus.PRUNED.value
        self.state_store.save_synapse(syn)
        mutation_id = f"mut_{uuid.uuid4().hex[:12]}"
        self.state_store.save_mutation(MutationEntry(
            mutation_id=mutation_id,
            cycle_id=f"manual_remove_{synapse_id}",
            action="MANUAL_REMOVE_SYNAPSE",
            target_type="synapse",
            target_id=synapse_id,
            before=before,
            after={"status": SynapseStatus.PRUNED.value},
            reason=f"manual_remove_by_{operator}",
        ))
        # dispatch ManualMutationEvent 让源 neuron 在下次 growth_cycle 感知(spec 4.7)
        from core.research_graph.neuron.events import ManualMutationEvent
        self.dispatch_event(ManualMutationEvent(
            event_id=f"manual_remove_evt_{synapse_id}",
            target_neuron_id=syn.source_neuron_id,
            payload={
                "mutation_refs": [mutation_id],
                "manual_action": "MANUAL_REMOVE_SYNAPSE",
                "synapse_id": synapse_id,
            },
        ))

    # ------------------------------------------------------------------
    # 修复缺口2: cold_start 后从 GraphStore 重建证据池
    # cold_start 只创建神经元，不重建 _evidence_pool/_neuron_evidence_map。
    # 重启后即使 GraphStore.evidence 有全部历史证据，内存池仍为空，
    # 导致 EvidenceLedgerReaderAdapter 查询为空、tick 拿不到证据。
    # 此方法遍历 GraphStore.evidence 幂等补充证据池，不依赖 checkpoint。
    # ------------------------------------------------------------------

    def _rebuild_evidence_pool_from_graph_store(self) -> int:
        """从 GraphStore.evidence 幂等重建证据池（缺口2 修复）。

        cold_start 创建神经元后调用，遍历 GraphStore 中所有证据，
        对尚未在 _evidence_pool 中的证据：
        - 加入 _evidence_pool
        - 通过 evidence_index.lookup 路由到已索引神经元，填充 _neuron_evidence_map

        不设置 _evidence_stance（由 import_evidence_routing_state 从 checkpoint
        恢复，或 EvidenceLedgerReaderAdapter 默认 "support" 处理），避免覆盖
        checkpoint 的精确 stance。不 dispatch_event，避免 cold_start 阶段
        触发神经元事件风暴。已存在的条目跳过，保证幂等。

        Returns:
            新补充的证据数量。
        """
        gs = self.graph_store
        if gs is None:
            return 0
        added = 0
        for evidence_id, evidence in gs.evidence.items():
            if evidence_id in self._evidence_pool:
                continue
            self._evidence_pool[evidence_id] = evidence
            hit_ids = self.evidence_index.lookup(evidence)
            for nid in hit_ids:
                eid_list = self._neuron_evidence_map.setdefault(nid, [])
                if evidence_id not in eid_list:
                    eid_list.append(evidence_id)
            added += 1
        return added

    # ------------------------------------------------------------------
    # 修复 3：environment 内存池 checkpoint 导出/导入
    # _evidence_pool / _evidence_stance / _neuron_evidence_map 三个字典
    # 在 cold_start 时不重建，重启后 EvidenceLedgerReaderAdapter 查询不到
    # 任何证据。此处提供 export/import，由 EnginePersistence 调用。
    # _evidence_pool 不序列化 Evidence 对象本身（GraphStore 已经持久化），
    # 仅导出 evidence_id 列表 + stance + neuron→evidence_ids 映射，
    # import 时从 GraphStore.evidence 取回 Evidence 对象。
    # ------------------------------------------------------------------

    def export_evidence_routing_state(self) -> dict[str, Any]:
        """导出证据路由内存池状态，供 checkpoint 序列化。

        Returns:
            dict 含 evidence_ids / evidence_stance / neuron_evidence_map /
            tick_count / saved_at。evidence_ids 为已路由过的证据 ID 列表
            （顺序保留）。无证据时返回空结构。
        """
        return {
            "evidence_ids": list(self._evidence_pool.keys()),
            "evidence_stance": dict(self._evidence_stance),
            "neuron_evidence_map": {
                nid: list(eids) for nid, eids in self._neuron_evidence_map.items()
            },
            "tick_count": self._tick_count,
            "active_neuron_ids": list(self._active_neuron_ids),
        }

    def import_evidence_routing_state(
        self, state: dict[str, Any], graph_store: Any | None = None,
    ) -> dict[str, int]:
        """从 checkpoint 状态恢复证据路由内存池。

        仅恢复 state 中存在且 GraphStore 中仍存在的证据，避免引用已被清理
        的过期证据。_evidence_stance 缺失时默认 "support"。_active_neuron_ids
        仅在集合为空时合并（避免覆盖 cold_start 已激活的神经元）。

        Args:
            state: export_evidence_routing_state 返回的 dict。
            graph_store: GraphStore 实例，用于取回 Evidence 对象；
                None 时使用 self.graph_store。

        Returns:
            恢复统计 dict：evidence_restored / stance_restored /
            neuron_map_restored / active_neurons_merged。
        """
        gs = graph_store or self.graph_store
        evidence_restored = 0
        stance_restored = 0
        neuron_map_restored = 0

        evidence_ids: list[str] = list(state.get("evidence_ids", []))
        stance_map: dict[str, str] = dict(state.get("evidence_stance", {}))
        neuron_map: dict[str, list[str]] = {
            str(nid): list(eids)
            for nid, eids in state.get("neuron_evidence_map", {}).items()
        }

        # 1. 重建 _evidence_pool：仅保留 GraphStore 中仍存在的证据
        if gs is not None:
            for eid in evidence_ids:
                evidence = gs.evidence.get(eid)
                if evidence is None:
                    continue
                self._evidence_pool[eid] = evidence
                evidence_restored += 1
                # 同步 stance（缺失默认 "support"）
                if eid not in self._evidence_stance:
                    self._evidence_stance[eid] = stance_map.get(eid, "support")
                    stance_restored += 1

        # 2. 重建 _neuron_evidence_map：仅保留 _evidence_pool 中存在的证据
        valid_evidence_ids = set(self._evidence_pool.keys())
        for nid, eids in neuron_map.items():
            filtered = [eid for eid in eids if eid in valid_evidence_ids]
            if not filtered:
                continue
            existing = self._neuron_evidence_map.setdefault(nid, [])
            for eid in filtered:
                if eid not in existing:
                    existing.append(eid)
                    neuron_map_restored += 1

        # 3. 合并 _active_neuron_ids：仅在集合为空时合并，避免覆盖 cold_start
        active_neurons_merged = 0
        if not self._active_neuron_ids:
            for nid in state.get("active_neuron_ids", []):
                self._active_neuron_ids.add(nid)
                active_neurons_merged += 1

        return {
            "evidence_restored": evidence_restored,
            "stance_restored": stance_restored,
            "neuron_map_restored": neuron_map_restored,
            "active_neurons_merged": active_neurons_merged,
        }
