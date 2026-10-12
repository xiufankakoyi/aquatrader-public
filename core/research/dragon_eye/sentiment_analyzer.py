"""
DragonEye 情绪分析器
提炼并扩展 quant/combined.py 中的情绪判断逻辑，使 DragonEye 模块不依赖外部脚本即可输出
"周期阶段 / 风险等级 / 接力情绪 / 综合分数"四类指标。

设计原则：
- 纯函数式：所有公开函数接受 dict/JSON 数据，返回标准结构，便于单元测试
- 防御性：输入字段缺失时给出降级值（不抛异常，不让前端页面崩溃）
- 单一职责：只做情绪打分，不做 IO，不调用数据库
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple


# ============================================================================
# 情绪周期阶段常量
# ============================================================================
PHASE_ICE = "冰点期"
PHASE_PEAK = "高潮期"
PHASE_FERMENT = "发酵期"
PHASE_FADE = "退潮期"
PHASE_RECOVER = "复苏期"
PHASE_OSCILLATE = "震荡期"

VALID_PHASES = {
    PHASE_ICE,
    PHASE_PEAK,
    PHASE_FERMENT,
    PHASE_FADE,
    PHASE_RECOVER,
    PHASE_OSCILLATE,
}


# ============================================================================
# 风险等级常量
# ============================================================================
RISK_HIGH = "高风险"
RISK_MEDIUM = "中等风险"
RISK_LOW = "低风险"
RISK_SAFE = "风险可控"

VALID_RISKS = {RISK_HIGH, RISK_MEDIUM, RISK_LOW, RISK_SAFE}


# ============================================================================
# 综合情绪分数档位
# ============================================================================
SCORE_PANIC_THRESHOLD = 20.0   # < 20 恐慌
SCORE_PESSIMIST_THRESHOLD = 40.0  # < 40 偏空
SCORE_NEUTRAL_HIGH = 60.0      # < 60 偏多
SCORE_BULLISH_THRESHOLD = 80.0  # < 80 强势


@dataclass
class CyclePhaseResult:
    """周期阶段判断结果"""
    phase: str
    reasons: List[str]
    score: float  # 0-100，数值越高情绪越热


@dataclass
class SentimentScore:
    """综合情绪打分结果"""
    cycle_phase: str
    cycle_reasons: List[str]
    risk_level: str
    promotion_1to2: float
    promotion_2to3: float
    promotion_high: float
    theme_continuity: float
    theme_flow: str  # 题材轮动方向：流入/流出/稳定/无主线
    total_score: float
    summary: str  # 简短文字总结（"强势"/"偏暖"/"震荡"/"偏冷"/"恐慌"）


# ============================================================================
# 工具函数
# ============================================================================
def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全转 float，None/字符串/异常都走默认值"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _safe_ratio(value: Any, default: float = 0.0) -> float:
    """情绪 brokenRatio 可能以小数（0.18）或百分数（18.0）出现，统一归一到 0-1"""
    val = _safe_float(value, default)
    # 若超过 1.0，认为是百分数形式
    if val > 1.0:
        val /= 100.0
    return max(0.0, min(1.0, val))


def _extract_ladder_max_height(ladder: Optional[Dict[str, Any]]) -> int:
    """从 ladder（key=连板层级, value=股票列表）提取最高连板高度"""
    if not ladder:
        return 0
    try:
        return max((int(k) for k in ladder.keys()), default=0)
    except (TypeError, ValueError):
        return 0


def _theme_continuity(cur_themes: List[Dict], prev_themes: List[Dict]) -> float:
    """计算主线题材的连续性分数 (0-1)。
    思路：今日 top-2 题材与昨日 top-2 题材的交集比例。
    """
    if not cur_themes or not prev_themes:
        return 0.0
    cur_set = {
        t.get("name") for t in _as_records(cur_themes)[:2] if t.get("name")
    }
    prev_set = {
        t.get("name") for t in _as_records(prev_themes)[:2] if t.get("name")
    }
    if not cur_set or not prev_set:
        return 0.0
    overlap = len(cur_set & prev_set)
    return overlap / max(len(cur_set), 1)


# ============================================================================
# 题材轮动方向常量
# ============================================================================
THEME_FLOW_INFLOW = "流入"     # 新题材进入 top-N
THEME_FLOW_OUTFLOW = "流出"    # 旧题材跌出 top-N
THEME_FLOW_STABLE = "稳定"     # 主流题材不变
THEME_FLOW_NONE = "无主线"     # 没有明确主线

VALID_THEME_FLOWS = {THEME_FLOW_INFLOW, THEME_FLOW_OUTFLOW, THEME_FLOW_STABLE, THEME_FLOW_NONE}


def judge_theme_flow(
    cur_themes: Optional[List[Dict]],
    prev_themes: Optional[List[Dict]],
) -> str:
    """
    判断题材轮动方向（流入/流出/稳定/无主线）。

    判断逻辑：
    1. 若无 cur_themes 或主题数 < 2 -> 无主线
    2. 若有 prev_themes 且交集 >= 1 -> 稳定
    3. 若 cur_themes 中出现 prev 中没有的新题材（>=1）-> 流入
    4. 若 cur_themes 全部都不在 prev 中 -> 流出（旧主线全退）

    Args:
        cur_themes: 当日主题列表（含 name 字段）
        prev_themes: 前一交易日主题列表

    Returns:
        str: THEME_FLOW_INFLOW / THEME_FLOW_OUTFLOW / THEME_FLOW_STABLE / THEME_FLOW_NONE
    """
    cur_list = _as_records(cur_themes)
    prev_list = _as_records(prev_themes)

    cur_names = {t.get("name") for t in cur_list[:3] if t.get("name")}
    prev_names = {t.get("name") for t in prev_list[:3] if t.get("name")}

    if not cur_names:
        return THEME_FLOW_NONE
    if not prev_names:
        # 没有前一天数据：视为"无主线"（无法判断流入/流出）
        return THEME_FLOW_NONE

    overlap = cur_names & prev_names
    if overlap:
        return THEME_FLOW_STABLE
    # 完全没有交集：旧主线全部退出
    return THEME_FLOW_OUTFLOW


# ============================================================================
# 核心判定函数
# ============================================================================
def judge_cycle_phase(
    cur: Dict[str, Any],
    prev: Optional[Dict[str, Any]],
    prev2: Optional[Dict[str, Any]] = None,
) -> CyclePhaseResult:
    """
    判断当前市场情绪所处的周期阶段。

    Args:
        cur: 当日 sentiment 数据（含 marketSentiment / emotionMetrics / ladder / themes）
        prev: 前一交易日 sentiment 数据（可为空）
        prev2: 前两个交易日数据（用于发酵期判断，可选）

    Returns:
        CyclePhaseResult：阶段名称、判定理由列表、阶段分数
    """
    # 字段降级
    cur = _as_mapping(cur)
    prev = _as_mapping(prev) if prev is not None else None
    prev2 = _as_mapping(prev2) if prev2 is not None else None
    market = _as_mapping(cur.get("marketSentiment"))
    emotion = _as_mapping(cur.get("emotionMetrics"))
    rise = _safe_float(market.get("rise"))
    fall = _safe_float(market.get("fall"))
    broken_ratio = _safe_ratio(emotion.get("brokenRatio"))
    limit_down = _safe_float(emotion.get("limitDownCount"))
    max_height = _extract_ladder_max_height(cur.get("ladder"))

    # 冰点期：普跌 + 高炸板 + 跌停爆量
    if fall > 3500 and broken_ratio > 0.40 and limit_down > 50:
        return CyclePhaseResult(
            phase=PHASE_ICE,
            reasons=["下跌家数超3500", "炸板率超40%", "跌停数超50只"],
            score=10.0,
        )

    # 高潮期：普涨 + 低炸板
    if rise > 4000 and broken_ratio < 0.20:
        return CyclePhaseResult(
            phase=PHASE_PEAK,
            reasons=["上涨家数超4000", "炸板率低于20%", "情绪高涨"],
            score=90.0,
        )

    # 发酵期：连板高度突破 + 主线持续
    if max_height >= 5 and _is_theme_sustained(cur, prev, prev2):
        return CyclePhaseResult(
            phase=PHASE_FERMENT,
            reasons=[f"连板高度突破{max_height}板", "主线题材持续", "赚钱效应扩散"],
            score=75.0,
        )

    # 退潮期：高炸板 + 跌停增加
    if broken_ratio > 0.30 and limit_down > 20:
        return CyclePhaseResult(
            phase=PHASE_FADE,
            reasons=["炸板率超30%", "跌停数增加", "接力情绪降温"],
            score=25.0,
        )

    # 复苏期：上涨家数增加 + 炸板率下降（与前日比较）
    if prev:
        prev_market = _as_mapping(prev.get("marketSentiment"))
        prev_emotion = _as_mapping(prev.get("emotionMetrics"))
        prev_rise = _safe_float(prev_market.get("rise"))
        prev_broken = _safe_ratio(prev_emotion.get("brokenRatio"))
        if rise > prev_rise and broken_ratio < prev_broken:
            return CyclePhaseResult(
                phase=PHASE_RECOVER,
                reasons=["上涨家数增加", "炸板率下降", "情绪开始回暖"],
                score=55.0,
            )

    # 默认：震荡期
    return CyclePhaseResult(
        phase=PHASE_OSCILLATE,
        reasons=["多空分歧", "方向不明", "等待信号"],
        score=45.0,
    )


def _is_theme_sustained(
    cur: Dict[str, Any],
    prev: Optional[Dict[str, Any]],
    prev2: Optional[Dict[str, Any]] = None,
) -> bool:
    """判断主题是否具有连续性：与昨日或前日 top-2 题材有交集"""
    cur = _as_mapping(cur)
    cur_themes = {
        t.get("name") for t in _as_records(cur.get("themes"))[:2] if t.get("name")
    }
    if not cur_themes or not prev:
        return False
    prev_themes = {
        t.get("name") for t in _as_records(prev.get("themes"))[:2] if t.get("name")
    }
    if cur_themes & prev_themes:
        return True
    if prev2:
        prev2_themes = {
            t.get("name")
            for t in _as_records(prev2.get("themes"))[:2]
            if t.get("name")
        }
        if cur_themes & prev2_themes:
            return True
    return False


def judge_risk_level(limit_down_count: Any, broken_count: Any = 0) -> str:
    """
    根据跌停数 + 断板数判定风险等级。

    Args:
        limit_down_count: 跌停家数
        broken_count: 断板家数（次要指标）

    Returns:
        str: RISK_HIGH / RISK_MEDIUM / RISK_LOW / RISK_SAFE
    """
    down = _safe_float(limit_down_count)
    broken = _safe_float(broken_count)

    # 跌停数主导判定
    if down > 20 or (down > 10 and broken > 30):
        return RISK_HIGH
    if down > 10 or (down > 5 and broken > 20):
        return RISK_MEDIUM
    if down > 5 or broken > 10:
        return RISK_LOW
    return RISK_SAFE


def calculate_total_score(
    cycle_score: float,
    rise_count: float,
    fall_count: float,
    limit_up_count: float,
    limit_down_count: float,
    promotion_avg: float,
) -> Tuple[float, str]:
    """
    计算综合情绪分（0-100）及文字总结。

    加权设计：
    - 周期阶段分（30%）
    - 涨跌家数比（25%）：(rise - fall) / (rise + fall) * 100
    - 涨停/跌停比（20%）
    - 接力情绪（25%）：1进2、2进3、高标的均值

    Args:
        cycle_score: 周期阶段分数 0-100
        rise_count: 上涨家数
        fall_count: 下跌家数
        limit_up_count: 涨停家数
        limit_down_count: 跌停家数
        promotion_avg: 接力情绪平均（百分制 0-100）

    Returns:
        (total_score, summary)
    """
    cycle_score = max(0.0, min(100.0, _safe_float(cycle_score)))
    rise_count = max(0.0, _safe_float(rise_count))
    fall_count = max(0.0, _safe_float(fall_count))
    limit_up_count = max(0.0, _safe_float(limit_up_count))
    limit_down_count = max(0.0, _safe_float(limit_down_count))
    promotion_avg = _safe_float(promotion_avg)
    total_market = rise_count + fall_count
    if total_market <= 0:
        rise_ratio_score = 50.0
    else:
        rise_ratio_score = max(0.0, min(100.0, (rise_count - fall_count) / total_market * 100 + 50))

    total_limits = limit_up_count + limit_down_count
    if total_limits <= 0:
        limit_ratio_score = 50.0
    else:
        limit_ratio_score = max(0.0, min(100.0, limit_up_count / total_limits * 100))

    promo_score = max(0.0, min(100.0, promotion_avg))

    total = (
        cycle_score * 0.30
        + rise_ratio_score * 0.25
        + limit_ratio_score * 0.20
        + promo_score * 0.25
    )
    total = round(total, 2)

    if total >= SCORE_BULLISH_THRESHOLD:
        summary = "强势"
    elif total >= SCORE_NEUTRAL_HIGH:
        summary = "偏暖"
    elif total >= SCORE_PESSIMIST_THRESHOLD:
        summary = "震荡"
    elif total >= SCORE_PANIC_THRESHOLD:
        summary = "偏冷"
    else:
        summary = "恐慌"
    return total, summary


def analyze_sentiment(
    cur: Dict[str, Any],
    prev: Optional[Dict[str, Any]] = None,
    prev2: Optional[Dict[str, Any]] = None,
) -> SentimentScore:
    """
    一站式情绪分析入口。

    Args:
        cur: 当日 sentiment 原始数据
        prev: 前一交易日数据（可为空）
        prev2: 前两交易日数据（可为空）

    Returns:
        SentimentScore：完整情绪打分结果
    """
    cur = _as_mapping(cur)
    prev = _as_mapping(prev) if prev is not None else None
    prev2 = _as_mapping(prev2) if prev2 is not None else None
    market = _as_mapping(cur.get("marketSentiment"))
    emotion = _as_mapping(cur.get("emotionMetrics"))

    rise = _safe_float(market.get("rise"))
    fall = _safe_float(market.get("fall"))
    limit_up = _safe_float(emotion.get("limitUpCount"))
    limit_down = _safe_float(emotion.get("limitDownCount"))
    broken = _safe_float(emotion.get("brokenCount"))

    rates = _as_mapping(emotion.get("promotionRates"))
    p_1to2 = _safe_float(rates.get("1to2"))
    p_2to3 = _safe_float(rates.get("2to3"))
    p_high = _safe_float(rates.get("high"))
    promotion_avg = (p_1to2 + p_2to3 + p_high) / 3.0

    # 周期阶段
    phase = judge_cycle_phase(cur, prev, prev2)

    # 风险等级
    risk = judge_risk_level(limit_down, broken)

    # 主线连续性
    cur_themes = _as_records(cur.get("themes"))
    prev_themes = _as_records(prev.get("themes")) if prev else []
    continuity = _theme_continuity(cur_themes, prev_themes)

    # 题材轮动方向
    theme_flow = judge_theme_flow(cur_themes, prev_themes)

    # 综合分
    total, summary = calculate_total_score(
        cycle_score=phase.score,
        rise_count=rise,
        fall_count=fall,
        limit_up_count=limit_up,
        limit_down_count=limit_down,
        promotion_avg=promotion_avg,
    )

    return SentimentScore(
        cycle_phase=phase.phase,
        cycle_reasons=phase.reasons,
        risk_level=risk,
        promotion_1to2=p_1to2,
        promotion_2to3=p_2to3,
        promotion_high=p_high,
        theme_continuity=round(continuity, 4),
        theme_flow=theme_flow,
        total_score=total,
        summary=summary,
    )


def score_to_dict(score: SentimentScore) -> Dict[str, Any]:
    """SentimentScore -> dict，方便 JSON 序列化"""
    return asdict(score)
