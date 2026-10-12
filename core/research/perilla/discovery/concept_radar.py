"""ConceptRadar — 多源概念发现与热度评估.

从 Gateway 题材/板块、本地证据、新闻标题、研报标题及手动信号中
自动发现新概念，计算热度与新颖度评分，去重后输出 ConceptSignal 列表。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .models import (
    CONFIDENCE_LEVELS,
    SOURCE_TYPES,
    ConceptSignal,
    new_id,
    now_iso,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 热度评分权重
_HEAT_SOURCE_WEIGHT: float = 0.4   # 来源数量贡献
_HEAT_OCCURRENCE_WEIGHT: float = 0.6  # 出现频次贡献

# 新颖度评分权重
_NOVELTY_RECENCY_WEIGHT: float = 0.5   # 首次发现时间贡献
_NOVELTY_RARITY_WEIGHT: float = 0.5    # 本地证据稀缺度贡献

# 查询模板
_QUERY_TEMPLATES: List[str] = [
    "{topic} 产业链",
    "{topic} 概念股",
    "{topic} 龙头公司",
    "{topic} 供应商",
    "{topic} 核心技术",
    "{topic} 市场规模",
]

# 复合概念常见分隔词
_COMPOUND_SEPARATORS: List[str] = [
    "及", "与", "和", "/", "、", "+",
]


class ConceptRadar:
    """多源概念发现雷达.

    从 Gateway 题材/板块、本地证据中心、新闻/研报标题以及手动信号文件中
    自动发现概念，计算热度与新颖度，去重排序后输出 ConceptSignal 列表。

    设计原则：
    - 防御性编程：所有外部调用均包裹 try/except，Gateway 不可用时降级为空列表
    - 懒加载：Gateway 仅在需要时导入
    - 幂等安全：同一概念名去重（大小写无关、去除首尾空白）
    """

    def __init__(self, project_root: Optional[str] = None) -> None:
        """初始化 ConceptRadar.

        Args:
            project_root: 项目根目录，用于定位 data/input 等路径。
                          默认取当前文件向上三级目录。
        """
        if project_root is None:
            project_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..")
            )
        self._project_root = project_root

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def auto_discover_concepts(
        self, lookback_days: int = 7
    ) -> List[ConceptSignal]:
        """从多源数据自动发现概念信号.

        数据源优先级：
        1. Gateway 题材/板块快照 (theme_snapshot, sector_snapshot)
        2. 本地证据中心近期证据标题
        3. 近期新闻/研报/公告标题
        4. 问财语义搜索（通用发现）
        5. 手动信号文件 (perilla_manual_signals.json)

        对每个概念计算 heat_score、novelty_score、confidence，
        去重后按 heat_score 降序排列。

        Args:
            lookback_days: 回溯天数，仅统计此时间窗口内的数据。

        Returns:
            去重排序后的 ConceptSignal 列表。
        """
        try:
            lookback_days = max(0, int(lookback_days))
        except (TypeError, ValueError, OverflowError):
            lookback_days = 7
        cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()
        raw_concepts: Dict[str, Dict[str, Any]] = {}

        # --- 数据源 1: Gateway 题材/板块 ---
        gateway_items = [
            item for item in _items(self._load_gateway_themes())
            if isinstance(item, Mapping)
        ]
        for item in gateway_items:
            name = self._normalize_name(item.get("name", ""))
            if not name:
                continue
            self._merge_raw_concept(
                raw_concepts, name=name,
                source="gateway",
                source_type="sector_page" if item.get("source") == "sector" else "concept_page",
                seen_at=now_iso(),
                evidence_id="",
                leading_stock=item.get("leading_stock", ""),
                heat=item.get("heat", 0.0),
            )

        # --- 数据源 2: 本地证据中心 ---
        evidence_items = [
            item for item in _items(self._load_local_evidence(cutoff))
            if isinstance(item, Mapping)
        ]
        for item in evidence_items:
            name = self._normalize_name(item.get("name", ""))
            if not name:
                continue
            self._merge_raw_concept(
                raw_concepts, name=name,
                source="evidence_center",
                source_type=item.get("source_type", "local_evidence"),
                seen_at=item.get("seen_at", now_iso()),
                evidence_id=item.get("evidence_id", ""),
            )

        # --- 数据源 3: 近期新闻/研报/公告标题 ---
        news_items = [
            item for item in _items(self._load_recent_titles(cutoff))
            if isinstance(item, Mapping)
        ]
        for item in news_items:
            name = self._normalize_name(item.get("name", ""))
            if not name:
                continue
            self._merge_raw_concept(
                raw_concepts, name=name,
                source="news_report",
                source_type=item.get("source_type", "news"),
                seen_at=item.get("seen_at", now_iso()),
                evidence_id=item.get("evidence_id", ""),
            )

        # --- 数据源 4: 问财语义搜索（通用发现） ---
        search_items = [
            item for item in _items(self._load_gateway_search(""))
            if isinstance(item, Mapping)
        ]
        for item in search_items:
            name = self._normalize_name(item.get("name", ""))
            if not name:
                continue
            self._merge_raw_concept(
                raw_concepts, name=name,
                source="wencai_search",
                source_type=item.get("source_type", "wencai_search"),
                seen_at=now_iso(),
                evidence_id="",
                leading_stock=item.get("leading_stock", ""),
            )

        # --- 数据源 5: 手动信号 ---
        manual_items = [
            item for item in _items(self._load_manual_signals())
            if isinstance(item, Mapping)
        ]
        for item in manual_items:
            name = self._normalize_name(item.get("name", ""))
            if not name:
                continue
            self._merge_raw_concept(
                raw_concepts, name=name,
                source="manual",
                source_type="manual",
                seen_at=item.get("seen_at", now_iso()),
                evidence_id="",
            )

        # --- 构建 ConceptSignal 列表 ---
        signals: List[ConceptSignal] = []
        for _, info in raw_concepts.items():
            name = _text(info.get("name"))
            if not name:
                continue
            sources = info["sources"]
            source_types = info["source_types"]
            occurrences = info["occurrences"]
            first_seen = info["first_seen"]
            last_seen = info["last_seen"]
            evidence_ids = info["evidence_ids"]
            leading_stocks = info.get("leading_stocks", set())
            gateway_heat = info.get("gateway_heat", 0.0)

            heat = self._compute_heat_score(name, occurrences, list(sources))
            # 若 Gateway 提供了涨跌幅热度，取加权融合
            if gateway_heat > 0:
                heat = 0.6 * heat + 0.4 * min(abs(gateway_heat) / 10.0, 1.0)
            novelty = self._compute_novelty_score(
                first_seen, last_seen, len(evidence_ids)
            )
            confidence = self._infer_confidence(len(sources))

            # 构建 notes：包含领涨股和热度信息
            notes_parts: List[str] = []
            if leading_stocks:
                notes_parts.append(
                    f"领涨股: {', '.join(sorted(leading_stocks))}"
                )
            if gateway_heat > 0:
                notes_parts.append(f"Gateway涨跌幅(%): {gateway_heat:.2f}")

            signals.append(ConceptSignal(
                concept_id=new_id("cs_"),
                name=name,
                aliases=[],
                detected_from=sorted(sources),
                source_types=sorted(source_types),
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                heat_score=heat,
                novelty_score=novelty,
                confidence=confidence,
                raw_evidence_ids=sorted(set(evidence_ids)),
                notes="; ".join(notes_parts),
            ))

        signals.sort(key=lambda s: (-s.heat_score, s.name))
        return signals

    def discover_from_topic(self, topic: str) -> ConceptSignal:
        """从用户提供的主题名生成概念信号，自动扩展别名与搜索查询.

        扩展策略：
        - 使用查询模板生成搜索查询
        - 基于规则的复合概念拆分（按常见分隔词拆分）
        - 查找重叠术语生成别名
        - 通过问财搜索发现关联概念和公司，丰富别名

        Args:
            topic: 用户提供的主题名称。

        Returns:
            包含别名和搜索查询的 ConceptSignal。
        """
        normalized = self._normalize_name(topic)
        aliases: List[str] = []
        search_queries: List[str] = []
        related_companies: List[str] = []

        # 生成搜索查询
        for tpl in _QUERY_TEMPLATES:
            search_queries.append(tpl.format(topic=normalized))

        # 基于规则的复合概念拆分
        for sep in _COMPOUND_SEPARATORS:
            if sep in normalized:
                parts = [p.strip() for p in normalized.split(sep) if p.strip()]
                aliases.extend(parts)
                break

        # 常见缩写/同义词扩展（规则：去除后缀词）
        suffixes_to_strip = ["概念", "板块", "题材", "产业链", "概念股"]
        for suffix in suffixes_to_strip:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                alias = normalized[: -len(suffix)]
                if alias and alias not in aliases:
                    aliases.append(alias)

        # 通过问财搜索发现关联概念和公司
        search_items = [
            item for item in _items(self._load_gateway_search(normalized))
            if isinstance(item, Mapping)
        ]
        for item in search_items:
            item_name = self._normalize_name(item.get("name", ""))
            source_type = _text(item.get("source_type"))
            if source_type == "wencai_search" and item_name:
                # 搜索发现的概念作为别名候选
                if item_name not in aliases and item_name != normalized:
                    aliases.append(item_name)
            if source_type == "wencai_company" and item_name:
                related_companies.append(item_name)

        # 生成需求锚点假设（记录在 notes 中）
        demand_hypotheses = self._generate_demand_anchor_hypotheses(normalized)

        now = now_iso()
        # 构建笔记
        notes_parts = [
            f"search_queries: {search_queries}",
            f"demand_anchor_hypotheses: {demand_hypotheses}",
        ]
        if related_companies:
            notes_parts.append(f"related_companies: {related_companies}")

        return ConceptSignal(
            concept_id=new_id("cs_"),
            name=normalized,
            aliases=aliases,
            detected_from=["manual_topic"],
            source_types=["manual"],
            first_seen_at=now,
            last_seen_at=now,
            heat_score=0.5,
            novelty_score=0.5,
            confidence="low",
            raw_evidence_ids=[],
            notes="; ".join(notes_parts),
        )

    # ------------------------------------------------------------------
    # 评分方法
    # ------------------------------------------------------------------

    def _compute_heat_score(
        self, concept_name: str, occurrences: int, sources: List[str]
    ) -> float:
        """计算概念热度评分.

        评分逻辑：
        - 来源数量越多 → 分数越高（对数缩放）
        - 出现频次越高 → 分数越高（对数缩放）
        - 两项加权求和，上限 1.0

        Args:
            concept_name: 概念名称（用于日志）。
            occurrences: 该概念在所有来源中的出现次数。
            sources: 来源标识列表。

        Returns:
            热度评分 [0.0, 1.0]。
        """
        sources = _items(sources)
        try:
            occurrences = max(0, int(occurrences))
        except (TypeError, ValueError, OverflowError):
            occurrences = 0
        n_sources = len(sources)
        # 对数缩放：1源→0, 2源→0.69, 3源→1.1, 归一化到 ~1.0
        source_score = math.log1p(n_sources) / math.log1p(5)  # 5源时约=1.0
        occurrence_score = math.log1p(occurrences) / math.log1p(20)  # 20次时约=1.0

        raw = (
            _HEAT_SOURCE_WEIGHT * source_score
            + _HEAT_OCCURRENCE_WEIGHT * occurrence_score
        )
        return min(max(raw, 0.0), 1.0)

    def _compute_novelty_score(
        self, first_seen: str, last_seen: str, local_evidence_count: int
    ) -> float:
        """计算概念新颖度评分.

        评分逻辑：
        - 首次发现时间越近 → 新颖度越高
        - 本地证据数量越少 → 新颖度越高

        Args:
            first_seen: 首次发现时间（ISO 格式）。
            last_seen: 最近发现时间（ISO 格式）。
            local_evidence_count: 本地证据条目数量。

        Returns:
            新颖度评分 [0.0, 1.0]。
        """
        # 时间新颖度
        recency_score = 0.5  # 默认值
        try:
            first_dt = datetime.fromisoformat(_text(first_seen))
            now_dt = datetime.now()
            days_ago = max((now_dt - first_dt).days, 0)
            # 指数衰减：0天→1.0, 7天→0.5, 30天→0.1
            recency_score = math.exp(-days_ago / 10.0)
        except (ValueError, TypeError, OverflowError):
            logger.debug("无法解析 first_seen 时间: %s，使用默认新颖度", first_seen)

        # 稀缺度：证据越少越新颖
        try:
            local_evidence_count = max(0, int(local_evidence_count))
        except (TypeError, ValueError, OverflowError):
            local_evidence_count = 0
        rarity_score = 1.0 / (1.0 + local_evidence_count)

        raw = (
            _NOVELTY_RECENCY_WEIGHT * recency_score
            + _NOVELTY_RARITY_WEIGHT * rarity_score
        )
        return min(max(raw, 0.0), 1.0)

    # ------------------------------------------------------------------
    # 数据源加载（防御性：永不崩溃）
    # ------------------------------------------------------------------

    def _load_gateway_themes(self) -> List[dict]:
        """从 ChinaStockDataGateway 获取题材/板块数据.

        懒加载 Gateway，仅在调用时导入。任何异常均降级为空列表。

        字段映射（同花顺页面抓取的实际字段名）：
        - 板块: 板块/题材名称
        - 涨跌幅(%): 热度指标
        - 领涨股: 领涨股票名称

        Returns:
            题材/板块字典列表，每项包含 name, source, leading_stock, heat。
        """
        items: List[dict] = []
        try:
            from data_svc.gateways.china_stock_data_gateway import (
                ChinaStockDataGateway,
            )

            gw = ChinaStockDataGateway()

            # 题材快照
            theme_result = gw.fetch_themes()
            if theme_result.success and theme_result.parsed_items:
                for itm in theme_result.parsed_items:
                    if not isinstance(itm, Mapping):
                        continue
                    name = itm.get("板块", itm.get("name", itm.get("主题", "")))
                    leading_stock = itm.get("领涨股", "")
                    heat = self._safe_float(itm.get("涨跌幅(%)", 0.0))
                    items.append({
                        "name": name,
                        "source": "theme",
                        "leading_stock": leading_stock,
                        "heat": heat,
                    })

            # 板块快照
            sector_result = gw.fetch_sector()
            if sector_result.success and sector_result.parsed_items:
                for itm in sector_result.parsed_items:
                    if not isinstance(itm, Mapping):
                        continue
                    name = itm.get("板块", itm.get("name", itm.get("行业", "")))
                    leading_stock = itm.get("领涨股", "")
                    heat = self._safe_float(itm.get("涨跌幅(%)", 0.0))
                    items.append({
                        "name": name,
                        "source": "sector",
                        "leading_stock": leading_stock,
                        "heat": heat,
                    })

            logger.info(
                "Gateway 加载完成: %d 题材, %d 板块",
                len([i for i in items if i["source"] == "theme"]),
                len([i for i in items if i["source"] == "sector"]),
            )
        except ImportError:
            logger.warning("ChinaStockDataGateway 不可用，跳过 Gateway 数据源")
        except Exception as exc:
            logger.warning("Gateway 调用异常: %s", exc)

        return items

    def _load_local_evidence(self, cutoff_iso: str) -> List[dict]:
        """从本地 EvidenceCenter 加载近期证据中的概念.

        仅提取证据类型为 theme/sector/catalyst/report 的条目，
        取其 title 和 themes 字段作为概念来源。

        Args:
            cutoff_iso: ISO 格式的时间截止线，仅取此时间之后的证据。

        Returns:
            概念字典列表，每项包含 name, source_type, seen_at, evidence_id。
        """
        items: List[dict] = []
        try:
            from core.research.evidence.evidence_center import EvidenceCenter

            ec = EvidenceCenter()
            # EvidenceCenter 默认为空实例；尝试从最近导出文件加载
            latest_path = Path(self._project_root) / "data" / "reports" / "evidence_center_latest.json"
            if latest_path.is_file():
                with open(latest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                evidences = data.get("evidences", []) if isinstance(data, Mapping) else []
                for ev in _items(evidences):
                    if not isinstance(ev, Mapping):
                        continue
                    ev_type = ev.get("evidence_type", "")
                    if ev_type not in ("theme", "sector", "catalyst", "report"):
                        continue
                    seen_at = ev.get("created_at", ev.get("data_date", ""))
                    if seen_at and seen_at < cutoff_iso:
                        continue
                    # 从 themes 字段提取概念名
                    for theme_name in _items(ev.get("themes")):
                        theme_name = self._normalize_name(theme_name)
                        if not theme_name:
                            continue
                        items.append({
                            "name": theme_name,
                            "source_type": ev_type,
                            "seen_at": seen_at or now_iso(),
                            "evidence_id": ev.get("evidence_id", ""),
                        })
                    # 从 title 提取（若 themes 为空）
                    if not ev.get("themes") and ev.get("title"):
                        items.append({
                            "name": ev["title"],
                            "source_type": ev_type,
                            "seen_at": seen_at or now_iso(),
                            "evidence_id": ev.get("evidence_id", ""),
                        })
                logger.info("本地证据加载完成: %d 条概念", len(items))
            else:
                logger.debug("未找到证据中心导出文件: %s", latest_path)
        except ImportError:
            logger.warning("EvidenceCenter 不可用，跳过本地证据数据源")
        except Exception as exc:
            logger.warning("本地证据加载异常: %s", exc)

        return items

    def _load_recent_titles(self, cutoff_iso: str, topic: str = "") -> List[dict]:
        """从近期新闻/研报标题中提取概念关键词.

        扫描 Gateway 报告和公告数据，以及本地存储的新闻标题文件。

        字段映射（Gateway 实际返回字段名）：
        - 研报: 研报内容(标题), 股票名称, 机构名称, 报告日期, PDF链接, 股票代码
        - 公告: title, name, date, code, pdf_url

        Args:
            cutoff_iso: ISO 格式的时间截止线。
            topic: 可选主题关键词，用于筛选相关标的的研报/公告。

        Returns:
            概念字典列表。
        """
        items: List[dict] = []

        # 尝试从 Gateway 获取近期报告/公告
        try:
            from data_svc.gateways.china_stock_data_gateway import (
                ChinaStockDataGateway,
            )

            gw = ChinaStockDataGateway()
            report_symbols = self._resolve_report_symbols(topic)

            # 研报
            for symbol in report_symbols:
                result = gw.fetch_reports(symbol, limit=5)
                if result.success and result.parsed_items:
                    for itm in result.parsed_items:
                        if not isinstance(itm, Mapping):
                            continue
                        title = itm.get("研报内容", itm.get("title", itm.get("报告标题", "")))
                        company = itm.get("股票名称", itm.get("name", ""))
                        if title:
                            items.append({
                                "name": title,
                                "source_type": "report",
                                "seen_at": now_iso(),
                                "evidence_id": "",
                                "company": company,
                            })

            # 公告
            for symbol in report_symbols:
                result = gw.fetch_announcements(symbol, limit=5)
                if result.success and result.parsed_items:
                    for itm in result.parsed_items:
                        if not isinstance(itm, Mapping):
                            continue
                        title = itm.get("title", itm.get("公告标题", ""))
                        company = itm.get("name", itm.get("股票名称", ""))
                        if title:
                            items.append({
                                "name": title,
                                "source_type": "announcement",
                                "seen_at": now_iso(),
                                "evidence_id": "",
                                "company": company,
                            })
        except Exception as exc:
            logger.debug("Gateway 报告/公告加载异常: %s", exc)

        # 尝试从本地新闻标题文件加载
        news_path = Path(self._project_root) / "data" / "input" / "recent_news_titles.json"
        if news_path.is_file():
            try:
                with open(news_path, "r", encoding="utf-8") as f:
                    news_data = json.load(f)
                for entry in news_data if isinstance(news_data, list) else []:
                    if not isinstance(entry, Mapping):
                        continue
                    title = entry.get("title", "")
                    seen_at = _text(entry.get("published_at", entry.get("date", "")))
                    if title and (not seen_at or seen_at >= cutoff_iso):
                        items.append({
                            "name": title,
                            "source_type": "news",
                            "seen_at": seen_at or now_iso(),
                            "evidence_id": "",
                        })
            except Exception as exc:
                logger.debug("本地新闻标题加载异常: %s", exc)

        return items

    def _load_gateway_search(self, topic: str) -> List[dict]:
        """通过问财语义搜索发现概念与公司.

        尝试直接调用 vendor 模块的 wencai_search 函数。
        任何异常均降级为空列表，永不崩溃。

        Args:
            topic: 搜索关键词，空字符串时返回空列表。

        Returns:
            概念/公司字典列表，每项包含 name, source_type, leading_stock, symbol。
        """
        items: List[dict] = []
        if not topic:
            return items
        try:
            from vendor.china_stock_data.scripts.china_stock import wencai_search

            result = wencai_search(topic)
            if not isinstance(result, dict):
                return items
            # wencai_search 返回错误时跳过
            if "error" in result:
                logger.debug("问财搜索返回错误: %s", result.get("error", ""))
                return items
            for row in result.get("results", []):
                if not isinstance(row, Mapping):
                    continue
                # 提取概念名称和公司名称
                concept_name = row.get("概念", row.get("板块", ""))
                company_name = row.get("股票名称", row.get("name", ""))
                symbol = row.get("股票代码", row.get("code", ""))
                if concept_name:
                    items.append({
                        "name": concept_name,
                        "source_type": "wencai_search",
                        "leading_stock": company_name,
                        "symbol": symbol,
                    })
                if company_name and company_name != concept_name:
                    items.append({
                        "name": company_name,
                        "source_type": "wencai_company",
                        "symbol": symbol,
                    })
            logger.info("问财搜索完成: topic=%s, %d 条结果", topic, len(items))
        except ImportError:
            logger.debug("wencai_search 不可用，跳过问财搜索")
        except Exception as exc:
            logger.debug("问财搜索异常: %s", exc)

        return items

    def _load_manual_signals(
        self, path: str = "data/input/perilla_manual_signals.json"
    ) -> List[dict]:
        """从手动信号 JSON 文件加载概念.

        文件格式示例::

            [
              {"name": "固态电池", "seen_at": "2025-06-01T10:00:00"},
              {"name": "钙钛矿", "seen_at": "2025-06-02T14:30:00"}
            ]

        Args:
            path: 相对于项目根目录的文件路径。

        Returns:
            概念字典列表。文件不存在时返回空列表。
        """
        full_path = Path(self._project_root) / path
        if not full_path.is_file():
            logger.debug("手动信号文件不存在: %s", full_path)
            return []

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning("手动信号文件格式错误: 期望列表，得到 %s", type(data).__name__)
                return []
            logger.info("手动信号加载完成: %d 条", len(data))
            return [item for item in data if isinstance(item, Mapping)]
        except json.JSONDecodeError as exc:
            logger.warning("手动信号文件 JSON 解析失败: %s", exc)
            return []
        except Exception as exc:
            logger.warning("手动信号文件加载异常: %s", exc)
            return []

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_name(name: str) -> str:
        """标准化概念名称：去除首尾空白、转小写用于去重比较.

        Args:
            name: 原始概念名称。

        Returns:
            标准化后的名称。
        """
        return _text(name).strip()

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        """安全转换为浮点数，处理字符串中的百分号等非标准格式.

        Args:
            value: 待转换的值，可能是 float/int/str。
            default: 转换失败时的默认值。

        Returns:
            转换后的浮点数。
        """
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                return default
            return parsed if math.isfinite(parsed) else default
        if isinstance(value, str):
            # 去除百分号和空白
            cleaned = value.replace("%", "").replace("％", "").strip()
            try:
                parsed = float(cleaned)
                return parsed if math.isfinite(parsed) else default
            except (ValueError, TypeError, OverflowError):
                return default
        return default

    def _resolve_report_symbols(self, topic: str) -> List[str]:
        """根据主题解析用于查询研报/公告的标的列表.

        无主题时返回默认热门标的；有主题时尝试从 Gateway 题材中
        匹配领涨股对应的标的代码。

        Args:
            topic: 主题关键词，空字符串时使用默认标的。

        Returns:
            标的代码列表。
        """
        default_symbols = ["000001", "600519"]

        if not topic:
            return default_symbols

        # 尝试从已加载的 Gateway 题材中查找匹配的领涨股代码
        try:
            from data_svc.gateways.china_stock_data_gateway import (
                ChinaStockDataGateway,
            )

            gw = ChinaStockDataGateway()
            symbols: List[str] = []

            # 在题材快照中搜索匹配主题的条目
            theme_result = gw.fetch_themes()
            if theme_result.success and theme_result.parsed_items:
                for itm in theme_result.parsed_items:
                    if not isinstance(itm, Mapping):
                        continue
                    name = itm.get("板块", itm.get("name", ""))
                    name = _text(name)
                    if topic in name or name in topic:
                        code = itm.get("领涨股代码", "")
                        if code:
                            symbols.append(code)

            # 在板块快照中搜索匹配主题的条目
            sector_result = gw.fetch_sector()
            if sector_result.success and sector_result.parsed_items:
                for itm in sector_result.parsed_items:
                    if not isinstance(itm, Mapping):
                        continue
                    name = itm.get("板块", itm.get("name", ""))
                    name = _text(name)
                    if topic in name or name in topic:
                        code = itm.get("领涨股代码", "")
                        if code:
                            symbols.append(code)

            if symbols:
                # 去重，保留前 3 个
                seen: Set[str] = set()
                unique: List[str] = []
                for s in symbols:
                    if s not in seen:
                        seen.add(s)
                        unique.append(s)
                return unique[:3]
        except Exception as exc:
            logger.debug("解析报告标的异常: %s，使用默认标的", exc)

        return default_symbols

    @staticmethod
    def _dedup_key(name: str) -> str:
        """生成去重键：小写 + 去除空白.

        Args:
            name: 标准化后的概念名称。

        Returns:
            用于去重比较的键。
        """
        return re.sub(r"\s+", "", _text(name).lower())

    @staticmethod
    def _infer_confidence(n_sources: int) -> str:
        """根据来源数量推断置信度.

        Args:
            n_sources: 不重复的来源数量。

        Returns:
            置信度字符串 (high / medium / low)。
        """
        try:
            n_sources = max(0, int(n_sources))
        except (TypeError, ValueError, OverflowError):
            n_sources = 0
        if n_sources >= 3:
            return "high"
        if n_sources >= 2:
            return "medium"
        return "low"

    def _merge_raw_concept(
        self,
        raw: Dict[str, Dict[str, Any]],
        name: str,
        source: str,
        source_type: str,
        seen_at: str,
        evidence_id: str,
        leading_stock: str = "",
        heat: float = 0.0,
    ) -> None:
        """将一条原始概念合并到聚合字典中.

        同名概念（按 _dedup_key 判断）合并来源、更新时间窗口、
        累加出现次数。同时保留领涨股和热度元数据。

        Args:
            raw: 聚合字典 {dedup_key: info_dict}。
            name: 概念名称。
            source: 来源标识。
            source_type: 来源类型。
            seen_at: 发现时间。
            evidence_id: 证据 ID。
            leading_stock: 领涨股名称（可选）。
            heat: 热度值（可选，来自涨跌幅）。
        """
        if not isinstance(raw, dict):
            return
        name = self._normalize_name(name)
        if not name:
            return
        source = _text(source) or "unknown"
        source_type = _text(source_type) or "local_evidence"
        seen_at = _text(seen_at)
        evidence_id = _text(evidence_id)
        leading_stock = _text(leading_stock)
        heat = self._safe_float(heat)
        key = self._dedup_key(name)
        if key not in raw:
            raw[key] = {
                "name": name,
                "sources": set(),
                "source_types": set(),
                "occurrences": 0,
                "first_seen": seen_at,
                "last_seen": seen_at,
                "evidence_ids": [],
                "leading_stocks": set(),
                "gateway_heat": 0.0,
            }
        info = raw[key]
        info["sources"].add(source)
        info["source_types"].add(source_type)
        info["occurrences"] += 1
        if evidence_id:
            info["evidence_ids"].append(evidence_id)
        # 保留领涨股信息
        if leading_stock:
            info["leading_stocks"].add(leading_stock)
        # 保留最大热度值
        if heat > self._safe_float(info.get("gateway_heat")):
            info["gateway_heat"] = heat
        # 更新时间窗口
        if seen_at:
            if not info["first_seen"] or seen_at < info["first_seen"]:
                info["first_seen"] = seen_at
            if not info["last_seen"] or seen_at > info["last_seen"]:
                info["last_seen"] = seen_at

    @staticmethod
    def _generate_demand_anchor_hypotheses(topic: str) -> List[str]:
        """基于主题名生成需求锚点假设.

        使用规则模板，为常见概念类型生成可能的需求锚点方向。

        Args:
            topic: 标准化后的主题名称。

        Returns:
            需求锚点假设字符串列表。
        """
        hypotheses: List[str] = []

        # 通用假设
        hypotheses.append(f"{topic} 终端产品需求增长")
        hypotheses.append(f"{topic} 政策催化")

        # 根据后缀词推断锚点类型
        if topic.endswith("电池") or topic.endswith("芯片") or topic.endswith("材料"):
            hypotheses.append(f"{topic} 产能瓶颈")
            hypotheses.append(f"{topic} 供应商集中度")

        if topic.endswith("技术") or topic.endswith("工艺"):
            hypotheses.append(f"{topic} 技术路线切换成本")

        if any(kw in topic for kw in ["新能源", "光伏", "风电", "储能"]):
            hypotheses.append(f"{topic} 装机量驱动")

        if any(kw in topic for kw in ["AI", "人工智能", "算力"]):
            hypotheses.append(f"{topic} 算力需求驱动")

        return hypotheses
