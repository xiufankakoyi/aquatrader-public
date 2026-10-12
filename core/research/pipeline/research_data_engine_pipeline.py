"""Research Data Engine Pipeline — 11 步主流程。

Usage: python -m core.research.pipeline.research_data_engine_pipeline
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)


def _safe_symbols(symbols: Any) -> list[str]:
    if not isinstance(symbols, (list, tuple, set)):
        return []
    return list(dict.fromkeys(
        item.strip() for item in symbols
        if isinstance(item, str) and item.strip()
    ))


def _safe_trade_date(value: Any) -> str:
    candidate = str(value or "")[:10]
    try:
        return datetime.strptime(candidate, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d")


def _event_value(event: Any, name: str, default: Any = "") -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


class ResearchDataEnginePipeline:
    """11 步主流程管线。

    Steps:
    1. Probe Providers — 探测数据提供者状态
    2. MultiSource Ingestion — 多源数据采集
    3. EventBus publish — 发布原始事件
    4. SmallBatchProcessor — 小批量抽取处理
    5. Evidence Center update — 更新证据中心
    6. Entity Registry build — 构建实体注册表
    7. Evidence Graph build — 构建证据图谱
    8. QuantFlow run — 运行量化流水线（可选）
    9. Fast Validation Loop run — 运行快速验证循环
    10. Virtual Research Desk run — 运行虚拟投研台（可选）
    11. Final brief — 生成最终简报
    """

    def __init__(self) -> None:
        """初始化管线。"""
        from core.research.engine.research_engine import ResearchEngine

        self.engine = ResearchEngine()
        self.report_dir = PROJECT_ROOT / "data" / "reports"
        self._step_results: Dict[str, Dict[str, Any]] = {}

    def run(
        self,
        trade_date: Optional[str] = None,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """运行完整的 11 步管线。

        每步捕获异常并继续。单个 Provider 失败不阻塞管线。
        无 LLM API 不阻塞管线。无 iwencai key 不阻塞管线。
        无新闻/研报时输出显式空状态，永不崩溃。

        Args:
            trade_date: 交易日期，默认今天
            symbols: 股票代码列表

        Returns:
            包含所有步骤结果的摘要字典
        """
        self._step_results = {}
        start_time = datetime.now()
        trade_date = _safe_trade_date(trade_date or start_time.strftime("%Y-%m-%d"))
        symbols = _safe_symbols(symbols)

        logger.info("Research Data Engine Pipeline 启动: trade_date=%s", trade_date)
        print(f"\n[ResearchDataEngine] Pipeline 启动: trade_date={trade_date}\n")

        # Step 1: Probe Providers
        print("[Step 1/11] 探测数据提供者...")
        self._step_results["probe_providers"] = self._step_probe_providers()

        # Step 2: MultiSource Ingestion
        print("[Step 2/11] 多源数据采集...")
        self._step_results["ingestion"] = self._step_ingestion(trade_date, symbols)

        # Step 3: EventBus publish
        print("[Step 3/11] 发布原始事件...")
        self._step_results["event_bus_publish"] = self._step_event_bus_publish(
            self._step_results["ingestion"]
        )

        # Step 4: SmallBatchProcessor
        print("[Step 4/11] 小批量抽取处理...")
        self._step_results["extraction"] = self._step_extraction(
            self._step_results["ingestion"]
        )

        # Step 5: Evidence Center update
        print("[Step 5/11] 更新证据中心...")
        self._step_results["evidence_update"] = self._step_evidence_update(
            self._step_results["ingestion"],
            self._step_results["extraction"],
            trade_date,
        )

        # Step 6: Entity Registry build
        print("[Step 6/11] 构建实体注册表...")
        self._step_results["entity_build"] = self._step_entity_build(
            self._step_results["extraction"]
        )

        # Step 7: Evidence Graph build
        print("[Step 7/11] 构建证据图谱...")
        self._step_results["graph_build"] = self._step_graph_build(
            self._step_results["extraction"]
        )

        # Step 8: QuantFlow run (optional)
        print("[Step 8/11] 运行量化流水线（可选）...")
        self._step_results["quantflow"] = self._step_quantflow(trade_date)

        # Step 9: Fast Validation Loop run
        print("[Step 9/11] 运行快速验证循环...")
        self._step_results["validation"] = self._step_validation(symbols, trade_date)

        # Step 10: Virtual Research Desk run (optional)
        print("[Step 10/11] 运行虚拟投研台（可选）...")
        self._step_results["vrd"] = self._step_vrd(trade_date)

        # Step 11: Final brief
        print("[Step 11/11] 生成最终简报...")
        self._step_results["final_brief"] = self._step_final_brief(
            self._step_results, trade_date
        )

        # 汇总
        elapsed = (datetime.now() - start_time).total_seconds()
        optional_steps = {"quantflow", "vrd"}
        has_error = any(
            name not in optional_steps and result.get("status") == "error"
            for name, result in self._step_results.items()
        )

        result: Dict[str, Any] = {
            "success": not has_error,
            "trade_date": trade_date,
            "elapsed_seconds": round(elapsed, 1),
            "steps": self._step_results,
            "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

        # 导出报告
        self.export_report(result)

        print(f"\n[ResearchDataEngine] Pipeline 完成: elapsed={elapsed:.1f}s, "
              f"success={result['success']}\n")

        return result

    # ------------------------------------------------------------------
    # 11 步实现
    # ------------------------------------------------------------------

    def _step_probe_providers(self) -> Dict[str, Any]:
        """Step 1: 探测数据提供者状态。

        Returns:
            探测结果
        """
        try:
            health = self.engine.registry.health_check_all()
            available = self.engine.registry.get_available_providers()

            providers_info: Dict[str, Any] = {}
            for name, status in health.items():
                providers_info[name] = {
                    "enabled": status.enabled,
                    "status": status.status,
                    "capabilities": status.capabilities,
                    "last_error": status.last_error,
                }

            return {
                "status": "ok",
                "total_providers": len(health),
                "available_providers": available,
                "providers": providers_info,
            }
        except Exception as exc:
            logger.error("探测 Provider 失败: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc), "providers": {}}

    def _step_ingestion(
        self,
        trade_date: str,
        symbols: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Step 2: 多源数据采集。

        Args:
            trade_date: 交易日期
            symbols: 股票代码列表

        Returns:
            采集结果
        """
        try:
            result = self.engine.ingestion.collect_all(
                trade_date=trade_date,
                symbols=symbols or [],
            )
            return {
                "status": "ok",
                "raw_event_count": len(result.get("raw_events", [])),
                "normalized_event_count": len(result.get("normalized_events", [])),
                "document_count": len(result.get("documents", [])),
                "dedup_stats": result.get("dedup_stats", {}),
                "provider_results": result.get("provider_results", {}),
                "errors": result.get("errors", []),
                "normalized_events": result.get("normalized_events", []),
            }
        except Exception as exc:
            logger.error("多源采集失败: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc), "normalized_events": []}

    def _step_event_bus_publish(
        self,
        ingestion_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Step 3: 发布原始事件到 EventBus。

        Args:
            ingestion_result: 采集结果

        Returns:
            发布结果
        """
        try:
            from core.events.models import EventEnvelope, EnvelopeType

            normalized_events = ingestion_result.get("normalized_events", [])
            if not isinstance(normalized_events, (list, tuple)):
                normalized_events = []
            published_count = 0

            for event in normalized_events:
                try:
                    payload = (
                        asdict(event)
                        if hasattr(event, "__dataclass_fields__")
                        else dict(event)
                        if isinstance(event, Mapping)
                        else {}
                    )
                    envelope = EventEnvelope(
                        event_type=EnvelopeType.NORMALIZED_EVENT,
                        topic=f"data.{_event_value(event, 'entity_type', 'stock')}",
                        source="research_data_engine_pipeline",
                        payload=payload,
                    )
                    self.engine.event_bus.publish(envelope)

                    # 路由
                    self.engine.event_router.route(envelope)

                    published_count += 1
                except Exception as exc:
                    logger.debug("发布事件失败: %s", exc)

            return {
                "status": "ok",
                "published_count": published_count,
                "total_events": len(normalized_events),
            }
        except Exception as exc:
            logger.error("事件发布失败: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc), "published_count": 0}

    def _step_extraction(
        self,
        ingestion_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Step 4: 小批量抽取处理。

        Args:
            ingestion_result: 采集结果

        Returns:
            抽取结果
        """
        try:
            normalized_events = ingestion_result.get("normalized_events", [])
            if not normalized_events:
                return {
                    "status": "ok",
                    "extraction_count": 0,
                    "results": [],
                    "note": "无归一化事件需要处理",
                }

            results = self.engine.processor.process_batch(normalized_events)

            return {
                "status": "ok",
                "extraction_count": len(results),
                "results": results,
            }
        except Exception as exc:
            logger.error("抽取处理失败: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc), "extraction_count": 0, "results": []}

    def _step_evidence_update(
        self,
        ingestion_result: Dict[str, Any],
        extraction_result: Dict[str, Any],
        trade_date: str,
    ) -> Dict[str, Any]:
        """Step 5: 更新证据中心。

        Args:
            ingestion_result: 采集结果
            extraction_result: 抽取结果
            trade_date: 交易日期

        Returns:
            证据更新结果
        """
        try:
            before_count = len(self.engine.evidence_center.evidences)

            # 从抽取结果更新证据
            extraction_results = extraction_result.get("results", [])
            self.engine._update_evidence_from_extraction(extraction_results, trade_date)

            # 扩容采集
            self.engine.evidence_center.collect_from_industry_chain_if_available(trade_date)
            self.engine.evidence_center.collect_from_finance_radar_if_available(trade_date)

            after_count = len(self.engine.evidence_center.evidences)

            # 标记 stale
            stale_count = self.engine.evidence_center.mark_stale(trade_date)

            # 导出
            export = self.engine.evidence_center.export()

            return {
                "status": "ok",
                "before_count": before_count,
                "after_count": after_count,
                "new_count": after_count - before_count,
                "stale_count": stale_count,
                "export": export,
            }
        except Exception as exc:
            logger.error("证据更新失败: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc)}

    def _step_entity_build(
        self,
        extraction_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Step 6: 构建实体注册表。

        Args:
            extraction_result: 抽取结果

        Returns:
            实体构建结果
        """
        try:
            before_count = self.engine.entity_registry.count()

            extraction_results = extraction_result.get("results", [])
            self.engine._build_graph_from_extraction(extraction_results)

            after_count = self.engine.entity_registry.count()

            # 按类型统计
            type_counts: Dict[str, int] = {}
            for entity in self.engine.entity_registry.all_entities():
                t = entity.entity_type
                type_counts[t] = type_counts.get(t, 0) + 1

            return {
                "status": "ok",
                "before_count": before_count,
                "after_count": after_count,
                "new_count": after_count - before_count,
                "type_counts": type_counts,
            }
        except Exception as exc:
            logger.error("实体构建失败: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc)}

    def _step_graph_build(
        self,
        extraction_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Step 7: 构建证据图谱。

        Args:
            extraction_result: 抽取结果

        Returns:
            图谱构建结果
        """
        try:
            before_count = self.engine.evidence_graph.relation_count()

            # 图谱已在 _step_entity_build 中通过 _build_graph_from_extraction 构建
            # 这里只做统计
            after_count = self.engine.evidence_graph.relation_count()

            # 按关系类型统计
            type_counts: Dict[str, int] = {}
            for rel in self.engine.evidence_graph.all_relations():
                t = rel.relation_type
                type_counts[t] = type_counts.get(t, 0) + 1

            return {
                "status": "ok",
                "before_count": before_count,
                "after_count": after_count,
                "new_count": after_count - before_count,
                "type_counts": type_counts,
            }
        except Exception as exc:
            logger.error("图谱构建失败: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc)}

    def _step_quantflow(self, trade_date: str) -> Dict[str, Any]:
        """Step 8: 运行量化流水线（可选）。

        如果 QuantFlow 不可用则跳过，不阻塞管线。

        Args:
            trade_date: 交易日期

        Returns:
            QuantFlow 运行结果
        """
        try:
            from core.research.pipeline.quant_flow_pipeline import QuantFlowPipeline

            qf = QuantFlowPipeline()
            report = qf.run(write_files=True)

            return {
                "status": "ok",
                "qf_status": report.get("status", "unknown"),
                "global_latest_trade_date": report.get("global_latest_trade_date", ""),
                "stages_count": len(report.get("stages", [])),
            }
        except ImportError:
            return {"status": "skipped", "reason": "QuantFlowPipeline 不可用"}
        except Exception as exc:
            logger.warning("QuantFlow 运行失败（可选步骤）: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc), "reason": "可选步骤，不影响管线"}

    def _step_validation(
        self,
        symbols: Optional[List[str]],
        trade_date: str,
    ) -> Dict[str, Any]:
        """Step 9: 运行快速验证循环。

        Args:
            symbols: 股票代码列表
            trade_date: 交易日期

        Returns:
            验证结果
        """
        try:
            # 确定验证目标
            if symbols:
                target_symbols = symbols
            else:
                # 从证据中心提取所有涉及的股票
                target_symbols = list({
                    sym
                    for ev in self.engine.evidence_center.evidences
                    for sym in (
                        _event_value(ev, "symbols", [])
                        if isinstance(_event_value(ev, "symbols", []), (list, tuple, set))
                        else []
                    )
                    if isinstance(sym, str) and sym
                })

            if not target_symbols:
                return {
                    "status": "ok",
                    "validation_count": 0,
                    "results": [],
                    "note": "无股票需要验证",
                }

            # 最多验证 50 只
            target_symbols = target_symbols[:50]

            results = self.engine.validation_loop.validate_batch(
                target_symbols, trade_date
            )

            # 导出验证报告
            export = self.engine.validation_loop.export_report(results, trade_date)

            # 统计
            decision_counts: Dict[str, int] = {}
            for r in results if isinstance(results, (list, tuple)) else []:
                if not isinstance(r, Mapping):
                    continue
                d = r.get("decision", "UNKNOWN")
                decision_counts[d] = decision_counts.get(d, 0) + 1

            return {
                "status": "ok",
                "validation_count": len(results),
                "decision_counts": decision_counts,
                "export": export,
            }
        except Exception as exc:
            logger.error("验证循环失败: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc), "validation_count": 0}

    def _step_vrd(self, trade_date: str) -> Dict[str, Any]:
        """Step 10: 运行虚拟投研台（可选）。

        如果 VRD 不可用则跳过，不阻塞管线。

        Args:
            trade_date: 交易日期

        Returns:
            VRD 运行结果
        """
        try:
            from core.research.pipeline.virtual_research_desk_pipeline import run_pipeline

            report = run_pipeline(write_files=True)

            return {
                "status": "ok",
                "final_output_level": report.get("final_output_level", "UNKNOWN"),
                "candidate_stock_count": report.get("candidate_stock_count", 0),
            }
        except ImportError:
            return {"status": "skipped", "reason": "VirtualResearchDesk 不可用"}
        except Exception as exc:
            logger.warning("VRD 运行失败（可选步骤）: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc), "reason": "可选步骤，不影响管线"}

    def _step_final_brief(
        self,
        all_results: Dict[str, Dict[str, Any]],
        trade_date: str,
    ) -> Dict[str, Any]:
        """Step 11: 生成最终简报。

        Args:
            all_results: 所有步骤结果
            trade_date: 交易日期

        Returns:
            最终简报
        """
        # 汇总各步骤状态
        step_statuses = {
            name: result.get("status", "unknown")
            for name, result in all_results.items()
        }

        # 统计
        ok_count = sum(1 for s in step_statuses.values() if s == "ok")
        error_count = sum(1 for s in step_statuses.values() if s == "error")
        skipped_count = sum(1 for s in step_statuses.values() if s == "skipped")

        # 证据统计
        evidence_count = len(self.engine.evidence_center.evidences)
        entity_count = self.engine.entity_registry.count()
        relation_count = self.engine.evidence_graph.relation_count()

        # 验证统计
        validation = all_results.get("validation", {})
        validation_count = validation.get("validation_count", 0)
        decision_counts = validation.get("decision_counts", {})

        # 采集统计
        ingestion = all_results.get("ingestion", {})
        raw_count = ingestion.get("raw_event_count", 0)
        normalized_count = ingestion.get("normalized_event_count", 0)

        # 抽取统计
        extraction = all_results.get("extraction", {})
        extraction_count = extraction.get("extraction_count", 0)

        # 整体状态
        overall_status = "ok"
        if error_count > 0:
            # 可选步骤的错误不影响整体状态
            required_steps = {"probe_providers", "ingestion", "evidence_update"}
            required_errors = sum(
                1 for name in required_steps
                if all_results.get(name, {}).get("status") == "error"
            )
            if required_errors > 0:
                overall_status = "error"
            else:
                overall_status = "warning"

        brief: Dict[str, Any] = {
            "status": overall_status,
            "trade_date": trade_date,
            "step_statuses": step_statuses,
            "summary": {
                "ok_steps": ok_count,
                "error_steps": error_count,
                "skipped_steps": skipped_count,
                "raw_events": raw_count,
                "normalized_events": normalized_count,
                "extraction_results": extraction_count,
                "evidence_count": evidence_count,
                "entity_count": entity_count,
                "relation_count": relation_count,
                "validation_count": validation_count,
                "decision_counts": decision_counts,
            },
            "errors": [
                {"step": name, "error": result.get("error", "")}
                for name, result in all_results.items()
                if result.get("status") == "error"
            ],
        }

        return brief

    def export_report(self, results: Dict[str, Any]) -> None:
        """导出管线报告。

        输出到:
        - data/reports/research_data_engine_latest.json
        - data/reports/research_data_engine_latest.md
        - data/reports/research_data_engine_v1_report.md

        Args:
            results: 管线运行结果
        """
        self.report_dir.mkdir(parents=True, exist_ok=True)

        # JSON 报告
        json_path = self.report_dir / "research_data_engine_latest.json"
        try:
            # 序列化时排除不可序列化的对象
            serializable = self._make_serializable(results)
            json_path.write_text(
                json.dumps(serializable, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("导出 JSON 报告失败: %s", exc)

        # Markdown 报告
        md_path = self.report_dir / "research_data_engine_latest.md"
        try:
            md_content = self._format_md_report(results)
            md_path.write_text(md_content, encoding="utf-8")
        except Exception as exc:
            logger.warning("导出 Markdown 报告失败: %s", exc)

        # v1 报告
        v1_path = self.report_dir / "research_data_engine_v1_report.md"
        try:
            v1_content = self._format_v1_report(results)
            v1_path.write_text(v1_content, encoding="utf-8")
        except Exception as exc:
            logger.warning("导出 v1 报告失败: %s", exc)

    # ------------------------------------------------------------------
    # 报告格式化
    # ------------------------------------------------------------------

    @staticmethod
    def _format_md_report(results: Dict[str, Any]) -> str:
        """格式化 Markdown 报告。

        Args:
            results: 管线运行结果

        Returns:
            Markdown 格式字符串
        """
        trade_date = results.get("trade_date", "")
        brief = results.get("steps", {}).get("final_brief", {})
        summary = brief.get("summary", {})

        lines = [
            f"# Research Data Engine Pipeline 报告 — {trade_date}",
            "",
            f"- 生成时间: {results.get('generated_at', '')}",
            f"- 总体状态: {brief.get('status', 'unknown')}",
            f"- 耗时: {results.get('elapsed_seconds', 0):.1f} 秒",
            "",
            "## 步骤状态",
            "",
            "| 步骤 | 状态 |",
            "| --- | --- |",
        ]

        step_names = {
            "probe_providers": "1. 探测 Provider",
            "ingestion": "2. 多源采集",
            "event_bus_publish": "3. 事件发布",
            "extraction": "4. 抽取处理",
            "evidence_update": "5. 证据更新",
            "entity_build": "6. 实体构建",
            "graph_build": "7. 图谱构建",
            "quantflow": "8. QuantFlow（可选）",
            "validation": "9. 验证循环",
            "vrd": "10. 虚拟投研台（可选）",
            "final_brief": "11. 最终简报",
        }

        steps = results.get("steps", {})
        for key, display_name in step_names.items():
            step = steps.get(key, {})
            status = step.get("status", "unknown")
            status_icon = {
                "ok": "OK",
                "error": "ERROR",
                "skipped": "SKIPPED",
                "warning": "WARNING",
            }.get(status, status)
            lines.append(f"| {display_name} | {status_icon} |")

        lines.extend([
            "",
            "## 数据统计",
            "",
            f"- 原始事件: {summary.get('raw_events', 0)}",
            f"- 归一化事件: {summary.get('normalized_events', 0)}",
            f"- 抽取结果: {summary.get('extraction_results', 0)}",
            f"- 证据总数: {summary.get('evidence_count', 0)}",
            f"- 实体总数: {summary.get('entity_count', 0)}",
            f"- 关系总数: {summary.get('relation_count', 0)}",
            f"- 验证股票数: {summary.get('validation_count', 0)}",
        ])

        decision_counts = summary.get("decision_counts", {})
        if decision_counts:
            lines.append("")
            lines.append("## 验证决策分布")
            lines.append("")
            for decision, count in decision_counts.items():
                lines.append(f"- {decision}: {count}")

        errors = brief.get("errors", [])
        if errors:
            lines.extend(["", "## 错误", ""])
            for err in errors:
                lines.append(f"- **{err.get('step', '')}**: {err.get('error', '')}")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _format_v1_report(results: Dict[str, Any]) -> str:
        """格式化 v1 版本报告。

        Args:
            results: 管线运行结果

        Returns:
            Markdown 格式字符串
        """
        trade_date = results.get("trade_date", "")
        brief = results.get("steps", {}).get("final_brief", {})
        summary = brief.get("summary", {})

        lines = [
            f"# Research Data Engine v1 Report",
            "",
            f"**Date**: {trade_date}",
            f"**Status**: {brief.get('status', 'unknown')}",
            f"**Elapsed**: {results.get('elapsed_seconds', 0):.1f}s",
            "",
            "## Pipeline Steps",
            "",
        ]

        steps = results.get("steps", {})
        step_order = [
            "probe_providers", "ingestion", "event_bus_publish",
            "extraction", "evidence_update", "entity_build",
            "graph_build", "quantflow", "validation", "vrd",
            "final_brief",
        ]

        for step_name in step_order:
            step = steps.get(step_name, {})
            status = step.get("status", "unknown")
            lines.append(f"### {step_name}")
            lines.append(f"- Status: `{status}`")
            if step.get("error"):
                lines.append(f"- Error: {step['error']}")
            if step_name == "probe_providers":
                providers = step.get("providers", {})
                lines.append(f"- Total providers: {step.get('total_providers', 0)}")
                lines.append(f"- Available: {step.get('available_providers', [])}")
                for pname, pinfo in providers.items():
                    lines.append(
                        f"  - {pname}: enabled={pinfo.get('enabled')}, "
                        f"status={pinfo.get('status')}"
                    )
            elif step_name == "ingestion":
                lines.append(f"- Raw events: {step.get('raw_event_count', 0)}")
                lines.append(f"- Normalized events: {step.get('normalized_event_count', 0)}")
            elif step_name == "extraction":
                lines.append(f"- Extraction count: {step.get('extraction_count', 0)}")
            elif step_name == "evidence_update":
                lines.append(f"- Before: {step.get('before_count', 0)}")
                lines.append(f"- After: {step.get('after_count', 0)}")
                lines.append(f"- New: {step.get('new_count', 0)}")
                lines.append(f"- Stale: {step.get('stale_count', 0)}")
            elif step_name == "entity_build":
                lines.append(f"- Entity count: {step.get('after_count', 0)}")
                type_counts = step.get("type_counts", {})
                if type_counts:
                    for t, c in type_counts.items():
                        lines.append(f"  - {t}: {c}")
            elif step_name == "graph_build":
                lines.append(f"- Relation count: {step.get('after_count', 0)}")
                type_counts = step.get("type_counts", {})
                if type_counts:
                    for t, c in type_counts.items():
                        lines.append(f"  - {t}: {c}")
            elif step_name == "validation":
                lines.append(f"- Validated: {step.get('validation_count', 0)}")
                dc = step.get("decision_counts", {})
                if dc:
                    for d, c in dc.items():
                        lines.append(f"  - {d}: {c}")
            lines.append("")

        lines.extend([
            "## Summary",
            "",
            f"- Evidence: {summary.get('evidence_count', 0)}",
            f"- Entities: {summary.get('entity_count', 0)}",
            f"- Relations: {summary.get('relation_count', 0)}",
            f"- Validated stocks: {summary.get('validation_count', 0)}",
            "",
        ])

        return "\n".join(lines)

    @staticmethod
    def _make_serializable(obj: Any) -> Any:
        """将对象转换为可 JSON 序列化的格式。

        Args:
            obj: 待转换对象

        Returns:
            可序列化对象
        """
        if isinstance(obj, Mapping):
            return {
                k: ResearchDataEnginePipeline._make_serializable(v)
                for k, v in obj.items()
            }
        elif isinstance(obj, (list, tuple, set)):
            return [ResearchDataEnginePipeline._make_serializable(v) for v in obj]
        elif hasattr(obj, "__dataclass_fields__"):
            return ResearchDataEnginePipeline._make_serializable(asdict(obj))
        elif isinstance(obj, (str, int, float, bool, type(None))):
            return obj
        else:
            return str(obj)


def main() -> None:
    """命令行入口。"""
    pipeline = ResearchDataEnginePipeline()
    result = pipeline.run()
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
