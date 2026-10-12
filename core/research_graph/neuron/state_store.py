"""Neuron state persistence with lancedb 4 tables.

表结构:
    neurons    — neuron_id 主键,存储 ResearchNeuron 完整状态
    synapses   — synapse_id 主键,支持 source/target neuron_id 反查
    pulses     — pulse_id 主键,支持 status/emitter 查询
    mutations  — mutation_id 主键,追加型,支持 target_id/cycle_id 查询

核心标量字段独立列便于查询;完整字段序列化为 JSON 存 extra_payload 列保证扩展性。
"""
from __future__ import annotations

import json
from typing import Any

import lancedb
import pyarrow as pa

from core.research_graph.neuron.models import (
    GrowthPulse,
    MutationEntry,
    ResearchNeuron,
    ResearchSynapse,
)


class NeuronStateStore:
    """神经元状态持久化(lancedb 4 表)。"""

    NEURONS_SCHEMA = pa.schema([
        pa.field("neuron_id", pa.string()),
        pa.field("node_id", pa.string()),
        pa.field("node_type", pa.string()),
        pa.field("canonical_name", pa.string()),
        pa.field("verification_status", pa.string()),
        pa.field("metabolism_state", pa.string()),
        pa.field("nutrition_level", pa.float32()),
        pa.field("confidence", pa.float32()),
        pa.field("uncertainty", pa.float32()),
        pa.field("staleness", pa.float32()),
        pa.field("attention_required", pa.bool_()),
        pa.field("extra_payload", pa.string()),
    ])

    SYNAPSES_SCHEMA = pa.schema([
        pa.field("synapse_id", pa.string()),
        pa.field("source_neuron_id", pa.string()),
        pa.field("target_kind", pa.string()),
        pa.field("target_neuron_id", pa.string()),
        pa.field("direction", pa.string()),
        pa.field("dimension", pa.string()),
        pa.field("relation", pa.string()),
        pa.field("strength", pa.float32()),
        pa.field("confidence", pa.float32()),
        pa.field("inhibition", pa.float32()),
        pa.field("status", pa.string()),
        pa.field("attention_required", pa.bool_()),
        pa.field("extra_payload", pa.string()),
    ])

    PULSES_SCHEMA = pa.schema([
        pa.field("pulse_id", pa.string()),
        pa.field("emitter_neuron_id", pa.string()),
        pa.field("pulse_type", pa.string()),
        pa.field("status", pa.string()),
        pa.field("priority", pa.int32()),
        pa.field("emitted_at", pa.string()),
        pa.field("expires_at", pa.string()),
        pa.field("extra_payload", pa.string()),
    ])

    MUTATIONS_SCHEMA = pa.schema([
        pa.field("mutation_id", pa.string()),
        pa.field("cycle_id", pa.string()),
        pa.field("action", pa.string()),
        pa.field("target_type", pa.string()),
        pa.field("target_id", pa.string()),
        pa.field("is_tombstone", pa.bool_()),
        pa.field("rollback_of", pa.string()),
        pa.field("created_at", pa.string()),
        pa.field("extra_payload", pa.string()),
    ])

    def __init__(self, db_path: str = "data/neuron_state.lance") -> None:
        """初始化 lancedb 连接与 4 张表。

        Args:
            db_path: lancedb 数据库路径,默认 "data/neuron_state.lance"。
        """
        self.db = lancedb.connect(db_path)
        self._ensure_table("neurons", self.NEURONS_SCHEMA)
        self._ensure_table("synapses", self.SYNAPSES_SCHEMA)
        self._ensure_table("pulses", self.PULSES_SCHEMA)
        self._ensure_table("mutations", self.MUTATIONS_SCHEMA)

    def _ensure_table(self, name: str, schema: pa.Schema) -> None:
        """确保表存在,不存在则创建空表。

        Args:
            name: 表名。
            schema: pyarrow schema。
        """
        # lancedb 0.29+ list_tables() 返回 named tuple,需取 .tables 字段
        existing = self.db.list_tables().tables
        if name not in existing:
            empty = pa.table({f.name: [] for f in schema}, schema=schema)
            self.db.create_table(name, empty)

    # ── Neuron CRUD ──

    def save_neuron(self, neuron: ResearchNeuron) -> None:
        """保存神经元(upsert by neuron_id)。

        Args:
            neuron: ResearchNeuron 实例。
        """
        record = {
            "neuron_id": neuron.neuron_id,
            "node_id": neuron.node_id,
            "node_type": neuron.node_type,
            "canonical_name": neuron.canonical_name,
            "verification_status": neuron.verification_status,
            "metabolism_state": neuron.metabolism_state,
            "nutrition_level": float(neuron.nutrition_level),
            "confidence": float(neuron.confidence),
            "uncertainty": float(neuron.uncertainty),
            "staleness": float(neuron.staleness),
            "attention_required": neuron.attention_required,
            "extra_payload": json.dumps(neuron.to_dict(), ensure_ascii=False),
        }
        self._upsert("neurons", self.NEURONS_SCHEMA, "neuron_id", record)

    def get_neuron(self, neuron_id: str) -> ResearchNeuron | None:
        """按 neuron_id 查询。

        Args:
            neuron_id: 神经元 ID。

        Returns:
            ResearchNeuron 实例,不存在返回 None。
        """
        table = self.db.open_table("neurons")
        rows = table.search().where(f"neuron_id = '{neuron_id}'").limit(1).to_list()
        if not rows:
            return None
        payload = json.loads(rows[0]["extra_payload"])
        return ResearchNeuron.from_dict(payload)

    # ── Synapse CRUD ──

    def save_synapse(self, synapse: ResearchSynapse) -> None:
        """保存突触,保存前强制 validate()。

        Args:
            synapse: ResearchSynapse 实例。

        Raises:
            ValueError: synapse.validate() 失败时抛出。
        """
        synapse.validate()
        record = {
            "synapse_id": synapse.synapse_id,
            "source_neuron_id": synapse.source_neuron_id,
            "target_kind": synapse.target_kind,
            "target_neuron_id": synapse.target_neuron_id or "",
            "direction": synapse.direction,
            "dimension": synapse.dimension,
            "relation": synapse.relation,
            "strength": float(synapse.strength),
            "confidence": float(synapse.confidence),
            "inhibition": float(synapse.inhibition),
            "status": synapse.status,
            "attention_required": synapse.attention_required,
            "extra_payload": json.dumps(synapse.to_dict(), ensure_ascii=False),
        }
        self._upsert("synapses", self.SYNAPSES_SCHEMA, "synapse_id", record)

    def get_synapse(self, synapse_id: str) -> ResearchSynapse | None:
        """按 synapse_id 查询。

        Args:
            synapse_id: 突触 ID。

        Returns:
            ResearchSynapse 实例,不存在返回 None。
        """
        table = self.db.open_table("synapses")
        rows = table.search().where(f"synapse_id = '{synapse_id}'").limit(1).to_list()
        if not rows:
            return None
        payload = json.loads(rows[0]["extra_payload"])
        return ResearchSynapse.from_dict(payload)

    def get_synapses_by_neuron(self, neuron_id: str) -> list[ResearchSynapse]:
        """按 source_neuron_id 或 target_neuron_id 反查突触。

        Args:
            neuron_id: 神经元 ID。

        Returns:
            匹配的 ResearchSynapse 列表。
        """
        table = self.db.open_table("synapses")
        rows = table.search().where(
            f"source_neuron_id = '{neuron_id}' OR target_neuron_id = '{neuron_id}'"
        ).to_list()
        return [ResearchSynapse.from_dict(json.loads(r["extra_payload"])) for r in rows]

    def get_all_synapses(self) -> list[ResearchSynapse]:
        """查询所有非 PRUNED 突触。

        Returns:
            ResearchSynapse 列表。
        """
        table = self.db.open_table("synapses")
        rows = table.search().where("status != 'PRUNED'").to_list()
        return [ResearchSynapse.from_dict(json.loads(r["extra_payload"])) for r in rows]

    # ── Pulse CRUD ──

    def save_pulse(self, pulse: GrowthPulse) -> None:
        """保存脉冲(upsert by pulse_id)。

        Args:
            pulse: GrowthPulse 实例。
        """
        record = {
            "pulse_id": pulse.pulse_id,
            "emitter_neuron_id": pulse.emitter_neuron_id,
            "pulse_type": pulse.pulse_type,
            "status": pulse.status,
            "priority": int(pulse.priority),
            "emitted_at": pulse.emitted_at,
            "expires_at": pulse.expires_at,
            "extra_payload": json.dumps(pulse.to_dict(), ensure_ascii=False),
        }
        self._upsert("pulses", self.PULSES_SCHEMA, "pulse_id", record)

    def get_pulse(self, pulse_id: str) -> GrowthPulse | None:
        """按 pulse_id 查询。

        Args:
            pulse_id: 脉冲 ID。

        Returns:
            GrowthPulse 实例,不存在返回 None。
        """
        table = self.db.open_table("pulses")
        rows = table.search().where(f"pulse_id = '{pulse_id}'").limit(1).to_list()
        if not rows:
            return None
        payload = json.loads(rows[0]["extra_payload"])
        return GrowthPulse.from_dict(payload)

    def get_emitted_pulses(self, limit: int = 100) -> list[GrowthPulse]:
        """查询所有 EMITTED 状态的 pulse,按 priority 降序。

        Args:
            limit: 最大返回数。

        Returns:
            按优先级降序排列的 GrowthPulse 列表。
        """
        table = self.db.open_table("pulses")
        rows = table.search().where("status = 'EMITTED'").limit(limit).to_list()
        pulses = [GrowthPulse.from_dict(json.loads(r["extra_payload"])) for r in rows]
        return sorted(pulses, key=lambda p: p.priority, reverse=True)

    # ── Mutation CRUD ──

    def save_mutation(self, entry: MutationEntry) -> None:
        """追加 mutation 记录(append-only,不 upsert)。

        Args:
            entry: MutationEntry 实例。
        """
        table = self.db.open_table("mutations")
        record = {
            "mutation_id": entry.mutation_id,
            "cycle_id": entry.cycle_id,
            "action": entry.action,
            "target_type": entry.target_type,
            "target_id": entry.target_id,
            "is_tombstone": entry.is_tombstone,
            "rollback_of": entry.rollback_of,
            "created_at": entry.created_at,
            "extra_payload": json.dumps(entry.to_dict(), ensure_ascii=False),
        }
        arrow_table = pa.table(
            {k: [v] for k, v in record.items()}, schema=self.MUTATIONS_SCHEMA
        )
        table.add(arrow_table)

    def get_mutation(self, mutation_id: str) -> MutationEntry | None:
        """按 mutation_id 查询单条 mutation(用于 rollback)。

        Args:
            mutation_id: mutation ID。

        Returns:
            MutationEntry 实例,不存在返回 None。
        """
        table = self.db.open_table("mutations")
        rows = table.search().where(f"mutation_id = '{mutation_id}'").limit(1).to_list()
        if not rows:
            return None
        payload = json.loads(rows[0]["extra_payload"])
        return MutationEntry.from_dict(payload)

    def get_mutations_by_target(self, target_id: str) -> list[MutationEntry]:
        """按 target_id 查询 mutation 历史(含 tombstone)。

        Args:
            target_id: 目标对象 ID。

        Returns:
            匹配的 MutationEntry 列表。
        """
        table = self.db.open_table("mutations")
        rows = table.search().where(f"target_id = '{target_id}'").to_list()
        return [MutationEntry.from_dict(json.loads(r["extra_payload"])) for r in rows]

    # ── 内部 upsert 工具 ──

    def _upsert(
        self,
        table_name: str,
        schema: pa.Schema,
        id_field: str,
        record: dict[str, Any],
    ) -> None:
        """upsert:先删旧记录再追加新记录(lancedb 无原生 upsert)。

        Args:
            table_name: 表名。
            schema: pyarrow schema。
            id_field: 主键字段名。
            record: 记录 dict。
        """
        table = self.db.open_table(table_name)
        record_id = record[id_field]
        try:
            table.delete(f"{id_field} = '{record_id}'")
        except Exception:
            pass  # 首次插入无旧记录
        arrow_table = pa.table({k: [v] for k, v in record.items()}, schema=schema)
        table.add(arrow_table)
