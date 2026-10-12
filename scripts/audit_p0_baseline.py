"""P0 基线审计脚本（只读）。

按计划 P0.1 要求：
1. 审计 research_control.db / earnings.db / expectations.db / candidates.db / quant.db / theme.db
2. 记录每张正式表的记录数、schema version 和最近更新时间
3. 保存 git status --short，不自动清理工作区
4. 运行 pytest 与 npm run build，记录退出码
5. 产物写入 data/research_value_runs/p0_baseline_<timestamp>/，不覆盖历史目录

严格遵守：
- 所有数据库操作只读，绝不写入或迁移
- 不修改工作区文件
- 不自动清理 git 未提交改动
- 出错时降级为 existing_failure，不阻塞其他审计项
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_CANDIDATES = [
    "data/research_control/research_control.db",
    "data/research_control/earnings.db",
    "data/research_control/expectations.db",
    "data/research_control/candidates.db",
    "data/research_control/adaptation.db",
    "data/quant/quant.db",
    "data/theme_control/theme.db",
    "data/theme_control/membership_evidence.db",
    "data/theme_control/sector_policy_versions.db",
    "data/research_control.db",
    "data/research_events.db",
    "data/outcome_tracker.db",
]


@dataclass
class TableAudit:
    """单张表的审计结果。"""

    table_name: str
    row_count: int = 0
    schema_version: str = ""
    latest_updated_at: str = ""
    columns: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class DatabaseAudit:
    """单个数据库的审计结果。"""

    db_path: str
    exists: bool = False
    size_bytes: int = 0
    tables: list[TableAudit] = field(default_factory=list)
    error: str = ""


@dataclass
class BaselineReport:
    """P0 基线报告。"""

    generated_at: str
    project_root: str
    databases: list[DatabaseAudit] = field(default_factory=list)
    git_status: str = ""
    git_branch: str = ""
    pytest_summary: dict[str, Any] = field(default_factory=dict)
    npm_build_summary: dict[str, Any] = field(default_factory=dict)
    api_routes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def utc_now_iso() -> str:
    """返回当前 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _audit_table(conn: sqlite3.Connection, table_name: str) -> TableAudit:
    """审计单张表（只读）。

    Args:
        conn: SQLite 连接，已开启 row_factory。
        table_name: 表名。

    Returns:
        TableAudit 对象；出错时 error 字段填充原因。
    """
    audit = TableAudit(table_name=table_name)
    try:
        cursor = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        audit.row_count = _safe_int(cursor.fetchone()[0])
    except sqlite3.Error as exc:
        audit.error = f"count_failed: {exc}"
        return audit
    try:
        cursor = conn.execute(f'PRAGMA table_info("{table_name}")')
        audit.columns = [str(row[1]) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        if not audit.error:
            audit.error = f"schema_failed: {exc}"
    # 查找最近 updated_at / created_at 字段
    for column in ("updated_at", "created_at", "finished_at", "resolved_at", "fetched_at"):
        if column in audit.columns:
            try:
                cursor = conn.execute(
                    f'SELECT MAX("{column}") FROM "{table_name}"'
                )
                row = cursor.fetchone()
                if row and row[0]:
                    audit.latest_updated_at = str(row[0])
                    break
            except sqlite3.Error:
                continue
    return audit


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    """列出所有用户表（只读）。"""
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [str(row[0]) for row in cursor.fetchall()]
    except sqlite3.Error:
        return []


def _read_schema_version(conn: sqlite3.Connection) -> str:
    """读取 schema_meta 表中的 schema_version（只读）。"""
    try:
        cursor = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )
        row = cursor.fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def audit_database(db_path: Path) -> DatabaseAudit:
    """审计单个数据库（只读，不修改）。

    Args:
        db_path: 数据库文件路径。

    Returns:
        DatabaseAudit 对象。
    """
    audit = DatabaseAudit(db_path=str(db_path))
    if not db_path.exists():
        audit.error = "db_not_found"
        return audit
    audit.exists = True
    audit.size_bytes = db_path.stat().st_size
    try:
        # uri=true + mode=ro 严格只读
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        audit.error = f"open_failed: {exc}"
        return audit
    try:
        audit.tables = [
            _audit_table(conn, table_name)
            for table_name in _list_tables(conn)
        ]
        schema_version = _read_schema_version(conn)
        if schema_version:
            for table in audit.tables:
                if not table.schema_version:
                    table.schema_version = schema_version
    finally:
        conn.close()
    return audit


def collect_git_status() -> tuple[str, str]:
    """读取 git status 与当前分支（不修改工作区）。"""
    try:
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        status_text = status.stdout if status.returncode == 0 else f"ERROR: {status.stderr}"
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        status_text = f"git_status_failed: {exc}"
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        branch_text = branch.stdout.strip() if branch.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError):
        branch_text = ""
    return status_text, branch_text


def run_pytest_summary(target: str = "tests") -> dict[str, Any]:
    """运行 pytest 子集，捕获退出码与汇总行（不修改工作区）。

    Args:
        target: pytest 收集目录，默认 tests；支持空格分隔多个目录（如 "test tests"）。

    Returns:
        汇总字典：{command, exit_code, stdout_tail, stderr_tail, duration_sec}
    """
    started = datetime.now(timezone.utc)
    # P0 复验整改：target 按空格拆分为多个路径，避免 "test tests" 被当成单一路径
    target_paths = target.split() if isinstance(target, str) else list(target)
    command = [sys.executable, "-m", "pytest", "-q", "--no-header", *target_paths]
    try:
        proc = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        exit_code = proc.returncode
        stdout_tail = proc.stdout[-4000:]
        stderr_tail = proc.stderr[-2000:]
    except subprocess.TimeoutExpired:
        exit_code = -1
        stdout_tail = "TIMEOUT_AFTER_900s"
        stderr_tail = ""
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        exit_code = -2
        stdout_tail = ""
        stderr_tail = str(exc)
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    return {
        "command": " ".join(command),
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "duration_sec": round(duration, 2),
    }


def run_npm_build_summary() -> dict[str, Any]:
    """运行 npm run build，捕获退出码与汇总（不修改工作区）。"""
    npm_exec = shutil.which("npm")
    if not npm_exec:
        return {
            "command": "npm run build",
            "exit_code": -3,
            "stdout_tail": "",
            "stderr_tail": "npm_not_found",
            "duration_sec": 0.0,
        }
    started = datetime.now(timezone.utc)
    command = [npm_exec, "run", "build"]
    try:
        proc = subprocess.run(
            command,
            cwd=PROJECT_ROOT / "myapp",
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        exit_code = proc.returncode
        stdout_tail = proc.stdout[-4000:]
        stderr_tail = proc.stderr[-2000:]
    except subprocess.TimeoutExpired:
        exit_code = -1
        stdout_tail = "TIMEOUT_AFTER_600s"
        stderr_tail = ""
    except subprocess.SubprocessError as exc:
        exit_code = -2
        stdout_tail = ""
        stderr_tail = str(exc)
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    return {
        "command": " ".join(command),
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "duration_sec": round(duration, 2),
    }


def collect_api_routes() -> list[str]:
    """枚举 research_routes 中已注册的 API 路径（只读，不导入运行时）。"""
    routes_path = PROJECT_ROOT / "server" / "routes" / "research_routes.py"
    if not routes_path.exists():
        return []
    try:
        text = routes_path.read_text(encoding="utf-8")
    except OSError:
        return []
    routes: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "@research_bp." in stripped and ("get(" in stripped or "post(" in stripped or "route(" in stripped):
            routes.append(stripped)
    return routes


def write_report(report: BaselineReport, output_root: Path) -> Path:
    """将基线报告写入 output_root 下的 baseline.json。

    Args:
        report: 基线报告对象。
        output_root: 输出目录；不存在时创建。

    Returns:
        写入文件路径。
    """
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / "baseline.json"
    git_status_file = output_root / "git_status.txt"
    pytest_log_file = output_root / "pytest.log"
    npm_build_log_file = output_root / "npm_build.log"
    target.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    git_status_file.write_text(report.git_status, encoding="utf-8")
    pytest_log_file.write_text(
        f"COMMAND: {report.pytest_summary.get('command', '')}\n"
        f"EXIT: {report.pytest_summary.get('exit_code', '')}\n"
        f"DURATION: {report.pytest_summary.get('duration_sec', '')}s\n\n"
        f"STDOUT:\n{report.pytest_summary.get('stdout_tail', '')}\n\n"
        f"STDERR:\n{report.pytest_summary.get('stderr_tail', '')}\n",
        encoding="utf-8",
    )
    npm_build_log_file.write_text(
        f"COMMAND: {report.npm_build_summary.get('command', '')}\n"
        f"EXIT: {report.npm_build_summary.get('exit_code', '')}\n"
        f"DURATION: {report.npm_build_summary.get('duration_sec', '')}s\n\n"
        f"STDOUT:\n{report.npm_build_summary.get('stdout_tail', '')}\n\n"
        f"STDERR:\n{report.npm_build_summary.get('stderr_tail', '')}\n",
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    """脚本入口。

    Args:
        argv: 命令行参数（仅用于测试）。

    Returns:
        退出码；0 表示审计成功完成，>0 表示存在 failed 项。
    """
    parser = argparse.ArgumentParser(description="P0 基线审计（只读）")
    parser.add_argument(
        "--output-root",
        default="data/research_value_runs",
        help="基线报告写入根目录，默认 data/research_value_runs",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="跳过 pytest 子集运行（用于快速重跑）",
    )
    parser.add_argument(
        "--skip-npm-build",
        action="store_true",
        help="跳过 npm run build 运行（用于快速重跑）",
    )
    parser.add_argument(
        "--pytest-target",
        default="tests",
        help="pytest 收集目录，默认 tests",
    )
    args = parser.parse_args(argv)

    report = BaselineReport(
        generated_at=utc_now_iso(),
        project_root=str(PROJECT_ROOT),
    )

    for relative in DEFAULT_DB_CANDIDATES:
        db_path = PROJECT_ROOT / relative
        try:
            report.databases.append(audit_database(db_path))
        except Exception as exc:  # pragma: no cover - 防御性
            report.errors.append(f"db_audit_failed:{relative}:{exc}")
            report.databases.append(DatabaseAudit(
                db_path=str(db_path),
                error=f"audit_exception: {exc}",
            ))

    git_status, git_branch = collect_git_status()
    report.git_status = git_status
    report.git_branch = git_branch

    report.api_routes = collect_api_routes()

    if not args.skip_pytest:
        report.pytest_summary = run_pytest_summary(args.pytest_target)
    else:
        report.pytest_summary = {"skipped": True}

    if not args.skip_npm_build:
        report.npm_build_summary = run_npm_build_summary()
    else:
        report.npm_build_summary = {"skipped": True}

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_root = PROJECT_ROOT / args.output_root / f"p0_baseline_{timestamp}"
    target = write_report(report, output_root)
    print(f"[p0_baseline] report written to {target}")
    failed = [db for db in report.databases if not db.exists]
    if failed:
        print(f"[p0_baseline] WARNING: {len(failed)} database(s) not found:")
        for db in failed:
            print(f"  - {db.db_path}")
    if report.pytest_summary.get("exit_code", 0) not in (0, 5):
        print(f"[p0_baseline] pytest exit_code={report.pytest_summary.get('exit_code')}")
    if report.npm_build_summary.get("exit_code", 0) != 0:
        print(f"[p0_baseline] npm run build exit_code={report.npm_build_summary.get('exit_code')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
