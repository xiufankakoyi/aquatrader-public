"""动态主题控制面服务层。

DynamicThemeEngine 串联数据源路由、事件聚类、主题装配、持续性/反转/历史再发现
三个探测器，输出最终 ThemePacket 列表。任一组件异常均降级记录，不中断整体流程。
"""
from __future__ import annotations

import logging
import uuid
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Callable

from .contracts import ThemePacket, generate_theme_id, generate_theme_run_id
from .event_clusterer import EventClusterer, NewsEvent
from .legacy_theme_detector import LegacyThemeDetector
from .persistence_detector import NewsPersistenceDetector
from .reversal_detector import IceReversalDetector
from .repository import ThemeRepository
from .source_router import SourceSnapshot, ThemeSourceRouter
from .theme_assembler import ThemeAssembler

logger = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Asia/Shanghai")


class DynamicThemeEngine:
    """动态主题引擎服务层。

    串联以下流程：
        source_router.fetch_concept_snapshot
        → event_clusterer.cluster_events
        → theme_assembler.assemble
        → persistence_detector.detect_many
        → reversal_detector.detect_many
        → legacy_theme_detector.detect_many
        → legacy_theme_detector.scan_legacy_themes
        → 合并去重 → 输出 ThemePacket 列表

    设计原则：
    - 防御性编程：任一组件抛异常时捕获并记录到 degradation_reasons，不中断流程
    - 降级标记：source_router 返回降级快照或任一组件失败时，packet.degraded=True
    - 合并去重：同 theme_id 取评分更高的；scan_legacy_themes 输出覆盖 detect_many
    """

    STRATEGY_VERSION = "dynamic_theme_v1"

    def __init__(
        self,
        source_router: ThemeSourceRouter,
        event_clusterer: EventClusterer,
        theme_assembler: ThemeAssembler,
        persistence_detector: NewsPersistenceDetector,
        reversal_detector: IceReversalDetector,
        legacy_theme_detector: LegacyThemeDetector,
        repository: ThemeRepository | None = None,
        news_loader: Callable[[], list[dict[str, Any]]] | None = None,
        market_feature_enricher: Any | None = None,
    ) -> None:
        """初始化动态主题引擎。

        Args:
            source_router: 主题数据源路由器。
            event_clusterer: 新闻事件聚类器。
            theme_assembler: 主题装配器。
            persistence_detector: 新闻持续性检测器。
            reversal_detector: 冰点反转检测器。
            legacy_theme_detector: 历史主题再发现检测器。
            repository: ThemePacket 持久化仓库，可选。
        """
        self._source_router = source_router
        self._event_clusterer = event_clusterer
        self._theme_assembler = theme_assembler
        self._persistence_detector = persistence_detector
        self._reversal_detector = reversal_detector
        self._legacy_theme_detector = legacy_theme_detector
        self._repository = repository
        self._news_loader = news_loader
        self._market_feature_enricher = market_feature_enricher

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #

    def generate(
        self,
        *,
        as_of: str = "",
        trade_date: str = "",
        max_themes: int = 15,
        max_source_boards: int | None = None,
    ) -> list[ThemePacket]:
        """生成动态主题列表。

        完整流程：
        1. 通过 source_router 获取板块快照
        2. 通过 event_clusterer 聚类新闻事件
        3. 通过 theme_assembler 装配为初始 ThemePacket
        4. 通过三个探测器分别检测
        5. 合并去重（同 theme_id 取评分更高者，scan_legacy_themes 覆盖）
        6. 限制 max_themes
        7. 持久化到 repository（如果提供）

        Args:
            as_of: PIT 时点（ISO 8601），空则用当前 UTC。
            trade_date: 交易日（YYYY-MM-DD），空则取 as_of 前 10 字符。
            max_themes: 最大返回主题数。

        Returns:
            ThemePacket 列表，按主题类型分桶（news_persistence / ice_reversal / legacy_reentry）。
        """
        as_of_resolved = as_of or datetime.now(MARKET_TZ).isoformat()
        trade_date_resolved = trade_date or as_of_resolved[:10]
        trace_id = uuid.uuid4().hex[:16]
        detected_at = datetime.now(MARKET_TZ).isoformat()
        degradation_reasons: list[str] = []

        # 1. 获取板块快照
        snapshot = self._fetch_snapshot(
            trade_date_resolved,
            degradation_reasons,
            max_source_boards=max_source_boards,
        )
        if snapshot is None:
            # source_router 失败，直接返回空列表
            return []

        # 2. 新闻事件聚类
        news_events = self._cluster_news_events(as_of_resolved, degradation_reasons)

        # 3. 装配初始 ThemePacket
        packets = self._assemble_packets(
            snapshot, news_events, degradation_reasons
        )
        if not packets:
            return []

        if self._market_feature_enricher is not None:
            try:
                packets = self._market_feature_enricher.enrich_many(
                    packets, as_of=as_of_resolved
                )
            except Exception as exc:
                degradation_reasons.append(f"theme_market_feature_enricher_failed: {exc}")

        # 4. 三个探测器分别检测（探测器不依赖 trace_id 等元数据，可先于 stamp 运行）
        persistence_results = self._run_detector_safe(
            self._persistence_detector, "persistence_detector",
            packets, as_of_resolved, degradation_reasons,
        )
        reversal_results = self._run_detector_safe(
            self._reversal_detector, "reversal_detector",
            packets, as_of_resolved, degradation_reasons,
        )
        legacy_detect_results = self._run_detector_safe(
            self._legacy_theme_detector, "legacy_theme_detector",
            packets, as_of_resolved, degradation_reasons,
        )

        # 5. 主动扫描历史再发现主题
        scan_results = self._scan_legacy_themes_safe(
            as_of_resolved, degradation_reasons
        )

        # 6. 合并去重
        detected_packets = persistence_results + reversal_results + legacy_detect_results
        merged = self._merge_packets(detected_packets, scan_results)

        # Preserve evidence-bearing packets that have not yet met promotion
        # thresholds as research-discovery WATCH items.  Dropping them made a
        # healthy news scan indistinguishable from a broken pipeline.
        merged_ids = {packet.theme_id for packet in merged}
        for packet in packets:
            has_information_signal = bool(packet.catalysts)
            has_market_signal = packet.phase in {"STARTUP", "REVERSAL_CANDIDATE"}
            if (
                packet.theme_id
                and packet.theme_id not in merged_ids
                and packet.members
                and (has_information_signal or has_market_signal)
            ):
                exploratory = deepcopy(packet)
                exploratory.theme_type = (
                    "research_discovery"
                    if has_information_signal else "market_discovery"
                )
                if not has_market_signal:
                    exploratory.phase = "UNKNOWN"
                exploratory.degraded = True
                exploratory.degradation_reasons.append("awaiting_research_validation")
                if has_market_signal and not has_information_signal:
                    exploratory.degradation_reasons.append(
                        "market_only_requires_research_catalyst"
                    )
                merged.append(exploratory)
                merged_ids.add(exploratory.theme_id)

        merged.sort(key=self._packet_score, reverse=True)

        # 7. 限制 max_themes
        merged = merged[: max(0, max_themes)]

        # 8. 填充运行时元数据（含全部降级原因，stamp 在探测器之后以确保降级原因完整）
        self._stamp_packets(
            merged,
            snapshot=snapshot,
            as_of=as_of_resolved,
            detected_at=detected_at,
            trace_id=trace_id,
            degradation_reasons=degradation_reasons,
        )

        # 9. 持久化
        self._persist_packets(merged, degradation_reasons)

        return merged

    # ------------------------------------------------------------------ #
    # P2.3 只读输出契约（供 discovery/change_radar 消费）
    # ------------------------------------------------------------------ #

    def export_theme_change_signals(
        self,
        packets: list[ThemePacket] | None = None,
        *,
        as_of: str = "",
    ) -> list[dict[str, Any]]:
        """只读导出主题变化信号，供 ChangeRadar 消费。

        P2.3 要求「修改 core/quant/theme/service.py，仅增加正式只读输出契约」。
        本方法仅从已生成的 ThemePacket 列表导出主题变化信号，不修改任何状态，
        不触发新的检索或探测，不输出交易动作建议。

        输出的信号需满足 P2.3 验收点 2「事件必须有时间、实体、方向、来源和影响路径」：
        - observed_at：取 packet.detected_at 或 as_of
        - entity_name：取 packet.theme_name
        - direction：根据 negative_risk_score 与 news_score 推断
        - source_refs：取 packet.trace_id 与 catalysts 的 source_url
        - impact_path：取 packet.theme_name + phase + theme_type

        Args:
            packets: 已生成的 ThemePacket 列表；None 表示调用 generate 后内部缓存
            as_of: PIT 时点；空则取 packet.as_of

        Returns:
            主题变化信号字典列表；每个字典含 ChangeEvent 必填字段
        """
        # 仅只读：不修改 packets 状态
        if packets is None:
            return []
        signals: list[dict[str, Any]] = []
        cutoff = (as_of or "")[:10]
        for packet in packets:
            try:
                signal = self._export_one_signal(packet, cutoff)
                if signal is not None:
                    signals.append(signal)
            except Exception:  # noqa: BLE001
                # AGENTS.md：缺数据降级，不崩溃
                continue
        return signals

    @staticmethod
    def _export_one_signal(
        packet: ThemePacket,
        cutoff: str,
    ) -> dict[str, Any] | None:
        """导出单个 ThemePacket 为变化信号。"""
        observed_at = (packet.detected_at or packet.as_of or cutoff)[:10]
        if not observed_at:
            return None
        theme_name = packet.theme_name or ""
        if not theme_name:
            return None
        # 方向推断：negative_risk_score > 0.6 → negative；news_score > 0.6 → positive
        if packet.negative_risk_score > 0.6:
            direction = "negative"
        elif packet.news_score > 0.6:
            direction = "positive"
        else:
            direction = "neutral"
        source_refs: list[str] = [packet.trace_id] if packet.trace_id else []
        for catalyst in packet.catalysts:
            if catalyst.source_url:
                source_refs.append(catalyst.source_url)
            if catalyst.event_date:
                # 用 catalyst 的 event_date 作为更精确的 observed_at
                observed_at = catalyst.event_date[:10]
                break
        if not source_refs:
            source_refs = [packet.theme_id or "theme_unknown"]
        impact_path = (
            f"{theme_name} | phase={packet.phase or 'unknown'} | "
            f"type={packet.theme_type or 'unknown'}"
        )
        return {
            "theme_id": packet.theme_id,
            "observed_at": observed_at,
            "entity_name": theme_name,
            "entity_kind": "industry",
            "direction": direction,
            "magnitude": packet.news_score,
            "magnitude_unit": "score",
            "source_refs": source_refs,
            "impact_path": impact_path,
            "impact_path_steps": [
                f"theme_phase: {packet.phase or 'unknown'}",
                f"theme_type: {packet.theme_type or 'unknown'}",
                f"persistence_score: {packet.persistence_score:.3f}",
                f"breadth_score: {packet.breadth_score:.3f}",
            ],
            "expires_at": (packet.expires_at or "")[:10],
            "event_type": "policy_subsidy",  # 默认；调用方可覆盖
        }

    def generate_one(
        self,
        theme_keywords: str,
        event_entities: str,
        core_symbols: list[str],
        *,
        as_of: str = "",
    ) -> ThemePacket | None:
        """针对已知主题关键词生成单个 ThemePacket（用于测试和手动触发）。

        通过 generate_theme_id 生成 theme_id，构造初始 packet 后跑探测器，
        返回评分最高的探测器输出；若所有探测器都未通过，返回装配后的原始 packet。

        Args:
            theme_keywords: 主题关键词（如 "HBM"）。
            event_entities: 核心事件实体描述。
            core_symbols: 核心股票代码列表。
            as_of: PIT 时点（ISO 8601），空则用当前 UTC。

        Returns:
            ThemePacket 实例；装配失败时返回 None。
        """
        as_of_resolved = as_of or datetime.now(MARKET_TZ).isoformat()
        trace_id = uuid.uuid4().hex[:16]
        detected_at = datetime.now(MARKET_TZ).isoformat()
        degradation_reasons: list[str] = []

        # 获取快照（失败时用空快照降级，仍可生成 packet）
        snapshot = self._fetch_snapshot(
            as_of_resolved[:10], degradation_reasons, allow_empty=True
        )
        if snapshot is None:
            snapshot = SourceSnapshot()

        # 生成 theme_id 和 theme_run_id
        theme_id = generate_theme_id(theme_keywords, event_entities, core_symbols)
        theme_run_id = generate_theme_run_id(theme_id, as_of_resolved)

        # 装配初始 packet
        try:
            packet = self._theme_assembler.assemble(
                theme_name=theme_keywords,
                theme_keywords=[theme_keywords] if theme_keywords else [],
                event_entities=event_entities,
                snapshot=snapshot,
                news_events=None,
            )
        except Exception as exc:  # noqa: BLE001 — 防御性：装配失败返回 None
            logger.warning("generate_one assemble failed: %s", exc)
            degradation_reasons.append(f"theme_assembler_failed: {exc}")
            return None

        # 覆盖 theme_id / theme_run_id（确保与传入参数一致）
        packet.theme_id = theme_id
        packet.theme_run_id = theme_run_id

        # 填充运行时元数据
        self._stamp_packets(
            [packet],
            snapshot=snapshot,
            as_of=as_of_resolved,
            detected_at=detected_at,
            trace_id=trace_id,
            degradation_reasons=degradation_reasons,
        )

        # 跑三个探测器，取评分最高的输出
        detector_results: list[ThemePacket] = []
        for detector, name in (
            (self._persistence_detector, "persistence_detector"),
            (self._reversal_detector, "reversal_detector"),
            (self._legacy_theme_detector, "legacy_theme_detector"),
        ):
            try:
                result = detector.detect_many([packet], as_of=as_of_resolved)
                detector_results.extend(result)
            except Exception as exc:  # noqa: BLE001 — 防御性：单探测器失败跳过
                logger.warning("generate_one %s failed: %s", name, exc)
                packet.degradation_reasons.append(f"{name}_failed: {exc}")

        # 取评分最高的探测器输出；若无通过，返回原始 packet
        if detector_results:
            best = max(detector_results, key=self._packet_score)
            # 保留降级标记
            if packet.degraded:
                best.degraded = True
                best.degradation_reasons.extend(packet.degradation_reasons)
            packet = best

        # 持久化
        self._persist_packets([packet], degradation_reasons)

        return packet

    # ------------------------------------------------------------------ #
    # 流程步骤
    # ------------------------------------------------------------------ #

    def _fetch_snapshot(
        self,
        trade_date: str,
        degradation_reasons: list[str],
        *,
        allow_empty: bool = False,
        max_source_boards: int | None = None,
    ) -> SourceSnapshot | None:
        """通过 source_router 获取板块快照。

        Args:
            trade_date: 交易日期。
            degradation_reasons: 降级原因收集列表。
            allow_empty: True 时即使快照降级也返回（用于 generate_one）。

        Returns:
            SourceSnapshot 实例；source_router 抛异常且 allow_empty=False 时返回 None。
        """
        try:
            kwargs: dict[str, Any] = {"trade_date": trade_date}
            if max_source_boards is not None:
                kwargs["max_boards"] = max(1, int(max_source_boards))
            snapshot = self._source_router.fetch_concept_snapshot(**kwargs)
        except Exception as exc:  # noqa: BLE001 — 防御性：source_router 失败
            logger.warning("source_router fetch failed: %s", exc)
            degradation_reasons.append(f"source_router_failed: {exc}")
            return None if not allow_empty else SourceSnapshot()

        # 检查快照降级
        if self._is_snapshot_degraded(snapshot):
            degradation_reasons.append("source_snapshot_degraded")
        return snapshot

    def _cluster_news_events(
        self, as_of: str, degradation_reasons: list[str]
    ) -> list[NewsEvent]:
        """通过 event_clusterer 聚类新闻事件。

        当前流程未注入独立新闻源，传空列表；子类可覆写以注入新闻数据。
        聚类失败或返回空时记录降级。

        Args:
            degradation_reasons: 降级原因收集列表。

        Returns:
            NewsEvent 列表；失败时返回空列表。
        """
        try:
            as_of_dt = self._parse_datetime(as_of)
            market_now = datetime.now(MARKET_TZ)
            market_as_of = as_of_dt.astimezone(MARKET_TZ) if as_of_dt is not None else None
            if market_as_of is not None and market_as_of.date() < market_now.date():
                degradation_reasons.append("historical_current_news_fetch_forbidden")
                return []
            news_items = self._news_loader() if self._news_loader is not None else []
            # One named company is sufficient to create an exploratory theme;
            # persistence promotion still requires independent sources and
            # broader membership in NewsPersistenceDetector.
            events = self._event_clusterer.cluster_events(news_items, min_co_occurrence=1)
            visible: list[NewsEvent] = []
            for event in events:
                event_dt = self._parse_event_datetime(event.event_date, as_of_dt)
                if event_dt is None:
                    degradation_reasons.append(f"news_time_unknown:{event.event_id}")
                    # Unknown-time evidence is visible only in a live/current
                    # scan and remains degraded; never expose it historically.
                    if market_as_of is None or market_as_of.date() == market_now.date():
                        visible.append(event)
                    continue
                if as_of_dt is None or event_dt <= as_of_dt:
                    visible.append(event)
            events = visible
        except Exception as exc:  # noqa: BLE001 — 防御性：聚类失败跳过
            logger.warning("event_clusterer failed: %s", exc)
            degradation_reasons.append(f"event_clusterer_failed: {exc}")
            return []

        if not events:
            degradation_reasons.append("event_clusterer_empty")
        return events

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @classmethod
    def _parse_event_datetime(
        cls, value: str, as_of: datetime | None
    ) -> datetime | None:
        """解析新闻事件时间为 timezone-aware datetime。

        按优先级依次尝试以下格式：
        1. 13 位纯数字 → Unix 毫秒时间戳
        2. 10 位纯数字 → Unix 秒时间戳
        3. 8 位纯数字 → YYYYMMDD 日期
        4. 中文日期格式（YYYY年MM月DD日 [HH:MM[:SS]]）
        5. ISO 格式（fromisoformat，含/不含时区）
        6. MM-DD HH:MM（用 as_of 年份补全）
        7. HH:MM（用 as_of 日期补全）

        未知格式返回 None，调用方须按安全降级规则处理：
        实时页面可展示但标记"时间未知"，历史评价不可见；
        不得将未知时间赋值为 as_of，不得将抓取时间当发布时间。

        Args:
            value: 原始时间字符串。
            as_of: PIT 时点，用于补全缺省日期/年份。

        Returns:
            解析后的 timezone-aware datetime；无法解析时返回 None。
        """
        text = str(value or "").strip()
        if not text:
            return None

        # 1. 13 位纯数字 → Unix 毫秒时间戳
        if text.isdigit() and len(text) == 13:
            try:
                return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
            except Exception:
                return None

        # 2. 10 位纯数字 → Unix 秒时间戳
        if text.isdigit() and len(text) == 10:
            try:
                return datetime.fromtimestamp(int(text), tz=timezone.utc)
            except Exception:
                return None

        # 3. 8 位纯数字 → YYYYMMDD 日期
        if text.isdigit() and len(text) == 8:
            try:
                parsed = datetime.strptime(text, "%Y%m%d")
                return parsed.replace(tzinfo=timezone.utc)
            except Exception:
                return None

        # 4. 中文日期格式（YYYY年MM月DD日 [HH:MM[:SS]]）
        if "年" in text and "月" in text:
            parsed = cls._parse_chinese_datetime(text)
            if parsed is not None:
                return parsed

        # 5. ISO 格式（fromisoformat，含/不含时区）
        parsed = cls._parse_datetime(text)
        if parsed is not None:
            return parsed

        # 6. MM-DD HH:MM（用 as_of 年份补全）
        if as_of is not None:
            parsed = cls._parse_month_day_time(text, as_of)
            if parsed is not None:
                return parsed

        # 7. HH:MM（用 as_of 日期补全）
        if as_of is not None:
            try:
                clock = datetime.strptime(text[:5], "%H:%M").time()
                return datetime.combine(
                    as_of.date(), clock, tzinfo=as_of.tzinfo or timezone.utc
                )
            except Exception:
                pass
        return None

    @staticmethod
    def _parse_chinese_datetime(text: str) -> datetime | None:
        """解析中文日期格式。

        支持格式：
        - YYYY年MM月DD日
        - YYYY年MM月DD日 HH:MM
        - YYYY年MM月DD日 HH:MM:SS

        月份/日期支持非零填充（如"7月13日"）。

        Args:
            text: 中文日期字符串。

        Returns:
            timezone-aware datetime（UTC）；无法解析时返回 None。
        """
        cleaned = text.strip()
        # 依次尝试带秒、带时分、纯日期三种格式
        for fmt in ("%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M", "%Y年%m月%d日"):
            try:
                parsed = datetime.strptime(cleaned, fmt)
                return parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_month_day_time(text: str, as_of: datetime) -> datetime | None:
        """解析 MM-DD HH:MM 格式，用 as_of 年份补全。

        用于处理省略年份的日期时间（如"07-13 14:30"），
        拼接 as_of 年份后解析，避免无年份解析的弃用警告。

        Args:
            text: 时间字符串，如 "07-13 14:30"。
            as_of: PIT 时点，提供年份和时区。

        Returns:
            timezone-aware datetime；无法解析时返回 None。
        """
        try:
            # 拼接 as_of 年份，避免 Python 3.15 无年份解析弃用警告
            parsed = datetime.strptime(
                f"{as_of.year}-{text.strip()}", "%Y-%m-%d %H:%M"
            )
            return parsed.replace(tzinfo=as_of.tzinfo or timezone.utc)
        except ValueError:
            return None

    def _assemble_packets(
        self,
        snapshot: SourceSnapshot,
        news_events: list[NewsEvent],
        degradation_reasons: list[str],
    ) -> list[ThemePacket]:
        """通过 theme_assembler 装配初始 ThemePacket 列表。

        从 snapshot.boards 提取主题候选，对每个板块调用 assemble。
        装配失败时记录降级并返回空列表。

        Args:
            snapshot: 数据源快照。
            news_events: 新闻事件列表。
            degradation_reasons: 降级原因收集列表。

        Returns:
            初始 ThemePacket 列表；装配失败时返回空列表。
        """
        packets: list[ThemePacket] = []
        for board in snapshot.boards:
            if not board.board_name:
                continue
            try:
                relevant_events = [
                    event for event in news_events
                    if self._event_matches_theme(event, board.board_name)
                ]
                packet = self._theme_assembler.assemble(
                    theme_name=board.board_name,
                    theme_keywords=[board.board_name],
                    event_entities=board.board_name,
                    snapshot=snapshot,
                    news_events=relevant_events,
                )
                packets.append(packet)
            except Exception as exc:  # noqa: BLE001 — 防御性：单个板块装配失败跳过
                logger.warning(
                    "theme_assembler failed for board=%s: %s",
                    board.board_name, exc,
                )
                degradation_reasons.append(
                    f"theme_assembler_failed: {exc}"
                )

        # Concept APIs are allowed to fail.  Independent news clusters must be
        # able to create ephemeral themes on their own; otherwise the declared
        # eastmoney -> ths -> news fallback chain is not real.
        if not snapshot.boards:
            degradation_reasons.append("no_concept_boards")
        for event in news_events:
            if not event.keywords or (
                not event.theme_hints and len(event.co_occurred_symbols) < 2
            ):
                continue
            theme_name = event.theme_hints[0] if event.theme_hints else event.keywords[0]
            try:
                event_snapshot = snapshot
                resolved_board = None
                resolver = getattr(self._source_router, "resolve_dynamic_theme_members", None)
                if callable(resolver):
                    resolved_board = resolver(theme_name)
                    if resolved_board is not None:
                        matching_boards = [
                            board for board in snapshot.boards
                            if board.board_name.strip().lower() == theme_name.strip().lower()
                        ]
                        event_snapshot_id = "snap_event_" + hashlib.sha1(
                            f"{snapshot.snapshot_id}|{resolved_board.raw_response_hash}".encode("utf-8")
                        ).hexdigest()[:12]
                        event_snapshot = SourceSnapshot(
                            snapshot_id=event_snapshot_id,
                            trade_date=snapshot.trade_date,
                            fetched_at=snapshot.fetched_at,
                            providers_used=list(dict.fromkeys([*snapshot.providers_used, "wencai"])),
                            boards=[*matching_boards, resolved_board],
                            is_trading_hours=snapshot.is_trading_hours,
                            source_as_of=snapshot.source_as_of,
                            degraded_reasons=list(snapshot.degraded_reasons),
                            content_hash=snapshot.content_hash,
                        )
                packet = self._theme_assembler.assemble(
                    theme_name=theme_name,
                    theme_keywords=list(event.keywords[:5]),
                    event_entities=event.title or theme_name,
                    snapshot=event_snapshot,
                    news_events=[event],
                )
                if resolved_board is not None:
                    packet.source_status["member_provider"] = "wencai"
                    packet.source_status["reported_member_count"] = str(
                        resolved_board.reported_member_count
                    )
                    # 截断结构化信息：写入 source_status 供下游候选质量控制感知
                    packet.source_status.update({
                        "requested_count": str(resolved_board.requested_count),
                        "returned_count": str(resolved_board.returned_count),
                        "reported_total": str(resolved_board.reported_total),
                        "coverage_ratio": (
                            str(resolved_board.coverage_ratio)
                            if resolved_board.coverage_ratio is not None
                            else ""
                        ),
                        "pagination_supported": str(resolved_board.pagination_supported),
                        "next_page_available": str(resolved_board.next_page_available),
                        "members_truncated": str(resolved_board.members_truncated),
                    })
                    if resolved_board.members_truncated:
                        packet.degraded = True
                        packet.degradation_reasons.append("wencai_members_truncated")
                if "news_cluster" not in packet.degradation_reasons:
                    packet.degradation_reasons.append("concept_sources_unavailable_news_only")
                packet.degraded = True
                packets.append(packet)
            except Exception as exc:  # noqa: BLE001
                logger.warning("theme_assembler failed for news event=%s: %s", event.event_id, exc)
                degradation_reasons.append(f"news_theme_assembler_failed: {exc}")

        if not packets:
            degradation_reasons.append("theme_assembler_no_packets")
        return packets

    @staticmethod
    def _event_matches_theme(event: NewsEvent, theme_name: str) -> bool:
        text = f"{event.title} {event.summary}".lower()
        name = str(theme_name or "").strip().lower()
        if name and name in text:
            return True
        return any(str(keyword).strip().lower() in name for keyword in event.keywords if keyword)

    def _run_detector_safe(
        self,
        detector: Any,
        name: str,
        packets: list[ThemePacket],
        as_of: str,
        degradation_reasons: list[str],
    ) -> list[ThemePacket]:
        """安全调用探测器 detect_many，异常时记录降级并返回空列表。

        Args:
            detector: 探测器实例（需有 detect_many 方法）。
            name: 探测器名称（用于降级原因记录）。
            packets: 待检测的 ThemePacket 列表。
            as_of: 检测基准时间。
            degradation_reasons: 降级原因收集列表。

        Returns:
            通过检测的 ThemePacket 列表；异常时返回空列表。
        """
        try:
            return detector.detect_many(packets, as_of=as_of)
        except Exception as exc:  # noqa: BLE001 — 防御性：探测器失败跳过
            logger.warning("%s detect_many failed: %s", name, exc)
            degradation_reasons.append(f"{name}_failed: {exc}")
            return []

    def _scan_legacy_themes_safe(
        self, as_of: str, degradation_reasons: list[str]
    ) -> list[ThemePacket]:
        """安全调用 legacy_theme_detector.scan_legacy_themes。

        Args:
            as_of: 检测基准时间。
            degradation_reasons: 降级原因收集列表。

        Returns:
            再发现候选 ThemePacket 列表；异常时返回空列表。
        """
        try:
            return self._legacy_theme_detector.scan_legacy_themes(as_of=as_of)
        except Exception as exc:  # noqa: BLE001 — 防御性：扫描失败跳过
            logger.warning("scan_legacy_themes failed: %s", exc)
            degradation_reasons.append(f"scan_legacy_themes_failed: {exc}")
            return []

    def _stamp_packets(
        self,
        packets: list[ThemePacket],
        *,
        snapshot: SourceSnapshot,
        as_of: str,
        detected_at: str,
        trace_id: str,
        degradation_reasons: list[str],
    ) -> None:
        """为每个 packet 填充运行时元数据。

        Args:
            packets: 待填充的 ThemePacket 列表。
            snapshot: 数据源快照（用于 source_snapshot_id）。
            as_of: PIT 时点。
            detected_at: 检测时间戳。
            trace_id: 本次运行追踪 ID。
            degradation_reasons: 降级原因列表（会追加到每个 packet）。
        """
        for packet in packets:
            packet.trace_id = trace_id
            packet.strategy_version = self.STRATEGY_VERSION
            if not packet.source_snapshot_id:
                packet.source_snapshot_id = snapshot.snapshot_id
            packet.source_status = {
                **packet.source_status,
                "providers": ",".join(snapshot.providers_used),
                "source_as_of": snapshot.source_as_of or snapshot.trade_date,
                "snapshot_content_hash": snapshot.content_hash,
            }
            packet.as_of = as_of
            packet.detected_at = detected_at
            # 重新生成 theme_run_id 以确保与 as_of 一致
            if packet.theme_id:
                packet.theme_run_id = generate_theme_run_id(
                    packet.theme_id, as_of
                )
            if degradation_reasons:
                packet.degraded = True
                packet.degradation_reasons.extend(degradation_reasons)

    def _merge_packets(
        self,
        detected_packets: list[ThemePacket],
        scan_packets: list[ThemePacket],
    ) -> list[ThemePacket]:
        """合并去重。

        规则：
        - detected_packets 之间：同 theme_id 取评分更高的
        - scan_packets 覆盖 detected_packets（scan_legacy_themes 输出更完整）

        Args:
            detected_packets: 三个探测器 detect_many 的合并输出。
            scan_packets: scan_legacy_themes 的输出。

        Returns:
            去重后的 ThemePacket 列表。
        """
        merged: dict[str, ThemePacket] = {}

        # 1. 合并 detected_packets，同 theme_id 取评分更高者
        for packet in detected_packets:
            if not packet.theme_id:
                continue
            existing = merged.get(packet.theme_id)
            if existing is None or self._packet_score(packet) > self._packet_score(existing):
                merged[packet.theme_id] = packet

        # 2. scan_packets 覆盖（更完整）
        for packet in scan_packets:
            if not packet.theme_id:
                continue
            merged[packet.theme_id] = packet

        return list(merged.values())

    def _persist_packets(
        self,
        packets: list[ThemePacket],
        degradation_reasons: list[str],
    ) -> None:
        """持久化 ThemePacket 列表到 repository。

        持久化失败不阻塞返回，记录到 degradation_reasons。

        Args:
            packets: 待持久化的 ThemePacket 列表。
            degradation_reasons: 降级原因收集列表。
        """
        if self._repository is None:
            return
        for packet in packets:
            try:
                self._repository.save_packet(packet)
            except Exception as exc:  # noqa: BLE001 — 防御性：持久化失败不阻塞
                logger.warning("repository save_packet failed: %s", exc)
                reason = f"repository_save_failed: {exc}"
                packet.degraded = True
                packet.degradation_reasons.append(reason)
                degradation_reasons.append(reason)

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #

    def _is_snapshot_degraded(self, snapshot: SourceSnapshot) -> bool:
        """判断快照是否处于降级状态。

        判断依据：
        - source_status 字段（防御性获取，未来扩展兼容）含 unavailable/degraded
        - providers_used 为空（所有源失败）
        - providers_used 含 "last_good"（使用上次快照降级）

        Args:
            snapshot: 数据源快照。

        Returns:
            降级返回 True，否则 False。
        """
        # 防御性：未来 SourceSnapshot 可能新增 source_status 字段
        source_status = getattr(snapshot, "source_status", None) or {}
        for status in source_status.values() if isinstance(source_status, dict) else []:
            if status in ("unavailable", "degraded"):
                return True
        if not snapshot.providers_used:
            return True
        if snapshot.degraded_reasons:
            return True
        if any("last_good" in provider for provider in snapshot.providers_used):
            return True
        return False

    @staticmethod
    def _packet_score(packet: ThemePacket) -> float:
        """计算 packet 综合评分，用于合并去重比较。

        取 news_score / reversal_score / persistence_score 中的最大值。

        Args:
            packet: ThemePacket 实例。

        Returns:
            综合评分。
        """
        return max(
            packet.news_score,
            packet.reversal_score,
            packet.persistence_score,
        )
