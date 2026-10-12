"""[DEPRECATED] 中央 GrowthAgent —— 已被 neuron growth_cycle + GrowthMiningEngine 取代。

本模块仅因 test_fifth_battle_growth_agent_finds_daily_completion_tasks 保留,
Plan 4 集成阶段会用新 neuron/lifecycle 机制重建等价覆盖后物理删除此文件。
新代码不应 import GrowthAgent/GrowthTask,改用 GrowthMiningRequest.from_pulse()。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.research_graph.schema import EdgeStatus, EvidenceLevel, NodeType, RelationType
from core.research_graph.store import GraphStore


@dataclass
class GrowthTask:
    task_id: str
    target_id: str
    issue_type: str
    reason: str
    suggested_action: str
    priority: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GrowthAgent:
    """Find weak graph areas that need daily completion work."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def discover_tasks(self) -> list[GrowthTask]:
        tasks: list[GrowthTask] = []
        tasks.extend(self._coarse_or_under_decomposed_nodes())
        tasks.extend(self._company_mapping_tasks())
        tasks.extend(self._edge_evidence_tasks())
        return sorted(tasks, key=lambda item: (item.priority, item.task_id))

    def _coarse_or_under_decomposed_nodes(self) -> list[GrowthTask]:
        tasks: list[GrowthTask] = []
        for node_id, node in self.store.nodes.items():
            neighbors = self._neighbor_types(node_id)
            if node.type == NodeType.THEME.value and NodeType.PROCESS.value not in neighbors:
                tasks.append(GrowthTask(
                    task_id=f"growth_{node_id}_too_coarse",
                    target_id=node_id,
                    issue_type="节点太粗",
                    reason="主题节点缺少工艺级拆解",
                    suggested_action="补充 PROCESS / MATERIAL / EQUIPMENT 层级",
                    priority=10,
                ))
            if node.type == NodeType.PROCESS.value and not (
                {NodeType.MATERIAL.value, NodeType.EQUIPMENT.value} & neighbors
            ):
                tasks.append(GrowthTask(
                    task_id=f"growth_{node_id}_missing_material_decomposition",
                    target_id=node_id,
                    issue_type="缺少材料级拆解",
                    reason="工艺节点没有挂接材料或设备节点",
                    suggested_action="补充材料、设备、工艺参数和供应商",
                    priority=20,
                ))
        return tasks

    def _company_mapping_tasks(self) -> list[GrowthTask]:
        tasks: list[GrowthTask] = []
        for node_id, node in self.store.nodes.items():
            if node.type != NodeType.COMPANY.value:
                continue
            if not node.attributes.get("ticker"):
                tasks.append(GrowthTask(
                    task_id=f"growth_{node_id}_missing_a_share_mapping",
                    target_id=node_id,
                    issue_type="缺少 A 股映射",
                    reason="公司节点缺少 ticker / 证券映射",
                    suggested_action="补充 A 股代码、上市板块和主体名称",
                    priority=30,
                ))
            has_equity_edge = any(
                node_id in edge.subjects + edge.objects
                and edge.relation in {RelationType.OWNS.value, RelationType.INVESTS_IN.value}
                for edge in self.store.edges.values()
            )
            if not has_equity_edge:
                tasks.append(GrowthTask(
                    task_id=f"growth_{node_id}_missing_equity_penetration",
                    target_id=node_id,
                    issue_type="缺少股权穿透",
                    reason="公司节点没有控股、参股或子公司路径",
                    suggested_action="补充 SUBSIDIARY / OWNS / INVESTS_IN 关系",
                    priority=35,
                ))
        return tasks

    def _edge_evidence_tasks(self) -> list[GrowthTask]:
        tasks: list[GrowthTask] = []
        for edge_id, edge in self.store.edges.items():
            if edge.status == EdgeStatus.CONFLICTED.value:
                tasks.append(GrowthTask(
                    task_id=f"growth_{edge_id}_conflicted",
                    target_id=edge_id,
                    issue_type="存在冲突证据",
                    reason="关系正反证据并存，不能进入重点候选",
                    suggested_action="触发人工复核或继续挖掘官方证据",
                    priority=5,
                ))
            if edge.confidence == EvidenceLevel.SPECULATIVE.value:
                tasks.append(GrowthTask(
                    task_id=f"growth_{edge_id}_speculative_only",
                    target_id=edge_id,
                    issue_type="只有 SPECULATIVE 证据",
                    reason="关系缺少多源交叉证据或官方证据",
                    suggested_action="挖掘年报、公告、合同、客户认证或多源研报",
                    priority=15,
                ))
            for evidence_id in edge.evidence_ids + edge.counter_evidence_ids:
                evidence = self.store.evidence.get(evidence_id)
                if evidence and evidence.freshness != "current":
                    tasks.append(GrowthTask(
                        task_id=f"growth_{evidence_id}_stale",
                        target_id=evidence_id,
                        issue_type="证据过期",
                        reason=f"证据 freshness={evidence.freshness}",
                        suggested_action="重新抓取或补充最新证据",
                        priority=25,
                    ))
        return tasks

    def _neighbor_types(self, node_id: str) -> set[str]:
        result: set[str] = set()
        for edge in self.store.edges.values():
            if node_id in edge.subjects:
                result.update(
                    self.store.nodes[target].type for target in edge.objects if target in self.store.nodes
                )
            if node_id in edge.objects:
                result.update(
                    self.store.nodes[target].type for target in edge.subjects if target in self.store.nodes
                )
        return result
