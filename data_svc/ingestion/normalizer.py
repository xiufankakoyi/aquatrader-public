"""多源数据归一化。"""

from datetime import datetime

import pandas as pd


class Normalizer:
    """将不同数据源的字段统一为标准 schema。"""

    STANDARD_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "source"]

    def normalize_daily(self, raw_df: pd.DataFrame, symbol: str, source: str) -> pd.DataFrame:
        """
        归一化日线行情数据。

        Args:
            raw_df: 原始数据框。
            symbol: 股票代码。
            source: 数据源名称。

        Returns:
            归一化后的数据框。
        """
        if raw_df is None or raw_df.empty:
            return pd.DataFrame(columns=self.STANDARD_COLUMNS)

        df = raw_df.copy()

        rename_map = {
            "trade_date": "date",
            "trade_time": "date",
            "stock_code": "symbol",
            "code": "symbol",
            "ts_code": "symbol",
            "vol": "volume",
            "volume": "volume",
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
        }

        for src_col, dst_col in rename_map.items():
            if src_col in df.columns and dst_col not in df.columns:
                df.rename(columns={src_col: dst_col}, inplace=True)

        if "symbol" not in df.columns:
            df["symbol"] = symbol

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["source"] = source

        keep_cols = [c for c in self.STANDARD_COLUMNS if c in df.columns]
        return df[keep_cols].copy()

    def normalize(self, events: list[dict], source: str = "gateway") -> list[dict]:
        """
        将原始事件列表归一化为标准事件结构。

        Args:
            events: 原始事件字典列表。
            source: 来源标识。

        Returns:
            归一化后的事件列表。
        """
        results: list[dict] = []
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            normalized = dict(ev)
            normalized.setdefault("source", source)
            normalized.setdefault("normalized_at", datetime.now().isoformat())
            results.append(normalized)
        return results
