"""OutcomeTracker：Alpha路径结果追踪与反馈闭环。

记录每条promoted路径的评分特征与事后收益，为贝叶斯权重校准提供训练数据。
支持SQLite持久化和增量写入。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else str(value or "").strip()


def _finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _bounded_score(value: Any) -> float:
    number = _finite_float(value, 0.0) or 0.0
    return max(0.0, min(1.0, number))


def _safe_path(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text:
            result.append(text)
    return result


def _safe_features(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        _text(key): _bounded_score(item)
        for key, item in value.items()
        if _text(key)
    }


def _safe_metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_limit(value: Any, default: int = 1000, maximum: int = 10_000) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        limit = default
    return max(0, min(limit, maximum))


def _safe_days(value: Any, default: int = 60) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError, OverflowError):
        days = default
    return max(0, min(days, 3650))


def _decode_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


@dataclass
class AlphaOutcome:
    """单条Alpha路径的评分特征与事后结果。"""

    record_id: str = ""
    path_id: str = ""
    root_entity: str = ""
    target_entity: str = ""
    path: list[str] = field(default_factory=list)
    promoted_at: str = ""
    # 评分时刻的特征向量（维度名->分数）
    features: dict[str, float] = field(default_factory=dict)
    final_score: float = 0.0
    quality_label: str = ""
    # 事后结果
    resolved_at: str = ""
    forward_return_20d: float | None = None
    forward_return_60d: float | None = None
    forward_return_120d: float | None = None
    excess_return_20d: float | None = None
    excess_return_60d: float | None = None
    outcome_label: str = ""  # success / failure / neutral / pending
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "path_id": self.path_id,
            "root_entity": self.root_entity,
            "target_entity": self.target_entity,
            "path": self.path,
            "promoted_at": self.promoted_at,
            "features": self.features,
            "final_score": self.final_score,
            "quality_label": self.quality_label,
            "resolved_at": self.resolved_at,
            "forward_return_20d": self.forward_return_20d,
            "forward_return_60d": self.forward_return_60d,
            "forward_return_120d": self.forward_return_120d,
            "excess_return_20d": self.excess_return_20d,
            "excess_return_60d": self.excess_return_60d,
            "outcome_label": self.outcome_label,
            "metadata": self.metadata,
        }


class OutcomeTracker:
    """Alpha结果追踪器，提供反馈闭环数据。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化追踪器。

        Args:
            db_path: SQLite数据库路径，None则使用默认路径
        """
        self._lock = threading.RLock()
        if db_path:
            self._db_path = Path(db_path)
        else:
            self._db_path = Path(__file__).resolve().parents[4] / "data" / "outcome_tracker.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表。"""
        with self._lock, sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alpha_outcomes (
                    record_id TEXT PRIMARY KEY,
                    path_id TEXT NOT NULL,
                    root_entity TEXT NOT NULL,
                    target_entity TEXT NOT NULL,
                    path_json TEXT NOT NULL,
                    promoted_at TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    final_score REAL NOT NULL,
                    quality_label TEXT NOT NULL DEFAULT '',
                    resolved_at TEXT DEFAULT '',
                    forward_return_20d REAL,
                    forward_return_60d REAL,
                    forward_return_120d REAL,
                    excess_return_20d REAL,
                    excess_return_60d REAL,
                    outcome_label TEXT NOT NULL DEFAULT 'pending',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_promoted ON alpha_outcomes(promoted_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_label ON alpha_outcomes(outcome_label)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_target ON alpha_outcomes(target_entity)")
            conn.commit()

    def record_promotion(
        self,
        path_id: str,
        root_entity: str,
        target_entity: str,
        path: list[str],
        features: dict[str, float],
        final_score: float,
        quality_label: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """记录一条Alpha路径晋升事件。

        Args:
            path_id: 路径唯一ID
            root_entity: 根概念
            target_entity: 目标实体（公司）
            path: 路径节点列表
            features: 评分时刻各维度分数
            final_score: 最终评分
            quality_label: 质量标签
            metadata: 额外元数据

        Returns:
            record_id
        """
        import hashlib
        path_id = _text(path_id)
        root_entity = _text(root_entity)
        target_entity = _text(target_entity)
        path = _safe_path(path)
        features = _safe_features(features)
        final_score = _bounded_score(final_score)
        quality_label = _text(quality_label)
        metadata = _safe_metadata(metadata)
        now = datetime.now().isoformat()
        record_id = hashlib.sha256(
            f"{path_id}|{now}|{final_score:.4f}".encode()
        ).hexdigest()[:16]

        outcome = AlphaOutcome(
            record_id=record_id,
            path_id=path_id,
            root_entity=root_entity,
            target_entity=target_entity,
            path=list(path),
            promoted_at=now,
            features=dict(features),
            final_score=final_score,
            quality_label=quality_label,
            metadata=metadata,
        )

        with self._lock, sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO alpha_outcomes
                   (record_id, path_id, root_entity, target_entity, path_json,
                    promoted_at, features_json, final_score, quality_label, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id, path_id, root_entity, target_entity,
                    json.dumps(path, ensure_ascii=False),
                    now,
                    json.dumps(features, ensure_ascii=False),
                    final_score, quality_label,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            conn.commit()
        logger.debug("Recorded promotion: %s -> %s (score=%.3f)", path_id, target_entity, final_score)
        return record_id

    def resolve_outcome(
        self,
        record_id: str,
        forward_return_20d: float | None = None,
        forward_return_60d: float | None = None,
        forward_return_120d: float | None = None,
        excess_return_20d: float | None = None,
        excess_return_60d: float | None = None,
        outcome_label: str | None = None,
    ) -> bool:
        """记录一条路径的事后结果。

        Args:
            record_id: 记录ID
            forward_return_*: 远期绝对收益
            excess_return_*: 远期超额收益
            outcome_label: success/failure/neutral，None则自动判定

        Returns:
            是否成功更新
        """
        now = datetime.now().isoformat()
        forward_return_20d = _finite_float(forward_return_20d)
        forward_return_60d = _finite_float(forward_return_60d)
        forward_return_120d = _finite_float(forward_return_120d)
        excess_return_20d = _finite_float(excess_return_20d)
        excess_return_60d = _finite_float(excess_return_60d)
        record_id = _text(record_id)
        if outcome_label is not None:
            outcome_label = _text(outcome_label).lower()
            if outcome_label not in {"success", "failure", "neutral", "pending"}:
                outcome_label = "pending"
        if outcome_label is None:
            # 自动判定：60日超额收益>5%为success，<-5%为failure
            if excess_return_60d is not None:
                if excess_return_60d > 0.05:
                    outcome_label = "success"
                elif excess_return_60d < -0.05:
                    outcome_label = "failure"
                else:
                    outcome_label = "neutral"
            elif forward_return_60d is not None:
                if forward_return_60d > 0.10:
                    outcome_label = "success"
                elif forward_return_60d < -0.10:
                    outcome_label = "failure"
                else:
                    outcome_label = "neutral"
            else:
                outcome_label = "pending"

        with self._lock, sqlite3.connect(str(self._db_path)) as conn:
            cursor = conn.execute(
                """UPDATE alpha_outcomes SET
                    resolved_at=?, forward_return_20d=?, forward_return_60d=?,
                    forward_return_120d=?, excess_return_20d=?, excess_return_60d=?,
                    outcome_label=?
                   WHERE record_id=?""",
                (now, forward_return_20d, forward_return_60d, forward_return_120d,
                 excess_return_20d, excess_return_60d, outcome_label, record_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_labeled_outcomes(
        self,
        min_label: str = "success",
        since: str | None = None,
        limit: int = 1000,
    ) -> list[AlphaOutcome]:
        """获取有标签的结果用于训练。

        Args:
            min_label: 最低标签（success/failure/neutral）
            since: 起始时间ISO字符串
            limit: 最大返回数量

        Returns:
            AlphaOutcome列表
        """
        label_order = {"pending": 0, "neutral": 1, "failure": 2, "success": 2}
        min_level = label_order.get(_text(min_label).lower(), 0)

        query = "SELECT * FROM alpha_outcomes WHERE outcome_label != 'pending'"
        params: list[Any] = []
        if since:
            query += " AND promoted_at >= ?"
            params.append(since)
        query += " ORDER BY promoted_at DESC LIMIT ?"
        params.append(_safe_limit(limit))

        results: list[AlphaOutcome] = []
        with self._lock, sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(query, params):
                if label_order.get(row["outcome_label"], 0) < min_level:
                    continue
                try:
                    outcome = AlphaOutcome(
                        record_id=row["record_id"],
                        path_id=row["path_id"],
                        root_entity=row["root_entity"],
                        target_entity=row["target_entity"],
                        path=_decode_json(row["path_json"], []),
                        promoted_at=row["promoted_at"],
                        features=_decode_json(row["features_json"], {}),
                        final_score=row["final_score"],
                        quality_label=row["quality_label"],
                        resolved_at=row["resolved_at"] or "",
                        forward_return_20d=row["forward_return_20d"],
                        forward_return_60d=row["forward_return_60d"],
                        forward_return_120d=row["forward_return_120d"],
                        excess_return_20d=row["excess_return_20d"],
                        excess_return_60d=row["excess_return_60d"],
                        outcome_label=row["outcome_label"],
                        metadata=_decode_json(row["metadata_json"] or "{}", {}),
                    )
                    if not isinstance(outcome.path, list):
                        raise ValueError("path must be a list")
                    if not isinstance(outcome.features, Mapping):
                        raise ValueError("features must be a mapping")
                    if not isinstance(outcome.metadata, Mapping):
                        raise ValueError("metadata must be a mapping")
                    results.append(outcome)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.warning("Skipping malformed outcome %s: %s", row["record_id"], e)
        return results

    def get_pending_resolutions(self, days_elapsed: int = 60) -> list[AlphaOutcome]:
        """获取需要resolve的过期记录。

        Args:
            days_elapsed: 晋升后超过多少天的记录需要resolve

        Returns:
            待resolve的记录列表
        """
        cutoff = (datetime.now() - timedelta(days=_safe_days(days_elapsed))).isoformat()
        results: list[AlphaOutcome] = []
        with self._lock, sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT * FROM alpha_outcomes WHERE outcome_label='pending' AND promoted_at <= ?",
                (cutoff,),
            ):
                try:
                    path = _decode_json(row["path_json"], [])
                    features = _decode_json(row["features_json"], {})
                    if not isinstance(path, list) or not isinstance(features, Mapping):
                        continue
                    results.append(AlphaOutcome(
                        record_id=row["record_id"],
                        path_id=row["path_id"],
                        root_entity=row["root_entity"],
                        target_entity=row["target_entity"],
                        path=path,
                        promoted_at=row["promoted_at"],
                        features=features,
                        final_score=row["final_score"],
                        quality_label=row["quality_label"],
                    ))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        return results

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        with self._lock, sqlite3.connect(str(self._db_path)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM alpha_outcomes").fetchone()[0]
            by_label = {}
            for row in conn.execute(
                "SELECT outcome_label, COUNT(*) as cnt FROM alpha_outcomes GROUP BY outcome_label"
            ):
                by_label[row[0]] = row[1]
            resolved = sum(v for k, v in by_label.items() if k != "pending")
            return {
                "total_records": total,
                "resolved_records": resolved,
                "by_label": by_label,
                "db_path": str(self._db_path),
            }
