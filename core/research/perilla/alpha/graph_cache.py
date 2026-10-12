"""GraphCache：SAG知识库的性能优化组件。

提供：
1. 节点深度缓存：避免每次score_node都BFS全图
2. 节点分数缓存：TTL缓存，同一root下节点分数不重复计算
3. 邻接表索引：将O(E)的邻居查找优化为O(1)
4. WAL预写日志：增量持久化，替代全量JSON dump
"""
from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from collections import OrderedDict, defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _safe_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _edge_key(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return tuple(value)


def _record_map(rows: Any, key: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get(key)
        if isinstance(identifier, str) and identifier:
            result[identifier] = dict(row)
    return result


class NodeDepthCache:
    """节点深度缓存：缓存(root, node) -> shortest_depth，避免重复BFS。

    使用 OrderedDict 实现真正的 LRU：get 命中时 move_to_end。
    """

    def __init__(self, max_size: int = 10000) -> None:
        self._cache: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._max_size = _safe_int(max_size, 10000)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, root: str, node: str) -> int | None:
        key = (root, node)
        with self._lock:
            if key in self._cache:
                self._hits += 1
                # LRU：命中时移到末尾（最近使用）
                self._cache.move_to_end(key)
                return self._cache[key]
            self._misses += 1
            return None

    def set(self, root: str, node: str, depth: int) -> None:
        key = (root, node)
        with self._lock:
            if self._max_size <= 0:
                return
            self._cache[key] = depth
            self._cache.move_to_end(key)
            # LRU淘汰：从头部删除最久未使用
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def invalidate_node(self, node: str) -> None:
        """节点变更时，失效所有包含该节点的缓存条目。"""
        with self._lock:
            to_remove = [k for k in self._cache if k[1] == node or k[0] == node]
            for k in to_remove:
                self._cache.pop(k, None)

    def invalidate_all(self) -> None:
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0
            return {"size": len(self._cache), "hits": self._hits, "misses": self._misses, "hit_rate": hit_rate}


class NodeScoreCache:
    """节点分数TTL缓存：(root, node) -> score_dict，带过期时间。

    使用 OrderedDict 实现真正的 LRU：get 命中时 move_to_end。
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 5000) -> None:
        self._cache: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()
        self._ttl = max(0, _safe_int(ttl_seconds, 300))
        self._max_size = _safe_int(max_size, 5000)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, root: str, node: str) -> dict[str, Any] | None:
        key = (root, node)
        now = time.time()
        with self._lock:
            if key in self._cache:
                ts, value = self._cache[key]
                if now - ts < self._ttl:
                    self._hits += 1
                    # LRU：命中时移到末尾（最近使用）
                    self._cache.move_to_end(key)
                    return deepcopy(value)
                else:
                    self._cache.pop(key, None)
            self._misses += 1
            return None

    def set(self, root: str, node: str, score: dict[str, Any]) -> None:
        key = (root, node)
        now = time.time()
        with self._lock:
            if self._max_size <= 0:
                return
            self._cache[key] = (now, deepcopy(dict(score)) if isinstance(score, Mapping) else {})
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def invalidate_node(self, node: str) -> None:
        with self._lock:
            to_remove = [k for k in self._cache if k[1] == node or k[0] == node]
            for k in to_remove:
                self._cache.pop(k, None)

    def invalidate_all(self) -> None:
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0
            return {"size": len(self._cache), "hits": self._hits, "misses": self._misses, "hit_rate": hit_rate}


class AdjacencyIndex:
    """邻接表索引：O(1)邻居查找，替代O(E)遍历。"""

    def __init__(self) -> None:
        self._out_edges: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self._in_edges: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self._lock = threading.RLock()
        self._edge_count = 0

    def rebuild(self, edges: dict[tuple[str, str, str], Any]) -> None:
        """从edges字典重建索引。"""
        with self._lock:
            self._out_edges.clear()
            self._in_edges.clear()
            for raw_key in edges if isinstance(edges, Mapping) else ():
                key = _edge_key(raw_key)
                if key is None:
                    continue
                head, _rel, tail = key
                self._out_edges[head].append(key)
                self._in_edges[tail].append(key)
            self._edge_count = sum(len(items) for items in self._out_edges.values())

    def add_edge(self, head: str, relation: str, tail: str) -> None:
        with self._lock:
            key = _edge_key((head, relation, tail))
            if key is None:
                return
            if key not in self._out_edges.get(head, []):
                self._out_edges[head].append(key)
                self._in_edges[tail].append(key)
                self._edge_count += 1

    def remove_edge(self, head: str, relation: str, tail: str) -> None:
        with self._lock:
            key = _edge_key((head, relation, tail))
            if key is None:
                return
            if key in self._out_edges.get(head, []):
                self._out_edges[head].remove(key)
                if key in self._in_edges.get(tail, []):
                    self._in_edges[tail].remove(key)
                self._edge_count -= 1

    def get_out(self, node: str) -> list[tuple[str, str, str]]:
        with self._lock:
            return list(self._out_edges.get(node, []))

    def get_in(self, node: str) -> list[tuple[str, str, str]]:
        with self._lock:
            return list(self._in_edges.get(node, []))

    def get_both(self, node: str) -> list[tuple[str, str, str]]:
        with self._lock:
            return list(dict.fromkeys(self._out_edges.get(node, []) + self._in_edges.get(node, [])))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"indexed_edges": self._edge_count, "out_nodes": len(self._out_edges), "in_nodes": len(self._in_edges)}


class WALWriter:
    """WAL预写日志：增量持久化，避免每次save()全量dump JSON。

    工作方式：
    1. 每次add_claim/add_event_claim/update_edge_confidence时，追加一条WAL记录
    2. 定期（flush_interval或达到max_entries）将WAL合并到主JSON快照
    3. 启动时从快照+WAL恢复
    """

    def __init__(
        self,
        storage_path: Path,
        flush_interval_seconds: int = 5,
        max_entries_before_rotate: int = 1000,
    ) -> None:
        self._storage_path = storage_path
        self._wal_path = storage_path.with_suffix(".wal")
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_interval = max(0, _safe_int(flush_interval_seconds, 5))
        self._max_entries = max(1, _safe_int(max_entries_before_rotate, 1000))
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._last_flush_time = time.time()
        self._total_writes = 0

    def append(self, operation: str, data: dict[str, Any]) -> None:
        """追加一条WAL记录。

        Args:
            operation: 操作类型（add_claim, add_event, update_edge, update_event_edge, update_claim_status, update_event_status）
            data: 操作数据
        """
        entry = {
            "op": operation if isinstance(operation, str) else str(operation or ""),
            "ts": time.time(),
            "data": dict(data) if isinstance(data, Mapping) else {},
        }
        with self._lock:
            self._buffer.append(entry)
            self._total_writes += 1
            # 判断是否需要flush
            should_flush = (
                len(self._buffer) >= self._max_entries
                or (time.time() - self._last_flush_time) >= self._flush_interval
            )
            if should_flush:
                self._flush_locked()

    def _flush_locked(self) -> None:
        """将缓冲区写入WAL文件。"""
        if not self._buffer:
            return
        try:
            lines: list[str] = []
            for entry in self._buffer:
                try:
                    lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
                except (TypeError, ValueError) as exc:
                    logger.warning("Skipping unserializable WAL entry: %s", exc)
            with open(self._wal_path, "a", encoding="utf-8") as f:
                f.writelines(lines)
            self._buffer.clear()
            self._last_flush_time = time.time()
        except OSError as e:
            logger.error("WAL flush failed: %s", e)

    def flush(self) -> None:
        """强制flush缓冲区。"""
        with self._lock:
            self._flush_locked()

    def recover(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """从WAL恢复状态到快照。

        Args:
            snapshot: 从主JSON加载的快照

        Returns:
            恢复后的完整状态
        """
        if not self._wal_path.exists():
            return snapshot

        state = deepcopy(dict(snapshot)) if isinstance(snapshot, Mapping) else {}
        nodes = _record_map(state.get("nodes", []), "name")
        claims = _record_map(state.get("claims", []), "claim_id")
        event_claims = _record_map(state.get("event_claims", []), "event_id")
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in state.get("edges", []) if isinstance(state.get("edges", []), list) else []:
            if isinstance(row, Mapping):
                key = _edge_key((row.get("head"), row.get("relation"), row.get("tail")))
                if key is not None:
                    edges[key] = dict(row)
        event_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in state.get("event_edges", []) if isinstance(state.get("event_edges", []), list) else []:
            if isinstance(row, Mapping):
                key = _edge_key((row.get("subject"), row.get("event_type"), row.get("object")))
                if key is not None:
                    event_edges[key] = dict(row)

        try:
            with open(self._wal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    self._apply_entry(entry, nodes, edges, claims, event_edges, event_claims)
        except OSError as e:
            logger.error("WAL recovery failed: %s", e)

        state["nodes"] = list(nodes.values())
        state["edges"] = list(edges.values())
        state["claims"] = list(claims.values())
        state["event_edges"] = list(event_edges.values())
        state["event_claims"] = list(event_claims.values())
        return state

    @staticmethod
    def _apply_entry(
        entry: dict[str, Any],
        nodes: dict[str, dict],
        edges: dict[tuple, dict],
        claims: dict[str, dict],
        event_edges: dict[tuple, dict],
        event_claims: dict[str, dict],
    ) -> None:
        """应用单条WAL记录到内存状态。"""
        if not isinstance(entry, Mapping):
            return
        op = entry.get("op")
        data = entry.get("data", {})
        if not isinstance(data, Mapping):
            return

        if op == "add_claim":
            claim = data.get("claim")
            if isinstance(claim, Mapping) and isinstance(claim.get("claim_id"), str):
                claims[claim["claim_id"]] = dict(claim)
            node_data_list = data.get("nodes", [])
            for n in node_data_list if isinstance(node_data_list, list) else []:
                if isinstance(n, Mapping) and isinstance(n.get("name"), str):
                    nodes[n["name"]] = dict(n)
            edge_key_data = _edge_key(data.get("edge_key"))
            edge_data = data.get("edge")
            if edge_key_data and isinstance(edge_data, Mapping):
                edges[edge_key_data] = dict(edge_data)
        elif op == "update_edge":
            edge_key_data = _edge_key(data.get("edge_key"))
            edge_data = data.get("edge")
            if edge_key_data and isinstance(edge_data, Mapping):
                edges[edge_key_data] = dict(edge_data)
        elif op == "update_claim_status":
            cid = data.get("claim_id")
            if cid and cid in claims:
                claims[cid]["status"] = data.get("status", claims[cid].get("status"))
        elif op == "add_event":
            ec = data.get("event_claim")
            if isinstance(ec, Mapping) and isinstance(ec.get("event_id"), str):
                event_claims[ec["event_id"]] = dict(ec)
            ee = data.get("event_edge")
            eek = _edge_key(data.get("event_edge_key"))
            if isinstance(ee, Mapping) and eek:
                event_edges[eek] = dict(ee)
        elif op == "update_event_edge":
            ee = data.get("event_edge")
            eek = _edge_key(data.get("event_edge_key"))
            if isinstance(ee, Mapping) and eek:
                event_edges[eek] = dict(ee)
        elif op == "update_event_status":
            eid = data.get("event_id")
            if eid and eid in event_claims:
                event_claims[eid]["status"] = data.get("status", event_claims[eid].get("status"))

    def checkpoint(self) -> None:
        """在主快照保存成功后，截断WAL。"""
        with self._lock:
            self._flush_locked()
            try:
                if self._wal_path.exists():
                    self._wal_path.unlink()
            except OSError:
                pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            try:
                wal_size = self._wal_path.stat().st_size if self._wal_path.exists() else 0
            except OSError:
                wal_size = 0
            return {
                "buffered_entries": len(self._buffer),
                "total_writes": self._total_writes,
                "wal_file_bytes": wal_size,
                "time_since_last_flush": time.time() - self._last_flush_time,
            }
