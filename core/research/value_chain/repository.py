"""不可覆盖的研究运行仓储。

P0.3 实现：每次公司研究运行保存独立目录 + SQLite 元数据索引。
- 原始大 JSON 永远保存在独立 run 目录，SQLite 只存元数据。
- 同输入重复运行生成新版本，不覆盖旧版本。
- 字段缺失/旧 schema/文件损坏时返回降级状态，不使列表 API 崩溃。
- 迁移脚本只索引现有目录，不修改原 JSON；重复执行幂等。

按 docs/superpowers/plans/2026-07-18-institutional-research-p0-trusted-foundation.md
Task P0.3 实施。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION_CURRENT: int = 1


def _utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    """安全读取 JSON；文件缺失/损坏时返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _compute_source_fingerprint(source_snapshots: dict[str, Any]) -> str:
    """对 source_snapshots 计算稳定指纹，用于检测输入是否变化。

    指纹只基于 capability + record count + provider + as_of + status，
    不含具体记录内容，避免大 JSON 哈希性能开销。
    """
    parts: list[str] = []
    for capability in sorted(source_snapshots.keys()):
        snapshot = source_snapshots.get(capability)
        if not isinstance(snapshot, dict):
            parts.append(f"{capability}:invalid")
            continue
        records = snapshot.get("records") or []
        parts.append(
            f"{capability}|status={snapshot.get('status', '')}|"
            f"provider={snapshot.get('provider', '')}|"
            f"as_of={snapshot.get('as_of', '')}|"
            f"count={len(records) if isinstance(records, list) else 0}"
        )
    raw = "\n".join(parts).encode("utf-8")
    return "fp_" + hashlib.sha256(raw).hexdigest()[:16]


def _extract_model_versions(dossier: dict[str, Any]) -> dict[str, str]:
    """从 dossier 中提取各模块版本号。"""
    def nested(name: str) -> Mapping[str, Any]:
        value = dossier.get(name)
        return value if isinstance(value, Mapping) else {}

    return {
        "schema_version": str(dossier.get("schema_version") or ""),
        "research_assessment_version": str(nested("research_assessment").get("version") or ""),
        "profit_bridge_kind": str(nested("profit_bridge").get("status") or ""),
        "financial_quality_status": str(nested("financial_quality").get("status") or ""),
        "valuation_status": str(nested("valuation").get("status") or ""),
    }


class ResearchRunRepository:
    """公司研究运行仓储：SQLite 索引 + 不可覆盖磁盘产物。

    表结构（v1）：
    - schema_meta(key, value)：记录 schema_version
    - research_value_runs(run_id PK, symbol, as_of, schema_version, status,
                          created_at, artifact_path, source_fingerprint,
                          model_versions_json, payload_json)
    - 索引：idx_runs_symbol(symbol, created_at DESC)
            idx_runs_as_of(as_of, created_at DESC)
            idx_runs_status(status, created_at DESC)
            idx_runs_symbol_as_of(symbol, as_of, created_at DESC)
    """

    SCHEMA_VERSION: int = SCHEMA_VERSION_CURRENT

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        runs_root: str | Path = "data/research_value_runs",
    ) -> None:
        """初始化研究运行仓储。

        Args:
            db_path: SQLite 索引库路径。None 时从环境变量
                ``RESEARCH_VALUE_RUNS_DB_PATH`` 读取，默认
                ``data/research_control/research_value_runs.db``。
            runs_root: 运行产物根目录，用于读取历史 dossier/acceptance。
        """
        if db_path is None:
            db_path = os.getenv(
                "RESEARCH_VALUE_RUNS_DB_PATH",
                "data/research_control/research_value_runs.db",
            )
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.runs_root = Path(runs_root)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _read_db_schema_version(self, conn: sqlite3.Connection) -> int:
        """读取 DB 中已记录的 schema 版本；无记录时返回 0。"""
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return 0

    def migrate(self) -> None:
        """创建/升级 schema；按版本顺序增量迁移，可重复执行。

        版本历史：
        - v1: 初始 schema，包含 research_value_runs 表与基础索引。
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db_version = self._read_db_schema_version(conn)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS research_value_runs (
                    run_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL DEFAULT '',
                    as_of TEXT NOT NULL DEFAULT '',
                    schema_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    artifact_path TEXT NOT NULL DEFAULT '',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    model_versions_json TEXT NOT NULL DEFAULT '{}',
                    payload_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_symbol "
                "ON research_value_runs(symbol, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_as_of "
                "ON research_value_runs(as_of, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_status "
                "ON research_value_runs(status, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_symbol_as_of "
                "ON research_value_runs(symbol, as_of, created_at DESC)"
            )
            if db_version < self.SCHEMA_VERSION:
                conn.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(self.SCHEMA_VERSION),),
                )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def index_run(
        self,
        *,
        run_id: str,
        symbol: str,
        as_of: str,
        schema_version: str,
        status: str,
        artifact_path: str,
        source_fingerprint: str,
        model_versions: dict[str, str],
        created_at: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """插入或更新一条运行索引。

        Args:
            run_id: 运行 ID（唯一）。
            symbol: 公司代码。
            as_of: PIT 时点。
            schema_version: dossier schema 版本。
            status: 运行状态（ok/partial/failed/degraded）。
            artifact_path: 运行目录绝对路径。
            source_fingerprint: 来源快照指纹。
            model_versions: 模块版本号字典。
            created_at: 创建时间（UTC ISO）。
            payload: 可选附加元数据。
        """
        columns = (
            "run_id", "symbol", "as_of", "schema_version", "status",
            "created_at", "artifact_path", "source_fingerprint",
            "model_versions_json", "payload_json",
        )
        values = (
            run_id, symbol, as_of, schema_version, status,
            created_at, artifact_path, source_fingerprint,
            json.dumps(model_versions or {}, ensure_ascii=False),
            json.dumps(payload or {}, ensure_ascii=False),
        )
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{c}=excluded.{c}" for c in columns[1:])
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO research_value_runs({','.join(columns)}) "
                f"VALUES({placeholders}) "
                f"ON CONFLICT(run_id) DO UPDATE SET {updates}",
                values,
            )

    def index_dossier_run(
        self,
        *,
        run_id: str,
        dossier: dict[str, Any],
        acceptance: dict[str, Any],
        artifact_path: str,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """从 dossier + acceptance 提取元数据并写入索引。

        Args:
            run_id: 运行 ID。
            dossier: 序列化后的 CompanyDossier 字典。
            acceptance: 序列化后的 acceptance 字典。
            artifact_path: 运行目录绝对路径。
            created_at: 可选创建时间，默认当前 UTC。

        Returns:
            实际写入的索引元数据字典。
        """
        created_at = created_at or _utc_now_iso()
        symbol = str(dossier.get("symbol") or "")
        as_of = str(dossier.get("as_of") or "")
        schema_version = str(dossier.get("schema_version") or "")
        status = str(acceptance.get("status") or "partial")
        source_snapshots = dossier.get("source_snapshots") or {}
        if not isinstance(source_snapshots, dict):
            source_snapshots = {}
        source_fingerprint = _compute_source_fingerprint(source_snapshots)
        model_versions = _extract_model_versions(dossier)
        raw_gap_tasks = acceptance.get("gap_tasks")
        gap_tasks = raw_gap_tasks if isinstance(raw_gap_tasks, Mapping) else {}
        payload = {
            "degradation_reasons": acceptance.get("degradation_reasons") or [],
            "gap_task_ids": gap_tasks.get("created_task_ids") or [],
            "financial_quality_status": acceptance.get("financial_quality_status") or "",
            "valuation_status": acceptance.get("valuation_status") or "",
            "technical_status": acceptance.get("technical_status") or "",
            "source_statuses": acceptance.get("source_statuses") or {},
        }
        self.index_run(
            run_id=run_id, symbol=symbol, as_of=as_of,
            schema_version=schema_version, status=status,
            artifact_path=artifact_path, source_fingerprint=source_fingerprint,
            model_versions=model_versions, created_at=created_at, payload=payload,
        )
        return {
            "run_id": run_id, "symbol": symbol, "as_of": as_of,
            "schema_version": schema_version, "status": status,
            "artifact_path": artifact_path, "source_fingerprint": source_fingerprint,
            "model_versions": model_versions, "created_at": created_at,
            "payload": payload,
        }

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """按 run_id 读取单条索引记录；不存在返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_value_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_runs(
        self,
        *,
        symbol: str = "",
        as_of: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """按 symbol/as_of/status 分页查询运行索引。

        Args:
            symbol: 公司代码过滤；空串表示不过滤。
            as_of: PIT 时点过滤；空串表示不过滤。
            status: 状态过滤；空串表示不过滤。
            page: 页码（1-based）。
            page_size: 每页大小，最大 100。

        Returns:
            分页结果字典：
            - ``items``: 当前页运行元数据列表。
            - ``page``: 当前页码。
            - ``page_size``: 实际每页大小。
            - ``total``: 总记录数。
            - ``total_pages``: 总页数。
        """
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = max(1, min(100, int(page_size)))
        except (TypeError, ValueError):
            page_size = 20
        conditions: list[str] = []
        params: list[Any] = []
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        if as_of:
            conditions.append("as_of=?")
            params.append(as_of)
        if status:
            conditions.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM research_value_runs{where}", params
            ).fetchone()
            total = int(total_row[0]) if total_row else 0
            offset = (page - 1) * page_size
            rows = conn.execute(
                f"SELECT * FROM research_value_runs{where} "
                f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
        items = [self._row_to_dict(row) for row in rows]
        total_pages = (total + page_size - 1) // page_size if total else 0
        return {
            "items": items, "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages,
        }

    def latest_run(self, symbol: str) -> dict[str, Any] | None:
        """返回某公司最新一条运行索引；不存在返回 None。"""
        result = self.list_runs(symbol=symbol, page=1, page_size=1)
        return result["items"][0] if result["items"] else None

    # ------------------------------------------------------------------
    # 磁盘产物读取（不可覆盖）
    # ------------------------------------------------------------------

    def read_dossier(self, run_id: str) -> dict[str, Any] | None:
        """读取运行目录中的 dossier.json；不存在/损坏返回 None。"""
        record = self.get_run(run_id)
        if not record:
            return None
        path = Path(record["artifact_path"]) / "dossier.json"
        return _safe_read_json(path)

    def read_acceptance(self, run_id: str) -> dict[str, Any] | None:
        """读取运行目录中的 acceptance.json；不存在/损坏返回 None。"""
        record = self.get_run(run_id)
        if not record:
            return None
        path = Path(record["artifact_path"]) / "acceptance.json"
        return _safe_read_json(path)

    def read_run_bundle(self, run_id: str) -> dict[str, Any]:
        """读取运行 bundle：索引元数据 + dossier + acceptance。

        缺失/损坏字段返回降级状态，不抛异常。

        Returns:
            ``{"run": ..., "dossier": ..., "acceptance": ..., "degraded": bool}``
        """
        record = self.get_run(run_id)
        if not record:
            return {
                "run": None, "dossier": None, "acceptance": None,
                "degraded": True, "degradation_reason": "run_not_found",
            }
        dossier = self._safe_read_with_path(
            Path(record["artifact_path"]) / "dossier.json"
        )
        acceptance = self._safe_read_with_path(
            Path(record["artifact_path"]) / "acceptance.json"
        )
        degraded = dossier is None or acceptance is None
        return {
            "run": record, "dossier": dossier, "acceptance": acceptance,
            "degraded": degraded,
            "degradation_reason": (
                "dossier_or_acceptance_missing_or_corrupt" if degraded else ""
            ),
        }

    @staticmethod
    def _safe_read_with_path(path: Path) -> dict[str, Any] | None:
        """与 _safe_read_json 一致，便于单测替换。"""
        return _safe_read_json(path)

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def diff_runs(self, run_id_a: str, run_id_b: str) -> dict[str, Any]:
        """比较两个 run 的事实/假设/预期/估值/缺口变化。

        缺失的 run 或字段会以降级形式呈现，不抛异常。

        Args:
            run_id_a: 基线 run ID。
            run_id_b: 对比 run ID。

        Returns:
            Diff 结果字典：
            - ``base_run``/``compare_run``: 两个 run 的索引元数据（或 None）。
            - ``facts``: 事实层差异。
            - ``assumptions``: 假设层差异。
            - ``expectations``: 预期层差异。
            - ``valuation``: 估值层差异。
            - ``gaps``: 缺口差异。
            - ``degraded``: 是否降级。
            - ``degradation_reason``: 降级原因。
        """
        bundle_a = self.read_run_bundle(run_id_a)
        bundle_b = self.read_run_bundle(run_id_b)
        degraded = bundle_a["degraded"] or bundle_b["degraded"]
        reasons: list[str] = []
        if bundle_a["degraded"]:
            reasons.append(f"base_run:{bundle_a['degradation_reason']}")
        if bundle_b["degraded"]:
            reasons.append(f"compare_run:{bundle_b['degradation_reason']}")
        dossier_a = bundle_a.get("dossier") or {}
        dossier_b = bundle_b.get("dossier") or {}
        return {
            "base_run": bundle_a["run"],
            "compare_run": bundle_b["run"],
            "facts": self._diff_facts(dossier_a, dossier_b),
            "assumptions": self._diff_assumptions(dossier_a, dossier_b),
            "expectations": self._diff_expectations(dossier_a, dossier_b),
            "valuation": self._diff_valuation(dossier_a, dossier_b),
            "gaps": self._diff_gaps(dossier_a, dossier_b, bundle_a, bundle_b),
            "degraded": degraded,
            "degradation_reason": ";".join(reasons),
        }

    @staticmethod
    def _diff_facts(dossier_a: dict[str, Any], dossier_b: dict[str, Any]) -> dict[str, Any]:
        """比较事实层：已验证因果边、观察事实、前瞻计划。"""
        def extract(dossier: dict[str, Any]) -> dict[str, Any]:
            industry = dossier.get("industry_context") or {}
            return {
                "verified_edges": [
                    edge.get("claim_id") for edge in (industry.get("verified_edges") or [])
                    if isinstance(edge, dict)
                ],
                "observed_facts": [
                    edge.get("claim_id") for edge in (industry.get("observed_facts") or [])
                    if isinstance(edge, dict)
                ],
                "forward_plans": [
                    edge.get("claim_id") for edge in (industry.get("forward_plans") or [])
                    if isinstance(edge, dict)
                ],
            }
        a, b = extract(dossier_a), extract(dossier_b)
        return {
            "base": a, "compare": b,
            "added": {
                key: [item for item in b[key] if item not in a[key]]
                for key in a
            },
            "removed": {
                key: [item for item in a[key] if item not in b[key]]
                for key in a
            },
        }

    @staticmethod
    def _diff_assumptions(dossier_a: dict[str, Any], dossier_b: dict[str, Any]) -> dict[str, Any]:
        """比较假设层：profit_bridge 中的 forward_driver_assumptions / forward_disclosed_indicators。"""
        def extract(dossier: dict[str, Any]) -> dict[str, Any]:
            bridge = dossier.get("profit_bridge") or {}
            return {
                "forward_driver_assumptions_count": len(
                    bridge.get("forward_driver_assumptions") or []
                ),
                "forward_disclosed_indicators_count": len(
                    bridge.get("forward_disclosed_indicators") or []
                ),
                "forward_guidance_count": len(bridge.get("forward_guidance") or []),
                "market_implied_scenarios_count": len(
                    bridge.get("market_implied_scenarios") or []
                ),
            }
        a, b = extract(dossier_a), extract(dossier_b)
        return {
            "base": a, "compare": b,
            "delta": {key: b[key] - a[key] for key in a},
        }

    @staticmethod
    def _diff_expectations(dossier_a: dict[str, Any], dossier_b: dict[str, Any]) -> dict[str, Any]:
        """比较预期层：verified/rejected expectations 数量。"""
        def extract(dossier: dict[str, Any]) -> dict[str, Any]:
            ctx = dossier.get("expectation_context") or {}
            return {
                "verified_expectations": len(ctx.get("verified_expectations") or []),
                "rejected_expectations": len(ctx.get("rejected_expectations") or []),
            }
        a, b = extract(dossier_a), extract(dossier_b)
        return {"base": a, "compare": b, "delta": {key: b[key] - a[key] for key in a}}

    @staticmethod
    def _diff_valuation(dossier_a: dict[str, Any], dossier_b: dict[str, Any]) -> dict[str, Any]:
        """比较估值层：metrics 字典。"""
        def extract(dossier: dict[str, Any]) -> dict[str, Any]:
            valuation = dossier.get("valuation") or {}
            return dict(valuation.get("metrics") or {})
        a, b = extract(dossier_a), extract(dossier_b)
        return {
            "base": a, "compare": b,
            "added": {key: b[key] for key in b if key not in a},
            "removed": {key: a[key] for key in a if key not in b},
            "changed": {
                key: {"from": a[key], "to": b[key]}
                for key in a if key in b and a[key] != b[key]
            },
        }

    @staticmethod
    def _diff_gaps(
        dossier_a: dict[str, Any], dossier_b: dict[str, Any],
        bundle_a: dict[str, Any], bundle_b: dict[str, Any],
    ) -> dict[str, Any]:
        """比较缺口层：degradation_reasons。"""
        def extract(bundle: dict[str, Any]) -> list[str]:
            acceptance = bundle.get("acceptance") or {}
            return sorted({str(item) for item in acceptance.get("degradation_reasons") or []})
        a, b = extract(bundle_a), extract(bundle_b)
        return {
            "base": a, "compare": b,
            "added": [item for item in b if item not in a],
            "removed": [item for item in a if item not in b],
        }

    # ------------------------------------------------------------------
    # 迁移（从现有 run 目录幂等索引）
    # ------------------------------------------------------------------

    def migrate_from_disk(
        self,
        *,
        runs_root: str | Path | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """扫描 run 目录并写入索引；不修改原 JSON。

        幂等：已存在的 run_id 跳过，除非 force=True。

        Args:
            runs_root: 运行产物根目录；None 时使用构造函数的 runs_root。
            force: 是否强制重新索引已存在的 run_id。

        Returns:
            迁移统计：scanned / indexed / skipped / failed / degraded。
        """
        root = Path(runs_root) if runs_root else self.runs_root
        if not root.exists():
            return {
                "scanned": 0, "indexed": 0, "skipped": 0, "failed": 0,
                "degraded": 0, "runs_root": str(root), "reason": "runs_root_missing",
            }
        scanned = 0
        indexed = 0
        skipped = 0
        failed = 0
        degraded = 0
        existing_ids: set[str] = set()
        if not force:
            with self._connect() as conn:
                rows = conn.execute("SELECT run_id FROM research_value_runs").fetchall()
            existing_ids = {str(row[0]) for row in rows}
        for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            scanned += 1
            run_id = run_dir.name
            if run_id in existing_ids and not force:
                skipped += 1
                continue
            dossier = _safe_read_json(run_dir / "dossier.json")
            acceptance = _safe_read_json(run_dir / "acceptance.json")
            if dossier is None or acceptance is None:
                failed += 1
                degraded += 1
                if force:
                    self.index_run(
                        run_id=run_id, symbol="", as_of="", schema_version="",
                        status="degraded",
                        artifact_path=str(run_dir.resolve()),
                        source_fingerprint="",
                        model_versions={},
                        created_at=_utc_now_iso(),
                        payload={
                            "degradation_reason": "dossier_or_acceptance_missing_or_corrupt",
                        },
                    )
                continue
            try:
                self.index_dossier_run(
                    run_id=run_id, dossier=dossier, acceptance=acceptance,
                    artifact_path=str(run_dir.resolve()),
                    created_at=str(acceptance.get("finished_at") or _utc_now_iso()),
                )
                indexed += 1
            except Exception:
                failed += 1
                degraded += 1
        return {
            "scanned": scanned, "indexed": indexed, "skipped": skipped,
            "failed": failed, "degraded": degraded, "runs_root": str(root),
        }

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """将 sqlite Row 转为字典，反序列化 JSON 字段。"""
        data = dict(row)
        for key in ("model_versions_json", "payload_json"):
            raw = data.get(key)
            # 使用 removesuffix 而非 rstrip，避免字符集误伤（如 model_versions 末尾的 s/n/o）
            target = key.removesuffix("_json")
            if not raw:
                data[target] = {}
                continue
            try:
                data[target] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data[target] = {}
            del data[key]
        return data

    def stats(self) -> dict[str, Any]:
        """返回仓储统计摘要。"""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM research_value_runs"
            ).fetchone()[0]
            status_rows = conn.execute(
                "SELECT status, COUNT(*) FROM research_value_runs GROUP BY status"
            ).fetchall()
            symbol_rows = conn.execute(
                "SELECT symbol, COUNT(*) FROM research_value_runs "
                "WHERE symbol != '' GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 20"
            ).fetchall()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "db_path": str(self.db_path),
            "runs_root": str(self.runs_root),
            "total_runs": int(total),
            "status_distribution": {str(row[0]): int(row[1]) for row in status_rows},
            "top_symbols": {str(row[0]): int(row[1]) for row in symbol_rows},
        }
