"""Grounded compact recap helpers shared by Research and ClawBot."""

from __future__ import annotations

import re
import queue
import threading
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _code(value: Any) -> str:
    match = re.search(r"\d{6}", str(value or ""))
    return match.group(0) if match else ""


def compare_previous_mentions(
    previous_watchlist: list[dict[str, Any]],
    current_snapshots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Evaluate yesterday's observations using today's fresh quotes."""
    current = {
        _code(row.get("symbol")): row
        for row in current_snapshots
        if isinstance(row, dict) and _code(row.get("symbol"))
    }
    tracking: list[dict[str, Any]] = []
    corrections: list[str] = []
    for old in previous_watchlist or []:
        code = _code(old.get("symbol"))
        row = current.get(code)
        name = str(old.get("stock_name") or code or "未知")
        if not row or not row.get("quote_fresh", False):
            tracking.append(
                {"symbol": code, "stock_name": name, "outcome": "待验证", "learning_label": ""}
            )
            continue
        change = _number(row.get("pct_chg"))
        if change <= -9.5:
            outcome, label = "大幅回撤", "昨日判断需修正"
            corrections.append(name)
        elif change <= -3:
            outcome, label = "走弱", "继续复核"
        elif change >= 9.5:
            outcome, label = "显著走强", ""
        elif change > 0:
            outcome, label = "上涨", ""
        elif change < 0:
            outcome, label = "下跌", ""
        else:
            outcome, label = "平盘", ""
        tracking.append(
            {
                "symbol": code,
                "stock_name": str(row.get("stock_name") or name),
                "previous_pct_chg": round(_number(old.get("pct_chg")), 2),
                "current_pct_chg": round(change, 2),
                "outcome": outcome,
                "learning_label": label,
            }
        )
    return tracking, corrections


def summarize_board_fund_flow(
    frame: pd.DataFrame | None, *, theme_names: list[str]
) -> dict[str, Any]:
    """Normalize existing ProviderRegistry board-flow output."""
    if frame is None or frame.empty:
        return {"status": "empty", "source": "", "theme_matches": [], "leaders": []}
    rows = []
    for source in frame.to_dict(orient="records"):
        name = str(source.get("board_name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "board_name": name,
                "main_net_inflow": _number(source.get("main_net_inflow")),
                "main_net_inflow_pct": source.get("main_net_inflow_pct"),
                "provider": str(source.get("provider") or "unknown"),
                "methodology": str(source.get("methodology") or "供应商主力资金口径"),
                "as_of": str(source.get("trade_date") or source.get("as_of") or source.get("updated_at") or ""),
            }
        )
    rows.sort(key=lambda row: row["main_net_inflow"], reverse=True)
    wanted = [str(name).strip() for name in theme_names if str(name).strip()]
    matches = []
    for theme in wanted:
        matched = next(
            (
                row
                for row in rows
                if theme == row["board_name"]
                or theme in row["board_name"]
                or row["board_name"] in theme
                or (
                    len(theme) >= 2
                    and len(row["board_name"]) >= 2
                    and theme[:2] == row["board_name"][:2]
                )
            ),
            None,
        )
        if matched:
            matches.append({"theme_name": theme, **matched})
    source = next((row["provider"] for row in rows if row["provider"]), "")
    return {
        "status": "ok" if rows else "empty",
        "source": source,
        "theme_matches": matches,
        "leaders": rows[:5],
    }


def summarize_external_markets(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    """Keep only US/Korean/Japanese indices and preserve the provider timestamp."""
    if frame is None or frame.empty:
        return []
    patterns = ("纳斯达克", "标普", "道琼斯", "KOSPI", "韩国", "日经", "N225")
    output = []
    for row in frame.to_dict(orient="records"):
        name = str(row.get("名称") or row.get("name") or "")
        if not any(pattern.lower() in name.lower() for pattern in patterns):
            continue
        output.append(
            {
                "name": name,
                "pct_chg": round(_number(row.get("涨跌幅") or row.get("pct_chg")), 2),
                "price": round(_number(row.get("最新价") or row.get("price")), 2),
                "as_of": str(row.get("最新行情时间") or row.get("as_of") or ""),
                "source": str(row.get("provider") or "akshare"),
            }
        )
    priority = {"纳斯达克": 0, "标普": 1, "道琼斯": 2, "KOSPI": 3, "韩国": 3, "日经": 4, "n225": 4}
    output.sort(
        key=lambda row: min(
            (rank for key, rank in priority.items() if key.lower() in row["name"].lower()),
            default=99,
        )
    )
    return output[:6]


def _external_eastmoney() -> pd.DataFrame:
    import akshare as ak

    frame = ak.index_global_spot_em()
    if frame is not None and not frame.empty:
        frame = frame.copy()
        frame["provider"] = "EastMoney"
    return frame


def _external_yahoo(as_of: str) -> pd.DataFrame:
    import yfinance as yf

    cutoff = datetime.fromisoformat(as_of[:10]).date()
    start = cutoff - timedelta(days=7)
    end = cutoff + timedelta(days=1)
    symbols = {
        "^IXIC": "纳斯达克",
        "^GSPC": "标普500",
        "^DJI": "道琼斯",
        "^KS11": "韩国KOSPI",
        "^N225": "日经225",
    }
    history = yf.download(
        list(symbols),
        start=start.isoformat(),
        end=end.isoformat(),
        progress=False,
        auto_adjust=False,
        threads=True,
        timeout=6,
    )
    rows: list[dict[str, Any]] = []
    if history is None or history.empty or "Close" not in history:
        return pd.DataFrame()
    for symbol, name in symbols.items():
        close_values = history["Close"]
        if isinstance(close_values, pd.DataFrame):
            if symbol not in close_values.columns:
                continue
            close_values = close_values[symbol]
        closes = pd.to_numeric(close_values, errors="coerce").dropna()
        if len(closes) < 2:
            continue
        previous, current = float(closes.iloc[-2]), float(closes.iloc[-1])
        rows.append({
            "name": name,
            "pct_chg": (current / previous - 1.0) * 100 if previous else 0.0,
            "price": current,
            "as_of": str(closes.index[-1]),
            "provider": "Yahoo",
        })
    return pd.DataFrame(rows)


def _external_sina(as_of: str) -> pd.DataFrame:
    import akshare as ak

    cutoff = date.fromisoformat(as_of[:10])
    rows: list[dict[str, Any]] = []
    for symbol, name in ((".IXIC", "纳斯达克"), (".INX", "标普500"), (".DJI", "道琼斯")):
        try:
            history = ak.index_us_stock_sina(symbol=symbol)
        except Exception:
            continue
        if history is None or history.empty:
            continue
        history = history.copy()
        history["date"] = pd.to_datetime(history["date"], errors="coerce")
        history = history[history["date"].dt.date <= cutoff].dropna(subset=["date", "close"])
        if len(history) < 2:
            continue
        previous, current = float(history.iloc[-2]["close"]), float(history.iloc[-1]["close"])
        rows.append({
            "name": name,
            "pct_chg": (current / previous - 1.0) * 100 if previous else 0.0,
            "price": current,
            "as_of": str(history.iloc[-1]["date"]),
            "provider": "Sina",
        })
    return pd.DataFrame(rows)


def load_external_market_snapshot(
    *,
    as_of: str = "",
    loaders: list[tuple[str, Any]] | None = None,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Load one coherent external-market snapshot without exposing raw errors."""
    report_time = as_of or datetime.now().isoformat()
    configured = loaders or [
        ("EastMoney", _external_eastmoney),
        ("Yahoo", lambda: _external_yahoo(report_time)),
        ("Sina", lambda: _external_sina(report_time)),
    ]
    attempts: list[str] = []
    for provider, loader in configured:
        attempts.append(provider)
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                outcome.put((True, loader()))
            except Exception as exc:
                outcome.put((False, exc))

        threading.Thread(target=run, daemon=True).start()
        try:
            succeeded, value = outcome.get(timeout=max(0.001, timeout_seconds))
        except queue.Empty:
            continue
        if not succeeded:
            continue
        markets = summarize_external_markets(value)
        if markets:
            return {
                "status": "ok",
                "source": provider,
                "markets": markets,
                "attempts": attempts,
                "error": "",
            }
    return {
        "status": "unavailable",
        "source": "",
        "markets": [],
        "attempts": attempts,
        "error": f"外围数据暂不可用（已尝试：{'、'.join(attempts)}）",
    }


def build_short_recap(payload: dict[str, Any]) -> str:
    """Build a deterministic low-token recap from report fields only."""
    review = payload.get("market_review") or {}
    average = _number(review.get("average_pct_chg"))
    rating = (
        "强势" if average >= 1.5 else "偏强" if average >= 0.3
        else "震荡" if average > -0.3 else "偏弱" if average > -1.5 else "弱势"
    )
    themes = [
        str(row.get("theme_name") or "")
        for row in payload.get("theme_strength") or []
        if str(row.get("theme_name") or "")
    ][:3]
    theme_text = "、".join(themes) or "暂无可靠数据"
    leaders = []
    for row in (payload.get("close_watchlist") or [])[:3]:
        name = str(row.get("stock_name") or row.get("symbol") or "未知")
        change = _number(row.get("pct_chg"))
        support = str(row.get("support_label") or "暂无支撑数据")
        price = row.get("support_price")
        level = f"{support} {float(price):.2f}" if price not in (None, "") else support
        leaders.append(f"{name} {change:+.2f}%，{level}")
    leader_text = "；".join(leaders) or "暂无可靠数据"
    focus = themes[0] if themes else "暂无可靠方向"
    reason = "强度与资金延续待验" if themes else "数据不足继续观察"
    ai_summary = str(payload.get("ai_summary") or "").strip()
    if ai_summary and "AI生成" not in ai_summary:
        ai_summary += "（AI生成）"
    raw_date = str(payload.get("as_of") or "")[:10]
    try:
        year, month, day = (int(part) for part in raw_date.split("-"))
        date = f"{year}/{month}/{day}"
    except (TypeError, ValueError):
        date = raw_date.replace("-", "/")
    lines = [
        f"{date}复盘",
        f"今日大盘：{rating}，涨{int(_number(review.get('rising_count')))}跌{int(_number(review.get('falling_count')))}",
        f"领涨板块：{theme_text}",
        f"抗跌板块：{theme_text}",
        f"龙头股：{leader_text}",
        f"明日关注：{focus}",
        f"原因：{reason[:15]}",
    ]
    if ai_summary:
        lines.append(ai_summary[:80])
    return "\n".join(lines)[:360]
