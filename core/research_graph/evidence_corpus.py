from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from core.research_graph.schema import EdgeStatus, EvidenceLevel, NodeType, RelationType


REQUIRED_RUN_FILES = (
    "source_manifest.json",
    "raw_evidence.jsonl",
    "candidate_claims.jsonl",
    "counter_evidence_candidates.jsonl",
    "graph_hypotheses.jsonl",
    "company_alias_map.json",
    "topic_gap_report.md",
    "quality_report.md",
    "progress_log.md",
    "failed_sources.jsonl",
)

ALLOWED_EVIDENCE_LEVELS = {item.value for item in EvidenceLevel}
ALLOWED_NODE_TYPES = {item.value for item in NodeType}
ALLOWED_RELATIONS = {item.value for item in RelationType}
MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")


class CorpusValidationError(ValueError):
    pass


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CorpusValidationError(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def validate_run_directory(run_dir: Path) -> dict[str, Any]:
    missing = [name for name in REQUIRED_RUN_FILES if not (run_dir / name).exists()]
    if missing:
        raise CorpusValidationError(f"missing run files: {missing}")

    manifest = json.loads((run_dir / "source_manifest.json").read_text(encoding="utf-8"))
    raw_evidence = read_jsonl(run_dir / "raw_evidence.jsonl")
    claims = read_jsonl(run_dir / "candidate_claims.jsonl")
    counters = read_jsonl(run_dir / "counter_evidence_candidates.jsonl")
    hypotheses = read_jsonl(run_dir / "graph_hypotheses.jsonl")
    failed = read_jsonl(run_dir / "failed_sources.jsonl")

    source_ids = _validate_manifest(manifest)
    evidence_ids = _validate_raw_evidence(raw_evidence, source_ids)
    claim_ids = _validate_claims(claims, evidence_ids)
    counter_ids = _validate_counters(counters, source_ids, claim_ids)
    _validate_hypotheses(hypotheses, evidence_ids, claim_ids, counter_ids)
    _validate_failed_sources(failed)
    _validate_topic_coverage(manifest, raw_evidence, claims, counters, hypotheses)

    return {
        "sources": len(manifest),
        "successful_sources": sum(1 for item in manifest if item.get("status") == "success"),
        "failed_sources": sum(1 for item in manifest if item.get("status") == "failed"),
        "raw_evidence": len(raw_evidence),
        "candidate_claims": len(claims),
        "counter_evidence": len(counters),
        "graph_hypotheses": len(hypotheses),
        "confidence_distribution": dict(Counter(item["suggested_confidence"] for item in claims)),
    }


def _validate_manifest(rows: list[dict[str, Any]]) -> set[str]:
    required = {
        "source_id",
        "topic",
        "source_type",
        "title",
        "url",
        "publisher",
        "publish_date",
        "retrieved_at",
        "status",
        "failure_reason",
        "relevance_hint",
    }
    source_ids: set[str] = set()
    urls: set[str] = set()
    for row in rows:
        _require(row, required, "source_manifest")
        if row["source_id"] in source_ids:
            raise CorpusValidationError(f"duplicate source_id: {row['source_id']}")
        if row["url"] in urls:
            raise CorpusValidationError(f"duplicate source url: {row['url']}")
        if row["status"] not in {"success", "failed", "skipped"}:
            raise CorpusValidationError(f"invalid source status: {row['status']}")
        source_ids.add(row["source_id"])
        urls.add(row["url"])
    return source_ids


def _validate_raw_evidence(rows: list[dict[str, Any]], source_ids: set[str]) -> set[str]:
    required = {
        "evidence_id",
        "topic",
        "source_id",
        "source_type",
        "title",
        "url",
        "publish_date",
        "extracted_text",
        "summary",
        "mentioned_entities",
        "mentioned_chain_nodes",
        "possible_relation_types",
        "evidence_strength_hint",
        "why_strength",
        "needs_human_review",
    }
    evidence_ids: set[str] = set()
    for row in rows:
        _require(row, required, "raw_evidence")
        if row["source_id"] not in source_ids:
            raise CorpusValidationError(f"evidence {row['evidence_id']} has unknown source_id")
        if row["evidence_strength_hint"] not in ALLOWED_EVIDENCE_LEVELS:
            raise CorpusValidationError(f"invalid evidence strength: {row['evidence_strength_hint']}")
        for relation in row["possible_relation_types"]:
            if relation not in ALLOWED_RELATIONS:
                raise CorpusValidationError(f"invalid possible relation: {relation}")
        evidence_ids.add(row["evidence_id"])
    return evidence_ids


def _validate_claims(rows: list[dict[str, Any]], evidence_ids: set[str]) -> set[str]:
    required = {
        "claim_id",
        "topic",
        "subject",
        "relation",
        "object",
        "path_hint",
        "supporting_evidence_ids",
        "counter_evidence_ids",
        "suggested_confidence",
        "suggested_status",
        "reason",
        "missing_verification",
        "main_board_relevance",
        "stock_code",
        "stock_name",
    }
    claim_ids: set[str] = set()
    for row in rows:
        _require(row, required, "candidate_claims")
        if row["relation"] not in ALLOWED_RELATIONS:
            raise CorpusValidationError(f"invalid claim relation: {row['relation']}")
        if row["subject"]["type"] not in ALLOWED_NODE_TYPES or row["object"]["type"] not in ALLOWED_NODE_TYPES:
            raise CorpusValidationError(f"invalid claim node type: {row['claim_id']}")
        if row["suggested_status"] != EdgeStatus.CANDIDATE.value:
            raise CorpusValidationError(f"claim {row['claim_id']} is not CANDIDATE")
        if row["suggested_confidence"] not in ALLOWED_EVIDENCE_LEVELS:
            raise CorpusValidationError(f"invalid claim confidence: {row['suggested_confidence']}")
        support = set(row["supporting_evidence_ids"])
        counter = set(row["counter_evidence_ids"])
        if not support and row["suggested_confidence"] in {EvidenceLevel.LIKELY.value, EvidenceLevel.PROVEN.value}:
            raise CorpusValidationError(f"claim {row['claim_id']} has high confidence without evidence_id")
        if not support.issubset(evidence_ids) or not counter.issubset(evidence_ids):
            raise CorpusValidationError(f"claim {row['claim_id']} references unknown evidence")
        if row["subject"]["type"] in {NodeType.COMPANY.value, NodeType.SUBSIDIARY.value}:
            if row["object"]["type"] == NodeType.THEME.value:
                raise CorpusValidationError(f"company claim directly connects to THEME: {row['claim_id']}")
            if not row["path_hint"] or len(row["path_hint"]) < 3:
                raise CorpusValidationError(f"company claim missing specific path_hint: {row['claim_id']}")
        if row["main_board_relevance"] == "eligible" and not _is_main_board_code(row["stock_code"]):
            raise CorpusValidationError(f"non-mainboard claim marked eligible: {row['claim_id']}")
        claim_ids.add(row["claim_id"])
    return claim_ids


def _validate_counters(rows: list[dict[str, Any]], source_ids: set[str], claim_ids: set[str]) -> set[str]:
    required = {
        "counter_id",
        "topic",
        "target_claim_id",
        "source_id",
        "counter_type",
        "text",
        "summary",
        "severity",
        "suggested_action",
    }
    counter_ids: set[str] = set()
    for row in rows:
        _require(row, required, "counter_evidence_candidates")
        if row["target_claim_id"] not in claim_ids:
            raise CorpusValidationError(f"counter targets unknown claim: {row['target_claim_id']}")
        if row["source_id"] not in source_ids:
            raise CorpusValidationError(f"counter references unknown source: {row['source_id']}")
        if row["severity"] not in {"low", "medium", "high"}:
            raise CorpusValidationError(f"invalid counter severity: {row['severity']}")
        counter_ids.add(row["counter_id"])
    return counter_ids


def _validate_hypotheses(
    rows: list[dict[str, Any]],
    evidence_ids: set[str],
    claim_ids: set[str],
    counter_ids: set[str],
) -> None:
    required = {
        "hypothesis_id",
        "topic",
        "benefit_path",
        "claim_ids",
        "supporting_evidence_ids",
        "counter_evidence_ids",
        "suggested_confidence",
        "suggested_status",
        "path_specificity",
        "bottleneck_reason",
        "verification_questions",
        "do_not_activate_reason",
    }
    for row in rows:
        _require(row, required, "graph_hypotheses")
        if row["suggested_status"] == EdgeStatus.ACTIVE.value:
            raise CorpusValidationError(f"hypothesis is ACTIVE: {row['hypothesis_id']}")
        if row["suggested_status"] != EdgeStatus.CANDIDATE.value:
            raise CorpusValidationError(f"hypothesis is not CANDIDATE: {row['hypothesis_id']}")
        if not set(row["claim_ids"]).issubset(claim_ids):
            raise CorpusValidationError(f"hypothesis references unknown claim: {row['hypothesis_id']}")
        if not set(row["supporting_evidence_ids"]).issubset(evidence_ids):
            raise CorpusValidationError(f"hypothesis references unknown evidence: {row['hypothesis_id']}")
        if not row["supporting_evidence_ids"]:
            raise CorpusValidationError(f"hypothesis has no evidence_id: {row['hypothesis_id']}")
        unknown_counters = set(row["counter_evidence_ids"]) - (evidence_ids | counter_ids)
        if unknown_counters:
            raise CorpusValidationError(f"hypothesis references unknown counter ids: {unknown_counters}")


def _validate_failed_sources(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        _require(row, {"source_id", "topic", "url", "failure_reason", "retrieved_at"}, "failed_sources")


def _validate_topic_coverage(
    manifest: list[dict[str, Any]],
    raw_evidence: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    counters: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
) -> None:
    required_topics = {"HBM", "CPO/硅光/光互连", "800V HVDC/数据中心电力"}
    for topic in required_topics:
        if not any(row["topic"] == topic for row in manifest):
            raise CorpusValidationError(f"topic missing source_manifest rows: {topic}")
        if not any(row["topic"] == topic for row in raw_evidence):
            raise CorpusValidationError(f"topic missing raw evidence: {topic}")
        if not any(row["topic"] == topic for row in claims):
            raise CorpusValidationError(f"topic missing candidate claims: {topic}")
        if not any(row["topic"] == topic for row in counters):
            raise CorpusValidationError(f"topic missing counter candidates: {topic}")
        if not any(row["topic"] == topic for row in hypotheses):
            raise CorpusValidationError(f"topic missing graph hypotheses: {topic}")


def _require(row: dict[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(row))
    if missing:
        raise CorpusValidationError(f"{label} row missing fields: {missing}")


def _is_main_board_code(stock_code: str) -> bool:
    digits = "".join(ch for ch in stock_code if ch.isdigit())
    return bool(digits.startswith(MAIN_BOARD_PREFIXES))
