"""P4B 证据演化追踪器。

追踪 research_hypotheses 的证据状态变化，输出 evidence_timeline / belief_state / post_mortem。

hybrid 模式：
- posterior_probability 公式始终用规则计算（确定性）
- LLM 仅辅助评估 evidence_strength/evidence_velocity 的归一化值
- LLM 失败时降级到规则归一化

belief_state.state 按 5 档分级，禁止 confirmed。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.llm_processing.llm_cache import LLMCache
from core.llm_processing.prompt_templates import (
    P4B_PROMPT_VERSION,
    build_p4b_belief_prompt,
    check_compliance,
    get_p4b_belief_system_prompt,
)

logger = logging.getLogger(__name__)

_DEFAULT_INPUT_DIR = Path("data/reports/perilla_discovery")
_DEFAULT_OUTPUT_DIR = Path("data/reports/perilla_discovery")
_DEFAULT_CACHE_DIR = Path("data/llm_cache/extractions")
try:
    _MAX_LLM_ITEMS = max(0, min(1000, int(os.getenv("AQUARESEARCH_LLM_MAX_ITEMS", "20") or "20")))
except (TypeError, ValueError, OverflowError):
    _MAX_LLM_ITEMS = 20

# event_type 限定 6 类
_ALLOWED_EVENT_TYPES: Set[str] = {
    "hypothesis_created", "evidence_added", "edge_added",
    "search_task_created", "skepticism_updated", "market_move_observed",
}

# belief_state.state 5 档分级（禁止 confirmed）
_ALLOWED_BELIEF_STATES: Set[str] = {
    "rejected_or_low_priority", "hypothesis", "weak_signal",
    "watchlist_candidate", "needs_manual_review",
}

# 默认先验概率
_DEFAULT_PRIOR = 0.2


def _short_hash(text: str) -> str:
    """计算短哈希。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    """当前时间 ISO 格式。"""
    return datetime.now(timezone.utc).isoformat()


def _records(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return normalized if math.isfinite(normalized) else default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _bounded_int(value: Any, default: int, maximum: int = 1000) -> int:
    try:
        number = int(float(value))
    except (OverflowError, TypeError, ValueError):
        number = default
    return max(0, min(maximum, number))


def _local_llm_enabled() -> bool:
    """环境变量层面的 LLM 开关。"""
    return os.getenv("LOCAL_LLM_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 返回的 JSON 对象。"""
    clean = str(text or "").strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    try:
        parsed = json.loads(clean)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(clean[start:end])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _call_aqua_llm_json(llm: Any, user_prompt: str, system_prompt: str) -> Optional[Dict[str, Any]]:
    """复用 AquaLLM 的 report 接口调用 JSON LLM。"""
    if hasattr(llm, "generate_json"):
        parsed = llm.generate_json(system_prompt=system_prompt, user_prompt=user_prompt)
        return parsed if isinstance(parsed, dict) else None
    content = llm.generate_report(user_prompt=user_prompt, system_prompt=system_prompt)
    return _parse_json_object(content)


def _llm_model_name(llm: Any) -> str:
    """尽量从 AquaLLM 或 mock 中取得模型名。"""
    if hasattr(llm, "model_name"):
        return str(getattr(llm, "model_name") or "")
    try:
        from config.config import Config
        return str(getattr(Config, "LLM_MODEL_NAME", "") or "")
    except Exception:
        return ""


def _cache_get_json(cache: LLMCache, raw_hash: str, prompt_suffix: str, model_name: str) -> Optional[Dict[str, Any]]:
    """读取 JSON LLM 缓存。"""
    cache_key = LLMCache._compute_cache_key(raw_hash, prompt_suffix, P4B_PROMPT_VERSION, model_name)
    cache_file = cache._cache_path(cache_key)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if data.get("prompt_version") != P4B_PROMPT_VERSION:
            return None
        parsed = data.get("parsed_result")
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.debug("P4B LLM 缓存读取失败: %s", exc)
        return None


def _cache_set_json(
    cache: LLMCache,
    raw_hash: str,
    prompt_suffix: str,
    model_name: str,
    parsed: Dict[str, Any],
) -> None:
    """写入 JSON LLM 缓存。"""
    try:
        cache_key = LLMCache._compute_cache_key(raw_hash, prompt_suffix, P4B_PROMPT_VERSION, model_name)
        cache_file = cache._cache_path(cache_key)
        cache_file.write_text(
            json.dumps({
                "raw_hash": raw_hash,
                "chunk_text_hash": prompt_suffix,
                "prompt_version": P4B_PROMPT_VERSION,
                "model_name": model_name,
                "parsed_result": parsed,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("P4B LLM 缓存写入失败: %s", exc)


def load_inputs(input_dir: Optional[Path] = None) -> Dict[str, Any]:
    """加载 P4B 输入数据。

    读取 research_hypotheses / evidence_items / evidence_edges / search_tasks /
    skepticism_scores，可选读取 abnormal_moves_latest.json。

    Args:
        input_dir: 输入目录

    Returns:
        dict，含各输入数据
    """
    base = Path(input_dir) if input_dir else _DEFAULT_INPUT_DIR
    result: Dict[str, Any] = {
        "research_hypotheses": [],
        "evidence_items": [],
        "evidence_edges": [],
        "search_tasks": [],
        "skepticism_scores": [],
        "abnormal_moves": [],
    }

    for key, filename in [
        ("research_hypotheses", "research_hypotheses_latest.json"),
        ("evidence_items", "evidence_items_latest.json"),
        ("evidence_edges", "evidence_edges_latest.json"),
        ("search_tasks", "search_tasks_from_missing_edges_latest.json"),
        ("skepticism_scores", "skepticism_scores_latest.json"),
    ]:
        path = base / filename
        if path.exists():
            try:
                result[key] = _records(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("读取 %s 失败: %s", filename, exc)

    # 可选：读取市场异动文件
    market_path = base / "abnormal_moves_latest.json"
    if market_path.exists():
        try:
            result["abnormal_moves"] = _records(
                json.loads(market_path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取 abnormal_moves_latest.json 失败: %s", exc)

    return result


def build_evidence_timeline(
    hypotheses: List[Dict[str, Any]],
    evidence_items: List[Dict[str, Any]],
    evidence_edges: List[Dict[str, Any]],
    search_tasks: List[Dict[str, Any]],
    skepticism_scores: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """为每个 hypothesis_id 建立证据时间线。

    event_type 限定 6 类：hypothesis_created / evidence_added / edge_added /
    search_task_created / skepticism_updated / market_move_observed

    Args:
        hypotheses: 假设列表
        evidence_items: 证据项列表
        evidence_edges: 证据边列表
        search_tasks: 搜索任务列表
        skepticism_scores: skepticism_scores 列表

    Returns:
        时间线事件列表
    """
    hypotheses = _records(hypotheses)
    evidence_items = _records(evidence_items)
    evidence_edges = _records(evidence_edges)
    search_tasks = _records(search_tasks)
    skepticism_scores = _records(skepticism_scores)
    timeline: List[Dict[str, Any]] = []

    # 建立 hypothesis_id -> 相关数据映射
    hyp_ids = {_text(h.get("hypothesis_id")) for h in hypotheses if _text(h.get("hypothesis_id"))}
    hyp_evidence_docs: Dict[str, List[str]] = {}
    for hyp in hypotheses:
        hyp_id = _text(hyp.get("hypothesis_id"))
        if not hyp_id:
            continue
        doc_ids = hyp.get("evidence_document_ids", [])
        hyp_evidence_docs[hyp_id] = (
            [_text(doc_id) for doc_id in doc_ids if _text(doc_id)]
            if isinstance(doc_ids, (list, tuple, set)) else []
        )

    # 1. hypothesis_created 事件
    for hyp in hypotheses:
        hyp_id = _text(hyp.get("hypothesis_id"))
        timeline.append({
            "timeline_id": f"tl_{_short_hash(hyp_id + 'created')}",
            "hypothesis_id": hyp_id,
            "event_time": _text(hyp.get("created_at"), _now_iso()),
            "event_type": "hypothesis_created",
            "evidence_source": "hypothesis_engine",
            "evidence_level": "hypothesis",
            "related_document_id": "",
            "quote": _text(hyp.get("hypothesis_text")),
            "delta_score": 0.0,
            "reason": "假设创建",
        })

    # 2. evidence_added 事件（基于 evidence_items）
    for item in evidence_items:
        doc_id = _text(item.get("document_id"))
        # 找到与该文档相关的假设
        for hyp_id, doc_ids in hyp_evidence_docs.items():
            if doc_id in doc_ids:
                timeline.append({
                    "timeline_id": f"tl_{_short_hash(hyp_id + doc_id + 'evidence')}",
                    "hypothesis_id": hyp_id,
                    "event_time": _text(item.get("published_at"), _now_iso()),
                    "event_type": "evidence_added",
                    "evidence_source": _text(item.get("evidence_type"), "unknown"),
                    "evidence_level": _text(item.get("evidence_level"), "hypothesis"),
                    "related_document_id": doc_id,
                    "quote": str(item.get("quote", ""))[:200],
                    "delta_score": 0.1,
                    "reason": "新增证据",
                })

    # 3. edge_added 事件
    for edge in evidence_edges[:20]:  # 限制数量
        source_node = _text(edge.get("source_node"))
        for hyp in hypotheses:
            source_nodes = hyp.get("source_nodes", [])
            normalized_nodes = {
                _text(node) for node in source_nodes
            } if isinstance(source_nodes, (list, tuple, set)) else set()
            if source_node and source_node in normalized_nodes:
                hyp_id = _text(hyp.get("hypothesis_id"))
                edge_id = _text(edge.get("edge_id"))
                timeline.append({
                    "timeline_id": f"tl_{_short_hash(hyp_id + edge_id + 'edge')}",
                    "hypothesis_id": hyp_id,
                    "event_time": _text(edge.get("created_at"), _now_iso()),
                    "event_type": "edge_added",
                    "evidence_source": "evidence_edges",
                    "evidence_level": _text(edge.get("confidence_level"), "hypothesis"),
                    "related_document_id": "",
                    "quote": f"{source_node} -> {_text(edge.get('target_node'))}",
                    "delta_score": 0.05,
                    "reason": "新增证据边",
                })

    # 4. search_task_created 事件
    for task in search_tasks[:20]:
        related_node = _text(task.get("related_node"))
        for hyp in hypotheses:
            source_nodes = hyp.get("source_nodes", [])
            normalized_nodes = {
                _text(node) for node in source_nodes
            } if isinstance(source_nodes, (list, tuple, set)) else set()
            if related_node and related_node in normalized_nodes:
                hyp_id = _text(hyp.get("hypothesis_id"))
                task_id = _text(task.get("task_id"))
                timeline.append({
                    "timeline_id": f"tl_{_short_hash(hyp_id + task_id + 'task')}",
                    "hypothesis_id": hyp_id,
                    "event_time": _text(task.get("created_at"), _now_iso()),
                    "event_type": "search_task_created",
                    "evidence_source": "search_task_compiler",
                    "evidence_level": "hypothesis",
                    "related_document_id": "",
                    "quote": str(task.get("query", "")),
                    "delta_score": 0.0,
                    "reason": "新增搜索任务",
                })

    # 5. skepticism_updated 事件
    for score in skepticism_scores:
        hyp_id = _text(score.get("hypothesis_id"))
        if hyp_id in hyp_ids:
            timeline.append({
                "timeline_id": f"tl_{_short_hash(hyp_id + 'skepticism')}",
                "hypothesis_id": hyp_id,
                "event_time": _now_iso(),
                "event_type": "skepticism_updated",
                "evidence_source": "evidence_review_agent",
                "evidence_level": "hypothesis",
                "related_document_id": "",
                "quote": "",
                "delta_score": -_safe_float(score.get("skepticism_score")) * 0.3,
                "reason": f"skepticism_score 更新为 {score.get('skepticism_score', 0)}",
            })

    # 按时间排序
    timeline.sort(key=lambda x: _text(x.get("event_time")))
    return timeline


def _compute_belief_state_by_rules(
    hyp: Dict[str, Any],
    timeline_events: List[Dict[str, Any]],
    skepticism_score: float,
    market_attention: float,
) -> Dict[str, Any]:
    """规则模式计算 belief_state（确定性，不接 LLM）。

    posterior_probability = prior + evidence_strength*0.3 + evidence_velocity*0.2 +
                            evidence_diversity*0.2 + market_attention*0.1 -
                            skepticism_score*0.3

    Args:
        hyp: 假设
        timeline_events: 该假设的时间线事件
        skepticism_score: skepticism_score
        market_attention: 市场关注度

    Returns:
        belief_state dict
    """
    hyp_id = _text(hyp.get("hypothesis_id"))
    skepticism_score = max(0.0, min(1.0, _safe_float(skepticism_score, 0.5)))
    market_attention = max(0.0, min(1.0, _safe_float(market_attention)))
    timeline_events = _records(timeline_events)

    # prior_probability 默认 0.2
    prior_probability = _DEFAULT_PRIOR

    # evidence_strength: 基于证据数量归一化
    evidence_count = len([e for e in timeline_events if e.get("event_type") == "evidence_added"])
    evidence_strength = round(min(1.0, evidence_count / 5.0), 4)

    # evidence_velocity: 基于最近证据增长速度（简化为证据数/10）
    evidence_velocity = round(min(1.0, evidence_count / 10.0), 4)

    # evidence_diversity: 基于证据来源多样性
    sources = set()
    for e in timeline_events:
        if e.get("evidence_source"):
            sources.add(_text(e["evidence_source"]))
    evidence_diversity = round(min(1.0, len(sources) / 5.0), 4)

    # posterior_probability 公式（始终用规则计算）
    posterior = (
        prior_probability +
        evidence_strength * 0.3 +
        evidence_velocity * 0.2 +
        evidence_diversity * 0.2 +
        market_attention * 0.1 -
        skepticism_score * 0.3
    )
    # 限制在 0-1
    posterior = round(max(0.0, min(1.0, posterior)), 4)

    # state 按 5 档分级
    if posterior < 0.2:
        state = "rejected_or_low_priority"
    elif posterior < 0.4:
        state = "hypothesis"
    elif posterior < 0.6:
        state = "weak_signal"
    elif posterior < 0.75:
        state = "watchlist_candidate"
    else:
        state = "needs_manual_review"

    # 强制禁止 confirmed
    if state == "confirmed":
        state = "needs_manual_review"

    return {
        "hypothesis_id": hyp_id,
        "prior_probability": prior_probability,
        "evidence_strength": evidence_strength,
        "evidence_velocity": evidence_velocity,
        "evidence_diversity": evidence_diversity,
        "skepticism_score": round(skepticism_score, 4),
        "market_attention": round(market_attention, 4),
        "posterior_probability": posterior,
        "state": state,
        "reason": (
            f"prior={prior_probability} + "
            f"evidence_strength*0.3={evidence_strength * 0.3:.4f} + "
            f"evidence_velocity*0.2={evidence_velocity * 0.2:.4f} + "
            f"evidence_diversity*0.2={evidence_diversity * 0.2:.4f} + "
            f"market_attention*0.1={market_attention * 0.1:.4f} - "
            f"skepticism*0.3={skepticism_score * 0.3:.4f} = {posterior}"
        ),
    }


def _state_from_posterior(posterior: float) -> str:
    """按 5 档规则映射 belief_state.state。"""
    if posterior < 0.2:
        return "rejected_or_low_priority"
    if posterior < 0.4:
        return "hypothesis"
    if posterior < 0.6:
        return "weak_signal"
    if posterior < 0.75:
        return "watchlist_candidate"
    return "needs_manual_review"


def _recompute_belief_with_llm_scores(
    belief: Dict[str, Any],
    evidence_strength: float,
    evidence_velocity: float,
    llm_reason: str,
) -> Dict[str, Any]:
    """用 LLM 归一化值替换 evidence_strength/evidence_velocity 后按规则公式重算。"""
    prior = max(0.0, min(1.0, _safe_float(belief.get("prior_probability"), _DEFAULT_PRIOR)))
    diversity = max(0.0, min(1.0, _safe_float(belief.get("evidence_diversity"))))
    skepticism = max(0.0, min(1.0, _safe_float(belief.get("skepticism_score"))))
    market_attention = max(0.0, min(1.0, _safe_float(belief.get("market_attention"))))
    strength = round(max(0.0, min(1.0, _safe_float(evidence_strength))), 4)
    velocity = round(max(0.0, min(1.0, _safe_float(evidence_velocity))), 4)
    posterior = (
        prior +
        strength * 0.3 +
        velocity * 0.2 +
        diversity * 0.2 +
        market_attention * 0.1 -
        skepticism * 0.3
    )
    posterior = round(max(0.0, min(1.0, posterior)), 4)
    updated = dict(belief)
    updated.update({
        "evidence_strength": strength,
        "evidence_velocity": velocity,
        "posterior_probability": posterior,
        "state": _state_from_posterior(posterior),
        "reason": (
            f"prior={prior} + evidence_strength*0.3={strength * 0.3:.4f} + "
            f"evidence_velocity*0.2={velocity * 0.2:.4f} + "
            f"evidence_diversity*0.2={diversity * 0.2:.4f} + "
            f"market_attention*0.1={market_attention * 0.1:.4f} - "
            f"skepticism*0.3={skepticism * 0.3:.4f} = {posterior}; "
            f"LLM 辅助归一化: {llm_reason}"
        ),
        "llm_status": "llm",
    })
    return updated


def _evaluate_belief_with_llm(
    llm: Any,
    cache: LLMCache,
    hyp: Dict[str, Any],
    timeline_events: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """用 AquaLLM 辅助评估 evidence_strength/evidence_velocity。"""
    hyp_id = _text(hyp.get("hypothesis_id"))
    hyp_text = _text(hyp.get("hypothesis_text"))
    timeline_json = json.dumps(_records(timeline_events)[:20], ensure_ascii=False, allow_nan=False)
    prompt = build_p4b_belief_prompt(hyp_text, timeline_json)
    system_prompt = get_p4b_belief_system_prompt()
    model_name = _llm_model_name(llm)
    raw_hash = _short_hash(hyp_id + hyp_text)
    prompt_suffix = _short_hash("belief|" + prompt)

    try:
        parsed = _cache_get_json(cache, raw_hash, prompt_suffix, model_name)
        if parsed is None:
            parsed = _call_aqua_llm_json(llm, prompt, system_prompt)
            if parsed:
                _cache_set_json(cache, raw_hash, prompt_suffix, model_name, parsed)
        if not parsed or not check_compliance(json.dumps(parsed, ensure_ascii=False)):
            return None
        strength = _safe_float(parsed.get("evidence_strength"), -1.0)
        velocity = _safe_float(parsed.get("evidence_velocity"), -1.0)
        if not (0 <= strength <= 1 and 0 <= velocity <= 1):
            return None
        reason = _text(parsed.get("reason"))
        if not check_compliance(reason):
            return None
        return {
            "evidence_strength": strength,
            "evidence_velocity": velocity,
            "reason": reason,
            "llm_model": model_name,
        }
    except Exception as exc:
        logger.warning("P4B belief_state LLM 评估失败，降级到规则: %s", exc)
        return None


def update_belief_state(
    hypotheses: List[Dict[str, Any]],
    timeline: List[Dict[str, Any]],
    skepticism_scores: List[Dict[str, Any]],
    market_moves: List[Dict[str, Any]],
    use_llm: bool = True,
    llm: Any | None = None,
    cache: LLMCache | None = None,
) -> List[Dict[str, Any]]:
    """更新 belief_state（hybrid 模式）。

    posterior_probability 公式始终用规则计算（确定性）。
    LLM 仅辅助评估 evidence_strength/evidence_velocity 的归一化值。
    LLM 失败时降级到规则归一化。

    Args:
        hypotheses: 假设列表
        timeline: 时间线事件列表
        skepticism_scores: skepticism_scores 列表
        market_moves: 市场异动列表
        use_llm: 是否使用 LLM

    Returns:
        belief_state 列表
    """
    # 建立映射
    hypotheses = _records(hypotheses)
    timeline = _records(timeline)
    skepticism_scores = _records(skepticism_scores)
    market_moves = _records(market_moves)
    score_map = {
        _text(s.get("hypothesis_id")): max(
            0.0, min(1.0, _safe_float(s.get("skepticism_score"), 0.5))
        )
        for s in skepticism_scores
        if _text(s.get("hypothesis_id"))
    }
    timeline_by_hyp: Dict[str, List[Dict[str, Any]]] = {}
    for event in timeline:
        hyp_id = _text(event.get("hypothesis_id"))
        timeline_by_hyp.setdefault(hyp_id, []).append(event)

    # market_attention: 如果有市场异动数据，则 0.5，否则 0
    market_attention = 0.5 if market_moves else 0.0

    llm_enabled = use_llm and _local_llm_enabled()
    llm_client = llm
    if llm_enabled and llm_client is None:
        try:
            from core.utils.llm_client import AquaLLM
            llm_client = AquaLLM()
        except Exception as exc:
            logger.info("AquaLLM 不可用，P4B belief_state 降级到规则模式: %s", exc)
            llm_client = None
    llm_cache = cache or LLMCache(str(_DEFAULT_CACHE_DIR))

    belief_states: List[Dict[str, Any]] = []
    for idx, hyp in enumerate(hypotheses):
        hyp_id = _text(hyp.get("hypothesis_id"))
        skepticism = score_map.get(hyp_id, 0.5)
        hyp_timeline = timeline_by_hyp.get(hyp_id, [])

        # 始终用规则计算（确定性）
        belief = _compute_belief_state_by_rules(
            hyp, hyp_timeline, skepticism, market_attention
        )

        # LLM 辅助评估（可选，失败时降级）
        if llm_enabled and llm_client is not None and idx < _MAX_LLM_ITEMS:
            llm_result = _evaluate_belief_with_llm(
                llm_client, llm_cache, hyp, hyp_timeline
            )
            if llm_result:
                belief = _recompute_belief_with_llm_scores(
                    belief,
                    llm_result["evidence_strength"],
                    llm_result["evidence_velocity"],
                    llm_result["reason"],
                )
                belief["llm_model"] = llm_result["llm_model"]
            else:
                belief["llm_status"] = "rule_fallback"
        elif use_llm:
            belief["llm_status"] = "rule_fallback"

        belief_states.append(belief)

    return belief_states


def build_post_mortem_stub(
    hypotheses: List[Dict[str, Any]],
    belief_states: List[Dict[str, Any]],
    review_horizon_days: int = 20,
) -> List[Dict[str, Any]]:
    """生成复盘模板，自动填充 future_review_date。

    future_review_date 根据当前日期 + review_horizon_days 自然日近似
    （20 个交易日 ≈ 28 个自然日）。actual_outcome 仍为空，等待
    fill_post_mortem_outcomes 在到期时回填。

    Args:
        hypotheses: 假设列表
        belief_states: belief_state 列表
        review_horizon_days: 复盘展望期（交易日），默认 20。

    Returns:
        复盘模板列表
    """
    from datetime import timedelta
    hypotheses = _records(hypotheses)
    belief_states = _records(belief_states)
    state_map = {
        _text(b.get("hypothesis_id")): _text(b.get("state"), "hypothesis")
        for b in belief_states
        if _text(b.get("hypothesis_id"))
    }

    natural_days = _bounded_int(review_horizon_days, 20, maximum=3650) * 7 // 5
    future_date = (datetime.now() + timedelta(days=natural_days)).strftime("%Y-%m-%d")

    post_mortems: List[Dict[str, Any]] = []
    for hyp in hypotheses:
        hyp_id = _text(hyp.get("hypothesis_id"))
        current_state = state_map.get(hyp_id, "hypothesis")
        hyp_text = _text(hyp.get("hypothesis_text"))
        post_mortems.append({
            "hypothesis_id": hyp_id,
            "created_at": _now_iso(),
            "current_state": current_state,
            "hypothesis_text": hyp_text,
            "future_review_date": future_date,
            "expected_validation": _expected_validation_for_state(current_state),
            "actual_outcome": "",
            "review_notes": "",
            "reviewed": False,
        })

    return post_mortems


def _expected_validation_for_state(state: str) -> str:
    """根据 belief_state 给出预期验证方式。"""
    mapping = {
        "rejected_or_low_priority": "需要反转性证据（如政策转向、业绩暴雷）才能重新激活",
        "hypothesis": "需要补充公告/订单/产业链调研等实质性证据",
        "weak_signal": "需要更多来源确认和资金流向验证",
        "watchlist_candidate": "需要价格突破或成交量配合确认",
        "needs_manual_review": "需人工审核：证据充分但存在矛盾，需结合市场环境判断",
    }
    return mapping.get(state, "需要补充实质性证据")


def fill_post_mortem_outcomes(
    post_mortems: List[Dict[str, Any]],
    price_getter: Any | None = None,
    today: str = "",
) -> List[Dict[str, Any]]:
    """回填已到期复盘的 actual_outcome。

    对 future_review_date <= today 的复盘模板，尝试通过 price_getter 获取
    相关 symbol 的区间收益作为 outcome 参考。如果没有 price_getter 或
    找不到 symbol，actual_outcome 标记为 pending_review。

    Args:
        post_mortems: build_post_mortem_stub 生成的列表。
        price_getter: 价格查询对象，需实现 get_return(symbol, start, end) -> float。
        today: 今天日期字符串 YYYY-MM-DD，默认使用当前日期。

    Returns:
        更新后的 post_mortems 列表。
    """
    post_mortems = _records(post_mortems)
    today = _text(today, datetime.now().strftime("%Y-%m-%d"))

    for pm in post_mortems:
        if pm.get("reviewed"):
            continue
        review_date = _text(pm.get("future_review_date"))
        if not review_date or review_date > today:
            continue

        created = _text(pm.get("created_at"))[:10]
        if not created:
            pm["actual_outcome"] = "pending_review: no_creation_date"
            continue

        symbols = pm.get("symbols", []) or []
        symbols = [_text(symbol) for symbol in symbols if _text(symbol)] if isinstance(symbols, (list, tuple, set)) else []
        if not symbols:
            import re
            sym_match = re.findall(r"\b\d{6}\.(?:SZ|SH|BJ)\b", _text(pm.get("hypothesis_text")))
            symbols = sym_match

        if price_getter is not None and symbols:
            returns = []
            for sym in symbols[:3]:
                try:
                    ret = _safe_float(price_getter.get_return(sym, created, today), float("nan"))
                    if math.isfinite(ret):
                        returns.append((sym, ret))
                except Exception:
                    continue
            if returns:
                avg_ret = _safe_float(sum(r for _, r in returns) / len(returns))
                if avg_ret > 0.05:
                    outcome = f"validated: avg_return={avg_ret:.2%}"
                elif avg_ret < -0.05:
                    outcome = f"rejected: avg_return={avg_ret:.2%}"
                else:
                    outcome = f"inconclusive: avg_return={avg_ret:.2%}"
                pm["actual_outcome"] = outcome
                pm["review_notes"] = f"auto-reviewed symbols={[s for s, _ in returns]}"
                pm["reviewed"] = True
                pm["reviewed_at"] = _now_iso()
                continue

        pm["actual_outcome"] = "pending_review: no_price_data_or_symbols"
        pm["review_notes"] = "到期但无价格数据，需人工审核"

    return post_mortems


def run_evidence_evolution(
    input_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """运行 P4B 证据演化全流程。

    Args:
        input_dir: 输入目录
        output_dir: 输出目录
        use_llm: 是否使用 LLM

    Returns:
        dict summary
    """
    base = Path(input_dir) if input_dir else _DEFAULT_INPUT_DIR
    out = Path(output_dir) if output_dir else base

    try:
        inputs = load_inputs(base)
        hypotheses = inputs["research_hypotheses"]

        if not hypotheses:
            return {
                "module": "evidence_evolution",
                "success": False,
                "outputs": [],
                "counts": {},
                "error": "research_hypotheses_latest.json 不存在或为空",
            }

        # 1. 构建时间线
        timeline = build_evidence_timeline(
            hypotheses,
            inputs["evidence_items"],
            inputs["evidence_edges"],
            inputs["search_tasks"],
            inputs["skepticism_scores"],
        )

        # 2. 更新 belief_state
        belief_states = update_belief_state(
            hypotheses,
            timeline,
            inputs["skepticism_scores"],
            inputs["abnormal_moves"],
            use_llm=use_llm,
        )

        # 3. 生成复盘模板
        post_mortems = build_post_mortem_stub(hypotheses, belief_states)

        # 输出文件
        out.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        timeline_path = out / "evidence_timeline_latest.json"
        timeline_path.write_text(
            json.dumps(timeline, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        outputs.append(str(timeline_path))

        belief_path = out / "belief_state_latest.json"
        belief_path.write_text(
            json.dumps(belief_states, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        outputs.append(str(belief_path))

        post_mortem_path = out / "post_mortem_latest.json"
        post_mortem_path.write_text(
            json.dumps(post_mortems, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        outputs.append(str(post_mortem_path))

        return {
            "module": "evidence_evolution",
            "success": True,
            "outputs": outputs,
            "counts": {
                "evidence_timeline": len(timeline),
                "belief_state": len(belief_states),
                "post_mortem": len(post_mortems),
            },
        }
    except Exception as exc:
        logger.error("run_evidence_evolution 失败: %s", exc, exc_info=True)
        return {
            "module": "evidence_evolution",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }
