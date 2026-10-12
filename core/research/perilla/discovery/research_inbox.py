"""v1.26 Research Inbox + Discovery Watchlist.

把每次紫苏叶发现的结果沉淀成持续追踪的研究工作台：
- DiscoveryItem → ResearchInboxItem（收件箱）
- EvidenceChangeLog（证据变化追踪）
- Research Priority Score（研究优先级）
- Next Verification Queries（下一步验证搜索）
- Daily Discovery Brief（每日简报）
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    EvidenceChangeLog,
    ResearchInboxItem,
    DailyDiscoveryBrief,
    new_id,
    now_iso,
)

logger = logging.getLogger(__name__)

# 证据等级排序（从低到高）
_EVIDENCE_ORDER = [
    "INSUFFICIENT_EVIDENCE",
    "CONCEPT_CLAIM_ONLY",
    "RUMOR_ONLY",
    "EARLY_SIGNAL",
    "PROBABLE_SUPPLIER",
    "CONFIRMED_SUPPLIER",
]

_EVIDENCE_RANK = {level: idx for idx, level in enumerate(_EVIDENCE_ORDER)}


def _decode_model(model_type: Any, payload: Any) -> Any | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        return model_type(**dict(payload))
    except (TypeError, ValueError):
        return None


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temp_path = Path(raw_temp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


class ResearchInbox:
    """研究收件箱 —— 持续追踪紫苏叶候选的研究工作台.

    核心功能：
    1. 每次发现结果入箱（新增/更新）
    2. 证据等级变化检测（Delta）
    3. 研究优先级计算
    4. 下一步验证查询生成
    5. 每日简报生成
    6. 持久化存储（JSON）
    """

    def __init__(self, storage_path: str = "data/research_inbox") -> None:
        """初始化研究收件箱.

        Args:
            storage_path: 存储目录路径。
        """
        self._storage_path = storage_path
        self._items: Dict[str, ResearchInboxItem] = {}  # key = "company|node|concept"
        self._changelog: List[EvidenceChangeLog] = []
        self._load()

    # ── 入箱 ──────────────────────────────────────────────

    def ingest_candidates(
        self,
        candidates: List[Any],  # List[PerillaLeafCandidate]
        topic: str,
        run_id: str,
    ) -> Tuple[int, int, List[EvidenceChangeLog]]:
        """将一次发现的候选入箱.

        Args:
            candidates: PerillaLeafCandidate 列表。
            topic: 主题。
            run_id: 运行 ID。

        Returns:
            (新增数, 更新数, 变更日志列表)。
        """
        new_count = 0
        updated_count = 0
        changes = []

        for c in candidates:
            if not c.company_name or c.company_name.startswith("待验证"):
                continue

            key = f"{c.company_name}|{c.perilla_node}|{topic}"
            now = now_iso()

            if key in self._items:
                # 更新已有条目
                item = self._items[key]
                change = self._detect_change(item, c, topic, run_id, now)
                if change and change.change_type != "NO_CHANGE":
                    changes.append(change)
                    self._changelog.append(change)
                    item.evidence_history.append(change.change_id)

                # 更新字段
                item.evidence_level = c.evidence_level
                item.classification = c.company_classification
                item.total_score = c.total_score
                item.bottleneck_logic = c.bottleneck_logic
                item.red_team_risks = c.red_team_risks
                item.risk_level = c.risk_level
                item.last_updated_at = now
                item.last_run_id = run_id
                if c.evidence_links:
                    item.evidence_links = c.evidence_links

                # 重新计算优先级
                self._compute_priority(item)

                # 更新收件箱状态
                self._update_inbox_status(item)

                # 生成下一步验证查询
                item.next_verification_queries = self._generate_verification_queries(item)

                updated_count += 1
            else:
                # 新增条目
                item = ResearchInboxItem(
                    item_id=new_id("ri_"),
                    company_name=c.company_name,
                    symbol=c.symbol,
                    market=c.market,
                    chain_node=c.perilla_node,
                    concept=topic,
                    main_dish=c.main_dish,
                    evidence_level=c.evidence_level,
                    classification=c.company_classification,
                    total_score=c.total_score,
                    bottleneck_logic=c.bottleneck_logic,
                    inbox_status="NEW",
                    research_priority_score=0.0,
                    research_priority_level="WATCH_ONLY",
                    evidence_links=c.evidence_links or [],
                    red_team_risks=c.red_team_risks or [],
                    risk_level=c.risk_level,
                    first_seen_at=now,
                    last_updated_at=now,
                    last_run_id=run_id,
                    evidence_history=[],
                    next_verification_queries=[],
                    notes="",
                )
                self._compute_priority(item)
                self._update_inbox_status(item)
                item.next_verification_queries = self._generate_verification_queries(item)
                self._items[key] = item
                new_count += 1

        self._save()
        return new_count, updated_count, changes

    # ── 证据变化检测 ──────────────────────────────────────

    def _detect_change(
        self,
        old_item: ResearchInboxItem,
        new_candidate: Any,
        topic: str,
        run_id: str,
        now: str,
    ) -> EvidenceChangeLog:
        """检测证据等级变化.

        Args:
            old_item: 已有条目。
            new_candidate: 新的候选数据。
            topic: 主题。
            run_id: 运行 ID。
            now: 当前时间。

        Returns:
            变更日志。
        """
        old_level = old_item.evidence_level
        new_level = new_candidate.evidence_level
        old_class = old_item.classification
        new_class = new_candidate.company_classification

        old_rank = _EVIDENCE_RANK.get(old_level, 0)
        new_rank = _EVIDENCE_RANK.get(new_level, 0)

        if new_rank > old_rank:
            change_type = "UPGRADE"
            reason = f"证据升级: {old_level} → {new_level}"
        elif new_rank < old_rank:
            change_type = "DOWNGRADE"
            reason = f"证据降级: {old_level} → {new_level}"
        elif old_class != new_class:
            change_type = "CLASSIFICATION_CHANGE"
            reason = f"分类变更: {old_class} → {new_class}"
        elif new_candidate.evidence_links and len(new_candidate.evidence_links) > len(old_item.evidence_links):
            change_type = "NEW_EVIDENCE"
            reason = f"新增证据链接: {len(new_candidate.evidence_links) - len(old_item.evidence_links)} 条"
        else:
            change_type = "NO_CHANGE"
            reason = ""

        return EvidenceChangeLog(
            change_id=new_id("ec_"),
            item_id=old_item.item_id,
            company_name=old_item.company_name,
            symbol=old_item.symbol,
            chain_node=old_item.chain_node,
            change_type=change_type,
            old_evidence_level=old_level,
            new_evidence_level=new_level,
            old_classification=old_class,
            new_classification=new_class,
            reason=reason,
            detected_at=now,
            run_id=run_id,
        )

    # ── 研究优先级 ────────────────────────────────────────

    def _compute_priority(self, item: ResearchInboxItem) -> None:
        """计算研究优先级分数和等级.

        公式：
        research_priority_score =
            perilla_fit_score (六问总分/12 * 25)
            + evidence_upgrade_score (证据等级 * 5)
            + novelty_score (新发现 +10)
            + bottleneck_score (紫苏叶分类 +15)
            + recency_score (24h内 +5)
            - crowding_risk (高风险 -10)
            - evidence_weakness_penalty (证据不足 -15)

        Args:
            item: 收件箱条目。
        """
        score = 0.0

        # perilla_fit_score: 六问总分权重
        score += (item.total_score / 12) * 25

        # evidence_upgrade_score: 证据等级权重
        evidence_rank = _EVIDENCE_RANK.get(item.evidence_level, 0)
        score += evidence_rank * 5

        # novelty_score: 新发现加分
        if item.inbox_status == "NEW":
            score += 10

        # bottleneck_score: 紫苏叶分类加分
        if item.classification == "紫苏叶":
            score += 15
        elif item.classification == "早期信号":
            score += 8

        # recency_score: 最近更新加分
        if item.last_updated_at:
            try:
                updated = datetime.fromisoformat(item.last_updated_at)
                hours_ago = (datetime.now() - updated).total_seconds() / 3600
                if hours_ago < 24:
                    score += 5
            except (ValueError, TypeError):
                pass

        # crowding_risk: 高风险减分
        if item.risk_level == "high":
            score -= 10
        elif item.risk_level == "medium":
            score -= 3

        # evidence_weakness_penalty: 证据不足减分
        if item.evidence_level in ("INSUFFICIENT_EVIDENCE", "CONCEPT_CLAIM_ONLY"):
            score -= 15
        elif item.evidence_level == "RUMOR_ONLY":
            score -= 8

        # 证据升级历史加分
        upgrade_count = sum(
            1 for ch_id in item.evidence_history
            if any(c.change_id == ch_id and c.change_type == "UPGRADE"
                   for c in self._changelog)
        )
        score += upgrade_count * 5

        item.research_priority_score = round(score, 1)

        # 确定优先级等级
        if score >= 40:
            item.research_priority_level = "HIGH_PRIORITY_RESEARCH"
        elif score >= 25:
            item.research_priority_level = "MEDIUM_PRIORITY_RESEARCH"
        elif score >= 10:
            item.research_priority_level = "LOW_PRIORITY_RESEARCH"
        else:
            item.research_priority_level = "WATCH_ONLY"

    # ── 收件箱状态更新 ────────────────────────────────────

    def _update_inbox_status(self, item: ResearchInboxItem) -> None:
        """根据分类和优先级更新收件箱状态.

        Args:
            item: 收件箱条目。
        """
        if item.classification == "概念蹭热":
            item.inbox_status = "DISMISSED"
        elif item.research_priority_level == "HIGH_PRIORITY_RESEARCH":
            if item.evidence_level in ("CONFIRMED_SUPPLIER", "PROBABLE_SUPPLIER"):
                item.inbox_status = "DEEP_RESEARCH_READY"
            else:
                item.inbox_status = "REVIEWING"
        elif item.research_priority_level in ("MEDIUM_PRIORITY_RESEARCH", "LOW_PRIORITY_RESEARCH"):
            item.inbox_status = "WATCHING"
        # 其他保持原状态

    # ── 下一步验证查询 ────────────────────────────────────

    @staticmethod
    def _generate_verification_queries(item: ResearchInboxItem) -> List[str]:
        """自动生成下一步搜索验证查询.

        Args:
            item: 收件箱条目。

        Returns:
            搜索查询列表。
        """
        queries = []
        cn = item.company_name
        node = item.chain_node
        symbol = item.symbol

        # 基础供货验证
        queries.append(f"{cn} {node} 供应商")
        queries.append(f"{cn} {node} 供货 认证")

        # 客户验证
        queries.append(f"{cn} {node} 客户 订单")

        # 如果是 A 股，加公告搜索
        if item.market == "A_SHARE" and symbol:
            queries.append(f"{cn} {symbol} 公告 {node}")

        # 如果是紫苏叶候选，加瓶颈验证
        if item.classification == "紫苏叶":
            queries.append(f"{cn} {node} 产能 扩产")
            queries.append(f"{cn} {node} 竞争对手 替代")

        # 如果证据不足，加更多搜索
        if item.evidence_level in ("INSUFFICIENT_EVIDENCE", "CONCEPT_CLAIM_ONLY"):
            queries.append(f"{cn} {node} 研报")
            queries.append(f"{cn} {node} 展会 招聘")

        return queries[:6]  # 最多 6 条

    # ── 每日简报 ──────────────────────────────────────────

    def generate_daily_brief(self, date: Optional[str] = None) -> DailyDiscoveryBrief:
        """生成每日发现简报.

        Args:
            date: 日期（YYYY-MM-DD），默认今天。

        Returns:
            每日简报。
        """
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        today_changes = [
            c for c in self._changelog
            if c.detected_at.startswith(date)
        ]

        # 今日新增
        new_items = [
            item for item in self._items.values()
            if item.first_seen_at.startswith(date)
        ]

        # 证据升级
        upgraded_ids = {c.item_id for c in today_changes if c.change_type == "UPGRADE"}
        upgraded_items = [
            self._items[k] for k, item in self._items.items()
            if item.item_id in upgraded_ids
        ]

        # 证据降级
        downgraded_ids = {c.item_id for c in today_changes if c.change_type == "DOWNGRADE"}
        downgraded_items = [
            self._items[k] for k, item in self._items.items()
            if item.item_id in downgraded_ids
        ]

        # 高优先级观察池
        high_priority = [
            item for item in self._items.values()
            if item.research_priority_level in ("HIGH_PRIORITY_RESEARCH", "MEDIUM_PRIORITY_RESEARCH")
            and item.inbox_status not in ("DISMISSED", "STALE")
        ]
        high_priority.sort(key=lambda x: -x.research_priority_score)

        # 需要人工确认
        needs_confirmation = [
            item for item in self._items.values()
            if item.inbox_status in ("NEW", "REVIEWING")
            and item.classification not in ("概念蹭热",)
        ]

        return DailyDiscoveryBrief(
            brief_id=new_id("db_"),
            date=date,
            new_items=new_items,
            upgraded_items=upgraded_items,
            downgraded_items=downgraded_items,
            high_priority_watch=high_priority[:10],
            needs_human_confirmation=needs_confirmation[:10],
            total_inbox=len(self._items),
            total_watching=sum(
                1 for i in self._items.values()
                if i.inbox_status in ("WATCHING", "REVIEWING", "DEEP_RESEARCH_READY")
            ),
            generated_at=now_iso(),
        )

    # ── 查询 ──────────────────────────────────────────────

    def get_items(
        self,
        status: Optional[str] = None,
        classification: Optional[str] = None,
        priority_level: Optional[str] = None,
        concept: Optional[str] = None,
        sort_by: str = "priority",
    ) -> List[ResearchInboxItem]:
        """查询收件箱条目.

        Args:
            status: 按状态过滤。
            classification: 按分类过滤。
            priority_level: 按优先级过滤。
            concept: 按概念/主题过滤。
            sort_by: 排序方式（priority/updated/score）。

        Returns:
            条目列表。
        """
        items = list(self._items.values())

        if status:
            items = [i for i in items if i.inbox_status == status]
        if classification:
            items = [i for i in items if i.classification == classification]
        if priority_level:
            items = [i for i in items if i.research_priority_level == priority_level]
        if concept:
            items = [i for i in items if i.concept == concept]

        if sort_by == "priority":
            items.sort(key=lambda x: -x.research_priority_score)
        elif sort_by == "updated":
            items.sort(key=lambda x: x.last_updated_at, reverse=True)
        elif sort_by == "score":
            items.sort(key=lambda x: -x.total_score)

        return items

    def get_changelog(
        self,
        company_name: Optional[str] = None,
        change_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[EvidenceChangeLog]:
        """查询变更日志.

        Args:
            company_name: 按公司名过滤。
            change_type: 按变更类型过滤。
            limit: 返回条数限制。

        Returns:
            变更日志列表。
        """
        logs = list(self._changelog)
        if company_name:
            logs = [l for l in logs if l.company_name == company_name]
        if change_type:
            logs = [l for l in logs if l.change_type == change_type]
        logs.sort(key=lambda x: x.detected_at, reverse=True)
        return logs[:limit]

    # ── 持久化 ────────────────────────────────────────────

    def _save(self) -> None:
        """保存到 JSON 文件."""
        storage_path = Path(self._storage_path)
        _write_json_atomic(
            storage_path / "inbox_items.json",
            {k: asdict(v) for k, v in self._items.items()},
        )
        _write_json_atomic(
            storage_path / "changelog.json",
            [asdict(c) for c in self._changelog],
        )

    def _load(self) -> None:
        """从 JSON 文件加载."""
        items_path = Path(self._storage_path) / "inbox_items.json"
        if items_path.exists():
            try:
                with items_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, Mapping):
                    for k, v in data.items():
                        item = _decode_model(ResearchInboxItem, v)
                        if item is not None:
                            self._items[str(k)] = item
            except Exception as e:
                logger.warning("加载收件箱失败: %s", e)

        changelog_path = Path(self._storage_path) / "changelog.json"
        if changelog_path.exists():
            try:
                with changelog_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, (list, tuple)):
                    self._changelog = [
                        change for payload in data
                        if (change := _decode_model(EvidenceChangeLog, payload)) is not None
                    ]
            except Exception as e:
                logger.warning("加载变更日志失败: %s", e)


def render_daily_brief_md(brief: DailyDiscoveryBrief) -> str:
    """将每日简报渲染为 Markdown.

    Args:
        brief: 每日简报。

    Returns:
        Markdown 字符串。
    """
    lines = [
        f"# 研究工作台日报: {brief.date}",
        "",
        f"**收件箱总数**: {brief.total_inbox} | **观察池**: {brief.total_watching}",
        "",
    ]

    # 今日新增
    if brief.new_items:
        lines.append("## 今日新增线索")
        lines.append("")
        lines.append("| 公司 | 环节 | 分类 | 证据等级 | 优先级 |")
        lines.append("|---|---|---|---|---|")
        for item in brief.new_items:
            lines.append(
                f"| {item.company_name}({item.symbol}) | {item.chain_node} | "
                f"{item.classification} | {item.evidence_level} | "
                f"{item.research_priority_level} |"
            )
        lines.append("")

    # 证据升级
    if brief.upgraded_items:
        lines.append("## 证据升级")
        lines.append("")
        for item in brief.upgraded_items:
            # 找最近的升级记录
            upgrades = [
                c for c in item.evidence_history
            ]
            lines.append(
                f"- **{item.company_name}** ({item.chain_node}): "
                f"证据升级 → {item.evidence_level} | 优先级: {item.research_priority_level}"
            )
        lines.append("")

    # 证据降级
    if brief.downgraded_items:
        lines.append("## 证据降级")
        lines.append("")
        for item in brief.downgraded_items:
            lines.append(
                f"- **{item.company_name}** ({item.chain_node}): "
                f"证据降级 → {item.evidence_level}"
            )
        lines.append("")

    # 高优先级观察池
    if brief.high_priority_watch:
        lines.append("## 高优先级观察池")
        lines.append("")
        lines.append("| 公司 | 环节 | 分类 | 优先级分 | 下一步验证 |")
        lines.append("|---|---|---|---|---|")
        for item in brief.high_priority_watch:
            next_queries = "；".join(item.next_verification_queries[:2]) if item.next_verification_queries else "—"
            lines.append(
                f"| {item.company_name}({item.symbol}) | {item.chain_node} | "
                f"{item.classification} | {item.research_priority_score} | {next_queries} |"
            )
        lines.append("")

    # 需要人工确认
    if brief.needs_human_confirmation:
        lines.append("## 需要人工确认")
        lines.append("")
        for item in brief.needs_human_confirmation:
            lines.append(
                f"- **{item.company_name}** ({item.chain_node}): "
                f"{item.evidence_level} | {item.classification} | "
                f"验证: {'；'.join(item.next_verification_queries[:2])}"
            )
        lines.append("")

    lines.append("---")
    lines.append("*本简报由 Research Inbox 自动生成，仅供研究参考，不构成投资建议。*")

    return "\n".join(lines)
