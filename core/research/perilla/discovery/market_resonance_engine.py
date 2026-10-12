"""P4B 市场共振引擎。

计算市场异动与研究假设的共振，生成 trigger_signals。

market_resonance_engine 始终用规则计算（确定性，不接 LLM）。
trigger_type 限定 4 类，next_action 限定 4 类，禁止买入/卖出。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_DEFAULT_INPUT_DIR = Path("data/reports/perilla_discovery")
_DEFAULT_OUTPUT_DIR = Path("data/reports/perilla_discovery")

# trigger_type 限定 4 类
_ALLOWED_TRIGGER_TYPES: Set[str] = {
    "evidence_upgrade", "market_resonance", "skepticism_too_high", "needs_manual_review",
}

# next_action 限定 4 类
_ALLOWED_NEXT_ACTIONS: Set[str] = {
    "keep_monitoring", "run_search_tasks", "manual_review", "downgrade",
}

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _text(value: Any, default: str = "") -> str:
    """安全读取标识和文本字段。"""
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


def _rows(value: Any) -> list[Mapping[str, Any]]:
    """只保留可消费的映射行，并支持 tuple 等轻量序列。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _short_hash(text: str) -> str:
    """计算短哈希。"""
    return hashlib.sha1(_text(text).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    """当前时间 ISO 格式。"""
    return datetime.now().isoformat()


def load_market_moves(input_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """加载市场异动数据。

    文件缺失返回空列表，不崩溃。

    Args:
        input_dir: 输入目录

    Returns:
        市场异动列表
    """
    base = Path(input_dir) if input_dir else _DEFAULT_INPUT_DIR
    # 尝试多个可能路径
    for path in [
        base / "abnormal_moves_latest.json",
        base.parent / "market" / "abnormal_moves_latest.json",
    ]:
        try:
            if not path.exists():
                continue
        except OSError as exc:
            logger.warning("检查 %s 失败: %s", path, exc)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as exc:
            logger.warning("读取 %s 失败: %s", path, exc)
            continue
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
    return []


def compute_market_resonance(
    hypotheses: List[Dict[str, Any]],
    belief_states: List[Dict[str, Any]],
    market_moves: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """计算市场共振（始终用规则计算，确定性，不接 LLM）。

    Args:
        hypotheses: 假设列表
        belief_states: belief_state 列表
        market_moves: 市场异动列表

    Returns:
        market_resonance 列表
    """
    # 建立 hypothesis_id -> belief_state 映射
    hypotheses = _rows(hypotheses)
    belief_states = _rows(belief_states)
    market_moves = _rows(market_moves)
    belief_map = {
        _text(b.get("hypothesis_id")): b
        for b in belief_states
        if _text(b.get("hypothesis_id"))
    }

    resonances: List[Dict[str, Any]] = []
    for hyp in hypotheses:
        hyp_id = _text(hyp.get("hypothesis_id"))
        source_nodes = hyp.get("source_nodes", [])
        source_nodes = [
            _text(node) for node in source_nodes
            if _text(node)
        ] if isinstance(source_nodes, (list, tuple)) else []
        belief = belief_map.get(hyp_id, {})

        # 匹配市场异动项
        matched_items: List[Dict[str, Any]] = []
        for move in market_moves:
            move_text = " ".join([
                _text(move.get("name")),
                _text(move.get("concept")),
                _text(move.get("reason")),
            ])
            # 检查是否与假设的 source_nodes 匹配
            if any(node in move_text for node in source_nodes):
                matched_items.append(move)

        # market_attention_score: 基于匹配数归一化
        market_attention_score = round(min(1.0, len(matched_items) / 3.0), 4)

        # resonance_reason
        if matched_items:
            reason = f"假设涉及的概念在市场异动中出现 {len(matched_items)} 次"
        else:
            reason = "无市场异动匹配"

        resonances.append({
            "hypothesis_id": hyp_id,
            "related_terms": source_nodes,
            "matched_market_items": [
                {"name": m.get("name", ""), "concept": m.get("concept", "")}
                for m in matched_items[:5]
            ],
            "market_attention_score": market_attention_score,
            "resonance_reason": reason,
            "status": "pending",
        })

    return resonances


def generate_trigger_signals(
    hypotheses: List[Dict[str, Any]],
    belief_states: List[Dict[str, Any]],
    market_resonances: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """生成触发信号。

    trigger_type 限定 4 类：evidence_upgrade / market_resonance /
    skepticism_too_high / needs_manual_review

    next_action 限定 4 类：keep_monitoring / run_search_tasks /
    manual_review / downgrade

    禁止输出买入/卖出。

    Args:
        hypotheses: 假设列表
        belief_states: belief_state 列表
        market_resonances: 市场共振列表

    Returns:
        触发信号列表
    """
    # 建立映射
    hypotheses = _rows(hypotheses)
    belief_states = _rows(belief_states)
    market_resonances = _rows(market_resonances)
    belief_map = {
        _text(b.get("hypothesis_id")): b
        for b in belief_states
        if _text(b.get("hypothesis_id"))
    }

    resonance_map = {
        _text(r.get("hypothesis_id")): r
        for r in market_resonances
        if _text(r.get("hypothesis_id"))
    }

    signals: List[Dict[str, Any]] = []
    for hyp in hypotheses:
        hyp_id = _text(hyp.get("hypothesis_id"))
        belief = belief_map.get(hyp_id, {})
        resonance = resonance_map.get(hyp_id, {})

        posterior = max(0.0, min(1.0, _safe_float(belief.get("posterior_probability"))))
        skepticism = max(0.0, min(1.0, _safe_float(belief.get("skepticism_score"))))
        state = _text(belief.get("state"), "hypothesis")
        market_attention = max(0.0, min(1.0, _safe_float(resonance.get("market_attention_score"))))

        # 规则生成触发信号
        trigger_type = ""
        next_action = ""
        trigger_text = ""
        evidence_reason = ""
        market_reason = ""

        if state == "needs_manual_review":
            trigger_type = "needs_manual_review"
            next_action = "manual_review"
            trigger_text = f"假设 {hyp_id} 后验概率 {posterior:.4f} 过高，需人工审查"
            evidence_reason = f"posterior_probability={posterior:.4f}"
            market_reason = ""
        elif skepticism > 0.7:
            trigger_type = "skepticism_too_high"
            next_action = "downgrade"
            trigger_text = f"假设 {hyp_id} skepticism_score {skepticism:.4f} 过高，建议降级"
            evidence_reason = f"skepticism_score={skepticism:.4f}"
            market_reason = ""
        elif market_attention > 0.5 and posterior > 0.4:
            trigger_type = "market_resonance"
            next_action = "run_search_tasks"
            trigger_text = f"假设 {hyp_id} 与市场异动共振，market_attention={market_attention:.4f}"
            evidence_reason = f"posterior_probability={posterior:.4f}"
            market_reason = f"market_attention_score={market_attention:.4f}"
        elif posterior > 0.5:
            trigger_type = "evidence_upgrade"
            next_action = "keep_monitoring"
            trigger_text = f"假设 {hyp_id} 证据增强，posterior={posterior:.4f}"
            evidence_reason = f"posterior_probability={posterior:.4f}"
            market_reason = ""
        else:
            # 默认：持续监测
            trigger_type = "evidence_upgrade"
            next_action = "keep_monitoring"
            trigger_text = f"假设 {hyp_id} 持续监测中"
            evidence_reason = f"posterior_probability={posterior:.4f}"
            market_reason = ""

        # 强制限定枚举
        if trigger_type not in _ALLOWED_TRIGGER_TYPES:
            trigger_type = "needs_manual_review"
        if next_action not in _ALLOWED_NEXT_ACTIONS:
            next_action = "keep_monitoring"

        signals.append({
            "trigger_id": f"trigger_{_short_hash(hyp_id + trigger_type)}",
            "hypothesis_id": hyp_id,
            "trigger_type": trigger_type,
            "trigger_text": trigger_text,
            "evidence_reason": evidence_reason,
            "market_reason": market_reason,
            "next_action": next_action,
            "status": "pending",
        })

    return signals


def run_market_resonance(
    input_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """运行市场共振引擎全流程。

    Args:
        input_dir: 输入目录
        output_dir: 输出目录

    Returns:
        dict summary
    """
    base = Path(input_dir) if input_dir else _DEFAULT_INPUT_DIR
    out = Path(output_dir) if output_dir else base

    try:
        # 加载假设和 belief_state
        hyp_path = base / "research_hypotheses_latest.json"
        belief_path = base / "belief_state_latest.json"

        if not hyp_path.exists():
            return {
                "module": "market_resonance_engine",
                "success": False,
                "outputs": [],
                "counts": {},
                "error": "research_hypotheses_latest.json 不存在",
            }

        hypotheses = json.loads(hyp_path.read_text(encoding="utf-8"))
        belief_states = []
        if belief_path.exists():
            belief_states = json.loads(belief_path.read_text(encoding="utf-8"))

        # 加载市场异动
        market_moves = load_market_moves(base)

        # 1. 计算市场共振
        resonances = compute_market_resonance(hypotheses, belief_states, market_moves)

        # 2. 生成触发信号
        signals = generate_trigger_signals(hypotheses, belief_states, resonances)

        # 输出文件
        out.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        resonance_path = out / "market_resonance_latest.json"
        resonance_path.write_text(
            json.dumps(resonances, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(resonance_path))

        signals_path = out / "trigger_signals_latest.json"
        signals_path.write_text(
            json.dumps(signals, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(signals_path))

        return {
            "module": "market_resonance_engine",
            "success": True,
            "outputs": outputs,
            "counts": {
                "market_resonance": len(resonances),
                "trigger_signals": len(signals),
            },
        }
    except Exception as exc:
        logger.error("run_market_resonance 失败: %s", exc, exc_info=True)
        return {
            "module": "market_resonance_engine",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }
