from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import json

from core.us_options.config import ScanConfig
from core.us_options.feishu import push_scan_to_feishu
from core.us_options.greeks import black_scholes_greeks
from core.us_options.provider import CboeDelayedOptionProvider, YFinanceOptionProvider


CRITICAL_FIELDS = (
    "symbol",
    "underlying_price",
    "option_type",
    "expiry",
    "strike",
    "bid",
    "ask",
    "volume",
    "openInterest",
)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:
        return None
    return result


def _round(value: Any, digits: int = 6) -> Any:
    number = _safe_float(value)
    return round(number, digits) if number is not None else None


def enrich_contract(raw: dict[str, Any], config: ScanConfig, relaxed_filter: bool = False) -> dict[str, Any]:
    row = dict(raw)
    option_type = str(row.get("option_type", "")).lower()
    row["option_type"] = option_type
    row["strategy"] = "long_call" if option_type == "call" else "long_put" if option_type == "put" else None
    row["side"] = "buy_to_open"
    row["data_source"] = row.get("data_source") or config.data_source
    row["data_quality"] = row.get("data_quality") or config.data_quality
    row["manual_check_required"] = True
    row["relaxed_filter"] = relaxed_filter

    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    row["bid"] = bid
    row["ask"] = ask
    row["mid"] = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    row["contract_cost"] = ask * 100.0 if ask is not None else None
    row["spread_pct"] = (
        (ask - bid) / row["mid"]
        if bid is not None and ask is not None and row["mid"] and row["mid"] > 0
        else None
    )

    for key in ("underlying_price", "strike", "implied_volatility", "delta", "gamma", "theta"):
        row[key] = _safe_float(row.get(key))
    dte = _safe_float(row.get("DTE"))
    row["DTE"] = int(dte) if dte is not None else None
    volume = _safe_float(row.get("volume"))
    open_interest = _safe_float(row.get("openInterest"))
    row["volume"] = int(volume) if volume is not None else None
    row["openInterest"] = int(open_interest) if open_interest is not None else None

    if row.get("delta") is None or row.get("gamma") is None or row.get("theta") is None:
        greeks = black_scholes_greeks(
            underlying_price=row.get("underlying_price") or 0.0,
            strike=row.get("strike") or 0.0,
            dte=row.get("DTE") or 0,
            option_type=option_type,
            implied_volatility=row.get("implied_volatility") or 0.0,
            risk_free_rate=config.risk_free_rate,
            dividend_yield=config.dividend_yield,
        )
        if greeks:
            row["delta"] = row["delta"] if row["delta"] is not None else greeks.delta
            row["gamma"] = row["gamma"] if row["gamma"] is not None else greeks.gamma
            row["theta"] = row["theta"] if row["theta"] is not None else greeks.theta

    row["theta_burn_pct"] = (
        abs(row["theta"]) / ask if row.get("theta") is not None and ask is not None and ask > 0 else None
    )
    row["risk_flags"] = risk_flags(row, config)
    row["liquidity_grade"] = liquidity_grade(row, config)
    row["alert_level"] = alert_level(row)
    row["reason_short"] = reason_short(row)
    row["contract_hint"] = contract_hint(row)
    row["push_text"] = format_push_line(row)
    score, breakdown = score_contract(row, config)
    row["score"] = score
    row["score_breakdown"] = breakdown
    return normalize_output(row)


def risk_flags(row: dict[str, Any], config: ScanConfig) -> list[str]:
    flags: list[str] = []
    thresholds = config.liquidity
    missing = [field for field in CRITICAL_FIELDS if row.get(field) is None]
    if missing:
        flags.append("DATA_MISSING")
    bid = row.get("bid")
    ask = row.get("ask")
    spread_pct = row.get("spread_pct")
    volume = row.get("volume")
    open_interest = row.get("openInterest")

    if bid is None or bid <= 0:
        flags.append("NO_BID")
    if ask is None or ask <= 0:
        flags.append("NO_ASK")
    if ask is not None and ask > thresholds.max_ask:
        flags.append("TOO_EXPENSIVE")
    if ask is not None and 0 < ask < thresholds.min_ask:
        flags.append("TOO_CHEAP_STALE")
    if spread_pct is None:
        flags.append("SPREAD_MISSING")
    elif spread_pct > thresholds.max_spread_pct:
        flags.append("WIDE_SPREAD")
    if volume is None or volume < thresholds.min_volume:
        flags.append("LOW_VOLUME")
    if open_interest is None or open_interest < thresholds.min_open_interest:
        flags.append("LOW_OI")
    if (
        bid is None
        or bid <= 0
        or volume is None
        or open_interest is None
        or volume < thresholds.min_volume
        or open_interest < thresholds.min_open_interest
    ):
        flags.append("ILLIQUID")
    return sorted(set(flags))


def liquidity_grade(row: dict[str, Any], config: ScanConfig) -> str:
    thresholds = config.liquidity
    bid = row.get("bid")
    ask = row.get("ask")
    spread_pct = row.get("spread_pct")
    volume = row.get("volume")
    open_interest = row.get("openInterest")

    if (
        bid is None
        or ask is None
        or bid <= 0
        or ask <= 0
        or ask > thresholds.max_ask
        or spread_pct is None
        or volume is None
        or open_interest is None
    ):
        return "DEAD"
    if spread_pct > 1.0 or open_interest < 20:
        return "DEAD"
    if (
        ask <= thresholds.preferred_max_ask
        and ask >= 0.10
        and spread_pct <= thresholds.preferred_spread_pct
        and volume >= thresholds.preferred_volume
        and open_interest >= thresholds.preferred_open_interest
    ):
        return "LIQUID"
    if (
        ask <= thresholds.max_ask
        and spread_pct <= thresholds.max_spread_pct
        and volume >= thresholds.min_volume
        and open_interest >= thresholds.min_open_interest
    ):
        return "TRADABLE"
    return "WATCH"


def alert_level(row: dict[str, Any]) -> str:
    grade = row.get("liquidity_grade")
    if grade in {"LIQUID", "TRADABLE"}:
        return "OPEN"
    if grade == "WATCH":
        return "WATCH"
    return "DEAD"


def reason_short(row: dict[str, Any]) -> str:
    flags = set(row.get("risk_flags") or [])
    if "NO_BID" in flags:
        return "无Bid"
    if "TOO_EXPENSIVE" in flags:
        return "超预算"
    if "WIDE_SPREAD" in flags:
        return "价差宽"
    if "LOW_VOLUME" in flags and "LOW_OI" in flags:
        return "量/OI弱"
    if "LOW_VOLUME" in flags:
        return "成交弱"
    if "LOW_OI" in flags:
        return "OI弱"
    if "TOO_CHEAP_STALE" in flags:
        return "过低价可能僵尸"
    return "盘口可跟踪"


def reject_reasons(row: dict[str, Any], config: ScanConfig) -> list[str]:
    reasons: list[str] = []
    if row.get("fetch_error"):
        reasons.append("FETCH_ERROR")
    if "DATA_MISSING" in row.get("risk_flags", []):
        reasons.append("DATA_MISSING")
    if config.direction == "call" and row.get("option_type") != "call":
        reasons.append("DIRECTION_EXCLUDED")
    if config.direction == "put" and row.get("option_type") != "put":
        reasons.append("DIRECTION_EXCLUDED")
    if row.get("option_type") not in {"call", "put"}:
        reasons.append("UNSUPPORTED_OPTION_TYPE")
    if row.get("liquidity_grade") == "DEAD":
        reasons.append("DEAD_LIQUIDITY")
    if "NO_BID" in row.get("risk_flags", []):
        reasons.append("NO_BID")
    if "TOO_EXPENSIVE" in row.get("risk_flags", []):
        reasons.append("TOO_EXPENSIVE")
    if row.get("contract_cost") is None or row["contract_cost"] > config.budget:
        reasons.append("OVER_BUDGET")
    return sorted(set(reasons))


def score_contract(row: dict[str, Any], config: ScanConfig) -> tuple[float, dict[str, float]]:
    thresholds = config.liquidity
    bid = row.get("bid") or 0.0
    ask = row.get("ask") or 0.0
    volume = max(row.get("volume") or 0, 0)
    open_interest = max(row.get("openInterest") or 0, 0)
    spread_pct = row.get("spread_pct")
    if spread_pct is None:
        spread_pct = 2.0

    bid_exists_score = 25.0 if bid >= thresholds.min_bid else 0.0
    volume_score = min(25.0, volume / thresholds.preferred_volume * 25.0)
    open_interest_score = min(20.0, open_interest / thresholds.preferred_open_interest * 20.0)
    tight_spread_score = max(0.0, 20.0 * (1.0 - min(spread_pct, thresholds.max_spread_pct) / thresholds.max_spread_pct))
    affordable_price_score = _affordable_price_score(ask)

    no_bid_penalty = 35.0 if bid < thresholds.min_bid else 0.0
    wide_spread_penalty = 20.0 if spread_pct > thresholds.max_spread_pct else 0.0
    low_liquidity_penalty = 15.0 if volume < thresholds.min_volume or open_interest < thresholds.min_open_interest else 0.0
    stale_quote_penalty = 10.0 if ask and ask < thresholds.min_ask else 0.0

    breakdown = {
        "bid_exists_score": bid_exists_score,
        "volume_score": volume_score,
        "open_interest_score": open_interest_score,
        "tight_spread_score": tight_spread_score,
        "affordable_price_score": affordable_price_score,
        "no_bid_penalty": no_bid_penalty,
        "wide_spread_penalty": wide_spread_penalty,
        "low_liquidity_penalty": low_liquidity_penalty,
        "stale_quote_penalty": stale_quote_penalty,
    }
    score = (
        bid_exists_score
        + volume_score
        + open_interest_score
        + tight_spread_score
        + affordable_price_score
        - no_bid_penalty
        - wide_spread_penalty
        - low_liquidity_penalty
        - stale_quote_penalty
    )
    return round(max(0.0, min(100.0, score)), 4), {key: round(value, 4) for key, value in breakdown.items()}


def _affordable_price_score(ask: float) -> float:
    if ask <= 0:
        return 0.0
    if 0.10 <= ask <= 0.35:
        return 10.0
    if ask <= 0.50:
        return 7.0
    if ask < 0.10:
        return 5.0
    return 0.0


def normalize_output(row: dict[str, Any]) -> dict[str, Any]:
    numeric_digits = {
        "underlying_price": 4,
        "strike": 4,
        "bid": 4,
        "ask": 4,
        "mid": 4,
        "contract_cost": 4,
        "implied_volatility": 6,
        "delta": 6,
        "gamma": 6,
        "theta": 6,
        "theta_burn_pct": 6,
        "spread_pct": 6,
        "score": 4,
    }
    output = dict(row)
    for key, digits in numeric_digits.items():
        output[key] = _round(output.get(key), digits)
    output["push_text"] = format_push_line(output)
    return output


def evaluate_contracts(raw_contracts: Iterable[dict[str, Any]], config: ScanConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enriched = [enrich_contract(row, config) for row in list(raw_contracts)]
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in enriched:
        reasons = reject_reasons(row, config)
        if reasons:
            dead = dict(row)
            dead["reject_reasons"] = reasons
            rejected.append(dead)
        else:
            candidates.append(row)
    candidates = sorted(candidates, key=lambda item: item.get("score") or 0, reverse=True)[: config.top_n]
    rejected = sorted(rejected, key=lambda item: item.get("score") or 0, reverse=True)
    return candidates, rejected


def scan_us_options(config: ScanConfig) -> dict[str, Any]:
    raw_contracts: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    successful_symbols = 0
    pending_symbols = list(config.universe)

    providers = _select_providers(config)
    if not providers:
        failures = [{"symbol": symbol, "reason": f"unsupported data_source: {config.data_source}"} for symbol in config.universe]
    else:
        for provider in providers:
            if not pending_symbols:
                break
            next_pending: list[str] = []
            for symbol in pending_symbols:
                rows, error = provider.fetch_symbol(symbol, config)
                if rows:
                    successful_symbols += 1
                    raw_contracts.extend(rows)
                else:
                    next_pending.append(symbol)
                    if error:
                        failures.append({"symbol": symbol, "reason": f"{provider.data_source}: {error}"})
            pending_symbols = next_pending

    candidates, rejected = evaluate_contracts(raw_contracts, config)
    result = write_scan_outputs(
        config=config,
        raw_contracts=raw_contracts,
        candidates=candidates,
        rejected=rejected,
        failures=failures,
        successful_symbols=successful_symbols,
    )
    if config.push_feishu:
        message = build_push_message(
            config=config,
            candidates=candidates,
            rejected=rejected,
            failures=failures,
            run_dir=result["run_dir"],
        )
        result["feishu_push"] = push_scan_to_feishu(message, result["run_dir"])
    return result


def _select_providers(config: ScanConfig) -> list[Any]:
    data_source = (config.data_source or "auto").lower()
    providers: list[Any] = []
    if data_source in {"auto", "yfinance"}:
        try:
            providers.append(YFinanceOptionProvider())
        except RuntimeError:
            if data_source == "yfinance":
                return []
    if data_source in {"auto", "cboe", "cboe_delayed"}:
        providers.append(CboeDelayedOptionProvider())
    return providers


def write_scan_outputs(
    config: ScanConfig,
    raw_contracts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    failures: list[dict[str, str]] | None = None,
    successful_symbols: int = 0,
    run_time: datetime | None = None,
) -> dict[str, Any]:
    run_time = run_time or datetime.now()
    run_dir = Path(config.output_root) / run_time.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "scan_config.json", config.to_dict())
    _write_jsonl(run_dir / "raw_option_chain.jsonl", raw_contracts)
    _write_jsonl(run_dir / "candidates.jsonl", candidates)
    _write_jsonl(run_dir / "rejected.jsonl", rejected)

    failure_rows = failures or []
    push_preview = build_push_message(config, candidates, rejected, failure_rows, str(run_dir))
    (run_dir / "feishu_message.txt").write_text(push_preview, encoding="utf-8")
    report = build_scan_report(
        config=config,
        run_time=run_time,
        raw_contracts=raw_contracts,
        candidates=candidates,
        rejected=rejected,
        failures=failure_rows,
        successful_symbols=successful_symbols,
    )
    (run_dir / "scan_report.md").write_text(report, encoding="utf-8")
    return {
        "run_dir": str(run_dir),
        "raw_count": len(raw_contracts),
        "candidate_count": len(candidates),
        "rejected_count": len(rejected),
        "successful_symbols": successful_symbols,
        "failed_symbols": len(failure_rows),
    }


def build_scan_report(
    config: ScanConfig,
    run_time: datetime,
    raw_contracts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    failures: list[dict[str, str]],
    successful_symbols: int,
) -> str:
    reason_counter: Counter[str] = Counter()
    for row in rejected:
        reason_counter.update(row.get("reject_reasons", []))

    lines = [
        "# US Option Liquidity Radar v0",
        "",
        f"- 扫描时间：{run_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 数据源：{config.data_source}",
        f"- Universe 数量：{len(config.universe)}",
        f"- 成功拉取数量：{successful_symbols}",
        f"- 失败 symbol 数量：{len(failures)}",
        f"- 总合约数量：{len(raw_contracts)}",
        f"- 流动性候选数量：{len(candidates)}",
        "",
        "## Top Liquidity Candidates",
        "",
    ]
    if candidates:
        lines.extend(
            [
                "| level | symbol | contract | bid | ask | cost | volume | OI | spread | grade | reason | score |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|",
            ]
        )
        for row in candidates[: config.top_n]:
            lines.append(
                "| {alert_level} | {symbol} | {contract_hint} | {bid} | {ask} | {contract_cost} | {volume} | {openInterest} | {spread_pct:.2%} | {liquidity_grade} | {reason_short} | {score} |".format(
                    **{
                        **row,
                        "spread_pct": row.get("spread_pct") or 0.0,
                    }
                )
            )
    else:
        lines.append("暂无可推送流动性候选。")

    dead_preview = [row for row in rejected if row.get("alert_level") == "DEAD"][:5]
    lines.extend(["", "## DEAD 样例", ""])
    if dead_preview:
        for row in dead_preview:
            lines.append(f"- {format_push_line(row)}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 被拒绝最多的原因", ""])
    if reason_counter:
        for reason, count in reason_counter.most_common(10):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 拉取失败", ""])
    if failures:
        for item in failures:
            lines.append(f"- {item['symbol']}: {item['reason']}")
    else:
        lines.append("- 无")

    lines.extend(
        [
            "",
            "## 数据质量警告",
            "",
            "- 免费期权链可能延迟、缺失或临时不可用；本报告不承诺实时准确性。",
            "- 本模式只扫描期权流动性和盘口，不判断正股方向，不接正股触发接口。",
            "- candidates.jsonl 保留 DTE/Greeks/IV 等复盘字段，但短推送不使用这些字段。",
            "- 本工具只输出 long call / long put 候选和风险指标，不自动下单，不输出买入、卖出、仓位、止盈或止损建议。",
            "- 下单前必须用复星国际证券 App 核对实时 bid/ask、价差、成交量、OI 和可交易性。",
        ]
    )
    return "\n".join(lines) + "\n"


def build_push_message(
    config: ScanConfig,
    candidates: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    failures: list[dict[str, str]],
    run_dir: str,
) -> str:
    lines = [
        "AquaTrade Option Liquidity Radar",
        f"标的池：{', '.join(config.universe)}",
        f"候选：{len(candidates)}｜失败symbol：{len(failures)}",
    ]
    if candidates:
        lines.extend(row["push_text"] for row in candidates[: config.top_n])
    else:
        lines.append("暂无 OPEN/WATCH 流动性候选。")

    dead_rows = [row for row in rejected if row.get("alert_level") == "DEAD"][:3]
    if dead_rows:
        lines.append("DEAD样例：")
        lines.extend(format_push_line(row) for row in dead_rows)
    if failures:
        lines.append("拉取失败：" + "; ".join(f"{item['symbol']} {item['reason']}" for item in failures[:5]))
    lines.extend(
        [
            f"详情目录：{run_dir}",
            "仅为流动性扫描，不判断方向，不自动下单；下单前必须用复星国际证券 App 核对实时盘口。",
        ]
    )
    return "\n".join(lines)


def format_push_line(row: dict[str, Any]) -> str:
    level = row.get("alert_level") or alert_level(row)
    hint = row.get("contract_hint") or contract_hint(row)
    bid = _fmt_price(row.get("bid"))
    ask = _fmt_price(row.get("ask"))
    cost = _fmt_money(row.get("contract_cost"))
    vol = _fmt_compact(row.get("volume"))
    oi = _fmt_compact(row.get("openInterest"))
    spread = _fmt_pct(row.get("spread_pct"))
    reason = row.get("reason_short") or reason_short(row)
    if level == "DEAD":
        return f"[DEAD] {row.get('symbol')} {hint}｜{bid}/{ask}｜{reason}｜跳过"
    return f"[{level}] {row.get('symbol')} {hint}｜{bid}/{ask}={cost}｜Vol{vol} OI{oi}｜Spr{spread}"


def contract_hint(row: dict[str, Any]) -> str:
    strike = _fmt_strike(row.get("strike"))
    cp = "C" if row.get("option_type") == "call" else "P" if row.get("option_type") == "put" else "?"
    expiry = str(row.get("expiry") or "")
    try:
        expiry_hint = datetime.strptime(expiry, "%Y-%m-%d").strftime("%m/%d")
    except ValueError:
        expiry_hint = expiry
    return f"{strike}{cp} {expiry_hint}"


def _fmt_price(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "NA"
    return f"{number:.2f}".rstrip("0").rstrip(".") if number != 0 else "0"


def _fmt_money(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "$NA"
    return f"${number:.0f}"


def _fmt_pct(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "NA"
    return f"{number:.0%}"


def _fmt_strike(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "NA"
    return f"{number:g}"


def _fmt_compact(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "NA"
    if number >= 1000:
        compact = number / 1000.0
        return f"{compact:.1f}k".replace(".0k", "k")
    return str(int(number))


def _contract_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key))
        for key in ("symbol", "option_type", "expiry", "strike", "bid", "ask")
    )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
