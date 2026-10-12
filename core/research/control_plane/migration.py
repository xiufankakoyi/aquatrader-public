"""旧研究数据的非破坏、幂等迁移。"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .adapters import adapt_candidate, adapt_legacy_evidence, adapt_raw_document
from .repository import ResearchRepository
from .contracts import _safe_int, _safe_text


@dataclass
class MigrationReport:
    documents: int = 0
    claims: int = 0
    evidence: int = 0
    generated_skipped: int = 0
    malformed_skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class LazyResearchMigrator:
    def __init__(self, repository: ResearchRepository) -> None:
        self.repository = repository

    def migrate_jsonl(self, path: str | Path, kind: str, *, claim_id: str = "") -> MigrationReport:
        report = MigrationReport()
        kind = _safe_text(kind)
        claim_id = _safe_text(claim_id)
        if kind not in {"document", "evidence", "claim"}:
            report.warnings.append(f"不支持的迁移类型:{kind}")
            return report
        source = Path(path)
        if not source.exists():
            report.warnings.append(f"文件不存在:{source}")
            return report
        try:
            handle = source.open("r", encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            report.warnings.append(str(exc))
            return report
        with handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, Mapping):
                        raise ValueError("migration payload must be an object")
                    if kind == "document":
                        trace, document = adapt_raw_document(payload)
                        self.repository.save_source_trace(trace)
                        self.repository.save_document(document)
                        report.documents += 1
                    elif kind == "evidence":
                        evidence = adapt_legacy_evidence(
                            payload,
                            claim_id=claim_id or _safe_text(payload.get("claim_id")),
                        )
                        if self.repository.save_evidence(evidence):
                            report.evidence += 1
                        else:
                            report.generated_skipped += 1
                    elif kind == "claim":
                        self.repository.save_claim(adapt_candidate(payload))
                        report.claims += 1
                except Exception as exc:
                    report.malformed_skipped += 1
                    report.warnings.append(str(exc))
        return report

    def migrate_candidate_store(self, store: Any) -> MigrationReport:
        report = MigrationReport()
        if store is None or not callable(getattr(store, "list_candidates", None)):
            report.warnings.append("候选存储不可用")
            return report
        try:
            nodes = store.list_candidates(view="exploration") or []
        except Exception as exc:
            report.malformed_skipped += 1
            report.warnings.append(str(exc))
            return report
        if not isinstance(nodes, (list, tuple, set)):
            report.malformed_skipped += 1
            report.warnings.append("candidate store returned an invalid collection")
            return report
        for node in nodes:
            if node is None:
                report.malformed_skipped += 1
                continue
            try:
                self.repository.save_claim(adapt_candidate(node))
                report.claims += 1
                slots = getattr(node, "evidence_slots", []) or []
                if not isinstance(slots, (list, tuple, set)):
                    slots = []
                for slot in slots:
                    evidence = adapt_legacy_evidence(
                        slot, claim_id=_safe_text(getattr(node, "candidate_id", ""))
                    )
                    if self.repository.save_evidence(evidence):
                        report.evidence += 1
                    else:
                        report.generated_skipped += 1
            except Exception as exc:
                report.malformed_skipped += 1
                report.warnings.append(str(exc))
        return report

