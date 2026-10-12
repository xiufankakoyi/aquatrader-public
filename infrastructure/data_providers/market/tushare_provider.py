"""
Tushare Pro 数据提供者。

使用 tushare 库获取日K线、股票基础信息、公告等。
需要 Tushare Token（从 config/setting.py 读取）。
"""

from __future__ import annotations

import logging
import os
import re
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import (
    DAILY_BARS_COLUMNS,
    EARNINGS_FORECAST_COLUMNS,
    FINANCE_SNAPSHOT_COLUMNS,
    CONSENSUS_ESTIMATE_COLUMNS,
    VALUATION_HISTORY_COLUMNS,
    MARKET_SNAPSHOT_COLUMNS,
    STOCK_BASIC_INFO_COLUMNS,
    BaseMarketDataProvider,
    empty_frame,
    ensure_columns,
    now_text,
    normalize_symbols,
    to_numeric_series,
    pick_column,
)

logger = logging.getLogger(__name__)

# 尝试导入 tushare
_ts_pro: Any = None
try:
    import tushare as _ts
    from config.setting import Setting

    _token = Setting.TUSHARE_TOKEN
    if _token:
        _ts_pro = _ts.pro_api(_token, timeout=5)
except Exception:
    pass


def _to_ts_code(code: str) -> str:
    """将股票代码转换为 Tushare 格式（如 600036.SH）。

    Args:
        code: 纯数字股票代码

    Returns:
        Tushare 格式代码
    """
    code = re.sub(r"[^0-9]", "", code)
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


class TushareProvider(BaseMarketDataProvider):
    """Tushare Pro 数据提供者，提供日K线、基础信息、公告。"""

    name = "tushare"

    def is_available(self) -> bool:
        """检查 tushare 是否已安装且 token 已配置。"""
        return _ts_pro is not None

    def health(self) -> dict[str, Any]:
        """返回健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": ["daily_bars", "stock_basic_info", "announcements", "earnings_forecasts", "finance_snapshot", "consensus_estimates", "valuation_history"],
        }

    def get_valuation_history(
        self, start_date: str, end_date: str, symbols: list[str] | None = None
    ) -> pd.DataFrame:
        if not self.is_available() or not symbols:
            return empty_frame(VALUATION_HISTORY_COLUMNS)
        frames: list[pd.DataFrame] = []
        for symbol in symbols:
            try:
                frame = _ts_pro.daily_basic(
                    ts_code=_to_ts_code(symbol),
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    fields=(
                        "ts_code,trade_date,close,turnover_rate,pe,pe_ttm,pb,ps,ps_ttm,"
                        "dv_ratio,dv_ttm,total_mv,circ_mv"
                    ),
                )
            except Exception as exc:
                logger.warning("Tushare daily_basic(%s) failed: %s", symbol, type(exc).__name__)
                continue
            if frame is not None and not frame.empty:
                frames.append(frame)
        if not frames:
            return empty_frame(VALUATION_HISTORY_COLUMNS)
        frame = pd.concat(frames, ignore_index=True)
        result = pd.DataFrame(index=frame.index)
        result["symbol"] = pick_column(frame, ["ts_code"], "").astype(str)
        raw_date = pick_column(frame, ["trade_date"], "").astype(str)
        result["trade_date"] = (
            raw_date.str[:4] + "-" + raw_date.str[4:6] + "-" + raw_date.str[6:8]
        )
        for output, source in (
            ("close", "close"), ("turnover_rate", "turnover_rate"),
            ("pe", "pe"), ("pe_ttm", "pe_ttm"), ("pb", "pb"),
            ("ps", "ps"), ("ps_ttm", "ps_ttm"),
            ("dividend_yield", "dv_ratio"), ("dividend_yield_ttm", "dv_ttm"),
        ):
            result[output] = to_numeric_series(pick_column(frame, [source]))
        result["total_market_cap"] = to_numeric_series(pick_column(frame, ["total_mv"])) * 10_000
        result["float_market_cap"] = to_numeric_series(pick_column(frame, ["circ_mv"])) * 10_000
        result["provider"] = self.name
        result["updated_at"] = now_text()
        return ensure_columns(result, VALUATION_HISTORY_COLUMNS)

    def get_consensus_estimates(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """Return sell-side estimate rows; Tushare monetary fields are 万元."""
        if not self.is_available() or not symbols:
            return empty_frame(CONSENSUS_ESTIMATE_COLUMNS)
        frames: list[pd.DataFrame] = []
        cache_dir = Path(os.getenv(
            "TUSHARE_CONSENSUS_CACHE_DIR", "data/research_cache/tushare_consensus"
        ))
        if len(symbols) > 1:
            cache_dir.mkdir(parents=True, exist_ok=True)
            raw_cache_path = cache_dir / "all_reports.json"
            state_path = cache_dir / "pagination_state.json"
            try:
                cached_rows = json.loads(raw_cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                cached_rows = []
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                state = {}
            lower = date.fromisoformat(start_date[:10])
            window_end = date.fromisoformat(end_date[:10])
            windows: list[tuple[date, date]] = []
            while window_end >= lower:
                month_start = window_end.replace(day=1)
                windows.append((max(lower, month_start), window_end))
                window_end = month_start - timedelta(days=1)

            daily_limit = max(1, min(10, int(os.getenv(
                "TUSHARE_REPORT_RC_DAILY_REQUESTS", "10"
            ))))
            today = date.today().isoformat()
            daily_usage = state.get("_daily_usage", {})
            used_today = int(daily_usage.get("calls", 0) or 0) if daily_usage.get("date") == today else 0
            request_budget = max(0, daily_limit - used_today)
            for window_start, current_end in windows:
                if request_budget <= 0:
                    break
                window_key = f"{window_start.isoformat()}:{current_end.isoformat()}"
                cursor = state.get(window_key, {})
                offset = int(cursor.get("next_offset", 0) or 0)
                if cursor.get("complete", False):
                    continue
                while request_budget > 0:
                    used_today += 1
                    request_budget -= 1
                    state["_daily_usage"] = {"date": today, "calls": used_today}
                    min_interval = max(0.0, float(os.getenv(
                        "TUSHARE_REPORT_RC_MIN_INTERVAL_SECONDS", "61"
                    )))
                    last_request_at = float(state.get("_last_request_at", 0.0) or 0.0)
                    wait_seconds = min_interval - (time.time() - last_request_at)
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    try:
                        page = _ts_pro.report_rc(
                            start_date=window_start.strftime("%Y%m%d"),
                            end_date=current_end.strftime("%Y%m%d"),
                            limit=3000,
                            offset=offset,
                        )
                    except Exception as exc:
                        state["_last_request_at"] = time.time()
                        message = str(exc)
                        if "10次/天" in message:
                            used_today = daily_limit
                            state["_daily_usage"] = {"date": today, "calls": daily_limit}
                        logger.warning(
                            "Tushare report_rc batch offset=%s failed: %s",
                            offset, type(exc).__name__,
                        )
                        request_budget = 0
                        break
                    state["_last_request_at"] = time.time()
                    page = page if page is not None else pd.DataFrame()
                    if not page.empty:
                        cached_rows.extend(page.to_dict(orient="records"))
                    offset += 3000
                    cursor = {
                        "next_offset": offset,
                        "complete": len(page) < 3000,
                    }
                    state[window_key] = cursor
                    if len(page) < 3000:
                        break
                if request_budget <= 0:
                    break
            if windows:
                try:
                    raw = pd.DataFrame(cached_rows)
                    if not raw.empty:
                        raw = raw.drop_duplicates()
                        cached_rows = raw.to_dict(orient="records")
                    temporary = raw_cache_path.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(cached_rows, ensure_ascii=False, default=str),
                        encoding="utf-8",
                    )
                    temporary.replace(raw_cache_path)
                    temporary_state = state_path.with_suffix(".json.tmp")
                    temporary_state.write_text(
                        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
                    )
                    temporary_state.replace(state_path)
                except OSError:
                    pass
            batch = pd.DataFrame(cached_rows)
            if not batch.empty and "ts_code" in batch:
                wanted = {_to_ts_code(symbol) for symbol in symbols}
                batch = batch[batch["ts_code"].astype(str).isin(wanted)]
                if "report_date" in batch:
                    raw_dates = batch["report_date"].astype(str).str.replace("-", "", regex=False)
                    batch = batch[
                        (raw_dates >= start_date.replace("-", ""))
                        & (raw_dates <= end_date.replace("-", ""))
                    ]
            if not batch.empty:
                frames.append(batch)
        else:
            for symbol in symbols:
                code = _to_ts_code(symbol)
                cache_path = cache_dir / f"{code.replace('.', '_')}.json"
                frame = pd.DataFrame()
                try:
                    frame = _ts_pro.report_rc(
                        ts_code=code,
                        start_date=start_date.replace("-", ""),
                        end_date=end_date.replace("-", ""),
                    )
                except Exception as exc:
                    logger.warning("Tushare report_rc(%s) failed: %s", symbol, type(exc).__name__)
                    if cache_path.exists():
                        try:
                            frame = pd.DataFrame(json.loads(cache_path.read_text(encoding="utf-8")))
                        except (OSError, json.JSONDecodeError, TypeError):
                            frame = pd.DataFrame()
                if frame is not None and not frame.empty:
                    if "report_date" in frame:
                        raw_dates = frame["report_date"].astype(str).str.replace("-", "", regex=False)
                        frame = frame[
                            (raw_dates >= start_date.replace("-", ""))
                            & (raw_dates <= end_date.replace("-", ""))
                        ]
                    if frame.empty:
                        continue
                    frames.append(frame)
                    try:
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        temporary = cache_path.with_suffix(".json.tmp")
                        temporary.write_text(
                            json.dumps(frame.to_dict(orient="records"), ensure_ascii=False, default=str),
                            encoding="utf-8",
                        )
                        temporary.replace(cache_path)
                    except OSError:
                        pass
        if not frames:
            return empty_frame(CONSENSUS_ESTIMATE_COLUMNS)
        frame = pd.concat(frames, ignore_index=True)
        result = pd.DataFrame(index=frame.index)
        result["symbol"] = pick_column(frame, ["ts_code"], "").astype(str)
        result["company_name"] = pick_column(frame, ["name"], "").astype(str)
        result["report_date"] = pick_column(frame, ["report_date"], "").astype(str)
        result["report_title"] = pick_column(frame, ["report_title"], "").astype(str)
        result["report_type"] = pick_column(frame, ["report_type", "classify"], "").astype(str)
        result["org_name"] = pick_column(frame, ["org_name"], "").astype(str)
        result["author_name"] = pick_column(frame, ["author_name"], "").astype(str)
        result["forecast_period"] = pick_column(frame, ["quarter"], "").astype(str)
        for output, source in (
            ("revenue_forecast_yuan", "op_rt"),
            ("operating_profit_forecast_yuan", "op_pr"),
            ("total_profit_forecast_yuan", "tp"),
            ("net_profit_forecast_yuan", "np"),
        ):
            result[output] = to_numeric_series(pick_column(frame, [source])) * 10_000
        result["eps_forecast"] = to_numeric_series(pick_column(frame, ["eps"]))
        result["pe_forecast"] = to_numeric_series(pick_column(frame, ["pe"]))
        result["roe_forecast"] = to_numeric_series(pick_column(frame, ["roe"])) / 100.0
        result["provider"] = self.name
        result["updated_at"] = now_text()
        return ensure_columns(result, CONSENSUS_ESTIMATE_COLUMNS)

    def get_finance_snapshot(
        self,
        symbols: list[str] | None = None,
        as_of: str | None = None,
        periods: int = 12,
    ) -> pd.DataFrame:
        """Merge statement and indicator rows by report period without future leakage."""
        if not self.is_available() or not symbols:
            return empty_frame(FINANCE_SNAPSHOT_COLUMNS)
        cutoff = (as_of or date.today().isoformat()).replace("-", "")[:8]
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            code = _to_ts_code(symbol)
            frames: dict[str, pd.DataFrame] = {}
            for name, method_name, fields in (
                ("income", "income", "ts_code,ann_date,end_date,revenue,n_income_attr_p"),
                ("cashflow", "cashflow", "ts_code,ann_date,end_date,n_cashflow_act,n_incr_cash_cash_equ,c_cash_equ_beg_period,c_cash_equ_end_period"),
                ("balance", "balancesheet", "ts_code,ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int,total_hldr_eqy_inc_min_int,minority_int,total_liab_hldr_eqy,money_cap,st_borr,lt_borr,bond_payable"),
                ("indicator", "fina_indicator", "ts_code,ann_date,end_date,grossprofit_margin,netprofit_margin,roe_waa,roic,debt_to_assets"),
            ):
                try:
                    frame = getattr(_ts_pro, method_name)(
                        ts_code=code,
                        end_date=cutoff,
                        fields=fields,
                    )
                except Exception as exc:
                    logger.warning("Tushare %s(%s) failed: %s", method_name, code, type(exc).__name__)
                    continue
                if frame is None or frame.empty:
                    continue
                frame = frame.copy()
                frame["ann_date"] = frame["ann_date"].astype(str)
                frame = frame[frame["ann_date"] <= cutoff]
                frame = frame.sort_values("ann_date").drop_duplicates("end_date", keep="last")
                frames[name] = frame

            periods_found = sorted(
                {str(value) for frame in frames.values() for value in frame.get("end_date", [])},
                reverse=True,
            )[: max(1, int(periods))]
            for period in periods_found:
                output: dict[str, Any] = {
                    "symbol": code,
                    "period": period,
                    "provider": self.name,
                    "updated_at": now_text(),
                }
                visible_dates: list[str] = []
                for name, frame in frames.items():
                    matched = frame[frame["end_date"].astype(str) == period]
                    if matched.empty:
                        continue
                    item = matched.iloc[-1]
                    visible_dates.append(str(item.get("ann_date") or ""))
                    if name == "income":
                        output["revenue"] = item.get("revenue")
                        output["net_profit"] = item.get("n_income_attr_p")
                    elif name == "cashflow":
                        output["operating_cash_flow"] = item.get("n_cashflow_act")
                        for target, source in (
                            ("cash_change", "n_incr_cash_cash_equ"),
                            ("cash_and_equivalents_begin", "c_cash_equ_beg_period"),
                            ("cash_and_equivalents_end", "c_cash_equ_end_period"),
                        ):
                            value = pd.to_numeric(pd.Series([item.get(source)]), errors="coerce").iloc[0]
                            if pd.notna(value):
                                output[target] = float(value)
                    elif name == "balance":
                        for target, source in (
                            ("total_assets", "total_assets"),
                            ("total_liabilities", "total_liab"),
                            ("total_equity", "total_hldr_eqy_inc_min_int"),
                            ("minority_interest", "minority_int"),
                            ("cash_and_equivalents", "money_cap"),
                        ):
                            value = pd.to_numeric(pd.Series([item.get(source)]), errors="coerce").iloc[0]
                            if pd.notna(value):
                                output[target] = float(value)
                        if "total_equity" not in output:
                            fallback_equity = pd.to_numeric(pd.Series([
                                item.get("total_hldr_eqy_exc_min_int")
                            ]), errors="coerce").iloc[0]
                            if pd.notna(fallback_equity):
                                output["total_equity"] = float(fallback_equity)
                        debt_values = pd.to_numeric(pd.Series([
                            item.get("st_borr"), item.get("lt_borr"), item.get("bond_payable"),
                        ]), errors="coerce").dropna()
                        if not debt_values.empty:
                            output["total_debt"] = float(debt_values.sum())
                    else:
                        for target, source in (
                            ("gross_margin", "grossprofit_margin"),
                            ("net_margin", "netprofit_margin"),
                            ("roe", "roe_waa"),
                            ("roic", "roic"),
                            ("debt_ratio", "debt_to_assets"),
                        ):
                            value = pd.to_numeric(pd.Series([item.get(source)]), errors="coerce").iloc[0]
                            if pd.notna(value):
                                output[target] = float(value) / 100.0
                output["published_at"] = max((value for value in visible_dates if value), default="")
                rows.append(output)
        if not rows:
            return empty_frame(FINANCE_SNAPSHOT_COLUMNS)
        return ensure_columns(pd.DataFrame(rows), FINANCE_SNAPSHOT_COLUMNS)

    def get_earnings_forecasts(
        self, start_date: str, end_date: str, symbols: list[str] | None = None
    ) -> pd.DataFrame:
        if not self.is_available():
            return empty_frame(EARNINGS_FORECAST_COLUMNS)
        try:
            frames = []
            if symbols:
                requests_to_make = [("symbol", symbol) for symbol in symbols]
            else:
                current = date.fromisoformat(start_date[:10])
                end = date.fromisoformat(end_date[:10])
                requests_to_make = []
                while current <= end:
                    requests_to_make.append(("date", current.strftime("%Y%m%d")))
                    current += timedelta(days=1)
            for query_type, value in requests_to_make:
                item = pd.DataFrame()
                for attempt in range(2):
                    try:
                        item = (
                            _ts_pro.forecast(
                                ts_code=_to_ts_code(value),
                                start_date=start_date.replace("-", ""),
                                end_date=end_date.replace("-", ""),
                            )
                            if query_type == "symbol"
                            else _ts_pro.forecast(ann_date=value)
                        )
                        break
                    except Exception as exc:
                        logger.warning(
                            "Tushare forecast failed %s=%s attempt=%s error=%s",
                            query_type,
                            value,
                            attempt + 1,
                            type(exc).__name__,
                        )
                if item is not None and not item.empty:
                    frames.append(item)
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        except Exception as exc:
            logger.warning("Tushare get_earnings_forecasts failed: %s", type(exc).__name__)
            return empty_frame(EARNINGS_FORECAST_COLUMNS)
        if frame is None or frame.empty:
            return empty_frame(EARNINGS_FORECAST_COLUMNS)
        result = pd.DataFrame(index=frame.index)
        result["symbol"] = pick_column(frame, ["ts_code", "symbol"], "").astype(str)
        if symbols:
            wanted = {re.sub(r"\D", "", str(symbol)) for symbol in symbols}
            result = result[result["symbol"].str.replace(r"\D", "", regex=True).isin(wanted)]
            frame = frame.loc[result.index]
        result["announcement_date"] = pick_column(frame, ["ann_date"], "").astype(str)
        result["period_end"] = pick_column(frame, ["end_date"], "").astype(str)
        result["forecast_type"] = pick_column(frame, ["type"], "").astype(str)
        result["change_min_pct"] = to_numeric_series(pick_column(frame, ["p_change_min"]))
        result["change_max_pct"] = to_numeric_series(pick_column(frame, ["p_change_max"]))
        result["net_profit_min_wan"] = to_numeric_series(pick_column(frame, ["net_profit_min"]))
        result["net_profit_max_wan"] = to_numeric_series(pick_column(frame, ["net_profit_max"]))
        result["summary"] = pick_column(frame, ["summary"], "").astype(str)
        result["change_reason"] = pick_column(frame, ["change_reason"], "").astype(str)
        result["provider"] = self.name
        result["updated_at"] = now_text()
        return ensure_columns(result, EARNINGS_FORECAST_COLUMNS)

    # ----------------------------------------------------------
    # 行情接口
    # ----------------------------------------------------------

    def get_realtime_quotes(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """Tushare 不提供实时行情，返回空 DataFrame。

        Args:
            symbols: 股票代码列表

        Returns:
            空 DataFrame
        """
        return empty_frame(MARKET_SNAPSHOT_COLUMNS)

    def get_daily_bars(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """通过 pro.daily() 获取日K线。

        Args:
            start_date: 起始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 DAILY_BARS_COLUMNS
        """
        if not self.is_available():
            return empty_frame(DAILY_BARS_COLUMNS)

        try:
            start = start_date.replace("-", "")
            end = end_date.replace("-", "")

            if symbols:
                all_rows: list[pd.DataFrame] = []
                for sym in symbols:
                    ts_code = _to_ts_code(sym)
                    try:
                        df = _ts_pro.daily(ts_code=ts_code, start_date=start, end_date=end)
                        if df is not None and not df.empty:
                            all_rows.append(self._transform_daily(df))
                    except Exception as exc:
                        logger.warning("Tushare daily(%s) 失败: %s", ts_code, exc)
                        continue

                if not all_rows:
                    return empty_frame(DAILY_BARS_COLUMNS)
                return pd.concat(all_rows, ignore_index=True)
            else:
                # 全市场查询（注意数据量）
                df = _ts_pro.daily(start_date=start, end_date=end)
                if df is None or df.empty:
                    return empty_frame(DAILY_BARS_COLUMNS)
                return self._transform_daily(df)
        except Exception as exc:
            logger.warning("Tushare get_daily_bars 失败: %s", exc)
            return empty_frame(DAILY_BARS_COLUMNS)

    def _transform_daily(self, df: pd.DataFrame) -> pd.DataFrame:
        """将 Tushare daily 返回的 DataFrame 转换为标准格式。

        Args:
            df: Tushare 原始 DataFrame

        Returns:
            标准格式的 DataFrame
        """
        result = pd.DataFrame(index=df.index)
        result["trade_date"] = df["trade_date"].astype(str).str[:4] + "-" + df["trade_date"].astype(str).str[4:6] + "-" + df["trade_date"].astype(str).str[6:8]
        result["symbol"] = df["ts_code"].astype(str).str.replace(r"\.(SH|SZ)$", "", regex=True)
        result["stock_name"] = ""
        result["open"] = to_numeric_series(df["open"])
        result["high"] = to_numeric_series(df["high"])
        result["low"] = to_numeric_series(df["low"])
        result["close"] = to_numeric_series(df["close"])
        result["pre_close"] = to_numeric_series(df["pre_close"])
        result["pct_chg"] = to_numeric_series(df["pct_chg"])
        result["amount"] = to_numeric_series(df["amount"]) * 1000  # Tushare 单位为千元
        result["volume"] = to_numeric_series(df["vol"])
        result["turnover_rate"] = pd.NA
        result["volume_ratio"] = pd.NA
        result["total_market_cap"] = pd.NA
        result["float_market_cap"] = pd.NA
        result["provider"] = self.name
        result["updated_at"] = now_text()
        return ensure_columns(result, DAILY_BARS_COLUMNS)

    # ----------------------------------------------------------
    # 基础信息
    # ----------------------------------------------------------

    def get_stock_basic_info(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """通过 pro.stock_basic() 获取股票基础信息。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 STOCK_BASIC_INFO_COLUMNS
        """
        if not self.is_available():
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

        try:
            df = _ts_pro.stock_basic(
                exchange="",
                list_status="L",
                fields="ts_code,symbol,name,area,industry,list_date,market,business_scope",
            )
            if df is None or df.empty:
                return empty_frame(STOCK_BASIC_INFO_COLUMNS)

            result = pd.DataFrame(index=df.index)
            result["symbol"] = df["symbol"].astype(str)
            result["stock_name"] = df["name"].astype(str)
            result["industry"] = df["industry"].astype(str)
            result["area"] = df["area"].astype(str)
            result["list_date"] = df["list_date"].astype(str)
            result["business_scope"] = df["business_scope"].astype(str) if "business_scope" in df.columns else ""
            result["main_business"] = ""
            result["provider"] = self.name
            result["updated_at"] = now_text()

            if symbols:
                symbol_set = set(normalize_symbols(symbols))
                result = result[result["symbol"].isin(symbol_set)]

            return ensure_columns(result, STOCK_BASIC_INFO_COLUMNS)
        except Exception as exc:
            logger.warning("Tushare get_stock_basic_info 失败: %s", exc)
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

    def get_stock_membership_history(
        self,
        statuses: tuple[str, ...] = ("L", "D", "P"),
    ) -> pd.DataFrame:
        """获取上市、退市和暂停上市公司的候选成员快照。

        Tushare ``stock_basic.list_status`` 官方取值为 L/D/P/G。本方法默认
        查询 L/D/P，保留 ``list_status`` 和 ``delist_date``。名称、行业和状态
        是当前快照，不能单独证明历史时点状态；PIT 认证还必须使用有效时间
        区间和当时可见的公司画像证据。
        """
        columns = [
            "ts_code", "symbol", "current_stock_name", "current_industry", "area", "list_date",
            "delist_date", "list_status", "business_scope", "provider",
            "updated_at",
        ]
        if not self.is_available():
            return pd.DataFrame(columns=columns)
        frames: list[pd.DataFrame] = []
        try:
            for status in statuses:
                if status not in {"L", "D", "P", "G"}:
                    raise ValueError(f"不支持的 list_status: {status}")
                raw = _ts_pro.stock_basic(
                    exchange="",
                    list_status=status,
                    fields=(
                        "ts_code,symbol,name,area,industry,list_date,"
                        "delist_date,list_status,business_scope"
                    ),
                )
                if raw is None or raw.empty:
                    continue
                result = pd.DataFrame(index=raw.index)
                result["ts_code"] = raw["ts_code"].astype(str)
                result["symbol"] = raw["symbol"].astype(str)
                result["current_stock_name"] = raw["name"].astype(str)
                result["current_industry"] = raw["industry"].fillna("").astype(str)
                result["area"] = raw["area"].fillna("").astype(str)
                result["list_date"] = raw["list_date"].fillna("").astype(str)
                result["delist_date"] = raw["delist_date"].fillna("").astype(str)
                result["list_status"] = status
                result["business_scope"] = raw.get("business_scope", "")
                result["provider"] = self.name
                result["updated_at"] = now_text()
                frames.append(result)
        except Exception as exc:
            logger.warning("Tushare get_stock_membership_history 失败: %s", exc)
            return pd.DataFrame(columns=columns)
        if not frames:
            return pd.DataFrame(columns=columns)
        return (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["symbol", "list_status"], keep="last")
            .reindex(columns=columns)
        )

    # ----------------------------------------------------------
    # 公告
    # ----------------------------------------------------------

    def get_announcements(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        """通过 pro.anns_d() 获取公告。

        Args:
            code: 股票代码
            count: 返回条数

        Returns:
            公告字典列表
        """
        if not self.is_available():
            return []

        try:
            ts_code = _to_ts_code(code)
            df = _ts_pro.anns_d(ts_code=ts_code, limit=count)
            if df is None or df.empty:
                return []
            return df.to_dict("records")
        except Exception as exc:
            logger.warning("Tushare get_announcements 失败: %s", exc)
            return []
