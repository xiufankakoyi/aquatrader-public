"""主题装配器。

将多源证据（东财概念、同花顺概念、新闻事件、行情共振）装配为 ThemePacket。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .contracts import (
    ThemeCatalyst,
    ThemeMember,
    ThemePacket,
    generate_theme_id,
    generate_theme_run_id,
)
from .event_clusterer import NewsEvent
from .source_router import ConceptBoard, SourceSnapshot

logger = logging.getLogger(__name__)


class ThemeAssembler:
    """主题装配器。

    将多源证据装配为 ThemePacket：
    1. 从 SourceSnapshot 提取概念板块成员
    2. 从 NewsEvent 列表提取新闻共现股票
    3. 合并成员，计算每个成员的置信度（shadow 分数，不用于生产门禁）
    4. 筛选 core_members：
       - 注入 membership_store 时：只保留有 grounded support 证据的成员
       - 未注入时：回退到 shadow confidence >= CORE_MEMBER_THRESHOLD（向后兼容）
    5. 计算 theme_id = hash(关键词 + 事件实体 + 核心股票集)

    无本地证据的成员只进入探索区（members 列表），不进入 core_members。
    """

    # core_members shadow confidence 阈值（仅未注入 membership_store 时回退使用）
    CORE_MEMBER_THRESHOLD = 0.6

    def __init__(
        self,
        strategy_version: str = "v1.0",
        membership_store: Any | None = None,
    ) -> None:
        """初始化。

        Args:
            strategy_version: 策略版本号。
            membership_store: 可选的 MembershipEvidenceStore 实例。注入后
                core_members 只包含有 grounded support 证据的成员；未注入时
                回退到 shadow confidence 阈值（向后兼容，不用于生产门禁）。
        """
        self._strategy_version = strategy_version
        self._membership_store = membership_store

    def assemble(
        self,
        theme_name: str,
        theme_keywords: list[str],
        event_entities: str,
        snapshot: SourceSnapshot,
        news_events: list[NewsEvent] | None = None,
        market_resonance_symbols: list[str] | None = None,
        aliases: list[str] | None = None,
    ) -> ThemePacket:
        """装配 ThemePacket。

        Args:
            theme_name: 主题名称。
            theme_keywords: 主题关键词列表（如 ["HBM", "高带宽内存"]）。
            event_entities: 核心事件实体描述（如 "AI PC 需求增长"）。
            snapshot: 数据源快照（含东财/同花顺板块）。
            news_events: 新闻事件列表（可选）。
            market_resonance_symbols: 行情共振股票列表（可选）。
            aliases: 主题别名。

        Returns:
            装配后的 ThemePacket。
        """
        news_events = news_events or []
        market_resonance_symbols = market_resonance_symbols or []
        aliases = aliases or []

        # 1. 从快照中提取与主题相关的板块
        relevant_boards = self._filter_relevant_boards(snapshot.boards, theme_keywords)

        # 2. 从新闻事件中提取共现股票
        news_symbols = self._extract_news_symbols(news_events, theme_keywords)

        # 3. 合并成员，计算置信度（保留 provider→board→member 独立观察和各自 source_as_of）
        members = self._merge_members(
            relevant_boards, news_symbols, market_resonance_symbols,
            snapshot_source_as_of=snapshot.source_as_of or snapshot.trade_date,
        )

        # 4. 筛选 core_members
        #    注入 membership_store 时：只保留有 grounded support 证据的成员（生产门禁）
        #    未注入时：回退到 shadow confidence 阈值（向后兼容，不用于生产门禁）
        if self._membership_store is not None:
            theme_id_for_check = generate_theme_id(theme_name, theme_name, [])
            core_members = [
                m for m in members
                if self._membership_store.is_member_confirmed(m.symbol, theme_id_for_check)
            ]
        else:
            core_members = [
                m for m in members
                if m.membership_confidence >= self.CORE_MEMBER_THRESHOLD
            ]

        # 5. 计算稳定叙事 theme_id。事件标题、成员集合均属于本次 episode，
        # 不参与主题身份，否则同一主题每次新闻/成员变化都会断开版本链。
        theme_id = generate_theme_id(
            theme_name, theme_name, []
        )

        # 6. 生成 catalysts
        catalysts = self._build_catalysts(news_events)

        # 7. 构建 ThemePacket
        now = datetime.now(timezone.utc).isoformat()
        theme_run_id = generate_theme_run_id(theme_id, now)

        packet = ThemePacket(
            theme_run_id=theme_run_id,
            theme_id=theme_id,
            theme_name=theme_name,
            aliases=aliases,
            theme_type="news_persistence",  # 默认类型，由探测器覆盖
            detected_at=now,
            as_of=now,
            expires_at="",  # 由探测器设置
            catalysts=catalysts,
            members=members,
            core_members=core_members,
            negative_risk_score=self._explicit_negative_risk(catalysts),
            source_snapshot_id=snapshot.snapshot_id,
            strategy_version=self._strategy_version,
            trace_id="",  # 由 service 层设置
        )

        # 8. 降级标记
        if not members:
            packet.degraded = True
            packet.degradation_reasons.append("no_members")
        if not core_members:
            packet.degraded = True
            packet.degradation_reasons.append("no_core_members")
        if not snapshot.providers_used:
            packet.degraded = True
            packet.degradation_reasons.append("no_data_sources")

        return packet

    @staticmethod
    def _explicit_negative_risk(catalysts: list[ThemeCatalyst]) -> float:
        """Score only explicit negative language present in source text.

        This is a blocking-risk prefilter, not a factual conclusion.  It never
        manufactures a positive/clear result when no counter search ran.
        """
        text = " ".join(
            f"{item.title} {item.summary}" for item in catalysts
        ).lower()
        if not text:
            return 0.0
        blocking_terms = (
            "立案调查", "行政处罚", "误导投资者", "财务造假", "重大违法",
            "退市", "不存在相关业务", "无相关业务", "终止", "撤回",
            "被罚", "罚单", "误导性陈述",
        )
        warning_terms = (
            "澄清", "风险提示", "减持", "亏损", "否认", "不涉及", "尚未",
            "不确定", "涉嫌", "误导",
        )
        if any(term in text for term in blocking_terms):
            return 0.85
        if any(term in text for term in warning_terms):
            return 0.50
        return 0.0

    def _filter_relevant_boards(
        self, boards: list[ConceptBoard], keywords: list[str]
    ) -> list[ConceptBoard]:
        """筛选与主题关键词相关的板块。"""
        relevant = []
        for board in boards:
            board_name_lower = board.board_name.lower()
            for kw in keywords:
                if kw.lower() in board_name_lower:
                    relevant.append(board)
                    break
        return relevant

    def _extract_news_symbols(
        self, events: list[NewsEvent], keywords: list[str]
    ) -> dict[str, list[str]]:
        """从新闻事件中提取与主题关键词相关的共现股票。

        Returns:
            {symbol: [事件来源]} — 每个股票命中的事件来源列表。
        """
        symbol_sources: dict[str, list[str]] = {}

        for event in events:
            # 检查事件是否与主题相关
            event_keywords = set(event.keywords)
            theme_keywords = set(keywords)
            if not event_keywords & theme_keywords:
                continue

            for symbol in event.co_occurred_symbols:
                if symbol not in symbol_sources:
                    symbol_sources[symbol] = []
                symbol_sources[symbol].extend(event.sources)

        return symbol_sources

    def _merge_members(
        self,
        boards: list[ConceptBoard],
        news_symbols: dict[str, list[str]],
        market_resonance_symbols: list[str],
        *,
        snapshot_source_as_of: str = "",
    ) -> list[ThemeMember]:
        """合并成员，计算每个成员的置信度。

        保留 provider→board→member 的独立观察和各自 source_as_of，
        不只保存成员并集和逗号 provider 字符串。每条 membership_evidence
        记录独立的 (provider, board_name, source_as_of) 三元组。

        Args:
            boards: 相关概念板块列表。
            news_symbols: 新闻共现股票 {symbol: [来源]}。
            market_resonance_symbols: 行情共振股票列表。
            snapshot_source_as_of: 快照的 source_as_of，用于为每条
                board 观察记录可见性时间戳。

        Returns:
            合并后的 ThemeMember 列表（按置信度降序）。
        """
        # 收集每个 symbol 的来源（保留 provider→board 独立观察）
        symbol_sources: dict[str, dict[str, list[str]]] = {}
        # symbol -> provider evidence buckets

        for board in boards:
            provider = board.provider
            for symbol in board.members:
                if symbol not in symbol_sources:
                    symbol_sources[symbol] = {"eastmoney": [], "ths": [], "semantic": [], "local": [], "news": [], "market": []}
                if provider in {"eastmoney", "ths"}:
                    symbol_sources[symbol][provider].append(board.board_name)
                elif provider == "wencai":
                    symbol_sources[symbol]["semantic"].append(board.board_name)
                elif "last_good" in provider or provider.startswith("local"):
                    symbol_sources[symbol]["local"].append(board.board_name)

        for symbol, sources in news_symbols.items():
            if symbol not in symbol_sources:
                symbol_sources[symbol] = {"eastmoney": [], "ths": [], "semantic": [], "local": [], "news": [], "market": []}
            symbol_sources[symbol]["news"] = sources

        for symbol in market_resonance_symbols:
            if symbol not in symbol_sources:
                symbol_sources[symbol] = {"eastmoney": [], "ths": [], "semantic": [], "local": [], "news": [], "market": []}
            symbol_sources[symbol]["market"].append("market_resonance")

        # 构建 ThemeMember 列表
        members: list[ThemeMember] = []
        for symbol, sources in symbol_sources.items():
            membership_sources: list[str] = []
            membership_reasons: list[str] = []

            has_eastmoney = bool(sources["eastmoney"])
            has_ths = bool(sources["ths"])
            has_news = bool(sources["news"])
            has_market = bool(sources["market"])
            has_semantic = bool(sources["semantic"])
            has_local = bool(sources["local"])

            if has_eastmoney:
                membership_sources.append("eastmoney_concept")
                membership_reasons.extend([f"东财板块: {b}" for b in sources["eastmoney"]])
            if has_ths:
                membership_sources.append("ths_concept")
                membership_reasons.extend([f"同花顺板块: {b}" for b in sources["ths"]])
            if has_news:
                membership_sources.append("news_cluster")
                membership_reasons.extend([f"新闻共现: {s}" for s in sources["news"]])
            if has_semantic:
                membership_sources.append("wencai_semantic")
                membership_reasons.extend([f"问财概念: {b}" for b in sources["semantic"]])
            if has_local:
                membership_sources.append("local_last_good")
                membership_reasons.extend([f"历史概念成员: {b}" for b in sources["local"]])
            if has_market:
                membership_sources.append("market_resonance")
                membership_reasons.append("行情共振")

            # 计算置信度
            confidence = self._compute_confidence(
                has_eastmoney, has_ths, has_news, has_market,
                has_semantic=has_semantic, has_local=has_local,
            )

            # 判断 market_role
            market_role = self._determine_market_role(
                confidence, has_eastmoney or has_semantic, has_news
            )

            # 构造 membership_evidence：保留 provider→board→member 独立观察和各自 source_as_of
            # 每条记录包含单一 provider（非逗号字符串）、board_name、source_as_of、fetched_at
            member_evidence: list[dict[str, Any]] = []
            for board in boards:
                if symbol not in board.members:
                    continue
                member_evidence.append({
                    "provider": board.provider,  # 单一 provider，非逗号字符串
                    "theme_name": board.board_name,
                    "board_id": board.board_id,
                    "raw_response_hash": board.raw_response_hash,
                    "fetched_at": board.fetched_at,
                    "source_as_of": snapshot_source_as_of,  # 快照可见性时间戳
                    "reported_member_count": board.reported_member_count,
                    "members_truncated": board.members_truncated,
                })

            members.append(ThemeMember(
                symbol=symbol,
                company_name="",  # 由 Research 填充
                membership_sources=membership_sources,
                membership_reasons=membership_reasons,
                membership_evidence=member_evidence,
                membership_confidence=confidence,
                market_role=market_role,
                first_seen_at=datetime.now(timezone.utc).isoformat(),
            ))

        # 按置信度降序排序
        members.sort(key=lambda m: m.membership_confidence, reverse=True)

        return members

    def _compute_confidence(
        self, has_eastmoney: bool, has_ths: bool, has_news: bool, has_market: bool,
        *, has_semantic: bool = False, has_local: bool = False,
    ) -> float:
        """计算成员置信度。

        规则：
        - 东财 + 新闻：高（0.85）
        - 东财 + 同花顺：中高（0.70）
        - 单一概念：中（0.50）
        - 只有行情共振：低（0.30）
        """
        if has_eastmoney and has_news:
            return 0.85
        elif has_semantic and has_news:
            # Semantic concept membership plus one news event is useful for
            # discovery but cannot establish a Research-validated core role.
            return 0.65
        elif has_semantic and (has_eastmoney or has_ths):
            return 0.80
        elif has_eastmoney and has_ths:
            return 0.70
        elif has_semantic:
            return 0.60
        elif has_eastmoney or has_ths:
            return 0.50
        elif has_local and has_news:
            return 0.55
        elif has_local:
            return 0.35
        elif has_market:
            return 0.30
        else:
            return 0.10

    def _determine_market_role(
        self, confidence: float, has_eastmoney: bool, has_news: bool
    ) -> str:
        """判断市场角色。

        - core: 高置信度 + 东财 + 新闻
        - leader: 高置信度
        - secondary: 中高/中置信度
        - diffusion: 低置信度
        """
        if confidence >= 0.8 and has_eastmoney and has_news:
            return "core"
        elif confidence >= 0.7:
            return "leader"
        elif confidence >= 0.4:
            return "secondary"
        else:
            return "diffusion"

    def _build_catalysts(self, events: list[NewsEvent]) -> list[ThemeCatalyst]:
        """从新闻事件构建催化剂。"""
        catalysts: list[ThemeCatalyst] = []
        for event in events[:5]:  # 最多 5 个催化剂
            catalysts.append(ThemeCatalyst(
                catalyst_id=event.event_id,
                event_type="news",
                title=event.title,
                summary=event.summary,
                source=", ".join(event.sources) if event.sources else "",
                source_url=", ".join(event.source_urls) if event.source_urls else "",
                event_date=event.event_date,
                confidence=1.0 if event.is_independent else 0.5,
            ))
        return catalysts
