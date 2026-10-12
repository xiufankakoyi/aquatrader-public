"""研究操作系统健康检查器。

按 P3.8 生产要求：
- SQLite 一致性检查（integrity_check、外键、schema_meta）
- 文件产物校验和（SHA-256 清单）
- provider 可用性探测（不实际发起网络请求，仅检查最近指标）

设计原则：
- 降级处理：单项检查失败不阻断整体
- 不依赖外部服务；所有检查可独立运行
- 不输出敏感路径细节到响应
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.runtime.observability.metrics import MetricsCollector, get_global_collector


@dataclass
class HealthCheckResult:
    """单项健康检查结果。

    Attributes:
        name: 检查项名称。
        status: 状态（ok/warning/error/unknown）。
        message: 人类可读消息。
        details: 附加详情；不含密钥。
        duration_ms: 检查耗时（毫秒）。
    """

    name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
            "duration_ms": round(self.duration_ms, 3),
        }


def _run_check(
    name: str, func: Callable[[], tuple[str, str, dict[str, Any]]]
) -> HealthCheckResult:
    """执行单项检查并包装为 HealthCheckResult。

    Args:
        name: 检查项名称。
        func: 检查函数，返回 (status, message, details)。

    Returns:
        HealthCheckResult 实例；异常时返回 error 状态。
    """
    import time

    started = time.monotonic()
    try:
        status, message, details = func()
    except Exception as exc:  # 防御性：单项检查异常不阻断整体
        status = "error"
        message = f"检查异常: {type(exc).__name__}: {exc}"
        details = {}
    duration_ms = (time.monotonic() - started) * 1000
    return HealthCheckResult(
        name=name,
        status=status,
        message=message,
        details=details,
        duration_ms=duration_ms,
    )


def check_sqlite_integrity(db_path: str | Path) -> HealthCheckResult:
    """检查 SQLite 数据库一致性。

    检查项：
    - PRAGMA integrity_check
    - PRAGMA foreign_key_check
    - schema_meta 表存在性

    Args:
        db_path: SQLite 数据库路径。

    Returns:
        HealthCheckResult；数据库不存在时返回 warning。
    """
    path = Path(db_path)

    def _check() -> tuple[str, str, dict[str, Any]]:
        if not path.exists():
            return (
                "warning",
                f"数据库文件不存在: {path.name}",
                {"path_exists": False},
            )
        with sqlite3.connect(str(path)) as conn:
            # integrity_check
            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_result = str(integrity_row[0]) if integrity_row else "unknown"
            # foreign_key_check
            fk_violations: list[dict[str, Any]] = []
            try:
                fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
                fk_violations = [
                    {"table": str(row[0]), "rowid": str(row[1]) if len(row) > 1 else ""}
                    for row in fk_rows
                ]
            except sqlite3.OperationalError:
                # 无外键表时跳过
                pass
            # schema_meta 表存在性
            has_schema_meta = False
            try:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
                ).fetchone()
                has_schema_meta = row is not None
            except sqlite3.OperationalError:
                pass
            # 表数量
            table_count_row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            table_count = int(table_count_row[0]) if table_count_row else 0
        details = {
            "integrity_result": integrity_result,
            "foreign_key_violations": fk_violations,
            "has_schema_meta": has_schema_meta,
            "table_count": table_count,
        }
        if integrity_result != "ok":
            return ("error", f"integrity_check 失败: {integrity_result}", details)
        if fk_violations:
            return (
                "warning",
                f"外键违规 {len(fk_violations)} 处",
                details,
            )
        return ("ok", f"SQLite 一致性正常，表数 {table_count}", details)

    return _run_check("sqlite_integrity", _check)


def check_file_checksums(
    manifest_path: str | Path,
) -> HealthCheckResult:
    """根据清单校验文件产物的 SHA-256。

    清单格式（JSON）::

        {
            "files": {
                "relative/path": "<sha256>",
                ...
            }
        }

    Args:
        manifest_path: 清单文件路径。

    Returns:
        HealthCheckResult；清单不存在时返回 warning。
    """
    path = Path(manifest_path)

    def _check() -> tuple[str, str, dict[str, Any]]:
        if not path.exists():
            return (
                "warning",
                f"校验清单不存在: {path.name}",
                {"manifest_exists": False},
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return (
                "error",
                f"清单读取失败: {type(exc).__name__}",
                {"manifest_exists": True},
            )
        files = payload.get("files") or {}
        if not files:
            return ("warning", "清单为空，无文件可校验", {"file_count": 0})
        base_dir = path.parent
        ok_count = 0
        missing: list[str] = []
        mismatched: list[str] = []
        for rel_path, expected_hash in files.items():
            target = base_dir / rel_path
            if not target.exists():
                missing.append(rel_path)
                continue
            actual_hash = _sha256_of_file(target)
            if str(expected_hash).lower() == actual_hash.lower():
                ok_count += 1
            else:
                mismatched.append(rel_path)
        details = {
            "total_files": len(files),
            "ok_count": ok_count,
            "missing_count": len(missing),
            "mismatched_count": len(mismatched),
            "missing": missing[:10],
            "mismatched": mismatched[:10],
        }
        if missing or mismatched:
            return (
                "warning",
                f"校验不一致：缺失 {len(missing)}，不匹配 {len(mismatched)}",
                details,
            )
        return (
            "ok",
            f"全部 {ok_count} 个文件校验通过",
            details,
        )

    return _run_check("file_checksums", _check)


def _sha256_of_file(path: Path, chunk_size: int = 65536) -> str:
    """计算文件的 SHA-256 哈希。

    Args:
        path: 文件路径。
        chunk_size: 读取块大小（字节）。

    Returns:
        十六进制哈希字符串。
    """
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_file_checksums(
    directory: str | Path,
    *,
    patterns: tuple[str, ...] = ("*.json", "*.md", "*.db"),
) -> dict[str, str]:
    """计算目录下匹配文件的 SHA-256 清单。

    Args:
        directory: 根目录。
        patterns: 文件 glob 模式元组。

    Returns:
        相对路径 -> SHA-256 的字典。
    """
    root = Path(directory)
    manifest: dict[str, str] = {}
    if not root.exists():
        return manifest
    for pattern in patterns:
        for path in root.rglob(pattern):
            if path.is_file():
                rel = str(path.relative_to(root)).replace("\\", "/")
                manifest[rel] = _sha256_of_file(path)
    return manifest


def write_checksum_manifest(
    directory: str | Path,
    manifest_path: str | Path,
    *,
    patterns: tuple[str, ...] = ("*.json", "*.md", "*.db"),
) -> dict[str, str]:
    """计算并写入校验清单。

    Args:
        directory: 待校验根目录。
        manifest_path: 清单输出路径。
        patterns: 文件 glob 模式元组。

    Returns:
        相对路径 -> SHA-256 的字典。
    """
    manifest = compute_file_checksums(directory, patterns=patterns)
    out_path = Path(manifest_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
             "files": manifest},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def check_provider_availability(
    provider_names: list[str],
    collector: MetricsCollector | None = None,
) -> HealthCheckResult:
    """检查 provider 可用性（基于指标快照）。

    不实际发起网络请求；仅检查 MetricsCollector 中记录的状态。

    Args:
        provider_names: 待检查的 provider 名称列表。
        collector: 指标收集器；默认使用全局单例。

    Returns:
        HealthCheckResult；任一 provider 不可用时返回 warning。
    """
    metrics = collector or get_global_collector()

    def _check() -> tuple[str, str, dict[str, Any]]:
        provider_status: dict[str, Any] = {}
        unavailable: list[str] = []
        for name in provider_names:
            metric = metrics.get_provider(name)
            if metric is None:
                provider_status[name] = {"registered": False, "available": False}
                unavailable.append(name)
            else:
                provider_status[name] = {
                    "registered": True,
                    "available": metric.available,
                    "availability_rate": round(metric.availability_rate, 4),
                    "rate_limited": metric.rate_limited,
                }
                if not metric.available:
                    unavailable.append(name)
        details = {
            "provider_count": len(provider_names),
            "unavailable_count": len(unavailable),
            "unavailable": unavailable,
            "providers": provider_status,
        }
        if unavailable:
            return (
                "warning",
                f"{len(unavailable)} 个 provider 不可用",
                details,
            )
        return ("ok", f"全部 {len(provider_names)} 个 provider 可用", details)

    return _run_check("provider_availability", _check)


class HealthChecker:
    """综合健康检查器；聚合多项检查并输出统一结果。

    用法::

        checker = HealthChecker()
        checker.add_check("sqlite", lambda: check_sqlite_integrity(db_path))
        report = checker.run_all()
    """

    def __init__(self) -> None:
        """初始化健康检查器。"""
        self._checks: list[tuple[str, Callable[[], HealthCheckResult]]] = []

    def add_check(
        self, name: str, func: Callable[[], HealthCheckResult]
    ) -> None:
        """注册单项检查。

        Args:
            name: 检查项名称。
            func: 检查函数；返回 HealthCheckResult。
        """
        self._checks.append((name, func))

    def run_all(self) -> dict[str, Any]:
        """运行所有注册的检查并返回综合报告。

        Returns:
            综合健康报告字典；包含 status、checks、generated_at。
        """
        results: list[HealthCheckResult] = []
        for _name, func in self._checks:
            try:
                result = func()
                # 使用注册时的名称覆盖，保证外部调用方能匹配
                result.name = _name
            except Exception as exc:  # 防御性：单项失败不阻断
                result = HealthCheckResult(
                    name=_name,
                    status="error",
                    message=f"检查异常: {type(exc).__name__}: {exc}",
                )
            results.append(result)
        # 综合状态：任一 error 则 error；任一 warning 则 warning；否则 ok
        if any(r.status == "error" for r in results):
            overall = "error"
        elif any(r.status == "warning" for r in results):
            overall = "warning"
        else:
            overall = "ok"
        return {
            "generated_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(timespec="seconds"),
            "status": overall,
            "check_count": len(results),
            "checks": [r.to_dict() for r in results],
        }


__all__ = [
    "HealthCheckResult",
    "HealthChecker",
    "check_file_checksums",
    "check_provider_availability",
    "check_sqlite_integrity",
    "compute_file_checksums",
    "write_checksum_manifest",
]
