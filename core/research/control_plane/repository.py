"""SQLite 研究控制面存储。使用显式 schema version 做幂等迁移。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TypeVar

from core.research_graph.evidence_grounding import verify_quote_grounding

from .contracts import (
    CalibrationRun, Claim, DocumentVersion, EvidenceRecord, FeedbackEvent,
    PolicyVersion, ResearchTask, ReviewItem, SourceTrace, StateTransition, _safe_int, _safe_text, utc_now,
)
from .policies import validate_state_transition

T = TypeVar("T")
logger = logging.getLogger(__name__)


class ResearchRepository:
    """SQLite 研究控制面存储，使用显式 schema version 做版本化迁移。

    迁移治理：
    - SCHEMA_VERSION 标记当前代码期望的最高 schema 版本
    - migrate() 读取 DB 中已记录的版本，按版本顺序执行增量迁移
    - 每个版本的迁移内容可追溯，支持从任意旧版本升级到最新版本
    """

    SCHEMA_VERSION = 4

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化研究控制面存储。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``RESEARCH_CONTROL_DB_PATH`` 读取，默认
                ``data/research_control/research_control.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "RESEARCH_CONTROL_DB_PATH",
                "data/research_control/research_control.db",
            )
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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

    def _read_db_schema_version(self, conn: sqlite3.Connection) -> int:
        """读取 DB 中已记录的 schema 版本；无记录时返回 0。"""
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return _safe_int(row[0], 0) if row else 0
        except (sqlite3.OperationalError, TypeError, ValueError, OverflowError):
            # schema_meta 表不存在
            return 0

    def migrate(self) -> None:
        """创建/升级 schema；按版本顺序增量迁移，可重复执行。

        版本历史：
        - v1: 初始 schema，包含 research_tasks/documents/claims 等核心表
        - v2: 新增 fact_outcomes/test_runs 表、transition_id 部分唯一索引、
              feedback dedup 索引、policy_versions domain 查询索引
        - v3: 新增 task_attempts 表，支持 Phase 3 跨日续跑 attempt 持久化
        - v4: 新增日预算汇总与 reservation 审计表，支持多 worker 原子预留
        """
        schema = {
            "research_tasks": ("task_id", "status", "created_at"),
            "documents": ("document_id", "status", "created_at"),
            "document_versions": ("document_version_id", "document_id", "retrieved_at"),
            "source_traces": ("source_trace_id", "lineage_id", "retrieved_at"),
            "claims": ("claim_id", "research_status", "updated_at"),
            "evidence": ("evidence_id", "claim_id", "created_at"),
            "claim_evidence": ("link_id", "claim_id", "created_at"),
            "state_transitions": ("transition_id", "claim_id", "created_at"),
            "review_items": ("review_id", "status", "created_at"),
            "feedback_events": ("feedback_id", "domain", "created_at"),
            "policy_versions": ("policy_version", "status", "created_at"),
            "calibration_runs": ("calibration_id", "status", "created_at"),
            "fact_outcomes": ("record_id", "fact_label", "created_at"),
            "test_runs": ("test_run_id", "code_version", "created_at"),
            # Phase 3: 跨日续跑 attempt 持久化表
            "task_attempts": ("attempt_id", "task_id", "created_at"),
        }
        with self._lock, self._connect() as conn:
            # 确保 schema_meta 表存在
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

            db_version = self._read_db_schema_version(conn)
            if db_version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported research schema version: {db_version}"
                )

            # v1: 创建所有核心表和基础索引
            for table, (pk, state_col, time_col) in schema.items():
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"{pk} TEXT PRIMARY KEY, {state_col} TEXT NOT NULL DEFAULT '', "
                    f"{time_col} TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL)"
                )
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{state_col} ON {table}({state_col})")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{time_col} ON {table}({time_col})")

            # v2: fact_outcomes / feedback / policy 版本化治理索引
            if db_version < 2:
                # fact_outcomes: transition_id 部分唯一索引保证幂等
                # 仅对 pending 状态记录唯一约束，已 resolve 的审计行可有多条
                # 先删除旧版全量唯一索引（若存在），再创建部分唯一索引
                conn.execute("DROP INDEX IF EXISTS idx_fact_outcomes_transition")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_outcomes_transition "
                    "ON fact_outcomes(json_extract(payload_json, '$.transition_id')) "
                    "WHERE json_extract(payload_json, '$.fact_label') = 'pending'"
                )
                # feedback_events: dedup_key + 时间窗口索引
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_feedback_dedup "
                    "ON feedback_events(json_extract(payload_json, '$.dedup_key'), "
                    "json_extract(payload_json, '$.created_at'))"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_feedback_domain_time "
                    "ON feedback_events(json_extract(payload_json, '$.domain'), "
                    "json_extract(payload_json, '$.created_at'))"
                )
                # policy_versions: domain + status 联合索引，加速 get_active_policy
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_policy_versions_domain_status "
                    "ON policy_versions(payload_json->'$.domain', status)"
                )
                # fact_outcomes: transition_id 查询索引（非唯一，用于审计链查询）
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fact_outcomes_transition_history "
                    "ON fact_outcomes(json_extract(payload_json, '$.transition_id'), "
                    "json_extract(payload_json, '$.created_at'))"
                )

            # v3: task_attempts 表索引（Phase 3 跨日续跑）
            # task_attempts 表已在 schema dict 中创建，这里补充索引
            if db_version < 3:
                # 按 task_id 查询最新 attempt 的索引
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_attempts_task_id "
                    "ON task_attempts(task_id, created_at)"
                )
                # 按 run_id 查询的索引（跨日续跑链路）
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_attempts_run_id "
                    "ON task_attempts(json_extract(payload_json, '$.run_id'), created_at)"
                )
                # 按 target_claim_id 查询的索引（避免重复创建 Claim）
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_attempts_claim_id "
                    "ON task_attempts(json_extract(payload_json, '$.target_claim_id'))"
                )

            # v4: SQLite 全局预算日汇总 + reservation 审计。预算管理器也
            # 会幂等创建这些表，以兼容直接传入 db_path 的调用方。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS research_budget_days (
                    budget_date TEXT PRIMARY KEY,
                    total_json TEXT NOT NULL,
                    reserved_json TEXT NOT NULL,
                    consumed_json TEXT NOT NULL,
                    released_json TEXT NOT NULL,
                    active_tasks INTEGER NOT NULL DEFAULT 0,
                    denied_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS research_budget_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    budget_date TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    action_id TEXT NOT NULL DEFAULT '',
                    requested_json TEXT NOT NULL,
                    allocated_json TEXT NOT NULL,
                    consumed_json TEXT NOT NULL,
                    released_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (budget_date, task_id, attempt_id, action_id)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_budget_reservations_date "
                "ON research_budget_reservations(budget_date, status, updated_at)"
            )

            # 更新 schema_version 到当前版本
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    def _upsert(self, table: str, pk: str, key: str, index_values: dict[str, Any], payload: dict[str, Any]) -> None:
        columns = [pk, *index_values.keys(), "payload_json"]
        values = [key, *index_values.values(), json.dumps(payload, ensure_ascii=False, allow_nan=False)]
        updates = ",".join(f"{col}=excluded.{col}" for col in columns[1:])
        placeholders = ",".join("?" for _ in columns)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders}) "
                f"ON CONFLICT({pk}) DO UPDATE SET {updates}", values,
            )

    def _get(self, table: str, pk: str, key: str, cls: type[T]) -> T | None:
        if not isinstance(key, str) or not key:
            return None
        with self._connect() as conn:
            row = conn.execute(f"SELECT payload_json FROM {table} WHERE {pk}=?", (key,)).fetchone()
        payload = self._decode_payload(row[0]) if row else None
        if payload is None:
            return None
        try:
            return cls.from_dict(payload)
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("invalid %s payload for %s: %s", cls.__name__, key, exc)
            return None

    def _list(self, table: str, cls: type[T], where: str = "", params: tuple[Any, ...] = (), limit: int = 100) -> list[T]:
        query = f"SELECT payload_json FROM {table}"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY rowid DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*params, self._safe_limit(limit))).fetchall()
        result: list[T] = []
        for row in rows:
            payload = self._decode_payload(row[0])
            if payload is not None:
                try:
                    result.append(cls.from_dict(payload))
                except (TypeError, ValueError, KeyError) as exc:
                    logger.warning("invalid %s payload skipped: %s", cls.__name__, exc)
        return result

    @staticmethod
    def _decode_payload(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, str):
            return None
        try:
            payload = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return dict(payload) if isinstance(payload, Mapping) else None

    @staticmethod
    def _safe_limit(value: Any) -> int:
        return max(1, min(_safe_int(value, 100), 1000))

    def save_task(self, item: ResearchTask) -> None:
        if not isinstance(item, ResearchTask):
            raise TypeError("期望 ResearchTask")
        item.updated_at = utc_now()
        self._upsert("research_tasks", "task_id", item.task_id, {"status": item.status, "created_at": item.created_at}, item.to_dict())

    def get_task(self, task_id: str) -> ResearchTask | None:
        return self._get("research_tasks", "task_id", task_id, ResearchTask)

    def list_tasks(self, status: str = "", limit: int = 100) -> list[ResearchTask]:
        return self._list("research_tasks", ResearchTask, "status=?" if status else "", (status,) if status else (), limit)

    def save_source_trace(self, item: SourceTrace) -> None:
        if not isinstance(item, SourceTrace):
            raise TypeError("期望 SourceTrace")
        self._upsert("source_traces", "source_trace_id", item.source_trace_id, {"lineage_id": item.lineage_id, "retrieved_at": item.retrieved_at}, item.to_dict())

    def save_document(self, item: DocumentVersion) -> None:
        if not isinstance(item, DocumentVersion):
            raise TypeError("期望 DocumentVersion")
        self._upsert("documents", "document_id", item.document_id, {"status": item.parse_status, "created_at": item.retrieved_at}, {"document_id": item.document_id, "status": item.parse_status, "created_at": item.retrieved_at})
        self._upsert("document_versions", "document_version_id", item.document_version_id, {"document_id": item.document_id, "retrieved_at": item.retrieved_at}, item.to_dict())

    def save_claim(self, item: Claim) -> None:
        """保存 Claim 并校验 research_status 迁移合法性 + 证据数量。

        - 新建 Claim（claim_id 不存在）：只允许初始状态（pending/discovered）
        - 更新 Claim：research_status 变化时调用 validate_state_transition 校验
        - 实际状态迁移（from_status 非空）时自动创建 StateTransition 审计记录
        - 迁移到 supported/active/trusted 时要求至少 1 条 support 证据

        Args:
            item: 待保存的 Claim 实例。

        Raises:
            ValueError: 当状态迁移非法时，message 为 "INVALID_STATE_TRANSITION"；
                        当证据数量不足时，message 为 "INSUFFICIENT_EVIDENCE"。
        """
        if not isinstance(item, Claim):
            raise TypeError("期望 Claim")
        # 读取已有 Claim 的当前 research_status
        existing = self.get_claim(item.claim_id)
        old_status = existing.research_status if existing is not None else ""
        new_status = item.research_status or ""

        # 状态变化时校验迁移合法性
        if old_status.lower() != new_status.lower():
            validate_state_transition(old_status, new_status)

            # 证据数量门禁：迁移到 supported/active/trusted 需 ≥1 条 support evidence
            if new_status.lower() in {"supported", "active", "trusted"}:
                if not self._has_support_evidence(item.claim_id):
                    raise ValueError("INSUFFICIENT_EVIDENCE")

        item.updated_at = utc_now()
        self._upsert("claims", "claim_id", item.claim_id, {"research_status": item.research_status, "updated_at": item.updated_at}, item.to_dict())

        # 实际状态迁移（非初始创建）时自动创建 StateTransition 审计记录
        if old_status and old_status.lower() != new_status.lower():
            self._record_state_transition(item, old_status, new_status)

    def _has_support_evidence(self, claim_id: str) -> bool:
        claim_id = _safe_text(claim_id)
        if not claim_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM evidence "
                "WHERE claim_id=? AND json_extract(payload_json, '$.stance')=? "
                "LIMIT 1",
                (claim_id, "support"),
            ).fetchone()
        return row is not None

    def _record_state_transition(self, claim: Claim, from_status: str, to_status: str) -> None:
        """创建并保存 StateTransition 审计记录。

        Args:
            claim: 发生状态迁移的 Claim。
            from_status: 迁移前状态；空串表示新建。
            to_status: 迁移后状态。
        """
        timestamp = utc_now()
        # transition_id = sha1(claim_id|from|to|timestamp)[:16]
        raw = f"{claim.claim_id}|{from_status}|{to_status}|{timestamp}"
        transition_id = "tr_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

        # 构建 evidence_snapshot：当前 Claim 关联的证据摘要
        evidence_list = self.list_evidence(claim.claim_id, limit=1000)
        evidence_snapshot: dict[str, Any] = {
            "evidence_count": len(evidence_list),
            "evidence_ids": [ev.evidence_id for ev in evidence_list[:20]],
            "support_count": sum(1 for ev in evidence_list if ev.stance == "support"),
            "counter_count": sum(1 for ev in evidence_list if ev.stance == "counter"),
        }

        transition = StateTransition(
            transition_id=transition_id,
            claim_id=claim.claim_id,
            from_status=from_status,
            to_status=to_status,
            policy_version="v1",
            evidence_snapshot=evidence_snapshot,
            created_at=timestamp,
            changed_at=timestamp,
            operator="system",
        )
        self.save_transition(transition)

    def get_claim(self, claim_id: str) -> Claim | None:
        return self._get("claims", "claim_id", claim_id, Claim)

    def list_claims(self, status: str = "", limit: int = 100) -> list[Claim]:
        return self._list("claims", Claim, "research_status=?" if status else "", (status,) if status else (), limit)

    def save_evidence(self, item: EvidenceRecord) -> bool:
        """保存 EvidenceRecord，执行伪证据拒绝、source_id 校验、stance 严格校验和 quote grounding 校验。

        校验顺序：
        1. evidence_kind 黑名单拒绝（generated/pseudo_evidence/retrieval_hypothesis/query_prototype）
        2. source_id 校验：source_trace_id / document_version_id / lineage_id 至少一个非空
        3. stance 严格校验：非法值拒绝保存（不再静默改 support）
        4. quote grounding 校验（fail-closed）：quote 和 document_version_id 同时存在时，
           文档不存在或 quote 无法在文档原文中定位均拒绝保存

        Args:
            item: 待保存的 EvidenceRecord 实例。

        Returns:
            True 表示保存成功；False 表示校验未通过（静默拒绝，不抛异常）。
        """
        if not isinstance(item, EvidenceRecord):
            return False
        # 1. 伪证据黑名单拒绝
        if item.evidence_kind in {"generated", "pseudo_evidence", "retrieval_hypothesis", "query_prototype"}:
            return False

        # 2. source_id 校验：至少一个来源标识非空（与 EvidenceLedger 一致）
        if not (item.source_trace_id or item.document_version_id or item.lineage_id):
            return False

        # 3. stance 严格校验：非法值拒绝保存（不再静默改 support）
        if item.stance not in {"support", "counter", "neutral"}:
            return False

        # 4. quote grounding 校验（fail-closed：文档不存在或 grounding 失败都拒绝）
        if item.quote and item.document_version_id:
            document_text = self._fetch_document_text(item.document_version_id)
            if not document_text:
                # 文档不存在：fail-closed 拒绝
                return False
            if not verify_quote_grounding(item.quote, document_text):
                return False

        self._upsert("evidence", "evidence_id", item.evidence_id, {"claim_id": item.claim_id, "created_at": item.created_at}, item.to_dict())
        link_id = f"{item.claim_id}:{item.evidence_id}"
        self._upsert("claim_evidence", "link_id", link_id, {"claim_id": item.claim_id, "created_at": item.created_at}, {"link_id": link_id, "claim_id": item.claim_id, "evidence_id": item.evidence_id, "created_at": item.created_at})
        return True

    def _fetch_document_text(self, document_version_id: str) -> str:
        """按 document_version_id 查询文档正文内容。

        Args:
            document_version_id: 文档版本 ID。

        Returns:
            文档正文文本；文档不存在或内容为空时返回空字符串。
        """
        document_version = self._get(
            "document_versions", "document_version_id", document_version_id, DocumentVersion,
        )
        if document_version is None:
            return ""
        return document_version.content_text or ""

    def list_evidence(self, claim_id: str = "", limit: int = 200) -> list[EvidenceRecord]:
        return self._list("evidence", EvidenceRecord, "claim_id=?" if claim_id else "", (claim_id,) if claim_id else (), limit)

    def save_transition(self, item: StateTransition) -> None:
        if not isinstance(item, StateTransition):
            raise TypeError("期望 StateTransition")
        self._upsert("state_transitions", "transition_id", item.transition_id, {"claim_id": item.claim_id, "created_at": item.created_at}, item.to_dict())

    def list_transitions(self, claim_id: str, limit: int = 100) -> list[StateTransition]:
        return self._list("state_transitions", StateTransition, "claim_id=?", (claim_id,), limit)

    def save_review(self, item: ReviewItem) -> None:
        if not isinstance(item, ReviewItem):
            raise TypeError("期望 ReviewItem")
        self._upsert("review_items", "review_id", item.review_id, {"status": item.status, "created_at": item.created_at}, item.to_dict())

    def get_review(self, review_id: str) -> ReviewItem | None:
        return self._get("review_items", "review_id", review_id, ReviewItem)

    def list_reviews(self, status: str = "pending", limit: int = 100) -> list[ReviewItem]:
        return self._list("review_items", ReviewItem, "status=?" if status else "", (status,) if status else (), limit)

    def save_feedback(self, item: FeedbackEvent) -> None:
        if not isinstance(item, FeedbackEvent):
            raise TypeError("期望 FeedbackEvent")
        self._upsert("feedback_events", "feedback_id", item.feedback_id, {"domain": item.domain, "created_at": item.created_at}, item.to_dict())

    def list_feedback(self, domain: str = "", limit: int = 100) -> list[FeedbackEvent]:
        return self._list("feedback_events", FeedbackEvent, "domain=?" if domain else "", (domain,) if domain else (), limit)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """返回一个事务上下文，检查与写入在同一事务中完成。

        用于 FeedbackController 的事务化去重/限流/深度控制。
        """
        with self._lock, self._connect() as conn:
            yield conn

    def save_fact_outcome(self, record: Any) -> None:
        """保存 FactOutcomeRecord。"""
        from .fact_outcome import FactOutcomeRecord
        if not isinstance(record, FactOutcomeRecord):
            raise TypeError(f"期望 FactOutcomeRecord，得到 {type(record)}")
        self._upsert(
            "fact_outcomes", "record_id", record.record_id,
            {"fact_label": record.fact_label, "created_at": record.created_at},
            record.to_dict(),
        )

    def get_fact_outcome(self, record_id: str) -> Any | None:
        """按 record_id 查询 FactOutcomeRecord。"""
        from .fact_outcome import FactOutcomeRecord
        return self._get("fact_outcomes", "record_id", record_id, FactOutcomeRecord)

    def get_fact_outcome_by_transition(self, transition_id: str) -> Any | None:
        """按 transition_id 查询 FactOutcomeRecord（幂等检查）。"""
        from .fact_outcome import FactOutcomeRecord
        transition_id = _safe_text(transition_id)
        if not transition_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM fact_outcomes WHERE json_extract(payload_json, '$.transition_id')=?",
                (transition_id,),
            ).fetchone()
        payload = self._decode_payload(row[0]) if row else None
        return FactOutcomeRecord.from_dict(payload) if payload is not None else None

    def list_fact_outcomes(self, fact_label: str = "", limit: int = 100) -> list[Any]:
        """列出 FactOutcomeRecord，按 created_at 降序。"""
        from .fact_outcome import FactOutcomeRecord
        return self._list(
            "fact_outcomes", FactOutcomeRecord,
            "fact_label=?" if fact_label else "",
            (fact_label,) if fact_label else (),
            limit,
        )

    def list_fact_outcome_history(self, transition_id: str, limit: int = 100) -> list[Any]:
        """按 transition_id 查询审计链，按 created_at 升序返回。

        同一 transition_id 的多条审计行按创建时间升序排列，
        确保审计链顺序稳定，不受 rowid 插入顺序影响。

        Args:
            transition_id: 事实迁移 ID。
            limit: 最大返回数量。

        Returns:
            按 created_at 升序排列的 FactOutcomeRecord 列表。
        """
        from .fact_outcome import FactOutcomeRecord
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM fact_outcomes "
                "WHERE json_extract(payload_json, '$.transition_id')=? "
                "ORDER BY json_extract(payload_json, '$.created_at') ASC LIMIT ?",
                (transition_id, self._safe_limit(limit)),
            ).fetchall()
        result = []
        for row in rows:
            payload = self._decode_payload(row[0])
            if payload is not None:
                result.append(FactOutcomeRecord.from_dict(payload))
        return result

    def save_test_run(self, record: Any) -> None:
        """保存 TestRunRecord。"""
        from .test_run_record import TestRunRecord
        if not isinstance(record, TestRunRecord):
            raise TypeError(f"期望 TestRunRecord，得到 {type(record)}")
        self._upsert(
            "test_runs", "test_run_id", record.test_run_id,
            {"code_version": record.code_version, "created_at": record.created_at},
            record.to_dict(),
        )

    def get_test_run(self, test_run_id: str) -> Any | None:
        """按 test_run_id 查询 TestRunRecord。"""
        from .test_run_record import TestRunRecord
        return self._get("test_runs", "test_run_id", test_run_id, TestRunRecord)

    def list_test_runs(self, limit: int = 100) -> list[Any]:
        """列出 TestRunRecord，按创建时间倒序。"""
        from .test_run_record import TestRunRecord
        return self._list("test_runs", TestRunRecord, limit=limit)

    def save_calibration(self, item: CalibrationRun) -> None:
        if not isinstance(item, CalibrationRun):
            raise TypeError("期望 CalibrationRun")
        self._upsert("calibration_runs", "calibration_id", item.calibration_id, {"status": item.status, "created_at": item.created_at}, item.to_dict())

    def get_calibration(self, calibration_id: str) -> CalibrationRun | None:
        return self._get("calibration_runs", "calibration_id", calibration_id, CalibrationRun)

    def list_calibrations(self, limit: int = 100) -> list[CalibrationRun]:
        return self._list("calibration_runs", CalibrationRun, limit=limit)

    def save_policy(self, item: PolicyVersion) -> None:
        if not isinstance(item, PolicyVersion):
            raise TypeError("期望 PolicyVersion")
        self._upsert("policy_versions", "policy_version", item.policy_version, {"status": item.status, "created_at": item.created_at}, item.to_dict())

    def list_policies(self, status: str = "", limit: int = 100, domain: str = "") -> list[PolicyVersion]:
        """列出策略版本，支持按 status 和 domain 过滤。"""
        conditions: list[str] = []
        params: tuple = ()
        if status:
            conditions.append("status=?")
            params = params + (status,)
        if domain:
            conditions.append("domain=?")
            params = params + (domain,)
        where = " AND ".join(conditions) if conditions else ""
        return self._list("policy_versions", PolicyVersion, where, params, limit)

    def get_policy(self, policy_version: str) -> PolicyVersion | None:
        """按 policy_version 查询单个策略。"""
        return self._get("policy_versions", "policy_version", policy_version, PolicyVersion)

    def get_active_policy(self, domain: str) -> PolicyVersion | None:
        """返回指定反馈域最近启用的策略；其他域的参数绝不串用。

        利用 policy_versions 表的 domain + status 联合索引加速查询。
        """
        domain = _safe_text(domain)
        if not domain:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM policy_versions "
                "WHERE status=? AND json_extract(payload_json, '$.domain')=? "
                "ORDER BY rowid DESC LIMIT 1",
                ("active", domain),
            ).fetchone()
        payload = self._decode_payload(row[0]) if row else None
        return PolicyVersion.from_dict(payload) if payload is not None else None

    def stats(self) -> dict[str, Any]:
        tables = ("research_tasks", "claims", "evidence", "state_transitions", "review_items", "feedback_events", "calibration_runs")
        with self._connect() as conn:
            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        return {"schema_version": self.SCHEMA_VERSION, "db_path": str(self.db_path), "counts": counts}

    def acceptance_report(self, as_of: str = "") -> dict[str, Any]:
        """生成可机器读取的迁移/降级/状态/血缘/PIT 验收摘要。"""
        with self._connect() as conn:
            stored = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("documents", "document_versions", "claims", "evidence")
            }
        tasks = self.list_tasks(limit=1000)
        claims = self.list_claims(limit=1000)
        evidence = self.list_evidence(limit=1000)
        with self._connect() as conn:
            attempt_payloads: list[dict[str, Any]] = []
            for row in conn.execute(
                "SELECT payload_json FROM task_attempts ORDER BY rowid DESC LIMIT 5000"
            ).fetchall():
                try:
                    payload = json.loads(row[0])
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    attempt_payloads.append(payload)
        state_distribution: dict[str, int] = {}
        for claim in claims:
            state_distribution[claim.research_status] = state_distribution.get(claim.research_status, 0) + 1
        lineages = {
            item.lineage_id or item.correlation_group or item.source_trace_id or item.evidence_id
            for item in evidence
        }
        future_blocked = 0
        unknown_time_excluded = 0
        if as_of:
            from .policies import evidence_visible_as_of, parse_time
            for item in evidence:
                if parse_time(item.published_at) is None and parse_time(item.retrieved_at) is None:
                    unknown_time_excluded += 1
                elif not evidence_visible_as_of(item, as_of, historical=True):
                    future_blocked += 1
        first_party_count = sum(1 for item in evidence if item.is_first_party)
        snippet_intercepted = 0
        market_intercepted = 0
        degradation_reasons: dict[str, int] = {}
        for task in tasks:
            checkpoint = task.checkpoint or {}
            retrieval_stats = checkpoint.get("retrieval_stats", {}) or {}
            retrieval_stats = retrieval_stats if isinstance(retrieval_stats, Mapping) else {}
            snippet_intercepted += _safe_int(retrieval_stats.get("snippet_documents_ignored"))
            market_intercepted += _safe_int(retrieval_stats.get("market_documents_ignored"))
            degradation_codes = checkpoint.get("degradation_codes", [])
            degradation_codes = degradation_codes if isinstance(degradation_codes, (list, tuple)) else []
            for code in degradation_codes:
                key = str(code)
                degradation_reasons[key] = degradation_reasons.get(key, 0) + 1
            failure_code = str((task.failure or {}).get("code") or "")
            if failure_code:
                degradation_reasons[failure_code] = degradation_reasons.get(failure_code, 0) + 1
        for attempt in attempt_payloads:
            degradation_codes = attempt.get("degradation_codes", [])
            degradation_codes = degradation_codes if isinstance(degradation_codes, (list, tuple)) else []
            for code in degradation_codes:
                key = str(code)
                degradation_reasons[key] = degradation_reasons.get(key, 0) + 1

        from .budget_manager import GlobalBudgetManager

        budget_date = str(as_of or "")[:10] or None
        budget_snapshot = GlobalBudgetManager(
            repository=self, budget_date=budget_date
        ).snapshot(budget_date=budget_date)
        budget_payload = budget_snapshot.to_dict()
        attempt_status_distribution: dict[str, int] = {}
        for attempt in attempt_payloads:
            status = str(attempt.get("status") or "unknown")
            attempt_status_distribution[status] = attempt_status_distribution.get(status, 0) + 1
        feedback_calibration = {
            "feedback": "started" if self.list_feedback(limit=1) else "not_started",
            "calibration": "started" if self.list_calibrations(limit=1) else "not_started",
        }
        return {
            "as_of": as_of,
            "migration_counts": stored,
            "failed_degradations": {
                "failed_tasks": sum(1 for item in tasks if item.status in {"failed", "timeout", "source_unavailable"}),
                "unknown_time_excluded": unknown_time_excluded,
            },
            "claim_state_distribution": state_distribution,
            "source_independence_rate": round(len(lineages) / len(evidence), 4) if evidence else 0.0,
            "first_party_evidence_count": first_party_count,
            "first_party_evidence_rate": round(first_party_count / len(evidence), 4) if evidence else 0.0,
            "future_evidence_blocked": future_blocked,
            "attempt_count": len(attempt_payloads),
            "attempt_status_distribution": attempt_status_distribution,
            "budget": budget_payload,
            "budget_reservation_count": budget_snapshot.reservation_count,
            "budget_reserved": budget_payload.get("reserved", {}),
            "budget_consumed": budget_payload.get("consumed", {}),
            "snippet_documents_intercepted": snippet_intercepted,
            "market_documents_intercepted": market_intercepted,
            "evidence_gates": {
                "snippet_documents_intercepted": snippet_intercepted,
                "market_documents_intercepted": market_intercepted,
            },
            "degradation_reasons": degradation_reasons,
            "feedback_calibration": feedback_calibration,
            "feedback_calibration_status": feedback_calibration,
        }
