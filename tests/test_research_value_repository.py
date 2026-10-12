"""P0.3 研究运行仓储测试。

按 docs/superpowers/plans/2026-07-18-institutional-research-p0-trusted-foundation.md
Task P0.3 验收要求覆盖：
- 可按 symbol/as_of/status 分页查询。
- 可读取任意历史 run。
- 可比较两个 run 的事实、假设、预期、估值和缺口变化。
- 重启后索引与产物一致。
- 迁移重复执行幂等。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.research.value_chain.repository import (
    ResearchRunRepository,
    _compute_source_fingerprint,
)


def _make_dossier(
    symbol: str, as_of: str, *, schema_version: str = "7",
) -> dict:
    """构造最小可用 dossier 字典。"""
    return {
        "symbol": symbol, "as_of": as_of, "schema_version": schema_version,
        "company_name": f"测试公司_{symbol}", "industry": "测试行业",
        "industry_context": {
            "verified_edges": [{"claim_id": f"edge_{symbol}_1"}],
            "observed_facts": [{"claim_id": f"obs_{symbol}_1"}],
            "forward_plans": [],
            "chain_status": "partial",
        },
        "profit_bridge": {
            "status": "partial",
            "forward_driver_assumptions": [{"metric": "capacity"}],
            "forward_disclosed_indicators": [
                {"metric": "sales_volume", "eligible_as_forward_assumption": True},
            ],
            "forward_guidance": [],
            "market_implied_scenarios": [],
        },
        "expectation_context": {
            "verified_expectations": [{"expectation_id": f"exp_{symbol}"}],
            "rejected_expectations": [],
        },
        "valuation": {
            "status": "partial", "metrics": {"pe": 18.5, "pb": 2.3},
        },
        "source_snapshots": {
            "profile": {
                "status": "ok", "provider": "fake", "as_of": as_of, "records": [{}],
            },
            "daily_bars": {
                "status": "ok", "provider": "fake", "as_of": as_of, "records": [{}],
            },
        },
        "research_assessment": {"version": "v1"},
        "financial_quality": {"status": "ok"},
    }


def _make_acceptance(symbol: str, as_of: str, *, status: str = "partial") -> dict:
    """构造 acceptance 字典。"""
    return {
        "run_id": "value_test",
        "symbol": symbol, "as_of": as_of, "status": status,
        "degradation_reasons": ["forward_profit_bridge_missing"],
        "source_statuses": {"profile": "ok"},
        "financial_quality_status": "ok",
        "valuation_status": "partial",
        "technical_status": "not_computable",
        "gap_tasks": {"created_task_ids": ["gap_1"], "existing_task_ids": []},
        "finished_at": "2026-07-18T10:00:00+00:00",
        "notice": "研究数据产物，不包含交易动作建议。",
    }


def _write_run_dir(
    runs_root: Path, run_id: str, dossier: dict, acceptance: dict,
) -> Path:
    """在 runs_root 下创建 run 目录并写入 dossier/acceptance JSON。"""
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "dossier.json").write_text(
        json.dumps(dossier, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (run_dir / "acceptance.json").write_text(
        json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return run_dir


# ---------------------------------------------------------------------------
# Schema 与初始化
# ---------------------------------------------------------------------------


def test_repository_creates_schema_and_indexes(tmp_path: Path) -> None:
    """仓储初始化必须创建 research_value_runs 表与索引；可重复执行。"""
    db_path = tmp_path / "runs.db"
    repo = ResearchRunRepository(db_path=db_path, runs_root=tmp_path)
    import sqlite3
    with sqlite3.connect(str(db_path)) as conn:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_runs_%'"
            ).fetchall()
        }
    assert "research_value_runs" in tables
    assert "schema_meta" in tables
    assert {"idx_runs_symbol", "idx_runs_as_of", "idx_runs_status", "idx_runs_symbol_as_of"} <= indexes
    # 重复执行不报错
    repo.migrate()


def test_schema_version_recorded(tmp_path: Path) -> None:
    """schema_meta 必须记录当前 schema_version。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    assert repo.SCHEMA_VERSION >= 1
    with repo._connect() as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    assert int(row[0]) == repo.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 写入与查询
# ---------------------------------------------------------------------------


def test_index_dossier_run_persists_all_fields(tmp_path: Path) -> None:
    """index_dossier_run 必须提取并持久化全部索引字段。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    dossier = _make_dossier("000001.SZ", "2026-07-18")
    acceptance = _make_acceptance("000001.SZ", "2026-07-18")
    run_dir = _write_run_dir(tmp_path, "value_run_a", dossier, acceptance)

    record_meta = repo.index_dossier_run(
        run_id="value_run_a", dossier=dossier, acceptance=acceptance,
        artifact_path=str(run_dir.resolve()),
        created_at="2026-07-18T10:00:00+00:00",
    )
    assert record_meta["symbol"] == "000001.SZ"
    assert record_meta["as_of"] == "2026-07-18"
    assert record_meta["status"] == "partial"
    assert record_meta["source_fingerprint"].startswith("fp_")

    record = repo.get_run("value_run_a")
    assert record is not None
    assert record["symbol"] == "000001.SZ"
    assert record["as_of"] == "2026-07-18"
    assert record["status"] == "partial"
    assert record["schema_version"] == "7"
    assert record["artifact_path"] == str(run_dir.resolve())
    assert record["source_fingerprint"].startswith("fp_")
    assert record["model_versions"]["schema_version"] == "7"
    assert record["model_versions"]["profit_bridge_kind"] == "partial"
    assert record["payload"]["degradation_reasons"] == ["forward_profit_bridge_missing"]
    assert record["payload"]["gap_task_ids"] == ["gap_1"]


def test_list_runs_filters_by_symbol_as_of_status(tmp_path: Path) -> None:
    """分页查询必须支持 symbol/as_of/status 过滤与分页字段。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    # 写入 5 条记录：3 条 000001.SZ + 2 条 600519.SH
    for idx in range(3):
        dossier = _make_dossier("000001.SZ", "2026-07-18")
        acceptance = _make_acceptance(
            "000001.SZ", "2026-07-18", status="ok" if idx == 0 else "partial",
        )
        run_dir = _write_run_dir(tmp_path, f"value_run_a_{idx}", dossier, acceptance)
        repo.index_dossier_run(
            run_id=f"value_run_a_{idx}", dossier=dossier, acceptance=acceptance,
            artifact_path=str(run_dir.resolve()),
            created_at=f"2026-07-18T10:0{idx}:00+00:00",
        )
    for idx in range(2):
        dossier = _make_dossier("600519.SH", "2026-07-17")
        acceptance = _make_acceptance("600519.SH", "2026-07-17", status="partial")
        run_dir = _write_run_dir(tmp_path, f"value_run_b_{idx}", dossier, acceptance)
        repo.index_dossier_run(
            run_id=f"value_run_b_{idx}", dossier=dossier, acceptance=acceptance,
            artifact_path=str(run_dir.resolve()),
            created_at=f"2026-07-17T10:0{idx}:00+00:00",
        )

    # symbol 过滤
    result = repo.list_runs(symbol="000001.SZ", page=1, page_size=10)
    assert result["total"] == 3
    assert len(result["items"]) == 3
    assert all(item["symbol"] == "000001.SZ" for item in result["items"])
    # created_at DESC
    assert result["items"][0]["created_at"] >= result["items"][-1]["created_at"]

    # as_of 过滤
    result = repo.list_runs(as_of="2026-07-17", page=1, page_size=10)
    assert result["total"] == 2
    assert all(item["as_of"] == "2026-07-17" for item in result["items"])

    # status 过滤
    result = repo.list_runs(status="ok", page=1, page_size=10)
    assert result["total"] == 1
    assert result["items"][0]["status"] == "ok"

    # 分页
    result = repo.list_runs(page=1, page_size=2)
    assert result["page"] == 1
    assert result["page_size"] == 2
    assert result["total"] == 5
    assert result["total_pages"] == 3
    assert len(result["items"]) == 2

    result_page2 = repo.list_runs(page=2, page_size=2)
    assert len(result_page2["items"]) == 2
    # 第二页的 created_at 应不晚于第一页
    assert result_page2["items"][0]["created_at"] <= result["items"][-1]["created_at"]


def test_list_runs_clamps_invalid_pagination_values(tmp_path: Path) -> None:
    repo = ResearchRunRepository(db_path=tmp_path / "runs.db", runs_root=tmp_path)

    result = repo.list_runs(page="invalid", page_size="invalid")

    assert result["page"] == 1
    assert result["page_size"] == 20
    assert result["total"] == 0


def test_latest_run_returns_most_recent(tmp_path: Path) -> None:
    """latest_run 返回 created_at 最新的运行。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    for idx in range(3):
        dossier = _make_dossier("000001.SZ", "2026-07-18")
        acceptance = _make_acceptance("000001.SZ", "2026-07-18")
        run_dir = _write_run_dir(tmp_path, f"value_run_l_{idx}", dossier, acceptance)
        repo.index_dossier_run(
            run_id=f"value_run_l_{idx}", dossier=dossier, acceptance=acceptance,
            artifact_path=str(run_dir.resolve()),
            created_at=f"2026-07-18T10:0{idx}:00+00:00",
        )
    latest = repo.latest_run("000001.SZ")
    assert latest is not None
    assert latest["run_id"] == "value_run_l_2"


# ---------------------------------------------------------------------------
# 磁盘产物读取与降级
# ---------------------------------------------------------------------------


def test_read_run_bundle_returns_dossier_and_acceptance(tmp_path: Path) -> None:
    """read_run_bundle 必须返回 dossier + acceptance + 索引元数据。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    dossier = _make_dossier("000001.SZ", "2026-07-18")
    acceptance = _make_acceptance("000001.SZ", "2026-07-18")
    run_dir = _write_run_dir(tmp_path, "value_run_r", dossier, acceptance)
    repo.index_dossier_run(
        run_id="value_run_r", dossier=dossier, acceptance=acceptance,
        artifact_path=str(run_dir.resolve()),
        created_at="2026-07-18T10:00:00+00:00",
    )

    bundle = repo.read_run_bundle("value_run_r")
    assert bundle["degraded"] is False
    assert bundle["dossier"]["symbol"] == "000001.SZ"
    assert bundle["acceptance"]["status"] == "partial"
    assert bundle["run"]["run_id"] == "value_run_r"


def test_read_run_bundle_degrades_when_json_missing(tmp_path: Path) -> None:
    """JSON 文件缺失时降级返回，不抛异常。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    # 索引中写入记录，但磁盘上 dossier.json 不存在
    repo.index_run(
        run_id="ghost_run", symbol="000001.SZ", as_of="2026-07-18",
        schema_version="7", status="partial",
        artifact_path=str(tmp_path / "ghost_run"),
        source_fingerprint="fp_x", model_versions={},
        created_at="2026-07-18T10:00:00+00:00",
    )
    bundle = repo.read_run_bundle("ghost_run")
    assert bundle["degraded"] is True
    assert bundle["dossier"] is None
    assert bundle["acceptance"] is None
    assert "missing_or_corrupt" in bundle["degradation_reason"]


def test_read_run_bundle_returns_not_found_for_unknown_run(tmp_path: Path) -> None:
    """未知 run_id 时降级返回 run_not_found。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    bundle = repo.read_run_bundle("never_existed")
    assert bundle["degraded"] is True
    assert bundle["run"] is None
    assert bundle["degradation_reason"] == "run_not_found"


def test_read_dossier_handles_corrupt_json(tmp_path: Path) -> None:
    """dossier.json 损坏时 read_dossier 返回 None，不抛异常。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    run_dir = tmp_path / "corrupt_run"
    run_dir.mkdir()
    (run_dir / "dossier.json").write_text("not-a-json{", encoding="utf-8")
    (run_dir / "acceptance.json").write_text("{}", encoding="utf-8")
    repo.index_run(
        run_id="corrupt_run", symbol="000001.SZ", as_of="2026-07-18",
        schema_version="7", status="partial",
        artifact_path=str(run_dir.resolve()),
        source_fingerprint="", model_versions={},
        created_at="2026-07-18T10:00:00+00:00",
    )
    assert repo.read_dossier("corrupt_run") is None


def test_read_dossier_rejects_non_object_json(tmp_path: Path) -> None:
    repo = ResearchRunRepository(db_path=tmp_path / "runs.db", runs_root=tmp_path)
    run_dir = tmp_path / "array_run"
    run_dir.mkdir()
    (run_dir / "dossier.json").write_text("[]", encoding="utf-8")
    repo.index_run(
        run_id="array_run", symbol="000001.SZ", as_of="2026-07-18",
        schema_version="7", status="partial",
        artifact_path=str(run_dir.resolve()), source_fingerprint="fp_x",
        model_versions={}, created_at="2026-07-18T10:00:00+00:00",
    )

    assert repo.read_dossier("array_run") is None


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_diff_runs_compares_facts_assumptions_expectations_valuation_gaps(
    tmp_path: Path,
) -> None:
    """diff_runs 必须覆盖事实/假设/预期/估值/缺口五个层面。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    dossier_a = _make_dossier("000001.SZ", "2026-07-18")
    dossier_a["industry_context"]["verified_edges"] = [
        {"claim_id": "edge_a1"}, {"claim_id": "edge_a2"},
    ]
    dossier_a["profit_bridge"]["forward_driver_assumptions"] = [{"metric": "capacity"}]
    dossier_a["valuation"]["metrics"] = {"pe": 18.0, "pb": 2.0}
    acceptance_a = _make_acceptance("000001.SZ", "2026-07-18")
    acceptance_a["degradation_reasons"] = ["forward_profit_bridge_missing", "gap_a"]

    dossier_b = _make_dossier("000001.SZ", "2026-07-18")
    dossier_b["industry_context"]["verified_edges"] = [{"claim_id": "edge_a2"}, {"claim_id": "edge_b1"}]
    dossier_b["profit_bridge"]["forward_driver_assumptions"] = [
        {"metric": "capacity"}, {"metric": "sales_volume"},
    ]
    dossier_b["valuation"]["metrics"] = {"pe": 22.0, "market_cap": 1.0e11}
    acceptance_b = _make_acceptance("000001.SZ", "2026-07-18")
    acceptance_b["degradation_reasons"] = ["gap_a", "gap_b"]

    run_dir_a = _write_run_dir(tmp_path, "value_diff_a", dossier_a, acceptance_a)
    run_dir_b = _write_run_dir(tmp_path, "value_diff_b", dossier_b, acceptance_b)
    repo.index_dossier_run(
        run_id="value_diff_a", dossier=dossier_a, acceptance=acceptance_a,
        artifact_path=str(run_dir_a.resolve()), created_at="2026-07-18T10:00:00+00:00",
    )
    repo.index_dossier_run(
        run_id="value_diff_b", dossier=dossier_b, acceptance=acceptance_b,
        artifact_path=str(run_dir_b.resolve()), created_at="2026-07-18T11:00:00+00:00",
    )

    diff = repo.diff_runs("value_diff_a", "value_diff_b")
    assert diff["degraded"] is False
    # 事实层
    assert "edge_a1" in diff["facts"]["removed"]["verified_edges"]
    assert "edge_b1" in diff["facts"]["added"]["verified_edges"]
    # 假设层
    assert diff["assumptions"]["delta"]["forward_driver_assumptions_count"] == 1
    # 预期层（数量未变）
    assert diff["expectations"]["delta"]["verified_expectations"] == 0
    # 估值层
    assert diff["valuation"]["changed"]["pe"] == {"from": 18.0, "to": 22.0}
    assert "market_cap" in diff["valuation"]["added"]
    # 缺口层
    assert "gap_b" in diff["gaps"]["added"]
    assert "forward_profit_bridge_missing" in diff["gaps"]["removed"]


def test_diff_runs_degrades_when_run_missing(tmp_path: Path) -> None:
    """diff_runs 在 run 不存在时降级，不抛异常。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path,
    )
    dossier = _make_dossier("000001.SZ", "2026-07-18")
    acceptance = _make_acceptance("000001.SZ", "2026-07-18")
    run_dir = _write_run_dir(tmp_path, "value_diff_only", dossier, acceptance)
    repo.index_dossier_run(
        run_id="value_diff_only", dossier=dossier, acceptance=acceptance,
        artifact_path=str(run_dir.resolve()), created_at="2026-07-18T10:00:00+00:00",
    )
    diff = repo.diff_runs("value_diff_only", "never_existed")
    assert diff["degraded"] is True
    assert "compare_run:run_not_found" in diff["degradation_reason"]
    assert diff["base_run"] is not None
    assert diff["compare_run"] is None


# ---------------------------------------------------------------------------
# 迁移幂等性
# ---------------------------------------------------------------------------


def test_migrate_from_disk_is_idempotent(tmp_path: Path) -> None:
    """迁移脚本重复执行必须幂等；第二次扫描全部跳过。"""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    dossier = _make_dossier("000001.SZ", "2026-07-18")
    acceptance = _make_acceptance("000001.SZ", "2026-07-18")
    _write_run_dir(runs_root, "value_mig_1", dossier, acceptance)
    _write_run_dir(runs_root, "value_mig_2", dossier, acceptance)

    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=runs_root,
    )
    first = repo.migrate_from_disk()
    assert first["scanned"] == 2
    assert first["indexed"] == 2
    assert first["skipped"] == 0
    assert first["failed"] == 0

    second = repo.migrate_from_disk()
    assert second["scanned"] == 2
    assert second["indexed"] == 0
    assert second["skipped"] == 2
    assert second["failed"] == 0

    # 数据可查询
    result = repo.list_runs(symbol="000001.SZ")
    assert result["total"] == 2


def test_migrate_from_disk_force_reindexes_corrupt_dirs(tmp_path: Path) -> None:
    """force=True 时损坏目录被重新索引为 degraded，不修改原 JSON。"""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    good_dir = _write_run_dir(
        runs_root, "value_good", _make_dossier("000001.SZ", "2026-07-18"),
        _make_acceptance("000001.SZ", "2026-07-18"),
    )
    bad_dir = runs_root / "value_bad"
    bad_dir.mkdir()
    (bad_dir / "dossier.json").write_text("corrupt{", encoding="utf-8")

    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=runs_root,
    )
    first = repo.migrate_from_disk()
    assert first["scanned"] == 2
    assert first["indexed"] == 1
    assert first["failed"] == 1
    assert first["degraded"] == 1

    # 强制重写：损坏目录被写入索引为 degraded
    second = repo.migrate_from_disk(force=True)
    assert second["indexed"] == 1  # 好的目录重写
    assert second["failed"] == 1
    assert second["degraded"] == 1

    degraded_record = repo.get_run("value_bad")
    assert degraded_record is not None
    assert degraded_record["status"] == "degraded"
    assert degraded_record["payload"]["degradation_reason"] == "dossier_or_acceptance_missing_or_corrupt"
    # 原 JSON 未被修改
    assert (bad_dir / "dossier.json").read_text(encoding="utf-8") == "corrupt{"
    # 好的目录仍可读取
    assert repo.read_dossier("value_good") is not None


def test_migrate_from_disk_handles_missing_root(tmp_path: Path) -> None:
    """runs_root 不存在时返回降级统计，不抛异常。"""
    repo = ResearchRunRepository(
        db_path=tmp_path / "runs.db", runs_root=tmp_path / "missing",
    )
    stats = repo.migrate_from_disk()
    assert stats["scanned"] == 0
    assert stats["reason"] == "runs_root_missing"


# ---------------------------------------------------------------------------
# 一致性
# ---------------------------------------------------------------------------


def test_restart_preserves_index_and_artifacts(tmp_path: Path) -> None:
    """重启后（重新打开 DB）索引与磁盘产物必须一致。"""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    db_path = tmp_path / "runs.db"
    dossier = _make_dossier("000001.SZ", "2026-07-18")
    acceptance = _make_acceptance("000001.SZ", "2026-07-18")
    run_dir = _write_run_dir(runs_root, "value_restart", dossier, acceptance)

    repo1 = ResearchRunRepository(db_path=db_path, runs_root=runs_root)
    repo1.index_dossier_run(
        run_id="value_restart", dossier=dossier, acceptance=acceptance,
        artifact_path=str(run_dir.resolve()), created_at="2026-07-18T10:00:00+00:00",
    )

    # 模拟重启：新进程打开同一 DB
    repo2 = ResearchRunRepository(db_path=db_path, runs_root=runs_root)
    record = repo2.get_run("value_restart")
    assert record is not None
    assert record["symbol"] == "000001.SZ"
    bundle = repo2.read_run_bundle("value_restart")
    assert bundle["degraded"] is False
    assert bundle["dossier"]["symbol"] == "000001.SZ"


def test_source_fingerprint_stable_for_same_inputs(tmp_path: Path) -> None:
    """相同 source_snapshots 必须产生相同指纹。"""
    snapshots = {
        "profile": {"status": "ok", "provider": "fake", "as_of": "2026-07-18", "records": [{}]},
        "daily_bars": {"status": "ok", "provider": "fake", "as_of": "2026-07-18", "records": [{}, {}]},
    }
    fp1 = _compute_source_fingerprint(snapshots)
    fp2 = _compute_source_fingerprint(dict(snapshots))
    assert fp1 == fp2
    # 顺序不同也应一致
    fp3 = _compute_source_fingerprint(dict(reversed(list(snapshots.items()))))
    assert fp1 == fp3
    # 内容变化时指纹变化
    snapshots2 = json.loads(json.dumps(snapshots))
    snapshots2["profile"]["records"] = [{}, {}]
    assert _compute_source_fingerprint(snapshots2) != fp1


def test_same_input_repeated_run_creates_new_version_not_overwrite(
    tmp_path: Path,
) -> None:
    """同输入重复运行必须生成新 run_id，不覆盖旧版本。"""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    db_path = tmp_path / "runs.db"
    repo = ResearchRunRepository(db_path=db_path, runs_root=runs_root)
    dossier = _make_dossier("000001.SZ", "2026-07-18")
    acceptance = _make_acceptance("000001.SZ", "2026-07-18")

    run_dir_a = _write_run_dir(runs_root, "value_dup_a", dossier, acceptance)
    run_dir_b = _write_run_dir(runs_root, "value_dup_b", dossier, acceptance)
    repo.index_dossier_run(
        run_id="value_dup_a", dossier=dossier, acceptance=acceptance,
        artifact_path=str(run_dir_a.resolve()), created_at="2026-07-18T10:00:00+00:00",
    )
    repo.index_dossier_run(
        run_id="value_dup_b", dossier=dossier, acceptance=acceptance,
        artifact_path=str(run_dir_b.resolve()), created_at="2026-07-18T11:00:00+00:00",
    )

    result = repo.list_runs(symbol="000001.SZ")
    assert result["total"] == 2
    # 两个 run_id 都存在
    ids = {item["run_id"] for item in result["items"]}
    assert ids == {"value_dup_a", "value_dup_b"}
    # 源指纹相同但 run_id 不同
    assert result["items"][0]["source_fingerprint"] == result["items"][1]["source_fingerprint"]


# ---------------------------------------------------------------------------
# 接入 service.py
# ---------------------------------------------------------------------------


def test_service_run_writes_to_repository(tmp_path: Path) -> None:
    """CompanyValueResearchService.run 必须写入运行仓储索引。"""
    import pandas as pd

    runs_root = tmp_path / "runs"

    class FakeRegistry:
        def __init__(self) -> None:
            self.logs: list[dict] = []

        def get_stock_basic_info(self, symbols=None):
            return pd.DataFrame([{
                "symbol": symbols[0], "stock_name": "测试公司", "industry": "测试行业",
                "main_business": "测试产品", "provider": "fake_profile",
            }])

        def get_daily_bars(self, start_date, end_date, symbols=None):
            return pd.DataFrame([{
                "trade_date": "2026-06-29", "symbol": symbols[0],
                "close": 10.0, "volume": 1000, "amount": 10000, "provider": "fake_bars",
            }])

        def get_earnings_forecasts(self, start_date, end_date, symbols=None):
            return pd.DataFrame()

        def get_announcements(self, code, count=10):
            return [{"title": "公告", "date": "2026-06-29", "source": "fake"}]

        def smart_quote(self, code):
            return {"code": code, "price": 12.9, "pe": 0, "source": "fake_quote"}

    from core.research.value_chain.service import CompanyValueResearchService

    result = CompanyValueResearchService(registry=FakeRegistry()).run(
        "000001.SZ", as_of="2026-06-30", output_root=runs_root,
    )
    run_id = result["run_id"]
    assert result["run_dir"]

    # 索引库位于 output_root/research_value_runs.db
    repo = ResearchRunRepository(
        db_path=runs_root / "research_value_runs.db", runs_root=runs_root,
    )
    record = repo.get_run(run_id)
    assert record is not None
    assert record["symbol"] == "000001.SZ"
    assert record["as_of"] == "2026-06-30"
    assert record["status"] in {"ok", "partial"}
    # 磁盘产物可读
    bundle = repo.read_run_bundle(run_id)
    assert bundle["degraded"] is False
    assert bundle["dossier"]["symbol"] == "000001.SZ"
