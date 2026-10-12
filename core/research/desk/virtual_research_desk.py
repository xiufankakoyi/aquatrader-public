"""Virtual Research Desk 编排器 — 五岗顺序执行与报告生成。

v2 改造:
  - 引入 Agent 讨论协议(discussion_protocol),五岗像研究员一样讨论、反驳、权衡
  - 输出 ResearchOpinion 对象,绑定观点/证据/置信度/风险/触发条件
  - 调用 ScenarioSimulator 对重点 candidate 做情景压力测试
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from config.config import Config
from core.research.desk.agents import (
    CIOAgent,
    DataOpsAgent,
    QuantValidationAgent,
    ResearchAgent,
    RiskAgent,
    _read_json,
)
from core.research.desk.discussion_protocol import (
    AgentDebate,
    AgentArgument,
    build_arguments_from_agent_outputs,
)
from core.research.desk.models import AgentOutput, CIOOutput
from core.research.opinion import OpinionConfidenceSynthesizer, ResearchOpinion
from core.research.review.research_review import FastMarketPriceProvider, ReviewEngine
from core.research_graph.scenario_simulator import ScenarioSimulator
from core.research_graph.valuation_bridge import ValuationBridge

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = PROJECT_ROOT / "data" / "reports"

logger = logging.getLogger(__name__)


def _mapping_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


class _FakeNeuron:
    """内存模式下供 ScenarioSimulator 使用的轻量神经元。"""

    def __init__(
        self,
        neuron_id: str,
        canonical_name: str,
        node_type: str,
        confidence: float = 0.0,
    ) -> None:
        self.neuron_id = neuron_id
        self.canonical_name = canonical_name
        self.node_type = node_type
        self.confidence = confidence
        self.is_tombstone = False


class _FakeSynapse:
    """内存模式下供 ScenarioSimulator 使用的轻量突触。"""

    def __init__(
        self,
        synapse_id: str,
        source_neuron_id: str,
        target_neuron_id: str,
        strength: float = 0.5,
        direction: str = "",
        relation: str = "",
    ) -> None:
        self.synapse_id = synapse_id
        self.source_neuron_id = source_neuron_id
        self.target_neuron_id = target_neuron_id
        self.target_kind = "EXISTING_NEURON"
        self.strength = strength
        self.direction = direction
        self.relation = relation
        self.status = "OPEN"


class VirtualResearchDesk:
    """虚拟投研台：编排 DataOps → Research → QuantValidation → Risk → CIO 五岗执行。

    v2 新增:
      - discussion_results: Agent 讨论结果
      - research_opinions: 结构化研究观点
      - scenario_reports: 情景压力测试结果
    """

    def __init__(
        self,
        scenario_simulator: ScenarioSimulator | None = None,
        opinion_synthesizer: OpinionConfidenceSynthesizer | None = None,
        valuation_bridge: ValuationBridge | None = None,
        review_engine: ReviewEngine | None = None,
        price_provider: Any | None = None,
    ) -> None:
        self.dataops_output: AgentOutput | None = None
        self.research_output: AgentOutput | None = None
        self.quant_output: AgentOutput | None = None
        self.risk_output: AgentOutput | None = None
        self.cio_output: CIOOutput | None = None
        self.trade_date: str = ""
        self.scenario_simulator = scenario_simulator
        self.opinion_synthesizer = opinion_synthesizer or OpinionConfidenceSynthesizer()
        self.valuation_bridge = valuation_bridge or ValuationBridge()
        if review_engine is None:
            price_provider = price_provider or FastMarketPriceProvider(db_path=Config.LANCEDB_PATH)
            self.review_engine = ReviewEngine(price_provider=price_provider)
        else:
            self.review_engine = review_engine

    def run(self, trade_date: str = "") -> Dict[str, Any]:
        """执行五岗顺序编排。

        Args:
            trade_date: 全局最新交易日（可选，默认从 QuantFlow 报告读取）

        Returns:
            完整报告字典
        """
        started_at = datetime.now().astimezone()

        # 解析全局最新交易日
        if not trade_date:
            quant_flow = _read_json("quant_flow_latest.json")
            trade_date = str(quant_flow.get("global_latest_trade_date") or "")
        self.trade_date = trade_date

        # 1. DataOps Agent
        self.dataops_output = DataOpsAgent().run(trade_date=trade_date)

        # 2. Research Agent
        self.research_output = ResearchAgent().run(trade_date=trade_date)

        # 3. QuantValidation Agent
        self.quant_output = QuantValidationAgent().run(trade_date=trade_date)

        # 提取 theme_candidates 和 stock_candidates 供 v2 逻辑使用
        research_details = self.research_output.details if isinstance(
            self.research_output.details, Mapping
        ) else {}
        quant_details = self.quant_output.details if isinstance(
            self.quant_output.details, Mapping
        ) else {}
        theme_candidates = _mapping_items(research_details.get("theme_candidates"))
        stock_candidates = _mapping_items(quant_details.get("stock_candidates"))

        # 4. Risk Agent
        self.risk_output = RiskAgent().run(
            dataops_output=self.dataops_output,
            research_output=self.research_output,
            quant_output=self.quant_output,
            trade_date=trade_date,
            theme_candidates=theme_candidates,
            stock_candidates=stock_candidates,
        )

        # 5. Agent 讨论协议(差距 4:像五个研究员开会)
        # 必须在 CIO 之前，让 CIO 基于讨论共识做最终汇总
        debate_results = self._run_agent_debate(theme_candidates, stock_candidates)

        # 6. CIO Agent（基于 debate 共识做最终决策）
        self.cio_output = CIOAgent().run(
            dataops_output=self.dataops_output,
            research_output=self.research_output,
            quant_output=self.quant_output,
            risk_output=self.risk_output,
            trade_date=trade_date,
            theme_candidates=theme_candidates,
            stock_candidates=stock_candidates,
            debate_results=[d.to_dict() for d in debate_results],
        )

        # 7. 生成结构化研究观点(差距 1+2:研究观点 + 不确定性管理)
        research_opinions = self._build_research_opinions(theme_candidates)

        # 8. 情景压力测试(差距 3:把 scenario_simulator 用起来)
        scenario_reports = self._run_scenario_analysis(research_opinions, stock_candidates)

        # 9. 估值桥接(差距 7:产业逻辑到公司盈利)
        valuation_signals = self._build_valuation_signals(research_opinions, stock_candidates)

        # 10. 记录复盘判断(差距 8:研究复盘与自我进化)
        self._record_desk_decisions(research_opinions, stock_candidates)

        # 10.5 自动评估已到期的判断并生成复盘报告
        review_summary = self._run_review_evaluation()

        # 11. 实盘接入占位（默认 DRY_RUN，不触发任何交易）
        if Config.RESEARCH_DRY_RUN:
            logger.info("RESEARCH_DRY_RUN=True，候选股票仅输出到报告，不进入 position_manager")
        else:
            logger.info("RESEARCH_DRY_RUN=False，实盘接入位置：可在此调用 position_manager / order_executor")

        completed_at = datetime.now().astimezone()

        report = {
            "run_at": started_at.isoformat(timespec="seconds"),
            "completed_at": completed_at.isoformat(timespec="seconds"),
            "trade_date": trade_date,
            "agents": {
                "DataOpsAgent": asdict(self.dataops_output),
                "ResearchAgent": asdict(self.research_output),
                "QuantValidationAgent": asdict(self.quant_output),
                "RiskAgent": asdict(self.risk_output),
                "CIOAgent": asdict(self.cio_output),
            },
            "discussion_results": [d.to_dict() for d in debate_results],
            "research_opinions": [o.to_dict() for o in research_opinions],
            "scenario_reports": scenario_reports,
            "valuation_signals": valuation_signals,
            "review_recorded": True,
            "review_summary": review_summary,
            "final_output_level": self.cio_output.final_output_level,
            "candidate_stock_count": len(self.cio_output.today_candidate_stocks),
            "final_decision": self.cio_output.final_decision,
            "theme_watchlist_count": len(self.cio_output.theme_watchlist),
            "stock_watchlist_count": len(self.cio_output.stock_watchlist),
            "blocked_items_count": len(self.cio_output.blocked_items),
        }

        return report

    def _run_agent_debate(
        self,
        theme_candidates: Any,
        stock_candidates: Any,
    ) -> list[Any]:
        """执行 Agent 讨论,返回每个候选对象的讨论结果。"""
        args_by_target = build_arguments_from_agent_outputs(
            self.dataops_output,
            self.research_output,
            self.quant_output,
            self.risk_output,
            theme_candidates=theme_candidates,
            stock_candidates=stock_candidates,
        )

        debate = AgentDebate()
        results: list[Any] = []
        for target_key, arguments in args_by_target.items():
            if not arguments:
                continue
            target_type = arguments[0].target_type
            results.append(debate.debate(target_type, target_key, arguments))
        return results

    def _build_research_opinions(
        self,
        theme_candidates: Any,
    ) -> list[ResearchOpinion]:
        """基于 theme candidates 和证据生成研究观点。"""
        opinions: list[ResearchOpinion] = []
        evidences = self._load_evidence_center_evidences()

        for cand in _mapping_items(theme_candidates):
            theme = cand.get("name") or cand.get("theme", "")
            if not theme:
                continue

            # 把 evidence 中 theme 匹配或 entity_name 匹配的作为支持证据
            support_evs = [
                ev for ev in evidences
                if theme in (ev.get("themes", []) or []) or ev.get("entity_name") == theme
            ]
            # 简单反证:source_type 为 risk 或 evidence_type 为 risk
            counter_evs = [
                ev for ev in support_evs
                if ev.get("source_type") == "risk" or ev.get("evidence_type") == "risk"
            ]
            support_evs = [ev for ev in support_evs if ev not in counter_evs]

            thesis = f"{theme} 具备研究价值,值得持续跟踪"
            if cand.get("reason"):
                thesis = cand.get("reason")

            opinion = self.opinion_synthesizer.synthesize(
                thesis=thesis,
                theme=theme,
                evidence_list=support_evs,
                counter_evidence_list=counter_evs,
                reference_date=self.trade_date,
            )
            opinions.append(opinion)
        return opinions

    def _run_scenario_analysis(
        self,
        research_opinions: list[ResearchOpinion],
        stock_candidates: Any,
    ) -> dict[str, Any]:
        """对重点研究观点跑情景压力测试。"""
        if not research_opinions:
            return {}

        # 构造内存神经元图(theme -> stocks)
        neurons: dict[str, Any] = {}
        synapses: dict[str, Any] = {}
        for opinion in research_opinions:
            nid = f"theme_{opinion.theme}"
            neurons[nid] = _FakeNeuron(
                neuron_id=nid,
                canonical_name=opinion.theme,
                node_type="THEME",
                confidence=opinion.confidence_score,
            )
            for stock in _mapping_items(stock_candidates):
                symbol = stock.get("symbol", "")
                if not symbol:
                    continue
                sid = f"stock_{symbol}"
                if sid not in neurons:
                    neurons[sid] = _FakeNeuron(
                        neuron_id=sid,
                        canonical_name=symbol,
                        node_type="STOCK",
                        confidence=0.5,
                    )
                syn_id = f"syn_{nid}_{sid}"
                synapses[syn_id] = _FakeSynapse(
                    synapse_id=syn_id,
                    source_neuron_id=nid,
                    target_neuron_id=sid,
                    strength=0.6,
                    direction="DOWNSTREAM_APPLICATION",
                    relation="MAPS_TO",
                )

        simulator = self.scenario_simulator or ScenarioSimulator.from_neurons_and_synapses(
            neurons, synapses
        )

        reports: dict[str, Any] = {}
        scenarios = [
            ("base_upside", 0.15, 2),
            ("demand_miss", -0.20, 2),
            ("supply_constraint", -0.15, 2),
        ]
        for opinion in research_opinions[:3]:  # 只对前 3 个重点观点跑压力测试
            target_id = f"theme_{opinion.theme}"
            if simulator._get_neuron(target_id) is None:
                continue
            reports[opinion.theme] = {}
            for label, shock, steps in scenarios:
                report = simulator.simulate_intervention(target_id, shock, steps)
                reports[opinion.theme][label] = report.to_dict()
        return reports

    def _build_valuation_signals(
        self,
        research_opinions: list[ResearchOpinion],
        stock_candidates: Any,
    ) -> dict[str, Any]:
        """基于研究观点生成估值桥接信号(差距 7)。"""
        signals: dict[str, Any] = {}
        evidences = self._load_evidence_center_evidences()
        for opinion in research_opinions[:3]:
            theme_evidences = [
                ev for ev in evidences
                if opinion.theme in (ev.get("themes", []) or [])
                or ev.get("entity_name") == opinion.theme
            ]
            # 用观点主题作为产业节点进行估值映射
            fake_neuron = _FakeNeuron(
                neuron_id=f"theme_{opinion.theme}",
                canonical_name=opinion.theme,
                node_type="THEME",
                confidence=opinion.confidence_score,
            )
            report = self.valuation_bridge.analyze(fake_neuron, theme_evidences)
            signals[opinion.theme] = report.to_dict()

        # 对候选股票也直接生成信号
        for stock in _mapping_items(stock_candidates):
            symbol = stock.get("symbol", "")
            if not symbol:
                continue
            fake_neuron = _FakeNeuron(
                neuron_id=f"stock_{symbol}",
                canonical_name=symbol,
                node_type="STOCK",
                confidence=0.5,
            )
            report = self.valuation_bridge.analyze(fake_neuron, [])
            signals[symbol] = report.to_dict()
        return signals

    def _record_desk_decisions(
        self,
        research_opinions: list[ResearchOpinion],
        stock_candidates: Any,
    ) -> None:
        """把 Desk 的关键决策记录到复盘日志(差距 8)。"""
        if self.cio_output is None:
            return

        # 记录 CIO 对主题的观点
        for opinion in research_opinions[:3]:
            decision = "WATCH"
            if opinion.confidence_score >= 0.75 and opinion.uncertainty.contradiction < 0.3:
                decision = "BUY_CANDIDATE"
            elif opinion.confidence_score < 0.35 or opinion.uncertainty.contradiction > 0.5:
                decision = "BLOCK"
            self.review_engine.record_judgment(
                agent="CIOAgent",
                target_type="theme",
                target_key=opinion.theme,
                decision=decision,
                confidence=opinion.confidence_level,
                reasoning=opinion.thesis,
                trade_date=self.trade_date,
                evidence_summary=", ".join(opinion.evidence_ids[:5]),
                metadata={"proxy_symbol": self._pick_proxy_symbol(stock_candidates, opinion.theme)},
            )

        # 记录 CIO 对股票候选的决策
        for candidate in _mapping_items(self.cio_output.today_candidate_stocks)[:10]:
            symbol = str(candidate.get("symbol") or candidate.get("stock_code") or "")
            if not symbol:
                continue
            self.review_engine.record_judgment(
                agent="CIOAgent",
                target_type="stock",
                target_key=symbol,
                decision="BUY_CANDIDATE",
                confidence="medium",
                reasoning=f"进入今日候选股票列表: {symbol}",
                trade_date=self.trade_date,
            )

    @staticmethod
    def _pick_proxy_symbol(stock_candidates: Any, theme: str) -> str:
        """为主题选一个 proxy 股票代码。"""
        for stock in _mapping_items(stock_candidates):
            themes = stock.get("themes", []) or []
            if theme in themes:
                return stock.get("symbol", "")
        for stock in _mapping_items(stock_candidates):
            return stock.get("symbol", "")
        return ""

    def _run_review_evaluation(self) -> Dict[str, Any]:
        """自动评估已到期的判断记录，生成复盘报告。

        Returns:
            复盘摘要字典。
        """
        all_judgments = self.review_engine.review_log.all_judgments()
        if not all_judgments:
            return {"evaluated_count": 0, "skipped": "no_judgments"}

        today_str = datetime.now().strftime("%Y-%m-%d")
        horizon_days = 20
        natural_days_threshold = horizon_days * 7 // 5
        evaluable = []
        for j in all_judgments:
            if not j.trade_date:
                continue
            try:
                td = datetime.strptime(j.trade_date, "%Y-%m-%d")
                if (datetime.now() - td).days >= natural_days_threshold:
                    evaluable.append(j)
            except ValueError:
                continue

        if not evaluable:
            return {"evaluated_count": 0, "skipped": "none_due", "total_judgments": len(all_judgments)}

        performance_records = self.review_engine.evaluate_performance(evaluable)
        weights = self.review_engine.adjust_agent_weights(performance_records, evaluable)
        lessons = self.review_engine.generate_lessons(performance_records, evaluable)
        report_path = self.review_engine.save_review_report(performance_records, weights, lessons)

        hit_count = sum(1 for p in performance_records if p.hit)
        return {
            "evaluated_count": len(performance_records),
            "hit_count": hit_count,
            "agent_weights": weights,
            "lessons": lessons,
            "report_path": str(report_path),
        }

    def _load_evidence_center_evidences(self) -> list[dict[str, Any]]:
        """读取 evidence_center_latest.json 中的证据列表。"""
        data = _read_json("evidence_center_latest.json")
        evidences = data.get("evidences")
        if not isinstance(evidences, list):
            return []
        return [item for item in evidences if isinstance(item, dict)]

    def write_reports(self, report: Dict[str, Any]) -> None:
        """写入 JSON 和 Markdown 报告。

        Args:
            report: run() 返回的完整报告字典
        """
        REPORT_DIR.mkdir(parents=True, exist_ok=True)

        # JSON 报告
        json_path = REPORT_DIR / "virtual_research_desk_latest.json"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Markdown 报告
        md_lines = self._generate_markdown(report)
        md_path = REPORT_DIR / "virtual_research_desk_latest.md"
        md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    @staticmethod
    def _generate_markdown(report: Dict[str, Any]) -> list[str]:
        """生成 Markdown 报告内容。

        Args:
            report: 完整报告字典

        Returns:
            Markdown 行列表
        """
        agents = report.get("agents", {})
        trade_date = report.get("trade_date", "N/A")

        lines = [
            "# 虚拟投研台报告",
            "",
            f"- 运行时间：{report.get('run_at', 'N/A')}",
            f"- 完成时间：{report.get('completed_at', 'N/A')}",
            f"- 交易日：`{trade_date}`",
            f"- 最终输出等级：`{report.get('final_output_level', 'N/A')}`",
            f"- 候选股票数：`{report.get('candidate_stock_count', 0)}`",
            "",
            "---",
            "",
            "## 一、数据岗 DataOps",
            "",
        ]

        dataops = agents.get("DataOpsAgent", {})
        lines.extend([
            f"- 决策：`{dataops.get('decision', 'N/A')}`",
            f"- 置信度：`{dataops.get('confidence', 'N/A')}`",
            f"- 摘要：{dataops.get('summary', 'N/A')}",
            f"- 理由：{dataops.get('reason', 'N/A')}",
            f"- 风险标记：`{', '.join(dataops.get('risk_flags', [])) or '无'}`",
        ])
        details = dataops.get("details", {})
        if details:
            lines.extend([
                f"- 数据健康状态：`{details.get('health_status', 'N/A')}`",
                f"- 模块日期：`{json.dumps(details.get('module_dates', {}), ensure_ascii=False)}`",
                f"- 过期模块：`{', '.join(details.get('stale_modules', [])) or '无'}`",
                f"- 有效证据数：`{details.get('fresh_evidence', 0)}`",
                f"- 过时证据数：`{details.get('stale_evidence', 0)}`",
            ])

        lines.extend(["", "---", "", "## 二、行研岗 Research", ""])

        research = agents.get("ResearchAgent", {})
        lines.extend([
            f"- 决策：`{research.get('decision', 'N/A')}`",
            f"- 置信度：`{research.get('confidence', 'N/A')}`",
            f"- 摘要：{research.get('summary', 'N/A')}",
            f"- 理由：{research.get('reason', 'N/A')}`",
            f"- 引用证据：`{', '.join(research.get('evidence_ids', [])) or '无'}`",
        ])
        r_details = research.get("details", {})
        if r_details:
            lines.extend([
                f"- 有效证据：`{r_details.get('fresh_evidence', 0)}` 条",
                f"- 过时证据：`{r_details.get('stale_evidence', 0)}` 条",
                f"- 证据强度分布：`{json.dumps(r_details.get('strength_distribution', {}), ensure_ascii=False)}`",
            ])
            themes = r_details.get("themes", [])
            if themes:
                lines.append("- 主要题材：")
                for t in themes[:5]:
                    lines.append(
                        f"  - {t.get('name', 'N/A')} "
                        f"(强度: {t.get('strength', 'N/A')}, "
                        f"evidence_id: `{t.get('evidence_id', 'N/A')}`)"
                    )
            directions = r_details.get("directions", [])
            if directions:
                lines.append("- DragonEye 方向：")
                for d in directions[:5]:
                    lines.append(f"  - {d.get('name', 'N/A')} (数量: {d.get('count', 'N/A')})")
            theme_candidates = r_details.get("theme_candidates", [])
            if theme_candidates:
                lines.append("- 题材候选（v2）：")
                for tc in theme_candidates[:10]:
                    lines.append(
                        f"  - {tc.get('theme', 'N/A')} "
                        f"(证据数: {tc.get('evidence_count', 0)}, "
                        f"置信度: {tc.get('confidence', 'N/A')}, "
                        f"决策: {tc.get('decision', 'N/A')})"
                    )

        lines.extend(["", "---", "", "## 三、量化验证岗 QuantValidation", ""])

        quant = agents.get("QuantValidationAgent", {})
        lines.extend([
            f"- 决策：`{quant.get('decision', 'N/A')}`",
            f"- 置信度：`{quant.get('confidence', 'N/A')}`",
            f"- 摘要：{quant.get('summary', 'N/A')}",
            f"- 理由：{quant.get('reason', 'N/A')}`",
        ])
        q_details = quant.get("details", {})
        if q_details:
            lines.extend([
                f"- 候选股票数：`{q_details.get('candidate_count', 0)}`",
                f"- PASS：`{q_details.get('pass_count', 0)}` 只",
                f"- WATCH：`{q_details.get('watch_count', 0)}` 只",
                f"- INSUFFICIENT：`{q_details.get('insufficient_count', 0)}` 只",
            ])
            results = q_details.get("validation_results", [])
            if results:
                lines.append("- 验证详情（前 10 只）：")
                for r in results[:10]:
                    lines.append(
                        f"  - {r.get('symbol', 'N/A')} {r.get('name', '')} "
                        f"→ {r.get('decision', 'N/A')} "
                        f"(因子: {'是' if r.get('has_factor_support') else '否'}, "
                        f"信号: {'是' if r.get('has_signal_support') else '否'})"
                    )
            stock_candidates = q_details.get("stock_candidates", [])
            if stock_candidates:
                lines.append("- 股票候选（v2）：")
                for sc in stock_candidates[:10]:
                    lines.append(
                        f"  - {sc.get('symbol', 'N/A')} {sc.get('stock_name', '')} "
                        f"(因子: {'✓' if sc.get('factor_confirmed') else '✗'}, "
                        f"信号: {'✓' if sc.get('signal_confirmed') else '✗'}, "
                        f"趋势: {'✓' if sc.get('trend_confirmed') else '✗'}, "
                        f"决策: {sc.get('decision', 'N/A')})"
                    )

        lines.extend(["", "---", "", "## 四、风控岗 Risk", ""])

        risk = agents.get("RiskAgent", {})
        lines.extend([
            f"- 决策：`{risk.get('decision', 'N/A')}`",
            f"- 置信度：`{risk.get('confidence', 'N/A')}`",
            f"- 摘要：{risk.get('summary', 'N/A')}",
            f"- 理由：{risk.get('reason', 'N/A')}`",
            f"- 风险标记：`{', '.join(risk.get('risk_flags', [])) or '无'}`",
        ])
        rk_details = risk.get("details", {})
        if rk_details:
            block_reasons = rk_details.get("block_reasons", [])
            watch_reasons = rk_details.get("watch_reasons", [])
            if block_reasons:
                lines.append("- BLOCK 原因：")
                for br in block_reasons:
                    lines.append(f"  - {br}")
            if watch_reasons:
                lines.append("- WATCH 原因：")
                for wr in watch_reasons:
                    lines.append(f"  - {wr}")
            lines.extend([
                f"- 无证据题材数：`{rk_details.get('no_evidence_themes_count', 0)}`",
                f"- 无证据股票数：`{rk_details.get('no_evidence_stocks_count', 0)}`",
                f"- 持仓估值完整：`{rk_details.get('valuation_complete', True)}`",
                f"- 策略阻塞原因：`{rk_details.get('strategy_blocked_reason', '') or '无'}`",
            ])
            # v2: Risk verdicts
            theme_verdicts = rk_details.get("theme_verdicts", [])
            stock_verdicts = rk_details.get("stock_verdicts", [])
            if theme_verdicts:
                lines.append("- 题材风控判定：")
                for tv in theme_verdicts[:10]:
                    lines.append(
                        f"  - {tv.get('theme', 'N/A')}: "
                        f"{tv.get('original_decision', 'N/A')} → {tv.get('final_decision', 'N/A')} "
                        f"({tv.get('reason', '')})"
                    )
            if stock_verdicts:
                lines.append("- 股票风控判定：")
                for sv in stock_verdicts[:10]:
                    lines.append(
                        f"  - {sv.get('symbol', 'N/A')}: "
                        f"{sv.get('original_decision', 'N/A')} → {sv.get('final_decision', 'N/A')} "
                        f"({sv.get('reason', '')})"
                    )

        lines.extend(["", "---", "", "## 五、CIO 总结", ""])

        cio = agents.get("CIOAgent", {})
        lines.extend([
            f"- 最终输出等级：`{cio.get('final_output_level', 'N/A')}`",
            f"- 决策：`{cio.get('decision', 'N/A')}`",
            f"- 置信度：`{cio.get('confidence', 'N/A')}`",
            f"- 摘要：{cio.get('summary', 'N/A')}",
            f"- 理由：{cio.get('reason', 'N/A')}`",
            f"- 引用证据：`{', '.join(cio.get('evidence_ids', [])) or '无'}`",
        ])

        cio_data = cio
        watch_dirs = cio_data.get("today_watch_directions", [])
        blocked_dirs = cio_data.get("today_blocked_directions", [])
        candidates = cio_data.get("today_candidate_stocks", [])
        no_trade = cio_data.get("today_no_trade_reasons", [])

        if watch_dirs:
            lines.append("- 今日可观察方向：")
            for wd in watch_dirs[:10]:
                name = wd.get("name", "N/A")
                source = wd.get("source", "")
                lines.append(f"  - {name} (来源: {source})")

        if blocked_dirs:
            lines.append("- 今日禁止方向：")
            for bd in blocked_dirs:
                lines.append(f"  - {bd.get('source', 'N/A')}: {bd.get('reason', 'N/A')}")

        if candidates:
            lines.append("- 今日候选股票：")
            for cs in candidates:
                ev_ids = cs.get("evidence_ids", [])
                lines.append(
                    f"  - {cs.get('symbol', 'N/A')} {cs.get('name', '')} "
                    f"(evidence_ids: {', '.join(ev_ids) or '无'})"
                )
        else:
            lines.append("- 今日候选股票：无")

        if no_trade:
            lines.append("- 今日不应交易的原因：")
            for nr in no_trade:
                lines.append(f"  - {nr}")

        # v2: CIO watchlist
        theme_wl = cio_data.get("theme_watchlist", [])
        stock_wl = cio_data.get("stock_watchlist", [])
        blocked_items = cio_data.get("blocked_items", [])
        insufficient_items = cio_data.get("insufficient_evidence_items", [])
        final_decision = cio_data.get("final_decision", "")
        market_view = cio_data.get("market_view", "")

        if market_view:
            lines.append(f"- 市场观点：{market_view}")
        if final_decision:
            lines.append(f"- 最终决策（v2）：`{final_decision}`")
        if theme_wl:
            lines.append("- 题材观察列表（v2）：")
            for tw in theme_wl[:10]:
                ev_ids = tw.get("evidence_ids", [])
                lines.append(
                    f"  - {tw.get('theme', 'N/A')} "
                    f"(决策: {tw.get('decision', 'N/A')}, "
                    f"证据: {len(ev_ids)}条)"
                )
        if stock_wl:
            lines.append("- 股票观察列表（v2）：")
            for sw in stock_wl[:10]:
                ev_ids = sw.get("evidence_ids", [])
                lines.append(
                    f"  - {sw.get('symbol', 'N/A')} {sw.get('stock_name', '')} "
                    f"(决策: {sw.get('decision', 'N/A')}, "
                    f"证据: {len(ev_ids)}条)"
                )
        if blocked_items:
            lines.append("- 阻断项（v2）：")
            for bi in blocked_items[:10]:
                lines.append(f"  - {bi.get('item', 'N/A')}: {bi.get('reason', '')}")
        if insufficient_items:
            lines.append("- 证据不足项（v2）：")
            for ii in insufficient_items[:10]:
                lines.append(f"  - {ii.get('item', 'N/A')}: {ii.get('reason', '')}")

        lines.extend(["", "---", ""])
        lines.append("*本报告由虚拟投研台自动生成，不构成投资建议。*")

        return lines
