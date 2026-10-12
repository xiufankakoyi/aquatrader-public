"""盘中动态选股器。

按 5 条规则从全 A 股（不含北交所）中各选 top1，去重后返回候选列表：
1. 3 分钟涨速最大
2. 15 分钟涨幅最大
3. 板块涨幅最大且板块内涨幅第一
4. 5 分钟量比放大到 3 倍以上且换手率急剧攀升
5. 板块跌幅最大且板块内跌幅第一（排除跌幅 <= -20% 及 ST）

实现策略：
- 先用 AKShare/Efinance 全 A 快照做全市场初筛（字段最全）。
- 对需要精确分钟级指标的规则 1、4，用 TDX 分时线重算后排序。
- 板块规则调用 ProviderRegistry 的概念板块接口。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from infrastructure.data_providers.provider_registry import ProviderRegistry

logger = logging.getLogger(__name__)

RULE_SPEED_3M = "3分钟涨速最大"
RULE_GAIN_15M = "15分钟涨幅最大"
RULE_SECTOR_GAIN_LEADER = "领涨板块+领涨股"
RULE_VOLUME_SURGE = "量比突增+换手攀升"
RULE_SECTOR_DROP_LAGGARD = "领跌板块+领跌股"

# 过滤阈值
_DROP_FILTER_PCT = -20.0
_VOLUME_RATIO_THRESHOLD = 3.0
_MIN_TURNOVER_RATE = 1.0


@dataclass
class IntradayPick:
    """选股结果项。

    Attributes:
        symbol: 股票代码（6 位纯数字）。
        stock_name: 股票简称。
        rule: 命中规则描述。
        price: 最新价。
        pct_chg: 涨跌幅（%）。
        extra: 规则相关附加字段。
    """

    symbol: str
    stock_name: str
    rule: str
    price: float = 0.0
    pct_chg: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def _is_a_share(code: str) -> bool:
    """判断是否为沪深 A 股（不含北交所）。"""
    return bool(re.fullmatch(r"(60\d{4}|68\d{4}|00\d{4}|30\d{4})", str(code)))


def _is_st(name: str) -> bool:
    """判断名称是否包含 ST 标识。"""
    return "ST" in str(name).upper()


def _base_filter(df: pd.DataFrame) -> pd.DataFrame:
    """基础过滤：正常交易、沪深 A 股、非 ST。"""
    if df.empty:
        return df
    required = {"symbol", "stock_name", "price", "pre_close"}
    if not required.issubset(df.columns):
        return df.iloc[0:0]
    mask = (
        df["price"].notna() & (df["price"] > 0)
        & df["pre_close"].notna() & (df["pre_close"] > 0)
        & df["symbol"].apply(_is_a_share)
        & ~df["stock_name"].apply(_is_st)
    )
    return df.loc[mask].copy()


def _to_pick(row: pd.Series, rule: str, extra: dict[str, Any] | None = None) -> IntradayPick:
    """将 DataFrame 行转换为 IntradayPick。"""
    return IntradayPick(
        symbol=str(row.get("symbol", "")),
        stock_name=str(row.get("stock_name", "")),
        rule=rule,
        price=float(row.get("price", 0) or 0),
        pct_chg=float(row.get("pct_chg", 0) or 0),
        extra=extra or {},
    )


def _spot_symbol_map(spot: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """将快照 DataFrame 转成按 symbol 索引的字典，用于补充板块成分股缺少的价格。"""
    if spot.empty or "symbol" not in spot.columns:
        return {}
    return {str(row["symbol"]): row.to_dict() for _, row in spot.iterrows()}


def _compute_3m_speed(data: list[dict[str, Any]]) -> float | None:
    """根据 TDX 1 分钟线计算最近 3 分钟涨速（%）。

    Args:
        data: TDX 返回的分钟数据，每项至少包含 price。

    Returns:
        最近 3 分钟涨跌幅（%）；数据不足时返回 None。
    """
    if len(data) < 4:
        return None
    prices = [float(d.get("price", 0)) for d in data if float(d.get("price", 0)) > 0]
    if len(prices) < 4:
        return None
    ref = prices[-4]
    cur = prices[-1]
    if ref <= 0:
        return None
    return round((cur - ref) / ref * 100, 3)


def _refine_speed_with_tdx(df: pd.DataFrame, tdx: Any) -> pd.DataFrame:
    """用 TDX 精确 3 分钟涨速重排候选。"""
    scores = []
    fallback_col = "speed" if "speed" in df.columns and df["speed"].notna().any() else "change_5min"
    for _, row in df.iterrows():
        series = tdx.get_intraday_series(row["symbol"])
        data = series.get("data", []) if isinstance(series, dict) else []
        exact = _compute_3m_speed(data)
        scores.append(exact if exact is not None else float(row.get(fallback_col, 0) or 0))
    result = df.copy()
    result["speed_exact"] = scores
    return result.sort_values("speed_exact", ascending=False)


def _pick_speed_3m(df: pd.DataFrame, tdx: Any | None = None, top_n: int = 1) -> list[IntradayPick]:
    """规则 1：3 分钟涨速最大。"""
    df = _base_filter(df)
    if df.empty:
        return []
    fallback_col = "speed" if "speed" in df.columns and df["speed"].notna().any() else "change_5min"
    if df[fallback_col].isna().all():
        return []
    candidates = df.dropna(subset=[fallback_col]).sort_values(fallback_col, ascending=False).head(max(top_n, 20))
    if tdx is not None and getattr(tdx, "is_available", lambda: False)():
        candidates = _refine_speed_with_tdx(candidates, tdx)
    picks = []
    for _, row in candidates.head(top_n).iterrows():
        extra = {"speed": row.get(fallback_col)}
        if "speed_exact" in row:
            extra["speed_exact"] = row["speed_exact"]
        picks.append(_to_pick(row, RULE_SPEED_3M, extra))
    return picks


def _pick_gain_15m(df: pd.DataFrame, top_n: int = 1) -> list[IntradayPick]:
    """规则 2：15 分钟涨幅最大。"""
    df = _base_filter(df)
    col = "change_15min"
    if col not in df.columns or df[col].isna().all():
        return []
    picks = []
    for _, row in df.dropna(subset=[col]).sort_values(col, ascending=False).head(top_n).iterrows():
        picks.append(_to_pick(row, RULE_GAIN_15M, {"change_15min": row[col]}))
    return picks


def _pick_sector_leader(
    registry: ProviderRegistry,
    spot: pd.DataFrame,
    top_n: int,
    ascending: bool,
    rule: str,
) -> list[IntradayPick]:
    """板块领涨/领跌股通用逻辑。"""
    boards = registry.get_concept_boards()
    if boards.empty or "change_pct" not in boards.columns:
        return []
    boards = boards.dropna(subset=["change_pct"]).sort_values("change_pct", ascending=ascending)
    spot_map = _spot_symbol_map(spot)
    picks: list[IntradayPick] = []
    for _, board in boards.iterrows():
        board_name = str(board.get("board_name", ""))
        if not board_name:
            continue
        try:
            members = registry.get_concept_board_members(board_name, board_name=board_name)
        except Exception as exc:
            logger.warning("板块 %s 成分股获取失败: %s", board_name, exc)
            continue
        if members.empty:
            continue
        members = members[members["symbol"].apply(_is_a_share) & ~members["stock_name"].apply(_is_st)]
        members = members[members["change_pct"].notna()]
        if ascending:
            members = members[members["change_pct"] >= _DROP_FILTER_PCT]
        else:
            members = members[members["change_pct"] > _DROP_FILTER_PCT]
        if members.empty:
            continue
        members = members.sort_values("change_pct", ascending=ascending).head(top_n)
        for _, row in members.iterrows():
            enriched = spot_map.get(str(row["symbol"]), {})
            picks.append(
                IntradayPick(
                    symbol=str(row["symbol"]),
                    stock_name=str(row.get("stock_name") or enriched.get("stock_name", "")),
                    rule=rule,
                    price=float(enriched.get("price", 0) or 0),
                    pct_chg=float(row["change_pct"]),
                    extra={
                        "board_name": board_name,
                        "board_change_pct": float(board["change_pct"]),
                    },
                )
            )
        if len(picks) >= top_n:
            break
    return picks[:top_n]


def _pick_sector_gain_leader(
    registry: ProviderRegistry, spot: pd.DataFrame, top_n: int = 1
) -> list[IntradayPick]:
    """规则 3：板块涨幅最大且板块内涨幅第一。"""
    return _pick_sector_leader(registry, spot, top_n, ascending=False, rule=RULE_SECTOR_GAIN_LEADER)


def _pick_sector_drop_laggard(
    registry: ProviderRegistry, spot: pd.DataFrame, top_n: int = 1
) -> list[IntradayPick]:
    """规则 5：板块跌幅最大且板块内跌幅第一（排除 <= -20% 及 ST）。"""
    return _pick_sector_leader(registry, spot, top_n, ascending=True, rule=RULE_SECTOR_DROP_LAGGARD)


def _compute_volume_surge_metrics(
    data: list[dict[str, Any]], float_market_cap: float
) -> tuple[float, float, float]:
    """根据 TDX 分钟线计算 5 分钟量比和换手率。

    Args:
        data: TDX 1 分钟数据。
        float_market_cap: 流通市值（元）。

    Returns:
        (exact_volume_ratio, turnover_5min, turnover_avg_window)
    """
    if len(data) < 10:
        return 0.0, 0.0, 0.0
    vols = [float(d.get("volume", d.get("vol", 0)) or 0) for d in data]
    prices = [float(d.get("price", 0) or 0) for d in data]
    recent5_vol = sum(vols[-5:])
    windows = [sum(vols[i : i + 5]) for i in range(0, max(1, len(vols) - 5), 5)]
    avg_vol = sum(windows) / len(windows) if windows else 0.0
    exact_ratio = round(recent5_vol / avg_vol, 2) if avg_vol > 0 else 0.0

    def window_amount(start: int) -> float:
        return sum(v * p for v, p in zip(vols[start : start + 5], prices[start : start + 5]))

    recent5_amount = window_amount(len(vols) - 5)
    avg_amount = sum(window_amount(i) for i in range(0, max(1, len(vols) - 5), 5)) / len(windows) if windows else 0.0
    turnover_5m = round(recent5_amount / float_market_cap * 100, 3) if float_market_cap > 0 else 0.0
    turnover_avg = round(avg_amount / float_market_cap * 100, 3) if float_market_cap > 0 else 0.0
    return exact_ratio, turnover_5m, turnover_avg


def _pick_volume_surge(
    df: pd.DataFrame, tdx: Any | None = None, top_n: int = 1
) -> list[IntradayPick]:
    """规则 4：量比 >= 3 且换手率显著放大。

    先用快照的 量比 + 换手率做初筛；若 TDX 可用，再对候选重算精确的 5 分钟量比和换手率。
    """
    df = _base_filter(df)
    if df.empty:
        return []
    vr_col = "volume_ratio"
    tr_col = "turnover_rate"
    if vr_col not in df.columns or tr_col not in df.columns:
        return []
    candidates = df[
        df[vr_col].notna()
        & (df[vr_col] >= _VOLUME_RATIO_THRESHOLD)
        & df[tr_col].notna()
        & (df[tr_col] >= _MIN_TURNOVER_RATE)
    ].copy()
    if candidates.empty:
        return []

    if tdx is not None and getattr(tdx, "is_available", lambda: False)():
        top_candidates = candidates.sort_values(tr_col, ascending=False).head(50)
        exact_ratios = []
        turnover_5min_list = []
        turnover_avg_list = []
        for _, row in top_candidates.iterrows():
            series = tdx.get_intraday_series(row["symbol"])
            data = series.get("data", []) if isinstance(series, dict) else []
            ratio, t5, t_avg = _compute_volume_surge_metrics(data, float(row.get("float_market_cap", 0) or 0))
            exact_ratios.append(ratio)
            turnover_5min_list.append(t5)
            turnover_avg_list.append(t_avg)
        top_candidates = top_candidates.copy()
        top_candidates["exact_volume_ratio"] = exact_ratios
        top_candidates["turnover_5min"] = turnover_5min_list
        top_candidates["turnover_avg"] = turnover_avg_list
        valid = top_candidates[top_candidates["exact_volume_ratio"] >= _VOLUME_RATIO_THRESHOLD]
        if valid.empty:
            valid = top_candidates
        valid = valid.sort_values(["exact_volume_ratio", "turnover_5min"], ascending=[False, False])
        selected = valid.head(top_n)
    else:
        selected = candidates.sort_values(tr_col, ascending=False).head(top_n)

    picks = []
    for _, row in selected.iterrows():
        extra = {"volume_ratio": row.get(vr_col), "turnover_rate": row.get(tr_col)}
        if "exact_volume_ratio" in row:
            extra.update(
                {
                    "exact_volume_ratio": row["exact_volume_ratio"],
                    "turnover_5min": row["turnover_5min"],
                    "turnover_avg": row["turnover_avg"],
                }
            )
        picks.append(_to_pick(row, RULE_VOLUME_SURGE, extra))
    return picks


def select_intraday_samples(
    registry: ProviderRegistry | None = None,
    tdx: Any | None = None,
    top_per_rule: int = 1,
) -> list[IntradayPick]:
    """按 5 条规则选股，去重后返回候选列表。

    Args:
        registry: 数据源注册表；为 None 时新建 ProviderRegistry。
        tdx: TDX 提供者；为 None 时尝试从 registry 获取。
        top_per_rule: 每条规则选取数量，默认 1。

    Returns:
        IntradayPick 列表，按规则顺序且已去重。
    """
    registry = registry or ProviderRegistry()
    if tdx is None and isinstance(registry, ProviderRegistry):
        tdx = registry.providers.get("tdx")

    spot = registry.get_all_a_spot()
    if spot.empty:
        logger.warning("全 A 快照为空，盘中选股器无法运行")
        return []

    results: list[IntradayPick] = []
    results.extend(_pick_speed_3m(spot, tdx, top_per_rule))
    results.extend(_pick_gain_15m(spot, top_per_rule))
    results.extend(_pick_sector_gain_leader(registry, spot, top_per_rule))
    results.extend(_pick_volume_surge(spot, tdx, top_per_rule))
    results.extend(_pick_sector_drop_laggard(registry, spot, top_per_rule))

    seen: set[str] = set()
    deduped: list[IntradayPick] = []
    for pick in results:
        if pick.symbol and pick.symbol not in seen:
            seen.add(pick.symbol)
            deduped.append(pick)
    return deduped
