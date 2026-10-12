"""Perilla Discovery Engine - 报告构建器.

将 PerillaDiscoveryReport 导出为 JSON 和 Markdown 格式，
同时导出各中间产物（概念信号、来源文档、供应链、瓶颈假设、
公司候选、紫苏发现条目）。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Any, Dict, List

from .models import (
    DISCOVERY_CLASSIFICATIONS,
    EVIDENCE_LEVELS,
    BottleneckHypothesis,
    CompanyCandidate,
    ConceptSignal,
    DynamicSupplyChain,
    PerillaDiscoveryItem,
    PerillaDiscoveryReport,
    SourceDocument,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 证据等级 → 中文显示映射
# ---------------------------------------------------------------------------
_EVIDENCE_LEVEL_DISPLAY: Dict[str, str] = {
    "CONFIRMED_SUPPLIER": "[确认] 确认供应商",
    "PROBABLE_SUPPLIER": "[可能] 可能供应商",
    "CONCEPT_CLAIM_ONLY": "[声明] 仅概念声明",
    "EARLY_SIGNAL": "[早期] 早期信号",
    "RUMOR_ONLY": "[传闻] 仅传闻",
    "HYPOTHESIS": "[假设] AI推理假设",
    "MAIN_ANCHOR": "[锚点] 主线锚点公司",
    "NON_CORE_MARKET": "[非核心] 非核心市场",
    "INSUFFICIENT_EVIDENCE": "[不足] 证据不足",
}

# ---------------------------------------------------------------------------
# 分类标签 → 中文显示映射
# ---------------------------------------------------------------------------
_CLASSIFICATION_DISPLAY: Dict[str, str] = {
    "PERILLA_CANDIDATE": "紫苏叶候选",
    "CONCEPT_CLAIM_ONLY": "概念候选池",
    "EARLY_SIGNAL_WATCH": "早期信号池",
    "RUMOR_WATCH": "传闻观察池",
    "HYPOTHESIS_ONLY": "AI假设池",
    "MAIN_ANCHOR": "主线锚点公司",
    "NON_CORE_MARKET": "非核心市场提示",
    "NEEDS_HUMAN_REVIEW": "待人工复核",
}

# 供应链层级 → 中文显示映射
_LAYER_DISPLAY: Dict[str, str] = {
    "terminal": "终端产品",
    "module": "核心模块",
    "component": "关键组件",
    "material": "基础材料",
    "equipment": "生产设备",
    "testing": "检测测试",
    "consumable": "耗材辅材",
    "service": "服务",
    "software": "软件",
    "unknown": "未知层级",
}

# 层级排序（用于树形渲染）
_LAYER_ORDER: List[str] = [
    "terminal", "module", "component", "material",
    "equipment", "testing", "consumable",
]


class ReportBuilder:
    """报告构建器 —— 将发现报告导出为 JSON 和 Markdown.

    支持完整报告导出和各中间产物单独导出。
    所有文件操作均包裹 try/except，单步失败不影响整体流程。
    """

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def build_report(
        self,
        report: PerillaDiscoveryReport,
        output_dir: str = "data/reports/perilla_discovery",
    ) -> str:
        """导出完整报告到 JSON 和 Markdown 文件.

        同时导出各中间产物文件。

        Args:
            report: 紫苏发现报告实例。
            output_dir: 输出目录路径。

        Returns:
            Markdown 报告文件的绝对路径。
        """
        os.makedirs(output_dir, exist_ok=True)

        # 导出完整报告 JSON
        json_path = os.path.join(output_dir, "perilla_discovery_report.json")
        self._safe_write_json(json_path, self._report_to_dict(report))

        # 导出完整报告 Markdown
        md_path = os.path.join(output_dir, "perilla_discovery_report.md")
        md_content = self._render_markdown(report)
        self._safe_write_text(md_path, md_content)

        # 导出中间产物
        self._export_intermediate(report, output_dir)

        abs_md_path = os.path.abspath(md_path)
        logger.info("报告已导出: JSON=%s, MD=%s", json_path, abs_md_path)
        return abs_md_path

    # ------------------------------------------------------------------
    # Markdown 渲染
    # ------------------------------------------------------------------

    def _render_markdown(self, report: PerillaDiscoveryReport) -> str:
        """渲染完整报告为 Markdown.

        按照 spec section 20 的 21 个章节顺序输出。

        Args:
            report: 紫苏发现报告实例。

        Returns:
            Markdown 格式字符串。
        """
        lines: List[str] = []

        # 标题
        lines.append("# Perilla Discovery 报告")
        lines.append("")
        lines.append(f"**Run ID**: {report.run_id}")
        lines.append(f"**生成时间**: {report.generated_at}")
        lines.append("")

        # 1. 运行模式
        lines.append("## 1. 运行模式")
        lines.append("")
        mode_display = "自动发现" if report.mode == "auto" else "主题驱动"
        lines.append(f"- **模式**: {mode_display} (`{report.mode}`)")
        lines.append("")

        # 2. 输入主题或自动发现来源
        lines.append("## 2. 输入主题或自动发现来源")
        lines.append("")
        if report.input_topic:
            lines.append(f"- **输入主题**: {report.input_topic}")
        else:
            sources = sorted({s for cs in report.concept_signals for s in cs.detected_from})
            if sources:
                lines.append("- **自动发现来源**: " + ", ".join(sources))
            else:
                lines.append("- **自动发现来源**: 无")
        lines.append("")

        # 3. 新概念列表
        lines.append("## 3. 新概念列表")
        lines.append("")
        if report.concept_signals:
            lines.append("| 概念名 | 热度 | 新颖度 | 置信度 | 来源数 |")
            lines.append("|--------|------|--------|--------|--------|")
            for cs in report.concept_signals:
                lines.append(
                    f"| {cs.name} | {cs.heat_score:.2f} | "
                    f"{cs.novelty_score:.2f} | {cs.confidence} | "
                    f"{len(cs.detected_from)} |"
                )
        else:
            lines.append("*暂无概念信号*")
        lines.append("")

        # 4. 需求锚点
        lines.append("## 4. 需求锚点")
        lines.append("")
        if report.demand_anchors:
            lines.append("| 锚点名 | 类型 | 置信度 |")
            lines.append("|--------|------|--------|")
            for da in report.demand_anchors:
                lines.append(f"| {da.name} | {da.anchor_type} | {da.confidence} |")
        else:
            lines.append("*暂无需求锚点*")
        lines.append("")

        # 5. 数据源覆盖情况
        lines.append("## 5. 数据源覆盖情况")
        lines.append("")
        stats = report.source_stats or {}
        if stats:
            for key, value in stats.items():
                lines.append(f"- **{key}**: {value}")
        else:
            lines.append("*暂无来源统计*")
        lines.append("")

        # 6. 指数页/概念页/板块页证据
        lines.append("## 6. 指数页/概念页/板块页证据")
        lines.append("")
        index_evidence = self._collect_evidence_by_types(
            report, {"index_page", "concept_page", "sector_page"},
        )
        if index_evidence:
            for ev in index_evidence:
                lines.append(f"- {ev}")
        else:
            lines.append("*暂无指数页/概念页/板块页证据*")
        lines.append("")

        # 7. 新闻/研报/公告证据
        lines.append("## 7. 新闻/研报/公告证据")
        lines.append("")
        news_evidence = self._collect_evidence_by_types(
            report, {"news", "report", "announcement"},
        )
        if news_evidence:
            for ev in news_evidence:
                lines.append(f"- {ev}")
        else:
            lines.append("*暂无新闻/研报/公告证据*")
        lines.append("")

        # 8. 早期信号证据
        lines.append("## 8. 早期信号证据")
        lines.append("")
        early_evidence = self._collect_evidence_by_types(
            report, {"recruitment", "expo", "tender", "certification", "sampling"},
        )
        if early_evidence:
            for ev in early_evidence:
                lines.append(f"- {ev}")
        else:
            lines.append("*暂无早期信号证据*")
        lines.append("")

        # 9. 动态产业链图
        lines.append("## 9. 动态产业链图")
        lines.append("")
        if report.dynamic_chains:
            for chain in report.dynamic_chains:
                lines.append(f"### 供应链: {chain.chain_id}")
                lines.append(f"- 构建方法: {chain.construction_method}")
                lines.append(f"- 置信度: {chain.confidence}")
                lines.append("")
                tree_text = self._render_chain_tree(chain)
                lines.append(tree_text)
                lines.append("")
        else:
            lines.append("*暂无动态供应链*")
            lines.append("")

        # 10. 疑似瓶颈环节
        lines.append("## 10. 疑似瓶颈环节")
        lines.append("")
        if report.bottleneck_hypotheses:
            lines.append("| 环节 | 信号数 | 六问总分 | 紫苏概率 | 状态 |")
            lines.append("|------|--------|----------|----------|------|")
            for bh in report.bottleneck_hypotheses:
                lines.append(
                    f"| {bh.node_name} | {len(bh.bottleneck_signals)} | "
                    f"{bh.total_score} | {bh.perilla_probability:.2%} | "
                    f"{bh.status} |"
                )
        else:
            lines.append("*暂无瓶颈假设*")
        lines.append("")

        # 11. 公司映射表
        lines.append("## 11. 公司映射表")
        lines.append("")
        if report.company_candidates:
            lines.append(
                "| 公司 | 市场 | 代码 | 关联环节 | 分类 | 证据等级 | "
                "证据摘要 | 来源链接 | 是否待复核 | 提示标签 |"
            )
            lines.append(
                "|------|------|------|----------|------|----------|"
                "----------|----------|------------|----------|"
            )
            # 按 discovery_items 查找分类和标签
            item_by_company = self._build_item_lookup(report.discovery_items)
            for cc in report.company_candidates:
                item = item_by_company.get(cc.candidate_id)
                classification = (
                    self._format_classification(item.classification)
                    if item else "-"
                )
                evidence_display = self._format_evidence_level(cc.evidence_level)
                summary = (cc.claim_summary or "-")[:40]
                source_links = ", ".join(cc.mapping_sources[:2]) if cc.mapping_sources else "-"
                needs_review = "是" if cc.needs_human_review else "否"
                warning = cc.market_warning or ""
                risk_tags = ""
                if item and item.risk_flags:
                    risk_tags = ", ".join(item.risk_flags[:2])
                tag_label = f"{warning}; {risk_tags}".strip("; ") if warning or risk_tags else "-"
                lines.append(
                    f"| {cc.company_name} | {cc.market} | "
                    f"{cc.symbol or '-'} | {cc.chain_node_id[:12] if cc.chain_node_id else '-'} | "
                    f"{classification} | {evidence_display} | "
                    f"{summary} | {source_links} | {needs_review} | {tag_label} |"
                )
        else:
            lines.append("*暂无公司候选*")
        lines.append("")

        # 12-18. 按分类分池展示
        pool_sections = [
            ("PERILLA_CANDIDATE", "12. 紫苏叶候选池"),
            ("CONCEPT_CLAIM_ONLY", "13. 概念候选池"),
            ("EARLY_SIGNAL_WATCH", "14. 早期信号池"),
            ("RUMOR_WATCH", "15. 传闻观察池"),
            ("HYPOTHESIS_ONLY", "16. AI假设池"),
            ("MAIN_ANCHOR", "17. 主线锚点公司"),
            ("NON_CORE_MARKET", "18. 非核心市场提示"),
        ]
        for classification, section_title in pool_sections:
            lines.append(f"## {section_title}")
            lines.append("")
            pool_items = [
                it for it in report.discovery_items
                if it.classification == classification
            ]
            if pool_items:
                for it in pool_items:
                    lines.append(f"### {it.company_name}")
                    lines.append(f"- **证券代码**: {it.symbol or '-'}")
                    lines.append(f"- **市场**: {it.market}")
                    lines.append(f"- **证据等级**: {self._format_evidence_level(it.evidence_level)}")
                    lines.append(f"- **紫苏概率**: {it.perilla_probability:.2%}")
                    lines.append(f"- **瓶颈逻辑**: {it.bottleneck_logic or '-'}")
                    lines.append(f"- **证据摘要**: {it.evidence_summary or '-'}")
                    if it.source_urls:
                        lines.append(f"- **来源链接**: {', '.join(it.source_urls[:5])}")
                    if it.risk_flags:
                        lines.append(f"- **风险标记**: {', '.join(it.risk_flags)}")
                    lines.append(f"- **审核状态**: {it.review_status}")
                    lines.append("")
            else:
                lines.append("*暂无条目*")
                lines.append("")

        # 19. 待人工复核队列
        lines.append("## 19. 待人工复核队列")
        lines.append("")
        if report.review_queue:
            lines.append("| 优先级 | ID | 原因 | 建议操作 |")
            lines.append("|--------|-----|------|----------|")
            for rq in report.review_queue:
                reason = str(rq.get("reason", "")).replace("|", "/")
                action = str(rq.get("required_action", "")).replace("|", "/")
                priority = rq.get("priority", "-")
                item_id = str(rq.get("item_id", ""))[:12]
                lines.append(f"| {priority} | {item_id}... | {reason} | {action} |")
        else:
            lines.append("*暂无待复核条目*")
        lines.append("")

        # 20. 下一步建议
        lines.append("## 20. 下一步建议")
        lines.append("")
        suggestions = self._generate_suggestions(report)
        for suggestion in suggestions:
            lines.append(f"- {suggestion}")
        lines.append("")

        # 21. 免责声明
        lines.append("## 21. 免责声明")
        lines.append("")
        lines.append(report.disclaimer)
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 供应链树形渲染
    # ------------------------------------------------------------------

    def _render_chain_tree(self, chain: DynamicSupplyChain) -> str:
        """将供应链渲染为缩进文本树.

        按层级分组（terminal → module → component → material →
        equipment → testing → consumable），每层缩进递增。

        Args:
            chain: 动态供应链实例。

        Returns:
            缩进文本树字符串。
        """
        if not chain or not chain.nodes:
            return "*空供应链*"

        # 按层级分组
        layer_groups: Dict[str, List[str]] = {}
        for node in chain.nodes:
            layer = node.layer or "unknown"
            label = node.name
            if node.is_anchor:
                label += " [锚点]"
            if node.is_potential_bottleneck:
                label += " [潜在瓶颈]"
            layer_groups.setdefault(layer, []).append(label)

        lines: List[str] = []
        for idx, layer in enumerate(_LAYER_ORDER):
            if layer not in layer_groups:
                continue
            indent = "  " * idx
            layer_cn = _LAYER_DISPLAY.get(layer, layer)
            lines.append(f"{indent}{layer_cn}:")
            for node_label in layer_groups[layer]:
                lines.append(f"{indent}  - {node_label}")

        # 处理不在预定义顺序中的层级
        for layer, names in layer_groups.items():
            if layer in _LAYER_ORDER:
                continue
            indent = "  " * len(_LAYER_ORDER)
            layer_cn = _LAYER_DISPLAY.get(layer, layer)
            lines.append(f"{indent}{layer_cn}:")
            for node_label in names:
                lines.append(f"{indent}  - {node_label}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 格式化辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _format_evidence_level(level: str) -> str:
        """将证据等级映射为中文显示文本.

        Args:
            level: 证据等级字符串。

        Returns:
            中文显示文本，未知等级原样返回。
        """
        return _EVIDENCE_LEVEL_DISPLAY.get(level, level)

    @staticmethod
    def _format_classification(cls: str) -> str:
        """将分类标签映射为中文显示文本.

        Args:
            cls: 分类标签字符串。

        Returns:
            中文显示文本，未知分类原样返回。
        """
        return _CLASSIFICATION_DISPLAY.get(cls, cls)

    # ------------------------------------------------------------------
    # 中间产物导出
    # ------------------------------------------------------------------

    def _export_intermediate(
        self,
        report: PerillaDiscoveryReport,
        output_dir: str,
    ) -> None:
        """导出各中间产物为 JSON 和 Markdown 文件.

        Args:
            report: 紫苏发现报告实例。
            output_dir: 输出目录路径。
        """
        # 概念信号
        self._export_intermediate_one(
            output_dir=output_dir,
            name="concept_signals",
            data=[asdict(cs) for cs in report.concept_signals],
            md_renderer=lambda: self._render_concept_signals_md(report.concept_signals),
        )

        # 来源文档（仅统计摘要）
        source_doc_count = self._count_source_docs(report)
        self._export_intermediate_one(
            output_dir=output_dir,
            name="source_documents",
            data=source_doc_count,
            md_renderer=lambda: self._render_source_docs_md(source_doc_count),
        )

        # 动态供应链
        self._export_intermediate_one(
            output_dir=output_dir,
            name="dynamic_chains",
            data=[asdict(ch) for ch in report.dynamic_chains],
            md_renderer=lambda: self._render_chains_md(report.dynamic_chains),
        )

        # 瓶颈假设
        self._export_intermediate_one(
            output_dir=output_dir,
            name="bottleneck_hypotheses",
            data=[asdict(bh) for bh in report.bottleneck_hypotheses],
            md_renderer=lambda: self._render_bottlenecks_md(report.bottleneck_hypotheses),
        )

        # 公司候选
        self._export_intermediate_one(
            output_dir=output_dir,
            name="company_candidates",
            data=[asdict(cc) for cc in report.company_candidates],
            md_renderer=lambda: self._render_candidates_md(report.company_candidates),
        )

        # 紫苏发现条目
        self._export_intermediate_one(
            output_dir=output_dir,
            name="perilla_discovery_items",
            data=[asdict(it) for it in report.discovery_items],
            md_renderer=lambda: self._render_items_md(report.discovery_items),
        )

    def _export_intermediate_one(
        self,
        output_dir: str,
        name: str,
        data: Any,
        md_renderer: Any,
    ) -> None:
        """导出单个中间产物.

        Args:
            output_dir: 输出目录路径。
            name: 文件名前缀。
            data: 要序列化为 JSON 的数据。
            md_renderer: 返回 Markdown 字符串的可调用对象。
        """
        json_path = os.path.join(output_dir, f"{name}_latest.json")
        md_path = os.path.join(output_dir, f"{name}_latest.md")

        self._safe_write_json(json_path, data)
        self._safe_write_text(md_path, md_renderer())

    # ------------------------------------------------------------------
    # 中间产物 Markdown 渲染
    # ------------------------------------------------------------------

    @staticmethod
    def _render_concept_signals_md(signals: List[ConceptSignal]) -> str:
        """渲染概念信号 Markdown.

        Args:
            signals: 概念信号列表。

        Returns:
            Markdown 字符串。
        """
        lines = ["# 概念信号", ""]
        if not signals:
            lines.append("*暂无概念信号*")
            return "\n".join(lines)

        lines.append("| 概念名 | 热度 | 新颖度 | 置信度 | 来源数 |")
        lines.append("|--------|------|--------|--------|--------|")
        for cs in signals:
            lines.append(
                f"| {cs.name} | {cs.heat_score:.2f} | "
                f"{cs.novelty_score:.2f} | {cs.confidence} | "
                f"{len(cs.detected_from)} |"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_source_docs_md(count_data: Dict[str, Any]) -> str:
        """渲染来源文档统计 Markdown.

        Args:
            count_data: 来源文档统计数据。

        Returns:
            Markdown 字符串。
        """
        lines = ["# 来源文档统计", ""]
        if not count_data:
            lines.append("*暂无来源文档*")
            return "\n".join(lines)

        for key, value in count_data.items():
            lines.append(f"- **{key}**: {value}")
        return "\n".join(lines)

    def _render_chains_md(self, chains: List[DynamicSupplyChain]) -> str:
        """渲染动态供应链 Markdown.

        Args:
            chains: 动态供应链列表。

        Returns:
            Markdown 字符串。
        """
        lines = ["# 动态供应链", ""]
        if not chains:
            lines.append("*暂无动态供应链*")
            return "\n".join(lines)

        for chain in chains:
            lines.append(f"## {chain.chain_id}")
            lines.append(f"- 构建方法: {chain.construction_method}")
            lines.append(f"- 节点数: {len(chain.nodes)}")
            lines.append(f"- 边数: {len(chain.edges)}")
            lines.append(f"- 置信度: {chain.confidence}")
            lines.append("")
            lines.append(self._render_chain_tree(chain))
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_bottlenecks_md(hypotheses: List[BottleneckHypothesis]) -> str:
        """渲染瓶颈假设 Markdown.

        Args:
            hypotheses: 瓶颈假设列表。

        Returns:
            Markdown 字符串。
        """
        lines = ["# 瓶颈假设", ""]
        if not hypotheses:
            lines.append("*暂无瓶颈假设*")
            return "\n".join(lines)

        lines.append("| 环节 | 信号数 | 六问总分 | 紫苏概率 | 状态 |")
        lines.append("|------|--------|----------|----------|------|")
        for bh in hypotheses:
            lines.append(
                f"| {bh.node_name} | {len(bh.bottleneck_signals)} | "
                f"{bh.total_score} | {bh.perilla_probability:.2%} | "
                f"{bh.status} |"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_candidates_md(candidates: List[CompanyCandidate]) -> str:
        """渲染公司候选 Markdown.

        Args:
            candidates: 公司候选列表。

        Returns:
            Markdown 字符串。
        """
        lines = ["# 公司候选", ""]
        if not candidates:
            lines.append("*暂无公司候选*")
            return "\n".join(lines)

        lines.append("| 公司 | 市场 | 代码 | 证据等级 | 待复核 |")
        lines.append("|------|------|------|----------|--------|")
        for cc in candidates:
            lines.append(
                f"| {cc.company_name} | {cc.market} | "
                f"{cc.symbol or '-'} | {cc.evidence_level} | "
                f"{'是' if cc.needs_human_review else '否'} |"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_items_md(items: List[PerillaDiscoveryItem]) -> str:
        """渲染紫苏发现条目 Markdown.

        Args:
            items: 发现条目列表。

        Returns:
            Markdown 字符串。
        """
        lines = ["# 紫苏发现条目", ""]
        if not items:
            lines.append("*暂无发现条目*")
            return "\n".join(lines)

        for it in items:
            lines.append(f"## {it.company_name}")
            lines.append(f"- **分类**: {it.classification}")
            lines.append(f"- **证据等级**: {it.evidence_level}")
            lines.append(f"- **紫苏概率**: {it.perilla_probability:.2%}")
            lines.append(f"- **审核状态**: {it.review_status}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _report_to_dict(report: PerillaDiscoveryReport) -> Dict[str, Any]:
        """将报告转换为可序列化字典.

        Args:
            report: 紫苏发现报告实例。

        Returns:
            可 JSON 序列化的字典。
        """
        return asdict(report)

    @staticmethod
    def _collect_evidence_by_types(
        report: PerillaDiscoveryReport,
        source_types: set,
    ) -> List[str]:
        """从公司候选和发现条目中收集指定来源类型的证据摘要.

        Args:
            report: 紫苏发现报告实例。
            source_types: 来源类型集合。

        Returns:
            证据摘要字符串列表。
        """
        evidence_list: List[str] = []
        seen: set = set()

        for cc in report.company_candidates:
            for src in cc.mapping_sources:
                src_lower = src.lower()
                if any(st in src_lower for st in source_types):
                    key = f"{cc.company_name}:{src[:60]}"
                    if key not in seen:
                        seen.add(key)
                        evidence_list.append(
                            f"**{cc.company_name}**: {src[:100]}"
                        )

        # 从发现条目中补充
        for it in report.discovery_items:
            if it.evidence_summary and it.evidence_summary != "暂无本地证据":
                for url in it.source_urls:
                    key = f"{it.company_name}:{url}"
                    if key not in seen:
                        seen.add(key)
                        evidence_list.append(
                            f"**{it.company_name}**: {it.evidence_summary[:100]}"
                        )

        return evidence_list

    @staticmethod
    def _count_source_docs(report: PerillaDiscoveryReport) -> Dict[str, Any]:
        """统计来源文档数量.

        Args:
            report: 紫苏发现报告实例。

        Returns:
            来源统计数据字典。
        """
        stats: Dict[str, Any] = dict(report.source_stats or {})

        # 从公司候选中统计映射来源类型
        source_type_counts: Dict[str, int] = {}
        for cc in report.company_candidates:
            for src in cc.mapping_sources:
                source_type_counts[src] = source_type_counts.get(src, 0) + 1

        stats["company_mapping_source_types"] = source_type_counts
        stats["total_concept_signals"] = len(report.concept_signals)
        stats["total_demand_anchors"] = len(report.demand_anchors)
        stats["total_chains"] = len(report.dynamic_chains)
        stats["total_bottleneck_hypotheses"] = len(report.bottleneck_hypotheses)
        stats["total_company_candidates"] = len(report.company_candidates)
        stats["total_discovery_items"] = len(report.discovery_items)
        stats["total_review_queue"] = len(report.review_queue)

        return stats

    @staticmethod
    def _build_item_lookup(
        items: List[PerillaDiscoveryItem],
    ) -> Dict[str, PerillaDiscoveryItem]:
        """构建公司候选 ID → 发现条目的查找表.

        由于 PerillaDiscoveryItem 没有 candidate_id 字段，
        通过 company_name 进行模糊匹配。

        Args:
            items: 发现条目列表。

        Returns:
            以 company_name 为键的查找字典。
        """
        lookup: Dict[str, PerillaDiscoveryItem] = {}
        for it in items:
            lookup[it.company_name] = it
        return lookup

    @staticmethod
    def _generate_suggestions(report: PerillaDiscoveryReport) -> List[str]:
        """根据报告内容生成下一步建议.

        Args:
            report: 紫苏发现报告实例。

        Returns:
            建议字符串列表。
        """
        suggestions: List[str] = []

        # 紫苏候选建议
        perilla_count = sum(
            1 for it in report.discovery_items
            if it.classification == "PERILLA_CANDIDATE"
        )
        if perilla_count > 0:
            suggestions.append(
                f"有 {perilla_count} 个紫苏叶候选，建议优先验证其供应关系"
            )

        # 待复核建议
        review_count = len(report.review_queue)
        if review_count > 0:
            suggestions.append(
                f"有 {review_count} 条待人工复核记录，建议按优先级逐一审核"
            )

        # 早期信号建议
        early_count = sum(
            1 for it in report.discovery_items
            if it.classification == "EARLY_SIGNAL_WATCH"
        )
        if early_count > 0:
            suggestions.append(
                f"有 {early_count} 个早期信号观察条目，建议持续跟踪"
            )

        # 假设验证建议
        hypothesis_count = sum(
            1 for it in report.discovery_items
            if it.classification == "HYPOTHESIS_ONLY"
        )
        if hypothesis_count > 0:
            suggestions.append(
                f"有 {hypothesis_count} 个纯假设条目，建议寻找外部证据验证"
            )

        # 概念候选建议
        concept_claim_count = sum(
            1 for it in report.discovery_items
            if it.classification == "CONCEPT_CLAIM_ONLY"
        )
        if concept_claim_count > 0:
            suggestions.append(
                f"有 {concept_claim_count} 个仅概念声明条目，建议排除蹭概念"
            )

        if not suggestions:
            suggestions.append("本次发现无特别需要关注的事项")

        return suggestions

    # ------------------------------------------------------------------
    # 安全文件写入
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_write_json(path: str, data: Any) -> None:
        """安全写入 JSON 文件，失败时仅记录日志不抛异常.

        Args:
            path: 文件路径。
            data: 要序列化的数据。
        """
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as exc:
            logger.error("写入 JSON 文件失败 %s: %s", path, exc)

    @staticmethod
    def _safe_write_text(path: str, content: str) -> None:
        """安全写入文本文件，失败时仅记录日志不抛异常.

        Args:
            path: 文件路径。
            content: 文本内容。
        """
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:
            logger.error("写入文本文件失败 %s: %s", path, exc)
