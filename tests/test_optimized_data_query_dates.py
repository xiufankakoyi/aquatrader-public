"""交易日缓存的边界行为与访问复杂度契约。"""

from data_svc.database.optimized_data_query import OptimizedStockDataQuery


class _IndexedDateCache(list):
    """只允许二分查找需要的长度/下标访问，拒绝线性扫描。"""

    def __contains__(self, _value):
        raise AssertionError("date cache must use indexed lookup")

    def index(self, _value, *_args):
        raise AssertionError("date cache must not call list.index")

    def __iter__(self):
        raise AssertionError("date cache must not iterate linearly")


def _query_with_dates(dates, parquet_dir):
    query = OptimizedStockDataQuery(warmup=False)
    query._cache = {}
    query._cache_size = 16
    query._all_trading_dates_cache = _IndexedDateCache(dates)
    query.parquet_dir = str(parquet_dir)
    return query


def test_previous_and_next_trade_date_use_sorted_cache_without_linear_scan(tmp_path):
    query = _query_with_dates(
        ["2026-01-05", "2026-01-06", "2026-01-08", "2026-01-09"],
        tmp_path / "missing",
    )

    assert query.get_prev_trade_date("2026-01-07") == "2026-01-06"
    assert query.get_prev_trade_date("2026-01-08") == "2026-01-06"
    assert query.get_next_trade_date("2026-01-07") == "2026-01-08"
    assert query.get_next_trade_date("2026-01-08") == "2026-01-09"
