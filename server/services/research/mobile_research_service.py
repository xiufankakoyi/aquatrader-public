"""
移动端研究数据服务
==================

为移动客户端提供只读的研究数据访问能力，包括：
1. 摘要总览（收件箱统计、观察池统计、任务统计）
2. 日报阅读（daily_brief、紫苏发现报告）
3. 收件箱浏览（按状态/分类过滤、分页）
4. 观察池浏览（按分类/优先级过滤、分页）
5. 任务查询（委托 task_service）
6. 报告浏览与阅读（白名单目录、路径穿越防护）
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from config.logger import get_logger

# 延迟导入 task_service（模块可能不存在）
try:
    from server.services.task_service import get_task_service
except ImportError:
    get_task_service = None

logger = get_logger(__name__)

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_REPORTS_DIR = _DATA_DIR / "reports"
_INBOX_FILE = _DATA_DIR / "research_inbox" / "inbox_items.json"

# Perilla Discovery 结构化数据目录
_PERILLA_DISCOVERY_DIR = _REPORTS_DIR / "perilla_discovery"

# P0 结构化板块文件名
_RAW_TERMS_FILE = _PERILLA_DISCOVERY_DIR / "raw_terms_latest.json"
_CANDIDATE_CONCEPTS_FILE = _PERILLA_DISCOVERY_DIR / "candidate_concepts_latest.json"
_EVIDENCE_EDGES_FILE = _PERILLA_DISCOVERY_DIR / "evidence_edges_latest.json"

# P8 结构化板块文件名
_CAUSAL_CHAINS_FILE = _PERILLA_DISCOVERY_DIR / "causal_chains_latest.json"
_MISSING_EDGES_FILE = _PERILLA_DISCOVERY_DIR / "missing_edges_latest.json"
_SEARCH_TASKS_FILE = _PERILLA_DISCOVERY_DIR / "search_tasks_latest.json"
_RESEARCH_HYPOTHESES_FILE = _PERILLA_DISCOVERY_DIR / "research_hypotheses_latest.json"
_ADVERSARIAL_REVIEWS_FILE = _PERILLA_DISCOVERY_DIR / "adversarial_reviews_latest.json"
_BELIEF_STATE_FILE = _PERILLA_DISCOVERY_DIR / "belief_state_latest.json"
_TRIGGER_SIGNALS_FILE = _PERILLA_DISCOVERY_DIR / "trigger_signals_latest.json"

# 文件缺失时的降级提示语
_P0_MISSING_HINT = "本地概念闭环数据暂未生成，请先运行：python scripts/run_local_concept_loop.py"
_P8_MISSING_HINT = "数据暂未生成。"

# 合规禁用词（日报禁止出现）
_FORBIDDEN_TERMS = ("买入", "卖出", "强烈推荐")

# 报告白名单目录（相对于 data/reports/）
_ALLOWED_REPORT_SUBDIRS = [
    "",  # data/reports/ 根目录
    "perilla_discovery",
    "single_stock_evidence",
    "perilla_leaf",
]

# 收件箱状态枚举
_INBOX_STATUSES = {"NEW", "WATCHING", "DEEP_RESEARCH_READY", "DISMISSED", "STALE"}

# 观察池分类枚举
_WATCHLIST_CLASSIFICATIONS = {"紫苏叶", "主链锚点", "概念宣称", "早期信号观察", "非核心市场"}


class MobileResearchService:
    """移动端研究数据服务

    提供只读的研究数据查询能力，所有数据从本地文件读取。
    不修改任何数据，不执行写操作。
    """

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _load_inbox_items(self) -> list[dict[str, Any]]:
        """从磁盘加载收件箱条目列表。

        Returns:
            收件箱条目列表，加载失败时返回空列表
        """
        if not _INBOX_FILE.exists():
            logger.warning("收件箱文件不存在: %s", _INBOX_FILE)
            return []
        try:
            with open(_INBOX_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # inbox_items.json 是 dict，值为条目字典
            if isinstance(raw, dict):
                return list(raw.values())
            if isinstance(raw, list):
                return raw
            logger.warning("收件箱文件格式异常: 期望 dict 或 list，实际 %s", type(raw).__name__)
            return []
        except Exception as e:
            logger.error("加载收件箱文件失败: %s", e, exc_info=True)
            return []

    @staticmethod
    def _read_file_safe(path: Path) -> Optional[str]:
        """安全读取文件内容。

        Args:
            path: 文件绝对路径

        Returns:
            文件内容字符串，文件不存在或读取失败时返回 None
        """
        if not path.exists() or not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("读取文件失败: %s — %s", path, e)
            return None

    @staticmethod
    def _markdown_to_html(md_text: str) -> str:
        """将 Markdown 文本转换为简单 HTML。

        优先使用 markdown 库，不可用时返回原始文本。

        Args:
            md_text: Markdown 原始文本

        Returns:
            HTML 字符串或原始 Markdown 文本
        """
        try:
            import markdown
            return markdown.markdown(md_text, extensions=["tables", "fenced_code"])
        except ImportError:
            logger.debug("markdown 库不可用，返回原始 Markdown 文本")
            return md_text

    # ------------------------------------------------------------------
    # P0 结构化板块加载与构建
    # ------------------------------------------------------------------

    @staticmethod
    def _load_json_list(path: Path) -> list[dict[str, Any]]:
        """安全加载 JSON 文件为列表。

        Args:
            path: JSON 文件绝对路径

        Returns:
            字典列表，文件缺失或解析失败时返回空列表
        """
        if not path.exists() or not path.is_file():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
            logger.warning("JSON 文件格式异常: 期望 list/dict，实际 %s — %s", type(data).__name__, path)
            return []
        except Exception as e:
            logger.error("加载 JSON 文件失败: %s — %s", path, e, exc_info=True)
            return []

    def _load_raw_terms(self) -> list[dict[str, Any]]:
        """加载 raw_terms_latest.json。

        Returns:
            原始词条列表，文件缺失时返回空列表
        """
        return self._load_json_list(_RAW_TERMS_FILE)

    def _load_candidate_concepts(self) -> list[dict[str, Any]]:
        """加载 candidate_concepts_latest.json。

        Returns:
            候选概念列表，文件缺失时返回空列表
        """
        return self._load_json_list(_CANDIDATE_CONCEPTS_FILE)

    def _load_evidence_edges(self) -> list[dict[str, Any]]:
        """加载 evidence_edges_latest.json。

        Returns:
            证据边列表，文件缺失时返回空列表
        """
        return self._load_json_list(_EVIDENCE_EDGES_FILE)

    def _build_raw_terms_section(self) -> dict[str, Any]:
        """构建【今日新词雷达】板块。

        按 burst_score 降序取前 15 条，每条格式：名称｜类型｜分数｜来源数。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        terms = self._load_raw_terms()
        if not terms:
            return {
                "markdown": _P0_MISSING_HINT,
                "html": self._markdown_to_html(_P0_MISSING_HINT),
                "source": "raw_terms_latest.json",
            }

        sorted_terms = sorted(
            terms,
            key=lambda x: x.get("burst_score", 0.0),
            reverse=True,
        )[:15]

        lines = ["## 今日新词雷达", ""]
        lines.append("| 名称 | 类型 | 突增分 | 来源数 |")
        lines.append("|---|---|---|---|")
        for t in sorted_terms:
            name = t.get("term", "")
            ttype = t.get("term_type", "unclassified")
            score = t.get("burst_score", 0.0)
            src_count = t.get("source_count", 0)
            lines.append(f"| {name} | {ttype} | {score:.4f} | {src_count} |")

        md_text = "\n".join(lines)
        return {
            "markdown": md_text,
            "html": self._markdown_to_html(md_text),
            "source": "raw_terms_latest.json",
        }

    def _build_candidate_concepts_section(self) -> dict[str, Any]:
        """构建【候选概念】板块。

        按 evidence_document_ids 长度降序取前 10 条，每条格式：名称｜类型｜主线｜证据数。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        concepts = self._load_candidate_concepts()
        if not concepts:
            return {
                "markdown": _P0_MISSING_HINT,
                "html": self._markdown_to_html(_P0_MISSING_HINT),
                "source": "candidate_concepts_latest.json",
            }

        sorted_concepts = sorted(
            concepts,
            key=lambda x: len(x.get("evidence_document_ids", [])),
            reverse=True,
        )[:10]

        lines = ["## 候选概念", ""]
        lines.append("| 名称 | 类型 | 主线 | 证据数 |")
        lines.append("|---|---|---|---|")
        for c in sorted_concepts:
            name = c.get("concept_name", "")
            ttype = c.get("term_type", "unclassified")
            theme = c.get("root_theme", "")
            ev_count = len(c.get("evidence_document_ids", []))
            lines.append(f"| {name} | {ttype} | {theme} | {ev_count} |")

        md_text = "\n".join(lines)
        return {
            "markdown": md_text,
            "html": self._markdown_to_html(md_text),
            "source": "candidate_concepts_latest.json",
        }

    def _build_evidence_edges_section(self) -> dict[str, Any]:
        """构建【新增证据边】板块。

        按 evidence_count 降序取前 10 条，每条格式：源节点｜关系｜目标节点｜置信度｜证据数。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        edges = self._load_evidence_edges()
        if not edges:
            return {
                "markdown": _P0_MISSING_HINT,
                "html": self._markdown_to_html(_P0_MISSING_HINT),
                "source": "evidence_edges_latest.json",
            }

        sorted_edges = sorted(
            edges,
            key=lambda x: x.get("evidence_count", 0),
            reverse=True,
        )[:10]

        lines = ["## 新增证据边", ""]
        lines.append("| 源节点 | 关系 | 目标节点 | 置信度 | 证据数 |")
        lines.append("|---|---|---|---|---|")
        for e in sorted_edges:
            src = e.get("source_node", "")
            rel = e.get("relation", "")
            tgt = e.get("target_node", "")
            conf = e.get("confidence_level", "")
            ev_count = e.get("evidence_count", 0)
            lines.append(f"| {src} | {rel} | {tgt} | {conf} | {ev_count} |")

        md_text = "\n".join(lines)
        return {
            "markdown": md_text,
            "html": self._markdown_to_html(md_text),
            "source": "evidence_edges_latest.json",
        }

    def _build_pending_note_section(self) -> dict[str, Any]:
        """构建【待补充说明】板块（固定提示语）。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        note = (
            "## 待补充说明\n\n"
            "当前为 P0 本地概念闭环阶段，以下能力待后续阶段补齐：\n\n"
            "1. 研报因果链抽取（P1）\n"
            "2. 新词雷达增强与统计评分（P2）\n"
            "3. 产业链图谱与缺失边发现（P3）\n"
            "4. 多智能体假设生成与反方审查（P4A）\n"
            "5. 证据演化与市场共振（P4B）\n"
        )
        return {
            "markdown": note,
            "html": self._markdown_to_html(note),
            "source": "pending_note",
        }

    # ------------------------------------------------------------------
    # P8 结构化板块加载与构建
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_text(text: Any, max_len: int = 80) -> str:
        """清理文本：截断 + 移除换行 + 合规过滤。

        Args:
            text: 原始文本
            max_len: 最大长度

        Returns:
            清理后的安全文本
        """
        if not text:
            return ""
        s = str(text).replace("\n", " ").replace("\r", " ").replace("|", "/")
        s = " ".join(s.split())  # 规范化空格
        if len(s) > max_len:
            s = s[:max_len] + "..."
        # 合规过滤：替换禁用词
        for term in _FORBIDDEN_TERMS:
            if term in s:
                s = s.replace(term, "***")
        return s

    @staticmethod
    def _format_score(value: Any) -> str:
        """格式化分数为 4 位小数。

        Args:
            value: 原始数值

        Returns:
            格式化后的字符串
        """
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return "0.0000"

    @staticmethod
    def _format_percent(value: Any) -> str:
        """格式化百分比为 2 位小数。

        Args:
            value: 原始数值（0-1）

        Returns:
            格式化后的百分比字符串
        """
        try:
            return f"{float(value) * 100:.2f}%"
        except (TypeError, ValueError):
            return "0.00%"

    def _build_causal_chains_section(self) -> dict[str, Any]:
        """构建【研报因果链】板块。

        按 confidence 降序取前 10 条，每条格式：cause｜mechanism｜effect｜置信度。
        文件缺失时降级显示"数据暂未生成。"。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        chains = self._load_json_list(_CAUSAL_CHAINS_FILE)
        if not chains:
            return {
                "markdown": _P8_MISSING_HINT,
                "html": self._markdown_to_html(_P8_MISSING_HINT),
                "source": "causal_chains_latest.json",
            }

        sorted_chains = sorted(
            chains,
            key=lambda x: {"low": 1, "medium": 2}.get(x.get("confidence", ""), 0),
            reverse=True,
        )[:10]

        lines = ["## 研报因果链", ""]
        lines.append("| 因果 | 机制 | 结果 | 置信度 |")
        lines.append("|---|---|---|---|")
        for c in sorted_chains:
            cause = self._sanitize_text(c.get("cause", ""), 30)
            mechanism = self._sanitize_text(c.get("mechanism", ""), 30)
            effect = self._sanitize_text(c.get("effect", ""), 30)
            conf = c.get("confidence", "")
            lines.append(f"| {cause} | {mechanism} | {effect} | {conf} |")

        md_text = "\n".join(lines)
        return {
            "markdown": md_text,
            "html": self._markdown_to_html(md_text),
            "source": "causal_chains_latest.json",
        }

    def _build_missing_edges_section(self) -> dict[str, Any]:
        """构建【缺失边与待搜索任务】板块。

        按 priority_score 降序取前 10 条 missing_edges，并统计 search_tasks 条数。
        每条格式：源节点｜类型｜目标提示｜优先级｜原因。
        文件缺失时降级显示"数据暂未生成。"。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        missing_edges = self._load_json_list(_MISSING_EDGES_FILE)
        search_tasks = self._load_json_list(_SEARCH_TASKS_FILE)

        if not missing_edges and not search_tasks:
            return {
                "markdown": _P8_MISSING_HINT,
                "html": self._markdown_to_html(_P8_MISSING_HINT),
                "source": "missing_edges_latest.json + search_tasks_latest.json",
            }

        sorted_edges = sorted(
            missing_edges,
            key=lambda x: float(x.get("priority_score", 0)),
            reverse=True,
        )[:10]

        lines = ["## 缺失边与待搜索任务", ""]
        lines.append(f"> 待搜索任务总数: {len(search_tasks)}")
        lines.append("")
        lines.append("| 源节点 | 类型 | 目标提示 | 优先级 | 原因 |")
        lines.append("|---|---|---|---|---|")
        for e in sorted_edges:
            src = self._sanitize_text(e.get("source_node", ""), 20)
            stype = self._sanitize_text(e.get("source_type", ""), 15)
            tgt = self._sanitize_text(e.get("target_hint", ""), 20)
            priority = self._format_score(e.get("priority_score", 0))
            reason = self._sanitize_text(e.get("reason", ""), 30)
            lines.append(f"| {src} | {stype} | {tgt} | {priority} | {reason} |")

        md_text = "\n".join(lines)
        return {
            "markdown": md_text,
            "html": self._markdown_to_html(md_text),
            "source": "missing_edges_latest.json + search_tasks_latest.json",
        }

    def _build_research_hypotheses_section(self) -> dict[str, Any]:
        """构建【新研究假设】板块。

        按 novelty_score 降序取前 10 条，每条格式：假设文本｜类型｜新颖度｜证据分｜状态。
        文件缺失时降级显示"数据暂未生成。"。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        hypotheses = self._load_json_list(_RESEARCH_HYPOTHESES_FILE)
        if not hypotheses:
            return {
                "markdown": _P8_MISSING_HINT,
                "html": self._markdown_to_html(_P8_MISSING_HINT),
                "source": "research_hypotheses_latest.json",
            }

        sorted_hyps = sorted(
            hypotheses,
            key=lambda x: float(x.get("novelty_score", 0)),
            reverse=True,
        )[:10]

        lines = ["## 新研究假设", ""]
        lines.append("| 假设 | 类型 | 新颖度 | 证据分 | 状态 |")
        lines.append("|---|---|---|---|---|")
        for h in sorted_hyps:
            text = self._sanitize_text(h.get("hypothesis_text", ""), 40)
            htype = self._sanitize_text(h.get("hypothesis_type", ""), 15)
            novelty = self._format_score(h.get("novelty_score", 0))
            evidence = self._format_score(h.get("evidence_score", 0))
            status = h.get("status", "")
            lines.append(f"| {text} | {htype} | {novelty} | {evidence} | {status} |")

        md_text = "\n".join(lines)
        return {
            "markdown": md_text,
            "html": self._markdown_to_html(md_text),
            "source": "research_hypotheses_latest.json",
        }

    def _build_adversarial_reviews_section(self) -> dict[str, Any]:
        """构建【反方审查】板块。

        按 severity 降序取前 10 条，每条格式：假设ID｜挑战类型｜严重度｜建议验证任务。
        文件缺失时降级显示"数据暂未生成。"。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        reviews = self._load_json_list(_ADVERSARIAL_REVIEWS_FILE)
        if not reviews:
            return {
                "markdown": _P8_MISSING_HINT,
                "html": self._markdown_to_html(_P8_MISSING_HINT),
                "source": "adversarial_reviews_latest.json",
            }

        severity_order = {"high": 3, "medium": 2, "low": 1}
        sorted_reviews = sorted(
            reviews,
            key=lambda x: severity_order.get(x.get("severity", ""), 0),
            reverse=True,
        )[:10]

        lines = ["## 反方审查", ""]
        lines.append("| 假设ID | 挑战类型 | 严重度 | 建议验证任务 |")
        lines.append("|---|---|---|---|")
        for r in sorted_reviews:
            hid = self._sanitize_text(r.get("hypothesis_id", ""), 15)
            ctype = self._sanitize_text(r.get("challenge_type", ""), 20)
            severity = r.get("severity", "")
            suggested = self._sanitize_text(r.get("suggested_validation_task", ""), 30)
            lines.append(f"| {hid} | {ctype} | {severity} | {suggested} |")

        md_text = "\n".join(lines)
        return {
            "markdown": md_text,
            "html": self._markdown_to_html(md_text),
            "source": "adversarial_reviews_latest.json",
        }

    def _build_evidence_evolution_section(self) -> dict[str, Any]:
        """构建【证据演化与市场共振】板块。

        合并 belief_state 和 trigger_signals，按 posterior_probability 降序取前 10 条。
        每条格式：假设ID｜后验概率｜状态｜触发动作。
        文件缺失时降级显示"数据暂未生成。"。

        Returns:
            板块字典，含 markdown / html / source 三个 key
        """
        beliefs = self._load_json_list(_BELIEF_STATE_FILE)
        triggers = self._load_json_list(_TRIGGER_SIGNALS_FILE)

        if not beliefs and not triggers:
            return {
                "markdown": _P8_MISSING_HINT,
                "html": self._markdown_to_html(_P8_MISSING_HINT),
                "source": "belief_state_latest.json + trigger_signals_latest.json",
            }

        # 构建 hypothesis_id → trigger 映射
        trigger_map: dict[str, dict[str, Any]] = {}
        for t in triggers:
            hid = t.get("hypothesis_id", "")
            if hid and hid not in trigger_map:
                trigger_map[hid] = t

        sorted_beliefs = sorted(
            beliefs,
            key=lambda x: float(x.get("posterior_probability", 0)),
            reverse=True,
        )[:10]

        lines = ["## 证据演化与市场共振", ""]
        lines.append(f"> 触发信号总数: {len(triggers)}")
        lines.append("")
        lines.append("| 假设ID | 后验概率 | 状态 | 触发动作 |")
        lines.append("|---|---|---|---|")
        for b in sorted_beliefs:
            hid = self._sanitize_text(b.get("hypothesis_id", ""), 15)
            posterior = self._format_percent(b.get("posterior_probability", 0))
            state = b.get("state", "")
            trigger = trigger_map.get(b.get("hypothesis_id", ""), {})
            action = trigger.get("next_action", "keep_monitoring")
            lines.append(f"| {hid} | {posterior} | {state} | {action} |")

        md_text = "\n".join(lines)
        return {
            "markdown": md_text,
            "html": self._markdown_to_html(md_text),
            "source": "belief_state_latest.json + trigger_signals_latest.json",
        }

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def _build_rich_daily_brief(self, base_brief: Optional[str]) -> str:
        """构造丰富的日报摘要。

        如果 base_brief 已经较长，直接返回；否则从 P0/P8 结构化数据中提取关键信息补充，
        让移动端首页摘要卡片内容更丰满。

        Args:
            base_brief: daily_brief.md 原始内容，可能为 None

        Returns:
            Markdown 格式的日报摘要文本
        """
        parts: list[str] = []
        if base_brief:
            parts.append(base_brief.strip())

        # 当原始日报较短时，自动追加结构化摘要
        need_supplement = (not base_brief) or len(base_brief.strip()) < 300
        if not need_supplement:
            return "\n\n".join(parts)

        # 今日新词雷达 TOP 5
        raw_terms = self._load_raw_terms()
        if raw_terms:
            top_terms = sorted(
                raw_terms, key=lambda x: x.get("burst_score", 0.0), reverse=True
            )[:5]
            term_lines = ["## 今日新词雷达 TOP5", ""]
            for t in top_terms:
                name = t.get("term", "")
                ttype = t.get("term_type", "unclassified")
                score = t.get("burst_score", 0.0)
                src_count = t.get("source_count", 0)
                term_lines.append(f"- **{name}** ({ttype}) 突增分 {score:.4f}，来源 {src_count}")
            parts.append("\n".join(term_lines))

        # 候选概念 TOP 3
        candidate_concepts = self._load_candidate_concepts()
        if candidate_concepts:
            top_concepts = sorted(
                candidate_concepts,
                key=lambda x: len(x.get("evidence_document_ids", [])),
                reverse=True,
            )[:3]
            concept_lines = ["## 候选概念 TOP3", ""]
            for c in top_concepts:
                name = c.get("concept_name", "")
                theme = c.get("root_theme", "")
                ev_count = len(c.get("evidence_document_ids", []))
                concept_lines.append(f"- **{name}**" + (f"（{theme}）" if theme else "") + f"，证据 {ev_count}")
            parts.append("\n".join(concept_lines))

        # 新增证据边 TOP 3
        evidence_edges = self._load_evidence_edges()
        if evidence_edges:
            top_edges = sorted(
                evidence_edges, key=lambda x: x.get("evidence_count", 0), reverse=True
            )[:3]
            edge_lines = ["## 新增证据边 TOP3", ""]
            for e in top_edges:
                src = e.get("source_node", "")
                rel = e.get("relation", "")
                tgt = e.get("target_node", "")
                ev_count = e.get("evidence_count", 0)
                edge_lines.append(f"- **{src}** {rel} **{tgt}**（证据 {ev_count}）")
            parts.append("\n".join(edge_lines))

        if len(parts) == 1:
            return parts[0]
        return "\n\n".join(parts)

    def get_summary(self) -> dict[str, Any]:
        """获取研究数据摘要总览。

        Returns:
            包含日期、日报、热门候选、收件箱统计、观察池统计、任务统计的字典
        """
        items = self._load_inbox_items()

        # 收件箱按状态统计
        inbox_stats: dict[str, int] = {s: 0 for s in _INBOX_STATUSES}
        for item in items:
            status = item.get("inbox_status", "NEW")
            if status in inbox_stats:
                inbox_stats[status] += 1
            else:
                inbox_stats[status] = inbox_stats.get(status, 0) + 1

        # 观察池按分类统计
        watchlist_stats: dict[str, int] = {}
        for item in items:
            cls = item.get("classification", "未分类")
            watchlist_stats[cls] = watchlist_stats.get(cls, 0) + 1

        # 按研究优先级排序，移动端取前 100 条以便前端做市场筛选
        sorted_items = sorted(
            items,
            key=lambda x: x.get("research_priority_score", 0),
            reverse=True,
        )
        top_candidates = sorted_items[:100]

        # 读取日报并构造丰富摘要
        daily_brief_path = _REPORTS_DIR / "daily_brief.md"
        daily_brief_content = self._read_file_safe(daily_brief_path)
        latest_daily_brief = self._build_rich_daily_brief(daily_brief_content)

        # 任务统计
        try:
            if get_task_service is not None:
                task_service = get_task_service()
                task_stats = task_service.get_task_stats()
            else:
                task_stats = {}
        except Exception as e:
            logger.error("获取任务统计失败: %s", e)
            task_stats = {}

        return {
            "date": date.today().isoformat(),
            "latest_daily_brief": latest_daily_brief,
            "top_candidates": top_candidates,
            "inbox_stats": inbox_stats,
            "watchlist_stats": watchlist_stats,
            "task_stats": task_stats,
            "last_updated": datetime.now().isoformat(),
        }

    def get_daily(self) -> dict[str, Any]:
        """获取最新日报内容。

        读取 daily_brief.md 和紫苏发现报告，返回原始 Markdown 和 HTML。
        同时追加 P0 结构化板块（今日新词雷达 / 候选概念 / 新增证据边 / 待补充说明），
        每个板块以新 key 形式追加，不删除原有 daily_brief / perilla_discovery。

        Returns:
            包含日报、紫苏发现报告及 P0 结构化板块的字典
        """
        # 日报
        daily_brief_path = _REPORTS_DIR / "daily_brief.md"
        daily_brief_md = self._read_file_safe(daily_brief_path)

        # 紫苏发现报告
        perilla_path = _REPORTS_DIR / "perilla_discovery" / "perilla_discovery_latest.md"
        perilla_md = self._read_file_safe(perilla_path)

        # 构造结果（保留原有 key）
        result: dict[str, Any] = {
            "daily_brief": None,
            "perilla_discovery": None,
        }

        if daily_brief_md is not None:
            result["daily_brief"] = {
                "markdown": daily_brief_md,
                "html": self._markdown_to_html(daily_brief_md),
                "source": "daily_brief.md",
            }

        if perilla_md is not None:
            result["perilla_discovery"] = {
                "markdown": perilla_md,
                "html": self._markdown_to_html(perilla_md),
                "source": "perilla_discovery/perilla_discovery_latest.md",
            }

        # 追加 P0 结构化板块（新 key，不删除原有 key）
        result["raw_terms_section"] = self._build_raw_terms_section()
        result["candidate_concepts_section"] = self._build_candidate_concepts_section()
        result["evidence_edges_section"] = self._build_evidence_edges_section()
        result["pending_note_section"] = self._build_pending_note_section()

        # 追加 P8 结构化板块（新 key，不删除原有 key）
        result["causal_chains_section"] = self._build_causal_chains_section()
        result["missing_edges_section"] = self._build_missing_edges_section()
        result["research_hypotheses_section"] = self._build_research_hypotheses_section()
        result["adversarial_reviews_section"] = self._build_adversarial_reviews_section()
        result["evidence_evolution_section"] = self._build_evidence_evolution_section()

        return result

    def get_inbox(
        self,
        status: Optional[str] = None,
        classification: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """获取收件箱条目列表。

        Args:
            status: 可选的状态过滤条件（NEW / WATCHING / DEEP_RESEARCH_READY / DISMISSED / STALE）
            classification: 可选的分类过滤条件
            limit: 返回数量上限
            offset: 跳过前 N 条记录

        Returns:
            包含 items 列表和 total 总数的字典
        """
        items = self._load_inbox_items()

        # 过滤
        if status:
            items = [i for i in items if i.get("inbox_status") == status]
        if classification:
            items = [i for i in items if i.get("classification") == classification]

        # 按研究优先级降序排序
        items.sort(key=lambda x: x.get("research_priority_score", 0), reverse=True)

        total = len(items)
        page = items[offset : offset + limit]

        # 精简每条记录，只返回移动端需要的字段
        simplified = []
        for item in page:
            simplified.append({
                "item_id": item.get("item_id", ""),
                "company_name": item.get("company_name", ""),
                "symbol": item.get("symbol", ""),
                "chain_node": item.get("chain_node", ""),
                "concept": item.get("concept", ""),
                "evidence_level": item.get("evidence_level", ""),
                "classification": item.get("classification", ""),
                "total_score": item.get("total_score", 0),
                "inbox_status": item.get("inbox_status", ""),
                "research_priority_score": item.get("research_priority_score", 0),
                "research_priority_level": item.get("research_priority_level", ""),
                "bottleneck_logic": item.get("bottleneck_logic", ""),
            })

        return {
            "items": simplified,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_watchlist(
        self,
        classification: Optional[str] = None,
        min_priority: Optional[float] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """获取观察池条目列表。

        Args:
            classification: 可选的分类过滤条件（紫苏叶 / 主链锚点 / 概念宣称 / 早期信号观察 / 非核心市场）
            min_priority: 可选的最低研究优先级分数
            limit: 返回数量上限
            offset: 跳过前 N 条记录

        Returns:
            包含 items 列表和 total 总数的字典
        """
        items = self._load_inbox_items()

        # 过滤
        if classification:
            items = [i for i in items if i.get("classification") == classification]
        if min_priority is not None:
            items = [
                i for i in items
                if i.get("research_priority_score", 0) >= min_priority
            ]

        # 按研究优先级降序排序
        items.sort(key=lambda x: x.get("research_priority_score", 0), reverse=True)

        total = len(items)
        page = items[offset : offset + limit]

        # 精简字段
        simplified = []
        for item in page:
            simplified.append({
                "item_id": item.get("item_id", ""),
                "company_name": item.get("company_name", ""),
                "symbol": item.get("symbol", ""),
                "chain_node": item.get("chain_node", ""),
                "concept": item.get("concept", ""),
                "evidence_level": item.get("evidence_level", ""),
                "classification": item.get("classification", ""),
                "total_score": item.get("total_score", 0),
                "inbox_status": item.get("inbox_status", ""),
                "research_priority_score": item.get("research_priority_score", 0),
                "research_priority_level": item.get("research_priority_level", ""),
                "bottleneck_logic": item.get("bottleneck_logic", ""),
            })

        return {
            "items": simplified,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _build_inbox_chain_concepts(self, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """从收件箱条目聚合 concept -> chain_node -> company 三层结构。

        Args:
            items: 收件箱条目列表

        Returns:
            以 concept 为键的聚合字典，每个值包含 nodes 和 companies 两个字典
        """
        concepts: dict[str, dict[str, Any]] = {}
        for item in items:
            concept = str(item.get("concept") or "其他概念").strip()
            chain_node = str(item.get("chain_node") or "其他环节").strip()
            company = str(item.get("company_name") or "未知公司").strip()
            symbol = str(item.get("symbol") or "").strip()
            if not concept or not chain_node or not company:
                continue

            concept_data = concepts.setdefault(concept, {"nodes": {}, "companies": {}})
            node_id = f"inbox-node::{concept}::{chain_node}"
            if node_id not in concept_data["nodes"]:
                concept_data["nodes"][node_id] = {
                    "id": node_id,
                    "name": chain_node,
                    "type": "chain_node",
                    "layer": "chain_node",
                    "order": 0,
                    "concept": concept,
                    "evidence_count": 0,
                    "score": 0.0,
                    "source": "inbox_items.json",
                    "description": str(item.get("bottleneck_logic") or "")[:120],
                }
            node = concept_data["nodes"][node_id]
            node["evidence_count"] += 1
            node["score"] = max(node["score"], float(item.get("research_priority_score", 0) or 0))

            company_key = f"{concept}::{chain_node}::{company}"
            if company_key not in concept_data["companies"]:
                display_name = company
                if symbol and symbol != "未上市":
                    display_name = f"{company} ({symbol})"
                concept_data["companies"][company_key] = {
                    "id": f"inbox-stock::{company_key}",
                    "name": display_name,
                    "type": "stock",
                    "layer": "company",
                    "order": 0,
                    "concept": concept,
                    "chain_node": chain_node,
                    "symbol": symbol,
                    "evidence_count": 1,
                    "score": float(item.get("research_priority_score", 0) or 0),
                    "source": "inbox_items.json",
                    "classification": str(item.get("classification") or ""),
                    "evidence_level": str(item.get("evidence_level") or ""),
                    "description": str(item.get("bottleneck_logic") or "")[:80],
                }
        return concepts

    def list_inbox_chain_concepts(self) -> list[dict[str, Any]]:
        """列出收件箱中所有可用于产业链展示的概念。

        Returns:
            概念元信息列表，按节点数与公司数降序排列
        """
        items = self._load_inbox_items()
        concepts = self._build_inbox_chain_concepts(items)
        result: list[dict[str, Any]] = []
        for concept, data in concepts.items():
            node_scores = [n["score"] for n in data["nodes"].values()]
            company_scores = [c["score"] for c in data["companies"].values()]
            result.append({
                "chain_id": f"inbox::{concept}",
                "name": concept,
                "node_count": len(data["nodes"]),
                "stock_count": len(data["companies"]),
                "max_score": round(max(company_scores or node_scores or [0.0]), 2),
                "source": "inbox_items.json",
            })
        result.sort(key=lambda x: (-x["node_count"], -x["stock_count"]))
        return result

    def get_chain_from_inbox(self, concept: str | None = None) -> dict[str, Any]:
        """从收件箱构建产业链拓扑图。

        按 concept -> chain_node -> company 三层横向展开，节点字段兼容
        /api/industry-chain/graph 的 T3.4 规范。未指定 concept 时选取节点数最多的概念作为主链。

        Args:
            concept: 可选，指定要构建的概念名称；为空时使用主概念

        Returns:
            兼容 GraphPayload 的字典，包含 nodes / links / layers / summary
        """
        items = self._load_inbox_items()
        empty_payload = {
            "nodes": [],
            "links": [],
            "layers": [],
            "summary": {
                "chain_id": "inbox",
                "chain_name": concept or "收件箱",
                "hot_score": 0.0,
                "market_strength": "暂无",
                "top_node": "",
                "top_node_name": "",
                "limit_up_count": 0,
                "turnover_change": 0.0,
                "node_count": 0,
                "stock_count": 0,
            },
            "data_date": date.today().isoformat(),
            "source": "inbox_items.json",
            "has_data": False,
            "demo_mode": False,
        }
        if not items:
            return empty_payload

        concepts = self._build_inbox_chain_concepts(items)
        if not concepts:
            return empty_payload

        if concept and concept in concepts:
            primary_concept = concept
        else:
            primary_concept = max(concepts.items(), key=lambda x: len(x[1]["nodes"]))[0]
        primary_data = concepts[primary_concept]

        theme_node: dict[str, Any] = {
            "id": f"inbox-theme::{primary_concept}",
            "name": primary_concept,
            "type": "theme",
            "layer": "theme",
            "order": 0,
            "evidence_count": sum(n["evidence_count"] for n in primary_data["nodes"].values()),
            "score": max((c["score"] for c in primary_data["companies"].values()), default=0.0),
            "source": "inbox_items.json",
        }

        nodes: list[dict[str, Any]] = [theme_node]
        nodes.extend(sorted(primary_data["nodes"].values(), key=lambda x: (-x["score"], x["name"])))
        nodes.extend(sorted(primary_data["companies"].values(), key=lambda x: (-x["score"], x["name"])))

        links: list[dict[str, Any]] = []
        for node in primary_data["nodes"].values():
            links.append({
                "source": theme_node["id"],
                "target": node["id"],
                "relation_type": "contains",
                "weight": 0.8,
                "evidence": [f"概念: {primary_concept}"],
            })
        for company in primary_data["companies"].values():
            parent_id = f"inbox-node::{company['concept']}::{company['chain_node']}"
            links.append({
                "source": parent_id,
                "target": company["id"],
                "relation_type": "belongs_to",
                "weight": 0.7,
                "evidence": [f"节点: {company['chain_node']}"],
            })

        stock_count = len(primary_data["companies"])
        top_node = max(nodes, key=lambda n: float(n.get("score", 0) or 0))
        return {
            "nodes": nodes,
            "links": links,
            "layers": [
                {"id": "theme", "name": "概念", "order": 0},
                {"id": "chain_node", "name": "产业链环节", "order": 1},
                {"id": "company", "name": "公司/标的", "order": 2},
            ],
            "summary": {
                "chain_id": f"inbox::{primary_concept}",
                "chain_name": primary_concept,
                "hot_score": round(float(top_node.get("score", 0) or 0), 2),
                "market_strength": "中" if top_node.get("score", 0) else "弱",
                "top_node": top_node.get("id", ""),
                "top_node_name": top_node.get("name", ""),
                "limit_up_count": 0,
                "turnover_change": 0.0,
                "node_count": len(nodes),
                "stock_count": stock_count,
            },
            "data_date": date.today().isoformat(),
            "source": "inbox_items.json",
            "has_data": True,
            "demo_mode": False,
        }

    def get_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """获取任务列表。

        委托给 task_service.list_tasks()。

        Args:
            status: 可选的状态过滤条件
            limit: 返回数量上限
            offset: 跳过前 N 条记录

        Returns:
            包含 tasks 列表和分页信息的字典
        """
        try:
            if get_task_service is not None:
                task_service = get_task_service()
                return task_service.list_tasks(status=status, limit=limit, offset=offset)
            return []
        except Exception as e:
            logger.error("获取任务列表失败: %s", e, exc_info=True)
            return {"tasks": [], "total": 0, "limit": limit, "offset": offset}

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """获取任务详情。

        委托给 task_service.get_task()。

        Args:
            task_id: 任务唯一标识

        Returns:
            任务详情字典，不存在时返回 None
        """
        try:
            if get_task_service is not None:
                task_service = get_task_service()
                return task_service.get_task(task_id)
            return None
        except Exception as e:
            logger.error("获取任务详情失败: %s", e, exc_info=True)
            return None

    def get_reports(self) -> dict[str, Any]:
        """获取可用报告列表。

        扫描白名单目录下的 .md 文件，返回文件元信息列表。

        Returns:
            包含 reports 列表的字典
        """
        reports: list[dict[str, Any]] = []

        for subdir in _ALLOWED_REPORT_SUBDIRS:
            scan_dir = _REPORTS_DIR / subdir if subdir else _REPORTS_DIR
            if not scan_dir.exists() or not scan_dir.is_dir():
                continue

            try:
                for entry in scan_dir.iterdir():
                    if not entry.is_file() or entry.suffix != ".md":
                        continue
                    # 计算 report_id（相对于 data/reports/ 的路径）
                    report_id = str(entry.relative_to(_REPORTS_DIR)).replace("\\", "/")
                    stat = entry.stat()
                    reports.append({
                        "report_id": report_id,
                        "filename": entry.name,
                        "path": str(entry),
                        "size": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
            except Exception as e:
                logger.error("扫描报告目录失败: %s — %s", scan_dir, e)

        # 按修改时间降序排序
        reports.sort(key=lambda x: x["modified_at"], reverse=True)

        return {"reports": reports}

    def get_report(self, report_id: str) -> Optional[dict[str, Any]]:
        """获取报告内容。

        Args:
            report_id: 报告标识，相对于 data/reports/ 的路径

        Returns:
            报告详情字典，路径无效时返回 None
        """
        abs_path = self.safe_report_path(report_id)
        if abs_path is None:
            return None

        content = self._read_file_safe(Path(abs_path))
        if content is None:
            return None

        stat = os.stat(abs_path)
        filename = os.path.basename(abs_path)

        return {
            "report_id": report_id,
            "filename": filename,
            "content": content,
            "html": self._markdown_to_html(content),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }

    def safe_report_path(self, report_id: str) -> Optional[str]:
        """验证并解析 report_id 为绝对路径。

        安全检查：
        1. 拒绝包含 ".." 的路径（防止路径穿越）
        2. 拒绝以 "/" 开头的路径（防止绝对路径注入）
        3. 只允许白名单目录下的文件
        4. 解析后的绝对路径必须在 data/reports/ 下

        Args:
            report_id: 报告标识，相对于 data/reports/ 的路径

        Returns:
            验证通过的绝对路径字符串，无效时返回 None
        """
        if not report_id:
            return None

        # 拒绝路径穿越和绝对路径
        if ".." in report_id or report_id.startswith("/"):
            logger.warning("报告路径安全校验失败（路径穿越或绝对路径）: %s", report_id)
            return None

        # 拒绝反斜杠路径穿越（Windows 兼容）
        normalized = report_id.replace("\\", "/")
        if ".." in normalized:
            logger.warning("报告路径安全校验失败（反斜杠穿越）: %s", report_id)
            return None

        # 解析绝对路径
        abs_path = (_REPORTS_DIR / normalized).resolve()

        # 确保解析后的路径仍在 reports 目录下
        if not str(abs_path).startswith(str(_REPORTS_DIR.resolve())):
            logger.warning("报告路径安全校验失败（逃逸 reports 目录）: %s", report_id)
            return None

        # 检查白名单目录
        # 计算相对于 reports 的子目录
        try:
            rel = abs_path.relative_to(_REPORTS_DIR.resolve())
            # 第一级目录名（或空串表示根目录）
            parts = rel.parts
            top_dir = parts[0] if len(parts) > 1 else ""
        except ValueError:
            logger.warning("报告路径安全校验失败（无法计算相对路径）: %s", report_id)
            return None

        if top_dir not in _ALLOWED_REPORT_SUBDIRS:
            logger.warning("报告路径安全校验失败（目录不在白名单中）: %s — top_dir=%s", report_id, top_dir)
            return None

        # 文件必须存在且是普通文件
        if not abs_path.exists() or not abs_path.is_file():
            return None

        return str(abs_path)


# 全局单例
_mobile_research_service: Optional[MobileResearchService] = None
_service_lock = __import__("threading").Lock()


def get_mobile_research_service() -> MobileResearchService:
    """获取 MobileResearchService 单例。

    Returns:
        MobileResearchService 实例
    """
    global _mobile_research_service
    if _mobile_research_service is None:
        with _service_lock:
            if _mobile_research_service is None:
                _mobile_research_service = MobileResearchService()
    return _mobile_research_service
