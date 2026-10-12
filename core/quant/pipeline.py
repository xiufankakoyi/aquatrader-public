"""QUANT Pipeline 编排器。

把 adapter → gate → sector → screener → signal_generator → repository 串成
完整流水线，提供单日运行入口，供定时任务或 API 调用。

设计原则：
- 单一入口：run_daily() 接收候选列表 + 当日行情，输出信号列表并落库
- 安全降级：任一组件异常不阻塞整体流程，标记 DEGRADED
- 不输出买卖/仓位/止盈止损
- 可重入：同一天重复运行覆盖旧信号
- P0-1：所有候选必须通过 Research 准入门禁才会进入筛选阶段

主题控制面扩展：run_with_themes() 基于 ThemePacket 执行 Pipeline，
跳过 adapter/gate/sector，直接由 CandidateQuotaGenerator → ThemeDailyScreener
→ generate_from_themes 串联。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.quant.adapter import adapt_research_candidates
from core.quant.contracts import (
    QuantCandidate,
    QuantSignal,
    SectorAssessment,
    ScreeningResult,
)
from core.quant.gate import (
    AdmissionResult,
    apply_admission_to_candidate,
    check_admission,
)
from core.quant.repository import QuantRepository
from core.quant.screening.screener import DailyScreener
from core.quant.sector.sector_engine import SectorRotationEngine
from core.quant.signal_generator import SignalGenerator
from core.quant.theme.candidate_quota import CandidateQuotaGenerator
from core.quant.theme.contracts import ThemePacket
from core.quant.theme.daily_screener import ThemeDailyScreener, ThemeScreeningResult

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Pipeline 单次运行结果。

    Attributes:
        signals: 生成的 QuantSignal 列表。
        sector_assessments: 板块评估结果映射。
        saved_count: 成功落库的信号数。
        degraded: 是否发生降级。
        errors: 错误信息列表（不阻塞流程）。
        admission_results: 各候选的准入门禁结果映射（candidate_id → AdmissionResult）。
        theme_candidates: 主题流程生成的候选列表（仅 run_with_themes 填充）。
        theme_screening_results: 主题筛选结果列表（仅 run_with_themes 填充）。
    """

    signals: list[QuantSignal] = field(default_factory=list)
    sector_assessments: dict[str, SectorAssessment] = field(default_factory=dict)
    saved_count: int = 0
    degraded: bool = False
    errors: list[str] = field(default_factory=list)
    admission_results: dict[str, AdmissionResult] = field(default_factory=dict)
    # 主题流程扩展字段
    theme_candidates: list[QuantCandidate] = field(default_factory=list)
    theme_screening_results: list[ThemeScreeningResult] = field(default_factory=list)
    # 候选生成漏斗诊断（仅 run_with_themes 填充），用于工作台结构化漏斗端点
    candidate_funnel: dict[str, Any] = field(default_factory=dict)


class QuantPipeline:
    """QUANT 完整流水线编排器。

    串联六大组件：
    1. adapter: ResearchCandidate → QuantCandidate
    2. gate: Research 准入门禁（P0-1）
    3. sector: 评估板块阶段
    4. screener: 日线筛选
    5. signal_generator: 生成 QuantSignal
    6. repository: 持久化

    不输出买卖/仓位/止盈止损。
    """

    def __init__(
        self,
        *,
        sector_engine: SectorRotationEngine | None = None,
        screener: DailyScreener | None = None,
        signal_generator: SignalGenerator | None = None,
        repository: QuantRepository | None = None,
        research_repository: Any | None = None,
        data_query: Any | None = None,
        candidate_repo: Any | None = None,
    ) -> None:
        """初始化 Pipeline。

        Args:
            sector_engine: 板块轮动引擎；None 时用默认实例。
            screener: 日线筛选引擎；None 时用默认实例，并注入 data_query。
            signal_generator: 信号生成器；None 时用默认实例。
            repository: 持久化仓库；None 时不落库（仅返回信号）。
            research_repository: Research 仓库（只读），用于准入门禁反查 Claim；
                None 时门禁降级为仅校验候选自身字段。
            data_query: 数据查询对象（UnifiedDataQuery 等），用于：
                1) stock_pool_today=None 时自动拉取当日全市场快照；
                2) screener 自动加载个股日线数据。
                None 时两个自动拉取能力都关闭，需调用方显式传参。
            candidate_repo: Research 候选仓库（CandidateRepository），
                用于 research_candidates=None 时自动拉取已发布的活跃候选。
                None 时关闭自动拉取，需调用方显式传候选列表。
        """
        self._data_query = data_query
        self._candidate_repo = candidate_repo
        # 把 data_query 注入到 screener 和 sector_engine（若它们未自带）
        if screener is None:
            self._screener = DailyScreener(data_query=data_query)
        else:
            self._screener = screener
        if sector_engine is None:
            # 默认用 auto 模式：东财概念 → 申万 → stock_basic 自动回退
            # 东财概念在网络阻断时返回空，auto 会自动降级到 sw 或 basic
            self._sector_engine = SectorRotationEngine(
                data_query=data_query, sector_type="auto",
            )
        else:
            self._sector_engine = sector_engine
        self._signal_generator = signal_generator or SignalGenerator()
        self._repository = repository
        self._research_repository = research_repository

    def run_daily(
        self,
        research_candidates: list[Any] | None = None,
        stock_pool_today: pd.DataFrame | None = None,
        current_date: str = "",
        *,
        daily_data_map: dict[str, pd.DataFrame] | None = None,
    ) -> PipelineResult:
        """执行单日 Pipeline。

        三处自动拉取（data_query / candidate_repo 不为 None 时）：
        1. research_candidates=None → 从 candidate_repo.list_candidates 拉取活跃候选
        2. stock_pool_today=None → 从 data_query.get_stock_pool(current_date) 拉取当日快照
        3. daily_data_map=None → screener 内部通过 data_query.get_stock_history 加载

        Args:
            research_candidates: ResearchCandidate 对象或 dict 列表；
                None 时从 candidate_repo 自动拉取（candidate_repo 必须非 None）。
            stock_pool_today: 当日全市场个股 OHLCV 数据；
                None 时从 data_query 自动拉取（data_query 必须非 None）。
            current_date: 当前日期（ISO 格式）；自动拉取场景下必须非空。
            daily_data_map: 预加载的个股日线数据 {symbol: DataFrame}；
                None 时由 screener 通过 data_query 获取。

        Returns:
            PipelineResult 运行结果。
        """
        result = PipelineResult()

        # 0. 自动拉取候选（research_candidates=None 时）
        if research_candidates is None:
            if self._candidate_repo is None:
                result.errors.append("research_candidates=None 且未配置 candidate_repo")
                result.degraded = True
                return result
            try:
                research_candidates = self._candidate_repo.list_candidates(
                    status="active", limit=200
                )
                logger.info("自动拉取候选 %d 个", len(research_candidates))
            except Exception as exc:
                logger.error("自动拉取候选失败: %s", exc)
                result.errors.append(f"拉取候选失败: {exc}")
                result.degraded = True
                return result

        # 0b. 自动拉取当日全市场快照（stock_pool_today=None 时）
        if stock_pool_today is None:
            if self._data_query is None:
                result.errors.append("stock_pool_today=None 且未配置 data_query")
                result.degraded = True
                return result
            try:
                stock_pool_today = self._data_query.get_stock_pool(current_date)
                if stock_pool_today is None or stock_pool_today.empty:
                    result.errors.append(f"data_query.get_stock_pool({current_date}) 返回空")
                    result.degraded = True
                    return result
                logger.info("自动拉取当日快照 %d 只股票", len(stock_pool_today))
            except Exception as exc:
                logger.error("自动拉取当日快照失败: %s", exc)
                result.errors.append(f"拉取当日快照失败: {exc}")
                result.degraded = True
                return result

        # 1. 适配候选
        try:
            candidates = adapt_research_candidates(research_candidates)
            if not candidates:
                result.errors.append("适配后候选列表为空")
                result.degraded = True
                return result
        except Exception as exc:
            logger.error("候选适配失败: %s", exc)
            result.errors.append(f"适配失败: {exc}")
            result.degraded = True
            return result

        # 2. Research 准入门禁（P0-1）
        self._apply_admission(candidates, current_date, result)

        # 3. 板块评估
        try:
            result.sector_assessments = self._sector_engine.assess(
                stock_pool_today, current_date
            )
            if not result.sector_assessments:
                result.errors.append("板块评估为空")
                result.degraded = True
        except Exception as exc:
            logger.error("板块评估失败: %s", exc)
            result.errors.append(f"板块评估失败: {exc}")
            result.degraded = True

        # 4. 日线筛选 + 5. 信号生成
        # P0-4：按 candidate_id 存储筛选结果，避免同证券多候选覆盖
        screening_results: dict[str, ScreeningResult] = {}
        for candidate in candidates:
            # 门禁未通过的候选也走信号生成（会被标记 REJECTED + RESEARCH_GATE_FAIL），
            # 但不必消耗日线筛选资源
            if not candidate.admission_passed:
                continue
            # P0-4：缺 candidate_id 不得进入筛选（防止多空 ID 覆盖同一筛选结果）
            if not candidate.candidate_id:
                candidate.degraded = True
                if "candidate_id" not in candidate.degraded_fields:
                    candidate.degraded_fields.append("candidate_id")
                continue
            try:
                # P0 板块名修复：用 symbol 反查板块，不用 sector_id 字符串匹配
                sector_name, sector_source = self._sector_engine.get_sector_for_symbol_with_fallback(
                    candidate.symbol, candidate.sector_id
                )
                # 用反查到的中文板块名更新 candidate.sector_id，供后续 signal_generator 使用
                if sector_name:
                    candidate.sector_id = sector_name
                sector_assessment = (
                    result.sector_assessments.get(sector_name)
                    if sector_name else None
                )
                if sector_assessment and sector_source:
                    tag = f"sector_source:{sector_source}"
                    if tag not in sector_assessment.degraded_fields:
                        sector_assessment.degraded_fields.append(tag)
                daily_data = None
                if daily_data_map is not None:
                    daily_data = daily_data_map.get(candidate.symbol)
                screening = self._screener.screen(
                    candidate, sector_assessment, current_date,
                    daily_data=daily_data,
                )
                screening_results[candidate.candidate_id] = screening
                if screening.degraded:
                    result.degraded = True
            except Exception as exc:
                logger.warning("候选 %s 筛选失败: %s", candidate.symbol, exc)
                result.errors.append(f"筛选失败 {candidate.symbol}: {exc}")
                result.degraded = True

        # 批量生成信号
        try:
            result.signals = self._signal_generator.generate_batch(
                candidates, result.sector_assessments, screening_results, current_date
            )
        except Exception as exc:
            logger.error("信号生成失败: %s", exc)
            result.errors.append(f"信号生成失败: {exc}")
            result.degraded = True
            return result

        # 6. 持久化
        if self._repository is not None and result.signals:
            try:
                result.saved_count = self._repository.save_signals_batch(result.signals)
            except Exception as exc:
                logger.error("信号持久化失败: %s", exc)
                result.errors.append(f"持久化失败: {exc}")
                result.degraded = True

        return result

    def run_for_candidates(
        self,
        candidates: list[QuantCandidate],
        stock_pool_today: pd.DataFrame | None = None,
        current_date: str = "",
        *,
        daily_data_map: dict[str, pd.DataFrame] | None = None,
    ) -> PipelineResult:
        """对已适配的 QuantCandidate 列表执行 Pipeline。

        跳过适配步骤，直接从板块评估开始。
        适用于上游已是 QuantCandidate 的场景。

        Args:
            candidates: QuantCandidate 列表。
            stock_pool_today: 当日全市场个股数据；
                None 时从 data_query 自动拉取（data_query 必须非 None）。
            current_date: 当前日期。
            daily_data_map: 预加载日线数据映射。

        Returns:
            PipelineResult 运行结果。
        """
        result = PipelineResult()

        if not candidates:
            result.errors.append("候选列表为空")
            return result

        # 自动拉取当日全市场快照
        if stock_pool_today is None:
            if self._data_query is None:
                result.errors.append("stock_pool_today=None 且未配置 data_query")
                result.degraded = True
                return result
            try:
                stock_pool_today = self._data_query.get_stock_pool(current_date)
                if stock_pool_today is None or stock_pool_today.empty:
                    result.errors.append(f"data_query.get_stock_pool({current_date}) 返回空")
                    result.degraded = True
                    return result
            except Exception as exc:
                logger.error("自动拉取当日快照失败: %s", exc)
                result.errors.append(f"拉取当日快照失败: {exc}")
                result.degraded = True
                return result

        # Research 准入门禁（P0-1）
        self._apply_admission(candidates, current_date, result)

        # 板块评估
        try:
            result.sector_assessments = self._sector_engine.assess(
                stock_pool_today, current_date
            )
            if not result.sector_assessments:
                result.errors.append("板块评估为空")
                result.degraded = True
        except Exception as exc:
            logger.error("板块评估失败: %s", exc)
            result.errors.append(f"板块评估失败: {exc}")
            result.degraded = True

        # 筛选（P0-4：按 candidate_id 存储）
        screening_results: dict[str, ScreeningResult] = {}
        for candidate in candidates:
            # 门禁未通过：跳过筛选，由信号生成器统一标 REJECTED
            if not candidate.admission_passed:
                continue
            # P0-4：缺 candidate_id 不得进入筛选
            if not candidate.candidate_id:
                candidate.degraded = True
                if "candidate_id" not in candidate.degraded_fields:
                    candidate.degraded_fields.append("candidate_id")
                continue
            try:
                # P0 板块名修复：用 symbol 反查板块，不用 sector_id 字符串匹配
                sector_name, sector_source = self._sector_engine.get_sector_for_symbol_with_fallback(
                    candidate.symbol, candidate.sector_id
                )
                # 用反查到的中文板块名更新 candidate.sector_id，供后续 signal_generator 使用
                if sector_name:
                    candidate.sector_id = sector_name
                sector_assessment = (
                    result.sector_assessments.get(sector_name)
                    if sector_name else None
                )
                if sector_assessment and sector_source:
                    tag = f"sector_source:{sector_source}"
                    if tag not in sector_assessment.degraded_fields:
                        sector_assessment.degraded_fields.append(tag)
                daily_data = None
                if daily_data_map is not None:
                    daily_data = daily_data_map.get(candidate.symbol)
                screening = self._screener.screen(
                    candidate, sector_assessment, current_date,
                    daily_data=daily_data,
                )
                screening_results[candidate.candidate_id] = screening
                if screening.degraded:
                    result.degraded = True
            except Exception as exc:
                logger.warning("候选 %s 筛选失败: %s", candidate.symbol, exc)
                result.errors.append(f"筛选失败 {candidate.symbol}: {exc}")
                result.degraded = True

        # 信号生成
        try:
            result.signals = self._signal_generator.generate_batch(
                candidates, result.sector_assessments, screening_results, current_date
            )
        except Exception as exc:
            logger.error("信号生成失败: %s", exc)
            result.errors.append(f"信号生成失败: {exc}")
            result.degraded = True
            return result

        # 持久化
        if self._repository is not None and result.signals:
            try:
                result.saved_count = self._repository.save_signals_batch(result.signals)
            except Exception as exc:
                logger.error("信号持久化失败: %s", exc)
                result.errors.append(f"持久化失败: {exc}")
                result.degraded = True

        return result

    # ------------------------------------------------------------------ #
    # 主题控制面入口
    # ------------------------------------------------------------------ #

    def run_with_themes(
        self,
        theme_packets: list[ThemePacket],
        *,
        current_date: str = "",
        daily_data_map: dict[str, pd.DataFrame] | None = None,
        theme_screener: ThemeDailyScreener | None = None,
        enforce_research_gate: bool = False,
    ) -> PipelineResult:
        """基于 ThemePacket 执行 Pipeline。

        与 run_daily 的差异：
        - 跳过 adapter / gate / sector 阶段
        - 由 CandidateQuotaGenerator 从 ThemePacket 生成候选
        - 由 ThemeDailyScreener 执行日线筛选（不依赖 SectorAssessment）
        - 由 SignalGenerator.generate_from_themes 生成信号
        - 同 source_snapshot_id 贯穿整个流程

        流程：
        1. candidate_quota.generate(theme_packets) → QuantCandidate 列表
        2. theme_screener.screen(candidates, theme_packets) → ThemeScreeningResult 列表
        3. signal_generator.generate_from_themes(candidates, theme_packets,
           screening_results) → QuantSignal 列表
        4. 持久化（如果配置了 repository）

        Args:
            theme_packets: ThemePacket 列表。
            current_date: 当前日期（ISO 格式）。
            daily_data_map: 预加载的个股日线数据 {symbol: DataFrame}；
                None 时 ThemeDailyScreener 内部不查日线，所有候选降级为 UNKNOWN。
            theme_screener: 自定义 ThemeDailyScreener；None 时用默认实例。

        Returns:
            PipelineResult，含 theme_candidates / theme_screening_results /
            signals 三个字段。
        """
        result = PipelineResult()

        if not theme_packets:
            result.errors.append("theme_packets 为空")
            return result

        # 1. 候选生成
        try:
            quota_generator = CandidateQuotaGenerator(
                enforce_research_gate=enforce_research_gate
            )
            funnel_result = quota_generator.generate_with_funnel(theme_packets)
            candidates = funnel_result.candidates
            result.theme_candidates = candidates
            result.candidate_funnel = funnel_result.to_dict()
            if not candidates:
                # 结构化错误：附漏斗诊断，前端可展示"为什么没有候选"
                result.errors.append("候选生成结果为空")
                result.degraded = True
                return result
            if enforce_research_gate:
                self._apply_admission(candidates, current_date, result)
        except Exception as exc:
            logger.error("候选生成失败: %s", exc)
            result.errors.append(f"候选生成失败: {exc}")
            result.degraded = True
            return result

        # 2. 主题日线筛选
        screener = theme_screener if theme_screener is not None else ThemeDailyScreener()
        try:
            screening_candidates = (
                [candidate for candidate in candidates if candidate.admission_passed]
                if enforce_research_gate else candidates
            )
            screening_results = screener.screen(
                screening_candidates,
                theme_packets,
                daily_data_map=daily_data_map,
                as_of=current_date,
            )
            result.theme_screening_results = screening_results
            # 任一筛选结果降级 → 整体降级
            if any(r.degraded for r in screening_results):
                result.degraded = True
        except Exception as exc:
            logger.error("主题筛选失败: %s", exc)
            result.errors.append(f"主题筛选失败: {exc}")
            result.degraded = True
            # 筛选失败时仍尝试生成信号（screening_results 为空，全部走 UNKNOWN 路径）
            screening_results = []

        # 3. 信号生成
        try:
            result.signals = self._signal_generator.generate_from_themes(
                candidates,
                theme_packets,
                screening_results,
                current_date=current_date,
            )
        except Exception as exc:
            logger.error("主题信号生成失败: %s", exc)
            result.errors.append(f"主题信号生成失败: {exc}")
            result.degraded = True
            return result

        # 4. 持久化
        if self._repository is not None and result.signals:
            try:
                result.saved_count = self._repository.save_signals_batch(result.signals)
            except Exception as exc:
                logger.error("主题信号持久化失败: %s", exc)
                result.errors.append(f"持久化失败: {exc}")
                result.degraded = True

        return result

    def _apply_admission(
        self,
        candidates: list[QuantCandidate],
        current_date: str,
        result: PipelineResult,
    ) -> None:
        """对所有候选执行 Research 准入门禁，就地更新候选的 admission_passed 字段。

        门禁失败的候选仍保留在候选列表中，由 SignalGenerator 标记为
        REJECTED + RESEARCH_GATE_FAIL，但不进入日线筛选阶段。

        Args:
            candidates: 候选列表（就地修改 admission_passed 等字段）。
            current_date: PIT 时点。
            result: Pipeline 结果（追加 admission_results 和降级标记）。
        """
        for candidate in candidates:
            try:
                admission = check_admission(
                    candidate,
                    research_repository=self._research_repository,
                    as_of=current_date,
                )
                apply_admission_to_candidate(candidate, admission)
                result.admission_results[candidate.candidate_id] = admission
                if admission.degraded:
                    result.degraded = True
                if not admission.passed:
                    logger.info(
                        "候选 %s 准入门禁未通过: %s",
                        candidate.candidate_id,
                        admission.reasons,
                    )
            except Exception as exc:
                logger.error("准入门禁异常 %s: %s", candidate.candidate_id, exc)
                candidate.admission_passed = False
                candidate.degraded = True
                if "admission" not in candidate.degraded_fields:
                    candidate.degraded_fields.append("admission")
                result.degraded = True
                result.errors.append(f"门禁异常 {candidate.candidate_id}: {exc}")
