#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""screener 模块前后端导入不匹配修复的专项测试。

覆盖三类问题：
1. POST /api/screener/field_stats（前端调用，后端原本 404）
2. POST /api/screener/export（前端调用，后端原本 404）
3. 前端 store 字段兼容逻辑（items || records || []）

测试均通过 monkeypatch mock LanceDB / 数据服务，不依赖真实数据。
"""
from __future__ import annotations

import sys
import types
from unittest.mock import patch, MagicMock

import polars as pl
import pytest
from flask import Flask

import server.routes.screener_routes as screener_routes
from server.routes.screener_routes import screener_bp


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(autouse=True)
def _clear_query_cache():
    """每个测试前清理 screener_routes._query_cache，避免跨测试缓存命中。"""
    screener_routes._query_cache._store.clear()
    yield
    screener_routes._query_cache._store.clear()


@pytest.fixture
def app() -> Flask:
    """构造仅注册 screener 蓝图的 Flask 应用，避免加载全部路由。"""
    flask_app = Flask(__name__)
    flask_app.register_blueprint(screener_bp)
    return flask_app


@pytest.fixture
def client(app: Flask):
    """Flask 测试客户端。"""
    return app.test_client()


@pytest.fixture
def mock_df() -> pl.DataFrame:
    """构造 3 行的 mock 日线 DataFrame，用于 field_stats / export 测试。"""
    return pl.DataFrame({
        'stock_code': ['000001', '000002', '000003'],
        'ts_code': ['000001.SZ', '000002.SZ', '000003.SZ'],
        'trade_date': ['2026-01-01', '2026-01-01', '2026-01-01'],
        'stock_name': ['平安银行', '万科A', '中国平安'],
        'close': [10.0, 20.0, 30.0],
        'volume': [1000, 2000, 3000],
        'pe': [15.0, 25.0, 35.0],
        'total_mv': [1e5, 2e5, 3e5],
    })


@pytest.fixture
def mock_service(mock_df: pl.DataFrame) -> MagicMock:
    """Mock ScreenerDataService，apply_filter_optimized 默认原样返回。"""
    service = MagicMock()
    service.get_data.return_value = mock_df
    service.apply_filter_optimized.return_value = mock_df
    return service


def _patch_screener_service(mock_service: MagicMock):
    """patch 函数内 lazy import 的 get_screener_service。

    screener_routes.py 在 endpoint 函数内通过
    `from server.routes.screener_data_service import get_screener_service` 引入。
    由于真实 screener_data_service 模块存在 pre-existing import bug
    （get_lancedb_reader 函数缺失），无法直接 import，
    因此通过 sys.modules 注入 mock 模块实现 patch。
    """
    mock_module = types.ModuleType('server.routes.screener_data_service')
    mock_module.get_screener_service = lambda: mock_service
    return patch.dict(sys.modules, {
        'server.routes.screener_data_service': mock_module,
    })


# ============================================================
# 1. TestFieldStatsEndpoint
# ============================================================

class TestFieldStatsEndpoint:
    """POST /api/screener/field_stats 端点测试。"""

    def test_missing_field_returns_400_invalid(self, client):
        """缺 field 返回 400 INVALID（不再默认 close）。"""
        response = client.post('/api/screener/field_stats', json={})
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert data['error']['code'] == 'SCREENER_INVALID_PARAMS'
        assert 'field' in data['error']['message']

    def test_valid_field_returns_200_with_stats(self, client, mock_df):
        """有效 field 返回 200 + stats dict（含 min/max/mean/median/std/q25/q75/count）。"""
        with patch.object(screener_routes, 'get_all_stocks_daily_df', return_value=mock_df), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/field_stats', json={'field': 'close'})

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        stats = data['data']
        assert stats['field'] == 'close'
        assert stats['date'] == '2026-01-01'
        assert stats['count'] == 3
        assert stats['min'] == 10.0
        assert stats['max'] == 30.0
        # 完整字段集
        for key in ('min', 'max', 'mean', 'median', 'std', 'q25', 'q75', 'count'):
            assert key in stats, f'stats 缺字段: {key}'

    def test_missing_date_degrades_to_latest(self, client, mock_df):
        """缺 date 自动降级到 latest_trade_date，不抛异常。"""
        with patch.object(screener_routes, 'get_all_stocks_daily_df', return_value=mock_df) as mock_get, \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/field_stats', json={'field': 'close'})

        assert response.status_code == 200
        data = response.get_json()
        assert data['data']['date'] == '2026-01-01'
        # 验证 get_all_stocks_daily_df 收到降级后的 date
        assert mock_get.called
        _, kwargs = mock_get.call_args
        assert kwargs.get('target_date') == '2026-01-01'

    def test_response_format_matches_get_stats(self, client, mock_df):
        """响应格式与 GET /stats 一致（success/data/meta/error 四顶层字段）。"""
        with patch.object(screener_routes, 'get_all_stocks_daily_df', return_value=mock_df), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/field_stats', json={'field': 'close'})

        data = response.get_json()
        # 顶层字段与 /stats 完全一致
        assert set(data.keys()) == {'success', 'data', 'meta', 'error'}
        assert data['success'] is True
        assert data['error'] is None
        # meta 必含字段
        assert 'elapsed_ms' in data['meta']
        assert 'query_plan' in data['meta']
        assert 'cache_hit' in data['meta']
        assert 'fields_pruned' in data['meta']
        assert 'warnings' in data['meta']

    def test_field_not_in_df_returns_400_invalid(self, client, mock_df):
        """字段不在 DataFrame 列中返回 400 INVALID。"""
        with patch.object(screener_routes, 'get_all_stocks_daily_df', return_value=mock_df), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post(
                '/api/screener/field_stats', json={'field': 'nonexistent_field'},
            )

        assert response.status_code == 400
        data = response.get_json()
        assert data['error']['code'] == 'SCREENER_INVALID_PARAMS'

    def test_no_data_for_date_returns_404_missing(self, client):
        """该日期无数据返回 404 MISSING。"""
        empty_df = pl.DataFrame()
        with patch.object(screener_routes, 'get_all_stocks_daily_df', return_value=empty_df), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post(
                '/api/screener/field_stats', json={'field': 'close'},
            )

        assert response.status_code == 404
        data = response.get_json()
        assert data['error']['code'] == 'SCREENER_DATA_MISSING'

    def test_error_response_has_trace_id_like_meta(self, client):
        """错误响应包含 endpoint 字段（trace_id 约定位置在 meta.endpoint）。"""
        response = client.post('/api/screener/field_stats', json={})
        data = response.get_json()
        # meta.endpoint 用于排障，与 trace_id 同等地位
        assert data['meta'].get('endpoint') == '/api/screener/field_stats'


# ============================================================
# 2. TestExportEndpoint
# ============================================================

class TestExportEndpoint:
    """POST /api/screener/export 端点测试。"""

    def test_valid_filter_returns_csv_with_disposition(self, client, mock_service):
        """POST 有效 filter 请求返回 200 + text/csv + Content-Disposition。"""
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/export', json={
                'conditions': [],
                'logic': 'AND',
            })

        assert response.status_code == 200
        assert 'text/csv' in response.content_type
        assert 'Content-Disposition' in response.headers
        assert 'screener_export_2026-01-01.csv' in response.headers['Content-Disposition']

    def test_csv_first_line_is_header(self, client, mock_service):
        """CSV 第一行是表头。"""
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/export', json={'conditions': []})

        first_line = response.data.decode('utf-8').split('\n', 1)[0]
        headers = first_line.split(',')
        # mock_df 至少包含 stock_code / close 等字段
        assert 'stock_code' in headers
        assert 'close' in headers

    def test_missing_date_degrades_to_latest(self, client, mock_service):
        """POST 缺 date 自动降级到 latest_trade_date。"""
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/export', json={'conditions': []})

        assert response.status_code == 200
        # 验证 service.get_data 调用时 date 参数为降级后的 latest_trade_date
        args, kwargs = mock_service.get_data.call_args
        assert kwargs.get('date') == '2026-01-01'

    def test_no_matching_data_returns_404_missing_json(self, client, mock_service):
        """POST 无匹配数据返回 404 MISSING（JSON 格式，不返回 CSV）。"""
        empty_df = pl.DataFrame({
            'stock_code': [], 'trade_date': [], 'close': [],
        })
        mock_service.apply_filter_optimized.return_value = empty_df
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/export', json={
                'conditions': [{'field': 'close', 'operator': '>', 'value': 1000000}],
            })

        assert response.status_code == 404
        # 必须是 JSON 错误响应，不是 CSV
        assert 'application/json' in response.content_type
        data = response.get_json()
        assert data['success'] is False
        assert data['error']['code'] == 'SCREENER_DATA_MISSING'

    def test_field_not_exist_returns_400_invalid_json(self, client, mock_service):
        """POST 字段不存在返回 400 INVALID（JSON 格式）。"""
        mock_service.apply_filter_optimized.side_effect = ValueError(
            "filter fields not available in loaded data: ['nonexistent_field']",
        )
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/export', json={
                'conditions': [{'field': 'nonexistent_field', 'operator': '>', 'value': 10}],
            })

        assert response.status_code == 400
        assert 'application/json' in response.content_type
        data = response.get_json()
        assert data['success'] is False
        assert data['error']['code'] == 'SCREENER_INVALID_PARAMS'

    def test_max_rows_truncation_with_warning(self, client, mock_service):
        """导出最大 10000 行限制：超过时截断并通过 X-Warning 提示。"""
        # 构造 15000 行 DataFrame（超过 EXPORT_MAX_ROWS=10000）
        big_df = pl.DataFrame({
            'stock_code': [f'{i:06d}' for i in range(15000)],
            'trade_date': ['2026-01-01'] * 15000,
            'close': [float(i) for i in range(15000)],
        })
        mock_service.apply_filter_optimized.return_value = big_df
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/export', json={'conditions': []})

        assert response.status_code == 200
        # X-Warning 头部存在（HTTP header 大小写不敏感，Flask 会保留原样）
        warning_header = response.headers.get('X-Warning') or response.headers.get('X-warning')
        assert warning_header is not None
        assert '10000' in warning_header
        # CSV 行数（除表头外）应该 <= 10000
        lines = response.data.decode('utf-8').rstrip('\n').split('\n')
        assert len(lines) - 1 <= 10000  # 减去表头

    def test_invalid_logic_returns_400(self, client, mock_service):
        """logic 参数非法返回 400 INVALID。"""
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/export', json={
                'conditions': [],
                'logic': 'INVALID',
            })

        assert response.status_code == 400
        data = response.get_json()
        assert data['error']['code'] == 'SCREENER_INVALID_PARAMS'

    def test_no_data_for_date_returns_404(self, client, mock_service):
        """该日期无数据返回 404 MISSING。"""
        mock_service.get_data.return_value = None
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/export', json={'conditions': []})

        assert response.status_code == 404
        data = response.get_json()
        assert data['error']['code'] == 'SCREENER_DATA_MISSING'


# ============================================================
# 3. TestStoreFieldCompatibility
# ============================================================

class TestStoreFieldCompatibility:
    """前端 store 字段兼容逻辑测试。

    验证 `res.data.data.items || res.data.data.records || []` 的 fallback：
    - 优先取 items（当前后端命名）
    - items 为空时降级到 records（兼容旧调用方）
    - 两者都无时返回空数组
    """

    @staticmethod
    def _frontend_fallback(payload_data: dict) -> list:
        """模拟前端 `items || records || []` 兼容逻辑（Python 等价实现）。"""
        items = payload_data.get('items')
        if items:
            return items
        records = payload_data.get('records')
        if records:
            return records
        return []

    def test_items_takes_priority(self):
        """后端返回 items 时优先使用。"""
        payload = {'items': [{'a': 1}], 'records': [{'b': 2}]}
        result = self._frontend_fallback(payload)
        assert result == [{'a': 1}]

    def test_records_fallback_when_items_missing(self):
        """items 不存在时降级到 records（兼容旧调用方）。"""
        payload = {'records': [{'b': 2}]}
        result = self._frontend_fallback(payload)
        assert result == [{'b': 2}]

    def test_records_fallback_when_items_empty(self):
        """items 为空数组时降级到 records。"""
        payload = {'items': [], 'records': [{'b': 2}]}
        result = self._frontend_fallback(payload)
        assert result == [{'b': 2}]

    def test_both_missing_returns_empty(self):
        """两者都无时返回空数组，避免 undefined 报错。"""
        payload = {}
        result = self._frontend_fallback(payload)
        assert result == []

    def test_both_empty_returns_empty(self):
        """两者都为空数组时返回空数组。"""
        payload = {'items': [], 'records': []}
        result = self._frontend_fallback(payload)
        assert result == []

    def test_backend_filter_endpoint_returns_items(self, client, mock_service):
        """端到端验证：后端 /filter 实际返回 items 字段，前端 fallback 可正确取到。"""
        with _patch_screener_service(mock_service), \
             patch.object(screener_routes, 'get_latest_trade_date', return_value='2026-01-01'):
            response = client.post('/api/screener/filter', json={
                'conditions': [],
                'logic': 'AND',
                'page': 1,
                'page_size': 50,
            })

        assert response.status_code == 200
        data = response.get_json()
        # 后端确实返回 items
        assert 'items' in data['data']
        # 前端 fallback 可正确取到
        records = self._frontend_fallback(data['data'])
        assert len(records) == 3  # mock_df 3 行
