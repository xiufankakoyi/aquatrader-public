"""Rule-based event structure scanner for PatternRadar."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config.logger import get_logger
from server.pattern_lab.pattern_backtester import compute_forward_stats
from server.pattern_lab.pattern_report import build_pattern_report
from server.pattern_lab.pattern_templates import get_pattern_template, merge_params

# 预计算缓存目录: 不强制写, 仅当文件存在时读
DEFAULT_PATTERN_CACHE_DIR = Path(
    os.environ.get(
        "AQUATRADE_PATTERN_CACHE_DIR",
        str(Path(__file__).resolve().parents[2] / "data" / "parquet" / "patterns"),
    )
)

logger = get_logger(__name__)


EVENT_SEQUENCE_COLUMNS = [
    "is_big_up",
    "is_big_down",
    "is_limit_up",
    "is_limit_down",
    "is_failed_limit_up",
    "is_gap_up",
    "is_gap_down",
    "close_near_high",
    "close_near_low",
    "high_open_low_close",
    "long_upper_shadow",
    "long_lower_shadow",
    "above_ma5",
    "below_ma5",
    "above_ma10",
    "new_high_20d",
    "volume_burst",
    "volume_shrink",
    "strong_attack_day",
    "first_divergence_day",
    "weak_acceptance_day",
    "counterattack_day",
    "break_board_day",
]


class PatternScanner:
    """Scan pre-generated daily event tags for short-term structures."""

    def __init__(self, event_store=None):
        self.event_store = event_store

    def search(
        self,
        pattern_id: str,
        start_date: str,
        end_date: str,
        symbols: Optional[List[str]] = None,
        params: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        max_symbols: int = 30,
        max_window_days: int = 60,
        top_n: Optional[int] = None,
    ) -> Dict[str, Any]:
        """扫描指定形态.

        Args:
            pattern_id: 形态模板 ID.
            start_date / end_date: 用户请求的扫描日期区间.
            symbols: 股票池, None 表示不限.
            params: 覆盖默认参数.
            limit: 返回的命中条数上限 (兼容旧前端).
            max_symbols: 首屏限制, 股票池超过此值时仅取前 N 个 (后台 job 传 0 表示不限).
            max_window_days: 首屏限制, 日期区间超过此值时收紧到最近 N 天 (0 表示不限).
            top_n: 业务上的"前 N 条", 优先级高于 limit; None 表示沿用 limit.

        Returns:
            build_pattern_report 的输出, 额外附带 query_plan / cache_hit / applied_window
        """
        from server.event_engine.event_store import DailyEventTagStore

        store = self.event_store or DailyEventTagStore()
        merged_params = merge_params(pattern_id, params)

        # ---- 首屏限制: 收紧 symbols / 日期窗口 ----
        symbols_list = list(symbols) if symbols else None
        applied_window = (start_date, end_date)
        if max_symbols and max_symbols > 0 and (symbols_list is None or len(symbols_list) > max_symbols):
            symbols_list = symbols_list[:max_symbols] if symbols_list else None
        if max_window_days and max_window_days > 0:
            try:
                sd = datetime.strptime(start_date[:10], "%Y-%m-%d")
                ed = datetime.strptime(end_date[:10], "%Y-%m-%d")
                if (ed - sd).days > max_window_days:
                    ed = sd + timedelta(days=max_window_days)
                    applied_window = (sd.strftime("%Y-%m-%d"), ed.strftime("%Y-%m-%d"))
            except ValueError:
                pass

        # ---- 优先尝试读预计算缓存 ----
        cache_result = self._try_load_cache(
            pattern_id=pattern_id,
            start_date=applied_window[0],
            end_date=applied_window[1],
            symbols=symbols_list,
        )
        if cache_result is not None:
            report = cache_result
            report.setdefault("query_plan", "precomputed_cache")
            report.setdefault("cache_hit", True)
            report.setdefault("meta", {})
            report["meta"]["cache_hit"] = True
            report["meta"]["query_plan"] = "precomputed_cache"
            report["meta"]["applied_window"] = {
                "start_date": applied_window[0],
                "end_date": applied_window[1],
                "max_symbols": max_symbols,
                "max_window_days": max_window_days,
            }
            # 截取 top_n / limit
            eff_top = int(top_n) if top_n else int(limit)
            report["results"] = report.get("results", [])[: max(1, eff_top)]
            return report

        # ---- 实时扫描 ----
        read_start = _shift_date(applied_window[0], -max(30, int(merged_params.get("trend_window_days", 20)) + 10))
        read_end = _shift_date(applied_window[1], 20)
        rows = store.query_event_tags(read_start, read_end, symbols=symbols_list, filters=None, limit=200000)
        event_df = pd.DataFrame(rows)
        report = self.search_dataframe(
            pattern_id,
            event_df,
            applied_window[0],
            applied_window[1],
            merged_params,
            limit=int(top_n) if top_n else int(limit),
        )
        report.setdefault("meta", {})
        report["meta"]["cache_hit"] = False
        report["meta"]["query_plan"] = "live_scan"
        report["meta"]["applied_window"] = {
            "start_date": applied_window[0],
            "end_date": applied_window[1],
            "max_symbols": max_symbols,
            "max_window_days": max_window_days,
        }
        return report

    # ---- 预计算缓存读取 ----
    @staticmethod
    def _resolve_cache_path(pattern_id: str) -> Optional[Path]:
        """根据 pattern_id 解析候选 parquet 路径, 文件不存在返回 None."""
        if not pattern_id:
            return None
        safe_id = re.sub(r"[^a-zA-Z0-9_\\-]", "_", str(pattern_id))
        candidates = [
            DEFAULT_PATTERN_CACHE_DIR / f"{safe_id}.parquet",
            DEFAULT_PATTERN_CACHE_DIR / f"{safe_id}.snappy.parquet",
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    @staticmethod
    def _filter_cache_by_query(
        df: pd.DataFrame,
        start_date: str,
        end_date: str,
        symbols: Optional[List[str]],
    ) -> pd.DataFrame:
        """按日期/股票过滤缓存结果, 字段缺失时降级返回原 df."""
        if df is None or df.empty:
            return df
        out = df
        if "match_date" in out.columns:
            out = out[(out["match_date"] >= str(start_date)) & (out["match_date"] <= str(end_date))]
        if symbols and "symbol" in out.columns:
            sym_set = {str(s) for s in symbols}
            out = out[out["symbol"].astype(str).isin(sym_set)]
        return out

    @classmethod
    def _try_load_cache(
        cls,
        pattern_id: str,
        start_date: str,
        end_date: str,
        symbols: Optional[List[str]],
    ) -> Optional[Dict[str, Any]]:
        """尝试读预计算缓存. 命中返回 report 字典, 失败返回 None.

        缓存 schema 约定:
          - 必须包含列: pattern_id, symbol, match_date, match_score
          - 其余字段透传, 缺失时降级处理
        """
        path = cls._resolve_cache_path(pattern_id)
        if path is None:
            return None
        try:
            df = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pattern_scanner] 缓存读取失败 (%s): %s", path, exc)
            return None
        if df is None or df.empty:
            return None
        if "pattern_id" in df.columns:
            df = df[df["pattern_id"].astype(str) == str(pattern_id)]
            if df.empty:
                return None
        filtered = cls._filter_cache_by_query(df, start_date, end_date, symbols)
        if filtered is None or filtered.empty:
            return None
        # 排序 + 限速
        if "match_score" in filtered.columns:
            filtered = filtered.sort_values("match_score", ascending=False)
        records: List[Dict[str, Any]] = []
        for row in filtered.to_dict("records"):
            records.append(
                {
                    "pattern_id": str(row.get("pattern_id") or pattern_id),
                    "pattern_name": str(row.get("pattern_name") or pattern_id),
                    "symbol": str(row.get("symbol") or row.get("stock_code") or ""),
                    "stock_name": _none_if_nan(row.get("stock_name")),
                    "match_date": str(row.get("match_date") or ""),
                    "match_score": float(row.get("match_score") or 0.0),
                    "hit_reasons": _safe_list(row.get("hit_reasons")),
                    "risk_flags": _safe_list(row.get("risk_flags")),
                    "event_sequence": _safe_list(row.get("event_sequence")),
                    "concept_tags": _safe_list(row.get("concept_tags")),
                    **{
                        k: row.get(k)
                        for k in (
                            "future_return_1d",
                            "future_return_3d",
                            "future_return_5d",
                            "future_return_10d",
                            "max_gain_5d",
                            "max_drawdown_5d",
                            "success_label",
                            "failure_reason",
                        )
                        if k in filtered.columns
                    },
                }
            )
        from server.pattern_lab.pattern_templates import get_pattern_template as _tpl

        try:
            template = _tpl(pattern_id)
            pattern_name = template.pattern_name
        except ValueError:
            pattern_name = pattern_id
        return build_pattern_report(
            pattern_id=pattern_id,
            pattern_name=pattern_name,
            start_date=start_date,
            end_date=end_date,
            params={},
            matches=records,
        )

    @classmethod
    def probe_cache(
        cls,
        pattern_id: str,
        start_date: str,
        end_date: str,
        symbols: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """对外暴露: 探测缓存是否命中, 命中返回 {hit, result, total, partial_count}."""
        result = cls._try_load_cache(pattern_id, start_date, end_date, symbols)
        if result is None:
            return None
        results = result.get("results") or []
        return {
            "hit": True,
            "result": result,
            "total": len(results),
            "partial_count": len(results[:10]),
        }

    def search_dataframe(
        self,
        pattern_id: str,
        event_df: pd.DataFrame,
        start_date: str,
        end_date: str,
        params: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        template = get_pattern_template(pattern_id)
        merged_params = merge_params(pattern_id, params)
        normalized = _apply_symbol_filters(_normalize_events(event_df), merged_params)

        matches: List[Dict[str, Any]] = []
        if not normalized.empty:
            for symbol, symbol_df in normalized.groupby("stock_code", sort=True):
                symbol_df = symbol_df.sort_values("trade_date").reset_index(drop=True)
                if pattern_id == "strong_break_reversal":
                    matches.extend(self._scan_strong_break_reversal(symbol_df, start_date, end_date, merged_params))
                elif pattern_id == "trend_ma5_pullback":
                    matches.extend(self._scan_trend_ma5_pullback(symbol_df, start_date, end_date, merged_params))
                elif pattern_id == "volume_reversal_repair":
                    matches.extend(self._scan_volume_reversal_repair(symbol_df, start_date, end_date, merged_params))
                else:
                    raise ValueError(f"unknown pattern_id: {pattern_id}")

        matches.sort(key=lambda item: (item["match_score"], item["match_date"]), reverse=True)
        matches = matches[: max(1, min(int(limit), 500))]
        return build_pattern_report(
            pattern_id=template.pattern_id,
            pattern_name=template.pattern_name,
            start_date=start_date,
            end_date=end_date,
            params=merged_params,
            matches=matches,
        )

    def _scan_strong_break_reversal(
        self,
        df: pd.DataFrame,
        start_date: str,
        end_date: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        matches = []
        lookback_days = int(params["lookback_days"])
        divergence_window = int(params["divergence_window_days"])

        for idx, row in enumerate(df.to_dict("records")):
            date = row["trade_date"]
            if date < start_date or date > end_date or not _truthy(row.get("counterattack_day")):
                continue
            if not _candidate_passes_common_filters(row, params):
                continue

            counterattack_pct = _num(row.get("change_pct"), 0.0) or 0.0
            if counterattack_pct < float(params["min_counterattack_pct"]):
                continue

            prior = df.iloc[max(0, idx - lookback_days) : idx]
            divergence = df.iloc[max(0, idx - divergence_window) : idx]
            if prior.empty or divergence.empty:
                continue

            strong_count = int(prior["strong_attack_day"].fillna(False).astype(bool).sum())
            first_close = _num(prior.iloc[0].get("close"))
            last_close = _num(prior.iloc[-1].get("close"))
            cumulative_gain = ((last_close / first_close - 1.0) * 100.0) if first_close and last_close else 0.0
            has_divergence = bool(
                divergence["first_divergence_day"].fillna(False).astype(bool).any()
                or divergence["break_board_day"].fillna(False).astype(bool).any()
            )
            post_divergence_ok = not bool((divergence["change_pct"].fillna(0) <= float(params["max_post_divergence_drop_pct"])).any())

            strong_ok = strong_count >= int(params["min_strong_attack_days"]) or cumulative_gain >= float(
                params["min_cumulative_gain_pct"]
            )
            if not (strong_ok and has_divergence and post_divergence_ok):
                continue

            score = 0.55 + min(strong_count, 4) * 0.06 + max(0.0, cumulative_gain) / 200.0
            if _truthy(row.get("close_near_high")):
                score += 0.08
            if _truthy(row.get("volume_burst")):
                score += 0.04

            reasons = [
                f"观察窗口内强攻击日 {strong_count} 次",
                f"观察窗口累计涨幅 {cumulative_gain:.2f}%",
                f"反包日涨幅 {counterattack_pct:.2f}%",
                "反包日前存在首次分歧或断板事件",
                "分歧后未出现直接跌崩",
                "当前日出现 counterattack_day",
            ]
            risks = []
            if _truthy(row.get("below_ma5")):
                risks.append("反包日仍在 MA5 下方")
            if _truthy(row.get("long_upper_shadow")):
                risks.append("反包日存在长上影")
            matches.append(self._build_match(df, idx, "strong_break_reversal", "强势断板反包", score, reasons, risks, params))
        return matches

    def _scan_trend_ma5_pullback(
        self,
        df: pd.DataFrame,
        start_date: str,
        end_date: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        matches = []
        trend_window = int(params["trend_window_days"])
        pullback_window = int(params["pullback_window_days"])

        for idx, row in enumerate(df.to_dict("records")):
            date = row["trade_date"]
            if date < start_date or date > end_date:
                continue
            if not _candidate_passes_common_filters(row, params):
                continue
            if idx < max(5, trend_window - 1):
                continue

            trend = df.iloc[idx - trend_window + 1 : idx + 1]
            prior_pullback = df.iloc[max(0, idx - pullback_window) : idx]
            if trend.empty or prior_pullback.empty:
                continue

            first_close = _num(trend.iloc[0].get("close"))
            current_close = _num(row.get("close"))
            trend_gain = ((current_close / first_close - 1.0) * 100.0) if first_close and current_close else 0.0
            above_ma5_days = int(trend["above_ma5"].fillna(False).astype(bool).sum())
            near_ma5 = prior_pullback["distance_to_ma5"].abs().le(float(params["ma5_near_pct"])).any()
            not_effective_break = not prior_pullback["distance_to_ma5"].le(float(params["max_ma5_break_pct"])).any()
            shrink = bool(prior_pullback["volume_shrink"].fillna(False).astype(bool).any())
            repair = (
                _num(row.get("change_pct"), 0.0) >= float(params["min_repair_pct"])
                and _num(row.get("close"), 0.0) > _num(row.get("open"), 0.0)
                and _truthy(row.get("above_ma5"))
            )

            if not (
                trend_gain >= float(params["min_trend_gain_pct"])
                and above_ma5_days >= int(params["min_above_ma5_days"])
                and near_ma5
                and not_effective_break
                and shrink
                and repair
            ):
                continue

            score = 0.48 + min(above_ma5_days / max(trend_window, 1), 1.0) * 0.25 + min(trend_gain, 30.0) / 150.0
            if _truthy(row.get("close_near_high")):
                score += 0.07

            reasons = [
                f"过去 {trend_window} 日涨幅 {trend_gain:.2f}%",
                f"过去 {trend_window} 日站上 MA5 天数 {above_ma5_days}",
                "回踩 MA5 附近且未有效跌破",
                "回踩窗口内出现缩量",
                "当前日出现修复阳线并重新站上 MA5",
            ]
            risks = []
            if _num(row.get("distance_to_20d_high"), 0.0) < -10.0:
                risks.append("距离 20 日高点仍较远")
            if _truthy(row.get("volume_burst")):
                risks.append("修复日量能过度放大")
            matches.append(self._build_match(df, idx, "trend_ma5_pullback", "趋势主升浪回踩 MA5", score, reasons, risks, params))
        return matches

    def _scan_volume_reversal_repair(
        self,
        df: pd.DataFrame,
        start_date: str,
        end_date: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        matches = []
        spike_window = int(params["spike_window_days"])

        for idx, row in enumerate(df.to_dict("records")):
            date = row["trade_date"]
            if date < start_date or date > end_date or idx == 0:
                continue
            if not _candidate_passes_common_filters(row, params):
                continue

            repair = (
                _num(row.get("change_pct"), 0.0) >= float(params["min_repair_pct"])
                and _truthy(row.get("close_near_high"))
            ) or _truthy(row.get("counterattack_day"))
            if not repair:
                continue

            spike_start = max(0, idx - spike_window)
            spike_candidates = []
            for spike_idx, spike in enumerate(
                df.iloc[spike_start:idx].to_dict("records"),
                start=spike_start,
            ):
                amplitude = _amplitude_pct(spike)
                has_reversal = _truthy(spike.get("high_open_low_close")) or _truthy(spike.get("long_upper_shadow"))
                if _truthy(spike.get("volume_burst")) and has_reversal and amplitude >= float(params["min_amplitude_pct"]):
                    spike_candidates.append((spike_idx, spike, amplitude))
            if not spike_candidates:
                continue

            spike_idx, spike, amplitude = spike_candidates[-1]
            score = 0.52 + min(amplitude, 18.0) / 90.0
            if _truthy(row.get("counterattack_day")):
                score += 0.12
            if _num(row.get("close"), 0.0) > _num(spike.get("close"), 0.0):
                score += 0.08

            reasons = [
                f"{spike['trade_date']} 出现爆量事件",
                f"爆量日振幅 {amplitude:.2f}%",
                "爆量日存在冲高回落或长上影",
                "随后 1-3 日内出现修复",
            ]
            risks = []
            if _truthy(row.get("below_ma5")):
                risks.append("修复日仍在 MA5 下方")
            if _num(row.get("amount_rank_20d"), 1.0) < 0.5:
                risks.append("修复日成交额分位偏低")
            match = self._build_match(df, idx, "volume_reversal_repair", "爆量冲高回落后修复", score, reasons, risks, params)
            match["hit_reasons"].insert(1, f"爆量冲高回落日：{spike['trade_date']}")
            match["event_sequence"] = _event_sequence(df, max(0, spike_idx - 1), min(len(df), idx + 6))
            matches.append(match)
        return matches

    def _build_match(
        self,
        df: pd.DataFrame,
        idx: int,
        pattern_id: str,
        pattern_name: str,
        score: float,
        hit_reasons: List[str],
        risk_flags: List[str],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        row = df.iloc[idx]
        forward = compute_forward_stats(df, idx, success_gain_5d_pct=float(params.get("success_gain_5d_pct", 5.0)))
        if forward.get("success_label") is None and "后续样本不足" not in risk_flags:
            risk_flags = [*risk_flags, "后续样本不足，当前候选或近端样本"]
        return {
            "pattern_id": pattern_id,
            "pattern_name": pattern_name,
            "symbol": str(row.get("stock_code")),
            "stock_name": _none_if_nan(row.get("stock_name")),
            "match_date": str(row.get("trade_date")),
            "event_sequence": _event_sequence(df, max(0, idx - 8), min(len(df), idx + 11)),
            "match_score": round(float(min(score, 1.0)), 4),
            "hit_reasons": hit_reasons,
            "risk_flags": risk_flags,
            "concept_tags": [],
            **forward,
        }


def _normalize_events(event_df: pd.DataFrame) -> pd.DataFrame:
    if event_df is None or event_df.empty:
        return pd.DataFrame()
    df = event_df.copy()
    if "stock_code" not in df.columns and "symbol" in df.columns:
        df["stock_code"] = df["symbol"]
    if "trade_date" not in df.columns and "date" in df.columns:
        df["trade_date"] = df["date"]
    if "stock_code" not in df.columns or "trade_date" not in df.columns:
        raise ValueError("event_df requires stock_code and trade_date")
    df["stock_code"] = df["stock_code"].astype(str)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for column in EVENT_SEQUENCE_COLUMNS:
        if column not in df.columns:
            df[column] = False
        df[column] = df[column].fillna(False).astype(bool)
    for column in [
        "open",
        "high",
        "low",
        "close",
        "change_pct",
        "volume",
        "amount",
        "total_mv",
        "turnover_rate",
        "amount_rank_20d",
        "distance_to_ma5",
        "distance_to_20d_high",
    ]:
        if column not in df.columns:
            df[column] = None
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if "stock_name" not in df.columns:
        df["stock_name"] = None
    return df.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)


def _apply_symbol_filters(df: pd.DataFrame, params: Dict[str, Any]) -> pd.DataFrame:
    if df.empty:
        return df
    filtered = df
    if _truthy(params.get("exclude_st")) and "stock_name" in filtered.columns:
        names = filtered["stock_name"].fillna("").astype(str).str.upper()
        filtered = filtered[~names.str.contains("ST", regex=False)]
    return filtered.reset_index(drop=True)


def _candidate_passes_common_filters(row: pd.Series, params: Dict[str, Any]) -> bool:
    min_amount = _first_param(params, "min_amount", "min_amount_yuan", "amount_min")
    if min_amount is not None and (_num(row.get("amount"), 0.0) or 0.0) < float(min_amount):
        return False

    min_market_cap = _first_param(params, "min_market_cap", "min_total_mv", "market_cap_min")
    if min_market_cap is not None and (_num(row.get("total_mv"), 0.0) or 0.0) < float(min_market_cap):
        return False

    max_market_cap = _first_param(params, "max_market_cap", "max_total_mv", "market_cap_max")
    total_mv = _num(row.get("total_mv"))
    if max_market_cap is not None and total_mv is not None and total_mv > float(max_market_cap):
        return False
    return True


def _first_param(params: Dict[str, Any], *names: str) -> Any:
    for name in names:
        value = params.get(name)
        if value is not None and value != "":
            return value
    return None


def _event_sequence(df: pd.DataFrame, start: int, end: int) -> List[Dict[str, Any]]:
    rows = []
    for row in df.iloc[start:end].to_dict("records"):
        events = [column for column in EVENT_SEQUENCE_COLUMNS if _truthy(row.get(column))]
        rows.append(
            {
                "date": str(row.get("trade_date")),
                "close": _num(row.get("close")),
                "change_pct": _num(row.get("change_pct")),
                "events": events,
            }
        )
    return rows


def _shift_date(date_text: str, calendar_days: int) -> str:
    return (datetime.strptime(str(date_text)[:10], "%Y-%m-%d") + timedelta(days=calendar_days)).strftime("%Y-%m-%d")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _none_if_nan(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return value
    return value


def _safe_list(value: Any) -> List[Any]:
    """将 cell 列表/ndarray 统一为 Python list, 字段缺失时降级返回空列表."""
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    if isinstance(value, list):
        return [item for item in value if item is not None]
    try:
        # numpy array / pd.Series
        return [item for item in list(value) if item is not None]
    except (TypeError, ValueError):
        return [value]


def _amplitude_pct(row: pd.Series) -> float:
    low = _num(row.get("low"), 0.0) or 0.0
    high = _num(row.get("high"), 0.0) or 0.0
    if low <= 0:
        return 0.0
    return (high / low - 1.0) * 100.0
