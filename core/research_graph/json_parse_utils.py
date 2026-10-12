"""robust_json_parse:尽力解析 LLM 返回的 JSON,失败则返回 ParseFailure。

设计原则:
- 清理 markdown fence(```json ... ```)
- 提取最外层 JSON(括号配对)
- 最小修复:去 trailing comma,补齐缺失括号
- 1 次重试(更硬的 prompt)
- 失败记入 rejected_extractions.json,不中断主流程
- 追踪记入 llm_extraction_trace.jsonl
- 汇总统计 validation_summary,parse_failure_rate > 10% 自动降级纯正则

不涉及整个 Pipeline,只聚焦 JSON 处理。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# prompt 版本标识,用于追踪
PROMPT_VERSION = "evidence_extractor_v1"


@dataclass
class ParseFailure:
    """JSON 解析失败结果。

    Attributes:
        raw_text: LLM 原始输出。
        error_type: 失败类型(no_json_object / json_parse_failed)。
        chunk_id: 关联的 chunk ID。
    """

    raw_text: str
    error_type: str
    chunk_id: str


def robust_json_parse(raw_text: str, chunk_id: str) -> dict | list | ParseFailure:
    """尽力解析 LLM 返回的 JSON,失败则返回 ParseFailure。

    流程:
    1. 清理 markdown fence(```json ... ```)
    2. 提取最外层 JSON(括号配对)
    3. 第一次 json.loads 尝试
    4. 最小修复:去 trailing comma,补齐缺失括号
    5. 失败返回 ParseFailure

    Args:
        raw_text: LLM 原始输出。
        chunk_id: 关联的 chunk ID,用于审计追踪。

    Returns:
        解析后的 dict/list,或 ParseFailure。
    """
    if not raw_text or not raw_text.strip():
        return ParseFailure(raw_text or "", "no_json_object", chunk_id)

    # 1. 清理 markdown fence
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw_text).strip()

    # 2. 提取最外层 JSON(括号配对,支持 { 和 [)
    start_char = "{["
    start = -1
    for i, ch in enumerate(cleaned):
        if ch in start_char:
            start = i
            break
    if start == -1:
        return ParseFailure(raw_text, "no_json_object", chunk_id)

    open_ch = cleaned[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    end = -1
    for i in range(start, len(cleaned)):
        if cleaned[i] == open_ch:
            depth += 1
        elif cleaned[i] == close_ch:
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        # 没有闭合,尝试修复整个尾部
        json_str = cleaned[start:]
    else:
        json_str = cleaned[start : end + 1]

    # 3. 第一次解析尝试
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # 4. 最小修复:去 trailing comma,补齐括号
    repaired = _minimal_repair(json_str, open_ch, close_ch)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        return ParseFailure(raw_text, "json_parse_failed", chunk_id)


def _minimal_repair(json_str: str, open_ch: str, close_ch: str) -> str:
    """轻量修复,不做过度猜测。

    - 去 trailing comma(,} 或 ,])
    - 补齐缺失的括号

    Args:
        json_str: 待修复的 JSON 字符串。
        open_ch: 起始括号({ 或 [)。
        close_ch: 结束括号(} 或 ])。

    Returns:
        修复后的 JSON 字符串。
    """
    # 去 trailing comma
    repaired = re.sub(r",\s*([}\]])", r"\1", json_str)

    # 补齐缺失的括号
    open_braces = repaired.count("{") - repaired.count("}")
    open_brackets = repaired.count("[") - repaired.count("]")

    if open_brackets > 0:
        repaired += "]" * open_brackets
    if open_braces > 0:
        repaired += "}" * open_braces

    return repaired


# 重试用的硬 prompt(只允许纯 JSON,无 markdown,无解释)
RETRY_PROMPT = """Return ONLY valid JSON. No markdown. No explanation.
Schema:
{"items":[{"subject":"","subject_type":"","relationship":"","object":"","object_type":"","stance":"","quote":"","confidence":0.0,"reason":""}]}
"""


@dataclass
class ExtractionTrace:
    """单条 chunk 的处理追踪记录。"""

    trace_id: str
    chunk_id: str
    parse_status: str  # success | repaired | retry_success | failed
    parse_error_type: str = ""
    retry_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0


@dataclass
class ValidationSummary:
    """LLM extraction 汇总统计,用于分级降级策略。"""

    chunks_attempted: int = 0
    parse_failed: int = 0
    parse_failure_rate: float = 0.0
    retried: int = 0
    recovered_after_retry: int = 0
    final_parse_failed: int = 0
    grade: str = "PASSED"  # PASSED | PASSED_WITH_WARNINGS | FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_extraction": {
                "chunks_attempted": self.chunks_attempted,
                "parse_failed": self.parse_failed,
                "parse_failure_rate": round(self.parse_failure_rate, 4),
                "retried": self.retried,
                "recovered_after_retry": self.recovered_after_retry,
                "final_parse_failed": self.final_parse_failed,
                "grade": self.grade,
            }
        }


class ExtractionTraceLogger:
    """追踪与拒绝记录写入器。

    - rejected_extractions.json:每条解析失败的 chunk 一行
    - llm_extraction_trace.jsonl:每条 chunk 的处理结果一行
    - validation_summary:汇总统计,parse_failure_rate > 10% 自动降级
    """

    def __init__(self, run_dir: Path, model_name: str = "") -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.rejected_path = self.run_dir / "rejected_extractions.jsonl"
        self.trace_path = self.run_dir / "llm_extraction_trace.jsonl"
        self.summary_path = self.run_dir / "validation_summary.json"
        self.model_name = model_name
        self.summary = ValidationSummary()

    def record_trace(self, trace: ExtractionTrace) -> None:
        """写入一条 trace 记录。"""
        self.summary.chunks_attempted += 1
        if trace.parse_status in ("failed",):
            self.summary.parse_failed += 1
        if trace.retry_count > 0:
            self.summary.retried += 1
        if trace.parse_status == "retry_success":
            self.summary.recovered_after_retry += 1
        if trace.parse_status == "failed":
            self.summary.final_parse_failed += 1

        row = {
            "trace_id": trace.trace_id,
            "chunk_id": trace.chunk_id,
            "parse_status": trace.parse_status,
            "parse_error_type": trace.parse_error_type,
            "retry_count": trace.retry_count,
            "accepted_count": trace.accepted_count,
            "rejected_count": trace.rejected_count,
            "estimated_input_tokens": trace.estimated_input_tokens,
            "estimated_output_tokens": trace.estimated_output_tokens,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def record_rejected(
        self,
        chunk_id: str,
        source_id: str,
        document_id: str,
        stage: str,
        failure: ParseFailure,
        retry_count: int,
    ) -> None:
        """写入一条 rejected 记录。"""
        raw_hash = "sha256:" + hashlib.sha256(
            failure.raw_text.encode("utf-8")
        ).hexdigest()
        row = {
            "chunk_id": chunk_id,
            "source_id": source_id,
            "document_id": document_id,
            "stage": stage,
            "reason": failure.error_type,
            "parse_error_type": failure.error_type,
            "raw_output_hash": raw_hash,
            "raw_output_preview": failure.raw_text[:300],
            "retry_count": retry_count,
            "model_name": self.model_name,
            "prompt_version": PROMPT_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with self.rejected_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def finalize(self) -> dict[str, Any]:
        """计算汇总统计,写入 validation_summary.json,返回分级结果。

        分级策略:
        - parse_failure_rate ≤ 3% → PASSED
        - 3% < rate ≤ 10% → PASSED_WITH_WARNINGS
        - rate > 10% → FAILED,自动关闭 LLM extraction,降级为纯正则
        """
        if self.summary.chunks_attempted > 0:
            self.summary.parse_failure_rate = (
                self.summary.final_parse_failed / self.summary.chunks_attempted
            )
        if self.summary.parse_failure_rate <= 0.03:
            self.summary.grade = "PASSED"
        elif self.summary.parse_failure_rate <= 0.10:
            self.summary.grade = "PASSED_WITH_WARNINGS"
        else:
            self.summary.grade = "FAILED"

        self.summary_path.write_text(
            json.dumps(self.summary.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.summary.to_dict()
