"""Structured verification for graph-first alpha paths.

The verifier only returns structured evidence checks. It never mutates the
knowledge base and it never fills missing evidence from outside the bundle.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from typing import Any

from core.research.perilla.alpha.path_discovery_schema import (
    PathCandidate,
    PathEvidenceBundle,
    PathVerificationResult,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else str(value or "").strip()


def _score(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, min(1.0, number)) if math.isfinite(number) else 0.0


def _mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        try:
            result.append(dict(item))
        except (TypeError, ValueError):
            continue
    return result


def _edge_key(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    return tuple(_text(item) for item in value)


class VerificationPromptBuilder:
    """Build bounded system and user prompts from a path evidence bundle."""

    def build(self, bundle: PathEvidenceBundle, query: str = "") -> tuple[str, str]:
        candidate = (
            bundle.path_candidate
            if isinstance(getattr(bundle, "path_candidate", None), PathCandidate)
            else PathCandidate(path_id="")
        )
        system_prompt = (
            "你是一名严谨的产业链证据审查员。只根据给定的结构路径和证据片段判断路径是否成立，"
            "禁止补充外部知识。输出 JSON，字段包括 is_valid、validity_score、evidence_strength、"
            "bottleneck_type、supported_edges、weak_edges、unsupported_edges、missing_evidence、"
            "contradictions、verifier_notes。"
        )
        nodes = candidate.nodes if isinstance(candidate.nodes, (list, tuple)) else []
        axes = candidate.axes if isinstance(candidate.axes, (list, tuple)) else []
        path_text = " -> ".join(_text(node) for node in nodes if node is not None)
        axes_text = ", ".join(_text(axis) for axis in axes if axis is not None)
        user_prompt = (
            f"查询意图: {_text(query) or '发现产业链 Alpha 机会'}\n"
            f"路径: {path_text}\n"
            f"路径类型: {_text(candidate.path_type) or 'unknown'}\n"
            f"结构轴: {axes_text or '无'}\n"
            f"\n证据片段:\n{self._format_evidence(getattr(bundle, 'evidence_chunks', []))}\n"
            f"\n缺失证据:\n{self._format_missing(getattr(bundle, 'missing_evidence', []))}\n"
            "\n请输出 JSON 格式的结构化验证结果。"
        )
        return system_prompt, user_prompt

    @staticmethod
    def _format_evidence(evidence_chunks: Any) -> str:
        chunks = [item for item in evidence_chunks if isinstance(item, Mapping)] if isinstance(evidence_chunks, (list, tuple)) else []
        if not chunks:
            return "  无证据片段"
        lines: list[str] = []
        for index, chunk in enumerate(chunks[:10], 1):
            lines.append(
                f"  [{index}] 来源={_text(chunk.get('source_type'))} | "
                f"边={chunk.get('edge_key', [])} | 证据={_text(chunk.get('evidence'))[:120]}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_missing(missing_evidence: Any) -> str:
        items = [item for item in missing_evidence if isinstance(item, Mapping)] if isinstance(missing_evidence, (list, tuple)) else []
        if not items:
            return "  无缺失证据"
        lines: list[str] = []
        seen: set[str] = set()
        for item in items:
            key = f"{item.get('head')}|{item.get('relation')}|{item.get('tail')}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"  - {_text(item.get('head'))} -> {_text(item.get('tail'))} ({_text(item.get('reason'))})"
            )
        return "\n".join(lines) or "  无缺失证据"


class VerificationResultParser:
    """Parse untrusted LLM JSON into a safe verification result."""

    def parse(self, raw: Any, path_id: Any) -> PathVerificationResult:
        try:
            data = json.loads(raw) if isinstance(raw, str) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            data = None
        if not isinstance(data, Mapping):
            logger.warning("LLM path verification returned invalid JSON")
            return PathVerificationResult(
                path_id=_text(path_id),
                is_valid=False,
                verifier_notes="verification_failed: invalid JSON output",
            )
        return PathVerificationResult(
            path_id=_text(path_id),
            is_valid=data.get("is_valid") is True,
            validity_score=_score(data.get("validity_score")),
            evidence_strength=_score(data.get("evidence_strength")),
            bottleneck_type=_text(data.get("bottleneck_type")) or "unknown",
            supported_edges=self._parse_edge_list(data.get("supported_edges")),
            weak_edges=self._parse_edge_list(data.get("weak_edges")),
            unsupported_edges=self._parse_edge_list(data.get("unsupported_edges")),
            missing_evidence=_mappings(data.get("missing_evidence")),
            contradictions=_mappings(data.get("contradictions")),
            verifier_notes=_text(data.get("verifier_notes")),
        )

    @staticmethod
    def _parse_edge_list(edges: Any) -> list[tuple[str, str, str]]:
        if not isinstance(edges, (list, tuple)):
            return []
        result: list[tuple[str, str, str]] = []
        for edge in edges:
            if isinstance(edge, (list, tuple)) and len(edge) == 3:
                result.append(tuple(_text(value) for value in edge))
            elif isinstance(edge, str):
                parts = edge.split("|")
                if len(parts) == 3:
                    result.append(tuple(part.strip() for part in parts))
        return result


class LLMPathVerifier:
    """Combine rule-based checks with an optional structured LLM verifier."""

    def __init__(self, llm_client: Any | None = None) -> None:
        self.prompt_builder = VerificationPromptBuilder()
        self.result_parser = VerificationResultParser()
        self.llm_client = llm_client

    def verify(self, bundle: PathEvidenceBundle, query: str = "") -> PathVerificationResult:
        candidate = (
            bundle.path_candidate
            if isinstance(getattr(bundle, "path_candidate", None), PathCandidate)
            else PathCandidate(path_id="")
        )
        path_id = _text(candidate.path_id)
        if self.llm_client is None:
            return self._rule_verify(bundle)
        try:
            system_prompt, user_prompt = self.prompt_builder.build(bundle, query=query)
            raw = self.llm_client.complete(system_prompt, user_prompt)
            return self.result_parser.parse(raw, path_id)
        except Exception as exc:
            logger.warning("LLM path verification failed; using rules: %s", exc)
            return self._rule_verify(bundle)

    def _rule_verify(self, bundle: PathEvidenceBundle) -> PathVerificationResult:
        candidate = (
            bundle.path_candidate
            if isinstance(getattr(bundle, "path_candidate", None), PathCandidate)
            else PathCandidate(path_id="")
        )
        coverage = _score(getattr(bundle, "coverage_score", 0.0))
        chunks = _mappings(getattr(bundle, "evidence_chunks", []))
        contradictions = _mappings(getattr(bundle, "contradiction_signals", []))
        evidence_strength = min(1.0, coverage * 0.6 + min(1.0, len(chunks) / 6.0) * 0.4)
        is_valid = coverage >= 0.5 and not contradictions
        validity_score = _score(evidence_strength * 0.7 + (0.3 if not contradictions else 0.0))

        covered_edge_keys = {
            edge_key
            for item in chunks
            for edge_key in [_edge_key(item.get("edge_key"))]
            if edge_key is not None
        }
        supported: list[tuple[str, str, str]] = []
        weak: list[tuple[str, str, str]] = []
        unsupported: list[tuple[str, str, str]] = []
        for raw_edge in candidate.edges if isinstance(candidate.edges, (list, tuple)) else []:
            if not isinstance(raw_edge, (list, tuple)) or len(raw_edge) != 3:
                continue
            edge = _edge_key(raw_edge)
            if edge is None:
                continue
            if edge not in covered_edge_keys:
                unsupported.append(edge)
                continue
            confidence = max(
                (
                    _score(item.get("confidence"))
                    for item in chunks
                    if _edge_key(item.get("edge_key")) == edge
                ),
                default=0.0,
            )
            (supported if confidence >= 0.6 else weak).append(edge)

        return PathVerificationResult(
            path_id=_text(candidate.path_id),
            is_valid=is_valid,
            validity_score=round(validity_score, 3),
            evidence_strength=round(evidence_strength, 3),
            bottleneck_type=self._infer_bottleneck_type(candidate.path_type, chunks),
            supported_edges=supported,
            weak_edges=weak,
            unsupported_edges=unsupported,
            missing_evidence=_mappings(getattr(bundle, "missing_evidence", [])),
            contradictions=contradictions,
            verifier_notes="rule-based verification",
        )

    @staticmethod
    def _infer_bottleneck_type(path_type: Any, evidence_chunks: Any) -> str:
        path_text = _text(path_type)
        for marker in ("import_dependency", "certification", "equipment", "process", "material"):
            if marker in path_text:
                return marker
        text = " ".join(_text(item.get("evidence")) for item in _mappings(evidence_chunks))
        if "国产" in text or "替代" in text:
            return "domestic_substitution"
        if "产能" in text or "扩产" in text:
            return "capacity"
        if "认证" in text:
            return "certification"
        return "structural_bottleneck"
