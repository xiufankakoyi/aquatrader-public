"""Smoke tests for the production research-monitor runtime."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from core.quant.theme.daily_screener import ThemeDailyScreener
from core.quant.theme.contracts import ThemeMember, ThemePacket
from core.quant.theme.market_feature_enricher import ThemeMarketFeatureEnricher
from core.research.control_plane.contracts import ResearchTask
from core.research.control_plane.repository import ResearchRepository
from core.runtime.research_monitor import ResearchMonitor, RuntimeResult
from infrastructure.data_providers.market.tencent_provider import _parse_tencent_fields
from infrastructure.data_providers.provider_registry import ProviderRegistry


def _write_premarket(path, *, as_of: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "latest_premarket.json").write_text(
        json.dumps({
            "run_id": "premarket_test",
            "as_of": as_of,
            "observations": [{
                "symbol": "600584.SH",
                "company_name": "长电科技",
                "theme_name": "先进封装",
                "status": "WATCH",
                "research_validated": True,
                "market_features": {"ma5": 100.0, "ma20": 90.0},
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _monitor(tmp_path) -> ResearchMonitor:
    return ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
    )


def test_flat_ma5_is_not_reported_as_trending() -> None:
    """价格刚站上横盘 MA5 时不能解释为上升趋势。"""
    closes = [100.0] * 20 + [99.0, 101.0, 99.0, 101.0, 100.5]
    frame = pd.DataFrame({
        "stock_code": ["600584.SH"] * len(closes),
        "trade_date": pd.date_range("2026-06-01", periods=len(closes), freq="B"),
        "close": closes,
        "amount": [1_000_000.0] * len(closes),
    })

    feature = ThemeMarketFeatureEnricher._symbol_features(frame)["600584.SH"]

    assert feature["ma5_slope"] < 0.003
    assert feature["technical_state"] != "TRENDING"


def test_push_blocks_payload_older_than_latest_market_data(tmp_path, monkeypatch) -> None:
    """报告行情日落后于本地最新日线时必须 fail-closed，不得推送。"""
    report = tmp_path / "report.md"
    report.write_text("# stale", encoding="utf-8")
    (tmp_path / "latest_success.json").write_text(
        json.dumps({"trade_date": "2026-07-15", "run_id": "premarket_test"}),
        encoding="utf-8",
    )
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(
            get_db_stats=lambda: {"latest_date": "2026-07-15"}
        ),
        push_dedup_store=SimpleNamespace(should_push=lambda **_: True),
    )
    notified: list[object] = []
    monkeypatch.setattr(
        "core.notifications.notification_service.get_notification_service_core",
        lambda: SimpleNamespace(notify=lambda payload: notified.append(payload) or True),
    )

    result = monitor._maybe_push(
        external_push=True,
        trade_date="2026-07-15",
        title="过期报告",
        summary="不应发出",
        report_path=report,
        payload={
            "run_id": "premarket_test",
            "status": "partial",
            "observations": [{
                "symbol": "600584.SH",
                "market_features": {"market_trade_date": "2026-07-14"},
            }],
        },
    )

    assert result == {
        "requested": True,
        "sent": False,
        "reason": "stale_market_data",
        "latest_market_date": "2026-07-15",
        "payload_market_date": "2026-07-14",
    }
    assert notified == []


def test_push_fails_closed_when_latest_market_date_is_unavailable(tmp_path, monkeypatch) -> None:
    report = tmp_path / "report.md"
    report.write_text("# freshness unavailable", encoding="utf-8")
    (tmp_path / "latest_success.json").write_text(
        json.dumps({"trade_date": "2026-07-15", "run_id": "premarket_test"}),
        encoding="utf-8",
    )

    def _raise_stats_error():
        raise RuntimeError("database unavailable")

    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(get_db_stats=_raise_stats_error),
        push_dedup_store=SimpleNamespace(should_push=lambda **_: True),
    )
    notified: list[object] = []
    monkeypatch.setattr(
        "core.notifications.notification_service.get_notification_service_core",
        lambda: SimpleNamespace(notify=lambda payload: notified.append(payload) or True),
    )

    result = monitor._maybe_push(
        external_push=True,
        trade_date="2026-07-15",
        title="freshness unavailable",
        summary="must not send",
        report_path=report,
        payload={
            "run_id": "premarket_test",
            "status": "partial",
            "observations": [{
                "symbol": "600584.SH",
                "market_features": {"market_trade_date": "2026-07-15"},
            }],
        },
    )

    assert result["sent"] is False
    assert result["reason"] == "market_freshness_unavailable"
    assert notified == []


def test_intraday_anomaly_push_uses_stable_entity_key(tmp_path, monkeypatch) -> None:
    report = tmp_path / "monitor.md"
    report.write_text("# 盘中异动", encoding="utf-8")
    (tmp_path / "latest_success.json").write_text(
        json.dumps({"trade_date": "2026-07-15", "run_id": "premarket_test"}),
        encoding="utf-8",
    )
    dedup_calls: list[dict] = []
    dedup_store = SimpleNamespace(
        should_push=lambda **kwargs: dedup_calls.append(kwargs) or True,
        record_push=lambda **_: None,
        record_failure=lambda **_: None,
    )
    notified: list[object] = []
    monkeypatch.setattr(
        "core.notifications.notification_service.get_notification_service_core",
        lambda: SimpleNamespace(notify=lambda payload: notified.append(payload) or True),
    )
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
        push_dedup_store=dedup_store,
    )

    result = monitor._maybe_push(
        external_push=True,
        trade_date="2026-07-15",
        title="盘中异动",
        summary="检测到 1 个异动",
        report_path=report,
        payload={
            "mode": "monitor",
            "run_id": "monitor_random_run_id",
            "status": "success",
            "anomaly_hypotheses": [{
                "symbol": "600584",
                "anomaly_type": "large_drop",
            }],
        },
    )

    assert result["sent"] is True
    assert len(notified) == 1
    assert dedup_calls[0]["entity_id"] == "600584:large_drop"


def test_tencent_parser_preserves_exchange_snapshot_timestamp() -> None:
    fields = [""] * 46
    fields[1] = "长电科技"
    fields[2] = "600584"
    fields[3] = "101.11"
    fields[4] = "100.00"
    fields[5] = "100.20"
    fields[6] = "1000"
    fields[7] = "2000"
    fields[30] = "20260713093501"
    fields[32] = "1.11"
    fields[33] = "102.00"
    fields[34] = "99.50"
    fields[38] = "1.2"
    fields[39] = "25.0"
    fields[44] = "900.0"
    fields[45] = "1000.0"

    parsed = _parse_tencent_fields(fields)

    assert parsed is not None
    assert parsed["market_timestamp"] == "20260713093501"


def test_monitor_marks_previous_day_quote_stale(tmp_path, monkeypatch) -> None:
    _write_premarket(tmp_path, as_of="2026-07-13T08:20:00+08:00")
    monkeypatch.setattr(
        ProviderRegistry,
        "get_realtime_quotes",
        lambda self, symbols: pd.DataFrame([{
            "symbol": "600584",
            "stock_name": "长电科技",
            "price": 101.11,
            "pct_chg": 1.0,
            "provider": "tencent",
            "market_timestamp": "20260710150000",
        }]),
    )

    result = _monitor(tmp_path).run_monitor(
        as_of="2026-07-13T09:35:00+08:00", external_push=False
    )

    assert result.status == "partial"
    assert result.payload["stale_quote_count"] == 1
    assert result.payload["snapshots"][0]["monitor_state"] == "STALE_QUOTE"
    assert result.payload["snapshots"][0]["quote_fresh"] is False


def test_monitor_does_not_push_without_price_anomaly(tmp_path, monkeypatch) -> None:
    _write_premarket(tmp_path, as_of="2026-07-13T08:20:00+08:00")
    (tmp_path / "latest_success.json").write_text(
        json.dumps({"trade_date": "2026-07-13", "run_id": "premarket_test"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ProviderRegistry,
        "get_realtime_quotes",
        lambda self, symbols: pd.DataFrame([{
            "symbol": "600584", "stock_name": "长电科技", "price": 101.0,
            "pct_chg": 1.0, "provider": "tencent",
            "market_timestamp": "20260713093500",
        }]),
    )
    notified: list[object] = []
    monkeypatch.setattr(
        "core.notifications.notification_service.get_notification_service_core",
        lambda: SimpleNamespace(notify=lambda payload: notified.append(payload) or True),
    )

    result = _monitor(tmp_path).run_monitor(
        as_of="2026-07-13T09:35:00+08:00", external_push=True
    )

    assert result.payload["push"]["reason"] == "no_alert_condition"
    assert notified == []


def test_monitor_rejects_previous_trade_date_premarket(tmp_path) -> None:
    _write_premarket(tmp_path, as_of="2026-07-10T08:20:00+08:00")

    result = _monitor(tmp_path).run_monitor(
        as_of="2026-07-13T09:35:00+08:00", external_push=False
    )

    assert result.status == "failed"
    assert "not from the current trade date" in result.errors[0]


def test_monitor_preserves_multiple_theme_links_for_same_security(tmp_path, monkeypatch) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "latest_premarket.json").write_text(json.dumps({
        "run_id": "premarket_test",
        "as_of": "2026-07-13T08:20:00+08:00",
        "observations": [
            {
                "symbol": "000021.SZ", "theme_name": "先进封装",
                "status": "WATCH", "research_validated": True,
                "claim_ids": ["claim_packaging"],
                "market_features": {"ma5": 55.0, "ma20": 50.0},
            },
            {
                "symbol": "000021.SZ", "theme_name": "新能源汽车",
                "status": "WATCH", "research_validated": True,
                "claim_ids": ["claim_ev"],
                "market_features": {"ma5": 55.0, "ma20": 50.0},
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        ProviderRegistry,
        "get_realtime_quotes",
        lambda self, symbols: pd.DataFrame([{
            "symbol": "000021", "stock_name": "深科技", "price": 56.0,
            "pct_chg": 1.0, "provider": "tencent",
            "market_timestamp": "20260713093500",
        }]),
    )

    result = _monitor(tmp_path).run_monitor(
        as_of="2026-07-13T09:35:00+08:00", external_push=False
    )

    snapshot = result.payload["snapshots"][0]
    assert snapshot["theme_names"] == ["先进封装", "新能源汽车"]
    assert snapshot["claim_ids"] == ["claim_packaging", "claim_ev"]
    assert snapshot["research_validated"] is True


def test_daily_screener_distinguishes_ma5_and_ma30_states() -> None:
    screener = ThemeDailyScreener()
    rising = pd.DataFrame({
        "close": [80.0 + index * 0.5 for index in range(30)],
        "amount": [1e8] * 30,
    })
    ma30_retest = pd.DataFrame({
        "close": [50.0] * 10 + [100.0] * 19 + [84.0],
        "amount": [1e8] * 30,
    })

    assert screener._compute_technical_scores(rising)[1] == "MA5_HOLD"
    assert screener._compute_technical_scores(ma30_retest)[1] == "MA30_RETEST"
    descending_input = rising.assign(
        trade_date=pd.date_range("2026-06-01", periods=30).astype(str)
    ).iloc[::-1]
    assert screener._compute_technical_scores(descending_input)[1] == "MA5_HOLD"


def test_observations_never_truncate_research_validated_core(tmp_path) -> None:
    core = ThemeMember(
        symbol="600584.SH",
        company_name="长电科技",
        market_role="leader",
        research_claim_ids=["claim_600584"],
        research_claim_status="ACTIVE",
        mapping_confidence=1.0,
        market_features={"leadership_score": 0.01, "technical_state": "MA10_RETEST"},
    )
    exploration = [
        ThemeMember(
            symbol=f"00000{index}.SZ",
            market_features={"leadership_score": 1.0 - index * 0.01},
        )
        for index in range(1, 8)
    ]
    packet = ThemePacket(
        theme_id="advanced_packaging",
        theme_name="先进封装",
        members=[*exploration, ThemeMember(symbol=core.symbol)],
        core_members=[core],
    )

    rows = _monitor(tmp_path)._build_observations(
        [{"enhanced": packet, "summary": {"research_gate_status": "validated"}}],
        SimpleNamespace(theme_screening_results=[]),
    )

    core_row = next(row for row in rows if row["symbol"] == "600584.SH")
    assert len(rows) == 6
    assert core_row["research_validated"] is True
    assert core_row["company_name"] == "长电科技"
    assert core_row["claim_ids"] == ["claim_600584"]
    assert core_row["claim_status"] == "ACTIVE"


def test_premarket_critical_path_ignores_stale_theme_backlog(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    stale = ResearchTask(
        task_id="stale_task",
        status="queued",
        params={"workflow": "dynamic_theme_review", "theme_id": "old_theme"},
    )
    repository.save_task(stale)
    monitor = ResearchMonitor(
        output_dir=tmp_path / "reports",
        research_repository=repository,
        data_query=SimpleNamespace(),
    )
    errors = []

    runs = monitor._execute_research_tasks(
        as_of="2026-07-13T08:20:00+08:00",
        blocked_theme_ids=set(),
        active_theme_ids={"current_theme"},
        active_theme_priorities={"current_theme": 1.0},
        max_tasks=3,
        errors=errors,
    )

    assert runs == []
    assert errors == []
    assert repository.get_task("stale_task").status == "queued"


def test_monitor_recovers_stale_dynamic_tasks_before_quote_processing(tmp_path, monkeypatch):
    _write_premarket(tmp_path, as_of="2026-07-13T08:20:00+08:00")
    monkeypatch.setattr(
        ProviderRegistry,
        "get_realtime_quotes",
        lambda self, symbols: pd.DataFrame([{
            "symbol": "600584",
            "stock_name": "闀跨數绉戞妧",
            "price": 101.11,
            "pct_chg": 1.0,
            "provider": "tencent",
            "market_timestamp": "20260713093501",
        }]),
    )

    task = ResearchTask(
        task_id="stale-monitor-task",
        status="running",
        params={"workflow": "dynamic_theme_review"},
    )

    class Resumer:
        def __init__(self):
            self.resumed = []

        def list_resumable_tasks(self):
            return [task]

        def resume_task(self, item):
            self.resumed.append(item.task_id)
            return item

    resumer = Resumer()
    runtime = SimpleNamespace(task_resumer=resumer)
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
        runtime_factory=lambda **kwargs: runtime,
    )

    result = monitor.run_monitor(
        as_of="2026-07-13T09:35:00+08:00", external_push=False
    )

    assert resumer.resumed == ["stale-monitor-task"]
    assert result.payload["research_recovery"]["recovered_tasks"] == 1


def test_close_recovers_stale_dynamic_tasks_before_writing_summary(tmp_path):
    task = ResearchTask(
        task_id="stale-close-task",
        status="partial",
        params={"workflow": "dynamic_theme_review"},
    )

    class Resumer:
        def __init__(self):
            self.resumed = []

        def list_resumable_tasks(self):
            return [task]

        def resume_task(self, item):
            self.resumed.append(item.task_id)
            return item

    resumer = Resumer()
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
        runtime_factory=lambda **kwargs: SimpleNamespace(task_resumer=resumer),
    )

    result = monitor.run_close(
        as_of="2026-07-13T15:30:00+08:00", external_push=False
    )

    assert resumer.resumed == ["stale-close-task"]
    assert result.payload["research_recovery"]["recovered_tasks"] == 1


def _write_close_history(path) -> None:
    target = path / "2026-07-15"
    target.mkdir(parents=True, exist_ok=True)
    snapshots = [
        {
            "symbol": "000756",
            "stock_name": "新华制药",
            "theme_names": ["创新药"],
            "pct_chg": 3.90,
            "quote_fresh": True,
            "market_timestamp": "20260715145930",
            "premarket_status": "WATCH",
            "research_validated": True,
            "claim_ids": ["claim-1"],
        },
        {
            "symbol": "000963",
            "stock_name": "华东医药",
            "theme_names": ["创新药", "消费"],
            "pct_chg": 4.98,
            "quote_fresh": True,
            "market_timestamp": "20260715145931",
            "premarket_status": "READY",
            "research_validated": True,
            "claim_ids": ["claim-2"],
        },
        {
            "symbol": "002020",
            "stock_name": "京新药业",
            "theme_names": ["创新药"],
            "pct_chg": 3.94,
            "quote_fresh": True,
            "market_timestamp": "20260715145932",
            "premarket_status": "WATCH",
            "research_validated": False,
            "claim_ids": [],
        },
        {
            "symbol": "600584",
            "stock_name": "长电科技",
            "theme_names": ["先进封装"],
            "pct_chg": 8.00,
            "quote_fresh": False,
            "market_timestamp": "20260714150000",
            "premarket_status": "WATCH",
            "research_validated": True,
            "claim_ids": ["old-claim"],
        },
    ]
    payload = {"as_of": "2026-07-15T14:59:40+08:00", "snapshots": snapshots}
    (target / "monitor_history.jsonl").write_text(
        json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def test_close_report_aggregates_themes_and_excludes_stale_quotes(tmp_path):
    _write_close_history(tmp_path)
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
        runtime_factory=lambda **kwargs: SimpleNamespace(
            task_resumer=SimpleNamespace(list_resumable_tasks=lambda: [])
        ),
    )

    result = monitor.run_close(
        as_of="2026-07-15T15:10:00+08:00", external_push=False
    )

    innovation = next(
        row for row in result.payload["theme_strength"]
        if row["theme_name"] == "创新药"
    )
    assert innovation["member_count"] == 3
    assert innovation["rising_count"] == 3
    assert innovation["average_pct_chg"] == 4.27
    assert innovation["core_company"] == "华东医药"
    assert result.payload["market_review"]["stale_excluded_count"] == 1
    assert all(
        row["symbol"] != "600584" for row in result.payload["close_watchlist"]
    )
    assert len(result.payload["close_watchlist"]) <= 5

    markdown = (tmp_path / "latest_close.md").read_text(encoding="utf-8")
    for heading in (
        "今日市场回顾", "强势主题", "涨幅最高公司", "催化与证据",
        "次日验证项", "数据图表", "公告与新闻头条",
    ):
        assert heading in markdown
    assert "| 排名 |" not in markdown
    assert "| 主题 | 成员 |" not in markdown
    assert "研究观察优先级说明" in markdown
    assert "当日强度30% + 近5日20% + 催化20% + 证据15% + 换手10% + 量能5%" in markdown
    assert "HTTPSConnectionPool" not in markdown
    for forbidden in ("买入", "卖出", "仓位", "止盈", "止损", "明日推荐"):
        assert forbidden not in markdown


def test_tail_research_outputs_at_most_five_fresh_observations(tmp_path):
    _write_close_history(tmp_path)
    latest = json.loads(
        (tmp_path / "2026-07-15" / "monitor_history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    (tmp_path / "latest_monitor.json").write_text(
        json.dumps(latest, ensure_ascii=False), encoding="utf-8"
    )
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
        theme_engine=SimpleNamespace(generate=lambda **kwargs: []),
        theme_review_processor=SimpleNamespace(),
    )

    result = monitor.run(
        mode="tail_research",
        as_of="2026-07-15T14:50:00+08:00",
        external_push=False,
    )

    assert result.status == "partial"
    assert result.errors
    assert len(result.payload["close_watchlist"]) <= 5
    assert all(row["quote_fresh"] for row in result.payload["close_watchlist"])
    assert (tmp_path / "latest_tail_research.md").exists()


def test_tail_research_discovers_new_intraday_theme_members(tmp_path, monkeypatch):
    _write_close_history(tmp_path)
    latest = json.loads(
        (tmp_path / "2026-07-15" / "monitor_history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    (tmp_path / "latest_monitor.json").write_text(
        json.dumps(latest, ensure_ascii=False), encoding="utf-8"
    )
    member = SimpleNamespace(
        symbol="600664.SH",
        company_name="哈药股份",
        research_claim_ids=["hy-claim"],
        research_claim_status="ACTIVE",
    )
    packet = SimpleNamespace(theme_name="消费", members=[member])
    engine = SimpleNamespace(generate=lambda **kwargs: [packet])
    monkeypatch.setattr(
        ProviderRegistry,
        "get_realtime_quotes",
        lambda self, symbols: pd.DataFrame([{
            "symbol": "600664",
            "stock_name": "哈药股份",
            "price": 4.49,
            "pct_chg": 10.05,
            "provider": "tencent",
            "market_timestamp": "20260715144950",
        }]),
    )
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
        theme_engine=engine,
        theme_review_processor=SimpleNamespace(),
    )

    result = monitor.run_tail_research(
        as_of="2026-07-15T14:50:00+08:00", external_push=False
    )

    consumer = next(
        row for row in result.payload["theme_strength"]
        if row["theme_name"] == "消费"
    )
    assert consumer["core_company"] == "哈药股份"
    assert any(
        row["symbol"] == "600664" for row in result.payload["close_watchlist"]
    )


def test_first_party_evidence_is_joined_from_local_repository(tmp_path):
    claim = SimpleNamespace(claim_id="claim-hy", subject_id="600664.SH")
    evidence = SimpleNamespace(
        is_first_party=True,
        stance="support",
        quote="公司预计半年度归母净利润同比增长。",
        metadata={},
    )
    repository = SimpleNamespace(
        list_claims=lambda status, limit: [claim] if status == "ACTIVE" else [],
        list_evidence=lambda claim_id, limit: [evidence],
    )
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=repository,
        data_query=SimpleNamespace(),
    )

    enriched = monitor._attach_local_first_party_evidence([{
        "symbol": "600664",
        "stock_name": "哈药股份",
        "claim_ids": [],
        "research_validated": False,
    }])

    assert enriched[0]["research_validated"] is True
    assert enriched[0]["first_party_evidence_count"] == 1
    assert "半年度归母净利润" in enriched[0]["first_party_evidence_quotes"][0]


def test_historical_evidence_join_rejects_future_or_unprovable_records(tmp_path):
    claims = [
        SimpleNamespace(
            claim_id="eligible-claim",
            subject_id="600664.SH",
            created_at="2026-07-15T09:00:00+08:00",
            valid_from="2026-07-15T09:00:00+08:00",
        ),
        SimpleNamespace(
            claim_id="future-claim",
            subject_id="600664.SH",
            created_at="2026-07-16T09:00:00+08:00",
            valid_from="2026-07-16T09:00:00+08:00",
        ),
        SimpleNamespace(
            claim_id="unprovable-claim",
            subject_id="600664.SH",
            created_at="",
            valid_from="",
        ),
    ]
    evidence = SimpleNamespace(
        is_first_party=True,
        stance="support",
        quote="future evidence",
        metadata={},
        published_at="2026-07-16T09:10:00+08:00",
        retrieved_at="2026-07-16T09:20:00+08:00",
        created_at="2026-07-16T09:30:00+08:00",
        valid_from="2026-07-16T09:10:00+08:00",
    )
    repository = SimpleNamespace(
        list_claims=lambda status, limit: claims if status == "ACTIVE" else [],
        list_evidence=lambda claim_id, limit: [evidence],
    )
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=repository,
        data_query=SimpleNamespace(),
    )

    enriched = monitor._attach_local_first_party_evidence(
        [{
            "symbol": "600664",
            "claim_ids": [
                "eligible-claim",
                "future-claim",
                "unprovable-claim",
            ],
            "research_validated": True,
        }],
        as_of="2026-07-15T14:50:00+08:00",
    )

    assert enriched[0]["research_validated"] is False
    assert enriched[0]["first_party_evidence_count"] == 0
    assert enriched[0]["first_party_evidence_quotes"] == []


def test_tail_research_exposes_dynamic_discovery_failure(tmp_path, monkeypatch):
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
    )
    monkeypatch.setattr(
        monitor,
        "_discover_dynamic_theme_snapshots",
        lambda **_kwargs: ([], ["dynamic theme quote refresh returned empty"]),
    )

    result = monitor.run_tail_research(
        as_of="2026-07-15T14:50:00+08:00", external_push=False
    )

    assert result.status == "failed"
    assert result.payload["status"] == "failed"
    assert result.errors == ["dynamic theme quote refresh returned empty"]


def test_explainable_research_ranking_compares_full_theme_members() -> None:
    rows = [
        {
            "symbol": "603538", "stock_name": "美诺华", "theme_names": ["创新药"],
            "pct_chg": 10.01, "five_session_pct": 39.39, "turnover_rate": 30.43,
            "volume_ratio": 1.24, "first_party_evidence_count": 2,
            "announcement_titles": ["药品生产许可证变更公告", "股票交易异常波动公告"],
        },
        {
            "symbol": "002317", "stock_name": "众生药业", "theme_names": ["创新药"],
            "pct_chg": 6.87, "five_session_pct": 28.19, "turnover_rate": 12.74,
            "volume_ratio": 1.51, "first_party_evidence_count": 1,
            "announcement_titles": ["股票交易异常波动公告"],
        },
        {
            "symbol": "600664", "stock_name": "哈药股份", "theme_names": ["创新药"],
            "pct_chg": 10.05, "five_session_pct": 45.31, "turnover_rate": 4.43,
            "volume_ratio": 0.68, "first_party_evidence_count": 3,
            "announcement_titles": ["2026年半年度业绩预增公告", "基本药物目录调整公告"],
        },
        {
            "symbol": "000963", "stock_name": "华东医药", "theme_names": ["创新药"],
            "pct_chg": 4.98, "five_session_pct": 8.64, "turnover_rate": 2.58,
            "volume_ratio": 2.11, "first_party_evidence_count": 1,
            "announcement_titles": ["药物临床试验批准通知书公告"],
        },
    ]

    ranked = ResearchMonitor._rank_research_candidates(rows, limit=4)

    assert [row["stock_name"] for row in ranked] == [
        "哈药股份", "美诺华", "华东医药", "众生药业"
    ]
    assert ranked[0]["rank"] == 1
    assert ranked[0]["research_score"] > ranked[-1]["research_score"]
    assert set(ranked[0]["score_components"]) == {
        "daily_strength", "persistence", "catalyst", "evidence_quality",
        "turnover_quality", "volume_confirmation",
    }
    assert "中报" in ranked[0]["selection_reason"]
    assert "换手" in ranked[1]["risk_explanation"]
    assert "落后于" in ranked[-1]["comparison_explanation"]


def test_ranking_uses_only_causally_relevant_recent_company_announcements() -> None:
    row = {
        "symbol": "688302",
        "stock_name": "海创药业",
        "theme_names": ["创新药"],
        "pct_chg": 9.98,
        "five_session_pct": 14.9,
        "turnover_rate": 5.59,
        "volume_ratio": 1.62,
        "volume_vs_ma5": 2.94,
        "volume_anomaly": True,
        "market_timestamp": "20260716150000",
        "first_party_evidence_count": 5,
        "announcements": [
            {
                "title": "自愿披露关于开展高风险生化复发（BCR）前列腺癌Ⅲ期临床试验获得药物临床试验批准通知书的公告",
                "date": "2026-07-16",
                "source": "cninfo",
            },
            {
                "title": "关于2025年年度报告的信息披露监管问询函之回复公告",
                "date": "2026-06-27",
                "source": "cninfo",
            },
            {
                "title": "MASH药物临床IIa期试验取得最终分析结果的公告",
                "date": "2026-06-16",
                "source": "cninfo",
            },
        ],
    }

    ranked = ResearchMonitor._rank_research_candidates([row], limit=1)[0]

    assert ranked["causal_first_party_evidence_count"] == 1
    assert ranked["score_components"]["evidence_quality"] == 1.0
    assert "BCR前列腺癌Ⅲ期临床试验获批" in ranked["selection_reason"]
    assert "2.94倍" in ranked["risk_explanation"]
    assert "不等同商业化" in ranked["risk_explanation"]
    assert len(ranked["causal_announcements"]) == 1

    row.update(
        quote_fresh=True,
        universe_scope="FULL_MARKET",
        market_symbol_count=1,
        ma5=39.51,
        ma10=39.02,
        previous_high=41.38,
    )
    analysis = ResearchMonitor._build_session_analysis(
        [row], as_of="2026-07-16T15:05:00+08:00"
    )
    watch = analysis["close_watchlist"][0]
    assert watch["evidence_status"] == "已验证（公司公告）"
    assert len(watch["announcements"]) == 1
    assert watch["technical_reference"] == "前高 41.38；MA5 39.51；MA10 39.02"
    assert "待第一方来源核验" not in watch["catalyst"]


def test_old_research_announcement_does_not_explain_current_session() -> None:
    ranked = ResearchMonitor._rank_research_candidates(
        [
            {
                "symbol": "688302",
                "stock_name": "海创药业",
                "theme_names": ["创新药"],
                "pct_chg": 9.98,
                "market_timestamp": "20260716150000",
                "announcements": [
                    {
                        "title": "MASH药物临床IIa期试验取得最终分析结果的公告",
                        "date": "2026-06-16",
                        "source": "cninfo",
                    }
                ],
            }
        ],
        limit=1,
    )[0]

    assert ranked["score_components"]["catalyst"] == 0.0
    assert ranked["score_components"]["evidence_quality"] == 0.0
    assert "暂无与当日直接相关的硬催化" in ranked["selection_reason"]


def test_full_market_quotes_are_fetched_in_bounded_parallel_batches(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_quotes(self, symbols):
        calls.append(list(symbols))
        return pd.DataFrame([
            {
                "symbol": symbol,
                "stock_name": symbol,
                "price": 10.0,
                "pct_chg": 1.0,
                "market_timestamp": "20260716145000",
            }
            for symbol in symbols
        ])

    monkeypatch.setattr(ProviderRegistry, "get_realtime_quotes", fake_quotes)

    quotes, metrics = ResearchMonitor._fetch_realtime_quotes_batched(
        [f"{index:06d}" for index in range(205)], batch_size=80, max_workers=8
    )

    assert sorted(len(batch) for batch in calls) == [45, 80, 80]
    assert len(quotes) == 205
    assert metrics["requested_symbol_count"] == 205
    assert metrics["returned_quote_count"] == 205
    assert metrics["failed_batch_count"] == 0


def test_session_analysis_marks_full_market_coverage() -> None:
    snapshots = [
        {
            "symbol": "600664",
            "stock_name": "哈药股份",
            "theme_names": ["创新药"],
            "pct_chg": 10.05,
            "quote_fresh": True,
            "market_timestamp": "20260716145000",
            "universe_scope": "FULL_MARKET",
            "market_symbol_count": 5528,
        }
    ]

    analysis = ResearchMonitor._build_session_analysis(
        snapshots, as_of="2026-07-16T14:50:00+08:00"
    )

    assert analysis["market_review"]["universe_scope"] == "FULL_MARKET"
    assert analysis["market_review"]["market_symbol_count"] == 5528
    assert analysis["market_review"]["fresh_quote_count"] == 1


def test_market_review_uses_board_specific_price_limit_thresholds() -> None:
    rows = [
        {"symbol": "688302", "stock_name": "海创药业", "pct_chg": 9.98},
        {"symbol": "600001", "stock_name": "主板公司", "pct_chg": 9.98},
        {"symbol": "301001", "stock_name": "创业板公司", "pct_chg": 19.9},
        {"symbol": "920001", "stock_name": "北交所公司", "pct_chg": 29.9},
        {"symbol": "600002", "stock_name": "ST公司", "pct_chg": 4.9},
    ]
    for row in rows:
        row.update(
            quote_fresh=True,
            market_timestamp="20260716150000",
            universe_scope="FULL_MARKET",
            market_symbol_count=len(rows),
        )

    sea_only = ResearchMonitor._build_session_analysis(
        [rows[0]], as_of="2026-07-16T15:05:00+08:00"
    )
    assert sea_only["market_review"]["near_limit_up_count"] == 0

    analysis = ResearchMonitor._build_session_analysis(
        rows, as_of="2026-07-16T15:05:00+08:00"
    )

    assert analysis["market_review"]["near_limit_up_count"] == 4


def test_dynamic_discovery_scans_full_market_and_requests_broad_theme_source(monkeypatch) -> None:
    generate_args: dict = {}

    def generate(**kwargs):
        generate_args.update(kwargs)
        member = SimpleNamespace(
            symbol="600664.SH",
            company_name="哈药股份",
            research_claim_ids=[],
            research_claim_status="",
        )
        return [SimpleNamespace(theme_name="创新药", members=[member])]

    basics = pd.DataFrame([
        {"symbol": "600664", "name": "哈药股份", "industry": "化学制药"},
        {"symbol": "603538", "name": "美诺华", "industry": "化学制药"},
        {"symbol": "002317", "name": "众生药业", "industry": "中成药"},
    ])
    monkeypatch.setattr(
        ResearchMonitor,
        "_fetch_realtime_quotes_batched",
        staticmethod(lambda symbols, **kwargs: (
            [
                {
                    "symbol": symbol,
                    "stock_name": symbol,
                    "price": 10.0,
                    "pct_chg": 1.0,
                    "market_timestamp": "20260716145000",
                }
                for symbol in symbols
            ],
            {
                "requested_symbol_count": len(symbols),
                "returned_quote_count": len(symbols),
                "failed_batch_count": 0,
                "errors": [],
            },
        )),
    )
    monitor = ResearchMonitor(
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(get_stock_basic=lambda: basics),
        theme_engine=SimpleNamespace(generate=generate),
    )

    rows, errors = monitor._discover_dynamic_theme_snapshots(
        as_of="2026-07-16T14:50:00+08:00"
    )

    assert errors == []
    assert generate_args["max_themes"] == 50
    assert generate_args["max_source_boards"] == 300
    assert len(rows) == 3
    assert all(row["universe_scope"] == "FULL_MARKET" for row in rows)
    assert all(row["market_symbol_count"] == 3 for row in rows)
    assert next(row for row in rows if row["symbol"] == "600664")["theme_names"] == ["创新药", "化学制药"]
    assert next(row for row in rows if row["symbol"] == "603538")["theme_names"] == ["化学制药"]
    assert next(row for row in rows if row["symbol"] == "002317")["theme_names"] == ["中成药"]


def test_close_discovery_falls_back_to_same_day_local_daily(monkeypatch) -> None:
    member = SimpleNamespace(
        symbol="603127.SH",
        company_name="昭衍新药",
        research_claim_ids=[],
        research_claim_status="",
    )
    engine = SimpleNamespace(
        generate=lambda **_: [SimpleNamespace(theme_name="创新药", members=[member])]
    )
    basics = pd.DataFrame([{"symbol": "603127.SH", "name": "昭衍新药"}])
    daily = pd.DataFrame(
        [
            {
                "stock_code": "603127.SH",
                "trade_date": "2026-07-16",
                "close": 53.25,
                "change_pct": 5.1956,
                "turnover_rate": 19.5611,
                "volume": 1233036.71,
            }
        ]
    )
    monkeypatch.setattr(
        ResearchMonitor,
        "_fetch_realtime_quotes_batched",
        staticmethod(
            lambda symbols, **kwargs: (
                [],
                {
                    "requested_symbol_count": len(symbols),
                    "returned_quote_count": 0,
                    "failed_batch_count": 1,
                    "errors": ["realtime unavailable"],
                },
            )
        ),
    )
    monitor = ResearchMonitor(
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(
            get_stock_basic=lambda: basics,
            get_stock_pool=lambda date: daily,
        ),
        theme_engine=engine,
    )

    rows, errors = monitor._discover_dynamic_theme_snapshots(
        as_of="2026-07-16T15:30:00+08:00"
    )

    assert len(rows) == 1
    assert rows[0]["symbol"] == "603127"
    assert rows[0]["theme_names"] == ["创新药"]
    assert rows[0]["pct_chg"] == 5.2
    assert rows[0]["provider"] == "local_daily"
    assert rows[0]["quote_fresh"] is True
    assert errors == []


def test_report_announcements_exclude_future_and_expired_items() -> None:
    announcements = [
        {"title": "未来公告", "date": "2026-07-17", "source": "cninfo"},
        {"title": "当日公告", "date": "2026-07-16", "source": "cninfo"},
        {"title": "近期公告", "date": "2026-07-01", "source": "cninfo"},
        {"title": "过期公告", "date": "2026-05-01", "source": "cninfo"},
        {"title": "无日期公告", "date": "", "source": "cninfo"},
    ]

    result = ResearchMonitor._pit_announcements(
        announcements, trade_date="2026-07-16"
    )

    assert [item["title"] for item in result] == ["当日公告", "近期公告"]


def test_intraday_announcements_require_precise_same_day_publish_time() -> None:
    announcements = [
        {"title": "same-day date only", "date": "2026-07-16", "source": "cninfo"},
        {
            "title": "published before cutoff",
            "published_at": "2026-07-16T14:30:00+08:00",
            "source": "cninfo",
        },
        {
            "title": "published after cutoff",
            "published_at": "2026-07-16T15:10:00+08:00",
            "source": "cninfo",
        },
        {"title": "prior day", "date": "2026-07-15", "source": "cninfo"},
    ]

    result = ResearchMonitor._pit_announcements(
        announcements,
        trade_date="2026-07-16",
        as_of="2026-07-16T14:50:00+08:00",
    )

    assert [item["title"] for item in result] == [
        "published before cutoff",
        "prior day",
    ]


def test_intraday_earnings_forecasts_fail_closed_by_precise_available_time() -> None:
    forecasts = [
        {
            "symbol": "600664",
            "announcement_date": "20260716",
            "forecast_type": "same-day date only",
        },
        {
            "symbol": "600664",
            "announcement_date": "20260716",
            "available_at": "2026-07-16T14:30:00+08:00",
            "forecast_type": "visible",
        },
        {
            "symbol": "600664",
            "announcement_date": "20260716",
            "published_at": "2026-07-16T15:10:00+08:00",
            "forecast_type": "future",
        },
        {
            "symbol": "600664",
            "announcement_date": "20260715",
            "forecast_type": "prior day",
        },
    ]

    result = ResearchMonitor._pit_earnings_forecasts(
        forecasts,
        as_of="2026-07-16T14:50:00+08:00",
    )

    assert [item["forecast_type"] for item in result] == ["visible", "prior day"]


def test_candidate_announcement_batch_timeout_degrades_without_crashing(
    monkeypatch, tmp_path
) -> None:
    import core.runtime.research_monitor as monitor_module
    import infrastructure.data_providers.research.cninfo_provider as cninfo_module

    class EmptyAnnouncementProvider:
        @staticmethod
        def get_announcements(**_kwargs):
            return []

    monkeypatch.setattr(cninfo_module, "CninfoProvider", EmptyAnnouncementProvider)
    monkeypatch.setattr(
        monitor_module,
        "as_completed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    monitor = ResearchMonitor(
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(get_stock_history=lambda *_args: pd.DataFrame()),
        output_dir=tmp_path,
    )

    result = monitor._enrich_candidate_research_features(
        [
            {
                "symbol": "603127",
                "stock_name": "昭衍新药",
                "pct_chg": 5.2,
                "theme_names": ["创新药"],
            }
        ],
        as_of="2026-07-16T15:05:00+08:00",
    )

    assert result[0]["symbol"] == "603127"
    assert result[0].get("announcements", []) == []


def test_candidate_history_enrichment_uses_one_bulk_query(monkeypatch, tmp_path) -> None:
    import infrastructure.data_providers.research.cninfo_provider as cninfo_module

    monkeypatch.setattr(
        cninfo_module,
        "CninfoProvider",
        lambda: SimpleNamespace(get_announcements=lambda **_kwargs: []),
    )
    calls = []

    def history(symbol, *_args):
        calls.append(symbol)
        return pd.DataFrame([
            {"stock_code": f"60000{i}.SH", "trade_date": "2026-07-15", "close": 10.0}
            for i in range(8)
        ])

    monitor = ResearchMonitor(
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(get_stock_history=history),
        output_dir=tmp_path,
    )
    rows = [
        {"symbol": f"60000{i}", "stock_name": str(i), "pct_chg": 5.0,
         "theme_names": ["化学制药"]}
        for i in range(8)
    ]

    monitor._enrich_candidate_research_features(rows, as_of="2026-07-16T14:50:00+08:00")

    assert calls == [None]


def test_official_announcement_candidate_is_reported_outside_top_five() -> None:
    rows = []
    for index, change in enumerate((10.0, 9.0, 8.0, 7.0, 6.0), start=1):
        rows.append(
            {
                "symbol": f"60000{index}",
                "stock_name": f"强势股{index}",
                "theme_names": ["创新药"],
                "pct_chg": change,
                "first_party_evidence_count": 3,
                "announcement_titles": ["药品注册公告"],
                "quote_fresh": True,
                "market_timestamp": "20260716150000",
                "universe_scope": "FULL_MARKET",
                "market_symbol_count": 6,
            }
        )
    rows.append(
        {
            "symbol": "603127",
            "stock_name": "昭衍新药",
            "theme_names": ["创新药"],
            "pct_chg": 5.2,
            "announcement_titles": ["2026年半年度业绩公告"],
            "announcements": [
                {
                    "title": "2026年半年度业绩公告",
                    "date": "2026-07-15",
                    "source": "cninfo",
                }
            ],
            "first_party_evidence_count": 1,
            "quote_fresh": True,
            "market_timestamp": "20260716150000",
            "universe_scope": "FULL_MARKET",
            "market_symbol_count": 6,
        }
    )

    analysis = ResearchMonitor._build_session_analysis(
        rows, as_of="2026-07-16T15:05:00+08:00"
    )

    assert all(row["symbol"] != "603127" for row in analysis["close_watchlist"])
    assert analysis["official_announcement_watchlist"][0]["symbol"] == "603127"
    assert analysis["official_announcement_watchlist"][0]["announcement_titles"] == [
        "2026年半年度业绩公告"
    ]


def test_report_skips_stock_fund_flow_when_board_upstream_is_empty(
    monkeypatch, tmp_path
) -> None:
    import infrastructure.data_providers.provider_registry as provider_module
    import core.runtime.research_monitor as monitor_module

    stock_calls = []

    class EmptyFlowRegistry:
        @staticmethod
        def get_board_fund_flow(_trade_date):
            return pd.DataFrame()

        @staticmethod
        def get_stock_fund_flow(_trade_date, _codes):
            stock_calls.append(list(_codes))
            return pd.DataFrame()

    monkeypatch.setattr(provider_module, "ProviderRegistry", EmptyFlowRegistry)
    monkeypatch.setattr(
        monitor_module,
        "load_external_market_snapshot",
        lambda **_kwargs: {
            "status": "unavailable", "source": "", "markets": [],
            "attempts": ["test"], "error": "外围数据暂不可用（已尝试：test）",
        },
    )
    monkeypatch.setattr(
        ResearchMonitor,
        "_generate_grounded_ai_summary",
        staticmethod(lambda _analysis: ""),
    )
    monitor = ResearchMonitor(
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
        output_dir=tmp_path,
    )
    analysis = {
        "market_review": {"average_pct_chg": 0.1},
        "theme_strength": [{"theme_name": "创新药"}],
        "close_watchlist": [{"symbol": "603127", "stock_name": "昭衍新药"}],
    }

    result = monitor._enrich_report_context(analysis, [], as_of="2026-07-16T15:05:00+08:00")

    assert result["fund_flow"]["status"] == "empty"
    assert result["close_watchlist"][0]["main_net_inflow"] is None
    assert stock_calls == []


def test_report_does_not_mix_ths_board_flow_with_eastmoney_stock_flow(
    monkeypatch, tmp_path
) -> None:
    import infrastructure.data_providers.provider_registry as provider_module
    import core.runtime.research_monitor as monitor_module

    stock_calls = []

    class ThsRegistry:
        @staticmethod
        def get_board_fund_flow(_trade_date):
            return pd.DataFrame([{
                "board_name": "化学制药", "main_net_inflow": 100_000_000,
                "provider": "akshare-ths", "methodology": "同花顺行业资金流",
                "trade_date": "2026-07-16",
            }])

        @staticmethod
        def get_stock_fund_flow(_trade_date, _codes):
            stock_calls.append(True)
            return pd.DataFrame()

    monkeypatch.setattr(provider_module, "ProviderRegistry", ThsRegistry)
    monkeypatch.setattr(
        monitor_module, "load_external_market_snapshot",
        lambda **_: {"status": "ok", "source": "Yahoo", "markets": [{
            "name": "纳斯达克", "pct_chg": 1.0, "price": 1.0,
            "as_of": "2026-07-16", "source": "Yahoo",
        }], "attempts": ["Yahoo"], "error": ""},
    )
    monkeypatch.setattr(ResearchMonitor, "_generate_grounded_ai_summary", staticmethod(lambda _: ""))
    monitor = ResearchMonitor(
        research_repository=SimpleNamespace(), data_query=SimpleNamespace(), output_dir=tmp_path
    )

    monitor._enrich_report_context(
        {
            "market_review": {"fresh_quote_count": 1, "market_symbol_count": 1},
            "theme_strength": [{"theme_name": "化学制药"}],
            "close_watchlist": [{"symbol": "600664"}],
        },
        [],
        as_of="2026-07-16T15:05:00+08:00",
    )

    assert stock_calls == []


def test_report_marks_missing_external_and_fund_flow_as_degraded(
    monkeypatch, tmp_path
) -> None:
    import infrastructure.data_providers.provider_registry as provider_module
    import core.runtime.research_monitor as monitor_module

    class EmptyRegistry:
        @staticmethod
        def get_board_fund_flow(_trade_date):
            return pd.DataFrame()

    monkeypatch.setattr(provider_module, "ProviderRegistry", EmptyRegistry)
    monkeypatch.setattr(
        monitor_module,
        "load_external_market_snapshot",
        lambda **_kwargs: {
            "status": "unavailable", "source": "", "markets": [],
            "attempts": ["EastMoney", "Yahoo", "Sina"],
            "error": "外围数据暂不可用（已尝试：EastMoney、Yahoo、Sina）",
        },
    )
    monkeypatch.setattr(ResearchMonitor, "_generate_grounded_ai_summary", staticmethod(lambda _: ""))
    monitor = ResearchMonitor(
        research_repository=SimpleNamespace(), data_query=SimpleNamespace(), output_dir=tmp_path
    )
    analysis = {
        "market_review": {"fresh_quote_count": 5000, "market_symbol_count": 5500},
        "theme_strength": [{"theme_name": "创新药"}],
        "close_watchlist": [],
    }

    result = monitor._enrich_report_context(analysis, [], as_of="2026-07-17T15:05:00+08:00")

    assert result["data_quality"]["status"] == "degraded"
    assert result["data_quality"]["ranking_scope"] == "domestic_only"
    assert "HTTPSConnectionPool" not in result["external_market_status"]["error"]


def test_runtime_fail_on_empty_applies_to_close_mode() -> None:
    result = RuntimeResult(
        mode="close", status="success", run_id="x", as_of="2026-07-17",
        payload={"close_watchlist": [], "data_quality": {"status": "insufficient"}},
    )

    gated = ResearchMonitor._apply_runtime_quality_gate(result, True, False)

    assert gated.exit_code == 3


def test_ai_summary_fallback_contains_breadth_rotation_and_fund_flow(monkeypatch) -> None:
    import core.utils.llm_usage as llm_usage_module

    monkeypatch.setattr(llm_usage_module, "load_llm_providers", lambda: [])
    analysis = {
        "market_review": {"average_pct_chg": -0.6, "rising_count": 2499, "falling_count": 2861},
        "theme_strength": [
            {"theme_name": "影视音像"}, {"theme_name": "旅游服务"}, {"theme_name": "创新药"},
        ],
        "fund_flow": {"leaders": [
            {"board_name": "文化传媒"}, {"board_name": "消费电子"},
        ]},
        "close_watchlist": [{"stock_name": "海创药业", "catalyst": "临床试验获批"}],
    }

    summary = ResearchMonitor._generate_grounded_ai_summary(analysis)

    assert "上涨2499/下跌2861" in summary
    assert "影视音像、旅游服务、创新药" in summary
    assert "文化传媒、消费电子" in summary
    assert summary.endswith("（规则总结）")


def test_dynamic_concept_is_kept_alongside_top_industry_rotation() -> None:
    rows = []
    for index in range(11):
        rows.append({
            "symbol": f"6000{index:02d}", "stock_name": str(index),
            "theme_names": [f"行业{index}"], "pct_chg": 11.0 - index * 0.5,
            "quote_fresh": True, "market_timestamp": "20260716150000",
            "universe_scope": "FULL_MARKET", "market_symbol_count": 12,
        })
    rows.append({
        "symbol": "600664", "stock_name": "哈药股份",
        "theme_names": ["创新药", "化学制药"], "concept_theme_names": ["创新药"],
        "pct_chg": 1.0, "quote_fresh": True, "market_timestamp": "20260716150000",
        "universe_scope": "FULL_MARKET", "market_symbol_count": 12,
    })

    result = ResearchMonitor._build_session_analysis(rows, as_of="2026-07-16T15:05:00+08:00")

    assert "创新药" in [row["theme_name"] for row in result["theme_strength"]]


def test_earnings_forecast_direction_changes_catalyst_score() -> None:
    base = {
        "pct_chg": 5.0, "five_session_pct": 10.0, "turnover_rate": 5.0,
        "volume_ratio": 1.0, "quote_fresh": True,
    }
    ranked = ResearchMonitor._rank_research_candidates([
        {**base, "symbol": "603127", "stock_name": "昭衍新药", "earnings_forecast": {
            "forecast_type": "预增", "change_min_pct": 884.9, "change_max_pct": 1377.4,
            "announcement_date": "20260715", "provider": "tushare",
        }},
        {**base, "symbol": "603567", "stock_name": "珍宝岛", "earnings_forecast": {
            "forecast_type": "续亏", "net_profit_min_wan": -29800,
            "net_profit_max_wan": -25500, "announcement_date": "20260715",
            "provider": "tushare",
        }},
    ], limit=2)

    by_name = {row["stock_name"]: row for row in ranked}
    assert by_name["昭衍新药"]["score_components"]["catalyst"] == 0.9
    assert "预增 +884.90%至+1377.40%" in by_name["昭衍新药"]["catalyst"]
    assert by_name["珍宝岛"]["score_components"]["catalyst"] == 0.0
    assert "续亏" in by_name["珍宝岛"]["risk_explanation"]


def test_five_session_strength_compounds_four_history_days_and_live_change() -> None:
    history = pd.DataFrame([
        {"trade_date": "2026-07-10", "change_pct": 10.13},
        {"trade_date": "2026-07-13", "change_pct": 10.09},
        {"trade_date": "2026-07-14", "change_pct": 9.97},
        {"trade_date": "2026-07-15", "change_pct": 10.05},
        # Same-day history must not be counted again with the live quote.
        {"trade_date": "2026-07-16", "change_pct": 1.00},
    ])

    result = ResearchMonitor._compound_five_session_strength(
        history, live_pct_chg=2.0, trade_date="2026-07-16"
    )

    expected = ((1.1013 * 1.1009 * 1.0997 * 1.1005 * 1.02) - 1.0) * 100
    assert result == round(expected, 2)


def test_monitor_exposes_market_session_and_anomaly_hypotheses(tmp_path, monkeypatch):
    """P6 集成：monitor payload 必须包含 market_session 和 anomaly_hypotheses 字段。

    价格异动（pct_chg >= 7%）只创建 RetrievalHypothesis，不直接升级优先级。
    """
    _write_premarket(tmp_path, as_of="2026-07-13T08:20:00+08:00")
    monkeypatch.setattr(
        ProviderRegistry,
        "get_realtime_quotes",
        lambda self, symbols: pd.DataFrame([{
            "symbol": "600584",
            "stock_name": "长电科技",
            "price": 108.50,
            "pct_chg": 8.5,
            "provider": "tencent",
            "market_timestamp": "20260713093501",
        }]),
    )

    result = _monitor(tmp_path).run_monitor(
        as_of="2026-07-13T09:35:00+08:00", external_push=False
    )

    # market_session 字段必须存在
    assert "market_session" in result.payload
    assert result.payload["market_session"] != ""

    # anomaly_hypotheses 字段必须存在
    assert "anomaly_hypotheses" in result.payload
    assert isinstance(result.payload["anomaly_hypotheses"], list)

    # 8.5% 涨幅应触发 surge 异动假设
    anomalies = result.payload["anomaly_hypotheses"]
    assert len(anomalies) >= 1
    assert anomalies[0]["anomaly_type"] == "surge"
    assert anomalies[0]["pct_chg"] == 8.5
    # 异动假设不进入证据槽
    assert anomalies[0]["eligible_for_evidence_slot"] is False

    # 每个 snapshot 必须有 freshness 字段
    for snapshot in result.payload["snapshots"]:
        assert "freshness" in snapshot
        assert "is_fresh" in snapshot["freshness"]


def test_monitor_env_var_isolation_for_output_dir(tmp_path, monkeypatch):
    """P6 环境隔离：RESEARCH_MONITOR_OUTPUT_DIR 环境变量必须被 ResearchMonitor 读取。"""
    exp_dir = tmp_path / "experiment"
    monkeypatch.setenv("RESEARCH_MONITOR_OUTPUT_DIR", str(exp_dir))

    # 不传 output_dir，应从环境变量读取
    monitor = ResearchMonitor(
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
    )
    assert monitor.output_dir == exp_dir


def test_maybe_push_injects_report_markdown_into_payload(tmp_path):
    """推送链路：_maybe_push 必须读取 report_path 文件内容并注入 NotificationPayload.data.markdown_content。

    验证飞书卡片能看到详细报告的链路：
    1. report_path 指向的 markdown 文件内容被读取
    2. NotificationPayload.data 中包含 markdown_content 字段
    3. 超长内容被截断到安全阈值
    """
    import pathlib

    from core.runtime.research_monitor import ResearchMonitor
    from core.notifications.models import NotificationPayload

    # 准备一个测试用 markdown 报告文件
    report_file = tmp_path / "report.md"
    report_markdown = "# 盘前复盘\n\n| 主题 | 状态 |\n|---|---|\n| 先进封装 | validated |"
    report_file.write_text(report_markdown, encoding="utf-8")

    captured_payloads: list[NotificationPayload] = []

    class _StubService:
        def notify(self, payload: NotificationPayload) -> bool:
            captured_payloads.append(payload)
            return True

    # 注入桩 dedup store，should_push 永远返回 True 避免第二次推送被拦截
    class _StubDedupStore:
        def should_push(self, **_kwargs) -> bool:
            return True

        def record_push(self, **_kwargs) -> None:
            pass

        def record_failure(self, **_kwargs) -> None:
            pass

    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=SimpleNamespace(),
        data_query=SimpleNamespace(),
        push_dedup_store=_StubDedupStore(),
    )

    # 写入 push gate 让 _maybe_push 通过前置检查
    import json as _json
    (tmp_path / "latest_success.json").write_text(
        _json.dumps({"trade_date": "2026-07-15", "run_id": "premarket_test"}),
        encoding="utf-8",
    )

    # 注入桩服务替代真实通知服务
    import core.notifications.notification_service as ns_module
    original_getter = ns_module.get_notification_service_core
    ns_module.get_notification_service_core = lambda: _StubService()
    try:
        result = monitor._maybe_push(
            external_push=True,
            trade_date="2026-07-15",
            title="AquaTrade 盘前研究复盘 2026-07-15",
            summary="动态主题 10 个",
            report_path=pathlib.Path(report_file),
            payload={"run_id": "premarket_test", "status": "success"},
        )
    finally:
        ns_module.get_notification_service_core = original_getter

    assert result["sent"] is True, f"推送失败 result={result}"
    assert len(captured_payloads) == 1
    captured = captured_payloads[0]
    assert captured.event_type == "daily_brief_ready"
    assert captured.data["markdown_content"] == report_markdown
    assert captured.data["report_path"] == str(report_file)

    # 验证超长内容会被截断
    long_markdown = "x" * 30000
    report_file.write_text(long_markdown, encoding="utf-8")
    captured_payloads.clear()

    ns_module.get_notification_service_core = lambda: _StubService()
    try:
        monitor._maybe_push(
            external_push=True,
            trade_date="2026-07-15",
            title="test",
            summary="s",
            report_path=pathlib.Path(report_file),
            payload={"run_id": "premarket_test", "status": "success"},
        )
    finally:
        ns_module.get_notification_service_core = original_getter

    truncated = captured_payloads[0].data["markdown_content"]
    assert len(truncated) < 30000
    assert "已截断" in truncated


def test_feishu_notifier_renders_full_markdown_when_available(tmp_path):
    """FeishuNotifier：data.markdown_content 存在但无结构化数据时退回 markdown 卡片。"""
    from core.notifications.feishu_notifier import FeishuNotifier
    from core.notifications.models import NotificationPayload

    notifier = FeishuNotifier()
    notifier._webhook_url = "https://example.com/hook"

    captured_markdown: list[str] = []
    captured_card: list[dict] = []

    class _StubPusher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def push_markdown(self, content, title="") -> bool:
            captured_markdown.append(content)
            return True

        def push_card(self, card, title="") -> bool:
            captured_card.append(card)
            return True

    # FeishuPush 在 _send_via_webhook 内通过 from core.notifications.feishu_push import FeishuPush
    # 局部导入，需要 monkeypatch 源模块的属性才能生效
    import core.notifications.feishu_push as fp_module
    original_pusher = fp_module.FeishuPush
    fp_module.FeishuPush = _StubPusher
    try:
        payload = NotificationPayload(
            event_type="daily_brief_ready",
            title="AquaTrade 盘前研究复盘 2026-07-15",
            summary="短摘要",
            priority="medium",
            links=[],
            data={
                "report_path": "/tmp/report.md",
                "markdown_content": "# 详细报告\n| 主题 |\n|---|\n| 先进封装 |",
                # 没有 themes/observations，应退回 markdown
            },
        )
        result = notifier._send_via_webhook("短文本", payload)
    finally:
        fp_module.FeishuPush = original_pusher

    assert result is True
    # 没有结构化数据时不应发送原生卡片
    assert len(captured_card) == 0
    assert len(captured_markdown) == 1
    md = captured_markdown[0]
    assert "# 详细报告" in md
    assert "先进封装" in md
    assert "🟡" in md
    assert "AquaTrade 盘前研究复盘 2026-07-15" in md


def test_feishu_notifier_falls_back_to_summary_without_markdown():
    """FeishuNotifier：data.markdown_content 缺失时退回到 summary 短文本模式。"""
    from core.notifications.feishu_notifier import FeishuNotifier
    from core.notifications.models import NotificationPayload

    notifier = FeishuNotifier()
    notifier._webhook_url = "https://example.com/hook"

    captured_markdown: list[str] = []

    class _StubPusher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def push_markdown(self, content, title="") -> bool:
            captured_markdown.append(content)
            return True

        def push_card(self, card, title="") -> bool:
            return True

    import core.notifications.feishu_push as fp_module
    original_pusher = fp_module.FeishuPush
    fp_module.FeishuPush = _StubPusher
    try:
        payload = NotificationPayload(
            event_type="daily_brief_ready",
            title="普通通知",
            summary="短摘要内容",
            priority="low",
            links=[],
            data={"report_path": "/tmp/x.md"},  # 没有 markdown_content 也没有结构化数据
        )
        notifier._send_via_webhook("短文本", payload)
    finally:
        fp_module.FeishuPush = original_pusher

    md = captured_markdown[0]
    assert "短摘要内容" in md
    assert "普通通知" in md


def test_feishu_notifier_builds_native_table_card_from_structured_payload():
    """FeishuNotifier：data 含 themes/observations 时必须构造飞书原生 table 卡片。

    验证：
    1. 调用 push_card 而非 push_markdown
    2. 卡片 elements 包含 table 组件（tag=table）
    3. 动态主题表行数 = themes 长度
    4. WATCH 表只包含 research_validated=True 的观察
    5. 探索表只包含 research_validated=False 的观察
    """
    from core.notifications.feishu_notifier import FeishuNotifier
    from core.notifications.models import NotificationPayload

    notifier = FeishuNotifier()
    notifier._webhook_url = "https://example.com/hook"

    captured_card: list[dict] = []
    captured_markdown: list[str] = []

    class _StubPusher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def push_markdown(self, content, title="") -> bool:
            captured_markdown.append(content)
            return True

        def push_card(self, card, title="") -> bool:
            captured_card.append(card)
            return True

    import core.notifications.feishu_push as fp_module
    original_pusher = fp_module.FeishuPush
    fp_module.FeishuPush = _StubPusher
    try:
        payload = NotificationPayload(
            event_type="daily_brief_ready",
            title="AquaTrade 盘前研究复盘 2026-07-15",
            summary="短摘要",
            priority="medium",
            links=[],
            data={
                "report_path": "/tmp/report.md",
                "markdown_content": "# fallback md",
                "themes": [
                    {
                        "theme_name": "先进封装",
                        "theme_type": "ice_reversal",
                        "phase": "ICE",
                        "negative_status": "clear",
                        "research_gate_status": "validated",
                        "validated_core_count": 2,
                    },
                    {
                        "theme_name": "创新药",
                        "theme_type": "research_discovery",
                        "phase": "UNKNOWN",
                        "negative_status": "clear",
                        "research_gate_status": "pending",
                        "validated_core_count": 0,
                    },
                ],
                "observations": [
                    {
                        "symbol": "002185.SZ",
                        "company_name": "华天科技",
                        "theme_name": "先进封装",
                        "technical_state": "ACCELERATION_RISK",
                        "status": "WATCH",
                        "research_validated": True,
                        "claim_status": "ACTIVE",
                        "market_features": {
                            "leadership_score": 0.9545,
                            "ret5": 0.171,
                            "ret20": 0.4991,
                        },
                    },
                    {
                        "symbol": "000063.SZ",
                        "company_name": "",
                        "theme_name": "数字经济",
                        "technical_state": "MA5_HOLD",
                        "status": "UNKNOWN",
                        "research_validated": False,
                        "claim_status": "",
                        "market_features": {},
                    },
                ],
                "research_task_runs": [
                    {
                        "task_id": "rt_theme_xxx",
                        "status": "succeeded",
                        "evidence_saved": 1,
                        "stopped_reason": "all_tiers_done",
                    }
                ],
                "ready_watch_count": 1,
                "status": "partial",
            },
        )
        result = notifier._send_via_webhook("短文本", payload)
    finally:
        fp_module.FeishuPush = original_pusher

    assert result is True
    # 必须走原生卡片路径，不走 markdown
    assert len(captured_card) == 1
    assert len(captured_markdown) == 0

    card = captured_card[0]
    assert card["header"]["title"]["content"] == "AquaTrade 盘前研究复盘 2026-07-15"
    assert card["header"]["template"] == "yellow"  # medium priority

    # 收集所有 table 组件
    tables = [e for e in card["elements"] if e.get("tag") == "table"]
    assert len(tables) == 4  # themes + watch + explore + tasks

    # 动态主题表
    themes_table = tables[0]
    assert len(themes_table["rows"]) == 2
    assert themes_table["rows"][0]["theme_name"] == "先进封装"
    assert themes_table["rows"][0]["research_gate_status"] == "validated"

    # WATCH 表（只含 research_validated=True）
    watch_table = tables[1]
    assert len(watch_table["rows"]) == 1
    assert watch_table["rows"][0]["symbol"] == "002185.SZ"
    assert watch_table["rows"][0]["company_name"] == "华天科技"
    assert watch_table["rows"][0]["ret5_pct"] == "17.10%"

    # 探索表（只含 research_validated=False）
    explore_table = tables[2]
    assert len(explore_table["rows"]) == 1
    assert explore_table["rows"][0]["symbol"] == "000063.SZ"
    assert explore_table["rows"][0]["company_name"] == "暂无本地证据"

    # 研究任务表
    tasks_table = tables[3]
    assert len(tasks_table["rows"]) == 1
    assert tasks_table["rows"][0]["status"] == "succeeded"
