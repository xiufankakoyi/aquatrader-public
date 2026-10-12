"""神经元生命周期操作:split / merge / prune / revive / rollback。

所有函数接受 NeuronStateStore(解耦 environment),变更写 MutationEntry(append-only)。
tombstone 不物理删除;PRUNED 不可自然复活,只能通过 rollback_mutation。
"""
from __future__ import annotations

import uuid
from typing import Any

from core.research_graph.neuron.enums import (
    MetabolismState, NeuronCreatedFrom, NeuronMutationAction, VerificationStatus,
)
from core.research_graph.neuron.models import MutationEntry, ResearchNeuron
from core.research_graph.neuron.state_store import NeuronStateStore


def _new_mutation_id(prefix: str = "mut") -> str:
    """生成 mutation ID。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LifecycleManager:
    """生命周期操作管理器。

    所有方法接受 state_store 参数(Plan 4 environment 调用时传入 self.state_store),
    避免循环依赖 environment。
    """

    def split_neuron(
        self,
        state_store: NeuronStateStore,
        parent_neuron_id: str,
        split_spec: list[dict[str, Any]],
        evidence_partition: dict[str, list[str]],
        reason: str,
        cycle_id: str = "",
    ) -> list[str]:
        """分裂过粗神经元(spec 5.1)。

        Args:
            state_store: 神经元状态存储。
            parent_neuron_id: 父神经元 ID。
            split_spec: 子神经元规格列表,每项含 neuron_id/node_id/canonical_name/node_type/dimensions。
            evidence_partition: {child_neuron_id: [evidence_id]} 证据分配。
            reason: 分裂理由。
            cycle_id: 所属 cycle(写 mutation 用)。

        Returns:
            新建子神经元 ID 列表。

        Raises:
            ValueError: 父神经元不存在,或 split_spec/evidence_partition 不一致。
        """
        parent = state_store.get_neuron(parent_neuron_id)
        if parent is None:
            raise ValueError(f"parent_not_found: {parent_neuron_id}")
        spec_ids = {s["neuron_id"] for s in split_spec}
        partition_ids = set(evidence_partition.keys())
        # partition 中的 neuron_id 必须全部出现在 split_spec 中
        if partition_ids - spec_ids:
            raise ValueError(
                f"partition_mismatch: {partition_ids - spec_ids} not in split_spec"
            )

        child_ids: list[str] = []
        cycle = cycle_id or f"split_{parent_neuron_id}"
        for spec in split_spec:
            child = ResearchNeuron(
                neuron_id=spec["neuron_id"],
                node_id=spec["node_id"],
                node_type=spec["node_type"],
                canonical_name=spec["canonical_name"],
                root_theme=parent.root_theme,
                created_from=NeuronCreatedFrom.SPLIT.value,
                verification_status=VerificationStatus.UNVERIFIED_SEED.value,
                metabolism_state=MetabolismState.ACTIVE.value,
                dimensions_observed=list(spec.get("dimensions", [])),
            )
            state_store.save_neuron(child)
            child_ids.append(child.neuron_id)
            state_store.save_mutation(MutationEntry(
                mutation_id=_new_mutation_id(),
                cycle_id=cycle,
                action=NeuronMutationAction.NEURON_CREATED.value,
                target_type="neuron",
                target_id=child.neuron_id,
                before={},
                after={"created_from": NeuronCreatedFrom.SPLIT.value,
                       "parent": parent_neuron_id,
                       "evidence_ids": evidence_partition.get(child.neuron_id, [])},
                reason=reason,
                evidence_ids=list(evidence_partition.get(child.neuron_id, [])),
            ))

        # 父神经元保留,记录 NEURON_SPLIT mutation(不物理删除,可后续 rollback)
        state_store.save_mutation(MutationEntry(
            mutation_id=_new_mutation_id(),
            cycle_id=cycle,
            action=NeuronMutationAction.NEURON_SPLIT.value,
            target_type="neuron",
            target_id=parent_neuron_id,
            before={"verification_status": parent.verification_status},
            after={"split_into": child_ids},
            reason=reason,
        ))
        return child_ids

    def merge_neuron(
        self,
        state_store: NeuronStateStore,
        source_neuron_ids: list[str],
        merged_spec: dict[str, Any],
        reason: str,
        cycle_id: str = "",
    ) -> str:
        """合并高度重叠神经元(spec 5.2)。

        Args:
            state_store: 神经元状态存储。
            source_neuron_ids: 源神经元 ID 列表(至少 2 个)。
            merged_spec: 合并后神经元规格,含 neuron_id/node_id/canonical_name/node_type。
            reason: 合并理由。
            cycle_id: 所属 cycle。

        Returns:
            新建 merged 神经元 ID。

        Raises:
            ValueError: 源神经元少于 2 个,或任一源神经元不存在。
        """
        if len(source_neuron_ids) < 2:
            raise ValueError("need_at_least_two_sources")
        sources: list[ResearchNeuron] = []
        for sid in source_neuron_ids:
            src = state_store.get_neuron(sid)
            if src is None:
                raise ValueError(f"source_not_found: {sid}")
            sources.append(src)

        cycle = cycle_id or f"merge_{'_'.join(source_neuron_ids)}"
        merged_id = merged_spec["neuron_id"]
        merged = ResearchNeuron(
            neuron_id=merged_id,
            node_id=merged_spec["node_id"],
            node_type=merged_spec["node_type"],
            canonical_name=merged_spec["canonical_name"],
            aliases=[s.canonical_name for s in sources],
            root_theme=sources[0].root_theme,
            created_from=NeuronCreatedFrom.MERGE.value,
            verification_status=VerificationStatus.CONFLICTED.value,
            attention_required=True,
            metabolism_state=MetabolismState.ACTIVE.value,
        )
        state_store.save_neuron(merged)

        # 源神经元置 tombstone(不物理删除),记录 NEURON_TOMBSTONE mutation
        for src in sources:
            src.is_tombstone = True
            state_store.save_neuron(src)
            state_store.save_mutation(MutationEntry(
                mutation_id=_new_mutation_id(),
                cycle_id=cycle,
                action=NeuronMutationAction.NEURON_TOMBSTONE.value,
                target_type="neuron",
                target_id=src.neuron_id,
                before={"is_tombstone": False},
                after={"is_tombstone": True, "merged_into": merged_id},
                reason=reason,
                is_tombstone=True,
            ))

        state_store.save_mutation(MutationEntry(
            mutation_id=_new_mutation_id(),
            cycle_id=cycle,
            action=NeuronMutationAction.NEURON_MERGED.value,
            target_type="neuron",
            target_id=merged_id,
            before={},
            after={"merged_from": source_neuron_ids,
                   "verification_status": VerificationStatus.CONFLICTED.value},
            reason=reason,
        ))
        return merged_id

    def prune_neuron(
        self,
        state_store: NeuronStateStore,
        neuron_id: str,
        reason: str,
        evidence_ids: list[str] | None = None,
        cycle_id: str = "",
    ) -> None:
        """凋亡(spec 5.3)。置 PRUNED 状态 + tombstone,不物理删除。

        Args:
            state_store: 神经元状态存储。
            neuron_id: 待凋亡神经元 ID。
            reason: 凋亡理由。
            evidence_ids: 关联证据 ID 列表。
            cycle_id: 所属 cycle。

        Raises:
            ValueError: 神经元不存在。
        """
        neuron = state_store.get_neuron(neuron_id)
        if neuron is None:
            raise ValueError(f"not_found: {neuron_id}")
        cycle = cycle_id or f"prune_{neuron_id}"
        before = {
            "verification_status": neuron.verification_status,
            "metabolism_state": neuron.metabolism_state,
            "is_tombstone": neuron.is_tombstone,
        }
        neuron.verification_status = VerificationStatus.PRUNED.value
        neuron.metabolism_state = MetabolismState.PRUNED.value
        neuron.is_tombstone = True
        state_store.save_neuron(neuron)
        state_store.save_mutation(MutationEntry(
            mutation_id=_new_mutation_id(),
            cycle_id=cycle,
            action=NeuronMutationAction.NEURON_PRUNED.value,
            target_type="neuron",
            target_id=neuron_id,
            before=before,
            after={
                "verification_status": VerificationStatus.PRUNED.value,
                "metabolism_state": MetabolismState.PRUNED.value,
                "is_tombstone": True,
            },
            reason=reason,
            evidence_ids=list(evidence_ids or []),
            is_tombstone=True,
        ))

    def revive_neuron(
        self,
        state_store: NeuronStateStore,
        neuron_id: str,
        new_evidence_ids: list[str],
        reason: str,
        cycle_id: str = "",
    ) -> bool:
        """复活(spec 5.3)。仅 DORMANT/DECAYING 可复活,PRUNED 不可自然复活。

        Args:
            state_store: 神经元状态存储。
            neuron_id: 待复活神经元 ID。
            new_evidence_ids: 触发复活的新证据 ID 列表。
            reason: 复活理由。
            cycle_id: 所属 cycle。

        Returns:
            True 表示复活成功;False 表示当前代谢状态不可复活(ACTIVE/PRUNED 等)。

        Raises:
            ValueError: 神经元不存在。
        """
        neuron = state_store.get_neuron(neuron_id)
        if neuron is None:
            raise ValueError(f"not_found: {neuron_id}")
        revivable = {MetabolismState.DORMANT.value, MetabolismState.DECAYING.value}
        if neuron.metabolism_state not in revivable:
            return False
        cycle = cycle_id or f"revive_{neuron_id}"
        before = {"metabolism_state": neuron.metabolism_state}
        neuron.metabolism_state = MetabolismState.ACTIVE.value
        state_store.save_neuron(neuron)
        state_store.save_mutation(MutationEntry(
            mutation_id=_new_mutation_id(),
            cycle_id=cycle,
            action=NeuronMutationAction.NEURON_REVIVED.value,
            target_type="neuron",
            target_id=neuron_id,
            before=before,
            after={"metabolism_state": MetabolismState.ACTIVE.value},
            reason=reason,
            evidence_ids=list(new_evidence_ids),
        ))
        return True

    def rollback_mutation(
        self,
        state_store: NeuronStateStore,
        mutation_id: str,
        reason: str,
        cycle_id: str = "",
    ) -> bool:
        """回滚指定 mutation(spec 5.4)。将 target 对象恢复到 before 状态。

        Args:
            state_store: 神经元状态存储。
            mutation_id: 待回滚的 mutation ID。
            reason: 回滚理由。
            cycle_id: 所属 cycle。

        Returns:
            True 表示回滚成功;False 表示 mutation 不存在或 target 已丢失。
        """
        original = state_store.get_mutation(mutation_id)
        if original is None:
            return False
        cycle = cycle_id or f"rollback_{mutation_id}"

        # 仅处理 neuron 类型回滚;synapse 等其他类型保留扩展点
        if original.target_type == "neuron":
            neuron = state_store.get_neuron(original.target_id)
            if neuron is None:
                return False
            for field_name, value in original.before.items():
                if hasattr(neuron, field_name):
                    setattr(neuron, field_name, value)
            # tombstone 类 mutation 回滚时清除 tombstone 标记
            if original.is_tombstone:
                neuron.is_tombstone = False
            state_store.save_neuron(neuron)

        # 记录回滚 mutation:before/after 互换,rollback_of 指向原 mutation_id
        state_store.save_mutation(MutationEntry(
            mutation_id=_new_mutation_id(),
            cycle_id=cycle,
            action=NeuronMutationAction.MUTATION_ROLLBACK.value,
            target_type=original.target_type,
            target_id=original.target_id,
            before=dict(original.after),
            after=dict(original.before),
            reason=reason,
            rollback_of=mutation_id,
        ))
        return True
