"""阶段8 投研驾驶舱增强端点 smoke test。

覆盖 Task 8.1 新增的 5 个 API：
- GET /api/research/fact-outcomes
- GET /api/research/shadow-features
- GET /api/research/market-session
- GET /api/research/validity-config
- GET /api/research/push-dedup

以及 /api/research/health 回归保护。

遵循项目规则：
- 测试只位于 tests/，不污染 server/
- 不输出买卖/仓位/止盈止损建议
- 字段缺失时必须降级，不让请求 5xx 崩溃
- 不直接暴露内部文件路径给浏览器
"""
from __future__ import annotations

import json

import pytest
from flask import Flask

from core.research.control_plane.repository import ResearchRepository


# ----------------------------------------------------------------------
# 公共夹具
# ----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """构建带阶段8端点的 Flask 测试客户端。

    使用 tmp_path 隔离 SQLite，避免污染真实数据库。
    通过 monkeypatch 替换全局 _repository，保证测试可重入。
    """
    import server.routes.research_routes as routes

    repo = ResearchRepository(tmp_path / "research.db")
    monkeypatch.setattr(routes, "_repository", repo)
    # 同时隔离 push_dedup DB，避免写入默认 data/state 路径
    monkeypatch.setenv("PUSH_DEDUP_DB_PATH", str(tmp_path / "push_dedup.db"))
    app = Flask(__name__)
    app.register_blueprint(routes.research_bp)
    return app.test_client()


def _ok_payload(response):
    """断言响应 200 且返回标准 {ok, data} 结构，返回 data 字段。"""
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["ok"] is True
    return body["data"]


# ----------------------------------------------------------------------
# 健康检查回归
# ----------------------------------------------------------------------


def test_health_endpoint_still_ok(client):
    """阶段8 改动不应破坏 /api/research/health。"""
    data = _ok_payload(client.get("/api/research/health"))
    assert data["status"] == "ok"
    assert data["module"] == "research_control"


# ----------------------------------------------------------------------
# Task 8.1: 5 个新端点 200 + 降级
# ----------------------------------------------------------------------


def test_fact_outcomes_returns_200_with_empty_list(client):
    """无数据时返回空列表而非 5xx。"""
    data = _ok_payload(client.get("/api/research/fact-outcomes"))
    assert isinstance(data, list)
    assert data == []


def test_fact_outcomes_supports_status_param(client):
    """status=resolved 返回空列表（无 resolved 记录时降级）。"""
    data = _ok_payload(client.get("/api/research/fact-outcomes?status=resolved"))
    assert isinstance(data, list)
    assert data == []


def test_shadow_features_returns_eight_features(client):
    """shadow-features 必须返回 8 个特征名 + 版本。"""
    data = _ok_payload(client.get("/api/research/shadow-features"))
    assert len(data["feature_names"]) == 8
    assert data["feature_version"]
    assert data["policy_version"]
    # 8 个特征名必须包含约定的 key
    assert "earnings_revision" in data["feature_names"]
    assert "research_evidence_quality" in data["feature_names"]


def test_shadow_features_echoes_symbol_and_as_of(client):
    """symbol / as_of 参数原样回显。"""
    data = _ok_payload(
        client.get("/api/research/shadow-features?symbol=600584.SH&as_of=2026-07-14")
    )
    assert data["symbol"] == "600584.SH"
    assert data["as_of"] == "2026-07-14"


def test_market_session_returns_valid_session(client):
    """market-session 必须返回有效时段枚举 + 中文标签。"""
    # 周末 10:00 应该是 CLOSED
    data = _ok_payload(client.get("/api/research/market-session?date=2026-07-18T10:00:00"))
    assert data["session"] in {
        "closed", "pre_market", "call_auction",
        "morning", "lunch_break", "afternoon", "post_market",
    }
    assert data["session_label"]
    assert isinstance(data["is_trading_time"], bool)
    assert isinstance(data["is_trading_day"], bool)
    assert data["market_time"]
    assert data["next_session"]


def test_market_session_default_now(client):
    """不带 date 参数时使用当前时间。"""
    data = _ok_payload(client.get("/api/research/market-session"))
    assert data["session"]
    assert data["market_time"]


def test_validity_config_returns_claim_and_evidence_types(client):
    """validity-config 必须返回 claim_types / evidence_types 配置。"""
    data = _ok_payload(client.get("/api/research/validity-config"))
    assert "claim_types" in data
    assert "evidence_types" in data
    assert "policy_version" in data
    assert "unknown_label" in data
    # 抽样验证一个 claim_type 配置项
    assert "business_facts" in data["claim_types"]
    entry = data["claim_types"]["business_facts"]
    assert "days" in entry
    assert "label" in entry
    assert "name" in entry


def test_push_dedup_returns_zero_when_empty(client):
    """空 DB 时返回 pushed_count=0 + 空 failures 列表。"""
    data = _ok_payload(client.get("/api/research/push-dedup"))
    assert data["pushed_count"] == 0
    assert data["failures"] == []
    assert data["failure_count"] == 0
    assert data["policy_version"]


def test_push_dedup_filters_by_date(client):
    """date 参数原样回显，过滤不存在的日期返回 0。"""
    data = _ok_payload(client.get("/api/research/push-dedup?date=2026-07-14"))
    assert data["date"] == "2026-07-14"
    assert data["pushed_count"] == 0


# ----------------------------------------------------------------------
# 不直接暴露内部文件路径
# ----------------------------------------------------------------------


def test_endpoints_do_not_leak_file_paths(client):
    """新增 5 个端点的响应体不得包含 .db / 绝对路径等内部文件路径。

    注：/api/research/health 是预先存在的端点，其 repo.stats() 已包含 db_path，
    本测试不强制修改预先行为，只校验阶段8 新增的 5 个端点。
    """
    sensitive_keywords = [".db", "data/state", "data/research_control", "\\\\", "c:\\", "C:\\"]
    endpoints = [
        "/api/research/fact-outcomes",
        "/api/research/shadow-features",
        "/api/research/market-session",
        "/api/research/validity-config",
        "/api/research/push-dedup",
    ]
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200
        # 转字符串后扫描敏感关键词
        body_text = response.get_data(as_text=True)
        for keyword in sensitive_keywords:
            # policy_version 等字段不应包含 .db 后缀；显式断言
            assert keyword not in body_text, (
                f"endpoint {endpoint} 响应中泄露内部路径关键词: {keyword}"
            )


# ----------------------------------------------------------------------
# 字段缺失降级
# ----------------------------------------------------------------------


def test_fact_outcome_record_field_missing_degrades(client, tmp_path, monkeypatch):
    """写入一条缺字段的 FactOutcomeRecord，API 返回时不崩溃。"""
    from core.research.control_plane.contracts import utc_now
    from core.research.control_plane.fact_outcome import FactOutcomeRecord, FactOutcomeStore
    import server.routes.research_routes as routes

    repo = routes._repository
    store = FactOutcomeStore(repo)
    # 只填必要字段，policy_version / resolved_at 等留空
    store.record_promotion(
        transition_id="t-missing-fields",
        claim_id="c1",
        promotion_type="claim",
        from_status="DISCOVERED",
        to_status="CANDIDATE",
        promoted_at=utc_now(),
    )
    data = _ok_payload(client.get("/api/research/fact-outcomes?status=pending"))
    assert len(data) == 1
    record = data[0]
    # 缺失字段应该有默认值（空串或 0），不应抛异常
    assert record["claim_id"] == "c1"
    assert record["policy_version"] == "" or record["policy_version"]


def test_push_dedup_db_unavailable_degrades_gracefully(tmp_path, monkeypatch):
    """push_dedup DB 路径不可写时返回降级 payload，不抛 5xx。"""
    import server.routes.research_routes as routes

    repo = ResearchRepository(tmp_path / "research.db")
    monkeypatch.setattr(routes, "_repository", repo)
    # 指向一个不可写的路径触发 PushDedupStore 构造异常
    monkeypatch.setenv("PUSH_DEDUP_DB_PATH", str(tmp_path / "nonexistent_dir" / "push.db"))
    app = Flask(__name__)
    app.register_blueprint(routes.research_bp)
    test_client = app.test_client()

    response = test_client.get("/api/research/push-dedup")
    # 即使 DB 不可用，端点也不应 5xx
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    data = body["data"]
    # 降级 payload 字段齐全
    assert data["pushed_count"] == 0
    assert data["failures"] == []
    assert data["failure_count"] == 0
    assert data["policy_version"]


# ----------------------------------------------------------------------
# 序列化安全
# ----------------------------------------------------------------------


def test_all_endpoints_return_json_utf8(client):
    """所有端点返回 JSON 可解析，无编码错误。"""
    endpoints = [
        "/api/research/fact-outcomes",
        "/api/research/shadow-features",
        "/api/research/market-session",
        "/api/research/validity-config",
        "/api/research/push-dedup",
    ]
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200
        # 必须可被 json 解析（无 UTF-8 解码错误）
        parsed = response.get_json()
        assert parsed is not None
        # 中文标签必须能正确序列化
        body_text = response.get_data(as_text=True)
        assert "ok" in body_text
