from __future__ import annotations

from datetime import datetime
from typing import Any
import re

import pandas as pd
import requests

from core.us_options.config import ScanConfig


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(result):
        return None
    return result


class YFinanceOptionProvider:
    data_source = "yfinance"

    def __init__(self) -> None:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("yfinance is not installed") from exc
        self._yf = yf

    def fetch_symbol(self, symbol: str, config: ScanConfig) -> tuple[list[dict[str, Any]], str | None]:
        try:
            ticker = self._yf.Ticker(symbol)
            underlying_price, price_error = self._get_underlying_price(ticker)
            if underlying_price is None:
                return [], price_error or "missing underlying price"
            expiries = self._select_expiries(list(getattr(ticker, "options", []) or []), config)
            if not expiries:
                return [], "no expiries in configured DTE range"

            rows: list[dict[str, Any]] = []
            for expiry, dte in expiries:
                try:
                    chain = ticker.option_chain(expiry)
                except Exception as exc:  # noqa: BLE001
                    rows.append(
                        {
                            "symbol": symbol,
                            "expiry": expiry,
                            "fetch_error": f"option_chain failed: {exc}",
                            "data_source": self.data_source,
                        }
                    )
                    continue
                rows.extend(self._frame_to_rows(chain.calls, symbol, underlying_price, expiry, dte, "call"))
                rows.extend(self._frame_to_rows(chain.puts, symbol, underlying_price, expiry, dte, "put"))
            return rows, None
        except Exception as exc:  # noqa: BLE001
            return [], str(exc)

    def _get_underlying_price(self, ticker: Any) -> tuple[float | None, str | None]:
        fast_info = getattr(ticker, "fast_info", None)
        for key in ("last_price", "lastPrice", "regular_market_price"):
            if fast_info is not None:
                try:
                    price = _safe_float(fast_info.get(key))
                except Exception as exc:  # noqa: BLE001
                    price = None
                if price and price > 0:
                    return price, None
        try:
            history = ticker.history(period="5d", auto_adjust=False)
        except Exception as exc:  # noqa: BLE001
            return None, f"underlying history failed: {exc}"
        if history is None or history.empty or "Close" not in history:
            return None, "missing underlying price"
        return _safe_float(history["Close"].dropna().iloc[-1]), None

    def _select_expiries(self, expiries: list[str], config: ScanConfig) -> list[tuple[str, int]]:
        today = datetime.now().date()
        selected: list[tuple[str, int]] = []
        for expiry in expiries:
            try:
                expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (expiry_date - today).days
            if config.min_dte <= dte <= config.max_dte:
                selected.append((expiry, dte))
        return selected

    def _frame_to_rows(
        self,
        frame: pd.DataFrame,
        symbol: str,
        underlying_price: float,
        expiry: str,
        dte: int,
        option_type: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if frame is None or frame.empty:
            return rows
        for _, row in frame.iterrows():
            rows.append(
                {
                    "symbol": symbol,
                    "underlying_price": underlying_price,
                    "option_type": option_type,
                    "strategy": "long_call" if option_type == "call" else "long_put",
                    "side": "buy_to_open",
                    "expiry": expiry,
                    "strike": _safe_float(row.get("strike")),
                    "bid": _safe_float(row.get("bid")),
                    "ask": _safe_float(row.get("ask")),
                    "last_price": _safe_float(row.get("lastPrice")),
                    "volume": _safe_float(row.get("volume")),
                    "openInterest": _safe_float(row.get("openInterest")),
                    "implied_volatility": _safe_float(row.get("impliedVolatility")),
                    "delta": _safe_float(row.get("delta")),
                    "gamma": _safe_float(row.get("gamma")),
                    "theta": _safe_float(row.get("theta")),
                    "DTE": dte,
                    "contract_symbol": row.get("contractSymbol"),
                    "data_source": self.data_source,
                    "data_quality": "delayed_or_unverified",
                    "manual_check_required": True,
                }
            )
        return rows


class CboeDelayedOptionProvider:
    data_source = "cboe_delayed"
    endpoint = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"

    def fetch_symbol(self, symbol: str, config: ScanConfig) -> tuple[list[dict[str, Any]], str | None]:
        try:
            response = requests.get(
                self.endpoint.format(symbol=symbol.upper()),
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0 AquaTrade option scanner"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return [], f"cboe request failed: {exc}"

        data = payload.get("data") or {}
        underlying_price = _safe_float(data.get("current_price") or data.get("close") or data.get("prev_day_close"))
        if underlying_price is None or underlying_price <= 0:
            return [], "cboe missing underlying price"

        options = data.get("options") or []
        if not options:
            return [], "cboe returned empty option chain"

        today = datetime.now().date()
        rows: list[dict[str, Any]] = []
        for item in options:
            parsed = parse_occ_option_symbol(str(item.get("option") or ""))
            if not parsed:
                continue
            expiry_date = parsed["expiry_date"]
            dte = (expiry_date - today).days
            if not (config.min_dte <= dte <= config.max_dte):
                continue
            rows.append(
                {
                    "symbol": symbol.upper(),
                    "underlying_price": underlying_price,
                    "option_type": parsed["option_type"],
                    "strategy": "long_call" if parsed["option_type"] == "call" else "long_put",
                    "side": "buy_to_open",
                    "expiry": parsed["expiry"],
                    "strike": parsed["strike"],
                    "bid": _safe_float(item.get("bid")),
                    "ask": _safe_float(item.get("ask")),
                    "last_price": _safe_float(item.get("last_trade_price")),
                    "volume": _safe_float(item.get("volume")),
                    "openInterest": _safe_float(item.get("open_interest")),
                    "implied_volatility": _safe_float(item.get("iv")),
                    "delta": _safe_float(item.get("delta")),
                    "gamma": _safe_float(item.get("gamma")),
                    "theta": _safe_float(item.get("theta")),
                    "DTE": dte,
                    "contract_symbol": item.get("option"),
                    "last_trade_time": item.get("last_trade_time"),
                    "quote_timestamp": payload.get("timestamp"),
                    "data_source": self.data_source,
                    "data_quality": "delayed_or_unverified",
                    "manual_check_required": True,
                }
            )
        return rows, None if rows else "cboe had no expiries in configured DTE range"


def parse_occ_option_symbol(option_symbol: str) -> dict[str, Any] | None:
    match = re.match(r"^(?P<root>[A-Z]+)(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$", option_symbol)
    if not match:
        return None
    raw_date = match.group("date")
    year = 2000 + int(raw_date[:2])
    month = int(raw_date[2:4])
    day = int(raw_date[4:6])
    try:
        expiry_date = datetime(year, month, day).date()
    except ValueError:
        return None
    option_type = "call" if match.group("cp") == "C" else "put"
    strike = int(match.group("strike")) / 1000.0
    return {
        "root": match.group("root"),
        "expiry": expiry_date.isoformat(),
        "expiry_date": expiry_date,
        "option_type": option_type,
        "strike": strike,
    }
