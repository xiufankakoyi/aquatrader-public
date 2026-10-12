"""Entity resolver for Graph-first Alpha Discovery Engine.

Resolves raw text entities into canonical entities with type information,
using local A-share universe, non-A-share companies, aliases, and material/equipment
term lists. Falls back to heuristic inference only when local data is insufficient.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.research.perilla.shared.ontology.ontology_models import NodeType, RelationType

logger = logging.getLogger(__name__)

# Relations that are allowed to have a company node as head or tail.
_COMPANY_RELATIONS: set[str] = {
    RelationType.SUPPLIED_BY.value,
    RelationType.IMPORT_DEPENDENT_ON.value,
    RelationType.CERTIFIED_BY.value,
    RelationType.SUBSTITUTE_FOR.value,  # company can substitute another entity
}


@dataclass
class ResolvedEntity:
    """Result of resolving a raw entity name."""

    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    entity_type: str = "generic_term"  # company/material/component/equipment/process/technology/product/concept/index_label/generic_term
    node_type: str = NodeType.GENERIC.value
    listed_status: str = "unknown"  # a_share/hk_share/us_share/non_a_share/subsidiary/business_unit/private/delisted/unknown
    stock_code: str = ""
    parent_company: str = ""
    country_or_region: str = ""
    resolver_confidence: float = 0.0
    resolver_reason: str = ""


class EntityResolver:
    """Resolve entity names to canonical forms and types."""

    @staticmethod
    def _detect_project_root() -> Path:
        """Detect project root by looking for the data/entity_universe directory."""
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "data" / "entity_universe").exists():
                return parent
        for parent in current.parents:
            if (parent / "AGENTS.md").exists() and (parent / "core").exists():
                return parent
        # Fallback to the expected depth if the directory is missing.
        parents = list(current.parents)
        return parents[5] if len(parents) > 5 else current.parent

    def __init__(
        self,
        entity_universe_dir: str | Path | None = None,
        a_share_universe_path: str | Path | None = None,
    ) -> None:
        """Initialize resolver and load local entity data.

        Args:
            entity_universe_dir: Directory containing JSON entity universe files.
                Defaults to project_root/data/entity_universe.
            a_share_universe_path: Optional preferred A-share universe JSON path.
                If provided and exists, it overrides the default detection.
        """
        self.project_root = self._detect_project_root()
        self.entity_universe_dir = (
            Path(entity_universe_dir) if entity_universe_dir else self.project_root / "data" / "entity_universe"
        )
        self._preferred_a_share_path = a_share_universe_path

        # Data stores
        self._a_share_by_name: dict[str, dict[str, Any]] = {}
        self._a_share_by_code: dict[str, dict[str, Any]] = {}
        self._non_a_share_by_name: dict[str, dict[str, Any]] = {}
        self._aliases: dict[str, dict[str, Any]] = {}
        self._material_equipment_terms: dict[str, dict[str, Any]] = {}
        self._generic_denylist: set[str] = set()

        # Load status
        self._a_share_loaded = False
        self._non_a_share_loaded = False
        self._alias_loaded = False
        self._material_equipment_loaded = False
        self._generic_denylist_loaded = False

        self._load_all()

    def _load_all(self) -> None:
        """Load all local entity data files."""
        self._load_a_share_universe()
        self._load_non_a_share_companies()
        self._load_aliases()
        self._load_material_equipment_terms()
        self._load_generic_denylist()

    def _load_a_share_universe(self) -> None:
        """Load A-share universe with auto-detection."""
        paths_to_try: list[Path] = []
        if self._preferred_a_share_path:
            paths_to_try.append(Path(self._preferred_a_share_path))
        paths_to_try.extend([
            self.project_root / "data" / "china_stock_universe.json",
            self.project_root / "data" / "entity_universe" / "china_stock_universe.json",
            self.entity_universe_dir / "a_share_stocks.json",
        ])

        for path in paths_to_try:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    for item in data:
                        if not isinstance(item, Mapping):
                            continue
                        name = str(item.get("name", "")).strip()
                        full_name = str(item.get("full_name", "")).strip()
                        code = str(item.get("code", "")).strip()
                        aliases = [str(a).strip() for a in item.get("aliases", []) if str(a).strip()]
                        former_names = [str(a).strip() for a in item.get("former_names", []) if str(a).strip()]
                        if not name:
                            continue
                        record = {
                            "name": name,
                            "full_name": full_name,
                            "code": code,
                            "aliases": aliases,
                            "former_names": former_names,
                        }
                        self._a_share_by_name[name] = record
                        if full_name:
                            self._a_share_by_name[full_name] = record
                        for alias in aliases:
                            self._a_share_by_name[alias] = record
                        for former in former_names:
                            self._a_share_by_name[former] = record
                        if code:
                            self._a_share_by_code[code] = record
                    self._a_share_loaded = True
                    logger.info("EntityResolver loaded A-share universe from %s (%d entries)", path, len(self._a_share_by_name))
                    return
                except Exception as e:
                    logger.warning("Failed to load A-share universe from %s: %s", path, e)

        self._a_share_loaded = False
        logger.warning("EntityResolver: A-share universe not available")

    def _load_non_a_share_companies(self) -> None:
        """Load non-A-share company list."""
        path = self.entity_universe_dir / "non_a_share_companies.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data:
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                self._non_a_share_by_name[name] = item
                for alias in item.get("aliases", []):
                    alias = str(alias).strip()
                    if alias:
                        self._non_a_share_by_name[alias] = item
            self._non_a_share_loaded = True
            logger.info("EntityResolver loaded non-A-share companies (%d names)", len(self._non_a_share_by_name))
        except Exception as e:
            logger.warning("Failed to load non-A-share companies: %s", e)

    def _load_aliases(self) -> None:
        """Load alias mapping table."""
        path = self.entity_universe_dir / "company_aliases.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, Mapping):
                return
            self._aliases = dict(data)
            self._alias_loaded = True
            logger.info("EntityResolver loaded aliases (%d entries)", len(self._aliases))
        except Exception as e:
            logger.warning("Failed to load aliases: %s", e)

    def _load_material_equipment_terms(self) -> None:
        """Load material/equipment/process/certification terms."""
        path = self.entity_universe_dir / "material_equipment_terms.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data:
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("name", "")).strip()
                category = str(item.get("category", "")).strip()
                if not name or not category:
                    continue
                self._material_equipment_terms[name] = item
                for alias in item.get("aliases", []):
                    alias = str(alias).strip()
                    if alias:
                        self._material_equipment_terms[alias] = item
            self._material_equipment_loaded = True
            logger.info("EntityResolver loaded material/equipment terms (%d names)", len(self._material_equipment_terms))
        except Exception as e:
            logger.warning("Failed to load material/equipment terms: %s", e)

    def _load_generic_denylist(self) -> None:
        """Load generic term denylist."""
        path = self.entity_universe_dir / "generic_denylist.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, (str, bytes, Mapping)):
                return
            self._generic_denylist = {str(x).strip() for x in data if str(x).strip()}
            self._generic_denylist_loaded = True
            logger.info("EntityResolver loaded generic denylist (%d entries)", len(self._generic_denylist))
        except Exception as e:
            logger.warning("Failed to load generic denylist: %s", e)

    def data_source_status(self) -> dict[str, Any]:
        """Return loading status of all data sources."""
        return {
            "a_share_universe_loaded": self._a_share_loaded,
            "a_share_universe_count": len(self._a_share_by_name),
            "non_a_share_companies_loaded": self._non_a_share_loaded,
            "non_a_share_companies_count": len(self._non_a_share_by_name),
            "aliases_loaded": self._alias_loaded,
            "aliases_count": len(self._aliases),
            "material_equipment_terms_loaded": self._material_equipment_loaded,
            "material_equipment_terms_count": len(self._material_equipment_terms),
            "generic_denylist_loaded": self._generic_denylist_loaded,
            "generic_denylist_count": len(self._generic_denylist),
        }

    @staticmethod
    def _normalize(name: str) -> str:
        """Normalize entity name for matching."""
        return str(name).strip().replace(" ", "").replace("　", "")

    def _lookup_alias(self, name: str) -> str:
        """Return canonical name from alias table if exists."""
        norm = self._normalize(name)
        alias_record = self._aliases.get(norm)
        if isinstance(alias_record, Mapping):
            return str(alias_record.get("canonical_name", name)).strip()
        return name

    def _resolve_company(self, name: str) -> ResolvedEntity | None:
        """Try to resolve name as a company."""
        canonical = self._lookup_alias(name)
        norm = self._normalize(canonical)

        # A-share
        record = self._a_share_by_name.get(norm)
        if record:
            return ResolvedEntity(
                canonical_name=record["name"],
                aliases=record.get("aliases", []),
                entity_type="company",
                node_type=NodeType.COMPANY.value,
                listed_status="a_share",
                stock_code=record.get("code", ""),
                parent_company="",
                country_or_region="中国",
                resolver_confidence=0.95,
                resolver_reason="a_share_universe_match",
            )

        # A-share by code
        record = self._a_share_by_code.get(norm)
        if record:
            return ResolvedEntity(
                canonical_name=record["name"],
                aliases=record.get("aliases", []),
                entity_type="company",
                node_type=NodeType.COMPANY.value,
                listed_status="a_share",
                stock_code=record.get("code", ""),
                parent_company="",
                country_or_region="中国",
                resolver_confidence=0.95,
                resolver_reason="a_share_code_match",
            )

        # Non-A-share
        record = self._non_a_share_by_name.get(norm)
        if record:
            return ResolvedEntity(
                canonical_name=record["name"],
                aliases=record.get("aliases", []),
                entity_type="company",
                node_type=NodeType.COMPANY.value,
                listed_status=record.get("listed_status", "non_a_share"),
                stock_code=record.get("stock_code", ""),
                parent_company=record.get("parent_company", ""),
                country_or_region=record.get("country", ""),
                resolver_confidence=0.9,
                resolver_reason="non_a_share_universe_match",
            )

        return None

    def _resolve_non_company(self, name: str) -> ResolvedEntity | None:
        """Try to resolve name as material/equipment/process/etc."""
        norm = self._normalize(name)
        record = self._material_equipment_terms.get(norm)
        if not record:
            return None

        category = record.get("category", "")
        node_type_map = {
            "material": NodeType.MATERIAL.value,
            "component": NodeType.COMPONENT.value,
            "equipment": NodeType.EQUIPMENT.value,
            "process": NodeType.PROCESS.value,
            "testing": NodeType.TESTING.value,
            "certification": NodeType.CERTIFICATION.value,
            "consumable": NodeType.CONSUMABLE.value,
        }
        return ResolvedEntity(
            canonical_name=record["name"],
            aliases=record.get("aliases", []),
            entity_type=category,
            node_type=node_type_map.get(category, NodeType.GENERIC.value),
            listed_status="unknown",
            resolver_confidence=0.85,
            resolver_reason=f"material_equipment_term_match:{category}",
        )

    def _is_generic_term(self, name: str) -> bool:
        """Check if name is a generic/banned term.

        Uses exact match first; substring match only when the denylisted term
        appears as a standalone word (not embedded inside a concrete term).
        This prevents valid material nodes like "产业链上游材料" from being
        rejected just because they contain a generic substring.
        """
        norm = self._normalize(name)
        if not norm:
            return False
        if norm in self._generic_denylist:
            return True
        for term in self._generic_denylist:
            if not term:
                continue
            if term == norm:
                return True
            idx = norm.find(term)
            if idx == -1:
                continue
            # Term must not be embedded inside a longer Chinese word.
            before = norm[idx - 1] if idx > 0 else ""
            after = norm[idx + len(term)] if idx + len(term) < len(norm) else ""
            if before and "\u4e00" <= before <= "\u9fff":
                continue
            if after and "\u4e00" <= after <= "\u9fff":
                continue
            return True
        return False

    def resolve(self, name: str) -> ResolvedEntity:
        """Resolve a raw entity name to a canonical entity.

        Args:
            name: Raw entity name.

        Returns:
            ResolvedEntity with best-effort type information.
        """
        if not name or not str(name).strip():
            return ResolvedEntity(
                canonical_name="",
                entity_type="generic_term",
                node_type=NodeType.GENERIC.value,
                resolver_confidence=0.0,
                resolver_reason="empty_name",
            )

        norm = self._normalize(name)

        # Non-company terms first to protect concrete material/equipment/process
        # nodes that may contain generic substrings (e.g. "产业链上游材料").
        non_company = self._resolve_non_company(name)
        if non_company:
            return non_company

        # Generic denylist
        if self._is_generic_term(name):
            return ResolvedEntity(
                canonical_name=norm,
                entity_type="generic_term",
                node_type=NodeType.GENERIC.value,
                resolver_confidence=0.2,
                resolver_reason="generic_denylist_match",
            )

        # Company
        company = self._resolve_company(name)
        if company:
            return company

        # Fallback heuristic (do not classify as material if it looks like a company)
        node_type = self._heuristic_node_type(name)
        return ResolvedEntity(
            canonical_name=norm,
            entity_type="unknown",
            node_type=node_type,
            listed_status="unknown",
            resolver_confidence=0.3,
            resolver_reason="heuristic_fallback",
        )

    @staticmethod
    def _heuristic_node_type(name: str) -> str:
        """Fallback heuristic for node type inference."""
        name_lower = name.lower()

        # Avoid classifying known company suffixes as material
        company_suffixes = [
            "股份", "科技", "电子", "材料", "有限公司", "集团", " Corp", " Inc", " Ltd",
            "博通", "迈威尔", "史陶比尔", "华为", "腾讯", "阿里", "平头哥", "海思", "住友",
        ]
        if any(s in name for s in company_suffixes):
            return NodeType.COMPANY.value

        material_keywords = [
            "矿", "化学", "树脂", "金属", "晶圆", "衬底", "气体", "液", "箔", "板",
            "外壳", "调制器", "接头", "冷板", "冷却液", "氟化液", "光芯片", "光器件",
            "覆铜板", "铜箔", "玻纤", "环氧树脂", "激光器", "探测器",
        ]
        if any(kw in name_lower for kw in material_keywords):
            return NodeType.MATERIAL.value

        equipment_keywords = [
            "设备", "机台", "光刻机", "刻蚀机", "钻孔机", "测试设备", "检测设备",
        ]
        if any(kw in name_lower for kw in equipment_keywords):
            return NodeType.EQUIPMENT.value

        process_keywords = ["工艺", "制程", "封装", "光刻", "蚀刻", "电镀", "钻孔"]
        if any(kw in name_lower for kw in process_keywords):
            return NodeType.PROCESS.value

        return NodeType.GENERIC.value

    def is_company(self, name: str) -> bool:
        """Return True if the resolved entity is a company."""
        resolved = self.resolve(name)
        return resolved.entity_type == "company"

    def is_generic_term(self, name: str) -> bool:
        """Return True if name resolves to a generic term."""
        resolved = self.resolve(name)
        return resolved.entity_type == "generic_term"

    def is_company_position_valid(
        self,
        head: str,
        relation: str,
        tail: str,
    ) -> bool:
        """Check whether a company node appears in a valid position.

        Company nodes should only appear in supplier/customer/competitor/company mapping
        layers, not in material/process/equipment layers.

        Args:
            head: Head entity name.
            relation: Relation type.
            tail: Tail entity name.

        Returns:
            True if any company node in the triple is in a valid position.
        """
        head_resolved = self.resolve(head)
        tail_resolved = self.resolve(tail)

        if head_resolved.entity_type != "company" and tail_resolved.entity_type != "company":
            return True  # no company involved

        if relation in _COMPANY_RELATIONS:
            return True

        # made_of/requires/processed_by/tested_by should not have company as material/process
        invalid_relations = {
            RelationType.MADE_OF.value,
            RelationType.REQUIRES.value,
            RelationType.PROCESSED_BY.value,
            RelationType.TESTED_BY.value,
            RelationType.CONTAINS.value,
            RelationType.BOTTLENECK_OF.value,
            RelationType.CONSTRAINED_BY.value,
            RelationType.COST_DRIVER_OF.value,
            RelationType.PERFORMANCE_DRIVER_OF.value,
        }
        if relation in invalid_relations:
            return False

        return True
