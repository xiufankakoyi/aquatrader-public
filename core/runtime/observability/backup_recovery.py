"""研究产物备份与恢复模块。

按 P3.8 生产要求：
- 支持研究产物目录的增量备份
- 校验和验证（SHA-256 清单）
- 恢复演练（dry-run 与实际恢复）

设计原则：
- 不依赖外部工具（如 rsync），纯 Python 实现
- 备份元数据持久化为 JSON 清单
- 恢复前校验校验和，不匹配时拒绝恢复
- 不备份密钥文件（按 patterns 过滤）
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.runtime.observability.health_checks import (
    _sha256_of_file,
    compute_file_checksums,
)


# 默认排除的文件名模式（避免备份密钥、临时文件）
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "secrets.py",
    "*.env",
    "*.key",
    "*.pem",
    "__pycache__",
    "*.pyc",
    ".DS_Store",
    "*.tmp",
    "*.lock",
)


def _matches_exclude(path: Path, exclude_patterns: tuple[str, ...]) -> bool:
    """判断路径是否匹配排除模式。

    Args:
        path: 文件路径。
        exclude_patterns: 排除模式元组。

    Returns:
        True 表示应排除。
    """
    name = path.name
    for pattern in exclude_patterns:
        if path.match(pattern):
            return True
        # 目录名匹配（如 __pycache__）
        if "*" not in pattern and pattern in path.parts:
            return True
    return False


@dataclass
class BackupManifest:
    """备份清单元数据。

    Attributes:
        backup_id: 备份唯一 ID。
        created_at: 备份创建时间（ISO8601）。
        source_dir: 源目录。
        file_count: 文件数。
        total_bytes: 总字节数。
        checksums: 相对路径 -> SHA-256。
        previous_backup_id: 上一次增量备份 ID；全量时为空。
        excluded_patterns: 排除模式列表。
    """

    backup_id: str
    created_at: str
    source_dir: str
    file_count: int = 0
    total_bytes: int = 0
    checksums: dict[str, str] = field(default_factory=dict)
    previous_backup_id: str = ""
    excluded_patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "source_dir": self.source_dir,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "checksums": dict(self.checksums),
            "previous_backup_id": self.previous_backup_id,
            "excluded_patterns": list(self.excluded_patterns),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BackupManifest":
        """从字典构造 BackupManifest。"""
        return cls(
            backup_id=str(payload.get("backup_id", "")),
            created_at=str(payload.get("created_at", "")),
            source_dir=str(payload.get("source_dir", "")),
            file_count=int(payload.get("file_count", 0)),
            total_bytes=int(payload.get("total_bytes", 0)),
            checksums=dict(payload.get("checksums", {})),
            previous_backup_id=str(payload.get("previous_backup_id", "")),
            excluded_patterns=list(payload.get("excluded_patterns", [])),
        )


class BackupManager:
    """研究产物备份与恢复管理器。

    支持全量与增量备份：
    - 全量：复制所有匹配文件，生成完整校验清单
    - 增量：仅复制校验和变化或新增的文件

    用法::

        manager = BackupManager(source_dir, backup_root)
        manifest = manager.create_backup()
        restored = manager.restore(manifest, target_dir)
    """

    def __init__(
        self,
        source_dir: str | Path,
        backup_root: str | Path,
        *,
        patterns: tuple[str, ...] = ("*.json", "*.md", "*.db", "*.txt"),
        exclude_patterns: tuple[str, ...] = DEFAULT_EXCLUDE_PATTERNS,
    ) -> None:
        """初始化备份管理器。

        Args:
            source_dir: 待备份的源目录。
            backup_root: 备份根目录；每次备份创建子目录。
            patterns: 包含的文件 glob 模式。
            exclude_patterns: 排除的文件模式（用于过滤密钥等）。
        """
        self.source_dir = Path(source_dir)
        self.backup_root = Path(backup_root)
        self.patterns = patterns
        self.exclude_patterns = exclude_patterns

    def create_backup(
        self,
        *,
        backup_id: str | None = None,
        incremental: bool = True,
    ) -> BackupManifest:
        """创建备份。

        Args:
            backup_id: 指定备份 ID；``None`` 时自动生成时间戳 ID。
            incremental: 是否增量备份；True 时仅复制变化的文件。

        Returns:
            BackupManifest 实例。
        """
        if not self.source_dir.exists():
            raise FileNotFoundError(f"源目录不存在: {self.source_dir}")
        backup_id = backup_id or self._generate_backup_id()
        backup_dir = self.backup_root / backup_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        # 读取上次备份清单（用于增量）
        previous_manifest = self._load_latest_manifest() if incremental else None
        previous_checksums = (
            previous_manifest.checksums if previous_manifest else {}
        )
        # 收集源文件
        source_files = self._collect_source_files()
        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source_dir=str(self.source_dir),
            excluded_patterns=list(self.exclude_patterns),
            previous_backup_id=(
                previous_manifest.backup_id if previous_manifest else ""
            ),
        )
        total_bytes = 0
        for rel_path, source_path in source_files.items():
            current_hash = _sha256_of_file(source_path)
            manifest.checksums[rel_path] = current_hash
            # 增量：仅复制变化的文件
            should_copy = (
                not incremental
                or rel_path not in previous_checksums
                or previous_checksums[rel_path] != current_hash
            )
            if should_copy:
                target_path = backup_dir / rel_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
            total_bytes += source_path.stat().st_size
        manifest.file_count = len(manifest.checksums)
        manifest.total_bytes = total_bytes
        # 写入清单
        manifest_file = backup_dir / "_backup_manifest.json"
        manifest_file.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 更新 latest 指针
        latest_file = self.backup_root / "_latest.json"
        latest_file.write_text(
            json.dumps({"backup_id": backup_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest

    def restore(
        self,
        manifest: BackupManifest,
        target_dir: str | Path,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """从备份恢复到目标目录。

        Args:
            manifest: 备份清单。
            target_dir: 恢复目标目录。
            dry_run: True 时仅校验不实际复制。

        Returns:
            恢复结果字典；包含 restored_count、verified_count、mismatches。
        """
        backup_dir = self.backup_root / manifest.backup_id
        target = Path(target_dir)
        result: dict[str, Any] = {
            "backup_id": manifest.backup_id,
            "target_dir": str(target),
            "dry_run": dry_run,
            "restored_count": 0,
            "verified_count": 0,
            "mismatched": [],
            "missing": [],
        }
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
        for rel_path, expected_hash in manifest.checksums.items():
            source_file = backup_dir / rel_path
            if not source_file.exists():
                result["missing"].append(rel_path)
                continue
            actual_hash = _sha256_of_file(source_file)
            if actual_hash.lower() != expected_hash.lower():
                result["mismatched"].append(rel_path)
                continue
            result["verified_count"] += 1
            if not dry_run:
                target_file = target / rel_path
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_file)
                result["restored_count"] += 1
        return result

    def verify_backup(self, manifest: BackupManifest) -> dict[str, Any]:
        """校验备份完整性（不恢复）。

        Args:
            manifest: 备份清单。

        Returns:
            校验结果字典。
        """
        return self.restore(manifest, target_dir=self.backup_root, dry_run=True)

    def list_backups(self) -> list[BackupManifest]:
        """列出所有备份清单（按时间升序）。"""
        manifests: list[BackupManifest] = []
        if not self.backup_root.exists():
            return manifests
        for child in sorted(self.backup_root.iterdir()):
            manifest_file = child / "_backup_manifest.json"
            if manifest_file.exists():
                try:
                    payload = json.loads(manifest_file.read_text(encoding="utf-8"))
                    manifests.append(BackupManifest.from_dict(payload))
                except (OSError, json.JSONDecodeError):
                    continue
        return manifests

    def run_recovery_drill(
        self,
        target_dir: str | Path,
        *,
        backup_id: str | None = None,
    ) -> dict[str, Any]:
        """执行恢复演练。

        流程：
        1. 选择指定备份（默认最新）
        2. 校验备份完整性
        3. 恢复到临时目标目录
        4. 校验恢复后的文件校验和

        Args:
            target_dir: 恢复目标目录。
            backup_id: 指定备份 ID；``None`` 时使用最新备份。

        Returns:
            演练结果字典；包含 manifest、verify、restore、final_check。
        """
        manifest = self._load_manifest(backup_id)
        if manifest is None:
            return {
                "status": "error",
                "message": f"备份不存在: {backup_id or 'latest'}",
            }
        verify_result = self.verify_backup(manifest)
        restore_result = self.restore(manifest, target_dir, dry_run=False)
        # 恢复后再次校验
        target_path = Path(target_dir)
        final_checksums = compute_file_checksums(target_path, patterns=self.patterns)
        final_ok = all(
            final_checksums.get(rel) == expected
            for rel, expected in manifest.checksums.items()
        )
        return {
            "status": "ok" if final_ok and not restore_result["mismatched"] else "warning",
            "manifest": manifest.to_dict(),
            "verify": verify_result,
            "restore": restore_result,
            "final_check": {
                "checked_count": len(manifest.checksums),
                "matched_count": sum(
                    1 for rel, expected in manifest.checksums.items()
                    if final_checksums.get(rel) == expected
                ),
                "all_matched": final_ok,
            },
            "drilled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _collect_source_files(self) -> dict[str, Path]:
        """收集源目录下匹配的文件。

        Returns:
            相对路径 -> Path 的字典。
        """
        files: dict[str, Path] = {}
        if not self.source_dir.exists():
            return files
        for pattern in self.patterns:
            for path in self.source_dir.rglob(pattern):
                if not path.is_file():
                    continue
                if _matches_exclude(path, self.exclude_patterns):
                    continue
                rel = str(path.relative_to(self.source_dir)).replace("\\", "/")
                files[rel] = path
        return files

    def _generate_backup_id(self) -> str:
        """生成时间戳备份 ID。"""
        return datetime.now(timezone.utc).strftime("backup_%Y%m%d_%H%M%S")

    def _load_latest_manifest(self) -> BackupManifest | None:
        """加载最新备份清单。"""
        latest_file = self.backup_root / "_latest.json"
        if not latest_file.exists():
            return None
        try:
            payload = json.loads(latest_file.read_text(encoding="utf-8"))
            backup_id = str(payload.get("backup_id", ""))
            if not backup_id:
                return None
            return self._load_manifest(backup_id)
        except (OSError, json.JSONDecodeError):
            return None

    def _load_manifest(self, backup_id: str | None) -> BackupManifest | None:
        """加载指定备份的清单。"""
        if not backup_id:
            return self._load_latest_manifest()
        manifest_file = self.backup_root / backup_id / "_backup_manifest.json"
        if not manifest_file.exists():
            return None
        try:
            payload = json.loads(manifest_file.read_text(encoding="utf-8"))
            return BackupManifest.from_dict(payload)
        except (OSError, json.JSONDecodeError):
            return None


__all__ = [
    "BackupManager",
    "BackupManifest",
    "DEFAULT_EXCLUDE_PATTERNS",
]
