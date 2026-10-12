"""冲突检测与自我怀疑模块。

识别 claim 之间的六类冲突，不删除冲突 claim，而是标记冲突状态并分组仲裁。
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.research.perilla.alpha.graph_schema import ClaimStatus, RelationType, get_source_weight
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.event.event_graph_schema import EventClaim, EventClaimStatus, EventType
from core.research.perilla.alpha.path_discovery_schema import PathCandidate, PathEvidenceBundle
from core.research.perilla.alpha.sag_knowledge_base import Claim, SAGKnowledgeBase, _now_iso

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite(value, default)))


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a model object or a mapping."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping_get(value: Any, key: Any, default: Any = None) -> Any:
    if not isinstance(value, Mapping):
        return default
    try:
        return value.get(key, default)
    except (TypeError, ValueError):
        return default


def _claim_id(value: Any) -> str:
    return _text(value)


def _edge_tuple(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    return tuple(_text(part) for part in value)  # type: ignore[return-value]


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()

EXCLUSIVITY_MARKERS: frozenset[str] = frozenset(["唯一", "仅", "独供", " exclusively ", "sole"])

# Kept for fallback only; entity_conflict now prefers EntityResolver.
COMPANY_SUFFIXES: frozenset[str] = frozenset([
    "股份", "科技", "电子", "材料", "有限公司", "集团", " Corp", " Inc", " Ltd",
    "博通", "迈威尔", "史陶比尔", "华为", "腾讯", "阿里", "平头哥", "海思", "住友",
])


def _parse_ts(ts: str) -> datetime | None:
    """解析时间戳。"""
    ts = _text(ts)
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except (TypeError, ValueError):
        return None


@dataclass
class ConflictRecord:
    """冲突记录。"""

    conflict_type: str
    contradiction_group_id: str
    primary_claim_id: str
    conflicting_claim_ids: list[str]
    resolution_reason: str
    suggested_status: str
    suggested_status_for_others: dict[str, str]


class ContradictionDetector:
    """Claim 冲突检测器。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        entity_resolver: EntityResolver | None = None,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库
            entity_resolver: 实体解析器，None 则自动新建
        """
        self.kb = kb
        self.entity_resolver = entity_resolver or EntityResolver()

    @staticmethod
    def _group_id(*claim_ids: str) -> str:
        """根据 claim id 生成稳定的 contradiction group id。"""
        stable = "|".join(sorted(_claim_id(claim_id) for claim_id in claim_ids))
        return "cg-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:10]

    def detect_for_claim(self, new_claim: Claim) -> list[ConflictRecord]:
        """检测新 claim 与已有 claim 的冲突。

        Args:
            new_claim: 新 claim（已加入 kb）

        Returns:
            冲突记录列表
        """
        if not isinstance(new_claim, Claim):
            return []
        records: list[ConflictRecord] = []
        for other in self._candidate_others(new_claim):
            record = self._detect_pair(new_claim, other)
            if record:
                records.append(record)
        return records

    def detect_all(self, use_partition: bool = True) -> list[ConflictRecord]:
        """全量扫描 KB 中的冲突。

        当 claim 数量超过阈值时，按 edge 分组进行分区检测以降低复杂度。

        Args:
            use_partition: 是否启用分区检测

        Returns:
            冲突记录列表
        """
        records: list[ConflictRecord] = []
        seen_pairs: set[tuple[str, str]] = set()
        claims_map = getattr(self.kb, "claims", {})
        claims = [claim for claim in claims_map.values() if isinstance(claim, Claim)] if isinstance(claims_map, Mapping) else []

        # 小规模 KB 直接 O(n^2) 扫描
        if not use_partition or len(claims) <= 10000:
            for i, a in enumerate(claims):
                for b in claims[i + 1 :]:
                    pair = tuple(sorted([_claim_id(a.claim_id), _claim_id(b.claim_id)]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    record = self._detect_pair(a, b)
                    if record:
                        records.append(record)
            return records

        # 大规模 KB：按 edge 分组检测
        # 1. 同 (head, relation, tail) 的 claim 之间可能冲突
        edge_groups: dict[tuple[str, str, str], list[Claim]] = {}
        for claim in claims:
            key = (_text(claim.head), _text(claim.relation), _text(claim.tail))
            edge_groups.setdefault(key, []).append(claim)

        for group_claims in edge_groups.values():
            for i, a in enumerate(group_claims):
                for b in group_claims[i + 1 :]:
                    pair = tuple(sorted([_claim_id(a.claim_id), _claim_id(b.claim_id)]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    record = self._detect_pair(a, b)
                    if record:
                        records.append(record)

        # 2. 相同 head 的 supplied_by / import_dependent_on 可能供应商冲突
        supplier_groups: dict[str, list[Claim]] = {}
        for claim in claims:
            relation = _text(claim.relation)
            if relation in ("supplied_by", "import_dependent_on"):
                supplier_groups.setdefault(_text(claim.head), []).append(claim)

        for group_claims in supplier_groups.values():
            for i, a in enumerate(group_claims):
                for b in group_claims[i + 1 :]:
                    pair = tuple(sorted([_claim_id(a.claim_id), _claim_id(b.claim_id)]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    record = self._detect_pair(a, b)
                    if record:
                        records.append(record)

        return records

    def _candidate_others(self, new_claim: Claim) -> list[Claim]:
        """收集可能与新 claim 冲突的候选 claim。"""
        if not isinstance(new_claim, Claim):
            return []
        candidates: list[Claim] = []
        edges = getattr(self.kb, "edges", {})
        claims = getattr(self.kb, "claims", {})
        edges = edges.values() if isinstance(edges, Mapping) else []
        claims = claims if isinstance(claims, Mapping) else {}
        for edge in edges:
            # 同 edge
            claim_ids = _items(_field(edge, "claim_ids"))
            new_claim_id = _claim_id(new_claim.claim_id)
            if new_claim_id in {_claim_id(cid) for cid in claim_ids}:
                for cid in claim_ids:
                    if _claim_id(cid) != new_claim_id:
                        other = _mapping_get(claims, cid)
                        if isinstance(other, Claim):
                            candidates.append(other)

        # 相同 head 的 supplied_by / import_dependent_on 可能供应商冲突
        edges = getattr(self.kb, "edges", {})
        edges = edges.values() if isinstance(edges, Mapping) else []
        for edge in edges:
            if _text(_field(edge, "head")) == _text(new_claim.head) and _text(_field(edge, "relation")) in (
                "supplied_by",
                "import_dependent_on",
            ):
                for cid in _items(_field(edge, "claim_ids")):
                    other = _mapping_get(claims, cid)
                    if isinstance(other, Claim) and _claim_id(other.claim_id) != _claim_id(new_claim.claim_id):
                        candidates.append(other)

        # 去重
        seen: set[str] = set()
        unique: list[Claim] = []
        for c in candidates:
            cid = _claim_id(c.claim_id)
            if cid not in seen:
                seen.add(cid)
                unique.append(c)
        return unique

    def _detect_pair(self, a: Claim, b: Claim) -> ConflictRecord | None:
        """检测两条 claim 的冲突。"""
        if not isinstance(a, Claim) or not isinstance(b, Claim):
            return None
        if _claim_id(a.claim_id) == _claim_id(b.claim_id):
            return None

        # exclusivity_conflict
        if self._is_exclusivity_conflict(a, b):
            return self._build_record("exclusivity_conflict", a, b)

        # supplier_conflict
        if self._is_supplier_conflict(a, b):
            return self._build_record("supplier_conflict", a, b)

        # relation_conflict
        if self._is_relation_conflict(a, b):
            return self._build_record("relation_conflict", a, b)

        # time_conflict
        if self._is_time_conflict(a, b):
            return self._build_record("time_conflict", a, b)

        # entity_conflict
        if self._is_entity_conflict(a, b):
            return self._build_record("entity_conflict", a, b)

        # source_conflict
        if self._is_source_conflict(a, b):
            return self._build_record("source_conflict", a, b)

        return None

    def _is_exclusivity_conflict(self, a: Claim, b: Claim) -> bool:
        """A 是唯一供应商 vs B 也是供应商。"""
        if _text(a.relation) not in ("supplied_by", "import_dependent_on"):
            return False
        if _text(b.relation) not in ("supplied_by", "import_dependent_on"):
            return False
        if _text(a.head) != _text(b.head):
            return False
        if _text(a.tail) == _text(b.tail):
            return False
        a_evidence = _text(a.evidence).lower()
        b_evidence = _text(b.evidence).lower()
        a_excl = any(m.lower() in a_evidence for m in EXCLUSIVITY_MARKERS)
        b_excl = any(m.lower() in b_evidence for m in EXCLUSIVITY_MARKERS)
        return a_excl or b_excl

    def _is_supplier_conflict(self, a: Claim, b: Claim) -> bool:
        """同一产品不同供应商且语义互斥。"""
        if _text(a.relation) not in ("supplied_by", "import_dependent_on"):
            return False
        if _text(b.relation) not in ("supplied_by", "import_dependent_on"):
            return False
        if _text(a.head) != _text(b.head):
            return False
        if _text(a.tail) == _text(b.tail):
            return False
        # 如果两者都来自较高权重来源，才判定为冲突
        a_weight = get_source_weight(_text(a.source_type))
        b_weight = get_source_weight(_text(b.source_type))
        return a_weight >= 0.65 and b_weight >= 0.65

    def _is_relation_conflict(self, a: Claim, b: Claim) -> bool:
        """同一 (head, tail) 但关系语义互斥。"""
        if _text(a.head) != _text(b.head) or _text(a.tail) != _text(b.tail):
            return False
        a_relation = _text(a.relation)
        b_relation = _text(b.relation)
        if a_relation == b_relation:
            return False
        # 互斥关系对
        mutex_pairs: set[frozenset[str]] = {
            frozenset(["made_of", "substitute_for"]),
            frozenset(["supplied_by", "import_dependent_on"]),
            frozenset(["contains", "substitute_for"]),
        }
        return frozenset([a_relation, b_relation]) in mutex_pairs

    def _is_time_conflict(self, a: Claim, b: Claim) -> bool:
        """同一 claim 内容但时间相差较大，且新来源权重高。"""
        if not isinstance(a, Claim) or not isinstance(b, Claim):
            return False
        if _text(a.head) != _text(b.head) or _text(a.relation) != _text(b.relation) or _text(a.tail) != _text(b.tail):
            return False
        ta = _parse_ts(a.timestamp)
        tb = _parse_ts(b.timestamp)
        if not ta or not tb:
            return False
        diff_days = abs((ta - tb).days)
        if diff_days < 180:
            return False
        # 至少一个高权重来源
        return get_source_weight(_text(a.source_type)) >= 0.85 or get_source_weight(_text(b.source_type)) >= 0.85

    def _is_entity_conflict(self, a: Claim, b: Claim) -> bool:
        """同名实体实际不是同一对象（基于 EntityResolver）。"""
        # 如果 head/tail 完全相同，不认为 entity_conflict
        if _text(a.head) == _text(b.head) and _text(a.tail) == _text(b.tail):
            return False

        same_head = _text(a.head) == _text(b.head) and _text(a.tail) != _text(b.tail)
        same_tail = _text(a.tail) == _text(b.tail) and _text(a.head) != _text(b.head)
        if not same_head and not same_tail:
            return False

        # 使用 EntityResolver 解析实体类型
        if same_head:
            resolve_values = (_text(a.tail), _text(b.tail))
        else:
            resolve_values = (_text(a.head), _text(b.head))
        try:
            a_resolved = self.entity_resolver.resolve(resolve_values[0])
            b_resolved = self.entity_resolver.resolve(resolve_values[1])
        except (AttributeError, TypeError, ValueError):
            return False

        a_type = _text(_field(a_resolved, "entity_type"), "unknown") or "unknown"
        b_type = _text(_field(b_resolved, "entity_type"), "unknown") or "unknown"

        # 如果两边都是泛概念或未知，不判定冲突
        if a_type in ("generic_term", "unknown") and b_type in ("generic_term", "unknown"):
            return False

        # entity_type 不同则判定冲突
        if a_type != b_type:
            return True

        # 同名公司但 listed_status 明显不同且无母子关系，也判定冲突
        if a_type == "company" and b_type == "company":
            a_listed = _text(_field(a_resolved, "listed_status"))
            b_listed = _text(_field(b_resolved, "listed_status"))
            if a_listed != b_listed:
                if a_listed in ("a_share", "hk_share", "us_share") and b_listed in ("a_share", "hk_share", "us_share"):
                    # 同为上市公司但不同市场，可能同名异地上市，不冲突
                    return False
                a_parent = _text(_field(a_resolved, "parent_company"))
                b_parent = _text(_field(b_resolved, "parent_company"))
                if a_parent and a_parent == b_parent:
                    return False
                a_name = _text(_field(a_resolved, "canonical_name"))
                b_name = _text(_field(b_resolved, "canonical_name"))
                if a_name and b_name and a_name != b_name:
                    return True

        return False

    def _is_source_conflict(self, a: Claim, b: Claim) -> bool:
        """低质量来源与高质量来源结论相反，或两个高权重来源对同一事实给出不同证据。"""
        if _text(a.head) != _text(b.head) or _text(a.relation) != _text(b.relation) or _text(a.tail) != _text(b.tail):
            return False
        if _text(a.evidence) == _text(b.evidence):
            return False
        a_weight = get_source_weight(_text(a.source_type))
        b_weight = get_source_weight(_text(b.source_type))
        low, high = sorted([a_weight, b_weight])
        # 低质量 vs 高质量：低质量来源应被取代
        if low < 0.5 and high >= 0.85:
            return True
        # 两个高权重来源出现不同证据：需要人工复核
        if low >= 0.85 and high >= 0.85:
            return True
        return False

    def _build_record(self, conflict_type: str, a: Claim, b: Claim) -> ConflictRecord:
        """构建冲突记录。"""
        group_id = self._group_id(a.claim_id, b.claim_id)
        a_weight = get_source_weight(_text(a.source_type))
        b_weight = get_source_weight(_text(b.source_type))

        # 默认：高权重保留，低权重 contradicted
        if a_weight > b_weight:
            primary = a
            other = b
        elif b_weight > a_weight:
            primary = b
            other = a
        else:
            # 同权重时，新 claim 作为 primary
            ta = _parse_ts(a.timestamp) or datetime.min
            tb = _parse_ts(b.timestamp) or datetime.min
            if tb >= ta:
                primary = b
                other = a
            else:
                primary = a
                other = b

        primary_id = _claim_id(primary.claim_id)
        other_id = _claim_id(other.claim_id)
        suggested_for_others = {other_id: ClaimStatus.CONTRADICTED.value}
        reason = f"{_text(conflict_type)}: {primary_id} ({_text(primary.source_type)}) vs {other_id} ({_text(other.source_type)})"

        # 双方高权重 → needs_review
        if a_weight >= 0.85 and b_weight >= 0.85:
            primary_status = ClaimStatus.NEEDS_REVIEW.value
            suggested_for_others = {other_id: ClaimStatus.NEEDS_REVIEW.value}
            reason += "；双方均为高权重来源，进入 needs_review"
        elif conflict_type == "source_conflict":
            # 低质量来源被高质量来源取代，标记为 deprecated 而非 contradicted
            primary_status = primary.status if primary.status != ClaimStatus.CONTRADICTED.value else ClaimStatus.SUPPORTED.value
            suggested_for_others = {other_id: ClaimStatus.DEPRECATED.value}
            reason += "；低质量来源被高质量来源取代，标记 deprecated"
        else:
            primary_status = primary.status if primary.status != ClaimStatus.CONTRADICTED.value else ClaimStatus.SUPPORTED.value

        return ConflictRecord(
            conflict_type=conflict_type,
            contradiction_group_id=group_id,
            primary_claim_id=primary_id,
            conflicting_claim_ids=[other_id],
            resolution_reason=reason,
            suggested_status=primary_status,
            suggested_status_for_others=suggested_for_others,
        )

    def resolve_conflict_group(self, group_id: str) -> dict[str, Any]:
        """按 source weight + timestamp + claim_score 仲裁冲突组。

        Args:
            group_id: 冲突组 ID

        Returns:
            仲裁结果
        """
        group_id = _text(group_id)
        group_claims: list[Claim] = []
        claims = getattr(self.kb, "claims", {})
        claims = claims.values() if isinstance(claims, Mapping) else []
        for claim in claims:
            if isinstance(claim, Claim) and claim.contradiction_group_id == group_id:
                group_claims.append(claim)

        if not group_claims:
            return {"group_id": group_id, "resolved": False, "reason": "未找到冲突组"}

        def _claim_priority(claim: Claim) -> tuple[float, datetime]:
            weight = get_source_weight(_text(claim.source_type))
            ts = _parse_ts(claim.timestamp) or datetime.min
            return (weight + _score(claim.claim_score), ts)

        group_claims.sort(key=_claim_priority, reverse=True)
        winner = group_claims[0]
        changed: list[str] = []

        for claim in group_claims[1:]:
            if claim.status != ClaimStatus.CONTRADICTED.value:
                try:
                    self.kb.update_claim_status(
                    claim_id=claim.claim_id,
                    new_status=ClaimStatus.CONTRADICTED.value,
                    reason=f"冲突组 {group_id} 仲裁失败：{winner.claim_id} 来源/评分更优",
                    trigger_claim_id=winner.claim_id,
                    changed_by="rule",
                    )
                except (AttributeError, KeyError, TypeError, ValueError):
                    logger.warning("failed to update contradiction group status", exc_info=True)
                    continue
                changed.append(_claim_id(claim.claim_id))

        return {
            "group_id": group_id,
            "resolved": True,
            "winner_claim_id": winner.claim_id,
            "changed_claim_ids": changed,
        }

    def mark_deprecated(self, old_claim: Claim, new_claim: Claim) -> None:
        """新公告/年报/官网推翻旧 claim 时使用。"""
        if not isinstance(old_claim, Claim) or not isinstance(new_claim, Claim):
            return
        try:
            self.kb.update_claim_status(
                claim_id=old_claim.claim_id,
                new_status=ClaimStatus.DEPRECATED.value,
                reason=f"被新来源 {_text(new_claim.source_type)} 推翻",
                trigger_claim_id=new_claim.claim_id,
                changed_by="rule",
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.warning("failed to deprecate superseded claim", exc_info=True)
        old_claim.superseded_by_claim_id = new_claim.claim_id
        old_claim.conflict_status = "deprecated"
        old_claim.valid_to = _text(new_claim.timestamp) or _now_iso()

    def apply_records(self, records: list[ConflictRecord]) -> dict[str, Any]:
        """将冲突记录应用到 KB。"""
        applied = 0
        claims = getattr(self.kb, "claims", {})
        claims = claims if isinstance(claims, Mapping) else {}
        for record in _items(records):
            if not isinstance(record, ConflictRecord) or not record.conflicting_claim_ids:
                continue
            primary = _mapping_get(claims, record.primary_claim_id)
            if not isinstance(primary, Claim):
                continue

            # 更新 primary 冲突标记
            primary.contradiction_group_id = record.contradiction_group_id
            primary.conflict_status = "conflicted"
            primary_contradicts = _items(getattr(primary, "contradicts_claim_ids", None))
            primary_id = _claim_id(record.conflicting_claim_ids[0])
            if primary_id not in {_claim_id(item) for item in primary_contradicts}:
                primary_contradicts.append(primary_id)
            primary.contradicts_claim_ids = primary_contradicts

            # 应用建议状态
            if primary.status != record.suggested_status:
                self.kb.update_claim_status(
                    claim_id=primary.claim_id,
                    new_status=record.suggested_status,
                    reason=record.resolution_reason,
                    trigger_claim_id=record.conflicting_claim_ids[0],
                    changed_by="rule",
                )

            for other_id, status in _mapping(record.suggested_status_for_others).items():
                other = _mapping_get(claims, other_id)
                if not isinstance(other, Claim):
                    continue
                other.contradiction_group_id = record.contradiction_group_id
                other.conflict_status = "conflicted"
                other_contradicts = _items(getattr(other, "contradicts_claim_ids", None))
                primary_id = _claim_id(primary.claim_id)
                if primary_id not in {_claim_id(item) for item in other_contradicts}:
                    other_contradicts.append(primary_id)
                other.contradicts_claim_ids = other_contradicts
                if other.status != status:
                    self.kb.update_claim_status(
                        claim_id=other.claim_id,
                        new_status=status,
                        reason=record.resolution_reason,
                        trigger_claim_id=primary.claim_id,
                        changed_by="rule",
                    )

            # time_conflict：新来源权重 >= 0.85 时，将旧 claim 标记为 deprecated
            if record.conflict_type == "time_conflict":
                other = _mapping_get(claims, record.conflicting_claim_ids[0])
                if isinstance(other, Claim):
                    old_claim, new_claim = primary, other
                    primary_ts = _parse_ts(primary.timestamp)
                    other_ts = _parse_ts(other.timestamp)
                    if primary_ts and other_ts and primary_ts > other_ts:
                        old_claim, new_claim = other, primary
                    if get_source_weight(_text(new_claim.source_type)) >= 0.85:
                        self.mark_deprecated(old_claim, new_claim)

            applied += 1

        return {"applied_records": applied}

    # ------------------------------------------------------------------
    # Event graph conflict detection (new API)
    # ------------------------------------------------------------------

    def detect_event_vs_stable(
        self,
        event_claim: EventClaim,
    ) -> list[EventConflictRecord]:
        """检测事件 claim 与稳定图 claim 之间的方向性冲突。

        例如：事件说国产替代进展，但稳定图仍标记为 import_dependent_on。

        Args:
            event_claim: 待检测事件 claim。

        Returns:
            EventConflictRecord 列表。
        """
        if not isinstance(event_claim, EventClaim):
            return []
        records: list[EventConflictRecord] = []
        subject = _text(event_claim.subject)
        obj = _text(event_claim.object)
        event_type = _text(event_claim.event_type)

        claims = getattr(self.kb, "claims", {})
        claims = claims.values() if isinstance(claims, Mapping) else []
        for claim in claims:
            if not isinstance(claim, Claim):
                continue
            if _text(claim.head) != subject or _text(claim.tail) != obj:
                continue
            if not _event_opposes_stable_relation(event_type, _text(claim.relation)):
                continue

            pressure = _contradiction_pressure_for_event(event_claim)
            records.append(
                EventConflictRecord(
                    conflict_type="event_vs_stable_relation",
                    event_claim_id=_claim_id(event_claim.event_id),
                    conflicting_claim_id=_claim_id(claim.claim_id),
                    affected_stable_edge_keys=[(_text(claim.head), _text(claim.relation), _text(claim.tail))],
                    affected_paths=[],
                    contradiction_pressure=pressure,
                    required_evidence_probe=[
                        "验证事件与稳定关系的时间窗口",
                        "确认方向性是否已发生变化",
                    ],
                    resolution_reason=(
                        f"事件 {event_type} 与稳定关系 {_text(claim.relation)} "
                        f"在 ({subject}, {obj}) 上方向冲突"
                    ),
                    time_window=event_claim.time or event_claim.source_time,
                )
            )

        return records

    def detect_event_vs_event(
        self,
        event_claim: EventClaim,
    ) -> list[EventConflictRecord]:
        """检测事件 claim 与其他事件 claim 之间的语义冲突。

        当前覆盖：
        - capacity_expansion vs supply_shortage（同一时间窗口）
        - domestic_substitution_progress vs import_dependency_signal

        Args:
            event_claim: 待检测事件 claim。

        Returns:
            EventConflictRecord 列表。
        """
        if not isinstance(event_claim, EventClaim):
            return []
        records: list[EventConflictRecord] = []
        event_claims = getattr(self.kb, "event_claims", {})
        event_claims = event_claims.values() if isinstance(event_claims, Mapping) else []
        for other in event_claims:
            if not isinstance(other, EventClaim):
                continue
            if _claim_id(other.event_id) == _claim_id(event_claim.event_id):
                continue
            if not _is_opposite_event_pair(event_claim.event_type, other.event_type):
                continue
            # 需要主体/对象相同或互换
            same_entities = (
                _text(event_claim.subject) == _text(other.subject)
                and _text(event_claim.object) == _text(other.object)
            ) or (
                _text(event_claim.subject) == _text(other.object)
                and _text(event_claim.object) == _text(other.subject)
            )
            if not same_entities:
                continue
            if not _within_days(event_claim, other, days=90):
                continue

            pressure = max(
                _contradiction_pressure_for_event(event_claim),
                _contradiction_pressure_for_event(other),
            )
            records.append(
                EventConflictRecord(
                    conflict_type="event_vs_event",
                    event_claim_id=_claim_id(event_claim.event_id),
                    conflicting_claim_id=_claim_id(other.event_id),
                    affected_stable_edge_keys=[],
                    affected_paths=[],
                    contradiction_pressure=pressure,
                    required_evidence_probe=[
                        "核实两条事件来源的发布时间",
                        "确认哪条事件更符合当前事实",
                    ],
                    resolution_reason=(
                        f"事件 {event_claim.event_type} 与 {other.event_type} "
                        f"在 ({event_claim.subject}, {event_claim.object}) 上语义冲突"
                    ),
                    time_window=f"{event_claim.time or event_claim.source_time} vs {other.time or other.source_time}",
                )
            )

        return records

    def apply_event_conflict_records(
        self,
        records: list[EventConflictRecord],
    ) -> dict[str, Any]:
        """将事件冲突记录应用到 KB。

        只标记 event_claim.contradiction_candidate / status，不删除、不直接修改稳定图。

        Args:
            records: EventConflictRecord 列表。

        Returns:
            {"applied_records": int, "contradicted_event_ids": list[str]}
        """
        applied = 0
        contradicted_ids: list[str] = []
        event_claims = getattr(self.kb, "event_claims", {})
        event_claims = event_claims if isinstance(event_claims, Mapping) else {}
        for record in _items(records):
            if not isinstance(record, EventConflictRecord):
                continue
            claim = _mapping_get(event_claims, record.event_claim_id)
            if not isinstance(claim, EventClaim):
                continue

            claim.contradiction_candidate = True
            applied += 1

            # 压力足够大时将事件标记为 CONTRADICTED
            if (
                _score(record.contradiction_pressure) >= 0.3
                and claim.status
                not in (
                    EventClaimStatus.CONTRADICTED.value,
                    EventClaimStatus.RETRACTED.value,
                )
            ):
                claim.status = EventClaimStatus.CONTRADICTED.value
                status_history = _items(_field(claim, "status_history"))
                status_history.append(
                    {
                        "status": EventClaimStatus.CONTRADICTED.value,
                        "reason": record.resolution_reason,
                        "changed_by": "rule",
                        "changed_at": _now_iso(),
                    }
                )
                claim.status_history = status_history[-50:]
                contradicted_ids.append(claim.event_id)

                # 同步更新对应 EventEdge 的冲突标记
                edge_key = (claim.subject, claim.event_type, claim.object)
                event_edges = getattr(self.kb, "event_edges", {})
                event_edges = event_edges if isinstance(event_edges, Mapping) else {}
                event_edge = _mapping_get(event_edges, edge_key)
                if event_edge is not None:
                    if isinstance(event_edge, MutableMapping):
                        event_edge["conflict_status"] = "conflicted"
                        ids = _items(event_edge.get("contradicting_claim_ids"))
                        if _claim_id(claim.event_id) not in {_claim_id(item) for item in ids}:
                            ids.append(_claim_id(claim.event_id))
                        event_edge["contradicting_claim_ids"] = ids
                    else:
                        event_edge.conflict_status = "conflicted"
                        ids = _items(_field(event_edge, "contradicting_claim_ids"))
                        if _claim_id(claim.event_id) not in {_claim_id(item) for item in ids}:
                            ids.append(_claim_id(claim.event_id))
                        event_edge.contradicting_claim_ids = ids

        return {
            "applied_records": applied,
            "contradicted_event_ids": contradicted_ids,
        }

    # ------------------------------------------------------------------
    # Path-level contradiction detection (new API)
    # ------------------------------------------------------------------

    def detect_path_contradictions(
        self,
        bundle: PathEvidenceBundle,
    ) -> list[PathContradictionRecord]:
        """检测路径证据包中的路径级冲突。

        Args:
            bundle: 路径证据包。

        Returns:
            PathContradictionRecord 列表。
        """
        if not isinstance(bundle, PathEvidenceBundle):
            return []
        records: list[PathContradictionRecord] = []
        candidate = bundle.path_candidate
        if not isinstance(candidate, PathCandidate):
            return records
        path_id = _text(candidate.path_id)

        # 1. Edge-level opposite evidence on the same (head, relation, tail)
        edges = getattr(self.kb, "edges", {})
        claims = getattr(self.kb, "claims", {})
        edges = edges if isinstance(edges, Mapping) else {}
        claims = claims if isinstance(claims, Mapping) else {}
        for edge_key in _items(candidate.edges):
            normalized_edge = _edge_tuple(edge_key)
            if normalized_edge is None:
                continue
            edge = _mapping_get(edges, normalized_edge)
            if not edge:
                continue
            edge_claims = [
                claim
                for cid in _items(_field(edge, "claim_ids"))
                for claim in [_mapping_get(claims, cid)]
                if isinstance(claim, Claim)
            ]
            if len(edge_claims) < 2:
                continue

            for i, a in enumerate(edge_claims):
                for b in edge_claims[i + 1 :]:
                    if self._is_opposite_evidence(a, b):
                        group_id = self._path_group_id([path_id])
                        records.append(PathContradictionRecord(
                            contradiction_type="same_edge_opposite_evidence",
                            contradiction_group_id=group_id,
                            affected_claims=[_claim_id(a.claim_id), _claim_id(b.claim_id)],
                            affected_edges=[normalized_edge],
                            affected_paths=[path_id],
                            severity="high",
                            recommended_action="require_more_evidence",
                            resolution_reason=f"Opposite evidence on {edge_key}: {a.claim_id} vs {b.claim_id}",
                        ))

        # 2. Event vs stable directional conflicts
        for event_claim in _items(bundle.event_claims):
            if not isinstance(event_claim, EventClaim):
                continue
            record = self.detect_event_vs_stable_path_conflict(event_claim, candidate)
            if record:
                records.append(record)

        # 3. Semantic conflict markers in evidence text
        semantic_records = self._detect_semantic_path_conflicts(bundle)
        records.extend(semantic_records)

        return records

    def detect_claim_vs_path_conflict(
        self,
        claim: Claim,
        path: PathCandidate,
    ) -> PathContradictionRecord | None:
        """检测单条 claim 与路径之间的冲突。"""
        if not isinstance(claim, Claim) or not isinstance(path, PathCandidate):
            return None
        claim_edge = (_text(claim.head), _text(claim.relation), _text(claim.tail))
        path_edges = {
            normalized_edge
            for edge in _items(path.edges)
            for normalized_edge in [_edge_tuple(edge)]
            if normalized_edge is not None
        }
        if claim_edge not in path_edges:
            return None

        edges = getattr(self.kb, "edges", {})
        claims = getattr(self.kb, "claims", {})
        edges = edges if isinstance(edges, Mapping) else {}
        claims = claims if isinstance(claims, Mapping) else {}
        edge = _mapping_get(edges, claim_edge)
        if not edge:
            return None

        for cid in _items(_field(edge, "claim_ids")):
            if _claim_id(cid) == _claim_id(claim.claim_id):
                continue
            other = _mapping_get(claims, cid)
            if not isinstance(other, Claim):
                continue
            if self._is_opposite_evidence(claim, other):
                return PathContradictionRecord(
                    contradiction_type="claim_vs_path_opposite_evidence",
                    contradiction_group_id=self._path_group_id([path.path_id]),
                    affected_claims=[_claim_id(claim.claim_id), _claim_id(other.claim_id)],
                    affected_edges=[claim_edge],
                    affected_paths=[path.path_id],
                    severity="high",
                    recommended_action="require_more_evidence",
                    resolution_reason=f"Claim {_claim_id(claim.claim_id)} conflicts with {_claim_id(other.claim_id)} on {claim_edge}",
                )
        return None

    def detect_event_vs_stable_path_conflict(
        self,
        event_claim: EventClaim,
        stable_path: PathCandidate,
    ) -> PathContradictionRecord | None:
        """检测事件 claim 与稳定路径之间的方向性冲突。"""
        if not isinstance(event_claim, EventClaim) or not isinstance(stable_path, PathCandidate):
            return None

        subject = _text(event_claim.subject)
        obj = _text(event_claim.object)
        event_type = _text(event_claim.event_type)

        for edge_key in _items(stable_path.edges):
            normalized_edge = _edge_tuple(edge_key)
            if normalized_edge is None:
                continue
            head, relation, tail = normalized_edge
            if head != subject or tail != obj:
                continue
            if not _event_opposes_stable_relation(event_type, relation):
                continue

            return PathContradictionRecord(
                contradiction_type="event_vs_stable_path_conflict",
                contradiction_group_id=self._path_group_id([_text(stable_path.path_id)]),
                affected_claims=[_claim_id(event_claim.event_id)],
                affected_edges=[normalized_edge],
                affected_paths=[_text(stable_path.path_id)],
                severity="high",
                recommended_action="keep_both_with_time_window",
                resolution_reason=(
                    f"Event {event_type} opposes stable relation {relation} "
                    f"on ({subject}, {obj})"
                ),
                time_window=_text(event_claim.time) or _text(event_claim.source_time),
            )
        return None

    def detect_ontology_violation(
        self,
        path_candidate: PathCandidate,
    ) -> PathContradictionRecord | None:
        """检测路径是否违反稳定 ontology。"""
        if not isinstance(path_candidate, PathCandidate):
            return None
        valid_relations = {r.value for r in RelationType}
        for edge_key in _items(path_candidate.edges):
            if not isinstance(edge_key, (list, tuple)) or len(edge_key) != 3:
                return PathContradictionRecord(
                    contradiction_type="ontology_violation",
                    contradiction_group_id=self._path_group_id([_text(path_candidate.path_id)]),
                    affected_claims=[],
                    affected_edges=[],
                    affected_paths=[_text(path_candidate.path_id)],
                    severity="high",
                    recommended_action="reject",
                    resolution_reason="malformed path edge",
                )
            normalized_edge = _edge_tuple(edge_key)
            relation = normalized_edge[1] if normalized_edge is not None else ""
            if relation not in valid_relations:
                return PathContradictionRecord(
                    contradiction_type="ontology_violation",
                    contradiction_group_id=self._path_group_id([path_candidate.path_id]),
                    affected_claims=[],
                    affected_edges=[normalized_edge] if normalized_edge is not None else [],
                    affected_paths=[_text(path_candidate.path_id)],
                    severity="high",
                    recommended_action="reject",
                    resolution_reason=f"Relation {relation} not in stable ontology",
                )

        promotable_types = {
            "supplier_dependency_path",
            "material_bottleneck_path",
            "equipment_bottleneck_path",
            "process_barrier_path",
            "certification_barrier_path",
            "domestic_substitution_path",
            "import_dependency_path",
        }
        path_type = _text(path_candidate.path_type, "unknown") or "unknown"
        if path_type not in promotable_types and path_type != "unknown":
            return PathContradictionRecord(
                contradiction_type="ontology_violation",
                contradiction_group_id=self._path_group_id([path_candidate.path_id]),
                affected_claims=[],
                affected_edges=[],
                affected_paths=[_text(path_candidate.path_id)],
                severity="medium",
                recommended_action="reject",
                resolution_reason=f"path_type={path_type} not in promotable ontology",
            )
        return None

    @staticmethod
    def _path_group_id(path_ids: list[str]) -> str:
        """根据 path id 生成稳定的冲突组 id。"""
        stable = "|".join(sorted(_text(path_id) for path_id in path_ids))
        return "pcg-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:10]

    def _detect_semantic_path_conflicts(
        self,
        bundle: PathEvidenceBundle,
    ) -> list[PathContradictionRecord]:
        """基于证据文本中的语义标记检测路径级冲突。"""
        if not isinstance(bundle, PathEvidenceBundle):
            return []
        records: list[PathContradictionRecord] = []
        candidate = bundle.path_candidate
        if not isinstance(candidate, PathCandidate):
            return records
        path_id = _text(candidate.path_id)

        # 检测 product_launched vs no_mass_production
        launched = False
        no_mass = False
        launched_claims: list[str] = []
        no_mass_claims: list[str] = []
        chunks = [chunk for chunk in _items(bundle.evidence_chunks) if isinstance(chunk, Mapping)]
        for chunk in chunks:
            text = _text(chunk.get("evidence"))
            cid = _text(chunk.get("claim_id"))
            if "发布" in text or "推出" in text or "上市" in text or "量产" in text:
                launched = True
                launched_claims.append(cid)
            if "未量产" in text or "未规模" in text or "未量产" in text or "小批量" in text:
                no_mass = True
                no_mass_claims.append(cid)

        if launched and no_mass:
            records.append(PathContradictionRecord(
                contradiction_type="product_launched_vs_no_mass_production",
                contradiction_group_id=self._path_group_id([path_id]),
                affected_claims=launched_claims + no_mass_claims,
                affected_edges=[],
                affected_paths=[path_id],
                severity="medium",
                recommended_action="require_more_evidence",
                resolution_reason="Evidence suggests both product launch and no mass production",
            ))

        # 检测 customer_certified vs failed_certification
        certified = False
        failed = False
        certified_claims: list[str] = []
        failed_claims: list[str] = []
        for chunk in chunks:
            text = _text(chunk.get("evidence"))
            cid = _text(chunk.get("claim_id"))
            if "通过认证" in text or "获得认证" in text or "客户认证" in text:
                certified = True
                certified_claims.append(cid)
            if "认证失败" in text or "未通过认证" in text or "未获认证" in text:
                failed = True
                failed_claims.append(cid)

        if certified and failed:
            records.append(PathContradictionRecord(
                contradiction_type="customer_certified_vs_failed_certification",
                contradiction_group_id=self._path_group_id([path_id]),
                affected_claims=certified_claims + failed_claims,
                affected_edges=[],
                affected_paths=[path_id],
                severity="high",
                recommended_action="require_more_evidence",
                resolution_reason="Evidence suggests both certification success and failure",
            ))

        # 检测 supplier_of vs rumor_only（低来源权重）
        edges = getattr(self.kb, "edges", {})
        claims = getattr(self.kb, "claims", {})
        edges = edges if isinstance(edges, Mapping) else {}
        claims = claims if isinstance(claims, Mapping) else {}
        for edge_key in _items(candidate.edges):
            normalized_edge = _edge_tuple(edge_key)
            if normalized_edge is None:
                continue
            edge = _mapping_get(edges, normalized_edge)
            if not edge:
                continue
            rumor_claims: list[str] = []
            solid_claims: list[str] = []
            for cid in _items(_field(edge, "claim_ids")):
                claim = _mapping_get(claims, cid)
                if not isinstance(claim, Claim):
                    continue
                if get_source_weight(_text(claim.source_type)) < 0.4:
                    rumor_claims.append(_claim_id(cid))
                elif get_source_weight(_text(claim.source_type)) >= 0.65:
                    solid_claims.append(_claim_id(cid))
            if rumor_claims and not solid_claims and len(rumor_claims) >= 2:
                records.append(PathContradictionRecord(
                    contradiction_type="supplier_vs_rumor_only",
                    contradiction_group_id=self._path_group_id([path_id]),
                    affected_claims=rumor_claims,
                    affected_edges=[normalized_edge],
                    affected_paths=[path_id],
                    severity="low",
                    recommended_action="downgrade",
                    resolution_reason=f"Edge {normalized_edge} supported only by low-reliability rumor sources",
                ))

        return records

    def _is_opposite_evidence(self, a: Claim, b: Claim) -> bool:
        """判断两条 claim 是否表达同一边上的正反证据。"""
        if not isinstance(a, Claim) or not isinstance(b, Claim):
            return False
        if a.head != b.head or a.relation != b.relation or a.tail != b.tail:
            return False
        if a.evidence == b.evidence:
            return False
        a_weight = get_source_weight(_text(a.source_type))
        b_weight = get_source_weight(_text(b.source_type))
        # 来源权重差异大且证据文本不同，视为潜在冲突
        if abs(a_weight - b_weight) > 0.4:
            return True
        # 低质量来源与高质量来源结论相反
        if min(a_weight, b_weight) < 0.5 and max(a_weight, b_weight) >= 0.85:
            return True
        return False


@dataclass
class EventConflictRecord:
    """事件图与稳定图/事件图之间的冲突记录。"""

    conflict_type: str
    event_claim_id: str
    conflicting_claim_id: str | None
    affected_stable_edge_keys: list[tuple[str, str, str]]
    affected_paths: list[list[str]]
    contradiction_pressure: float
    required_evidence_probe: list[str]
    resolution_reason: str
    time_window: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "conflict_type": self.conflict_type,
            "event_claim_id": self.event_claim_id,
            "conflicting_claim_id": self.conflicting_claim_id,
            "affected_stable_edge_keys": [
                list(e) for e in _items(self.affected_stable_edge_keys)
                if isinstance(e, (list, tuple)) and len(e) == 3
            ],
            "affected_paths": self.affected_paths,
            "contradiction_pressure": _score(self.contradiction_pressure),
            "required_evidence_probe": self.required_evidence_probe,
            "resolution_reason": self.resolution_reason,
            "time_window": self.time_window,
        }


@dataclass
class PathContradictionRecord:
    """路径级冲突记录。"""

    contradiction_type: str
    contradiction_group_id: str
    affected_claims: list[str]
    affected_edges: list[tuple[str, str, str]]
    affected_paths: list[str]
    severity: str
    recommended_action: str
    resolution_reason: str
    time_window: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "contradiction_type": self.contradiction_type,
            "contradiction_group_id": self.contradiction_group_id,
            "affected_claims": self.affected_claims,
            "affected_edges": [
                list(e) for e in _items(self.affected_edges)
                if isinstance(e, (list, tuple)) and len(e) == 3
            ],
            "affected_paths": self.affected_paths,
            "severity": self.severity,
            "recommended_action": self.recommended_action,
            "resolution_reason": self.resolution_reason,
            "time_window": self.time_window,
        }


# 语义互斥的事件类型对
_OPPOSITE_EVENT_PAIRS: set[frozenset[str]] = {
    frozenset([EventType.CAPACITY_EXPANSION.value, EventType.SUPPLY_SHORTAGE.value]),
    frozenset([EventType.DOMESTIC_SUBSTITUTION_PROGRESS.value, EventType.IMPORT_DEPENDENCY_SIGNAL.value]),
}


def _parse_event_time(claim: EventClaim) -> datetime | None:
    """解析 EventClaim 的时间字段。"""
    if not isinstance(claim, EventClaim):
        return None
    for ts in (claim.source_time, claim.time):
        dt = _parse_ts(ts)
        if dt is not None:
            return dt
    return None


def _within_days(a: EventClaim, b: EventClaim, days: int = 90) -> bool:
    """判断两个事件 claim 是否处于同一时间窗口内。"""
    days = max(0, int(_finite(days, 90)))
    dt_a = _parse_event_time(a)
    dt_b = _parse_event_time(b)
    if dt_a is None or dt_b is None:
        # 无法解析时间时保守认为可能冲突
        return True
    return abs((dt_a - dt_b).total_seconds()) <= days * 24 * 3600


def _is_opposite_event_pair(a: str, b: str) -> bool:
    """判断两个事件类型是否在语义上互斥。"""
    return frozenset([_text(a), _text(b)]) in _OPPOSITE_EVENT_PAIRS


def _contradiction_pressure_for_event(claim: EventClaim) -> float:
    """基于事件置信度、来源权重和新鲜度计算冲突压力。"""
    if not isinstance(claim, EventClaim):
        return 0.0
    freshness = _score(claim.freshness_score, 0.1) or 0.1
    return min(1.0, _score(claim.confidence) * get_source_weight(_text(claim.source_type)) * freshness)


# 事件类型与稳定图关系之间的方向性冲突映射
_EVENT_STABLE_OPPOSITES: dict[str, set[str]] = {
    EventType.DOMESTIC_SUBSTITUTION_PROGRESS.value: {
        RelationType.IMPORT_DEPENDENT_ON.value,
    },
    EventType.CAPACITY_EXPANSION.value: {
        RelationType.BOTTLENECK_OF.value,
        RelationType.CONSTRAINED_BY.value,
        RelationType.IMPORT_DEPENDENT_ON.value,
    },
    EventType.SUPPLY_SHORTAGE.value: {
        RelationType.SUPPLIED_BY.value,
    },
}


def _event_opposes_stable_relation(event_type: str, relation: str) -> bool:
    """判断某事件类型是否与某稳定图关系方向冲突。"""
    return _text(relation) in _EVENT_STABLE_OPPOSITES.get(_text(event_type), set())
