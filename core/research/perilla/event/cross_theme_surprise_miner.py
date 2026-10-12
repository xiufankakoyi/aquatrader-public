"""跨主题意外发现：从已知主题触发未研究主题的冷启动。

解决差距 6：有事件传播，但缺少"跨主题意外发现"。

发现逻辑（全部规则化，不引入外部 ML）：
  1. 获取源神经元所在主题和类型
  2. 通过 EntityResolver 找到同 category 的其他 material/equipment/product 术语
  3. 排除当前图中已存在的主题
  4. 对高置信度源事件，生成 COLD_START_THEME pulse，驱动后续 growth mining
  5. 同时利用现有图谱突触/边发现"桥接公司"触达的隐藏主题
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _bounded_int(value: Any, default: int, maximum: int = 10000) -> int:
    try:
        number = int(float(value))
    except (OverflowError, TypeError, ValueError):
        number = default
    return max(0, min(maximum, number))


@dataclass
class SurpriseCandidate:
    """跨主题意外发现候选。"""

    source_theme: str
    source_neuron_id: str
    suggested_theme: str
    suggested_seed_node: str
    link_type: str  # same_category / bridge_company / shared_alias
    strength: float
    reason: str
    suggested_queries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为 dict。"""
        return {
            "source_theme": self.source_theme,
            "source_neuron_id": self.source_neuron_id,
            "suggested_theme": self.suggested_theme,
            "suggested_seed_node": self.suggested_seed_node,
            "link_type": self.link_type,
            "strength": round(max(0.0, min(1.0, _finite_float(self.strength))), 4),
            "reason": self.reason,
            "suggested_queries": list(self.suggested_queries),
        }


class CrossThemeSurpriseMiner:
    """跨主题意外发现挖掘器。

    从源主题的新证据/高置信度神经元出发，
    利用本地实体词表和图谱边发现可能被影响的未研究主题。
    """

    def __init__(
        self,
        entity_resolver: Any | None = None,
        confidence_threshold: float = 0.5,
        min_surprise_strength: float = 0.3,
        max_candidates: int = 10,
    ) -> None:
        """初始化。

        Args:
            entity_resolver: EntityResolver 实例，用于解析术语 category。
            confidence_threshold: 源神经元触发意外发现的最低置信度。
            min_surprise_strength: 候选最低强度。
            max_candidates: 返回最大候选数。
        """
        self._entity_resolver = entity_resolver
        self._confidence_threshold = max(0.0, min(1.0, _finite_float(confidence_threshold, 0.5)))
        self._min_surprise_strength = max(0.0, min(1.0, _finite_float(min_surprise_strength, 0.3)))
        self._max_candidates = _bounded_int(max_candidates, 10)

    def mine(
        self,
        source_neuron_ids: list[str],
        neurons: list[Any],
        synapses: list[Any] | None = None,
        known_themes: set[str] | None = None,
    ) -> list[SurpriseCandidate]:
        """挖掘跨主题意外发现候选。

        Args:
            source_neuron_ids: 触发事件的源神经元 ID 列表。
            neurons: 当前超图所有非 tombstone 神经元。
            synapses: 当前超图所有非 PRUNED 突触，可选。
            known_themes: 已研究主题集合，可选。

        Returns:
            SurpriseCandidate 列表，按 strength 降序。
        """
        known_themes = {_text(theme) for theme in (known_themes if isinstance(known_themes, (set, list, tuple)) else []) if _text(theme)}
        synapses = synapses if isinstance(synapses, (list, tuple)) else []
        source_neuron_ids = [_text(node_id) for node_id in source_neuron_ids] if isinstance(source_neuron_ids, (list, tuple)) else []
        neurons = neurons if isinstance(neurons, (list, tuple)) else []

        neuron_by_id = {
            _text(_field(n, "neuron_id")): n
            for n in neurons
            if _text(_field(n, "neuron_id")) and not bool(_field(n, "is_tombstone", False))
        }
        candidates: list[SurpriseCandidate] = []
        seen: set[tuple[str, str, str]] = set()

        for nid in source_neuron_ids:
            src = neuron_by_id.get(nid)
            if src is None:
                continue
            src_conf = _finite_float(_field(src, "confidence"))
            if src_conf < self._confidence_threshold:
                continue

            src_theme = _text(_field(src, "root_theme"), "unknown") or "unknown"
            src_name = _text(_field(src, "canonical_name"))
            if not src_name:
                continue

            # 1. 同 category 术语扩展
            candidates.extend(
                self._same_category_candidates(
                    src_theme=src_theme,
                    src_id=nid,
                    src_name=src_name,
                    known_themes=known_themes,
                    seen=seen,
                )
            )

            # 2. 桥接公司扩展
            candidates.extend(
                self._bridge_company_candidates(
                    src_theme=src_theme,
                    src_id=nid,
                    src_name=src_name,
                    neurons=neurons,
                    synapses=synapses,
                    known_themes=known_themes,
                    seen=seen,
                )
            )

        candidates = [
            candidate for candidate in candidates
            if _finite_float(candidate.strength) >= self._min_surprise_strength
        ]
        candidates.sort(key=lambda c: c.strength, reverse=True)
        return candidates[: self._max_candidates]

    def _same_category_candidates(
        self,
        src_theme: str,
        src_id: str,
        src_name: str,
        known_themes: set[str],
        seen: set[tuple[str, str, str]],
    ) -> list[SurpriseCandidate]:
        """基于 EntityResolver 的 category 找同领域术语。"""
        if self._entity_resolver is None:
            return []

        try:
            resolved = self._entity_resolver.resolve(src_name)
        except (AttributeError, KeyError, TypeError, ValueError):
            return []
        if resolved is None or _text(_field(resolved, "entity_type")) not in ("material", "equipment", "component", "product"):
            return []

        terms = getattr(self._entity_resolver, "_material_equipment_terms", {})
        if not isinstance(terms, Mapping):
            return []
        src_record = terms.get(src_name.lower())
        if not isinstance(src_record, Mapping):
            return []

        src_category = _text(src_record.get("category"))
        if not src_category:
            return []

        results: list[SurpriseCandidate] = []
        # category 前缀相同视为同一大领域
        category_prefix = src_category.split("_")[0] if "_" in src_category else src_category

        known_theme_names = {_text(theme).lower() for theme in known_themes}
        for term, record in terms.items():
            if not isinstance(record, Mapping):
                continue
            category = _text(record.get("category"))
            if not category:
                continue
            # 只取 canonical name（过滤 alias）
            canonical = _text(record.get("name"))
            if not canonical or term != canonical.lower():
                continue
            if category == src_category and canonical.lower() == src_name.lower():
                continue
            if not category.startswith(category_prefix):
                continue

            suggested_theme = canonical
            if suggested_theme.lower() in known_theme_names:
                continue

            key = (src_id, "same_category", suggested_theme)
            if key in seen:
                continue
            seen.add(key)

            strength = 0.5 + 0.4 * (
                1.0 if category == src_category else 0.5
            )
            results.append(
                SurpriseCandidate(
                    source_theme=src_theme,
                    source_neuron_id=src_id,
                    suggested_theme=suggested_theme,
                    suggested_seed_node=canonical,
                    link_type="same_category",
                    strength=round(min(1.0, strength), 4),
                    reason=f"与 {src_name} 同属领域 {category}",
                    suggested_queries=[
                        f"{canonical} 产业",
                        f"{canonical} 供应商",
                    ],
                )
            )

        return results

    def _bridge_company_candidates(
        self,
        src_theme: str,
        src_id: str,
        src_name: str,
        neurons: list[Any],
        synapses: list[Any],
        known_themes: set[str],
        seen: set[tuple[str, str, str]],
    ) -> list[SurpriseCandidate]:
        """通过桥接公司发现隐藏主题。

        如果源神经元通过突触连接到某公司，该公司又连接到其他主题的材料/设备节点，
        则这些节点所属主题为意外发现候选。
        """
        if self._entity_resolver is None:
            return []

        neuron_by_id = {
            _text(_field(n, "neuron_id")): n
            for n in neurons
            if _text(_field(n, "neuron_id"))
        }
        bridge_companies: set[str] = set()

        # 找与源神经元直接相连的 company 节点
        for syn in synapses:
            if _text(_field(syn, "status")) in ("PRUNED",):
                continue
            src_side = _text(_field(syn, "source_neuron_id"))
            tgt_side = _text(_field(syn, "target_neuron_id"))
            if src_side == src_id:
                other = neuron_by_id.get(tgt_side)
            elif tgt_side == src_id:
                other = neuron_by_id.get(src_side)
            else:
                continue
            if other is None:
                continue
            try:
                resolved = self._entity_resolver.resolve(_text(_field(other, "canonical_name")))
            except (AttributeError, KeyError, TypeError, ValueError):
                resolved = None
            if resolved is not None and _text(_field(resolved, "entity_type")) == "company":
                bridge_companies.add(_text(_field(other, "neuron_id")))

        results: list[SurpriseCandidate] = []
        for syn in synapses:
            if _text(_field(syn, "status")) in ("PRUNED",):
                continue
            src_side = _text(_field(syn, "source_neuron_id"))
            tgt_side = _text(_field(syn, "target_neuron_id"))
            if src_side not in bridge_companies and tgt_side not in bridge_companies:
                continue

            company_id = src_side if src_side in bridge_companies else tgt_side
            other_id = tgt_side if company_id == src_side else src_side
            other = neuron_by_id.get(other_id)
            if other is None or other.neuron_id == src_id:
                continue

            other_theme = _text(_field(other, "root_theme"), "unknown") or "unknown"
            if other_theme == src_theme:
                continue
            # bridge_company 发现的是“意想不到的联动”，即使目标主题已存在于图中，
            # 只要通过桥接公司形成新的跨主题连接，就值得生成脉冲。
            key = (src_id, "bridge_company", other_theme)
            if key in seen:
                continue
            seen.add(key)

            company = neuron_by_id.get(company_id)
            company_name = _text(_field(company, "canonical_name"), company_id) if company else company_id
            results.append(
                SurpriseCandidate(
                    source_theme=src_theme,
                    source_neuron_id=src_id,
                    suggested_theme=other_theme,
                    suggested_seed_node=_text(_field(other, "canonical_name"), other_id),
                    link_type="bridge_company",
                    strength=round(max(0.3, min(1.0, _finite_float(_field(syn, "strength"), 0.5))), 4),
                    reason=f"通过桥接公司 {company_name} 与 {src_name} 形成跨主题连接",
                    suggested_queries=[
                        f"{other_theme} 供应商",
                        f"{company_name} {other_theme}",
                    ],
                )
            )

        return results
