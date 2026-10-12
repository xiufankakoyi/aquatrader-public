"""Daily pre-market research + paper trading orchestrator.

Runs the full research pipeline, selects A-share stocks from the research view,
and simulates buying at the opening price. Results are persisted to a local
portfolio JSON file for daily tracking.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paper_trading.data_source import AkSharePriceSource
from core.paper_trading.backtest_execution_adapter import BacktestExecutionPolicyAdapter
from core.paper_trading.portfolio import Portfolio
from core.paper_trading.selector import ResearchViewSelector
from core.research.perilla.alpha.outcome_tracker import OutcomeTracker
from core.research_graph.candidate_graph import CandidateStore
from core.research_graph.models import utc_now
from core.quant.backtest.models import BacktestConfig

logger = logging.getLogger(__name__)


@dataclass
class TradingDayResult:
    """Result of a single paper-trading day."""

    date: str
    topic: str
    research_out_dir: Path
    research_view_nodes: int
    selected_stocks: list[dict[str, Any]]
    executed_buys: list[dict[str, Any]]
    executed_sells: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    nav: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "topic": self.topic,
            "research_out_dir": str(self.research_out_dir),
            "research_view_nodes": self.research_view_nodes,
            "selected_stocks": self.selected_stocks,
            "executed_buys": self.executed_buys,
            "executed_sells": self.executed_sells,
            "skipped": self.skipped,
            "nav": self.nav,
        }


class PaperTradingOrchestrator:
    """Orchestrate daily research-driven paper trading."""

    def __init__(
        self,
        initial_cash: float = 1_000_000.0,
        portfolio_path: str | Path = "data/paper_trading/portfolio.json",
        max_position_pct: float | None = None,
        top_n: int = 10,
        min_research_nodes: int = 30,
        min_score: float = 0.5,
        price_cache_dir: str | Path = "data/paper_trading/price_cache",
        slippage_rate: float = 0.001,
        volume_cap_ratio: float = 0.05,
        outcome_tracker: OutcomeTracker | None = None,
        portfolio_constructor: Any | None = None,
        execution_model: Any | None = None,
    ) -> None:
        """Initialize orchestrator.

        Args:
            initial_cash: Starting cash for the paper portfolio.
            portfolio_path: JSON state file for the paper portfolio.
            max_position_pct: Max single-position weight at entry. If None, it is
                automatically set to 1 / top_n for equal-weight allocation.
            top_n: Max number of stocks to buy per day.
            min_research_nodes: Minimum research-view nodes required to trigger buying.
            min_score: Minimum activation score for a node to be considered.
            price_cache_dir: Directory to cache daily price DataFrames.
            outcome_tracker: Optional OutcomeTracker for alpha feedback loop.
            portfolio_constructor: Optional target-weight constructor injected into
                Portfolio.rebalance. None falls back to EqualWeightConstructor.
            execution_model: Optional execution model injected into
                Portfolio.rebalance. None falls back to DefaultExecutionModel.
        """
        self.initial_cash = initial_cash
        self.portfolio_path = Path(portfolio_path)
        self.top_n = top_n
        self.max_position_pct = max_position_pct if max_position_pct is not None else 1.0 / max(top_n, 1)
        self.min_research_nodes = min_research_nodes
        self.min_score = min_score
        self.price_cache_dir = Path(price_cache_dir)
        self.slippage_rate = max(0.0, float(slippage_rate))
        self.volume_cap_ratio = max(0.0, min(1.0, float(volume_cap_ratio)))
        self.execution_policy = BacktestExecutionPolicyAdapter(BacktestConfig(
            slippage_rate=self.slippage_rate,
            volume_cap_ratio=self.volume_cap_ratio,
        ))
        self.outcome_tracker = outcome_tracker
        # 组合构造与执行策略可外部注入，None 时由 Portfolio.rebalance 回退默认实现。
        self.portfolio_constructor = portfolio_constructor
        self.execution_model = execution_model

        self.portfolio = Portfolio(
            initial_cash=initial_cash,
            state_path=self.portfolio_path,
            max_position_pct=self.max_position_pct,
        )
        self.portfolio.COMMISSION_RATE = self.execution_policy.config.commission_rate
        self.portfolio.MIN_COMMISSION = self.execution_policy.config.min_commission
        self.portfolio.TAX_RATE = self.execution_policy.config.sell_tax
        self.price_source = AkSharePriceSource(cache_dir=self.price_cache_dir)

    def run_research(
        self,
        topic: str,
        run_dir: str | Path,
        out_dir: str | Path | None = None,
        max_rounds: int = 1,
        search_backend: str = "auto",
    ) -> Path:
        """Run the real research pipeline and return the output directory.

        Args:
            topic: Research topic.
            run_dir: Seed evidence directory.
            out_dir: Optional output directory; auto-generated if omitted.
            max_rounds: Maximum research loop rounds.
            search_backend: "auto", "searxng", or "ddg+bing".

        Returns:
            Path to the research output directory.
        """
        project_root = Path(__file__).resolve().parents[2]
        script = project_root / "sandbox" / "run_real_research_pipeline.py"

        cmd = [
            sys.executable,
            str(script),
            "--topic", topic,
            "--run-dir", str(run_dir),
            "--max-rounds", str(max_rounds),
            "--search-backend", search_backend,
        ]
        if out_dir:
            cmd.extend(["--out-dir", str(out_dir)])

        logger.info("Running research pipeline: %s", " ".join(cmd))
        result = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=False)
        if result.returncode != 0:
            raise RuntimeError(f"Research pipeline failed with code {result.returncode}")

        # Locate the latest research output directory if not explicitly provided
        if out_dir:
            return Path(out_dir)

        runs = sorted(
            (project_root / "data" / "research_runs").glob("real_pipeline_*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not runs:
            raise RuntimeError("No research output directory found")
        return runs[0]

    def run_trading_day(
        self,
        topic: str,
        research_out_dir: Path,
        trade_date: str | None = None,
    ) -> TradingDayResult:
        """Execute one paper-trading day from a completed research run.

        Args:
            topic: Research topic.
            research_out_dir: Output directory of the research pipeline.
            trade_date: Date string, defaults to today.

        Returns:
            TradingDayResult with executed trades and NAV.
        """
        date = trade_date or utc_now()[:10]
        store_dir = research_out_dir / "candidate_store"
        store = CandidateStore(db_path=store_dir)

        # SAG 事件特征必须先于公司评分。缺失时使用空 overlay，不改变稳定图事实。
        event_features: dict[str, dict[str, float]] = {}
        for filename in ("sag_event_features.json", "event_features.json"):
            event_path = research_out_dir / filename
            if not event_path.exists():
                continue
            try:
                payload = json.loads(event_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    event_features = payload.get("by_company", payload)
                    break
            except Exception as exc:
                logger.warning("Failed to load SAG event features %s: %s", event_path, exc)

        alpha_multiplier = 1.0
        try:
            from core.research.control_plane.repository import ResearchRepository
            active_alpha_policy = ResearchRepository().get_active_policy("alpha")
            configured = active_alpha_policy.parameters.get("alpha_weight_multiplier") if active_alpha_policy else None
            if isinstance(configured, (int, float)):
                alpha_multiplier = float(configured)
        except Exception as exc:
            logger.debug("读取 Alpha 校准策略失败，使用默认乘数: %s", exc)
        selector = ResearchViewSelector(
            candidate_store=store,
            research_out_dir=research_out_dir,
            # 开盘执行只允许使用交易日开始前已可见的研究证据。
            as_of=f"{date}T00:00:00+00:00",
            event_features=event_features,
            alpha_weight_multiplier=alpha_multiplier,
            outcome_tracker=self.outcome_tracker,
        )
        selected = selector.select(top_n=self.top_n, min_score=self.min_score)

        research_view_nodes = len(store.list_candidates(view="research"))

        executed_buys: list[dict[str, Any]] = []
        executed_sells: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        # Gate: only trade if research produced enough high-confidence nodes
        if research_view_nodes < self.min_research_nodes:
            logger.warning(
                "Trading gate blocked: research_view nodes %d < threshold %d",
                research_view_nodes, self.min_research_nodes,
            )
            nav = self.portfolio.current_nav({})
            return TradingDayResult(
                date=date,
                topic=topic,
                research_out_dir=research_out_dir,
                research_view_nodes=research_view_nodes,
                selected_stocks=[s.__dict__ for s in selected],
                executed_buys=executed_buys,
                executed_sells=executed_sells,
                skipped=[{"reason": "below_research_threshold", "threshold": self.min_research_nodes}],
                nav=nav,
            )

        # Build price map for selected stocks and existing positions.
        prices: dict[str, float] = {}
        max_quantity_by_code: dict[str, int] = {}
        impact_costs: dict[str, float] = {}  # P0-6：冲击成本（卖出后从现金扣除）
        for stock in selected:
            bar = self.price_source.get_daily_bar(stock.code, trade_date=date)
            try:
                stats = self.price_source.get_volume_stats(stock.code, date)
                avg_vol = float(stats.get("avg_volume_20d", 0.0) or 0.0)
            except Exception:
                avg_vol = 0.0
            decision = self.execution_policy.decide(bar, side="buy", average_volume=avg_vol)
            if decision.allowed:
                prices[stock.code] = decision.price
                max_quantity_by_code[stock.code] = decision.max_quantity
            else:
                skipped.append({
                    "code": stock.code,
                    "name": stock.name,
                    "reason": decision.rejection_reason,
                })
        for code in list(self.portfolio.positions.keys()):
            if code not in prices:
                bar = self.price_source.get_daily_bar(code, trade_date=date)
                pos = self.portfolio.positions[code]
                # P0-6：卖出时传入实际持仓数量和平均成交量，落实容量限制
                try:
                    stats = self.price_source.get_volume_stats(code, date)
                    avg_vol = float(stats.get("avg_volume_20d", 0.0) or 0.0)
                except Exception:
                    avg_vol = 0.0
                decision = self.execution_policy.decide(
                    bar, side="sell",
                    quantity=pos.quantity,
                    average_volume=avg_vol,
                )
                if decision.allowed:
                    prices[code] = decision.price
                    # P0-6：记录冲击成本，rebalance 后从现金扣除
                    if decision.impact_cost > 0:
                        impact_costs[code] = decision.impact_cost

        # Rebalance only when there is a new target list; otherwise keep holdings.
        if selected:
            targets = [
                {
                    "code": s.code,
                    "name": s.name,
                    "score": s.score,
                    "node_id": s.node_id,
                    "node_type": s.node_type,
                    "status": s.status,
                }
                for s in selected
                if s.code in prices
            ]
            rebalance_result = self.portfolio.rebalance(
                targets=targets,
                prices=prices,
                trade_date=date,
                reason_template=f"research_view: {topic}",
                max_quantity_by_code=max_quantity_by_code,
                constructor=self.portfolio_constructor,
                execution_model=self.execution_model,
            )
            executed_buys = rebalance_result["buys"]
            executed_sells = rebalance_result["sells"]
            skipped.extend([{"code": c, "reason": "rebalance_skipped"} for c in rebalance_result["skipped"]])

            # P0-6：冲击成本从现金扣除（已卖出的持仓）
            for code, impact in impact_costs.items():
                if code not in self.portfolio.positions:
                    self.portfolio.cash -= impact

        # Compute NAV using open prices for all positions.
        nav_prices: dict[str, float] = {}
        for code in self.portfolio.positions:
            nav_prices[code] = prices.get(code, self.portfolio.positions[code].avg_cost)
        nav = self.portfolio.current_nav(nav_prices)
        self.portfolio.save()

        result = TradingDayResult(
            date=date,
            topic=topic,
            research_out_dir=research_out_dir,
            research_view_nodes=research_view_nodes,
            selected_stocks=[s.__dict__ for s in selected],
            executed_buys=executed_buys,
            executed_sells=executed_sells,
            skipped=skipped,
            nav=nav,
        )
        self._resolve_pending_outcomes(date)
        self._record_alpha_feedback(result)
        return result

    def _resolve_pending_outcomes(self, date: str) -> None:
        """Resolve matured alpha outcomes using forward returns from price data.

        Fetches pending outcome records that have elapsed at least 20 days
        since promotion, computes the 20-day forward return from the price
        source, and marks each record as resolved.

        Args:
            date: The current trading date used as the observation endpoint.
        """
        if self.outcome_tracker is None:
            return
        for record in self.outcome_tracker.get_pending_resolutions(days_elapsed=20):
            promoted_date = (record.promoted_at or "")[:10]
            if not promoted_date:
                continue
            entry_bar = self.price_source.get_daily_bar(record.target_entity, trade_date=promoted_date)
            exit_bar = self.price_source.get_daily_bar(record.target_entity, trade_date=date)
            if entry_bar is None or exit_bar is None:
                continue
            if entry_bar.close <= 0:
                continue
            forward_return_20d = (exit_bar.close - entry_bar.close) / entry_bar.close
            updated = self.outcome_tracker.resolve_outcome(
                record_id=record.record_id,
                forward_return_20d=forward_return_20d,
                excess_return_60d=None,
            )
            if updated:
                # 同步写入 Alpha 域反馈，供 AlphaPolicyCalibrator 学习路径结果。
                # 反馈记录失败不应阻塞主交易流程。
                try:
                    from core.research.control_plane.contracts import FeedbackEvent
                    from core.research.control_plane.feedback import FeedbackController
                    from core.research.control_plane.repository import ResearchRepository

                    outcome_label = self._derive_path_outcome(
                        forward_return_20d=forward_return_20d,
                        excess_return_60d=None,
                    )
                    outcome = {
                        "success": "positive",
                        "failure": "negative",
                    }.get(outcome_label, "neutral")
                    FeedbackController(ResearchRepository()).record(
                        FeedbackEvent(
                            domain="alpha",
                            event_type="path_resolution",
                            event_kind="path_outcome",
                            target_id=record.path_id or "",
                            outcome=outcome,
                            metrics={
                                "forward_return_20d": forward_return_20d,
                                "forward_return_60d": record.forward_return_60d,
                                "excess_return_20d": record.excess_return_20d,
                                "excess_return_60d": record.excess_return_60d,
                                "final_score": record.final_score,
                            },
                            attribution={
                                "node_type": record.metadata.get("node_type", ""),
                                "root_entity": record.root_entity,
                                "target_entity": record.target_entity,
                            },
                        )
                    )
                except Exception as exc:
                    logger.debug("记录 path_outcome 反馈失败，不影响主流程: %s", exc)

    @staticmethod
    def _derive_path_outcome(
        forward_return_20d: float | None,
        excess_return_60d: float | None,
    ) -> str:
        """根据可用收益推导出路径结果标签。

        与 OutcomeTracker.resolve_outcome 的自动判定规则保持一致，
        避免在 resolve 后再次查询数据库即可得到 success/failure/neutral 标签。

        Args:
            forward_return_20d: 提升后 20 日远期绝对收益。
            excess_return_60d: 可选的 60 日超额收益。

        Returns:
            success、failure、neutral 或 pending 之一。
        """
        if excess_return_60d is not None:
            if excess_return_60d > 0.05:
                return "success"
            if excess_return_60d < -0.05:
                return "failure"
            return "neutral"
        if forward_return_20d is not None:
            if forward_return_20d > 0.05:
                return "success"
            if forward_return_20d < -0.05:
                return "failure"
            return "neutral"
        return "pending"

    def _record_alpha_feedback(self, result: TradingDayResult) -> None:
        """模拟结果只进入 Alpha 域；不会修改 Claim 或来源权重。"""
        try:
            from core.research.control_plane.contracts import FeedbackEvent
            from core.research.control_plane.feedback import FeedbackController
            from core.research.control_plane.repository import ResearchRepository

            total_value = float(result.nav.get("total_value", self.initial_cash) or self.initial_cash)
            simulated_return = total_value / max(self.initial_cash, 1.0) - 1.0
            nav_values = [
                float(item.get("total_value", self.initial_cash) or self.initial_cash)
                for item in self.portfolio.nav_history
            ]
            peak = self.initial_cash
            max_drawdown = 0.0
            for value in nav_values:
                peak = max(peak, value)
                max_drawdown = min(max_drawdown, value / max(peak, 1.0) - 1.0)
            FeedbackController(ResearchRepository()).record(
                FeedbackEvent(
                    domain="alpha",
                    event_type="paper_simulation_outcome",
                    target_id=result.topic,
                    metrics={
                        "simulated_return": round(simulated_return, 6),
                        "max_drawdown": round(max_drawdown, 6),
                        "excess_return": None,
                        "out_of_sample_ic": None,
                        "fact_confidence_mutation_allowed": False,
                    },
                    attribution={"research_out_dir": str(result.research_out_dir)},
                ),
                generate_task=abs(simulated_return) >= 0.10,
            )
        except Exception as exc:
            logger.warning("Alpha feedback recording failed: %s", exc)

        self._run_shadow_calibration()

    def _run_shadow_calibration(self) -> None:
        """Run Bayesian weight calibration in shadow mode.

        Instantiates a BayesianWeightCalibrator with the outcome tracker,
        fits the posterior, and persists the result as a shadow CalibrationRun
        when there are enough labeled samples (>= 10).

        Shadow runs never modify live weights; they are recorded for audit
        and future promotion review.
        """
        if self.outcome_tracker is None:
            return
        try:
            from core.research.control_plane.contracts import CalibrationRun
            from core.research.control_plane.repository import ResearchRepository
            from core.research.perilla.alpha.bayesian_weight_calibrator import (
                BayesianWeightCalibrator,
            )

            calibrator = BayesianWeightCalibrator(outcome_tracker=self.outcome_tracker)
            calibration_result = calibrator.calibrate()
            if calibration_result.n_samples < 10:
                return
            ResearchRepository().save_calibration(CalibrationRun(
                domain="alpha",
                status="shadow",
                sample_size=calibration_result.n_samples,
                metrics={
                    "log_likelihood": calibration_result.log_likelihood,
                    "n_positive": calibration_result.n_positive,
                    "n_negative": calibration_result.n_negative,
                    "walk_forward_score": calibration_result.walk_forward_score,
                },
                parameters={
                    posterior.name: posterior.posterior_mean
                    for posterior in calibration_result.posteriors
                },
            ))
        except Exception as exc:
            logger.warning("Bayesian weight calibration failed: %s", exc)

    def run(
        self,
        topic: str,
        run_dir: str | Path = "data/research_runs/overnight_evidence_20260629_231522",
        out_dir: str | Path | None = None,
        max_rounds: int = 1,
        search_backend: str = "auto",
        trade_date: str | None = None,
    ) -> TradingDayResult:
        """Run research + trading in one call.

        Args:
            topic: Research topic.
            run_dir: Seed evidence directory.
            out_dir: Optional research output directory.
            max_rounds: Research loop rounds.
            search_backend: Search backend to use.
            trade_date: Date string, defaults to today.

        Returns:
            TradingDayResult.
        """
        research_out_dir = self.run_research(
            topic=topic,
            run_dir=run_dir,
            out_dir=out_dir,
            max_rounds=max_rounds,
            search_backend=search_backend,
        )
        return self.run_trading_day(topic, research_out_dir, trade_date=trade_date)

    def save_daily_report(
        self,
        result: TradingDayResult,
        report_dir: str | Path = "data/paper_trading/daily_reports",
    ) -> Path:
        """Persist the daily trading report to JSON."""
        report_dir = Path(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"{result.date}_{result.topic}.json"
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path
