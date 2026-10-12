"""Autonomous concept scouting orchestration for Perilla Discovery."""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List

from .concept_discovery_engine import ConceptDiscoveryEngine
from .concept_signal_loader import ConceptSignalLoader
from .evidence_search_task_generator import EvidenceSearchTaskGenerator
from .research_question_engine import ResearchQuestionEngine
from .weak_signal_tracker import WeakSignalTracker

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _finite(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _row_key(row: Mapping[str, Any]) -> str:
    """构造稳定去重键，避免同一快照从多个候选路径重复进入流水线。"""
    try:
        return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(sorted((str(key), repr(value)) for key, value in row.items()))


class ConceptScoutEngine:
    """Run concept discovery, question generation, search tasks, and weak signals."""

    def __init__(
        self,
        project_root: str = ".",
        output_dir: str = "data/reports/perilla_discovery",
        min_search_tasks: int = 20,
    ) -> None:
        self.project_root = Path(project_root)
        self.output_dir = Path(output_dir)
        if not self.output_dir.is_absolute():
            self.output_dir = self.project_root / self.output_dir
        self.loader = ConceptSignalLoader(project_root=self.project_root, output_dir=self.output_dir)
        self.discovery_engine = ConceptDiscoveryEngine(
            project_root=str(self.project_root),
            output_dir=str(self.output_dir),
        )
        self.weak_signal_tracker = WeakSignalTracker()
        self.question_engine = ResearchQuestionEngine()
        self.task_generator = EvidenceSearchTaskGenerator()
        try:
            self.min_search_tasks = max(0, int(min_search_tasks))
        except (TypeError, ValueError, OverflowError):
            self.min_search_tasks = 20

    def run(self) -> Dict[str, Any]:
        discovered = [
            item for item in (self.discovery_engine.run() or [])
            if isinstance(item, Mapping)
        ]
        inbox_items = [
            item for item in (self.loader.load_inbox_items() or [])
            if isinstance(item, Mapping)
        ]
        company_candidates = [
            item for item in (self.loader.load_company_candidates() or [])
            if isinstance(item, Mapping)
        ]
        market_records = self._load_market_records()
        market_anomalies = self.weak_signal_tracker.identify_market_anomalies(market_records)

        weak_signals = self.weak_signal_tracker.build_signals(
            discovered_concepts=discovered,
            company_candidates=company_candidates,
            inbox_items=inbox_items,
        )
        questions = self.question_engine.generate_questions(
            discovered_concepts=discovered,
            weak_signals=weak_signals,
        )
        search_tasks = self.task_generator.generate_tasks(
            discovered_concepts=discovered,
            research_questions=questions,
            market_anomalies=market_anomalies,
            min_tasks=self.min_search_tasks,
        )

        questions_path = self.loader.write_research_questions(questions)
        weak_signals_path = self.loader.write_weak_signals(weak_signals)
        tasks_path = self.loader.write_evidence_search_tasks(search_tasks)
        items_path = self._write_discovery_items_md(discovered, weak_signals)

        logger.info("ConceptScoutEngine: discovered concepts=%d", len(discovered))
        logger.info("ConceptScoutEngine: research questions=%d", len(questions))
        logger.info("ConceptScoutEngine: weak signals=%d", len(weak_signals))
        logger.info("ConceptScoutEngine: evidence search tasks=%d", len(search_tasks))

        return {
            "discovered_concepts": discovered,
            "research_questions": questions,
            "weak_signals": weak_signals,
            "evidence_search_tasks": search_tasks,
            "market_anomalies": market_anomalies,
            "outputs": {
                "discovered_concepts": str(self.loader.discovered_concepts_path),
                "research_questions": str(questions_path),
                "weak_signals": str(weak_signals_path),
                "evidence_search_tasks": str(tasks_path),
                "perilla_discovery_items": str(items_path),
            },
        }

    def _load_market_records(self) -> List[Dict[str, Any]]:
        paths = [
            self.project_root / "data" / "market_anomalies_latest.json",
            self.project_root / "data" / "reports" / "market_anomalies_latest.json",
            self.output_dir / "market_anomalies_latest.json",
            self.project_root / "data" / "reports" / "market_snapshot_latest.json",
        ]
        rows: list[dict[str, Any]] = []
        seen_rows: set[str] = set()
        seen_paths: set[Path] = set()
        for path in paths:
            try:
                resolved_path = path.resolve()
            except OSError:
                continue
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            try:
                exists = path.exists()
            except OSError:
                continue
            if not exists:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("records") or data.get("data")
            else:
                items = ()
            for item in items if isinstance(items, list) else ():
                if not isinstance(item, dict):
                    continue
                key = _row_key(item)
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                rows.append(item)
        return rows

    def _write_discovery_items_md(
        self,
        discovered: List[Dict[str, Any]],
        weak_signals: List[Dict[str, Any]],
    ) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / "perilla_discovery_items_latest.md"
        lines = [
            "# \u7d2b\u82cf\u53d1\u73b0\u6761\u76ee",
            "",
            "\u672c\u6587\u4ef6\u7531 ConceptScoutEngine \u6839\u636e inbox\u3001\u6807\u9898\u548c\u5f31\u4fe1\u53f7\u751f\u6210\uff0c\u4ec5\u4f9b\u7814\u7a76\u8ddf\u8e2a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae\u3002",
            "",
            "## \u65b0\u6982\u5ff5\u7ebf\u7d22",
            "",
        ]
        discovered = [item for item in discovered if isinstance(item, Mapping)]
        weak_signals = [item for item in weak_signals if isinstance(item, Mapping)]
        if not discovered:
            lines.append("- \u6682\u65e0\u65b0\u6982\u5ff5\u7ebf\u7d22")
        for item in discovered[:30]:
            name = _text(item.get("normalized_name")) or _text(item.get("concept_name"))
            lines.extend(
                [
                    f"### {name}",
                    f"- \u4e3b\u7ebf: {item.get('root_theme', '')}",
                    f"- \u7236\u6982\u5ff5: {item.get('parent_concept', '')}",
                    f"- \u8bc1\u636e\u7b49\u7ea7: {item.get('evidence_level', '')}",
                    f"- \u6982\u5ff5\u5206: {_finite(item.get('concept_score')):.4f}",
                    f"- \u53d1\u73b0\u7406\u7531: {item.get('discovery_reason', '')}",
                    "",
                ]
            )

        lines.extend(["## \u5f85\u9a8c\u8bc1\u516c\u53f8\u5f31\u4fe1\u53f7", ""])
        if not weak_signals:
            lines.append("- \u6682\u65e0\u5f31\u4fe1\u53f7")
        for signal in weak_signals[:30]:
            lines.extend(
                [
                    f"### {signal.get('company_name', '')} / {signal.get('concept_name', '')}",
                    f"- \u7c7b\u578b: {signal.get('signal_type', '')}",
                    f"- \u8bc1\u636e\u7b49\u7ea7: {signal.get('evidence_level', '')}",
                    f"- \u72b6\u6001: {signal.get('status', '')}",
                    f"- \u6458\u8981: {signal.get('signal_summary', '')}",
                    "",
                ]
            )
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
