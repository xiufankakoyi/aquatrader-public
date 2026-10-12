"""LanceDB 管理接口兼容实现。"""

from typing import Any, Dict

from data_svc.storage.lancedb_reader import LanceDBDataReader


class LanceDBManager:
    """兼容 LanceDB 管理语义的本地数据健康检查器。"""

    def __init__(self):
        self.reader = LanceDBDataReader()

    def check_health(self) -> Dict[str, Any]:
        """检查本地数据是否可用。"""
        try:
            symbols = self.reader.get_symbols("daily_ohlcv")
            sample = symbols[0] if symbols else None
            df = None
            if sample:
                df = self.reader.get_daily(sample)
            return {
                "status": "ok",
                "symbols_count": len(symbols),
                "sample_symbol": sample,
                "sample_count": len(df) if df is not None else 0,
            }
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    def list_tables(self) -> list[str]:
        """返回支持的表名列表。"""
        return ["daily_ohlcv", "index_daily", "stock_info"]
