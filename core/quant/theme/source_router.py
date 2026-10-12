"""主题数据源统一路由器。

管理东财/同花顺/新闻/公告/last-good snapshot，东财失败时按序降级。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class ConceptBoard:
    """概念板块原始数据。"""
    board_name: str = ""
    board_id: str = ""
    provider: str = ""  # eastmoney / ths
    members: list[str] = field(default_factory=list)  # symbol 列表
    raw_response_hash: str = ""
    fetched_at: str = ""
    change_pct: float | None = None
    amount: float | None = None
    reported_member_count: int = 0
    members_truncated: bool = False
    # Discovery sampling bucket from the market ranking.  This is strategy
    # input metadata, not a concept classification, and prevents the member
    # resolver from guessing semantics from list positions.
    discovery_bucket: str = ""
    # 截断结构化信息：下游候选质量控制可感知成员是否被截断。
    requested_count: int = 0  # 请求的成员数量
    returned_count: int = 0  # 实际返回的成员数量（等于 len(members)）
    reported_total: int = 0  # 数据源报告的总成员数（reported_member_count 的更清晰命名）
    coverage_ratio: float | None = None  # 覆盖率（returned_count / reported_total），分母不可信时为 None
    pagination_supported: bool = False  # 数据源是否支持分页
    next_page_available: bool = False  # 是否有下一页

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "board_name": self.board_name,
            "board_id": self.board_id,
            "provider": self.provider,
            "members": self.members,
            "raw_response_hash": self.raw_response_hash,
            "fetched_at": self.fetched_at,
            "change_pct": self.change_pct,
            "amount": self.amount,
            "reported_member_count": self.reported_member_count,
            "members_truncated": self.members_truncated,
            "discovery_bucket": self.discovery_bucket,
            "requested_count": self.requested_count,
            "returned_count": self.returned_count,
            "reported_total": self.reported_total,
            "coverage_ratio": self.coverage_ratio,
            "pagination_supported": self.pagination_supported,
            "next_page_available": self.next_page_available,
        }


@dataclass
class SourceSnapshot:
    """数据源快照。"""
    snapshot_id: str = ""
    trade_date: str = ""
    fetched_at: str = ""
    providers_used: list[str] = field(default_factory=list)
    boards: list[ConceptBoard] = field(default_factory=list)
    is_trading_hours: bool = False
    source_as_of: str = ""
    degraded_reasons: list[str] = field(default_factory=list)
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "snapshot_id": self.snapshot_id,
            "trade_date": self.trade_date,
            "fetched_at": self.fetched_at,
            "providers_used": self.providers_used,
            "boards": [b.to_dict() for b in self.boards],
            "is_trading_hours": self.is_trading_hours,
            "source_as_of": self.source_as_of,
            "degraded_reasons": self.degraded_reasons,
            "content_hash": self.content_hash,
        }


class ThemeSnapshotStore:
    """版本化主题快照存储（SQLite）。

    使用 SQLite 持久化历史主题快照，支持按 source_as_of 查询合法历史版本。
    不从未来快照倒推历史：查询时仅返回 source_as_of <= requested_as_of 的最新版本。

    幂等键：(provider, source_as_of, content_hash)。
    相同 content_hash 在不同 source_as_of 必须保留新的可见性记录。

    Schema 拆分：
    - theme_snapshot_blobs: 不可变内容存储（content_hash PK, boards_json）
    - theme_snapshot_observations: 每次抓取的可见性记录（含 provider/source_as_of/fetched_at）
    - theme_source_snapshots: 旧表保留用于向后兼容读取（不再写入）
    """

    DEFAULT_DB_PATH = "data/theme_control/theme_snapshots.db"

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化 SQLite 快照存储并执行 schema 迁移。

        Args:
            db_path: 数据库路径，默认为 data/theme_control/theme_snapshots.db。
        """
        self.db_path = Path(db_path or self.DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """获取数据库连接上下文，启用 WAL 模式。

        Yields:
            sqlite3.Connection 连接对象，正常退出时提交，异常时回滚。
        """
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

    def _ensure_schema(self) -> None:
        """创建表与索引（幂等）。

        新 schema 拆分 content blob 与 observation：
        - theme_snapshot_blobs: content_hash 主键，存储 boards_json（不可变）
        - theme_snapshot_observations: 复合幂等键 (provider, source_as_of, content_hash)
        - theme_source_snapshots: 旧表保留只读兼容（不再写入）
        """
        with self._lock, self._connect() as conn:
            # 旧表保留用于向后兼容读取
            conn.execute(
                "CREATE TABLE IF NOT EXISTS theme_source_snapshots ("
                "snapshot_id TEXT PRIMARY KEY, "
                "provider TEXT, "
                "source_as_of TEXT, "
                "fetched_at TEXT, "
                "content_hash TEXT, "
                "policy_version TEXT, "
                "source_status TEXT, "
                "boards_json TEXT, "
                "degradation_reasons TEXT)"
            )
            # 新表：不可变 content blob（content_hash 去重）
            conn.execute(
                "CREATE TABLE IF NOT EXISTS theme_snapshot_blobs ("
                "content_hash TEXT PRIMARY KEY, "
                "boards_json TEXT NOT NULL DEFAULT '{}', "
                "content_fingerprint TEXT NOT NULL DEFAULT '')"
            )
            # 新表：每次抓取的可见性记录（observation）
            conn.execute(
                "CREATE TABLE IF NOT EXISTS theme_snapshot_observations ("
                "snapshot_id TEXT PRIMARY KEY, "
                "content_hash TEXT NOT NULL, "
                "provider TEXT NOT NULL DEFAULT '', "
                "source_as_of TEXT NOT NULL DEFAULT '', "
                "fetched_at TEXT NOT NULL DEFAULT '', "
                "policy_version TEXT NOT NULL DEFAULT '', "
                "source_status TEXT NOT NULL DEFAULT '', "
                "degradation_reasons TEXT NOT NULL DEFAULT '[]')"
            )
            # 复合幂等键唯一索引：(provider, source_as_of, content_hash)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_provider_asof_hash "
                "ON theme_snapshot_observations(provider, source_as_of, content_hash)"
            )
            # PIT 历史查询索引
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obs_source_as_of "
                "ON theme_snapshot_observations(source_as_of)"
            )
            # 旧表索引保留
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_provider_source_as_of "
                "ON theme_source_snapshots(provider, source_as_of)"
            )

    @staticmethod
    def _normalize_as_of(value: str) -> str:
        """将日期字符串归一化为 YYYYMMDD 格式，便于字符串比较。

        Args:
            value: 日期字符串（YYYYMMDD 或 ISO 格式）。

        Returns:
            8 位日期字符串，无效输入返回空串。
        """
        return str(value or "").replace("-", "").replace(":", "").replace("T", "")[:8]

    def save_snapshot(self, snapshot: SourceSnapshot, policy_version: str = "") -> bool:
        """保存快照，使用复合幂等键 (provider, source_as_of, content_hash)。

        幂等规则：
        - 相同 (provider, source_as_of, content_hash) 的快照只保存一次
        - 相同 content_hash 在不同 source_as_of 必须保留新的可见性记录
        - content blob 按 content_hash 去重存储（不可变）

        Args:
            snapshot: 要保存的主题快照。
            policy_version: 策略版本标识，用于追溯。

        Returns:
            True 表示新写入；False 表示因复合幂等键重复或无效而跳过。
        """
        if not snapshot.snapshot_id:
            return False
        content_hash = snapshot.content_hash or snapshot.snapshot_id.removeprefix("snap_")
        if not content_hash:
            return False
        provider = ",".join(snapshot.providers_used) if snapshot.providers_used else ""
        # 归一化为 YYYYMMDD，保证字符串比较语义正确
        source_as_of = self._normalize_as_of(snapshot.source_as_of or snapshot.trade_date)
        boards_json = json.dumps(
            [board.to_dict() for board in snapshot.boards], ensure_ascii=False
        )
        degradation_reasons = json.dumps(snapshot.degraded_reasons, ensure_ascii=False)
        source_status = "degraded" if snapshot.degraded_reasons else "ok"
        # content fingerprint 用于 blob 去重校验（与 content_hash 配合）
        content_fingerprint = hashlib.sha1(boards_json.encode("utf-8")).hexdigest()
        try:
            with self._lock, self._connect() as conn:
                # 1. 写入 content blob（content_hash 去重，已存在则跳过）
                conn.execute(
                    "INSERT OR IGNORE INTO theme_snapshot_blobs "
                    "(content_hash, boards_json, content_fingerprint) "
                    "VALUES (?, ?, ?)",
                    (content_hash, boards_json, content_fingerprint),
                )
                # 2. 检查复合幂等键：(provider, source_as_of, content_hash)
                #    相同 content + 不同 source_as_of 必须保留新可见性记录
                existing = conn.execute(
                    "SELECT 1 FROM theme_snapshot_observations "
                    "WHERE provider = ? AND source_as_of = ? AND content_hash = ?",
                    (provider, source_as_of, content_hash),
                ).fetchone()
                if existing:
                    return False
                # 3. 写入 observation 可见性记录
                conn.execute(
                    "INSERT OR IGNORE INTO theme_snapshot_observations "
                    "(snapshot_id, content_hash, provider, source_as_of, fetched_at, "
                    "policy_version, source_status, degradation_reasons) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot.snapshot_id,
                        content_hash,
                        provider,
                        source_as_of,
                        snapshot.fetched_at,
                        policy_version,
                        source_status,
                        degradation_reasons,
                    ),
                )
            return True
        except Exception as exc:
            logger.warning("保存主题快照失败: %s", exc)
            return False

    def get_historical_snapshot(
        self, requested_as_of: str, provider: str = ""
    ) -> SourceSnapshot | None:
        """选择 source_as_of <= requested_as_of 的最新版本。

        不从未来快照倒推历史：仅返回 source_as_of 不晚于请求日期的快照。
        优先从新表（observations JOIN blobs）查询；新表无数据时回退旧表。

        Args:
            requested_as_of: 请求的日期（YYYYMMDD 或 ISO 格式）。
            provider: 可选的数据源过滤，匹配 provider 字段子串。

        Returns:
            合法的历史快照；不存在时返回 None。
        """
        normalized = self._normalize_as_of(requested_as_of)
        if not normalized:
            return None
        try:
            with self._lock, self._connect() as conn:
                # 优先从新表查询：observations JOIN blobs
                if provider:
                    cursor = conn.execute(
                        "SELECT o.snapshot_id, o.provider, o.source_as_of, o.fetched_at, "
                        "o.content_hash, o.policy_version, o.source_status, "
                        "o.degradation_reasons, b.boards_json "
                        "FROM theme_snapshot_observations o "
                        "JOIN theme_snapshot_blobs b ON o.content_hash = b.content_hash "
                        "WHERE o.source_as_of <= ? AND o.provider LIKE ? "
                        "ORDER BY o.source_as_of DESC, o.fetched_at DESC LIMIT 1",
                        (normalized, f"%{provider}%"),
                    )
                else:
                    cursor = conn.execute(
                        "SELECT o.snapshot_id, o.provider, o.source_as_of, o.fetched_at, "
                        "o.content_hash, o.policy_version, o.source_status, "
                        "o.degradation_reasons, b.boards_json "
                        "FROM theme_snapshot_observations o "
                        "JOIN theme_snapshot_blobs b ON o.content_hash = b.content_hash "
                        "WHERE o.source_as_of <= ? "
                        "ORDER BY o.source_as_of DESC, o.fetched_at DESC LIMIT 1",
                        (normalized,),
                    )
                row = cursor.fetchone()
                if row is not None:
                    return self._row_to_snapshot(row)
                # 新表无数据，回退旧表（向后兼容）
                if provider:
                    cursor = conn.execute(
                        "SELECT * FROM theme_source_snapshots "
                        "WHERE source_as_of <= ? AND provider LIKE ? "
                        "ORDER BY source_as_of DESC, fetched_at DESC LIMIT 1",
                        (normalized, f"%{provider}%"),
                    )
                else:
                    cursor = conn.execute(
                        "SELECT * FROM theme_source_snapshots "
                        "WHERE source_as_of <= ? "
                        "ORDER BY source_as_of DESC, fetched_at DESC LIMIT 1",
                        (normalized,),
                    )
                row = cursor.fetchone()
                if row is None:
                    return None
                return self._row_to_snapshot(row)
        except Exception as exc:
            logger.warning("查询历史主题快照失败: %s", exc)
            return None

    def get_historical_membership_snapshot(
        self, requested_as_of: str, *, max_age_days: int = 7
    ) -> SourceSnapshot | None:
        """选择截止日前近期成员覆盖最完整的 PIT 快照。"""
        normalized = self._normalize_as_of(requested_as_of)
        if not normalized:
            return None
        try:
            requested = datetime.strptime(normalized, "%Y%m%d")
            earliest = (requested - timedelta(days=max(0, max_age_days))).strftime("%Y%m%d")
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT o.snapshot_id, o.provider, o.source_as_of, o.fetched_at, "
                    "o.content_hash, o.policy_version, o.source_status, "
                    "o.degradation_reasons, b.boards_json "
                    "FROM theme_snapshot_observations o "
                    "JOIN theme_snapshot_blobs b ON o.content_hash = b.content_hash "
                    "WHERE o.source_as_of BETWEEN ? AND ?",
                    (earliest, normalized),
                ).fetchall()
            snapshots = [self._row_to_snapshot(row) for row in rows]
            if not snapshots:
                return self.get_historical_snapshot(normalized)
            return max(
                snapshots,
                key=lambda snapshot: (
                    len({member for board in snapshot.boards for member in board.members}),
                    len(snapshot.boards),
                    snapshot.source_as_of,
                    snapshot.fetched_at,
                ),
            )
        except Exception as exc:
            logger.warning("查询历史主题成员快照失败: %s", exc)
            return self.get_historical_snapshot(normalized)

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> SourceSnapshot:
        """将数据库行转换为 SourceSnapshot 对象。

        支持新表（observations JOIN blobs）和旧表（theme_source_snapshots）两种 schema。

        Args:
            row: sqlite3.Row 查询结果行。

        Returns:
            反序列化后的 SourceSnapshot。
        """
        boards_data = json.loads(row["boards_json"] or "[]")
        boards = [
            ConceptBoard(**item) for item in boards_data if isinstance(item, dict)
        ]
        try:
            degraded_reasons = json.loads(row["degradation_reasons"] or "[]")
        except Exception:
            degraded_reasons = []
        return SourceSnapshot(
            snapshot_id=row["snapshot_id"],
            trade_date=row["source_as_of"],
            fetched_at=row["fetched_at"],
            providers_used=[
                part for part in str(row["provider"] or "").split(",") if part
            ],
            boards=boards,
            is_trading_hours=False,
            source_as_of=row["source_as_of"],
            degraded_reasons=list(degraded_reasons),
            content_hash=row["content_hash"],
        )

    def migrate_from_last_good(self, last_good_path: Path) -> int:
        """从旧 last_good.json 懒迁移到版本化存储。

        重复执行幂等：复合幂等键 (provider, source_as_of, content_hash) 相同的快照不会重复写入。

        Args:
            last_good_path: 旧 last_good.json 文件路径。

        Returns:
            成功迁移的快照数量（0 或 1）。
        """
        if not last_good_path.exists():
            return 0
        try:
            payload = json.loads(last_good_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取 last_good.json 失败: %s", exc)
            return 0
        boards = [
            ConceptBoard(**item)
            for item in payload.get("boards", [])
            if isinstance(item, dict)
        ]
        snapshot = SourceSnapshot(
            snapshot_id=str(payload.get("snapshot_id") or ""),
            trade_date=str(payload.get("trade_date") or ""),
            fetched_at=str(payload.get("fetched_at") or ""),
            providers_used=list(payload.get("providers_used") or []),
            boards=boards,
            is_trading_hours=bool(payload.get("is_trading_hours")),
            source_as_of=str(payload.get("source_as_of") or payload.get("trade_date") or ""),
            degraded_reasons=list(payload.get("degraded_reasons") or []),
            content_hash=str(payload.get("content_hash") or ""),
        )
        if not snapshot.snapshot_id:
            return 0
        if self.save_snapshot(snapshot, policy_version="migrated_from_last_good"):
            return 1
        return 0


class ThemeSourceRouter:
    """主题数据源统一路由器。

    降级链：东财 → 同花顺 → 新闻聚类 → last-good snapshot

    配置：
    - 单次超时：3-5 秒
    - 重试：最多 2 次，指数退避
    - 连续失败 3 次：熔断 10 分钟
    - 盘中有效快照 TTL：10-20 分钟
    - 收盘快照：冻结为当日版本
    """

    DEFAULT_TIMEOUT = 4.0  # 秒
    # A scheduled scan will retry on the next cycle.  Repeating a slow remote
    # scrape inside one request made the endpoint block for minutes.
    MAX_RETRIES = 0
    CIRCUIT_BREAK_THRESHOLD = 3
    CIRCUIT_BREAK_DURATION = 600  # 10 分钟
    INTRADAY_TTL = 900  # 15 分钟（10-20 分钟中间值）
    MAX_MEMBER_BOARDS = 10
    MEMBER_FETCH_BUDGET_SECONDS = 12.0
    MEMBER_FETCH_WORKERS = 4
    MIN_LAST_GOOD_MEMBER_BOARDS = 5
    MIN_LAST_GOOD_UNIQUE_MEMBERS = 20
    MIN_LAST_GOOD_RELATIVE_COVERAGE = 0.30

    def __init__(
        self,
        akshare_provider: Any = None,
        cache_dir: str = "data/theme_source_cache",
        persist_last_good: bool | None = None,
        enable_slow_scrapers: bool | None = None,
        snapshot_store: ThemeSnapshotStore | None = None,
    ) -> None:
        """初始化。

        Args:
            akshare_provider: AkshareProvider 实例（延迟创建，测试可注入 mock）。
            cache_dir: 快照缓存目录。
            persist_last_good: 是否持久化 last-good 快照，默认按 akshare_provider 推断。
            enable_slow_scrapers: 是否启用慢速抓取，默认按 akshare_provider 推断。
            snapshot_store: 版本化快照存储实例，None 时延迟创建并触发懒迁移。
        """
        self._akshare_provider = akshare_provider
        self._akshare_provider_injected = akshare_provider is not None
        self._cache_dir = cache_dir
        self._cache_path = Path(cache_dir)
        self._persist_last_good = (
            akshare_provider is None if persist_last_good is None else bool(persist_last_good)
        )
        self._enable_slow_scrapers = (
            akshare_provider is not None
            if enable_slow_scrapers is None
            else bool(enable_slow_scrapers)
        )

        # 熔断器状态：provider -> {failures: int, circuit_open_until: float}
        self._circuit_breakers: dict[str, dict[str, Any]] = {
            "eastmoney": {"failures": 0, "circuit_open_until": 0.0},
            "ths": {"failures": 0, "circuit_open_until": 0.0},
        }

        # 快照缓存：cache_key -> (snapshot, expiry_time)
        self._snapshot_cache: dict[str, tuple[SourceSnapshot, float]] = {}
        self._dynamic_member_cache: dict[str, tuple[ConceptBoard, float]] = {}
        self._semantic_registry: Any = None

        # 版本化快照存储（懒加载），首次访问时触发 last_good.json 懒迁移
        self._snapshot_store: ThemeSnapshotStore | None = snapshot_store
        self._snapshot_store_initialized = snapshot_store is not None

        # last-good snapshot
        self._last_good_snapshot: SourceSnapshot | None = self._load_last_good()

    def _get_snapshot_store(self) -> ThemeSnapshotStore | None:
        """延迟获取 ThemeSnapshotStore，首次访问时触发 last_good.json 懒迁移。

        Returns:
            ThemeSnapshotStore 实例；初始化失败时返回 None。
        """
        if self._snapshot_store_initialized:
            return self._snapshot_store
        self._snapshot_store_initialized = True
        try:
            store = ThemeSnapshotStore()
            # 懒迁移：将旧 last_good.json 导入版本化存储（幂等）
            last_good_path = self._cache_path / "last_good.json"
            if last_good_path.exists():
                migrated = store.migrate_from_last_good(last_good_path)
                if migrated:
                    logger.info("last_good.json 懒迁移到版本化快照存储: %d 条", migrated)
            self._snapshot_store = store
            return store
        except Exception as exc:
            logger.warning("ThemeSnapshotStore 初始化失败: %s", exc)
            self._snapshot_store = None
            return None

    @staticmethod
    def _records(value: Any, *, dict_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Normalize provider DataFrame/list/dict responses into records."""
        if value is None:
            return []
        if hasattr(value, "to_dict") and hasattr(value, "columns"):
            try:
                return list(value.to_dict(orient="records"))
            except Exception:
                return []
        if isinstance(value, dict):
            rows: list[dict[str, Any]] = []
            for key in dict_keys:
                candidate = value.get(key)
                if isinstance(candidate, list):
                    rows.extend(item for item in candidate if isinstance(item, dict))
            return rows or ([value] if any(not isinstance(v, (list, dict)) for v in value.values()) else [])
        if isinstance(value, (list, tuple)):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _first(record: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = record.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _load_last_good(self) -> SourceSnapshot | None:
        path = self._cache_path / "last_good.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            boards = [ConceptBoard(**item) for item in payload.get("boards", []) if isinstance(item, dict)]
            snapshot = SourceSnapshot(
                snapshot_id=str(payload.get("snapshot_id") or ""),
                trade_date=str(payload.get("trade_date") or ""),
                fetched_at=str(payload.get("fetched_at") or ""),
                providers_used=list(payload.get("providers_used") or []),
                boards=boards,
                is_trading_hours=bool(payload.get("is_trading_hours")),
                source_as_of=str(payload.get("source_as_of") or payload.get("trade_date") or ""),
                degraded_reasons=list(payload.get("degraded_reasons") or []),
                content_hash=str(payload.get("content_hash") or ""),
            )
            if not self._is_last_good_quality(snapshot):
                raise ValueError("last-good 快照未通过成员覆盖质量门")
            return snapshot
        except Exception as exc:
            logger.warning("忽略无效主题 last-good 快照: %s", exc)

        # Bootstrap only as a degraded fallback from the latest locally
        # ingested concept membership.  It never outranks a live source and
        # its original trade_date remains visible to downstream PIT checks.
        try:
            import pandas as pd

            members_path = Path("data/industry/concept_board_members.parquet")
            if not members_path.exists():
                return None
            frame = pd.read_parquet(members_path)
            if frame.empty:
                return None
            trade_date = str(frame["trade_date"].max())[:10] if "trade_date" in frame.columns else ""
            if "trade_date" in frame.columns:
                frame = frame[frame["trade_date"].astype(str).str[:10] == trade_date]
            boards: list[ConceptBoard] = []
            for (board_code, board_name), rows in frame.groupby(["board_code", "board_name"], dropna=False):
                symbols = sorted({str(value) for value in rows.get("symbol", []) if str(value).strip()})
                if not str(board_name).strip() or not symbols:
                    continue
                provider_values = sorted({str(value) for value in rows.get("provider", []) if str(value).strip()})
                boards.append(ConceptBoard(
                    board_name=str(board_name),
                    board_id=str(board_code),
                    provider="local_last_good:" + ",".join(provider_values[:2]),
                    members=symbols,
                    raw_response_hash=hashlib.sha1("|".join(symbols).encode("utf-8")).hexdigest()[:8],
                    fetched_at=trade_date,
                    returned_count=len(symbols),
                ))
            if not boards:
                return None
            snapshot = SourceSnapshot(
                snapshot_id="snap_local_" + hashlib.sha1(
                    f"{trade_date}|{len(boards)}".encode("utf-8")
                ).hexdigest()[:10],
                trade_date=trade_date,
                fetched_at=trade_date,
                providers_used=["local_last_good"],
                boards=boards,
                is_trading_hours=False,
                source_as_of=trade_date,
                degraded_reasons=["bootstrap_from_local_concept_membership"],
                content_hash=hashlib.sha1(
                    "|".join(f"{board.board_name}:{','.join(board.members)}" for board in boards).encode("utf-8")
                ).hexdigest(),
            )
            if not self._is_last_good_quality(snapshot):
                logger.warning("本地概念成员不足，不能作为 last-good")
                return None
            # Replace a corrupt/partial runtime cache with the verified local
            # snapshot.  The original source_as_of remains unchanged.
            self._save_last_good(snapshot)
            return snapshot
        except Exception as exc:
            logger.warning("加载本地概念 last-good 失败: %s", exc)
            return None

    @classmethod
    def _last_good_stats(cls, snapshot: SourceSnapshot | None) -> tuple[int, int]:
        if snapshot is None:
            return 0, 0
        member_boards = [board for board in snapshot.boards if board.members]
        unique_members = {
            symbol for board in member_boards for symbol in board.members if symbol
        }
        return len(member_boards), len(unique_members)

    @classmethod
    def _is_last_good_quality(
        cls,
        snapshot: SourceSnapshot | None,
        previous: SourceSnapshot | None = None,
    ) -> bool:
        """Return whether a snapshot is safe to become transaction fallback."""
        board_count, member_count = cls._last_good_stats(snapshot)
        if (
            board_count < cls.MIN_LAST_GOOD_MEMBER_BOARDS
            or member_count < cls.MIN_LAST_GOOD_UNIQUE_MEMBERS
        ):
            return False
        _, previous_member_count = cls._last_good_stats(previous)
        if (
            previous_member_count >= 50
            and member_count
            < previous_member_count * cls.MIN_LAST_GOOD_RELATIVE_COVERAGE
        ):
            return False
        return True

    def _save_last_good(
        self,
        snapshot: SourceSnapshot,
        *,
        previous: SourceSnapshot | None = None,
    ) -> bool:
        if not self._persist_last_good:
            return False
        board_count, member_count = self._last_good_stats(snapshot)
        if not self._is_last_good_quality(snapshot, previous):
            logger.warning(
                "主题成员快照未通过质量门，不覆盖 last-good: boards=%d members=%d",
                board_count, member_count,
            )
            return False
        try:
            self._cache_path.mkdir(parents=True, exist_ok=True)
            target = self._cache_path / "last_good.json"
            temp = self._cache_path / "last_good.json.tmp"
            temp.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False), encoding="utf-8")
            temp.replace(target)
            return True
        except Exception as exc:
            logger.warning("保存主题 last-good 快照失败: %s", exc)
            return False

    def _get_akshare_provider(self) -> Any:
        """延迟获取 AkshareProvider。"""
        if self._akshare_provider is None:
            try:
                from infrastructure.data_providers.market.akshare_provider import AkshareProvider
                self._akshare_provider = AkshareProvider()
            except Exception as exc:
                logger.warning("AkshareProvider 不可用: %s", exc)
                return None
        return self._akshare_provider

    def _is_circuit_open(self, provider: str) -> bool:
        """检查熔断器是否打开。"""
        state = self._circuit_breakers.get(provider)
        if state is None:
            return False
        if state["circuit_open_until"] > time.time():
            return True
        return False

    def _record_failure(self, provider: str) -> None:
        """记录失败，触发熔断。"""
        state = self._circuit_breakers.setdefault(provider, {"failures": 0, "circuit_open_until": 0.0})
        state["failures"] += 1
        if state["failures"] >= self.CIRCUIT_BREAK_THRESHOLD:
            self._open_circuit(provider)

    def _open_circuit(self, provider: str) -> None:
        state = self._circuit_breakers.setdefault(
            provider, {"failures": 0, "circuit_open_until": 0.0}
        )
        state["circuit_open_until"] = time.time() + self.CIRCUIT_BREAK_DURATION
        logger.warning("数据源 %s 熔断，持续 %d 秒", provider, self.CIRCUIT_BREAK_DURATION)

    def _record_success(self, provider: str) -> None:
        """记录成功，重置熔断器。"""
        state = self._circuit_breakers.get(provider)
        if state:
            state["failures"] = 0
            state["circuit_open_until"] = 0.0

    def _fetch_with_retry(self, fetch_func: Any, provider: str) -> Any:
        """带重试的抓取。"""
        last_exc = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                result = fetch_func()
                self._record_success(provider)
                return result
            except Exception as exc:
                last_exc = exc
                if attempt < self.MAX_RETRIES:
                    wait = (2 ** attempt) * 0.5  # 0.5s, 1s
                    time.sleep(wait)
        self._record_failure(provider)
        if provider == "eastmoney" and not self._is_circuit_open(provider):
            # push2 disconnects are IP/frequency blocks in production. Open
            # immediately after the bounded probe; scheduled retries only
            # extend that block and the next cycle already has live fallbacks.
            self._open_circuit(provider)
        raise last_exc

    def _fetch_eastmoney_concepts(self, max_boards: int = 30) -> list[ConceptBoard]:
        """从东财获取概念板块。"""
        provider = self._get_akshare_provider()
        if provider is None:
            raise RuntimeError("AkshareProvider 不可用")

        def _fetch():
            boards_data = self._records(provider.get_concept_boards())
            if not boards_data:
                raise RuntimeError("东财概念返回空结果")
            board_specs: list[tuple[dict[str, Any], str, str]] = []
            for board_info in boards_data[: min(max_boards, self.MAX_MEMBER_BOARDS)]:
                board_name = self._first(board_info, "board_name", "name", "板块名称", "概念名称")
                board_id = self._first(board_info, "board_code", "board_id", "code", "板块代码")
                if board_name:
                    board_specs.append((board_info, board_name, board_id))

            executor = ThreadPoolExecutor(max_workers=self.MEMBER_FETCH_WORKERS)
            future_map = {
                executor.submit(provider.get_concept_board_members, board_name): (board_info, board_name, board_id)
                for board_info, board_name, board_id in board_specs
            }
            done, pending = wait(future_map, timeout=self.MEMBER_FETCH_BUDGET_SECONDS)
            for future in pending:
                future.cancel()
            result = []
            for future in done:
                board_info, board_name, board_id = future_map[future]
                try:
                    members = self._records(future.result())
                except Exception:
                    members = []
                symbols = [self._first(m, "symbol", "code", "股票代码", "代码") for m in members]
                symbols = [symbol for symbol in symbols if symbol]
                raw_hash = hashlib.sha1(str(board_info).encode("utf-8")).hexdigest()[:8]
                result.append(ConceptBoard(
                    board_name=board_name,
                    board_id=board_id,
                    provider="eastmoney",
                    members=symbols,
                    raw_response_hash=raw_hash,
                    fetched_at=datetime.now(MARKET_TZ).isoformat(),
                    returned_count=len(symbols),
                ))
            executor.shutdown(wait=False, cancel_futures=True)
            if not result:
                raise RuntimeError("东财概念成分在总预算内无结果")
            return result

        return self._fetch_with_retry(_fetch, "eastmoney")

    def _fetch_efinance_concepts(self, max_boards: int = 30) -> list[ConceptBoard]:
        """Fetch a fast EastMoney-compatible concept ranking via efinance."""
        try:
            from infrastructure.data_providers.market.efinance_provider import EfinanceProvider

            frame = EfinanceProvider().get_concept_boards()
            records = self._records(frame)
            if not records:
                raise RuntimeError("Efinance 概念返回空结果")

            def number(row: dict[str, Any], key: str) -> float:
                try:
                    value = float(row.get(key))
                    return value if value == value else 0.0
                except Exception:
                    return 0.0

            # Keep a broad discovery sample.  The provider layer must not turn
            # the strategy into a top-gainers/bottom-losers selector.
            moderate = sorted(
                [row for row in records if 0.3 <= number(row, "change_pct") <= 3.0],
                key=lambda row: number(row, "amount"), reverse=True,
            )[:8]
            neutral = sorted(
                [row for row in records if abs(number(row, "change_pct")) < 0.8],
                key=lambda row: number(row, "amount"), reverse=True,
            )[:6]
            positive = sorted(records, key=lambda row: number(row, "change_pct"), reverse=True)[:5]
            negative = sorted(records, key=lambda row: number(row, "change_pct"))[:5]
            liquid = sorted(records, key=lambda row: number(row, "amount"), reverse=True)[:6]
            bucket_rows: dict[str, list[dict[str, Any]]] = {
                "moderate": moderate,
                "neutral": neutral,
                "positive": positive,
                "negative": negative,
                "liquid": liquid,
            }
            selected: list[tuple[dict[str, Any], str]] = []
            seen: set[str] = set()
            bucket_order = ("moderate", "neutral", "positive", "negative", "liquid")
            # First reserve one distinct board for each hypothesis bucket.
            # The same highly-liquid board can appear in several ranked lists,
            # so simply interleaving position zero still loses a bucket.
            for bucket in bucket_order:
                for row in bucket_rows[bucket]:
                    name = self._first(row, "board_name", "name", "板块名称")
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    selected.append((row, bucket))
                    break
                if len(selected) >= max_boards:
                    break
            # Fill remaining capacity round-robin without changing the bucket
            # assigned during discovery.
            for index in range(max((len(rows) for rows in bucket_rows.values()), default=0)):
                if len(selected) >= max_boards:
                    break
                for bucket in bucket_order:
                    rows = bucket_rows[bucket]
                    if index >= len(rows):
                        continue
                    row = rows[index]
                    name = self._first(row, "board_name", "name", "板块名称")
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    selected.append((row, bucket))
                    if len(selected) >= max_boards:
                        break
                if len(selected) >= max_boards:
                    break
            # A broad 14:50 scan asks for far more than the default discovery
            # buckets. Fill the remaining capacity from the complete provider
            # ranking so themes such as innovative drugs cannot disappear just
            # because they were outside a fixed 30-board sample.
            if len(selected) < max_boards:
                remaining = sorted(
                    records,
                    key=lambda row: (
                        number(row, "amount"), abs(number(row, "change_pct"))
                    ),
                    reverse=True,
                )
                for row in remaining:
                    name = self._first(row, "board_name", "name", "板块名称")
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    selected.append((row, "broad_market"))
                    if len(selected) >= max_boards:
                        break
            now = datetime.now(MARKET_TZ).isoformat()
            return [
                ConceptBoard(
                    board_name=self._first(row, "board_name", "name", "板块名称"),
                    board_id=self._first(row, "board_code", "code", "板块代码"),
                    provider="efinance",
                    members=[],
                    raw_response_hash=hashlib.sha1(str(row).encode("utf-8")).hexdigest()[:8],
                    fetched_at=now,
                    change_pct=number(row, "change_pct"),
                    amount=number(row, "amount"),
                    discovery_bucket=bucket,
                )
                for row, bucket in selected
            ]
        except Exception as exc:
            raise RuntimeError(f"Efinance 概念失败: {exc}") from exc

    def _fetch_ths_concepts(self, max_boards: int = 30) -> list[ConceptBoard]:
        """从同花顺获取概念板块。"""
        provider = self._get_akshare_provider()
        if provider is None:
            raise RuntimeError("AkshareProvider 不可用")

        def _fetch():
            # akshare_provider.get_hot_themes() 返回同花顺热点
            themes = self._records(provider.get_hot_themes(), dict_keys=("concept", "industry"))
            if not themes:
                raise RuntimeError("同花顺主题返回空结果")
            result = []
            for theme in themes[:max_boards]:
                board_name = self._first(
                    theme, "name", "board_name", "板块名称", "概念名称", "行业名称", "板块"
                )
                board_id = self._first(theme, "code", "board_id", "板块代码")
                if not board_name:
                    continue
                # AkshareProvider has no THS constituent endpoint.  Calling
                # get_concept_board_members here actually queries EastMoney
                # and would create false independent-source lineage.
                members: list[str] = []
                raw_hash = hashlib.sha1(str(theme).encode("utf-8")).hexdigest()[:8]
                result.append(ConceptBoard(
                    board_name=board_name,
                    board_id=board_id,
                    provider="ths",
                    members=members,
                    raw_response_hash=raw_hash,
                    fetched_at=datetime.now(MARKET_TZ).isoformat(),
                ))
            return result

        return self._fetch_with_retry(_fetch, "ths")

    def _resolve_market_board_members(
        self,
        boards: list[ConceptBoard],
        *,
        max_member_boards: int | None = None,
        member_count: int = 30,
    ) -> list[ConceptBoard]:
        """Resolve a bounded strong/weak/liquid board sample via Wencai."""
        if not boards:
            return []
        grouped: dict[str, list[ConceptBoard]] = {}
        unbucketed: list[ConceptBoard] = []
        for board in boards:
            if board.discovery_bucket:
                grouped.setdefault(board.discovery_bucket, []).append(board)
            else:
                unbucketed.append(board)
        selected: list[ConceptBoard] = []
        bucket_order = ("moderate", "neutral", "positive", "negative", "liquid")
        for index in range(2):
            for bucket in bucket_order:
                rows = grouped.get(bucket, [])
                if index < len(rows):
                    selected.append(rows[index])
        board_limit = max(1, int(max_member_boards or self.MAX_MEMBER_BOARDS))
        if len(selected) < board_limit:
            remainder = [
                board for board in boards
                if board not in selected
            ]
            remainder.sort(
                key=lambda board: (float(board.amount or 0.0), abs(float(board.change_pct or 0.0))),
                reverse=True,
            )
            selected.extend(remainder[: board_limit - len(selected)])
        selected = selected[:board_limit]
        deduped: list[ConceptBoard] = []
        seen: set[str] = set()
        for board in selected:
            key = self._normalized_theme(board.board_name)
            if key and key not in seen:
                seen.add(key)
                deduped.append(board)
        executor = ThreadPoolExecutor(max_workers=min(8, len(deduped) or 1))
        futures = {
            executor.submit(
                self.resolve_dynamic_theme_members,
                board.board_name,
                count=member_count,
            ): board
            for board in deduped
        }
        done, pending = wait(futures, timeout=10.0)
        for future in pending:
            future.cancel()
        resolved: list[ConceptBoard] = []
        for future in done:
            try:
                board = future.result()
                source_board = futures[future]
                if board is not None and board.members:
                    resolved.append(replace(
                        board,
                        change_pct=source_board.change_pct,
                        amount=source_board.amount,
                        discovery_bucket=source_board.discovery_bucket,
                    ))
            except Exception:
                continue
        executor.shutdown(wait=False, cancel_futures=True)
        return resolved

    def _is_trading_hours(self) -> bool:
        """判断当前是否盘中。"""
        now = datetime.now(MARKET_TZ)
        # 周末
        if now.weekday() >= 5:
            return False
        hour = now.hour
        minute = now.minute
        # 9:30-11:30, 13:00-15:00
        if (hour == 9 and minute >= 30) or (hour == 10) or (hour == 11 and minute <= 30):
            return True
        if hour == 13 or hour == 14 or (hour == 15 and minute == 0):
            return True
        return False

    def _generate_snapshot_id(
        self, providers: list[str], trade_date: str, boards: list[ConceptBoard] | None = None
    ) -> str:
        """Generate a deterministic ID from source content."""
        board_fingerprint = "|".join(
            sorted(
                f"{board.provider}:{board.board_id}:{board.board_name}:{board.raw_response_hash}:{','.join(board.members)}"
                for board in (boards or [])
            )
        )
        raw = f"{','.join(sorted(providers))}|{trade_date}|{board_fingerprint}"
        return "snap_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _get_cache_key(self, provider: str, provider_theme_id: str, trade_date: str, fetched_at: str) -> str:
        """缓存键 = provider + provider_theme_id + trade_date + fetched_at。"""
        return f"{provider}|{provider_theme_id}|{trade_date}|{fetched_at}"

    def _merge_boards_by_name(
        self, boards_by_source: list[tuple[str, list[ConceptBoard]]]
    ) -> list[ConceptBoard]:
        """按规范化 board_name 合并多源板块。

        同一规范化名称的板块合并成员，provider 字段记录所有来源（逗号分隔），
        保留每个来源的原始观察，不丢弃任何成员数据。

        Args:
            boards_by_source: [(provider_name, boards), ...] 多源板块列表，
                provider_name 为该批 boards 的来源标识。

        Returns:
            合并去重后的板块列表，按首次出现顺序排列。
        """
        grouped: dict[str, list[tuple[str, ConceptBoard]]] = {}
        order: list[str] = []
        for provider, boards in boards_by_source:
            for board in boards:
                key = self._normalized_theme(board.board_name)
                if not key:
                    continue
                if key not in grouped:
                    grouped[key] = []
                    order.append(key)
                grouped[key].append((provider, board))

        merged: list[ConceptBoard] = []
        for key in order:
            group = grouped[key]
            base_board: ConceptBoard | None = None
            members: list[str] = []
            seen_members: set[str] = set()
            providers: list[str] = []
            seen_providers: set[str] = set()
            for provider, board in group:
                # 优先选择带 discovery_bucket 的 board 作为基准，保留 efinance 的分桶元数据
                if base_board is None:
                    base_board = board
                elif board.discovery_bucket and not base_board.discovery_bucket:
                    base_board = board
                for symbol in board.members:
                    if symbol and symbol not in seen_members:
                        seen_members.add(symbol)
                        members.append(symbol)
                if provider not in seen_providers:
                    seen_providers.add(provider)
                    providers.append(provider)
            if base_board is None:
                continue
            merged.append(replace(
                base_board,
                members=members,
                provider=",".join(providers),
                returned_count=len(members),
            ))
        return merged

    def _fetch_all_sources_parallel(
        self, max_boards: int
    ) -> tuple[list[ConceptBoard], list[str], list[str]]:
        """多源有界并行采集与合并。

        使用有界线程池并行调用 EastMoney / THS 两个独立失败域，
        每个来源独立超时（DEFAULT_TIMEOUT 秒）和独立异常处理，一个失败不影响其他。
        efinance 与 AKShare 的概念接口都访问 EastMoney push2，因此只探测一次。
        合并规则：按规范化 board_name 去重，同名板块合并成员，provider 字段记录多源。

        Args:
            max_boards: 每个数据源的最大板块数。

        Returns:
            (合并后的 boards, providers_used, degraded_reasons)。
            providers_used 仅包含成功返回数据的来源；degraded_reasons 记录失败原因。
        """
        providers_used: list[str] = []
        degraded_reasons: list[str] = []
        boards_by_source: list[tuple[str, list[ConceptBoard]]] = []
        tasks: list[tuple[str, Any]] = []
        if self._is_circuit_open("eastmoney"):
            degraded_reasons.append("eastmoney_circuit_open")
        elif self._akshare_provider_injected:
            # Explicit provider injection is the diagnostic/test mode: retain
            # both adapters so callers can compare their normalized results.
            tasks.extend((
                ("efinance", lambda: self._fetch_efinance_concepts(max_boards)),
                (
                    "eastmoney",
                    lambda: self._fetch_with_retry(
                        lambda: self._fetch_eastmoney_concepts(max_boards),
                        "eastmoney",
                    ),
                ),
            ))
        else:
            tasks.append((
                "eastmoney",
                lambda: self._fetch_with_retry(
                    lambda: self._fetch_efinance_concepts(max_boards),
                    "eastmoney",
                ),
            ))
        if self._enable_slow_scrapers:
            if self._is_circuit_open("ths"):
                degraded_reasons.append("ths_circuit_open")
            elif self._get_akshare_provider() is not None:
                # Importing AKShare is slow on a cold process. Warm it before
                # starting the bounded network timer so a healthy THS call is
                # not misclassified as a source timeout.
                tasks.append(("ths", lambda: self._fetch_ths_concepts(max_boards)))
            else:
                degraded_reasons.append("ths_failed: provider unavailable")

        executor = ThreadPoolExecutor(max_workers=max(1, len(tasks)))
        future_map = {
            executor.submit(fetcher): name for name, fetcher in tasks
        }
        # 每个来源独立超时（DEFAULT_TIMEOUT 秒）
        done, pending = wait(future_map, timeout=self.DEFAULT_TIMEOUT)
        for future in pending:
            future.cancel()
        for future in done:
            name = future_map[future]
            try:
                boards = future.result()
                if boards:
                    boards_by_source.append((name, boards))
                    if name not in providers_used:
                        providers_used.append(name)
                else:
                    degraded_reasons.append(f"{name}_failed: empty result")
            except Exception as exc:
                logger.warning("%s 概念失败: %s", name, exc)
                degraded_reasons.append(f"{name}_failed: {exc}")
        for future in pending:
            name = future_map[future]
            degraded_reasons.append(
                f"{name}_failed: timeout after {self.DEFAULT_TIMEOUT}s"
            )
            if name == "eastmoney":
                self._record_failure(name)
                if not self._is_circuit_open(name):
                    self._open_circuit(name)
        executor.shutdown(wait=False, cancel_futures=True)

        merged_boards = self._merge_boards_by_name(boards_by_source)
        return merged_boards, providers_used, degraded_reasons

    def fetch_concept_snapshot(self, trade_date: str = "", max_boards: int = 30) -> SourceSnapshot:
        """获取概念板块快照，按降级链执行。

        降级链：东财 → 同花顺 → last-good snapshot
        不能返回空板块，也不能临时改用 stock_basic.industry。

        Args:
            trade_date: 交易日期（YYYYMMDD），空则用今天。
            max_boards: 最大板块数。

        Returns:
            SourceSnapshot 含 boards 列表。
        """
        if not trade_date:
            trade_date = datetime.now(MARKET_TZ).strftime("%Y%m%d")

        normalized_request_date = str(trade_date).replace("-", "")[:8]
        today = datetime.now(MARKET_TZ).strftime("%Y%m%d")
        if (
            self._akshare_provider is None
            and normalized_request_date
            and normalized_request_date < today
        ):
            # 1. 优先从版本化快照存储查询合法历史版本（source_as_of <= requested）
            store = self._get_snapshot_store()
            if store is not None:
                historical = store.get_historical_membership_snapshot(
                    normalized_request_date
                )
                if historical is not None:
                    return SourceSnapshot(
                        snapshot_id=historical.snapshot_id,
                        trade_date=str(trade_date),
                        fetched_at=historical.fetched_at,
                        providers_used=[*historical.providers_used, "historical_snapshot"],
                        boards=historical.boards,
                        is_trading_hours=False,
                        source_as_of=historical.source_as_of or historical.trade_date,
                        degraded_reasons=[
                            *historical.degraded_reasons,
                            "historical_live_fetch_forbidden",
                            "historical_snapshot",
                        ],
                        content_hash=historical.content_hash,
                    )

            # 2. 版本化存储未命中，回退到 last_good.json 兼容读取
            #    不从未来快照倒推历史：last_good.source_as_of > requested 时拒绝
            if self._last_good_snapshot is not None:
                snapshot = self._last_good_snapshot
                snapshot_date = str(snapshot.source_as_of or snapshot.trade_date).replace("-", "")[:8]
                if snapshot_date and snapshot_date <= normalized_request_date:
                    return SourceSnapshot(
                        snapshot_id=snapshot.snapshot_id,
                        trade_date=str(trade_date),
                        fetched_at=snapshot.fetched_at,
                        providers_used=[*snapshot.providers_used, "historical_last_good"],
                        boards=snapshot.boards,
                        is_trading_hours=False,
                        source_as_of=snapshot.source_as_of or snapshot.trade_date,
                        degraded_reasons=[
                            *snapshot.degraded_reasons,
                            "historical_live_fetch_forbidden",
                        ],
                        content_hash=snapshot.content_hash,
                    )

            # 3. 既无版本化快照，last_good 也是未来或不存在，返回空
            #    不从未来快照倒推历史
            return SourceSnapshot(
                trade_date=str(trade_date),
                source_as_of="",
                degraded_reasons=[
                    "historical_live_fetch_forbidden",
                    "no_point_in_time_snapshot",
                ],
            )

        is_trading = self._is_trading_hours()

        # 检查缓存
        cache_key = f"snapshot|{trade_date}|{is_trading}|{max_boards}"
        if cache_key in self._snapshot_cache:
            snapshot, expiry = self._snapshot_cache[cache_key]
            if time.time() < expiry:
                logger.info("使用缓存快照 %s", snapshot.snapshot_id)
                return snapshot

        providers_used = []
        boards = []
        degraded_reasons = []
        source_as_of = trade_date

        # 1. 多源有界并行采集与合并（efinance / eastmoney / ths）
        #    每个来源独立超时和异常处理，一个失败不阻断其他源参与合并。
        merged_boards, parallel_providers, parallel_reasons = (
            self._fetch_all_sources_parallel(max_boards)
        )
        boards.extend(merged_boards)
        providers_used.extend(parallel_providers)
        degraded_reasons.extend(parallel_reasons)

        # 2. Wencai 补充实时主题的成员。Efinance 有分桶时按分桶抽样；
        #    THS 只有真实主题名时也可作为检索问题，但不伪造 THS 成分来源。
        memberless_boards = [board for board in merged_boards if not board.members]
        if memberless_boards:
            semantic_boards = self._resolve_market_board_members(
                memberless_boards,
                max_member_boards=min(max_boards, 50),
                member_count=200 if max_boards > 30 else 30,
            )
            if semantic_boards:
                boards.extend(semantic_boards)
                providers_used.append("wencai")

        # 3. 并行采集全部失败，使用 last-good snapshot
        if not boards:
            if self._last_good_snapshot is not None:
                refreshed = self._resolve_market_board_members(
                    self._last_good_snapshot.boards,
                    max_member_boards=max(20, self.MAX_MEMBER_BOARDS),
                    member_count=30,
                )
                refreshed_snapshot = SourceSnapshot(boards=refreshed)
                if self._is_last_good_quality(refreshed_snapshot):
                    logger.warning("概念排行源失败，使用问财重新验证本地主题成员")
                    boards = refreshed
                    providers_used = ["wencai"]
                    source_as_of = trade_date
                    degraded_reasons.append(
                        "live_ranking_unavailable_wencai_membership_refresh"
                    )
                else:
                    logger.warning("所有概念源失败，使用 last-good snapshot")
                    boards = self._last_good_snapshot.boards
                    providers_used = self._last_good_snapshot.providers_used + ["last_good"]
                    source_as_of = self._last_good_snapshot.source_as_of or self._last_good_snapshot.trade_date
                    degraded_reasons.append("using_last_good_snapshot")
            else:
                # 没有 last-good snapshot，返回空快照（不抛异常，让上层降级处理）
                logger.error("所有概念源失败且无 last-good snapshot")
                degraded_reasons.append("all_sources_failed_no_last_good")

        # THS supplies a timely theme-name ranking but this provider adapter
        # has no genuine THS constituent endpoint.  Reuse matching local
        # membership only as explicitly degraded lineage instead of claiming
        # it as a second live source.
        if boards and not any(board.members for board in boards) and self._last_good_snapshot is not None:
            names = {board.board_name.strip().lower() for board in boards if board.board_name}
            fallback_boards = [
                board for board in self._last_good_snapshot.boards
                if board.board_name.strip().lower() in names and board.members
            ]
            if fallback_boards:
                boards.extend(fallback_boards)
                providers_used.append("last_good_membership")
                degraded_reasons.append("live_theme_names_with_last_good_membership")

        # 更新 last-good snapshot
        if boards and any(board.members for board in boards) and "last_good" not in providers_used:
            candidate_last_good = SourceSnapshot(
                snapshot_id=self._generate_snapshot_id(providers_used, trade_date, boards),
                trade_date=trade_date,
                fetched_at=datetime.now(MARKET_TZ).isoformat(),
                providers_used=providers_used,
                boards=boards,
                is_trading_hours=is_trading,
                source_as_of=source_as_of,
                degraded_reasons=list(degraded_reasons),
            )
            candidate_last_good.content_hash = candidate_last_good.snapshot_id.removeprefix("snap_")
            previous_last_good = self._last_good_snapshot
            if not self._persist_last_good:
                # Injected/test routers keep their small in-memory fallback but
                # never contaminate production disk state.
                self._last_good_snapshot = candidate_last_good
            elif self._save_last_good(candidate_last_good, previous=previous_last_good):
                # Assign only after the quality gate and atomic write succeed;
                # otherwise the in-memory transaction source remains intact.
                self._last_good_snapshot = candidate_last_good

        # 构建快照
        snapshot = SourceSnapshot(
            snapshot_id=self._generate_snapshot_id(providers_used, trade_date, boards),
            trade_date=trade_date,
            fetched_at=datetime.now(MARKET_TZ).isoformat(),
            providers_used=providers_used,
            boards=boards,
            is_trading_hours=is_trading,
            source_as_of=source_as_of,
            degraded_reasons=list(degraded_reasons),
        )
        snapshot.content_hash = snapshot.snapshot_id.removeprefix("snap_")

        # 持久化到版本化快照存储（幂等，content_hash 相同则跳过）
        # 仅在持有有效 boards 时保存，避免空快照污染历史版本
        if boards and any(board.members for board in boards):
            persist_store = self._get_snapshot_store()
            if persist_store is not None:
                persist_store.save_snapshot(snapshot)

        # 缓存
        ttl = self.INTRADAY_TTL if is_trading else 1800
        self._snapshot_cache[cache_key] = (snapshot, time.time() + ttl)

        return snapshot

    def resolve_dynamic_theme_members(
        self, theme_name: str, *, count: int = 30
    ) -> ConceptBoard | None:
        """Resolve current theme constituents through the semantic provider.

        Wencai is used only when its rows explicitly contain the requested
        concept in ``所属概念``.  Generic fallback rows are rejected so a
        malformed query cannot silently attach unrelated companies.
        """
        normalized_theme = self._normalized_theme(theme_name)
        if len(normalized_theme) < 2:
            return None
        cached = self._dynamic_member_cache.get(normalized_theme)
        requested_count = max(1, min(int(count), 200))
        if cached is not None and cached[1] > time.time():
            cached_board = cached[0]
            if (
                not cached_board.members_truncated
                or cached_board.returned_count >= requested_count
            ):
                return cached_board
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry
            if self._semantic_registry is None:
                self._semantic_registry = ProviderRegistry()
            result = self._semantic_registry.semantic_search(
                query=f"{theme_name}概念股", count=requested_count
            )
            if str(result.get("source") or "").lower() != "wencai":
                return None
            symbols: list[str] = []
            for row in result.get("results") or []:
                if not isinstance(row, dict):
                    continue
                concepts = row.get("所属概念") or row.get("所属概念板块") or []
                if isinstance(concepts, str):
                    concepts = [part.strip() for part in concepts.replace("；", ";").split(";")]
                if not any(
                    normalized_theme in self._normalized_theme(concept)
                    or self._normalized_theme(concept) in normalized_theme
                    for concept in concepts
                    if self._normalized_theme(concept)
                ):
                    continue
                symbol = self._first(row, "股票代码", "symbol", "code", "ts_code")
                if symbol:
                    symbols.append(symbol)
            symbols = list(dict.fromkeys(symbols))
            if not symbols:
                return None
            try:
                reported_count = int(result.get("code_count") or len(symbols))
            except Exception:
                reported_count = len(symbols)
            returned_count = len(symbols)
            # 覆盖率：仅在数据源报告的总数可信（>0）时计算，否则为 None
            coverage_ratio = (
                returned_count / reported_count if reported_count > 0 else None
            )
            board = ConceptBoard(
                board_name=theme_name,
                board_id="wencai:" + hashlib.sha1(normalized_theme.encode("utf-8")).hexdigest()[:10],
                provider="wencai",
                members=symbols,
                raw_response_hash=hashlib.sha1("|".join(symbols).encode("utf-8")).hexdigest()[:8],
                fetched_at=datetime.now(MARKET_TZ).isoformat(),
                reported_member_count=reported_count,
                members_truncated=reported_count > len(symbols),
                requested_count=count,
                returned_count=returned_count,
                reported_total=reported_count,
                coverage_ratio=coverage_ratio,
                pagination_supported=False,  # 问财当前不支持分页
                next_page_available=False,
            )
            ttl = self.INTRADAY_TTL if self._is_trading_hours() else 1800
            self._dynamic_member_cache[normalized_theme] = (board, time.time() + ttl)
            return board
        except Exception as exc:
            logger.warning("问财动态主题成员解析失败(%s): %s", theme_name, exc)
            return None

    @staticmethod
    def _normalized_theme(value: Any) -> str:
        import re
        return re.sub(r"[^0-9a-z\u4e00-\u9fa5]+", "", str(value or "").lower())

    def compute_member_confidence(
        self,
        symbol: str,
        eastmoney_boards: list[str],
        ths_boards: list[str],
        news_co_occurrence: bool = False,
        market_resonance: bool = False,
    ) -> float:
        """计算成员置信度。

        规则：
        - 东财 + 新闻产业逻辑同时命中：高（0.8+）
        - 东财、同花顺同时命中：中高（0.6-0.8）
        - 只有单一概念接口命中：中（0.4-0.6）
        - 只有行情共振、没有信息证据：低（0.2-0.4）

        Args:
            symbol: 股票代码。
            eastmoney_boards: 命中的东财板块名列表。
            ths_boards: 命中的同花顺板块名列表。
            news_co_occurrence: 是否在新闻共现中出现。
            market_resonance: 是否有行情共振。

        Returns:
            置信度 [0.0, 1.0]。
        """
        has_eastmoney = len(eastmoney_boards) > 0
        has_ths = len(ths_boards) > 0
        has_news = news_co_occurrence
        has_market = market_resonance

        if has_eastmoney and has_news:
            return 0.85  # 高
        elif has_eastmoney and has_ths:
            return 0.70  # 中高
        elif has_eastmoney or has_ths:
            return 0.50  # 中
        elif has_market:
            return 0.30  # 低
        else:
            return 0.10  # 极低
