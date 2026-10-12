from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


class ResearchCausalContext:
    """Read-only bridge that never promotes theme membership into a causal edge."""

    VERIFIED_STATES = {"CANDIDATE", "SUPPORTED", "ACTIVE", "TRUSTED"}

    def __init__(self, db_path: str | Path = "data/research_control/research_control.db") -> None:
        self.db_path = Path(db_path)

    @staticmethod
    def _visible(payload: Mapping[str, Any], as_of: str) -> bool:
        published = str(payload.get("published_at") or "")[:10]
        if not published:
            return False
        try:
            return datetime.fromisoformat(published).date() <= datetime.fromisoformat(as_of[:10]).date()
        except (TypeError, ValueError):
            return False

    def for_company(self, *, symbol: str, company_name: str, as_of: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "verified_edges": [],
            "exploration_links": [],
            "observed_facts": [],
            "forward_plans": [],
            "ontology_edges": [],
            "ontology_mappings": [],
            "rejected_edges": [],
            "gaps": [],
            "source": str(self.db_path),
        }
        if not self.db_path.exists():
            result["gaps"].append("research_control_missing")
            return result
        connection = sqlite3.connect(self.db_path)
        try:
            try:
                evidence_rows = connection.execute(
                    "SELECT claim_id, payload_json FROM evidence"
                ).fetchall()
                claim_rows = connection.execute(
                    "SELECT payload_json FROM claims"
                ).fetchall()
            except sqlite3.DatabaseError:
                result["gaps"].append("research_control_schema_missing")
                return result
            evidence_by_claim: dict[str, list[dict[str, Any]]] = {}
            for claim_id, payload_json in evidence_rows:
                try:
                    payload = json.loads(payload_json)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, Mapping):
                    continue
                if self._visible(payload, as_of):
                    evidence_by_claim.setdefault(str(claim_id), []).append(payload)
            for (payload_json,) in claim_rows:
                try:
                    claim = json.loads(payload_json)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(claim, Mapping):
                    continue
                identifiers = {str(claim.get("subject_id") or ""), str(claim.get("subject_name") or "")}
                claim_id = str(claim.get("claim_id") or "")
                relation = str(claim.get("relation") or "")
                attributes = claim.get("attributes")
                attributes = attributes if isinstance(attributes, Mapping) else {}
                if attributes.get("scope") == "industry_ontology" and claim_id in evidence_by_claim:
                    result["ontology_edges"].append({
                        "claim_id": claim_id,
                        "subject_id": claim.get("subject_id", ""),
                        "subject_name": claim.get("subject_name", ""),
                        "relation": relation,
                        "object_name": claim.get("object_name", ""),
                        "research_status": claim.get("research_status", ""),
                        "evidence": evidence_by_claim.get(claim_id, []),
                    })
                    continue
                if symbol not in identifiers and symbol.split(".")[0] not in identifiers and company_name not in identifiers:
                    continue
                edge = {
                    "claim_id": claim_id,
                    "subject_id": claim.get("subject_id", ""),
                    "subject_name": claim.get("subject_name", ""),
                    "relation": relation,
                    "object_id": claim.get("object_id", ""),
                    "object_name": claim.get("object_name", ""),
                    "direction": claim.get("direction", ""),
                    "research_status": claim.get("research_status", ""),
                    "evidence_confidence": claim.get("evidence_confidence", 0.0),
                    "evidence": evidence_by_claim.get(claim_id, []),
                }
                if relation.upper().startswith("OBSERVED_") and edge["evidence"]:
                    result["observed_facts"].append(edge)
                elif relation.upper().startswith("FORWARD_") and edge["evidence"]:
                    result["forward_plans"].append(edge)
                elif relation.lower() == "member_of_theme":
                    result["exploration_links"].append(edge)
                elif edge["research_status"] in self.VERIFIED_STATES and edge["evidence"]:
                    result["verified_edges"].append(edge)
                else:
                    result["rejected_edges"].append(edge)
        finally:
            connection.close()
        if not result["verified_edges"]:
            result["gaps"].append("verified_causal_chain_missing")
        relations = {str(edge.get("relation") or "").upper() for edge in result["verified_edges"]}
        def canonical_product(value: Any) -> str:
            normalized = "".join(character for character in str(value or "") if character.isalnum())
            for modifier in ("高端", "高速率", "高速", "通信收发", "通信", "收发"):
                normalized = normalized.replace(modifier, "")
            return normalized

        product_objects = [
            str(edge.get("object_name") or "") for edge in result["verified_edges"]
            if str(edge.get("relation") or "").upper() in {"PRODUCES", "DEVELOPS"}
        ]
        upstream_mapped = False
        seen_mappings: set[tuple[str, str]] = set()
        for ontology in result["ontology_edges"]:
            if str(ontology.get("relation") or "").upper() != "REQUIRES":
                continue
            ontology_subject = str(ontology.get("subject_name") or "")
            for product in product_objects:
                if canonical_product(product) != canonical_product(ontology_subject):
                    continue
                upstream_mapped = True
                mapping_key = (product, str(ontology.get("claim_id") or ""))
                if mapping_key in seen_mappings:
                    continue
                seen_mappings.add(mapping_key)
                result["ontology_mappings"].append({
                    "company_product": product,
                    "ontology_subject": ontology_subject,
                    "ontology_claim_id": ontology.get("claim_id", ""),
                    "mapping_method": "canonical_product_alias_v1",
                    "mapping_confidence": 0.9,
                })
        slots = {
            "product": bool(relations & {"PRODUCES", "DEVELOPS"}),
            "upstream_constraint": bool(relations & {"USES", "REQUIRES"}) or upstream_mapped,
            "downstream": bool(relations & {"SUPPLIES_TO", "CUSTOMER_OF", "SERVES_MARKET"}),
            "demand_driver": "BENEFITS_FROM" in relations,
        }
        result["slot_coverage"] = slots
        result["chain_status"] = (
            "verified" if all(slots.values()) else
            "partial" if any(slots.values()) else "missing"
        )
        if result["verified_edges"] and not all(slots.values()):
            result["gaps"].append("causal_chain_depth_insufficient")
        return result

    def industry_ontology_summary(
        self, *, symbol: str, company_name: str, as_of: str
    ) -> dict[str, Any]:
        """返回严格过滤 member_of_theme 的产业本体结论摘要。

        P1.5 验收点 1：member_of_theme 不进入市场份额、产品和竞争结论。
        本方法显式排除 exploration_links（member_of_theme 关系），
        仅返回 verified_edges / observed_facts / ontology_mappings 中的产业结论。

        Args:
            symbol: 公司股票代码
            company_name: 公司名称
            as_of: PIT 时点（YYYY-MM-DD）

        Returns:
            dict 包含：
                - products: 产品结论（PRODUCES / DEVELOPS）
                - upstream: 上游约束（USES / REQUIRES）
                - downstream: 下游客户（SUPPLIES_TO / CUSTOMER_OF / SERVES_MARKET）
                - demand_drivers: 需求驱动（BENEFITS_FROM）
                - competitors: 竞争者列表（严格为空，竞争结论只能从 CompetitiveLandscape 来）
                - market_share_evidence: 份额证据（严格为空，份额证据不从 ontology 推导）
                - member_of_theme_excluded: True（显式标记）
                - exploration_links_excluded: 被排除的 member_of_theme 数
                - observed_facts_count: 已观察事实数
                - source: db 路径
                - gaps: gap 列表
        """
        full = self.for_company(
            symbol=symbol, company_name=company_name, as_of=as_of
        )
        verified = full.get("verified_edges", [])
        observed = full.get("observed_facts", [])

        def _filter_by_relations(
            edges: list[dict[str, Any]], relations: set[str]
        ) -> list[dict[str, Any]]:
            return [
                edge
                for edge in edges
                if str(edge.get("relation") or "").upper() in relations
            ]

        products = _filter_by_relations(verified, {"PRODUCES", "DEVELOPS"})
        upstream = _filter_by_relations(verified, {"USES", "REQUIRES"})
        downstream = _filter_by_relations(
            verified, {"SUPPLIES_TO", "CUSTOMER_OF", "SERVES_MARKET"}
        )
        demand_drivers = _filter_by_relations(verified, {"BENEFITS_FROM"})

        return {
            "products": products,
            "upstream": upstream,
            "downstream": downstream,
            "demand_drivers": demand_drivers,
            # 验收点 1：竞争结论只能从 CompetitiveLandscape 来，不从 ontology 推导
            "competitors": [],
            # 验收点 1：份额证据不从 ontology 来
            "market_share_evidence": [],
            "member_of_theme_excluded": True,
            "exploration_links_excluded": len(full.get("exploration_links", [])),
            "observed_facts_count": len(observed),
            "ontology_mappings": full.get("ontology_mappings", []),
            "source": str(self.db_path),
            "gaps": full.get("gaps", []),
        }

    # ------------------------------------------------------------------ #
    # P2.3 只读输出契约（供 discovery/change_radar 消费）
    # ------------------------------------------------------------------ #

    def export_observed_facts_for_radar(
        self,
        *,
        symbol: str,
        company_name: str,
        as_of: str,
    ) -> list[dict[str, Any]]:
        """只读导出已观察事实，供 ChangeRadar 消费。

        P2.3 要求「修改 core/research/value_chain/causal_context.py」，
        本方法仅从既有 evidence/claims 表只读导出 observed_facts，不修改
        任何状态，不写入数据库，不输出交易动作建议。

        输出的每个事实需满足 P2.3 验收点 2「事件必须有时间、实体、方向、
        来源和影响路径」：
        - observed_at：取证据的 published_at
        - entity_name：取 claim 的 object_name
        - direction：取 claim 的 direction；缺失则 neutral
        - source_refs：取 claim_id 与 evidence source_ref
        - impact_path：取 relation + object_name

        与 for_company 的区别：
        - for_company 返回完整因果上下文（含 verified_edges / forward_plans 等）
        - 本方法仅返回 observed_facts 子集，且按 ChangeRadar 输入格式扁平化

        Args:
            symbol: 公司股票代码
            company_name: 公司名称
            as_of: PIT 时点（YYYY-MM-DD）

        Returns:
            已观察事实字典列表；每个字典含 ChangeEvent 必填字段
        """
        # 仅只读：复用 for_company 不修改状态
        full = self.for_company(
            symbol=symbol, company_name=company_name, as_of=as_of
        )
        observed_facts = full.get("observed_facts", []) or []
        results: list[dict[str, Any]] = []
        for fact in observed_facts:
            try:
                item = self._adapt_observed_fact(fact, symbol)
                if item is not None:
                    results.append(item)
            except Exception:  # noqa: BLE001
                # AGENTS.md：缺数据降级，不崩溃
                continue
        return results

    @staticmethod
    def _adapt_observed_fact(
        fact: dict[str, Any],
        symbol: str,
    ) -> dict[str, Any] | None:
        """适配单个 observed_fact 为雷达可消费的字典。"""
        object_name = str(fact.get("object_name", "") or "")
        if not object_name:
            return None
        relation = str(fact.get("relation", "") or "")
        # observed_at：从 evidence 取 published_at
        evidence_list = fact.get("evidence", []) or []
        if not isinstance(evidence_list, (list, tuple)):
            evidence_list = []
        observed_at = ""
        source_refs: list[str] = [str(fact.get("claim_id", ""))] if fact.get("claim_id") else []
        for evidence in evidence_list:
            if not isinstance(evidence, Mapping):
                continue
            published = str(evidence.get("published_at", "") or "")[:10]
            if published and (not observed_at or published < observed_at):
                observed_at = published
            src_ref = str(evidence.get("source_ref", "") or evidence.get("evidence_id", "") or "")
            if src_ref and src_ref not in source_refs:
                source_refs.append(src_ref)
        if not observed_at:
            return None
        if not source_refs:
            source_refs = [f"observed_{symbol}"]
        direction = str(fact.get("direction", "") or "neutral")
        if direction not in ("positive", "negative", "neutral"):
            direction = "neutral"
        impact_path = f"{relation} | {object_name} | {symbol}"
        return {
            "claim_id": str(fact.get("claim_id", "")),
            "observed_at": observed_at,
            "entity_name": object_name,
            "entity_kind": "product",
            "direction": direction,
            "magnitude": fact.get("evidence_confidence"),
            "magnitude_unit": "confidence",
            "source_refs": source_refs,
            "impact_path": impact_path,
            "impact_path_steps": [
                f"relation: {relation}",
                f"object: {object_name}",
                f"symbol: {symbol}",
            ],
            "event_type": "product_price_cost",  # 默认；调用方可覆盖
        }
