"""Point-in-time market feature enrichment for dynamic themes."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .contracts import ThemePacket


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class ThemeMarketFeatureEnricher:
    """Compute theme breadth, trend, reversal and crowding from local bars.

    All input rows are truncated at ``as_of``.  Missing or malformed market
    data leaves the packet degraded/UNKNOWN instead of manufacturing a score.
    """

    VERSION = "theme_market_features_v1"

    def __init__(self, data_query: Any | None = None, lookback_calendar_days: int = 90) -> None:
        if data_query is None:
            from data_svc.unified_data_query import UnifiedDataQuery

            data_query = UnifiedDataQuery()
        self._query = data_query
        self._lookback_days = max(45, int(lookback_calendar_days))

    def enrich_many(self, packets: list[ThemePacket], *, as_of: str) -> list[ThemePacket]:
        symbols = sorted({member.symbol for packet in packets for member in packet.members if member.symbol})
        if not symbols:
            return packets
        end = self._date_part(as_of)
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=self._lookback_days)).strftime("%Y-%m-%d")
        try:
            frame = self._query._read_polars(
                None, start, end,
                columns=["trade_date", "stock_code", "close", "volume", "amount"],
                table_name="daily_ohlcv",
            ).to_pandas()
        except Exception:
            frame = pd.DataFrame()
        if frame.empty or "stock_code" not in frame.columns:
            for packet in packets:
                packet.degraded = True
                packet.phase = packet.phase or "UNKNOWN"
                packet.degradation_reasons.append("theme_market_history_missing")
            return packets

        frame["stock_code"] = frame["stock_code"].astype(str)
        frame = frame[frame["stock_code"].isin(symbols)].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame["amount"] = pd.to_numeric(frame.get("amount"), errors="coerce")
        frame["volume"] = pd.to_numeric(frame.get("volume"), errors="coerce")
        frame = frame.dropna(subset=["trade_date", "close"]).sort_values(["stock_code", "trade_date"])
        market_trade_date = (
            frame["trade_date"].max().strftime("%Y-%m-%d") if not frame.empty else ""
        )
        features = self._symbol_features(frame)
        for packet in packets:
            self._enrich_packet(packet, features, market_trade_date=market_trade_date)
        return packets

    @staticmethod
    def _date_part(value: str) -> str:
        text = str(value or "")[:10]
        datetime.strptime(text, "%Y-%m-%d")
        return text

    @staticmethod
    def _symbol_features(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for symbol, rows in frame.groupby("stock_code"):
            closes = rows["close"].astype(float).to_numpy()
            if len(closes) < 21 or closes[-1] <= 0:
                continue
            current = float(closes[-1])

            def ma(days: int) -> float:
                return float(np.mean(closes[-min(days, len(closes)):]))

            def ret(days: int) -> float:
                if len(closes) <= days or closes[-days - 1] <= 0:
                    return 0.0
                return current / float(closes[-days - 1]) - 1.0

            ma20 = ma(20)
            ma5 = ma(5)
            ma10 = ma(10)
            ma30 = ma(30)
            ret5 = ret(5)
            ret20 = ret(20)
            distance_ma20 = current / ma20 - 1.0 if ma20 else 0.0
            # MA5 斜率：当前 MA5 与 5 日前 MA5 的相对变化。
            # 用于排除"价格站上 MA5 但 MA5 横盘"的假突破——MA5 平台时
            # 不应判定为 TRENDING / MA5_HOLD 强势状态，降级为 NEUTRAL。
            ma5_slope = 0.0
            if len(closes) >= 10:
                ma5_prev = float(np.mean(closes[-10:-5]))
                if ma5_prev > 0:
                    ma5_slope = (ma5 - ma5_prev) / ma5_prev
            # MA5 斜率阈值：5 日内 MA5 相对变化 ≥ 0.3% 视为上行，
            # 低于此阈值视为横盘，不参与 TRENDING / MA5_HOLD 判定。
            ma5_rising = ma5_slope >= 0.003
            if ret5 >= 0.12 or distance_ma20 >= 0.15:
                technical_state = "ACCELERATION_RISK"
            elif abs(current / ma5 - 1.0) <= 0.02 and ma5 >= ma10 >= ma20 and ma5_rising:
                technical_state = "MA5_HOLD"
            elif abs(current / ma10 - 1.0) <= 0.025 and current >= ma20:
                technical_state = "MA10_RETEST"
            elif abs(current / ma20 - 1.0) <= 0.03:
                technical_state = "MA20_RETEST"
            elif abs(current / ma30 - 1.0) <= 0.035:
                technical_state = "MA30_RETEST"
            elif ret20 <= -0.05 and ret5 >= 0.02 and current >= ma5:
                technical_state = "REVERSAL_CONFIRMING"
            elif current >= ma5 >= ma10 >= ma20 and ret20 > 0 and ma5_rising:
                technical_state = "TRENDING"
            elif current < ma20 and ret20 < 0:
                technical_state = "TREND_BROKEN"
            else:
                technical_state = "NEUTRAL"
            recent_amount = pd.to_numeric(rows["amount"], errors="coerce").tail(20)
            average_amount_20d = float(recent_amount.mean()) if recent_amount.notna().any() else 0.0
            output[str(symbol)] = {
                "close": current,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20,
                "ma30": ma30,
                "ret5": ret5,
                "ret20": ret20,
                "ma5_slope": round(ma5_slope, 4),
                "distance_ma5": current / ma5 - 1.0 if ma5 else 0.0,
                "distance_ma10": current / ma10 - 1.0 if ma10 else 0.0,
                "distance_ma20": distance_ma20,
                "distance_ma30": current / ma30 - 1.0 if ma30 else 0.0,
                "above_ma20": 1.0 if current >= ma20 else 0.0,
                "average_amount_20d": average_amount_20d,
                "technical_state": technical_state,
                "market_trade_date": rows["trade_date"].iloc[-1].strftime("%Y-%m-%d"),
            }
        return output

    def _enrich_packet(
        self,
        packet: ThemePacket,
        features: dict[str, dict[str, Any]],
        *,
        market_trade_date: str,
    ) -> None:
        member_rows = [
            (member, features[member.symbol])
            for member in packet.members if member.symbol in features
        ]
        rows = [row for _, row in member_rows]
        self._attach_member_features(member_rows)
        coverage = len(rows) / len(packet.members) if packet.members else 0.0
        if len(rows) < 2 or coverage < 0.25:
            packet.degraded = True
            packet.phase = packet.phase or "UNKNOWN"
            packet.degradation_reasons.append("theme_market_feature_coverage_insufficient")
            packet.market_features = {
                "coverage": round(coverage, 4),
                "sample_size": len(rows),
                "market_trade_date": market_trade_date,
            }
            return

        ret5 = float(np.median([row["ret5"] for row in rows]))
        ret20 = float(np.median([row["ret20"] for row in rows]))
        breadth = float(np.mean([row["above_ma20"] for row in rows]))
        extended = float(np.mean([
            1.0 if row["ret5"] >= 0.12 or row["distance_ma20"] >= 0.15 else 0.0
            for row in rows
        ]))
        oversold = _clamp((-ret20 - 0.03) / 0.15)
        turn = _clamp((ret5 + 0.02) / 0.08)
        reversal = _clamp(0.45 * oversold + 0.30 * turn + 0.25 * breadth)
        trend = _clamp(0.50 * _clamp((ret20 + 0.05) / 0.20) + 0.50 * breadth)
        crowding = _clamp(0.65 * extended + 0.35 * _clamp((ret5 - 0.05) / 0.15))

        packet.breadth_score = round(breadth, 4)
        packet.reversal_score = round(reversal, 4)
        packet.persistence_score = round(trend, 4)
        packet.crowding_score = round(crowding, 4)
        packet.market_features = {
            "coverage": round(coverage, 4),
            "sample_size": len(rows),
            "median_return_5d": round(ret5, 4),
            "median_return_20d": round(ret20, 4),
            "above_ma20_ratio": round(breadth, 4),
            "extended_ratio": round(extended, 4),
            "feature_version": self.VERSION,
            "market_trade_date": market_trade_date,
        }
        if crowding >= 0.70:
            packet.phase = "ACCELERATION_RISK"
            packet.phase_confidence = crowding
        elif reversal >= 0.65 and ret5 > 0:
            packet.phase = "REVERSAL_CANDIDATE"
            packet.phase_confidence = reversal
        elif trend >= 0.65 and crowding < 0.55:
            packet.phase = "STARTUP"
            packet.phase_confidence = trend
        elif oversold >= 0.70:
            packet.phase = "ICE"
            packet.phase_confidence = oversold
        else:
            packet.phase = packet.phase or "UNKNOWN"

    @staticmethod
    def _attach_member_features(
        member_rows: list[tuple[Any, dict[str, Any]]],
    ) -> None:
        """Attach auditable member technical states and relative leadership."""
        if not member_rows:
            return

        def percentile(values: list[float], value: float) -> float:
            if len(values) <= 1:
                return 0.5
            return sum(1 for item in values if item <= value) / len(values)

        ret5_values = [float(row.get("ret5") or 0.0) for _, row in member_rows]
        ret20_values = [float(row.get("ret20") or 0.0) for _, row in member_rows]
        amount_values = [float(row.get("average_amount_20d") or 0.0) for _, row in member_rows]
        scored: list[tuple[float, Any, dict[str, Any]]] = []
        for member, row in member_rows:
            trend_alignment = 1.0 if (
                float(row.get("close") or 0.0) >= float(row.get("ma10") or 0.0)
                >= float(row.get("ma20") or 0.0)
            ) else 0.0
            leadership = _clamp(
                0.30 * percentile(ret20_values, float(row.get("ret20") or 0.0))
                + 0.20 * percentile(ret5_values, float(row.get("ret5") or 0.0))
                + 0.25 * percentile(amount_values, float(row.get("average_amount_20d") or 0.0))
                + 0.25 * trend_alignment
            )
            scored.append((leadership, member, row))
        scored.sort(key=lambda item: (-item[0], str(item[1].symbol)))
        leader_symbols = {
            member.symbol for leadership, member, _ in scored[:3]
            if leadership >= 0.55
        }
        for leadership, member, row in scored:
            member.market_features = {
                key: round(value, 4) if isinstance(value, float) else value
                for key, value in row.items()
            }
            member.market_features["leadership_score"] = round(leadership, 4)
            member.market_features["market_role_candidate"] = (
                "leader_candidate" if member.symbol in leader_symbols else "member"
            )
