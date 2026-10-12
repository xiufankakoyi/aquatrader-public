"""Local data health inspection and report generation.

数据源角色说明（基于 AquaTrader 收敛后的数据架构）：
- ``lancedb``: 唯一行情与因子主数据源，``role=primary_market_store`` 且 ``blocking=True``。
- ``sqlite``: 仅作为元数据库（策略/组合/任务/设置等），``role=metadata_store`` 且 ``blocking=False``。
- ``parquet``: optional snapshot/export，``role=optional_snapshot`` 且 ``blocking=False``。
- ``parquet_cache``: 查询加速缓存，``role=query_acceleration_cache`` 且 ``blocking=False``。
- ``matrix_cache``/``factor_matrix_cache``: 回测加速缓存，``role=backtest_cache`` 且 ``blocking=False``。
- ``spider_data``/``industry``: 派生证据库，``role=derived_evidence`` 且 ``blocking=False``。

总体状态以 ``blocking=True`` 的数据集为裁决依据；``blocking=False`` 的数据集只产生
提示信息，不会拖垮主流程。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
REPORT_DIR = DATA_DIR / "reports"


# 数据集角色常量，集中维护以便 README / 报告 / 路由统一引用
ROLE_PRIMARY_MARKET_STORE = "primary_market_store"
ROLE_METADATA_STORE = "metadata_store"
ROLE_OPTIONAL_SNAPSHOT = "optional_snapshot"
ROLE_BACKTEST_CACHE = "backtest_cache"
ROLE_DERIVED_EVIDENCE = "derived_evidence"

# 各数据集的语义元数据：role / blocking / 空缺时的人类可读提示
DATASET_SEMANTICS: dict[str, dict[str, Any]] = {
    "lancedb": {
        "role": ROLE_PRIMARY_MARKET_STORE,
        "blocking": True,
        "missing_hint": "LanceDB 不可用：唯一行情主源缺失，请先执行 scripts/update/update_market_data_incremental.py",
    },
    "sqlite": {
        "role": ROLE_METADATA_STORE,
        "blocking": False,
        "missing_hint": "SQLite 元数据为空：策略/组合/任务等元数据未初始化，不影响行情主流程",
    },
    "parquet": {
        "role": ROLE_OPTIONAL_SNAPSHOT,
        "blocking": False,
        "missing_hint": "Parquet 快照缺失：仅用于离线导出，不参与默认更新闭环",
    },
    "matrix_cache": {
        "role": ROLE_BACKTEST_CACHE,
        "blocking": False,
        "missing_hint": "回测缓存缺失，可点击重建",
    },
    "factor_matrix_cache": {
        "role": ROLE_BACKTEST_CACHE,
        "blocking": False,
        "missing_hint": "factor_matrix_cache 缺失：执行 --target factors 重建",
    },
    "spider_data": {
        "role": ROLE_DERIVED_EVIDENCE,
        "blocking": False,
        "missing_hint": "spider_data 缺失：研究证据库为空",
    },
    "industry": {
        "role": ROLE_DERIVED_EVIDENCE,
        "blocking": False,
        "missing_hint": "industry 本地证据为空",
    },
    "parquet_cache": {
        "role": "query_acceleration_cache",
        "blocking": False,
        "missing_hint": "Parquet 查询加速缓存缺失：单股查询将走 LanceDB（较慢），执行 python scripts/update/build_parquet_partitions.py --full 构建",
    },
}


@dataclass
class DatasetHealth:
    name: str
    path: str
    exists: bool
    row_count: int | None
    earliest_date: str | None
    latest_date: str | None
    field_count: int | None
    missing_required_fields: list[str]
    missing_dates: list[str]
    source: str
    role: str = ""
    blocking: bool = False
    status: str = "unknown"
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def _weekday_gaps(values: Iterable[Any], limit: int = 100) -> list[str]:
    dates: set[date] = set()
    for value in values:
        text = _date_text(value)
        if not text:
            continue
        try:
            dates.add(datetime.fromisoformat(text).date())
        except ValueError:
            continue
    if len(dates) < 2:
        return []
    current = min(dates)
    end = max(dates)
    missing: list[str] = []
    while current <= end and len(missing) < limit:
        if current.weekday() < 5 and current not in dates:
            missing.append(current.isoformat())
        current += timedelta(days=1)
    return missing


def _semantic_for(name: str) -> tuple[str, bool, str]:
    """读取数据集的 role/blocking/missing_hint 元数据。"""

    meta = DATASET_SEMANTICS.get(name, {})
    return (
        meta.get("role", ""),
        bool(meta.get("blocking", False)),
        meta.get("missing_hint", ""),
    )


def _inspect_sqlite(path: Path) -> DatasetHealth:
    # SQLite 仅作为元数据库，stock_daily 等行情表不再由它承担。
    required: dict[str, set[str]] = {}
    role, blocking, missing_hint = _semantic_for("sqlite")
    if not path.exists():
        return DatasetHealth(
            "sqlite", str(path), False, 0, None, None, 0,
            [], [], "local_sqlite", role, blocking,
            "metadata_empty", missing_hint,
            extra={"note": "SQLite 文件不存在；元数据未初始化不影响主行情"},
        )
    row_count = 0
    field_count = 0
    earliest = None
    latest = None
    missing_fields: list[str] = []
    message = "SQLite 元数据库可访问（仅元数据，不含行情）"
    status = "ok"
    try:
        with sqlite3.connect(path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            # 仅元数据表（策略/组合/任务/系统设置）。行情表 stock_daily 不应再出现。
            market_tables = {"stock_daily", "stock_daily_legacy"}
            market_present = sorted(tables & market_tables)
            row_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            )
            if market_present:
                # 如果数据库里仍残留行情表，提示一次历史遗留，不视为阻塞
                message = (
                    f"SQLite 中残留行情表 {market_present}，请改用 LanceDB 作为行情主源"
                )
                status = "warning"
            elif row_count == 0:
                status = "metadata_empty"
                message = missing_hint
    except Exception as exc:
        status = "error"
        message = f"SQLite 检查失败: {exc}"
    return DatasetHealth(
        "sqlite",
        str(path),
        True,
        row_count,
        None,
        None,
        field_count,
        missing_fields,
        [],
        "local_sqlite",
        role,
        blocking,
        status,
        message,
    )


def _inspect_parquet(path: Path) -> DatasetHealth:
    # Parquet 不再作为行情主源，stale 只产生 snapshot_stale 提示，不阻塞主流程。
    required = {"stock_code", "trade_date", "open", "high", "low", "close"}
    role, blocking, missing_hint = _semantic_for("parquet")
    if not path.exists():
        return DatasetHealth(
            "parquet", str(path), False, 0, None, None, 0,
            [], [], "local_parquet", role, blocking,
            "snapshot_stale", missing_hint,
            extra={"note": "Parquet 仅作 optional snapshot；缺失不阻塞主流程"},
        )
    files = sorted(path.glob("*.parquet"))
    primary = path / "stock_daily.parquet"
    target = primary if primary.exists() else (files[0] if files else None)
    if target is None:
        return DatasetHealth(
            "parquet", str(path), True, 0, None, None, 0,
            [], [], "local_parquet", role, blocking,
            "snapshot_stale", missing_hint,
            extra={"note": "Parquet 目录中无文件，可手动执行 --target parquet-snapshot 导出"},
        )

    row_count = 0
    field_count = 0
    earliest = None
    latest = None
    missing_dates: list[str] = []
    missing_fields: list[str] = []
    status = "ok"
    message = f"快照文件: {target.name}（optional snapshot，不阻塞主流程）"
    try:
        import pyarrow.parquet as pq

        metadata = pq.read_metadata(target)
        row_count = metadata.num_rows
        fields = set(metadata.schema.names)
        field_count = len(fields)
        missing_fields = sorted(required - fields)
        date_field = "trade_date" if "trade_date" in fields else (
            "date" if "date" in fields else None
        )
        if date_field and row_count:
            import duckdb

            escaped = str(target).replace("'", "''")
            rows = duckdb.connect(database=":memory:").execute(
                f'SELECT DISTINCT CAST("{date_field}" AS DATE) AS d '
                f"FROM read_parquet('{escaped}') WHERE \"{date_field}\" IS NOT NULL "
                "ORDER BY d"
            ).fetchall()
            normalized = [_date_text(row[0]) for row in rows]
            if normalized:
                earliest = normalized[0]
                latest = normalized[-1]
                missing_dates = _weekday_gaps(normalized)
        if missing_fields or row_count == 0:
            # 字段缺失或空文件：仅视为 snapshot_stale，不阻塞
            status = "snapshot_stale"
            message = "Parquet 快照字段缺失或为空，建议重新导出"
    except Exception as exc:
        status = "snapshot_stale"
        message = f"Parquet 快照读取失败: {exc}"
    return DatasetHealth(
        "parquet",
        str(path),
        True,
        row_count,
        earliest,
        latest,
        field_count,
        missing_fields,
        missing_dates,
        "local_parquet",
        role,
        blocking,
        status,
        message,
    )


def _inspect_lancedb(path: Path) -> DatasetHealth:
    # LanceDB 是唯一行情与因子主数据源；缺失或空库会触发阻塞警告。
    required = {"stock_code", "trade_date", "close"}
    role, blocking, missing_hint = _semantic_for("lancedb")
    if not path.exists():
        return DatasetHealth(
            "lancedb", str(path), False, 0, None, None, 0,
            sorted(required), [], "local_lancedb", role, blocking,
            "error", missing_hint,
            extra={"blocking_reason": "行情主源缺失"},
        )
    row_count = 0
    field_count = 0
    earliest = None
    latest = None
    missing_fields: list[str] = []
    status = "unknown"
    message = "LanceDB 目录存在，未读取到可用表"
    try:
        import lancedb
        import pyarrow.compute as pc

        db = lancedb.connect(str(path))
        table_names = list(db.list_tables().tables)
        target_name = "daily_ohlcv" if "daily_ohlcv" in table_names else (
            table_names[0] if table_names else None
        )
        if target_name:
            table = db.open_table(target_name)
            fields = {field.name for field in table.schema}
            field_count = len(fields)
            missing_fields = sorted(required - fields)
            row_count = int(table.count_rows())
            if "trade_date" in fields and row_count:
                dates = (
                    table.to_lance()
                    .scanner(columns=["trade_date"])
                    .to_table()
                    .column("trade_date")
                )
                earliest = _date_text(pc.min(dates).as_py())
                latest = _date_text(pc.max(dates).as_py())
            if missing_fields or row_count == 0:
                status = "warning" if missing_fields else "error"
                message = (
                    f"LanceDB 表 {target_name} 字段缺失" if missing_fields
                    else f"LanceDB 表 {target_name} 为空，请先执行 update_market_data_incremental"
                )
            else:
                status = "ok"
                message = f"LanceDB 主行情表: {target_name}（最新 {latest}）"
    except Exception as exc:
        status = "warning"
        message = f"LanceDB 元数据读取失败: {exc}"
    return DatasetHealth(
        "lancedb",
        str(path),
        True,
        row_count,
        earliest,
        latest,
        field_count,
        missing_fields,
        [],
        "local_lancedb",
        role,
        blocking,
        status,
        message,
    )


def _inspect_parquet_cache(path: Path) -> DatasetHealth:
    """检查 Parquet 查询加速缓存的健康状态。"""

    role, blocking, missing_hint = _semantic_for("parquet_cache")
    manifest_path = path / "_manifest.json"

    if not path.exists() or not manifest_path.exists():
        return DatasetHealth(
            "parquet_cache", str(path), False, 0, None, None, 0,
            [], [], "local_parquet_cache", role, blocking,
            "cache_missing", missing_hint,
            extra={"note": "Parquet 查询加速缓存未构建，单股查询将走 LanceDB"},
        )

    # 读取 manifest
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return DatasetHealth(
            "parquet_cache", str(path), True, 0, None, None, 0,
            [], [], "local_parquet_cache", role, blocking,
            "cache_error", f"manifest 读取失败: {exc}",
        )

    cache_ltd = manifest.get("latest_trade_date", "")
    source_ltd = manifest.get("source_lancedb_latest_trade_date", "")
    daily_info = manifest.get("daily_ohlcv", {})
    factors_info = manifest.get("factors", {})
    total_rows = daily_info.get("total_rows", 0) + factors_info.get("total_rows", 0)
    stock_count = max(daily_info.get("stock_count", 0), factors_info.get("stock_count", 0))

    # 检查是否 stale：比较 manifest 的 latest_trade_date 与 LanceDB 的
    is_stale = False
    stale_message = ""
    lancedb_path = DATA_DIR / "lancedb"
    if lancedb_path.exists():
        try:
            import lancedb
            import pyarrow.compute as pc

            db = lancedb.connect(str(lancedb_path))
            tables = list(db.list_tables().tables)
            if "daily_ohlcv" in tables:
                table = db.open_table("daily_ohlcv")
                arrow = table.to_lance().scanner(columns=["trade_date"]).to_table()
                lancedb_ltd = _date_text(pc.max(arrow.column("trade_date")).as_py())
                if cache_ltd and lancedb_ltd and cache_ltd < lancedb_ltd:
                    is_stale = True
                    stale_message = (
                        f"Parquet cache 过期: cache={cache_ltd}, lancedb={lancedb_ltd}，"
                        "请执行 --incremental 重建"
                    )
        except Exception:
            pass

    # 统计分片文件数
    daily_dir = path / "daily_ohlcv"
    factors_dir = path / "factors"
    daily_files = len(list(daily_dir.glob("*.parquet"))) if daily_dir.exists() else 0
    factors_files = len(list(factors_dir.glob("*.parquet"))) if factors_dir.exists() else 0

    if is_stale:
        status = "cache_stale"
        message = stale_message
    elif total_rows == 0:
        status = "cache_empty"
        message = "Parquet cache 为空，请执行构建"
    else:
        status = "ok"
        message = f"Parquet 查询加速缓存正常: {stock_count} 只股票, cache最新={cache_ltd}"

    return DatasetHealth(
        "parquet_cache",
        str(path),
        True,
        total_rows,
        None,
        cache_ltd or None,
        None,
        [],
        [],
        "local_parquet_cache",
        role,
        blocking,
        status,
        message,
        extra={
            "daily_ohlcv_files": daily_files,
            "factors_files": factors_files,
            "stock_count": stock_count,
            "cache_stale": is_stale,
            "manifest_version": manifest.get("version", 0),
        },
    )


def _inspect_directory(name: str, path: Path, source: str) -> DatasetHealth:
    role, blocking, missing_hint = _semantic_for(name)
    exists = path.exists()
    file_count = 0
    if exists:
        try:
            file_count = sum(1 for item in path.rglob("*") if item.is_file())
        except OSError:
            file_count = 0
    if exists and file_count:
        status = "ok"
        message = f"{name} 目录文件数: {file_count}"
    elif exists:
        status = "backtest_cache_missing" if role == ROLE_BACKTEST_CACHE else (
            "metadata_empty" if role == ROLE_METADATA_STORE else "warning"
        )
        message = missing_hint or f"{name} 目录为空"
    else:
        status = "backtest_cache_missing" if role == ROLE_BACKTEST_CACHE else (
            "metadata_empty" if role == ROLE_METADATA_STORE else "warning"
        )
        message = missing_hint or f"{name} 目录不存在"
    return DatasetHealth(
        name,
        str(path),
        exists,
        file_count,
        None,
        None,
        None,
        [],
        [],
        source,
        role,
        blocking,
        status,
        message,
    )


def build_data_health_report(write_files: bool = True) -> dict[str, Any]:
    """汇总各数据源健康状态并落盘。

    总体状态以 ``blocking=True`` 的数据集为裁决依据：
    - 任一 ``blocking=True`` 数据集处于 ``error`` -> 总体 error
    - 任一 ``blocking=True`` 数据集处于 ``warning`` -> 总体 warning
    - 其它情况总体 ``ok``，``blocking=False`` 的提示信息仅作为 informational。
    """

    datasets = [
        _inspect_sqlite(DATA_DIR / "database" / "stock_data.db"),
        _inspect_lancedb(DATA_DIR / "lancedb"),
        _inspect_parquet_cache(DATA_DIR / "parquet_cache"),
        _inspect_parquet(DATA_DIR / "parquet_data"),
        _inspect_directory(
            "factor_matrix_cache", DATA_DIR / "factor_matrix_cache", "local_cache"
        ),
        _inspect_directory("matrix_cache", DATA_DIR / "matrix_cache", "local_cache"),
        _inspect_directory("spider_data", DATA_DIR / "spider_data", "local_spider"),
        _inspect_directory("industry", DATA_DIR / "industry", "local_structured_evidence"),
    ]
    overall = _compute_overall_status(datasets)
    blocking_summary = _summarize_blocking(datasets)
    informational: list[dict[str, Any]] = [
        {
            "name": item.name,
            "role": item.role,
            "status": item.status,
            "message": item.message,
        }
        for item in datasets
        if not item.blocking
    ]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": overall,
        "source": "local_structured_data",
        "message": "数据健康检查完成",
        "blocking": blocking_summary,
        "informational": informational,
        "datasets": [asdict(item) for item in datasets],
    }
    if write_files:
        write_data_health_reports(report)
    return report


def _compute_overall_status(datasets: Iterable[DatasetHealth]) -> str:
    """仅以 blocking=True 的数据集决定总体状态。"""

    blocking = [item for item in datasets if item.blocking]
    if not blocking:
        return "ok"
    if any(item.status == "error" for item in blocking):
        return "error"
    if any(item.status in {"warning", "unknown"} for item in blocking):
        return "warning"
    return "ok"


def _summarize_blocking(datasets: Iterable[DatasetHealth]) -> list[dict[str, Any]]:
    """汇总 blocking=True 数据集的状态，便于快速判断。"""

    return [
        {
            "name": item.name,
            "role": item.role,
            "status": item.status,
            "latest_date": item.latest_date,
            "row_count": item.row_count,
            "message": item.message,
        }
        for item in datasets
        if item.blocking
    ]


def write_data_health_reports(report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / "data_health_latest.json"
    md_path = REPORT_DIR / "data_health_latest.md"
    map_path = PROJECT_ROOT / "DATA_MAP.generated.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rows = [
        "# 数据健康报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 总体状态：`{report['status']}`",
        f"- 数据来源：`{report['source']}`",
        "",
        "## 数据源角色（按 blocking 划分）",
        "",
        "- `blocking=true` 的数据集决定总体状态；`blocking=false` 只作为提示。",
        "",
        "| 数据集 | 角色 | blocking | 状态 | 行数/文件数 | 最早日期 | 最新日期 | 缺失字段 | 缺失日期数 |",
        "| --- | --- | :---: | --- | ---: | --- | --- | --- | ---: |",
    ]
    for item in report["datasets"]:
        rows.append(
            "| {name} | {role} | {blocking} | {status} | {rows} | {earliest} | {latest} | {missing} | {gaps} |".format(
                name=item["name"],
                role=item.get("role", ""),
                blocking="true" if item.get("blocking") else "false",
                status=item["status"],
                rows=item["row_count"] if item["row_count"] is not None else "N/A",
                earliest=item["earliest_date"] or "N/A",
                latest=item["latest_date"] or "N/A",
                missing=", ".join(item["missing_required_fields"]) or "无",
                gaps=len(item["missing_dates"]),
            )
        )
    informational = report.get("informational") or []
    if informational:
        rows.extend(
            [
                "",
                "## 提示性信息（不阻塞）",
                "",
                "| 数据集 | 状态 | 提示 |",
                "| --- | --- | --- |",
            ]
        )
        for item in informational:
            rows.append(
                f"| {item['name']} | `{item['status']}` | {item['message']} |"
            )
    md_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    map_rows = [
        "# AquaTrade 数据地图（自动生成）",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        "| 数据集 | 角色 | blocking | 路径 | 来源 | 状态 | 字段数 |",
        "| --- | --- | :---: | --- | --- | --- | ---: |",
    ]
    for item in report["datasets"]:
        map_rows.append(
            f"| {item['name']} | `{item.get('role', '')}` | "
            f"{'true' if item.get('blocking') else 'false'} | "
            f"`{item['path']}` | `{item['source']}` | "
            f"`{item['status']}` | {item['field_count'] if item['field_count'] is not None else 'N/A'} |"
        )
    map_path.write_text("\n".join(map_rows) + "\n", encoding="utf-8")
