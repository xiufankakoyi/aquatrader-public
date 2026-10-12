"""P3.7 机构级研究工作台 API 测试。

覆盖 server/routes/research_model_routes.py 中的 9 个工作台只读端点：
- GET /api/research/models/workbench/overview
- GET /api/research/models/workbench/discovery-funnel
- GET /api/research/models/workbench/company-cards
- GET /api/research/models/workbench/profit-models
- GET /api/research/models/workbench/variant-perception
- GET /api/research/models/workbench/bear-questions
- GET /api/research/models/workbench/risk-calendar
- GET /api/research/models/workbench/data-health
- GET /api/research/models/workbench/scorecard

验收点：
- 每个端点返回 200 + success=true
- 每份响应包含 as_of / model_version / source / limitations 四字段
- 字段缺失时返回 degraded=true + 空数据结构，绝不返回 500
- 不输出买入/卖出/仓位/止盈/止损等交易动作措辞
- P3.1 模型治理端点也可访问（gate / list_models 等）
"""
from __future__ import annotations

import pytest
from flask import Flask

from server.routes.research_model_routes import research_model_bp


# ---------------------------------------------------------------------------
# 禁止的交易动作关键词（来自 AGENTS.md / P3 边界）
# ---------------------------------------------------------------------------
FORBIDDEN_TRADE_ACTION_KEYWORDS = (
    "买入",
    "卖出",
    "仓位",
    "止盈",
    "止损",
    "加仓",
    "减仓",
    "做多",
    "做空",
    "long",
    "short",
)


@pytest.fixture
def app() -> Flask:
    """构造测试用 Flask 应用，仅注册 research_model_bp。"""
    test_app = Flask(__name__)
    test_app.register_blueprint(research_model_bp)
    return test_app


@pytest.fixture
def client(app: Flask):
    """构造测试客户端。"""
    return app.test_client()


# ---------------------------------------------------------------------------
# 工作台只读端点：必须 200 + 标准元数据
# ---------------------------------------------------------------------------

WORKBENCH_ENDPOINTS = [
    "/api/research/models/workbench/overview",
    "/api/research/models/workbench/discovery-funnel",
    "/api/research/models/workbench/company-cards",
    "/api/research/models/workbench/profit-models",
    "/api/research/models/workbench/variant-perception",
    "/api/research/models/workbench/bear-questions",
    "/api/research/models/workbench/risk-calendar",
    "/api/research/models/workbench/data-health",
    "/api/research/models/workbench/scorecard",
]


@pytest.mark.parametrize("endpoint", WORKBENCH_ENDPOINTS)
def test_workbench_endpoints_return_200_with_meta(client, endpoint):
    """每个工作台端点必须返回 200 + 标准元数据四字段。"""
    response = client.get(endpoint)
    assert response.status_code == 200, f"{endpoint} 返回非 200：{response.status_code}"
    payload = response.get_json()
    assert payload["success"] is True, f"{endpoint} success != true"
    data = payload["data"]
    assert isinstance(data, dict), f"{endpoint} data 必须是 dict"
    # 标准四字段
    assert "as_of" in data, f"{endpoint} 缺 as_of"
    assert "model_version" in data, f"{endpoint} 缺 model_version"
    assert "source" in data, f"{endpoint} 缺 source"
    assert "limitations" in data, f"{endpoint} 缺 limitations"
    # limitations 必须是 list
    assert isinstance(data["limitations"], list), f"{endpoint} limitations 必须是 list"
    # model_version 非空
    assert data["model_version"], f"{endpoint} model_version 不能为空"
    assert data["source"], f"{endpoint} source 不能为空"


@pytest.mark.parametrize("endpoint", WORKBENCH_ENDPOINTS)
def test_workbench_endpoints_no_trade_action(client, endpoint):
    """工作台端点响应不得包含交易动作措辞。"""
    response = client.get(endpoint)
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    for keyword in FORBIDDEN_TRADE_ACTION_KEYWORDS:
        # 注意： limitations 字段中可能描述「不输出交易动作」，所以只匹配
        # 跟实际建议相关的高风险关键词
        if keyword in ("long", "short"):
            # 大小写不敏感
            assert keyword.lower() not in text.lower(), (
                f"{endpoint} 响应中包含禁止词 {keyword}"
            )
        else:
            # 中文关键词
            # 允许 limitations / disclaimer 中描述「不输出交易动作措辞」
            # 所以只检查对应中文关键词不应出现在「建议性」上下文中
            # 这里简化校验：直接搜全文本
            assert keyword not in text, (
                f"{endpoint} 响应中包含禁止词 {keyword}"
            )


def test_discovery_funnel_returns_four_stages(client):
    """发现漏斗返回 4 阶段。"""
    response = client.get("/api/research/models/workbench/discovery-funnel")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert len(data["stages"]) == 4, "发现漏斗必须有 4 个阶段"
    stage_keys = [s["stage"] for s in data["stages"]]
    assert stage_keys == ["universe", "liquidity", "fundamental", "readiness"]
    assert data["total_passed"] == 0
    assert data["degraded"] is True


def test_company_cards_returns_valid_structure(client):
    """公司研究卡返回合法结构（cards/total/degraded 字段齐全）。

    仓储有数据时 degraded=False；仓储无数据时 degraded=True。
    不假设仓储数据是否存在（取决于运行环境的 research_value_runs 表）。
    """
    response = client.get("/api/research/models/workbench/company-cards")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "cards" in data
    assert "total" in data
    assert "degraded" in data
    assert isinstance(data["cards"], list)
    assert isinstance(data["total"], int)
    # 有数据时不降级；无数据时降级
    if data["cards"]:
        assert data["degraded"] is False
    else:
        assert data["degraded"] is True


def test_company_cards_supports_symbol_filter(client):
    """公司研究卡支持 symbol 过滤参数。"""
    response = client.get(
        "/api/research/models/workbench/company-cards",
        query_string={"symbol": "600519.SH"},
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["symbol_filter"] == "600519.SH"


def test_profit_models_returns_assumption_basis_fields(client):
    """利润模型返回 fact_basis + assumption_basis 字段。"""
    response = client.get("/api/research/models/workbench/profit-models")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "scenarios" in data
    assert "fact_basis" in data
    assert "assumption_basis" in data
    if data["scenarios"] or data["assumption_basis"]:
        assert data["degraded"] is False
    else:
        assert data["degraded"] is True


def test_variant_perception_returns_empty_perceptions(client):
    """VariantPerception 无数据时返回空数组 + degraded=true。"""
    response = client.get("/api/research/models/workbench/variant-perception")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["perceptions"] == []
    assert data["active_count"] == 0
    assert data["degraded"] is True


def test_bear_questions_returns_committee_status(client):
    """反方问题端点返回 committee_status 默认结构。"""
    response = client.get("/api/research/models/workbench/bear-questions")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "bear_questions" in data
    assert "committee_status" in data
    cs = data["committee_status"]
    assert cs["state"] == "not_convened"
    assert cs["pending_questions"] == 0
    assert isinstance(cs["members"], list)


def test_risk_calendar_returns_forward_validation_events(client):
    """风险日历接入 P3 前向 observations.json，返回验证事件。

    旧版返回降级空数组；新版读取真实前向验证日。
    文件不存在时降级为空数组 + degraded=True。
    """
    response = client.get("/api/research/models/workbench/risk-calendar")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "events" in data
    assert "collected_days" in data
    assert "target_days" in data
    assert data["target_days"] == 20
    # 每个事件必须 trade_action_free=True，不输出交易动作
    for event in data["events"]:
        assert event.get("trade_action_free") is True
        assert "买入" not in event.get("title", "")
        assert "卖出" not in event.get("title", "")


def test_data_health_returns_model_status_summary(client):
    """数据健康端点返回 model_status_summary 结构。"""
    response = client.get("/api/research/models/workbench/data-health")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "data_health" in data
    assert "model_status_summary" in data
    summary = data["model_status_summary"]
    assert "active_count" in summary
    assert "suspended_count" in summary
    assert "any_suspended" in summary


def test_scorecard_returns_summary_with_learning_loop(client):
    """记分卡接入 ProductionLearningLoop，返回结构化汇总。

    旧版返回降级空数组；新版读取真实学习账本。
    无样本时 coverage_rate 为 null；有样本时返回 hit/miss/pending 分类。
    """
    response = client.get("/api/research/models/workbench/scorecard")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "scorecard" in data
    summary = data["summary"]
    assert "total_predictions" in summary
    assert "hit_count" in summary
    assert "miss_count" in summary
    assert "pending_count" in summary
    assert "settled_count" in summary
    assert "coverage_rate" in summary
    # 无已结算样本时 coverage_rate 为 None；有样本时为 0-1 之间浮点数
    if summary["settled_count"] == 0:
        assert summary["coverage_rate"] is None
    else:
        assert 0.0 <= summary["coverage_rate"] <= 1.0
    # 每条记分卡记录不输出交易动作措辞
    for item in data["scorecard"]:
        for field in ("research_question", "symbol", "prediction_id"):
            text = str(item.get(field, ""))
            for banned in ("买入", "卖出", "仓位", "止盈", "止损"):
                assert banned not in text


def test_overview_returns_p3_gate_and_modules(client):
    """总览返回拆分后的 P3 门禁状态 + 模块降级状态列表。

    新版 P3 门禁拆成 4 个独立状态，纠正旧版"P3门禁=P2前置"的语义错位：
    - p3_gate_status.p2_prerequisite: P2 前置门禁
    - p3_gate_status.p3_engineering: P3 工程链路状态
    - p3_gate_status.p3_forward_progress: P3 前向取证进度
    - p3_gate_status.p3_live_proven: P3 正式认证
    """
    response = client.get("/api/research/models/workbench/overview")
    assert response.status_code == 200
    data = response.get_json()["data"]
    # 兼容旧字段 p3_gate
    assert "p3_gate" in data
    assert "is_blocked" in data["p3_gate"]
    # 新版拆分后的 p3_gate_status
    assert "p3_gate_status" in data
    gate_status = data["p3_gate_status"]
    assert "p2_prerequisite" in gate_status
    assert "p3_engineering" in gate_status
    assert "p3_forward_progress" in gate_status
    assert "p3_live_proven" in gate_status
    # 前向进度应有 collected_days / target_days 字段
    forward = gate_status["p3_forward_progress"]
    assert "collected_days" in forward
    assert "target_days" in forward
    assert forward["target_days"] == 20
    assert "model_registry_summary" in data
    assert "modules" in data
    assert len(data["modules"]) == 9, "工作台必须有 9 个模块（含候选生成漏斗）"
    module_keys = {m["key"] for m in data["modules"]}
    expected_keys = {
        "discovery_funnel",
        "company_cards",
        "profit_models",
        "variant_perception",
        "bear_questions",
        "risk_calendar",
        "data_health",
        "scorecard",
        "candidate_funnel",
    }
    assert module_keys == expected_keys, f"模块键不匹配：{module_keys ^ expected_keys}"


def test_candidate_funnel_endpoint_returns_diagnostics(client):
    """候选生成漏斗端点返回结构化诊断，不再只显示 0。"""
    response = client.get("/api/research/models/workbench/candidate-funnel")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "funnel" in data
    assert "available" in data
    funnel = data["funnel"]
    if data["available"]:
        # 有报告时应包含主题统计
        assert "total_themes" in funnel
        assert "total_eligible" in funnel
        assert "total_dropped" in funnel
        # 不输出交易动作措辞
        for field in ("run_id", "as_of", "status"):
            text = str(data.get(field, ""))
            for banned in ("买入", "卖出", "仓位", "止盈", "止损"):
                assert banned not in text


def test_company_cards_endpoint_returns_research_runs(client):
    """公司研究卡端点接入 ResearchRunRepository，返回研究运行索引。"""
    response = client.get("/api/research/models/workbench/company-cards")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "cards" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
    assert "symbol_filter" in data
    # 每张卡片不输出交易动作措辞
    for card in data["cards"]:
        for field in ("run_id", "symbol", "company_name"):
            text = str(card.get(field, ""))
            for banned in ("买入", "卖出", "仓位", "止盈", "止损"):
                assert banned not in text


# ---------------------------------------------------------------------------
# P3.1 模型治理端点（已被注册到 Flask 应用）
# ---------------------------------------------------------------------------

def test_p3_gate_endpoint_registered(client):
    """P3.1 gate 端点必须可访问。"""
    response = client.get("/api/research/models/gate")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    data = payload["data"]
    assert "status" in data
    assert "is_blocked" in data
    assert "p2_overall_passed" in data


def test_list_models_endpoint_registered(client):
    """P3.1 list_models 端点必须可访问。"""
    response = client.get("/api/research/models")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert isinstance(payload["data"], list)


# ---------------------------------------------------------------------------
# 路由优先级：/workbench/<feature> 必须优先于 /<model_id>/<version> 匹配
# ---------------------------------------------------------------------------

def test_workbench_route_does_not_clash_with_model_route(client):
    """工作台静态路径优先于动态 /<model_id>/<version> 匹配。

    访问 /api/research/models/workbench/discovery-funnel 不应被解析为
    model_id=workbench, version=discovery-funnel。
    """
    response = client.get("/api/research/models/workbench/discovery-funnel")
    assert response.status_code == 200
    data = response.get_json()["data"]
    # 工作台响应有 stages 字段；model 详情响应没有
    assert "stages" in data, "工作台路由优先级失效，被 /<model_id>/<version> 抢匹配"
