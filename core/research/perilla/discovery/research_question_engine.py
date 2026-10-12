"""Generate research questions from discovered concepts and weak evidence gaps."""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Dict, Iterable, List

from .evidence_search_task_generator import CHAIN_TEMPLATE_NODES, SAFE_EVIDENCE_LEVELS


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, parsed)


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, (str, bytes, Mapping)) or value is None:
        return []
    try:
        items = list(value)
    except Exception:
        return []
    return [item for item in items if isinstance(item, Mapping)]


class ResearchQuestionEngine:
    """Create a pending question pool for autonomous Perilla research."""

    def generate_questions(
        self,
        discovered_concepts: Iterable[Dict[str, Any]],
        weak_signals: Iterable[Dict[str, Any]] | None = None,
        max_questions: int = 80,
    ) -> List[Dict[str, Any]]:
        questions: list[dict[str, Any]] = []
        seen: set[str] = set()

        for concept in _rows(discovered_concepts):
            questions.extend(self._questions_for_concept(concept, seen))

        for signal in _rows(weak_signals):
            question = self._question_for_weak_signal(signal)
            if question and question["question"] not in seen:
                seen.add(question["question"])
                questions.append(question)

        questions.sort(key=lambda item: _safe_float(item.get("priority_score")), reverse=True)
        return questions[:_safe_int(max_questions, 80)]

    def _questions_for_concept(
        self,
        concept: Dict[str, Any],
        seen: set[str],
    ) -> List[Dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not isinstance(concept, Mapping):
            return rows
        name = _text(concept.get("normalized_name") or concept.get("concept_name"))
        if not name:
            return rows
        root_theme = _text(concept.get("root_theme"))
        raw_companies = concept.get("related_companies")
        if isinstance(raw_companies, str):
            raw_companies = [raw_companies]
        if not isinstance(raw_companies, (list, tuple)):
            raw_companies = []
        companies = [_text(company) for company in raw_companies or [] if _text(company)][:8]
        priority = _safe_float(concept.get("concept_score"))
        evidence_level = self._safe_level(_text(concept.get("evidence_level"), "HYPOTHESIS"))
        reason = _text(concept.get("discovery_reason"))

        base_questions = [
            (
                f"{name} \u5728 {root_theme} \u4e3b\u7ebf\u4e2d\u5bf9\u5e94\u54ea\u4e9b\u4ea7\u4e1a\u94fe\u8282\u70b9\uff1f",
                "\u9700\u8981\u516c\u5f00\u8d44\u6599\u8bc1\u660e\u5176\u4ea7\u4e1a\u94fe\u5c42\u7ea7\u548c\u4e0a\u4e0b\u6e38\u5173\u7cfb",
                [f"{name} \u4ea7\u4e1a\u94fe", f"{name} \u4e0a\u4e0b\u6e38"],
            ),
            (
                f"{name} \u662f\u5426\u5b58\u5728\u74f6\u9888\u8282\u70b9\u6216\u66ff\u4ee3\u8def\u7ebf\uff1f",
                "\u9700\u8981\u6280\u672f\u8def\u7ebf\u3001\u74f6\u9888\u548c\u66ff\u4ee3\u65b9\u6848\u8bc1\u636e",
                [f"{name} \u74f6\u9888", f"{name} \u66ff\u4ee3\u8def\u7ebf", f"{name} \u964d\u672c"],
            ),
            (
                f"{name} \u76f8\u5173\u516c\u53f8\u662f\u5426\u53ea\u662f\u6982\u5ff5\u5171\u73b0\uff0c\u8fd8\u662f\u6709\u660e\u786e\u4f9b\u8d27\u6216\u8ba4\u8bc1\u8bc1\u636e\uff1f",
                "\u9700\u8981\u5b98\u7f51\u3001\u516c\u544a\u3001\u5e74\u62a5\u3001\u5ba2\u6237\u8ba4\u8bc1\u6216\u660e\u786e\u4f9b\u8d27\u8bc1\u636e",
                [f"{name} \u516c\u544a \u4f9b\u8d27", f"{name} \u5ba2\u6237\u8ba4\u8bc1", f"{name} \u5e74\u62a5"],
            ),
        ]

        for text, evidence_needed, queries in base_questions:
            if text in seen:
                continue
            seen.add(text)
            rows.append(
                self._build_question(
                    question=text,
                    root_theme=root_theme,
                    concepts=[name],
                    companies=companies,
                    hypothesis=reason or f"{name} \u5728 {root_theme} \u4e3b\u7ebf\u4e2d\u51fa\u73b0\u9ad8\u9891\u5171\u73b0",
                    evidence_needed=[evidence_needed],
                    search_queries=queries,
                    priority_score=priority,
                    evidence_level=evidence_level,
                )
            )

        source_items = concept.get("source_items")
        if not isinstance(source_items, (list, tuple)):
            source_items = []
        known_nodes = {
            str(source.get("chain_node") or "").strip()
            for source in source_items
            if isinstance(source, Mapping) and str(source.get("chain_node") or "").strip()
        }
        for node in CHAIN_TEMPLATE_NODES:
            if node in known_nodes:
                continue
            text = f"{name} \u7684\u201c{node}\u201d\u8282\u70b9\u662f\u5426\u5b58\u5728\u516c\u5f00\u8bc1\u636e\uff1f"
            if text in seen:
                continue
            seen.add(text)
            rows.append(
                self._build_question(
                    question=text,
                    root_theme=root_theme,
                    concepts=[name],
                    companies=companies,
                    hypothesis=f"{name} \u7684 {node} \u8282\u70b9\u5c1a\u672a\u88ab\u672c\u5730\u8bc1\u636e\u586b\u5145",
                    evidence_needed=[f"\u9700\u8981 {node} \u7684\u516c\u5f00\u8bc1\u636e"],
                    search_queries=[f"{name} {node}", f"{name} {node} \u516c\u5f00\u8d44\u6599"],
                    priority_score=max(priority - 0.05, 0.0),
                    evidence_level="HYPOTHESIS",
                )
            )
        return rows

    def _question_for_weak_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(signal, Mapping):
            return {}
        company = _text(signal.get("company_name"))
        concept = _text(signal.get("concept_name"))
        if not company or not concept:
            return {}
        root_theme = _text(signal.get("root_theme"))
        return self._build_question(
            question=f"{company} \u4e0e {concept} \u7684\u5173\u8054\u662f\u5f31\u4fe1\u53f7\u8fd8\u662f\u53ef\u9a8c\u8bc1\u4f9b\u8d27\u8bc1\u636e\uff1f",
            root_theme=root_theme,
            concepts=[concept],
            companies=[company],
            hypothesis=_text(signal.get("signal_summary")),
            evidence_needed=[
                "\u9700\u8981\u516c\u544a\u3001\u5b98\u7f51\u3001\u5e74\u62a5\u3001\u5ba2\u6237\u8ba4\u8bc1\u6216\u660e\u786e\u4f9b\u8d27\u8bc1\u636e"
            ],
            search_queries=[
                f"{company} {concept} \u516c\u544a",
                f"{company} {concept} \u5ba2\u6237\u8ba4\u8bc1",
                f"{company} {concept} \u62db\u8058",
            ],
            priority_score=_safe_float(signal.get("priority_score"), 0.4),
            evidence_level=self._safe_level(_text(signal.get("evidence_level"), "HYPOTHESIS")),
        )

    @staticmethod
    def _build_question(
        question: str,
        root_theme: str,
        concepts: List[str],
        companies: List[str],
        hypothesis: str,
        evidence_needed: List[str],
        search_queries: List[str],
        priority_score: float,
        evidence_level: str,
    ) -> Dict[str, Any]:
        return {
            "question_id": f"rq_{_short_hash(question)}",
            "question": _text(question),
            "root_theme": _text(root_theme),
            "related_concepts": [_text(item) for item in concepts if _text(item)],
            "related_companies": [_text(item) for item in companies if _text(item)],
            "hypothesis": _text(hypothesis),
            "evidence_needed": [_text(item) for item in evidence_needed if _text(item)],
            "search_queries": [_text(item) for item in search_queries if _text(item)],
            "priority_score": round(min(max(_safe_float(priority_score), 0.0), 1.0), 4),
            "status": "pending",
            "last_checked_at": datetime.now().isoformat(),
            "evidence_level": evidence_level,
        }

    @staticmethod
    def _safe_level(level: str) -> str:
        return level if level in SAFE_EVIDENCE_LEVELS else "HYPOTHESIS"


def _short_hash(text: str) -> str:
    return hashlib.sha1(_text(text).encode("utf-8")).hexdigest()[:12]
