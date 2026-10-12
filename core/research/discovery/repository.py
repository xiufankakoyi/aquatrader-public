"""P2.1 全市场研究截面仓储：SQLite + PIT 过滤 + 幂等迁移 + 不可覆盖。

按 P2 计划 §4 Task P2.1：
- 任意历史 as_of 只能使用当时已公开数据（PIT 过滤）
- 退市/暂停上市/历史成分处理不产生幸存者偏差（membership_status 显式标记）
- 截面构建保存 provider、版本、覆盖率和排除原因
- 不可覆盖：同 snapshot_id 重复保存抛异常

实现参考 P1 IndustryRepository 模式：
- 三张表：universe_snapshots / universe_members / universe_exclusions
- 每张表以 stable_id 为主键，索引列覆盖 as_of / symbol / universe_name
- PIT 过滤：as_of <= query_as_of
- 幂等迁移：migrate() 可重复执行
- 来源追溯：provenance 通过 payload_json 完整保存

线程安全：所有写操作加 RLock；SQLite 连接 WAL 模式。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    ExclusionRecord,
    UniverseMember,
    UniverseSnapshot,
    _normalize_as_of,
    new_id,
    utc_now,
)


# Schema 版本（用于幂等迁移校验）
SCHEMA_VERSION: str = "p2.1.0"


class UniverseSnapshotRepository:
    """全市场研究截面持久化仓储。

    不变量：
    - 同 snapshot_id 唯一，不可覆盖（重复 save 抛 ValueError）
    - 同 (snapshot_id, symbol) 唯一
    - PIT 查询：get_snapshot(as_of) 返回 as_of <= query_as_of 的最新截面
    - 退市/暂停上市/历史成分 membership_status 显式标记，不参与幸存者偏差过滤
    """

    SCHEMA_VERSION: str = SCHEMA_VERSION

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化仓储并执行幂等迁移。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``UNIVERSE_SNAPSHOT_DB_PATH`` 读取，默认
                ``data/research_control/universe_snapshot.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "UNIVERSE_SNAPSHOT_DB_PATH",
                "data/research_control/universe_snapshot.db",
            )
        path = Path(db_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        self.db_path = path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    # ------------------------------------------------------------------
    # 连接与迁移
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """获取 SQLite 连接上下文管理器。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        """创建/升级 schema；可重复执行（幂等）。

        三张表：
        - universe_snapshots：截面主表，snapshot_id 主键
        - universe_members：成员表，member_id 主键，(snapshot_id, symbol) 唯一
        - universe_exclusions：排除记录表，(snapshot_id, symbol) 唯一
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS universe_schema_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            # 截面主表
            conn.execute(
                "CREATE TABLE IF NOT EXISTS universe_snapshots ("
                "snapshot_id TEXT PRIMARY KEY, "
                "as_of TEXT NOT NULL, "
                "universe_name TEXT NOT NULL DEFAULT 'all_a_share', "
                "universe_version TEXT NOT NULL DEFAULT '', "
                "policy_version TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL DEFAULT '', "
                "payload_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_as_of "
                "ON universe_snapshots(as_of)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_universe "
                "ON universe_snapshots(universe_name)"
            )
            # 成员表
            conn.execute(
                "CREATE TABLE IF NOT EXISTS universe_members ("
                "member_id TEXT PRIMARY KEY, "
                "snapshot_id TEXT NOT NULL, "
                "symbol TEXT NOT NULL, "
                "as_of TEXT NOT NULL, "
                "membership_status TEXT NOT NULL DEFAULT 'listed', "
                "industry_code TEXT NOT NULL DEFAULT '', "
                "data_availability_score REAL NOT NULL DEFAULT 0.0, "
                "payload_json TEXT NOT NULL, "
                "UNIQUE(snapshot_id, symbol))"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_members_snapshot "
                "ON universe_members(snapshot_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_members_symbol "
                "ON universe_members(symbol)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_members_as_of "
                "ON universe_members(as_of)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_members_status "
                "ON universe_members(membership_status)"
            )
            # 排除记录表
            conn.execute(
                "CREATE TABLE IF NOT EXISTS universe_exclusions ("
                "excl_id TEXT PRIMARY KEY, "
                "snapshot_id TEXT NOT NULL, "
                "symbol TEXT NOT NULL, "
                "as_of TEXT NOT NULL, "
                "reason TEXT NOT NULL DEFAULT '', "
                "excluded_by TEXT NOT NULL DEFAULT 'builder', "
                "excluded_at TEXT NOT NULL DEFAULT '', "
                "payload_json TEXT NOT NULL, "
                "UNIQUE(snapshot_id, symbol))"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_excl_snapshot "
                "ON universe_exclusions(snapshot_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_excl_symbol "
                "ON universe_exclusions(symbol)"
            )
            conn.execute(
                "INSERT INTO universe_schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save(self, snapshot: UniverseSnapshot) -> None:
        """保存截面；同 snapshot_id 已存在则抛异常（不可覆盖）。

        Args:
            snapshot: 待保存的截面

        Raises:
            ValueError: 当 snapshot_id 已存在，或 (snapshot_id, symbol) 重复
        """
        if not isinstance(snapshot, UniverseSnapshot):
            raise TypeError("snapshot must be UniverseSnapshot")
        if not snapshot.snapshot_id:
            raise ValueError("snapshot_id 不能为空")
        if not snapshot.as_of:
            raise ValueError("as_of 不能为空")
        snapshot.as_of = _normalize_as_of(snapshot.as_of)
        snapshot.created_at = snapshot.created_at or utc_now()
        # 同步 member.as_of 与 member.snapshot_id
        for member in snapshot.members:
            member.snapshot_id = snapshot.snapshot_id
            member.as_of = snapshot.as_of
            if not member.member_id:
                member.member_id = new_id("member")
            member.compute_data_availability_score()
        # 同步 exclusion.as_of
        for excl in snapshot.exclusions:
            excl.as_of = snapshot.as_of
            if not excl.excluded_at:
                excl.excluded_at = utc_now()
        # 计算覆盖率统计
        snapshot.compute_coverage_stats()
        snapshot.provenance.total_member_count = len(snapshot.members)
        snapshot.provenance.exclusion_count = len(snapshot.exclusions)
        if not snapshot.provenance.built_at:
            snapshot.provenance.built_at = utc_now()

        snapshot_payload = snapshot.to_dict()
        with self._lock, self._connect() as conn:
            # 检查 snapshot_id 是否已存在（不可覆盖）
            existing = conn.execute(
                "SELECT 1 FROM universe_snapshots WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing:
                raise ValueError(
                    f"截面已存在，不可覆盖: snapshot_id={snapshot.snapshot_id}"
                )
            # 插入主表
            conn.execute(
                "INSERT INTO universe_snapshots("
                "snapshot_id, as_of, universe_name, universe_version, "
                "policy_version, created_at, payload_json) VALUES(?,?,?,?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.as_of,
                    snapshot.universe_name,
                    snapshot.universe_version,
                    snapshot.policy_version,
                    snapshot.created_at,
                    json.dumps(snapshot_payload, ensure_ascii=False, allow_nan=False),
                ),
            )
            # 插入成员表
            for member in snapshot.members:
                conn.execute(
                    "INSERT INTO universe_members("
                    "member_id, snapshot_id, symbol, as_of, membership_status, "
                    "industry_code, data_availability_score, payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        member.member_id,
                        member.snapshot_id,
                        member.symbol,
                        member.as_of,
                        member.membership_status,
                        member.industry_code,
                        member.data_availability_score,
                        json.dumps(member.to_dict(), ensure_ascii=False, allow_nan=False),
                    ),
                )
            # 插入排除记录表
            for excl in snapshot.exclusions:
                excl_id = new_id("excl")
                conn.execute(
                    "INSERT INTO universe_exclusions("
                    "excl_id, snapshot_id, symbol, as_of, reason, "
                    "excluded_by, excluded_at, payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        excl_id,
                        snapshot.snapshot_id,
                        excl.symbol,
                        excl.as_of,
                        excl.reason,
                        excl.excluded_by,
                        excl.excluded_at,
                        json.dumps(excl.to_dict(), ensure_ascii=False, allow_nan=False),
                    ),
                )

    def add_exclusion(self, exclusion: ExclusionRecord, snapshot_id: str) -> None:
        """追加排除记录到已存在的截面（用于人工后续追加）。

        Args:
            exclusion: 排除记录
            snapshot_id: 所属截面 ID

        Raises:
            ValueError: 当截面不存在
        """
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM universe_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if not existing:
                raise ValueError(f"截面不存在: snapshot_id={snapshot_id}")
            exclusion.as_of = _normalize_as_of(exclusion.as_of)
            if not exclusion.excluded_at:
                exclusion.excluded_at = utc_now()
            excl_id = new_id("excl")
            conn.execute(
                "INSERT INTO universe_exclusions("
                "excl_id, snapshot_id, symbol, as_of, reason, "
                "excluded_by, excluded_at, payload_json) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(snapshot_id, symbol) DO UPDATE SET "
                "reason=excluded.reason, "
                "excluded_by=excluded.excluded_by, "
                "excluded_at=excluded.excluded_at, "
                "payload_json=excluded.payload_json",
                (
                    excl_id,
                    snapshot_id,
                    exclusion.symbol,
                    exclusion.as_of,
                    exclusion.reason,
                    exclusion.excluded_by,
                    exclusion.excluded_at,
                    json.dumps(exclusion.to_dict(), ensure_ascii=False, allow_nan=False),
                ),
            )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, snapshot_id: str) -> UniverseSnapshot | None:
        """按 snapshot_id 查询截面完整数据（含 members 和 exclusions）。

        Args:
            snapshot_id: 截面 ID

        Returns:
            截面对象；不存在返回 None
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM universe_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if not row:
                return None
            try:
                snapshot = UniverseSnapshot.from_dict(json.loads(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            # 重新加载 members 与 exclusions（payload 中已含，但保证一致性）
            member_rows = conn.execute(
                "SELECT payload_json FROM universe_members WHERE snapshot_id=? "
                "ORDER BY symbol",
                (snapshot_id,),
            ).fetchall()
            members: list[UniverseMember] = []
            for row_data in member_rows:
                try:
                    members.append(UniverseMember.from_dict(json.loads(row_data["payload_json"])))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            snapshot.members = members
            excl_rows = conn.execute(
                "SELECT payload_json FROM universe_exclusions WHERE snapshot_id=? "
                "ORDER BY symbol",
                (snapshot_id,),
            ).fetchall()
            exclusions: list[ExclusionRecord] = []
            for row_data in excl_rows:
                try:
                    exclusions.append(ExclusionRecord.from_dict(json.loads(row_data["payload_json"])))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            snapshot.exclusions = exclusions
            return snapshot

    def get_by_as_of(
        self,
        as_of: str,
        universe_name: str = "all_a_share",
    ) -> UniverseSnapshot | None:
        """PIT 查询：返回 as_of 时点最新的截面。

        PIT 语义：返回 as_of <= query_as_of 中 created_at 最大的截面。
        若多个截面同 created_at，取 snapshot_id 字典序最大者（确定性）。

        Args:
            as_of: 查询时点 YYYY-MM-DD
            universe_name: 截面名称

        Returns:
            截面对象；不存在返回 None
        """
        cutoff = _normalize_as_of(as_of)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT snapshot_id FROM universe_snapshots "
                "WHERE as_of <= ? AND universe_name=? "
                "ORDER BY as_of DESC, snapshot_id DESC LIMIT 1",
                (cutoff, universe_name),
            ).fetchone()
            if not row:
                return None
            return self.get(row["snapshot_id"])

    def list_snapshots(
        self,
        universe_name: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列出截面摘要（不含 members 详情）。

        Args:
            universe_name: 截面名称过滤；None 表示全部
            limit: 返回上限

        Returns:
            截面摘要字典列表
        """
        with self._lock, self._connect() as conn:
            if universe_name:
                rows = conn.execute(
                    "SELECT snapshot_id, as_of, universe_name, universe_version, "
                    "policy_version, created_at, payload_json "
                    "FROM universe_snapshots WHERE universe_name=? "
                    "ORDER BY as_of DESC, snapshot_id DESC LIMIT ?",
                    (universe_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT snapshot_id, as_of, universe_name, universe_version, "
                    "policy_version, created_at, payload_json "
                    "FROM universe_snapshots "
                    "ORDER BY as_of DESC, snapshot_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            results: list[dict[str, Any]] = []
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                provenance = payload.get("provenance", {}) or {}
                if not isinstance(provenance, dict):
                    provenance = {}
                results.append({
                    "snapshot_id": r["snapshot_id"],
                    "as_of": r["as_of"],
                    "universe_name": r["universe_name"],
                    "universe_version": r["universe_version"],
                    "policy_version": r["policy_version"],
                    "created_at": r["created_at"],
                    "member_count": provenance.get("total_member_count", 0),
                    "exclusion_count": provenance.get("exclusion_count", 0),
                    "provider": provenance.get("provider", ""),
                    "builder_version": provenance.get("builder_version", ""),
                    "data_fingerprint": provenance.get("data_fingerprint", ""),
                })
            return results

    def get_member(
        self,
        snapshot_id: str,
        symbol: str,
    ) -> UniverseMember | None:
        """查询单个成员。

        Args:
            snapshot_id: 截面 ID
            symbol: 公司代码

        Returns:
            成员对象；不存在返回 None
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM universe_members "
                "WHERE snapshot_id=? AND symbol=?",
                (snapshot_id, symbol),
            ).fetchone()
            if not row:
                return None
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return UniverseMember.from_dict(payload)

    def list_members(
        self,
        snapshot_id: str,
        membership_status: str | None = None,
    ) -> list[UniverseMember]:
        """列出截面全部成员（可按 membership_status 过滤）。

        Args:
            snapshot_id: 截面 ID
            membership_status: 成分股状态过滤；None 表示全部

        Returns:
            成员列表
        """
        with self._lock, self._connect() as conn:
            if membership_status:
                rows = conn.execute(
                    "SELECT payload_json FROM universe_members "
                    "WHERE snapshot_id=? AND membership_status=? "
                    "ORDER BY symbol",
                    (snapshot_id, membership_status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM universe_members "
                    "WHERE snapshot_id=? ORDER BY symbol",
                    (snapshot_id,),
                ).fetchall()
            result: list[UniverseMember] = []
            for row in rows:
                try:
                    result.append(UniverseMember.from_dict(json.loads(row["payload_json"])))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            return result

    def list_exclusions(self, snapshot_id: str) -> list[ExclusionRecord]:
        """列出截面全部排除记录。

        Args:
            snapshot_id: 截面 ID

        Returns:
            排除记录列表
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM universe_exclusions "
                "WHERE snapshot_id=? ORDER BY symbol",
                (snapshot_id,),
            ).fetchall()
            result: list[ExclusionRecord] = []
            for row in rows:
                try:
                    result.append(ExclusionRecord.from_dict(json.loads(row["payload_json"])))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            return result

    # ------------------------------------------------------------------
    # PIT 校验（P2.1 验收点 1）
    # ------------------------------------------------------------------

    def validate_no_future_leakage(self, snapshot_id: str) -> list[dict[str, Any]]:
        """校验截面无未来数据泄漏。

        遍历所有成员的所有字段，检查 field.available_at <= snapshot.as_of。
        任一违反则记录违规字段。

        Args:
            snapshot_id: 截面 ID

        Returns:
            违规字段列表；空列表表示无泄漏
        """
        snapshot = self.get(snapshot_id)
        if snapshot is None:
            return [{
                "error": "snapshot_not_found",
                "snapshot_id": snapshot_id,
            }]
        cutoff = _normalize_as_of(snapshot.as_of)
        violations: list[dict[str, Any]] = []
        for member in snapshot.members:
            for group_name, group in member.field_groups.items():
                for fld in group.fields:
                    if not fld.available_at:
                        # 空字段不参与泄漏校验（P2.1 验收点 4）
                        continue
                    field_cutoff = _normalize_as_of(fld.available_at)
                    if field_cutoff > cutoff:
                        violations.append({
                            "snapshot_id": snapshot_id,
                            "symbol": member.symbol,
                            "group_name": group_name,
                            "field_name": fld.name,
                            "available_at": fld.available_at,
                            "snapshot_as_of": snapshot.as_of,
                            "violation": "field_available_at > snapshot_as_of",
                        })
        return violations

    # ------------------------------------------------------------------
    # 数据指纹
    # ------------------------------------------------------------------

    @staticmethod
    def compute_data_fingerprint(
        as_of: str,
        universe_name: str,
        provider: str,
        sorted_symbols: list[str],
    ) -> str:
        """计算截面数据指纹（sha256 前 16 字符）。

        Args:
            as_of: 截面时点
            universe_name: 截面名称
            provider: 数据提供方
            sorted_symbols: 排序后的成员代码列表

        Returns:
            形如 "fp_xxxxxxxxxxxxxxxx" 的指纹
        """
        raw = f"{as_of}|{universe_name}|{provider}|{','.join(sorted_symbols)}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"fp_{digest}"
