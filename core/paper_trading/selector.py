"""Select A-share stocks from research pipeline output.

Two complementary strategies are used:

1. Node-name resolution: research-view nodes whose name directly matches an
   A-share company are selected first.
2. Evidence-text mining: when research-view nodes are abstract concepts
   (e.g. "HBM关键材料"), we scan the underlying evidence corpus for mentions
   of A-share companies and score them by how often they appear in evidence
   linked to high-confidence research nodes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.research.perilla.alpha.outcome_tracker import OutcomeTracker
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research_graph.candidate_graph import CandidateNode, CandidateStore
from core.research_graph.candidate_schema import CandidateStatus
from core.research_graph.evidence_activator import EvidenceActivator

logger = logging.getLogger(__name__)


@dataclass
class SelectedStock:
    """A selected stock with its research provenance."""

    code: str
    name: str
    score: float
    node_id: str
    node_type: str
    status: str
    evidence_count: int
    resolver_confidence: float
    event_score: float = 0.0
    score_explanation: dict[str, Any] | None = None


class _AShareUniverse:
    """In-memory A-share name -> code lookup, excluding indices."""

    INDEX_CODE_PREFIXES: tuple[str, ...] = ("000", "399", "880", "930", "0000")
    INDEX_NAME_KEYWORDS: tuple[str, ...] = ("指数", "上证", "深证", "沪深", "中证", "创业板", "科创板")

    def __init__(self, entity_universe_dir: Path | None = None) -> None:
        self.project_root = self._detect_project_root()
        self.entity_universe_dir = entity_universe_dir or self.project_root / "data" / "entity_universe"
        self.records: list[dict[str, Any]] = []
        self.name_to_record: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _detect_project_root() -> Path:
        """Find project root by looking for data/entity_universe."""
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "data" / "entity_universe").exists():
                return parent
        for parent in current.parents:
            if (parent / "AGENTS.md").exists() and (parent / "core").exists():
                return parent
        parents = list(current.parents)
        return parents[5] if len(parents) > 5 else current.parent

    def _is_index(self, item: dict[str, Any]) -> bool:
        """Return True for index entries rather than tradeable stocks."""
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        if code.startswith(self.INDEX_CODE_PREFIXES):
            return True
        if any(kw in name for kw in self.INDEX_NAME_KEYWORDS):
            return True
        return False

    def _load(self) -> None:
        """Load A-share universe JSON and build name index."""
        paths = [
            self.project_root / "data" / "china_stock_universe.json",
            self.project_root / "data" / "entity_universe" / "china_stock_universe.json",
            self.entity_universe_dir / "a_share_stocks.json",
        ]
        for path in paths:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to read A-share universe %s: %s", path, exc)
                continue

            for item in data:
                if not isinstance(item, dict):
                    continue
                if self._is_index(item):
                    continue
                name = str(item.get("name", "")).strip()
                code = str(item.get("code", "")).strip()
                if not name or not code:
                    continue
                record = {
                    "name": name,
                    "full_name": str(item.get("full_name", "")).strip(),
                    "code": code,
                    "aliases": [str(a).strip() for a in item.get("aliases", []) if str(a).strip()],
                    "former_names": [str(a).strip() for a in item.get("former_names", []) if str(a).strip()],
                }
                self.records.append(record)
                self.name_to_record[name] = record
                if record["full_name"]:
                    self.name_to_record[record["full_name"]] = record
                for alias in record["aliases"]:
                    self.name_to_record[alias] = record
                for former in record["former_names"]:
                    self.name_to_record[former] = record

            logger.info("AShareUniverse loaded %d stocks from %s", len(self.records), path)
            return

        logger.warning("AShareUniverse: no A-share universe file found")

    def match_text(self, text: str) -> dict[str, dict[str, Any]]:
        """Return {code: record} for all A-share companies mentioned in text.

        Uses longest-match-first to avoid prefix collisions (e.g. 中兴 vs 中兴通讯).
        """
        if not text:
            return {}
        text = text.replace(" ", "").replace("　", "")
        # Sort company names by length descending so longer names win.
        names = sorted(self.name_to_record.keys(), key=len, reverse=True)
        found: dict[str, dict[str, Any]] = {}
        for name in names:
            if name in text:
                record = self.name_to_record[name]
                found[record["code"]] = record
        return found


class ResearchViewSelector:
    """Select A-share stocks from research-view nodes and evidence corpus."""

    def __init__(
        self,
        candidate_store: CandidateStore,
        entity_resolver: EntityResolver | None = None,
        research_out_dir: Path | str | None = None,
        as_of: str = "",
        event_features: dict[str, dict[str, float]] | None = None,
        alpha_weight_multiplier: float = 1.0,
        outcome_tracker: OutcomeTracker | None = None,
    ) -> None:
        """Initialize selector.

        Args:
            candidate_store: Candidate graph store.
            entity_resolver: Optional entity resolver; creates default if None.
            research_out_dir: Directory containing raw_evidence.jsonl. When
                provided, the selector also mines A-share company mentions from
                the evidence corpus.
            outcome_tracker: Optional OutcomeTracker for recording promoted
                paths to the alpha feedback loop.
        """
        self.candidate_store = candidate_store
        self.entity_resolver = entity_resolver or EntityResolver()
        self.research_out_dir = Path(research_out_dir) if research_out_dir else None
        self.as_of = as_of
        self.event_features = event_features or {}
        self.alpha_weight_multiplier = max(0.8, min(1.2, float(alpha_weight_multiplier)))
        self._a_share = _AShareUniverse()
        self.outcome_tracker = outcome_tracker

    def _node_activation_score(self, node: Any) -> float:
        """获取节点的激活分数，回测时按 as_of 过滤未来证据。

        Args:
            node: 候选节点。

        Returns:
            激活分数，范围 [0, 1]。
        """
        if self.as_of:
            return EvidenceActivator.compute_activation_score_as_of(
                node.evidence_slots, self.as_of
            )
        return float(node.activation_score or 0)

    def _load_evidence_texts(self) -> dict[str, str]:
        """Load raw evidence texts keyed by evidence_id."""
        texts: dict[str, str] = {}
        if not self.research_out_dir:
            return texts
        path = self.research_out_dir / "raw_evidence.jsonl"
        if not path.exists():
            return texts
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    evidence_id = str(row.get("evidence_id", "")).strip()
                    if not evidence_id:
                        continue
                    if self.as_of:
                        from core.research.control_plane.contracts import EvidenceRecord
                        from core.research.control_plane.policies import evidence_visible_as_of
                        metadata = dict(row.get("metadata") or {})
                        probe = EvidenceRecord(
                            evidence_id=evidence_id,
                            published_at=str(row.get("published_at") or row.get("publish_date") or ""),
                            retrieved_at=str(row.get("retrieved_at") or row.get("collected_at") or metadata.get("retrieved_at") or ""),
                        )
                        if not evidence_visible_as_of(probe, self.as_of, historical=True):
                            continue
                    parts = [
                        str(row.get("title", "")),
                        str(row.get("extracted_text", "")),
                        str(row.get("summary", "")),
                    ]
                    texts[evidence_id] = " ".join(p for p in parts if p)
        except Exception as exc:
            logger.warning("Failed to load evidence texts from %s: %s", path, exc)
        return texts

    def _mine_companies_from_nodes(
        self,
        nodes: list[CandidateNode],
        evidence_texts: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        """Score A-share companies by evidence mentions linked to research nodes.

        Returns a mapping of company code -> score metadata.
        """
        hits: dict[str, dict[str, Any]] = {}
        for node in nodes:
            node_score = self._node_activation_score(node)
            node_evidence_ids = {
                str(slot.get("evidence_id", "")).strip()
                for slot in node.evidence_slots
                if slot.get("evidence_id")
            }
            # Collect text from evidence slots attached to this node.
            linked_texts: list[str] = []
            for eid in node_evidence_ids:
                text = evidence_texts.get(eid)
                if text:
                    linked_texts.append(text)
            if not linked_texts:
                continue

            # Find companies mentioned in any linked evidence.
            seen_in_node: set[str] = set()
            for text in linked_texts:
                matched = self._a_share.match_text(text)
                for code, record in matched.items():
                    if code in seen_in_node:
                        continue
                    seen_in_node.add(code)
                    if code not in hits:
                        hits[code] = {
                            "record": record,
                            "mention_count": 0,
                            "linked_nodes": [],
                            "linked_node_scores": [],
                            "node_types": set(),
                        }
                    hits[code]["mention_count"] += 1
                    hits[code]["linked_nodes"].append(node.candidate_id)
                    hits[code]["linked_node_scores"].append(node_score)
                    hits[code]["node_types"].add(node.node_type)

        return hits

    def _resolve_node_name(self, node: CandidateNode) -> SelectedStock | None:
        """Try to select a stock when the node name itself is a company."""
        resolved = self.entity_resolver.resolve(node.name)
        if resolved.entity_type != "company":
            return None
        if resolved.listed_status != "a_share":
            return None
        code = resolved.stock_code
        if not code or len(code) < 6:
            return None
        code = code[-6:]
        event_score = self._event_score(code, resolved.canonical_name or node.name, node.candidate_id)
        structural_score = self._node_activation_score(node)
        final_score = min(1.0, (0.85 * structural_score + 0.15 * event_score) * self.alpha_weight_multiplier)
        return SelectedStock(
            code=code,
            name=resolved.canonical_name or node.name,
            score=round(final_score, 4),
            node_id=node.candidate_id,
            node_type=node.node_type,
            status=node.status,
            evidence_count=len(node.evidence_slots),
            resolver_confidence=resolved.resolver_confidence,
            event_score=round(event_score, 4),
            score_explanation={
                "structural_claim_score": round(structural_score, 4),
                "sag_event_score": round(event_score, 4),
                "order": "stable_graph + SAG_event_graph -> company_score",
                "as_of": self.as_of,
            },
        )

    def _event_score(self, code: str, name: str, node_id: str) -> float:
        features = (
            self.event_features.get(code)
            or self.event_features.get(name)
            or self.event_features.get(node_id)
            or {}
        )
        numeric = [float(value) for value in features.values() if isinstance(value, (int, float))]
        return max(0.0, min(1.0, sum(numeric) / len(numeric))) if numeric else 0.0

    def select(
        self,
        top_n: int = 10,
        min_score: float = 0.5,
        require_a_share: bool = True,
    ) -> list[SelectedStock]:
        """Select A-share stocks from research-view nodes.

        Args:
            top_n: Maximum number of stocks to return.
            min_score: Minimum activation score to consider.
            require_a_share: If True, only returns stocks with a valid A-share code.

        Returns:
            List of selected stocks, sorted by score descending.
        """
        # research_view = ACTIVE + TRUSTED nodes only
        nodes = self.candidate_store.list_candidates(view="research")
        active_trusted_nodes = [
            n for n in nodes
            if n.status in (
                CandidateStatus.ACTIVE.value,
                CandidateStatus.TRUSTED.value,
            )
            and self._node_activation_score(n) >= min_score
            and (n.attributes or {}).get("conflict_status", "none") in ("", "none", "resolved")
            and not (
                self.as_of
                and (n.attributes or {}).get("valid_to")
                and str((n.attributes or {}).get("valid_to")) < self.as_of[:10]
            )
        ]

        selected: list[SelectedStock] = []
        seen_codes: set[str] = set()

        # Strategy 1: nodes whose names directly resolve to A-share companies.
        for node in sorted(
            active_trusted_nodes,
            key=lambda n: self._node_activation_score(n),
            reverse=True,
        ):
            stock = self._resolve_node_name(node)
            if stock is None:
                continue
            if stock.code in seen_codes:
                continue
            seen_codes.add(stock.code)
            selected.append(stock)
            if self.outcome_tracker:
                self.outcome_tracker.record_promotion(
                    path_id=stock.node_id or "",
                    root_entity=getattr(stock, "theme", None) or "",
                    target_entity=stock.code,
                    path=[stock.node_id or "", stock.code],
                    features=dict(stock.score_explanation) if hasattr(stock, "score_explanation") else {},
                    final_score=stock.score,
                )
                # 同步写入 Alpha 域反馈，供 AlphaPolicyCalibrator 学习 node_type 维度权重
                try:
                    from core.research.control_plane.contracts import FeedbackEvent
                    from core.research.control_plane.feedback import FeedbackController
                    from core.research.control_plane.repository import ResearchRepository

                    FeedbackController(ResearchRepository()).record(
                        FeedbackEvent(
                            domain="alpha",
                            event_type="node_promotion",
                            event_kind="node_activation",
                            target_id=stock.node_id or "",
                            outcome="positive",
                            metrics={
                                "final_score": float(stock.score),
                                "structural_score": float(getattr(stock, "structural_score", 0)),
                                "event_score": float(getattr(stock, "event_score", 0)),
                            },
                            attribution={
                                "node_type": getattr(stock, "node_type", "") or "",
                                "node_id": stock.node_id or "",
                                "root_entity": getattr(stock, "theme", None) or "",
                                "target_entity": stock.code,
                            },
                        )
                    )
                except Exception as exc:
                    logger.debug("记录 alpha 反馈失败，不影响选股流程: %s", exc)
            if len(selected) >= top_n:
                logger.info(
                    "ResearchViewSelector: %d research nodes -> %d A-share stocks (node-name)",
                    len(nodes), len(selected),
                )
                return selected

        # Strategy 2: mine A-share companies from evidence texts linked to nodes.
        evidence_texts = self._load_evidence_texts()
        if evidence_texts:
            company_hits = self._mine_companies_from_nodes(active_trusted_nodes, evidence_texts)
            scored_hits: list[tuple[float, str, dict[str, Any]]] = []
            for code, meta in company_hits.items():
                if code in seen_codes:
                    continue
                node_scores = meta["linked_node_scores"] or [0.0]
                # Score = avg node activation + mention frequency + node-type coverage.
                # Higher weight for companies mentioned across PROCESS/MATERIAL/
                # EQUIPMENT/PRODUCT nodes to avoid concentrating on one chain segment.
                base_score = sum(node_scores) / len(node_scores)
                freq_bonus = 0.05 * meta["mention_count"]
                coverage_bonus = 0.15 * len(meta["node_types"])
                event_score = self._event_score(code, meta["record"]["name"], "")
                structural_score = base_score + freq_bonus + coverage_bonus
                final_score = min(1.0, (0.85 * structural_score + 0.15 * event_score) * self.alpha_weight_multiplier)
                meta["structural_score"] = structural_score
                meta["event_score"] = event_score
                if final_score < min_score:
                    continue
                scored_hits.append((final_score, code, meta))

            for score, code, meta in sorted(scored_hits, key=lambda x: x[0], reverse=True):
                record = meta["record"]
                # Attach to the highest-scoring linked node for provenance.
                best_node_id = ""
                best_node: CandidateNode | None = None
                for node in active_trusted_nodes:
                    if node.candidate_id in meta["linked_nodes"]:
                        if best_node is None or self._node_activation_score(node) > self._node_activation_score(
                        best_node
                    ):
                            best_node = node
                            best_node_id = node.candidate_id

                stock = SelectedStock(
                    code=code,
                    name=record["name"],
                    score=round(score, 4),
                    node_id=best_node_id,
                    node_type=best_node.node_type if best_node else "company",
                    status=best_node.status if best_node else CandidateStatus.ACTIVE.value,
                    evidence_count=meta["mention_count"],
                    resolver_confidence=0.85,
                    event_score=round(float(meta.get("event_score", 0.0)), 4),
                    score_explanation={
                        "structural_claim_score": round(float(meta.get("structural_score", 0.0)), 4),
                        "sag_event_score": round(float(meta.get("event_score", 0.0)), 4),
                        "order": "stable_graph + SAG_event_graph -> company_score",
                        "as_of": self.as_of,
                    },
                )
                selected.append(stock)
                if self.outcome_tracker:
                    self.outcome_tracker.record_promotion(
                        path_id=stock.node_id or "",
                        root_entity=getattr(stock, "theme", None) or "",
                        target_entity=stock.code,
                        path=[stock.node_id or "", stock.code],
                        features=dict(stock.score_explanation) if hasattr(stock, "score_explanation") else {},
                        final_score=stock.score,
                    )
                    # 同步写入 Alpha 域反馈，供 AlphaPolicyCalibrator 学习 node_type 维度权重
                    try:
                        from core.research.control_plane.contracts import FeedbackEvent
                        from core.research.control_plane.feedback import FeedbackController
                        from core.research.control_plane.repository import ResearchRepository

                        score_explanation = stock.score_explanation or {}
                        FeedbackController(ResearchRepository()).record(
                            FeedbackEvent(
                                domain="alpha",
                                event_type="node_promotion",
                                event_kind="node_activation",
                                target_id=stock.node_id or "",
                                outcome="positive",
                                metrics={
                                    "final_score": float(stock.score),
                                    "structural_score": float(
                                        score_explanation.get("structural_claim_score", 0)
                                    ),
                                    "event_score": float(
                                        score_explanation.get("sag_event_score", 0)
                                    ),
                                },
                                attribution={
                                    "node_type": getattr(stock, "node_type", "") or "",
                                    "node_id": stock.node_id or "",
                                    "root_entity": getattr(stock, "theme", None) or "",
                                    "target_entity": stock.code,
                                },
                            )
                        )
                    except Exception as exc:
                        logger.debug("记录 alpha 反馈失败，不影响选股流程: %s", exc)
                seen_codes.add(code)
                if len(selected) >= top_n:
                    break

        logger.info(
            "ResearchViewSelector: %d research nodes -> %d A-share stocks",
            len(nodes), len(selected),
        )
        return selected
