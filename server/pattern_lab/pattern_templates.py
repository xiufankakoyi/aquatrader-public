"""Rule templates and Flask endpoints for PatternRadar.

These templates are research filters only. They describe observable event
structures and deliberately avoid trading actions or position advice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List

from flask import Blueprint, jsonify


@dataclass(frozen=True)
class PatternTemplate:
    pattern_id: str
    pattern_name: str
    description: str
    default_params: Dict[str, Any]
    required_events: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


TEMPLATES: Dict[str, PatternTemplate] = {
    "strong_break_reversal": PatternTemplate(
        pattern_id="strong_break_reversal",
        pattern_name="强势断板反包",
        description="前期强攻击后出现分歧或断板，随后出现反包修复的事件结构。",
        default_params={
            "lookback_days": 5,
            "min_strong_attack_days": 2,
            "min_cumulative_gain_pct": 20.0,
            "divergence_window_days": 3,
            "max_post_divergence_drop_pct": -8.0,
            "min_counterattack_pct": 3.0,
            "success_gain_5d_pct": 5.0,
        },
        required_events=[
            "strong_attack_day",
            "first_divergence_day",
            "break_board_day",
            "counterattack_day",
        ],
    ),
    "trend_ma5_pullback": PatternTemplate(
        pattern_id="trend_ma5_pullback",
        pattern_name="趋势主升浪回踩 MA5",
        description="20 日上升趋势中，多次站上 MA5，缩量回踩 MA5 附近后出现修复阳线。",
        default_params={
            "trend_window_days": 20,
            "min_trend_gain_pct": 8.0,
            "min_above_ma5_days": 12,
            "pullback_window_days": 3,
            "ma5_near_pct": 2.0,
            "max_ma5_break_pct": -2.0,
            "min_repair_pct": 1.5,
            "success_gain_5d_pct": 4.0,
        },
        required_events=["above_ma5", "volume_shrink", "close_near_high"],
    ),
    "volume_reversal_repair": PatternTemplate(
        pattern_id="volume_reversal_repair",
        pattern_name="爆量冲高回落后修复",
        description="爆量大振幅冲高回落后，1-3 日内出现修复阳线的事件结构。",
        default_params={
            "spike_window_days": 3,
            "min_amplitude_pct": 7.0,
            "min_repair_pct": 2.0,
            "success_gain_5d_pct": 4.0,
        },
        required_events=[
            "volume_burst",
            "high_open_low_close",
            "long_upper_shadow",
            "counterattack_day",
        ],
    ),
}


def get_pattern_templates() -> List[Dict[str, Any]]:
    return [template.to_dict() for template in TEMPLATES.values()]


def get_pattern_template(pattern_id: str) -> PatternTemplate:
    try:
        return TEMPLATES[pattern_id]
    except KeyError as exc:
        raise ValueError(f"unknown pattern_id: {pattern_id}") from exc


def merge_params(pattern_id: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    template = get_pattern_template(pattern_id)
    merged = dict(template.default_params)
    if params:
        for key, value in params.items():
            if value is not None:
                merged[key] = value
    return merged


pattern_templates_bp = Blueprint("pattern_templates", __name__, url_prefix="/api/pattern-templates")


@pattern_templates_bp.route("/", methods=["GET"])
def list_templates():
    return jsonify({"templates": get_pattern_templates()})


@pattern_templates_bp.route("/<template_id>", methods=["GET"])
def get_template(template_id: str):
    try:
        return jsonify(get_pattern_template(template_id).to_dict())
    except ValueError:
        return jsonify({"error": "unknown pattern_id", "pattern_id": template_id}), 404
