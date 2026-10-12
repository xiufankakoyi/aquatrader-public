"""File loading helpers for Perilla concept discovery."""
from __future__ import annotations

import json
import csv
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .models import ConceptSignal, new_id, now_iso


class ConceptSignalLoader:
    """Load and write concept discovery inputs using tolerant JSON parsing."""

    def __init__(
        self,
        project_root: str | Path = ".",
        output_dir: str | Path = "data/reports/perilla_discovery",
    ) -> None:
        self.project_root = Path(project_root)
        self.output_dir = self._resolve(output_dir)
        self.inbox_path = self.project_root / "data" / "research_inbox" / "inbox_items.json"
        self.concept_signals_path = (
            self.project_root
            / "data"
            / "reports"
            / "perilla_discovery"
            / "concept_signals_latest.json"
        )
        self.company_candidates_path = (
            self.project_root
            / "data"
            / "reports"
            / "perilla_discovery"
            / "company_candidates_latest.json"
        )
        self.discovered_concepts_path = self.output_dir / "discovered_concepts_latest.json"
        self.research_questions_path = self.output_dir / "research_questions_latest.json"
        self.weak_signals_path = self.output_dir / "weak_signals_latest.json"
        self.evidence_search_tasks_path = self.output_dir / "evidence_search_tasks_latest.json"

    def load_inbox_items(self) -> List[Dict[str, Any]]:
        data = self._read_json(self.inbox_path)
        if isinstance(data, dict):
            return [item for item in data.values() if isinstance(item, dict)]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def load_concept_signals(self) -> List[Dict[str, Any]]:
        data = self._read_json(self.concept_signals_path)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("concepts") or data.get("items") or data.get("signals")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def load_company_candidates(self) -> List[Dict[str, Any]]:
        data = self._read_json(self.company_candidates_path)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("candidates") or data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def load_title_items(self) -> List[Dict[str, Any]]:
        """Load lightweight title-only items from local news/report files.

        递归扫描 data/documents/ 下所有子目录的 .txt 文件，避免遗漏 other 等子目录。
        """
        rows: list[dict[str, Any]] = []
        documents_dir = self.project_root / "data" / "documents"
        if not documents_dir.exists():
            return rows

        # 递归扫描所有子目录的 .txt 文件
        for file_path in sorted(documents_dir.rglob("*.txt")):
            subdir = file_path.parent.name
            category = "news_title" if subdir == "news" else "report_title"
            title = file_path.stem.split("_", 1)[-1]
            rows.append(
                {
                    "item_id": f"{category}_{file_path.stem}",
                    "title": title,
                    "category": category,
                    "content": _read_text_head(file_path),
                    "last_updated_at": _file_mtime_iso(file_path),
                }
            )

        sample_news_path = self.project_root / "data" / "sample_news.csv"
        if sample_news_path.exists():
            try:
                with sample_news_path.open("r", encoding="utf-8-sig", newline="") as fh:
                    for idx, row in enumerate(csv.DictReader(fh)):
                        rows.append(
                            {
                                "item_id": f"sample_news_{idx}",
                                "title": row.get("title", ""),
                                "summary": row.get("summary", ""),
                                "category": "sample_news",
                                "tags": [
                                    row.get("related_symbols", ""),
                                    row.get("related_concepts", ""),
                                ],
                                "last_updated_at": row.get("publish_time", ""),
                            }
                        )
            except Exception:
                pass
        return rows

    def load_discovered_concepts(self) -> List[Dict[str, Any]]:
        data = self._read_json(self.discovered_concepts_path)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("concepts") or data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def write_discovered_concepts(self, concepts: Iterable[Dict[str, Any]]) -> Path:
        return self._write_json_list(self.discovered_concepts_path, concepts)

    def write_research_questions(self, questions: Iterable[Dict[str, Any]]) -> Path:
        return self._write_json_list(self.research_questions_path, questions)

    def write_weak_signals(self, signals: Iterable[Dict[str, Any]]) -> Path:
        return self._write_json_list(self.weak_signals_path, signals)

    def write_evidence_search_tasks(self, tasks: Iterable[Dict[str, Any]]) -> Path:
        return self._write_json_list(self.evidence_search_tasks_path, tasks)

    def to_concept_signals(
        self,
        discovered_concepts: Iterable[Dict[str, Any]],
        min_score: float = 0.2,
    ) -> List[ConceptSignal]:
        signals: list[ConceptSignal] = []
        try:
            min_score = _to_float(min_score)
            items = iter(discovered_concepts or [])
        except TypeError:
            return signals
        for item in items:
            if not isinstance(item, Mapping):
                continue
            score = _to_float(item.get("concept_score"))
            if score < min_score:
                continue
            name = str(item.get("normalized_name") or item.get("concept_name") or "").strip()
            if not name:
                continue
            related_terms = item.get("related_terms", [])
            if not isinstance(related_terms, (list, tuple)):
                related_terms = []
            aliases = [
                str(term)
                for term in related_terms
                if str(term).strip() and str(term).strip() != name
            ]
            source_items = item.get("source_items") or []
            evidence_ids = []
            if isinstance(source_items, (list, tuple)):
                for source in source_items:
                    if isinstance(source, Mapping) and source.get("item_id"):
                        evidence_ids.append(str(source["item_id"]))

            signals.append(
                ConceptSignal(
                    concept_id=new_id("cs_disc_"),
                    name=name,
                    aliases=sorted(set(aliases)),
                    detected_from=["discovered_concepts_latest", "research_inbox"],
                    source_types=["research_inbox"],
                    first_seen_at=str(item.get("first_seen_at") or now_iso()),
                    last_seen_at=str(item.get("last_seen_at") or now_iso()),
                    heat_score=score,
                    novelty_score=_to_float(item.get("novelty_score"), default=score),
                    confidence=_confidence_from_score(score),
                    raw_evidence_ids=sorted(set(evidence_ids)),
                    notes=(
                        f"root_theme={item.get('root_theme', '')}; "
                        f"parent_concept={item.get('parent_concept', '')}; "
                        f"evidence_level={item.get('evidence_level', '')}; "
                        f"discovery_reason={item.get('discovery_reason', '')}"
                    ),
                )
            )
        return signals

    def _resolve(self, path: str | Path) -> Path:
        p = Path(path)
        if p.is_absolute():
            return p
        return self.project_root / p

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            if not path.exists():
                return None
        except (OSError, TypeError, ValueError):
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_json_list(
        self,
        path: Path,
        rows: Iterable[Dict[str, Any]],
    ) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = [row for row in (rows or []) if isinstance(row, Mapping)]
        except TypeError:
            data = []
        temp_path: Path | None = None
        try:
            fd, raw_temp_path = tempfile.mkstemp(
                prefix=f".{path.name}.",
                dir=path.parent,
            )
            temp_path = Path(raw_temp_path)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except OSError:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise
        return path


def serialize_concept_signals(signals: Iterable[ConceptSignal]) -> List[Dict[str, Any]]:
    try:
        return [asdict(signal) for signal in (signals or []) if isinstance(signal, ConceptSignal)]
    except TypeError:
        return []


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _confidence_from_score(score: float) -> str:
    if score >= 0.7:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _read_text_head(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""


def _file_mtime_iso(path: Path) -> str:
    try:
        from datetime import datetime

        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except Exception:
        return ""
