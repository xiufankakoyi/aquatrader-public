"""ResearchNeuron Growth Engine — 动态超图神经元生长内核。

子包结构:
    enums.py          — 所有 Enum
    models.py         — ResearchNeuron/ResearchSynapse/GrowthPulse/NeuronContext
    policy.py         — NeuronPolicyConfig
    state_store.py    — NeuronStateStore(lancedb 4 表)
    evidence_reader.py — 受限读取器 Protocol + 实现
    field_signals.py  — FieldSignalCalculator(neuron 内部工具,Plan 2)
    events.py         — 9 类 Event + EventRouter(Plan 3)
    environment.py    — ResearchGraphEnvironment(Plan 3)
    neuron.py         — ResearchNeuron.growth_cycle()(Plan 2)
    lifecycle.py      — split/merge/prune/revive/rollback(Plan 3)
"""
from __future__ import annotations

from core.research_graph.neuron.enums import (
    DEFAULT_DIMENSIONS,
    EvidenceStance,
    MetabolismState,
    NeuronCreatedFrom,
    NeuronMutationAction,
    PulseStatus,
    PulseType,
    SynapseDirection,
    SynapseRelation,
    SynapseStatus,
    SynapseTargetKind,
    VerificationStatus,
)
from core.research_graph.neuron.evidence_reader import (
    EvidenceLedgerReader,
    EvidenceLedgerReaderImpl,
    MutationLogWriter,
    MutationLogWriterImpl,
    SynapseStateReader,
    SynapseStateReaderImpl,
)
from core.research_graph.neuron.models import (
    EvidenceAggregate,
    FieldSignal,
    GrowthPulse,
    MutationEntry,
    NeighborRef,
    NeuronContext,
    ResourceBudget,
    ResearchNeuron,
    ResearchSynapse,
    SynapseUpdate,
    TargetDraft,
    WeightedEvidence,
)
from core.research_graph.neuron.policy import NeuronPolicyConfig
from core.research_graph.neuron.state_store import NeuronStateStore
from core.research_graph.neuron.field_signals import FieldSignalCalculator
from core.research_graph.neuron.neuron import (
    CommitResult,
    GrowthCycleResult,
    ResolveResult,
    SenseResult,
    growth_cycle,
)
from core.research_graph.neuron.events import (
    CounterEvidenceArrivedEvent,
    EvidenceArrivedEvent,
    EvidenceIndex,
    EvidenceRetractedEvent,
    EventRouter,
    GrowthPulseAbsorbedEvent,
    ManualMutationEvent,
    MergeCandidateEvent,
    NeighborStateChangedEvent,
    NeuronEvent,
    SplitCandidateEvent,
    TimeTickMetabolismEvent,
)
from core.research_graph.neuron.lifecycle import LifecycleManager
from core.research_graph.neuron.environment import (
    EnvironmentBudgetConfig,
    ResearchGraphEnvironment,
)

__all__ = [
    "DEFAULT_DIMENSIONS",
    "CommitResult",
    "CounterEvidenceArrivedEvent",
    "EnvironmentBudgetConfig",
    "EvidenceAggregate",
    "EvidenceArrivedEvent",
    "EvidenceIndex",
    "EvidenceLedgerReader",
    "EvidenceLedgerReaderImpl",
    "EvidenceRetractedEvent",
    "EvidenceStance",
    "EventRouter",
    "FieldSignal",
    "FieldSignalCalculator",
    "GrowthCycleResult",
    "GrowthPulse",
    "GrowthPulseAbsorbedEvent",
    "LifecycleManager",
    "ManualMutationEvent",
    "MergeCandidateEvent",
    "MetabolismState",
    "MutationEntry",
    "MutationLogWriter",
    "MutationLogWriterImpl",
    "NeighborRef",
    "NeighborStateChangedEvent",
    "NeuronContext",
    "NeuronCreatedFrom",
    "NeuronEvent",
    "NeuronMutationAction",
    "NeuronPolicyConfig",
    "NeuronStateStore",
    "PulseStatus",
    "PulseType",
    "ResourceBudget",
    "ResearchGraphEnvironment",
    "ResearchNeuron",
    "ResearchSynapse",
    "ResolveResult",
    "SenseResult",
    "SplitCandidateEvent",
    "SynapseDirection",
    "SynapseRelation",
    "SynapseStateReader",
    "SynapseStateReaderImpl",
    "SynapseStatus",
    "SynapseTargetKind",
    "SynapseUpdate",
    "TargetDraft",
    "TimeTickMetabolismEvent",
    "VerificationStatus",
    "WeightedEvidence",
    "growth_cycle",
]
