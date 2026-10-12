"""Canonical ontology registry.

OntologyRegistry 是 Perilla 全系统的语义中枢，集中管理：

- 稳定图关系类型（RelationType）
- 节点类型（NodeType）
- 事件类型（EventType）
- 结构轴（StructuralAxis）
- 可晋升路径类型（promotable path types）
- 事件类型到稳定关系的映射（event_type → relation_type）
- Perilla 信号类型到 SAG 事件类型的映射（signal_type → event_type）
- Perilla source_type 到 SAG SourceType 的映射（perilla_source → sag_source）
- 来源可信度权重（source weights）
- 结构轴关键词（axis keywords）

设计原则：
- 默认从 ``shared.ontology.ontology_models`` 的枚举初始化，保证与代码级枚举一致。
- 支持从 JSON 文件加载/保存，允许 runtime schema 扩展。
- 所有新增/修改 schema 的入口必须经 ``SchemaEvolutionEngine`` + ``SchemaPromotionGate``，
  不允许业务代码直接修改 registry 数据。
- ``RelationRegistry`` / ``AxisRegistry`` 作为向后兼容的 facade，未来逐步迁移到本类。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from core.research.perilla.shared.ontology.ontology_models import (
    AXIS_LABELS,
    NODE_TYPES,
    RELATION_LABELS,
    RELATION_TYPES,
    SOURCE_TYPES,
    STRUCTURAL_AXES,
    EVENT_TYPES,
    RelationType,
    NodeType,
    EventType,
    StructuralAxis,
    SourceType,
)
from core.research.perilla.alpha.graph_schema import (
    AXIS_KEYWORDS,
    SOURCE_WEIGHTS,
)

logger = logging.getLogger(__name__)

DEFAULT_ONTOLOGY_PATH = os.path.join(
    "data", "ontology", "ontology_registry.json"
)


@dataclass
class OntologySnapshot:
    """OntologyRegistry 某一时刻的只读快照。"""

    relations: frozenset[str]
    nodes: frozenset[str]
    events: frozenset[str]
    axes: frozenset[str]
    path_types: frozenset[str]
    event_to_relation: dict[str, str]
    signal_to_event: dict[str, str]
    perilla_source_to_sag: dict[str, str]
    source_weights: dict[str, float]
    axis_keywords: dict[str, list[str]]


class OntologyRegistry:
    """Canonical ontology registry with persistence.

    提供统一接口查询、校验和扩展 ontology。扩展操作只应由
    ``SchemaEvolutionEngine`` 调用，业务代码只读使用。
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        """Initialize registry from data or defaults.

        Args:
            data: Optional registry data dictionary. If None, defaults are loaded
                from canonical enums and constants.
        """
        self._data = dict(data) if isinstance(data, Mapping) else self._default_data()
        self._validate_data()

    # ------------------------------------------------------------------
    # Default data
    # ------------------------------------------------------------------
    @classmethod
    def _default_data(cls) -> dict[str, Any]:
        """Build default ontology data from canonical enums and constants."""
        return {
            "version": "1.0.0",
            "relations": {
                r: {
                    "label": RELATION_LABELS.get(r, r),
                    "domain": [],
                    "range": [],
                    "approved": True,
                }
                for r in RELATION_TYPES
            },
            "nodes": {
                n: {"approved": True}
                for n in NODE_TYPES
            },
            "events": {
                e: {"approved": True}
                for e in EVENT_TYPES
            },
            "axes": {
                a: {
                    "label": AXIS_LABELS.get(a, a),
                    "approved": True,
                }
                for a in STRUCTURAL_AXES
            },
            "path_types": {
                "supplier_dependency_path": {
                    "required_axes": [StructuralAxis.SUPPLIER.value],
                    "promotable": True,
                },
                "material_bottleneck_path": {
                    "required_axes": [StructuralAxis.MATERIAL.value],
                    "promotable": True,
                },
                "equipment_bottleneck_path": {
                    "required_axes": [StructuralAxis.EQUIPMENT.value],
                    "promotable": True,
                },
                "process_barrier_path": {
                    "required_axes": [StructuralAxis.PROCESS.value],
                    "promotable": True,
                },
                "certification_barrier_path": {
                    "required_axes": [StructuralAxis.CERTIFICATION.value],
                    "promotable": True,
                },
                "domestic_substitution_path": {
                    "required_axes": [StructuralAxis.DOMESTIC_SUBSTITUTION.value],
                    "promotable": True,
                },
                "import_dependency_path": {
                    "required_axes": [StructuralAxis.DOMESTIC_SUBSTITUTION.value],
                    "promotable": True,
                },
            },
            "event_to_relation": {
                EventType.PRICE_INCREASE.value: RelationType.COST_DRIVER_OF.value,
                EventType.CAPACITY_EXPANSION.value: RelationType.SUPPLIED_BY.value,
                EventType.CUSTOMER_CERTIFIED.value: RelationType.CERTIFIED_BY.value,
                EventType.ORDER_DISCLOSED.value: RelationType.SUPPLIED_BY.value,
                EventType.PRODUCT_LAUNCHED.value: RelationType.ENABLED_BY.value,
                EventType.SUPPLY_SHORTAGE.value: RelationType.BOTTLENECK_OF.value,
                EventType.DOMESTIC_SUBSTITUTION_PROGRESS.value: RelationType.SUBSTITUTE_FOR.value,
                EventType.POLICY_SIGNAL.value: RelationType.ENABLED_BY.value,
                EventType.IMPORT_DEPENDENCY_SIGNAL.value: RelationType.IMPORT_DEPENDENT_ON.value,
                EventType.CERTIFICATION_PROGRESS.value: RelationType.CERTIFIED_BY.value,
                EventType.CONCEPT_SIGNAL.value: RelationType.ENABLED_BY.value,
                EventType.BOTTLENECK_HYPOTHESIS.value: RelationType.BOTTLENECK_OF.value,
            },
            "signal_to_event": {
                "concept_signal": EventType.CONCEPT_SIGNAL.value,
                "bottleneck_hypothesis": EventType.BOTTLENECK_HYPOTHESIS.value,
                "supplier": EventType.ORDER_DISCLOSED.value,
                "certification": EventType.CERTIFICATION_PROGRESS.value,
                "sampling": EventType.CUSTOMER_CERTIFIED.value,
                "capacity": EventType.CAPACITY_EXPANSION.value,
                "recruitment": EventType.OTHER.value,
                "expo": EventType.OTHER.value,
                "tender": EventType.ORDER_DISCLOSED.value,
                "partner": EventType.OTHER.value,
                "customer": EventType.ORDER_DISCLOSED.value,
                "technical_route": EventType.DOMESTIC_SUBSTITUTION_PROGRESS.value,
                "rumor": EventType.OTHER.value,
                "concept_claim": EventType.CONCEPT_SIGNAL.value,
                "other": EventType.OTHER.value,
            },
            "perilla_source_to_sag": {
                "announcement": SourceType.ANNOUNCEMENT.value,
                "report": SourceType.BROKER_REPORT.value,
                "news": SourceType.NEWS.value,
                "rss": SourceType.NEWS.value,
                "official_site": SourceType.OFFICIAL_WEBSITE.value,
                "recruitment": SourceType.NEWS.value,
                "expo": SourceType.EXHIBITION.value,
                "tender": SourceType.ANNOUNCEMENT.value,
                "forum": SourceType.FORUM.value,
                "social": SourceType.SOCIAL_MEDIA.value,
                "meeting_note": SourceType.MEETING_MINUTES.value,
                "manual": SourceType.LLM_INFERENCE.value,
                "local_evidence": SourceType.LLM_INFERENCE.value,
                "concept_page": SourceType.WEB_SEARCH_SNIPPET.value,
                "sector_page": SourceType.WEB_SEARCH_SNIPPET.value,
                "search_result": SourceType.WEB_SEARCH_SNIPPET.value,
                "change_event": SourceType.NEWS.value,
                "bottleneck_hypothesis": SourceType.LLM_INFERENCE.value,
            },
            "source_weights": dict(SOURCE_WEIGHTS),
            "axis_keywords": {
                axis: list(keywords)
                for axis, keywords in AXIS_KEYWORDS.items()
            },
        }

    def _validate_data(self) -> None:
        """Ensure required sections exist."""
        for section in (
            "relations", "nodes", "events", "axes", "path_types",
            "event_to_relation", "signal_to_event", "perilla_source_to_sag",
            "source_weights", "axis_keywords",
        ):
            if not isinstance(self._data.get(section), Mapping):
                self._data[section] = {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "OntologyRegistry":
        """Load registry from JSON file, falling back to defaults.

        Args:
            path: Path to the JSON file. Defaults to ``DEFAULT_ONTOLOGY_PATH``.

        Returns:
            Loaded OntologyRegistry.
        """
        path = path or DEFAULT_ONTOLOGY_PATH
        if not os.path.exists(path):
            logger.info("Ontology registry not found at %s, using defaults", path)
            registry = cls.default()
            registry.save(path)
            return registry

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, Mapping):
                raise ValueError("ontology registry must be a JSON object")
            logger.info("Loaded ontology registry from %s", path)
            return cls(data)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            logger.warning("Failed to load ontology registry from %s: %s", path, e)
            return cls.default()

    def save(self, path: str | None = None) -> None:
        """Persist registry to JSON file.

        Args:
            path: Path to the JSON file. Defaults to ``DEFAULT_ONTOLOGY_PATH``.
        """
        path = path or DEFAULT_ONTOLOGY_PATH
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".ontology_registry.", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @classmethod
    def default(cls) -> "OntologyRegistry":
        """Return a registry initialized from canonical enums."""
        return cls(cls._default_data())

    def snapshot(self) -> OntologySnapshot:
        """Return an immutable snapshot of current ontology."""
        return OntologySnapshot(
            relations=self.all_relations(),
            nodes=self.all_node_types(),
            events=self.all_event_types(),
            axes=self.all_axes(),
            path_types=self.all_path_types(),
            event_to_relation=deepcopy(self._data["event_to_relation"]),
            signal_to_event=deepcopy(self._data["signal_to_event"]),
            perilla_source_to_sag=deepcopy(self._data["perilla_source_to_sag"]),
            source_weights=deepcopy(self._data["source_weights"]),
            axis_keywords={
                axis: list(keywords)
                for axis, keywords in self._data["axis_keywords"].items()
            },
        )

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------
    def is_valid_relation(self, relation: str) -> bool:
        """Return True if relation is in the canonical ontology."""
        return relation in self._data["relations"]

    def all_relations(self) -> frozenset[str]:
        """Return all registered relation values."""
        return frozenset(self._data["relations"].keys())

    def relation_metadata(self, relation: str) -> dict[str, Any] | None:
        """Return metadata for a relation, or None if unknown."""
        return self._data["relations"].get(relation)

    def relation_label(self, relation: str) -> str:
        """Return human-readable label for a relation."""
        meta = self.relation_metadata(relation)
        if isinstance(meta, Mapping) and meta.get("label"):
            return meta["label"]
        return RELATION_LABELS.get(relation, relation)

    def register_relation(
        self,
        relation: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a new relation type. Should only be called by evolution engine."""
        self._data["relations"][relation] = metadata or {"approved": True}

    # ------------------------------------------------------------------
    # Node types
    # ------------------------------------------------------------------
    def is_valid_node_type(self, node_type: str) -> bool:
        """Return True if node_type is in the canonical ontology."""
        return node_type in self._data["nodes"]

    def all_node_types(self) -> frozenset[str]:
        """Return all registered node types."""
        return frozenset(self._data["nodes"].keys())

    def register_node_type(
        self,
        node_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a new node type. Should only be called by evolution engine."""
        self._data["nodes"][node_type] = metadata or {"approved": True}

    # ------------------------------------------------------------------
    # Event types
    # ------------------------------------------------------------------
    def is_valid_event_type(self, event_type: str) -> bool:
        """Return True if event_type is in the canonical ontology."""
        return event_type in self._data["events"]

    def all_event_types(self) -> frozenset[str]:
        """Return all registered event types."""
        return frozenset(self._data["events"].keys())

    def register_event_type(
        self,
        event_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a new event type. Should only be called by evolution engine."""
        self._data["events"][event_type] = metadata or {"approved": True}

    # ------------------------------------------------------------------
    # Axes
    # ------------------------------------------------------------------
    def is_valid_axis(self, axis: str) -> bool:
        """Return True if axis is a registered structural axis."""
        return axis in self._data["axes"]

    def all_axes(self) -> frozenset[str]:
        """Return all registered structural axes."""
        return frozenset(self._data["axes"].keys())

    def axis_label(self, axis: str) -> str:
        """Return human-readable label for an axis."""
        meta = self._data["axes"].get(axis)
        if isinstance(meta, Mapping) and meta.get("label"):
            return meta["label"]
        return AXIS_LABELS.get(axis, axis)

    def register_axis(
        self,
        axis: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a new axis. Should only be called by evolution engine."""
        self._data["axes"][axis] = metadata or {"approved": True}

    # ------------------------------------------------------------------
    # Path types
    # ------------------------------------------------------------------
    def is_promotable_path_type(self, path_type: str) -> bool:
        """Return True if path_type is allowed to promote claims."""
        meta = self._data["path_types"].get(path_type)
        return bool(isinstance(meta, Mapping) and meta.get("promotable", False))

    def all_path_types(self) -> frozenset[str]:
        """Return all registered path types (including non-promotable)."""
        return frozenset(self._data["path_types"].keys())

    def all_promotable_path_types(self) -> frozenset[str]:
        """Return path types that are allowed to promote claims."""
        return frozenset(
            pt
            for pt, meta in self._data["path_types"].items()
            if isinstance(meta, Mapping) and meta.get("promotable", False)
        )

    def path_type_metadata(self, path_type: str) -> dict[str, Any] | None:
        """Return metadata for a path type."""
        return self._data["path_types"].get(path_type)

    def register_path_type(
        self,
        path_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a new path type. Should only be called by evolution engine."""
        self._data["path_types"][path_type] = metadata or {"promotable": False}

    # ------------------------------------------------------------------
    # Event → relation mapping
    # ------------------------------------------------------------------
    @property
    def event_to_relation(self) -> dict[str, str]:
        """Return the full event_type -> relation mapping dictionary."""
        return dict(self._data["event_to_relation"])

    def event_type_to_relation(self, event_type: str) -> str | None:
        """Return the stable relation mapped from an event type, or None."""
        return self._data["event_to_relation"].get(event_type)

    def register_event_to_relation(
        self,
        event_type: str,
        relation: str,
    ) -> None:
        """Register or update event_type → relation mapping.

        Should only be called by evolution engine.
        """
        self._data["event_to_relation"][event_type] = relation

    # ------------------------------------------------------------------
    # Signal → event mapping
    # ------------------------------------------------------------------
    @property
    def signal_to_event(self) -> dict[str, str]:
        """Return the full Perilla signal_type -> SAG event_type mapping."""
        return dict(self._data["signal_to_event"])

    def signal_type_to_event_type(self, signal_type: str) -> str:
        """Return the SAG event type for a Perilla signal type.

        Unknown signal types fall back to ``EventType.OTHER.value``.
        """
        return self._data["signal_to_event"].get(signal_type, EventType.OTHER.value)

    def register_signal_to_event(
        self,
        signal_type: str,
        event_type: str,
    ) -> None:
        """Register or update signal_type → event_type mapping.

        Should only be called by evolution engine.
        """
        self._data["signal_to_event"][signal_type] = event_type

    # ------------------------------------------------------------------
    # Perilla source → SAG source mapping
    # ------------------------------------------------------------------
    @property
    def perilla_source_to_sag(self) -> dict[str, str]:
        """Return the full Perilla source_type -> SAG SourceType mapping."""
        return dict(self._data["perilla_source_to_sag"])

    def perilla_source_to_sag_source(self, perilla_source: str) -> str:
        """Return the SAG SourceType for a Perilla source type.

        Unknown sources fall back to ``SourceType.LLM_INFERENCE.value``.
        """
        return self._data["perilla_source_to_sag"].get(
            perilla_source, SourceType.LLM_INFERENCE.value
        )

    def register_perilla_source_to_sag(
        self,
        perilla_source: str,
        sag_source: str,
    ) -> None:
        """Register or update Perilla source_type → SAG SourceType mapping.

        Should only be called by evolution engine.
        """
        self._data["perilla_source_to_sag"][perilla_source] = sag_source

    # ------------------------------------------------------------------
    # Source weights
    # ------------------------------------------------------------------
    def is_valid_source_type(self, source_type: str) -> bool:
        """Return True if source_type has a registered weight."""
        return source_type in self._data["source_weights"]

    def get_source_weight(self, source_type: str) -> float:
        """Return source weight, defaulting to 0.50 for unknown sources."""
        value = self._data["source_weights"].get(source_type, 0.50)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.50

    def set_source_weight(self, source_type: str, weight: float) -> None:
        """Set source weight. Should only be called by evolution engine."""
        try:
            value = float(weight)
        except (TypeError, ValueError):
            value = 0.50
        if value != value or value in (float("inf"), float("-inf")):
            value = 0.50
        self._data["source_weights"][source_type] = max(0.0, min(1.0, value))

    # ------------------------------------------------------------------
    # Axis keywords
    # ------------------------------------------------------------------
    def axis_keywords(self, axis: str) -> list[str]:
        """Return keywords associated with an axis."""
        value = self._data["axis_keywords"].get(axis, [])
        return list(value) if isinstance(value, (list, tuple)) else []

    def set_axis_keywords(self, axis: str, keywords: list[str]) -> None:
        """Set keywords for an axis. Should only be called by evolution engine."""
        self._data["axis_keywords"][axis] = list(keywords)

    # ------------------------------------------------------------------
    # Version
    # ------------------------------------------------------------------
    @property
    def version(self) -> str:
        """Return ontology version string."""
        return str(self._data.get("version", "0.0.0"))

    def set_version(self, version: str) -> None:
        """Set ontology version. Should only be called by evolution engine."""
        self._data["version"] = version
