from __future__ import annotations

import time
import sys
from types import SimpleNamespace

import pandas as pd

from core.quant.research_levels import calculate_technical_context
from core.runtime.research_digest import (
    build_short_recap,
    compare_previous_mentions,
    load_external_market_snapshot,
    _external_yahoo,
    summarize_board_fund_flow,
    summarize_external_markets,
)
from core.utils import llm_usage


def test_technical_context_calculates_ma_support_and_volume_anomaly() -> None:
    history = pd.DataFrame(
        {
            "trade_date": pd.date_range("2026-07-03", periods=10, freq="B"),
            "close": [10, 10.2, 10.4, 10.6, 10.8, 11, 11.2, 11.4, 11.6, 12],
            "high": [10.2, 10.4, 10.6, 10.8, 11, 11.2, 11.4, 11.6, 11.8, 12.2],
            "low": [9.8, 10, 10.2, 10.4, 10.6, 10.8, 11, 11.2, 11.4, 11.8],
            "volume": [100] * 9 + [220],
        }
    )

    result = calculate_technical_context(history, as_of="2026-07-16")

    assert result["ma5"] == 11.44
    assert result["ma10"] == 10.92
    assert result["previous_high"] == 11.8
    assert result["support_label"] == "前高"
    assert result["support_price"] == 11.8
    assert result["volume_vs_ma5"] > 1.8
    assert result["volume_anomaly"] is True


def test_short_recap_is_grounded_compact_and_has_no_trade_action() -> None:
    payload = {
        "as_of": "2026-07-16T15:27:00+08:00",
        "market_review": {
            "rising_count": 3200,
            "falling_count": 1800,
            "average_pct_chg": 0.82,
        },
        "theme_strength": [
            {"theme_name": "创新药", "average_pct_chg": 4.21},
            {"theme_name": "消费", "average_pct_chg": 2.10},
        ],
        "close_watchlist": [
            {
                "stock_name": "昭衍新药",
                "pct_chg": 5.20,
                "support_label": "MA5",
                "support_price": 48.12,
            }
        ],
        "ai_summary": "创新药领涨，量价结构较强（AI生成）",
    }

    text = build_short_recap(payload)

    assert text.startswith("2026/7/16复盘")
    assert "今日大盘：偏强" in text
    assert "领涨板块：创新药、消费" in text
    assert "龙头股：昭衍新药 +5.20%，MA5 48.12" in text
    assert "明日关注：创新药" in text
    assert "（AI生成）" in text
    assert all(word not in text for word in ("买入", "卖出", "止损", "仓位"))
    reason = next(line for line in text.splitlines() if line.startswith("原因："))[3:]
    assert len(reason) <= 15
    assert len(text) <= 360


def test_previous_limit_down_is_marked_as_learning_correction() -> None:
    previous = [{"symbol": "600584", "stock_name": "长电科技", "pct_chg": 3.2}]
    current = [
        {
            "symbol": "600584.SH",
            "stock_name": "长电科技",
            "pct_chg": -9.91,
            "quote_fresh": True,
        }
    ]

    tracking, corrections = compare_previous_mentions(previous, current)

    assert tracking[0]["outcome"] == "大幅回撤"
    assert tracking[0]["learning_label"] == "昨日判断需修正"
    assert corrections == ["长电科技"]


def test_board_fund_flow_keeps_source_and_matches_report_theme() -> None:
    frame = pd.DataFrame(
        [
            {"board_name": "创新药", "main_net_inflow": 1_500_000_000, "provider": "akshare"},
            {"board_name": "半导体", "main_net_inflow": -800_000_000, "provider": "akshare"},
        ]
    )

    result = summarize_board_fund_flow(frame, theme_names=["创新药"])

    assert result["status"] == "ok"
    assert result["theme_matches"][0]["theme_name"] == "创新药"
    assert result["theme_matches"][0]["main_net_inflow"] == 1_500_000_000
    assert result["source"] == "akshare"


def test_board_fund_flow_keeps_methodology_and_data_time() -> None:
    frame = pd.DataFrame([{
        "board_name": "医疗服务",
        "main_net_inflow": -1_751_000_000,
        "main_net_inflow_pct": None,
        "trade_date": "2026-07-17",
        "provider": "akshare-ths",
        "methodology": "同花顺行业资金流（净额字段，亿元）",
    }])

    result = summarize_board_fund_flow(frame, theme_names=["医疗服务"])

    assert result["leaders"][0]["as_of"] == "2026-07-17"
    assert result["leaders"][0]["methodology"].startswith("同花顺")


def test_board_fund_flow_matches_equivalent_vendor_industry_prefix() -> None:
    frame = pd.DataFrame([{
        "board_name": "影视院线", "main_net_inflow": 584_000_000,
        "provider": "akshare-ths",
    }])

    result = summarize_board_fund_flow(frame, theme_names=["影视音像"])

    assert result["theme_matches"][0]["board_name"] == "影视院线"


def test_external_market_summary_selects_us_and_korea_with_timestamp() -> None:
    frame = pd.DataFrame(
        [
            {"名称": "纳斯达克", "涨跌幅": 1.23, "最新价": 22000, "最新行情时间": "2026-07-16 05:00:00"},
            {"名称": "韩国KOSPI", "涨跌幅": -0.45, "最新价": 3200, "最新行情时间": "2026-07-16 14:50:00"},
            {"名称": "德国DAX", "涨跌幅": 0.20, "最新价": 25000, "最新行情时间": "2026-07-16 14:50:00"},
        ]
    )

    result = summarize_external_markets(frame)

    assert [row["name"] for row in result] == ["纳斯达克", "韩国KOSPI"]
    assert result[0]["as_of"] == "2026-07-16 05:00:00"


def test_external_market_snapshot_falls_back_without_leaking_transport_error() -> None:
    def broken_eastmoney():
        raise RuntimeError("HTTPSConnectionPool(host='push2.eastmoney.com')")

    def yahoo():
        return pd.DataFrame(
            [{"name": "纳斯达克", "pct_chg": 1.23, "price": 22000,
              "as_of": "2026-07-16 05:00:00", "provider": "Yahoo"}]
        )

    result = load_external_market_snapshot(
        loaders=[("EastMoney", broken_eastmoney), ("Yahoo", yahoo)]
    )

    assert result["status"] == "ok"
    assert result["source"] == "Yahoo"
    assert result["markets"][0]["as_of"] == "2026-07-16 05:00:00"
    assert "HTTPSConnectionPool" not in result["error"]


def test_external_market_snapshot_uses_friendly_message_when_all_sources_fail() -> None:
    def broken():
        raise RuntimeError("ProxyError: secret infrastructure detail")

    result = load_external_market_snapshot(
        loaders=[("EastMoney", broken), ("Yahoo", broken), ("Sina", broken)]
    )

    assert result == {
        "status": "unavailable",
        "source": "",
        "markets": [],
        "attempts": ["EastMoney", "Yahoo", "Sina"],
        "error": "外围数据暂不可用（已尝试：EastMoney、Yahoo、Sina）",
    }


def test_external_market_snapshot_does_not_block_report_on_hung_source() -> None:
    def hung():
        time.sleep(1)
        return pd.DataFrame()

    started = time.monotonic()
    result = load_external_market_snapshot(
        loaders=[("Hung", hung)], timeout_seconds=0.01
    )

    assert time.monotonic() - started < 0.2
    assert result["status"] == "unavailable"


def test_yahoo_loader_accepts_yfinance_multi_index_close(monkeypatch) -> None:
    symbols = ["^IXIC", "^GSPC", "^DJI", "^KS11"]
    columns = pd.MultiIndex.from_tuples([("Close", symbol) for symbol in symbols])
    history = pd.DataFrame(
        [[100.0] * 4, [101.0] * 4],
        index=pd.to_datetime(["2026-07-15", "2026-07-16"]),
        columns=columns,
    )
    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(download=lambda *_args, **_kwargs: history),
    )

    result = _external_yahoo("2026-07-16T15:05:00+08:00")

    assert len(result) == 4
    assert round(result.iloc[0]["pct_chg"], 2) == 1.0


def test_backup_flash_model_is_classified_as_flash(monkeypatch) -> None:
    values = {
        "LLM_API_BASE": "",
        "LLM_API_KEY": "",
        "LLM_MODEL_NAME": "",
        "LLM_BACKUP_API_BASE": "https://example.invalid",
        "LLM_BACKUP_API_KEY": "secret",
        "LLM_BACKUP_MODEL_NAME": "deepseek-v4-flash",
    }
    monkeypatch.setattr(llm_usage, "_config_value", lambda key, default="": values.get(key, default))

    providers = llm_usage.load_llm_providers()

    assert providers[0].name == "backup"
    assert providers[0].tier == "flash"
