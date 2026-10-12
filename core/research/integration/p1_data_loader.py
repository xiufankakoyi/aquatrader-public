"""P1.10 真实数据接入桥接层与合成数据工厂。

按 P1 方案 §5 Task P1.10 与 §6 失败停止规则：
- 若 tushare 可用：从 tushare 拉取 finance_snapshot / income / cashflow /
  fina_indicator / valuation_history / consensus_estimates 填充到
  P1.1 StatementRepository（内存版）与 P1.7 ExpectationRepository 与
  P1.8 ValuationRepository；不人工造数（AGENTS.md）。
- 若 tushare 不可用或某字段缺失：返回 LoadResult 标注「真实数据接入待后续工作」，
  不阻塞自动化测试（用合成数据）。

设计要点：
- 复用 P1.1-P1.8 已实现契约，不重新发明数据结构
- 防御性编程：任一 provider 调用失败时降级，不抛异常
- DRY：通过 _safe_provider_call 统一封装异常处理
- 不输出买入/卖出/仓位/止盈/止损等投资建议（AGENTS.md）
- 不凭空编造公司业务、概念归属、收入占比或证据来源（AGENTS.md）

合成数据工厂 SyntheticDataFactory 用于 50 公司认证冒烟测试：
- 生成完整的 NormalizedFinancialStatement / OperatingModel / ValuationCase 等
- 明确标注为「synthetic_smoke_test」，source_ref 以 "synthetic_" 前缀标识
- 仅供测试框架使用，**绝不进入生产环境**
"""
from __future__ import annotations

import logging
import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from core.research.fundamentals.contracts import (
    NormalizedFinancialStatement,
    StatementLine,
)
from core.research.operating_model.contracts import (
    OperatingDriver,
    OperatingModel,
    Segment,
    ForwardAssumption,
    BUSINESS_MODEL_MANUFACTURING,
    DRIVER_KIND_PRODUCT_REVENUE,
    DRIVER_KIND_ASP,
    DRIVER_KIND_GROSS_MARGIN,
    DRIVER_KIND_SHARE_COUNT,
)
from core.research.valuation.contracts import (
    Computability,
    SensitivityPoint,
    ValuationAssumption,
    ValuationCase,
    ValuationInput,
    ValuationMethod,
    ValuationReport,
)
from core.research.valuation.engine import ValuationEngine
from core.research.earnings.interval import Interval
from core.research.expectations.contracts import ConsensusEstimate, ExpectationValue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 50 公司样本（与 docs/research/institutional/p1-50-companies-sample.md 一致）
# ---------------------------------------------------------------------------

COMPANY_SAMPLE: list[dict[str, str]] = [
    # 银行（6 家）
    {"symbol": "600036.SH", "name": "招商银行", "industry": "银行"},
    {"symbol": "601398.SH", "name": "工商银行", "industry": "银行"},
    {"symbol": "601288.SH", "name": "农业银行", "industry": "银行"},
    {"symbol": "601939.SH", "name": "建设银行", "industry": "银行"},
    {"symbol": "600000.SH", "name": "浦发银行", "industry": "银行"},
    {"symbol": "000001.SZ", "name": "平安银行", "industry": "银行"},
    # 保险（4 家）
    {"symbol": "601318.SH", "name": "中国平安", "industry": "保险"},
    {"symbol": "601628.SH", "name": "中国人寿", "industry": "保险"},
    {"symbol": "601601.SH", "name": "中国太保", "industry": "保险"},
    {"symbol": "002318.SZ", "name": "新华保险", "industry": "保险"},
    # 证券（4 家）
    {"symbol": "600030.SH", "name": "中信证券", "industry": "证券"},
    {"symbol": "601688.SH", "name": "华泰证券", "industry": "证券"},
    {"symbol": "600999.SH", "name": "招商证券", "industry": "证券"},
    {"symbol": "000776.SZ", "name": "广发证券", "industry": "证券"},
    # 白酒（6 家）
    {"symbol": "600519.SH", "name": "贵州茅台", "industry": "白酒"},
    {"symbol": "000858.SZ", "name": "五粮液", "industry": "白酒"},
    {"symbol": "000568.SZ", "name": "泸州老窖", "industry": "白酒"},
    {"symbol": "002304.SZ", "name": "洋河股份", "industry": "白酒"},
    {"symbol": "000596.SZ", "name": "古井贡酒", "industry": "白酒"},
    {"symbol": "600809.SH", "name": "山西汾酒", "industry": "白酒"},
    # 医药（8 家）
    {"symbol": "600276.SH", "name": "恒瑞医药", "industry": "医药"},
    {"symbol": "000538.SZ", "name": "云南白药", "industry": "医药"},
    {"symbol": "600436.SH", "name": "片仔癀", "industry": "医药"},
    {"symbol": "000999.SZ", "name": "华润三九", "industry": "医药"},
    {"symbol": "600196.SH", "name": "复星医药", "industry": "医药"},
    {"symbol": "300003.SZ", "name": "乐普医疗", "industry": "医药"},
    {"symbol": "300015.SZ", "name": "爱尔眼科", "industry": "医药"},
    {"symbol": "603259.SH", "name": "药明康德", "industry": "医药"},
    # 新能源（8 家）
    {"symbol": "300750.SZ", "name": "宁德时代", "industry": "新能源"},
    {"symbol": "002594.SZ", "name": "比亚迪", "industry": "新能源"},
    {"symbol": "601012.SH", "name": "隆基绿能", "industry": "新能源"},
    {"symbol": "600438.SH", "name": "通威股份", "industry": "新能源"},
    {"symbol": "002709.SZ", "name": "天赐材料", "industry": "新能源"},
    {"symbol": "300274.SZ", "name": "阳光电源", "industry": "新能源"},
    {"symbol": "600089.SH", "name": "特变电工", "industry": "新能源"},
    {"symbol": "603799.SH", "name": "华友钴业", "industry": "新能源"},
    # TMT（7 家，剔除占位）
    {"symbol": "002415.SZ", "name": "海康威视", "industry": "TMT"},
    {"symbol": "000063.SZ", "name": "中兴通讯", "industry": "TMT"},
    {"symbol": "600050.SH", "name": "中国联通", "industry": "TMT"},
    {"symbol": "600100.SH", "name": "同方股份", "industry": "TMT"},
    {"symbol": "000977.SZ", "name": "浪潮信息", "industry": "TMT"},
    {"symbol": "002230.SZ", "name": "科大讯飞", "industry": "TMT"},
    {"symbol": "300059.SZ", "name": "东方财富", "industry": "TMT"},
    # 消费（8 家）
    {"symbol": "600887.SH", "name": "伊利股份", "industry": "消费"},
    {"symbol": "603288.SH", "name": "海天味业", "industry": "消费"},
    {"symbol": "000895.SZ", "name": "双汇发展", "industry": "消费"},
    {"symbol": "603899.SH", "name": "晨光股份", "industry": "消费"},
    {"symbol": "600690.SH", "name": "海尔智家", "industry": "消费"},
    {"symbol": "000333.SZ", "name": "美的集团", "industry": "消费"},
    {"symbol": "000651.SZ", "name": "格力电器", "industry": "消费"},
    {"symbol": "603214.SH", "name": "爱婴室", "industry": "消费"},
]

# 行业分布汇总
INDUSTRY_DISTRIBUTION: dict[str, int] = {}
for _item in COMPANY_SAMPLE:
    INDUSTRY_DISTRIBUTION[_item["industry"]] = INDUSTRY_DISTRIBUTION.get(_item["industry"], 0) + 1


# ---------------------------------------------------------------------------
# 加载结果统计
# ---------------------------------------------------------------------------

@dataclass
class LoadResult:
    """加载结果统计。

    Attributes:
        symbol: 公司代码
        success: 是否成功加载至少一项数据
        statements_loaded: 已加载的报表数量
        consensus_loaded: 已加载的一致预期数量
        valuation_loaded: 已加载的估值历史数量
        operating_models_loaded: 已加载的逐页复核年报经营模型数量
        errors: 错误信息列表
        data_source: 数据源（"tushare" / "synthetic" / "none"）
    """

    symbol: str = ""
    success: bool = False
    statements_loaded: int = 0
    consensus_loaded: int = 0
    valuation_loaded: int = 0
    operating_models_loaded: int = 0
    errors: list[str] = field(default_factory=list)
    data_source: str = "none"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "success": self.success,
            "statements_loaded": self.statements_loaded,
            "consensus_loaded": self.consensus_loaded,
            "valuation_loaded": self.valuation_loaded,
            "operating_models_loaded": self.operating_models_loaded,
            "errors": list(self.errors),
            "data_source": self.data_source,
        }


# ---------------------------------------------------------------------------
# 真实数据加载器
# ---------------------------------------------------------------------------

class P1DataLoader:
    """从 tushare / akshare 拉取真实数据填充到 P1.1-P1.8 领域仓储。

    设计原则：
    - 防御性编程：任一 provider 调用失败时降级，不抛异常
    - 不人工造数：仅保存 provider 返回的真实数据
    - 不输出投资建议：仅填充结构化字段，不做评价
    - 真实数据接入待后续工作：当 tushare 不可用时返回空 LoadResult

    Attributes:
        fundamentals_repo: StatementRepository 实例（P1.1）
        expectation_repo: ExpectationRepository 实例（P1.7）
        valuation_repo: ValuationRepository 实例（P1.8）
        operating_repo: OperatingModelRepository 实例（P1.3）
        annual_report_store: 已复核年报证据清单读取器
        peer_universe: 显式同业候选集；为空时不生成可比公司
        provider: tushare 数据提供者实例
    """

    def __init__(
        self,
        fundamentals_repo: Any = None,
        expectation_repo: Any = None,
        valuation_repo: Any = None,
        operating_repo: Any = None,
        annual_report_store: Any = None,
        peer_universe: list[dict[str, str]] | None = None,
        provider: Any = None,
    ) -> None:
        """初始化加载器。

        Args:
            fundamentals_repo: P1.1 StatementRepository 实例
            expectation_repo: P1.7 ExpectationRepository 实例
            valuation_repo: P1.8 ValuationRepository 实例
            operating_repo: P1.3 OperatingModelRepository 实例
            annual_report_store: 已复核年报证据清单读取器
            peer_universe: 显式同业候选集
            provider: 可选的 tushare provider 实例；为 None 时延迟初始化
        """
        self.fundamentals_repo = fundamentals_repo
        self.expectation_repo = expectation_repo
        self.valuation_repo = valuation_repo
        self.operating_repo = operating_repo
        self.annual_report_store = annual_report_store
        self.peer_universe = list(peer_universe or [])
        self._provider: Any = provider

    @property
    def provider(self) -> Any:
        """惰性初始化 tushare provider。"""
        if self._provider is None:
            try:
                from infrastructure.data_providers.market.tushare_provider import (
                    TushareProvider,
                )
                self._provider = TushareProvider()
            except Exception as exc:
                logger.warning("Tushare provider 初始化失败: %s", exc)
                self._provider = None
        return self._provider

    def is_real_data_available(self) -> bool:
        """检查真实数据源是否可用。

        Returns:
            True 当 tushare provider 可用且 token 已配置
        """
        provider = self.provider
        if provider is None:
            return False
        try:
            return bool(provider.is_available())
        except Exception:
            return False

    def load_company(
        self,
        symbol: str,
        as_of: str,
        lookback_days: int = 365,
    ) -> LoadResult:
        """加载单个公司的真实数据并填充到领域仓储。

        Args:
            symbol: 公司代码（如 "600519.SH"）
            as_of: PIT 时点（YYYY-MM-DD）
            lookback_days: 历史数据回看天数，默认 365

        Returns:
            LoadResult 加载结果统计
        """
        result = LoadResult(symbol=symbol, data_source="tushare")
        result.operating_models_loaded = self._load_operating_model(symbol, as_of)
        if not self.is_real_data_available():
            result.success = result.operating_models_loaded > 0
            result.data_source = "annual_report" if result.success else "none"
            result.errors.append("tushare_provider_unavailable")
            return result

        # 仅取 6 位数字代码作为 provider 入参
        symbol_digits = symbol.split(".")[0] if "." in symbol else symbol

        # 1. 加载 finance_snapshot -> StatementRepository
        result.statements_loaded = self._load_statements(symbol, symbol_digits, as_of)

        # 2. 加载 consensus_estimates -> ExpectationRepository
        result.consensus_loaded = self._load_consensus(symbol, symbol_digits, as_of, lookback_days)

        # 3. 加载 valuation_history -> ValuationRepository
        result.valuation_loaded = self._load_valuation_history(symbol, symbol_digits, as_of, lookback_days)

        result.success = (
            result.statements_loaded > 0
            or result.consensus_loaded > 0
            or result.valuation_loaded > 0
            or result.operating_models_loaded > 0
        )
        if not result.success:
            result.errors.append("no_data_returned_from_provider")
        return result

    def load_batch(
        self,
        symbols: list[str],
        as_of: str,
        lookback_days: int = 365,
    ) -> list[LoadResult]:
        """批量加载多家公司的真实数据。

        Args:
            symbols: 公司代码列表
            as_of: PIT 时点
            lookback_days: 历史数据回看天数

        Returns:
            LoadResult 列表，顺序与 symbols 一致
        """
        if not self.is_real_data_available():
            results: list[LoadResult] = []
            for symbol in symbols:
                operating_loaded = self._load_operating_model(symbol, as_of)
                results.append(LoadResult(
                    symbol=symbol,
                    data_source="annual_report" if operating_loaded else "none",
                    success=operating_loaded > 0,
                    operating_models_loaded=operating_loaded,
                    errors=["tushare_provider_unavailable"],
                ))
            return results

        try:
            consensus = self.provider.get_consensus_estimates(
                start_date=self._subtract_days(as_of, lookback_days),
                end_date=as_of,
                symbols=[symbol.split(".")[0] for symbol in symbols],
            )
        except Exception as exc:
            logger.warning("consensus_estimates batch load failed: %s", exc)
            consensus = pd.DataFrame()

        results: list[LoadResult] = []
        for symbol in symbols:
            symbol_digits = symbol.split(".")[0] if "." in symbol else symbol
            result = LoadResult(symbol=symbol, data_source="tushare")
            result.operating_models_loaded = self._load_operating_model(symbol, as_of)
            result.statements_loaded = self._load_statements(symbol, symbol_digits, as_of)
            result.consensus_loaded = self._load_consensus(
                symbol, symbol_digits, as_of, lookback_days, prefetched=consensus,
            )
            result.valuation_loaded = self._load_valuation_history(
                symbol, symbol_digits, as_of, lookback_days,
            )
            result.success = any((
                result.statements_loaded,
                result.consensus_loaded,
                result.valuation_loaded,
                result.operating_models_loaded,
            ))
            if not result.success:
                result.errors.append("no_data_returned_from_provider")
            results.append(result)
        return results

    # ---- 内部加载方法 ----

    def _load_operating_model(self, symbol: str, as_of: str) -> int:
        """Load one locally reviewed annual-report operating model, if available."""
        if self.operating_repo is None or self.annual_report_store is None:
            return 0
        try:
            model = self.annual_report_store.build(symbol, as_of)
            if model is None:
                return 0
            existing = self.operating_repo.get_latest(symbol, model.period, as_of=as_of)
            if existing is not None and existing.source_trace_id == model.source_trace_id:
                return 1
            version = 1
            if existing is not None:
                versions = self.operating_repo.list_versions(symbol, model.period)
                version = max(versions, default=0) + 1
            self.operating_repo.save(model, version=version)
            return 1
        except Exception as exc:
            logger.warning("annual report operating model load failed %s: %s", symbol, exc)
            return 0

    def _load_statements(
        self,
        symbol: str,
        symbol_digits: str,
        as_of: str,
    ) -> int:
        """从 tushare finance_snapshot 加载三表数据到 StatementRepository。

        Args:
            symbol: 公司代码（含 .SH/.SZ 后缀）
            symbol_digits: 6 位数字代码
            as_of: PIT 时点

        Returns:
            已加载的报表数量
        """
        if self.fundamentals_repo is None:
            return 0
        try:
            df = self.provider.get_finance_snapshot(
                symbols=[symbol_digits], as_of=as_of, periods=8,
            )
        except Exception as exc:
            logger.warning("finance_snapshot 加载失败 %s: %s", symbol, exc)
            return 0
        if df is None or df.empty:
            return 0

        loaded = 0
        for row in df.to_dict(orient="records"):
            try:
                stmt = self._row_to_statement(symbol, row)
                if stmt is not None:
                    # StatementRepository.save 同 (symbol, period, version) 重复抛异常
                    # 这里用 try/except 防止重复保存导致整批失败
                    try:
                        self.fundamentals_repo.save(stmt)
                        loaded += 1
                    except ValueError:
                        # 已存在则跳过（重复加载幂等）
                        pass
            except Exception as exc:
                logger.warning("转报表失败 %s: %s", symbol, exc)
        return loaded

    def _row_to_statement(
        self,
        symbol: str,
        row: Mapping[str, Any],
    ) -> NormalizedFinancialStatement | None:
        """将 finance_snapshot 行转为 NormalizedFinancialStatement。

        Args:
            symbol: 公司代码
            row: finance_snapshot DataFrame 行

        Returns:
            NormalizedFinancialStatement；缺关键字段返回 None
        """
        period_raw = self._clean_text(row.get("period"))
        period = self._date_text(period_raw)
        if not period:
            return None
        published_at = self._date_text(row.get("published_at")) or period

        source_ref = f"tushare:finance_snapshot:{symbol}:{period_raw}"
        currency = "CNY"
        unit = "元"

        def _line(metric_name: str, value: Any) -> StatementLine:
            """构造 StatementLine。"""
            try:
                v = float(value) if not self._is_missing(value) else None
            except (OverflowError, TypeError, ValueError):
                v = None
            if v is not None and not math.isfinite(v):
                v = None
            grounding = "grounding_ok" if v is not None else "missing"
            return StatementLine(
                metric_name=metric_name,
                value=v,
                unit=unit,
                currency=currency,
                source_ref=source_ref,
                grounding_status=grounding,
                reported_value=v,
            )

        def _append_line(
            target: list[StatementLine], metric_name: str, value: Any,
        ) -> None:
            line = _line(metric_name, value)
            if line.value is not None:
                target.append(line)

        income_lines: list[StatementLine] = []
        if not self._is_missing(row.get("revenue")):
            _append_line(income_lines, "revenue", row.get("revenue"))
        if not self._is_missing(row.get("net_profit")):
            _append_line(income_lines, "net_profit", row.get("net_profit"))

        cash_flow_lines: list[StatementLine] = []
        for metric_name in (
            "operating_cash_flow", "cash_change",
            "cash_and_equivalents_begin", "cash_and_equivalents_end",
        ):
            if not self._is_missing(row.get(metric_name)):
                _append_line(cash_flow_lines, metric_name, row.get(metric_name))

        balance_lines: list[StatementLine] = []
        for metric_name in (
            "total_assets",
            "total_liabilities",
            "total_equity",
            "minority_interest",
            "total_debt",
            "cash_and_equivalents",
        ):
            if not self._is_missing(row.get(metric_name)):
                _append_line(balance_lines, metric_name, row.get(metric_name))

        if not income_lines and not cash_flow_lines:
            return None

        return NormalizedFinancialStatement(
            symbol=symbol,
            period=period,
            published_at=published_at,
            available_at=published_at,
            currency=currency,
            unit=unit,
            income_statement=income_lines,
            balance_sheet=balance_lines,
            cash_flow_statement=cash_flow_lines,
            adjustments=[],
            accounting_policy_refs=[],
            restatement_version=1,
            source_trace_id=source_ref,
            schema_version="normalized_statement_v1",
        )

    def _load_consensus(
        self,
        symbol: str,
        symbol_digits: str,
        as_of: str,
        lookback_days: int,
        prefetched: pd.DataFrame | None = None,
    ) -> int:
        """从 tushare consensus_estimates 加载一致预期并持久化。

        Args:
            symbol: 公司代码
            symbol_digits: 6 位数字代码
            as_of: PIT 时点
            lookback_days: 回看天数

        Returns:
            已写入或更新的一致预期条数
        """
        if self.expectation_repo is None:
            return 0
        if prefetched is not None:
            df = prefetched
            if not df.empty and "symbol" in df:
                df = df[df["symbol"].astype(str).str.startswith(symbol_digits)]
        try:
            if prefetched is None:
                start_date = self._subtract_days(as_of, lookback_days)
                df = self.provider.get_consensus_estimates(
                    start_date=start_date, end_date=as_of, symbols=[symbol_digits],
                )
        except Exception as exc:
            logger.warning("consensus_estimates 加载失败 %s: %s", symbol, exc)
            return 0
        if df is None or df.empty:
            return 0

        loaded = 0
        metric_specs = (
            ("revenue_forecast_yuan", "revenue", "元", "CNY"),
            ("operating_profit_forecast_yuan", "operating_profit", "元", "CNY"),
            ("total_profit_forecast_yuan", "total_profit", "元", "CNY"),
            ("net_profit_forecast_yuan", "net_profit", "元", "CNY"),
            ("eps_forecast", "eps", "元/股", "CNY"),
            ("pe_forecast", "pe", "倍", ""),
            ("roe_forecast", "roe", "比例", ""),
        )
        for row in df.to_dict(orient="records"):
            period = self._clean_text(row.get("forecast_period"))
            published_at = self._date_text(row.get("report_date"))
            if not period or not published_at:
                continue
            provider = self._clean_text(row.get("provider")) or "tushare"
            institution = self._clean_text(row.get("org_name")) or provider
            analyst = self._clean_text(row.get("author_name"))
            retrieved_at = self._clean_text(row.get("updated_at")) or published_at
            source_ref = (
                f"{provider}:consensus:{symbol}:{published_at}:"
                f"{institution}:{analyst}:{period}"
            )
            values: list[ExpectationValue] = []
            for column, metric, unit, currency in metric_specs:
                value = self._coerce_number(row.get(column))
                if value is None:
                    continue
                values.append(ExpectationValue(
                    metric=metric,
                    value=Interval(float(value), float(value)),
                    unit=unit,
                    currency=currency,
                    period=period,
                    as_of=published_at,
                    data_version=retrieved_at,
                    source_claim_id=source_ref,
                    source_evidence_id=source_ref,
                    extraction_method="provider_structured",
                    confidence=Interval(0.0, 1.0),
                    published_at=published_at,
                    retrieved_at=retrieved_at,
                ))
            if not values:
                continue
            consensus_id = "cons_" + hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:16]
            estimate = ConsensusEstimate(
                consensus_id=consensus_id,
                company_symbol=symbol,
                values=values,
                provider=provider,
                institution_id=institution,
                analyst_id=analyst,
                sample_size=1,
                created_at=retrieved_at,
            )
            try:
                self.expectation_repo.save("consensus_estimates", estimate)
                loaded += 1
            except Exception as exc:
                logger.warning("一致预期保存失败 %s: %s", symbol, exc)
        return loaded

    def _load_valuation_history(
        self,
        symbol: str,
        symbol_digits: str,
        as_of: str,
        lookback_days: int,
    ) -> int:
        """从 tushare valuation_history 构建历史 PE/PB/PS 案例并持久化。

        Args:
            symbol: 公司代码
            symbol_digits: 6 位数字代码
            as_of: PIT 时点
            lookback_days: 回看天数

        Returns:
            已写入或更新的估值案例数量
        """
        if self.valuation_repo is None:
            return 0
        try:
            start_date = self._subtract_days(as_of, lookback_days)
            df = self.provider.get_valuation_history(
                start_date=start_date, end_date=as_of, symbols=[symbol_digits],
            )
        except Exception as exc:
            logger.warning("valuation_history 加载失败 %s: %s", symbol, exc)
            return 0
        if df is None or df.empty:
            return 0

        history = df.copy()
        if "trade_date" not in history.columns:
            return 0
        history["trade_date"] = history["trade_date"].map(self._date_text)
        history = history[
            history["trade_date"].ne("") & history["trade_date"].le(as_of[:10])
        ].sort_values("trade_date")
        if history.empty:
            return 0

        latest = history.iloc[-1]

        def _positive(value: Any) -> float:
            number = self._coerce_number(value)
            return number if number is not None and number > 0 else 0.0

        def _series(column: str) -> list[float]:
            if column not in history.columns:
                return []
            values = pd.to_numeric(history[column], errors="coerce").dropna()
            return [float(value) for value in values if float(value) > 0]

        price = _positive(latest.get("close"))
        pe = _positive(latest.get("pe_ttm"))
        pb = _positive(latest.get("pb"))
        ps = _positive(latest.get("ps_ttm"))
        market_cap = _positive(latest.get("total_market_cap"))
        shares = market_cap / price if market_cap > 0 and price > 0 else 0.0
        provider = str(latest.get("provider") or "tushare")
        market_version = str(latest.get("updated_at") or latest.get("trade_date") or "")
        source_ref = f"{provider}:valuation_history:{symbol}:{latest.get('trade_date')}"
        comparable_companies = self._load_comparable_peers(
            symbol=symbol,
            target_market_cap=market_cap,
            target_trade_date=str(latest.get("trade_date") or as_of),
        )
        financial_position: dict[str, float] = {}
        financial_version = ""
        if self.fundamentals_repo is not None:
            try:
                statement = self.fundamentals_repo.get_latest_statement(symbol, as_of=as_of)
                if statement is not None:
                    financial_version = str(statement.available_at or statement.published_at or "")
                    financial_position = {
                        line.metric_name: float(line.value)
                        for line in statement.balance_sheet
                        if line.value is not None and line.metric_name in {
                            "total_debt", "cash_and_equivalents", "minority_interest",
                        }
                    }
            except Exception as exc:
                logger.warning("valuation financial inputs unavailable %s: %s", symbol, exc)
        inputs = ValuationInput(
            company_symbol=symbol,
            as_of=str(latest.get("trade_date")),
            industry=self._target_industry(symbol),
            current_price=price,
            shares_outstanding=shares,
            diluted_shares=shares,
            market_cap=market_cap,
            total_debt=financial_position.get("total_debt", 0.0),
            cash_and_equivalents=financial_position.get("cash_and_equivalents", 0.0),
            minority_interest=financial_position.get("minority_interest", 0.0),
            historical_multiples={
                ValuationMethod.HISTORICAL_PE.value: _series("pe_ttm"),
                ValuationMethod.HISTORICAL_PB.value: _series("pb"),
                ValuationMethod.HISTORICAL_PS.value: _series("ps_ttm"),
            },
            comparable_companies=comparable_companies,
            trailing_eps=price / pe if pe > 0 else 0.0,
            book_value_per_share=price / pb if pb > 0 else 0.0,
            revenue_per_share=price / ps if ps > 0 else 0.0,
            market_data_version=market_version,
            financial_data_version=financial_version,
            model_version="p1_real_history_v1",
        )
        engine = ValuationEngine()
        loaded = 0
        for method in (
            ValuationMethod.HISTORICAL_PE.value,
            ValuationMethod.HISTORICAL_PB.value,
            ValuationMethod.HISTORICAL_PS.value,
        ):
            case = engine.compute_single(inputs, method)
            if not case.is_computable():
                continue
            stable_key = f"{source_ref}:{method}"
            case.case_id = "valc_" + hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:16]
            case.source_refs = [source_ref]
            try:
                self.valuation_repo.save_case(case)
                loaded += 1
            except Exception as exc:
                logger.warning("估值案例保存失败 %s/%s: %s", symbol, method, exc)
        return loaded

    def _load_comparable_peers(
        self,
        symbol: str,
        target_market_cap: float,
        target_trade_date: str,
    ) -> list[dict[str, Any]]:
        """Return real PE peers on a documented, uniform comparison basis.

        The candidate universe is explicit. Peers must share the target's industry
        and an explicitly sourced business-structure tag, use the latest observation
        no later than the target date, and have market capitalisation between 0.25x
        and 4x the target. Missing evidence or fields are dropped.
        """
        if not self.peer_universe or target_market_cap <= 0:
            return []
        target = next((row for row in self.peer_universe if row.get("symbol") == symbol), None)
        if (
            target is None
            or not target.get("industry")
            or not target.get("business_structure")
            or not target.get("business_structure_source_ref")
        ):
            return []
        industry = str(target["industry"])
        business_structure = str(target["business_structure"])
        candidates = [
            row for row in self.peer_universe
            if row.get("symbol") != symbol and row.get("industry") == industry
            and row.get("business_structure") == business_structure
            and bool(row.get("business_structure_source_ref"))
        ]
        if not candidates:
            return []
        candidate_by_symbol = {str(row["symbol"]): row for row in candidates}
        try:
            frame = self.provider.get_valuation_history(
                start_date=self._subtract_days(target_trade_date, 10),
                end_date=target_trade_date,
                symbols=[item.split(".")[0] for item in candidate_by_symbol],
            )
        except Exception as exc:
            logger.warning("comparable peer valuation load failed %s: %s", symbol, exc)
            return []
        if frame is None or frame.empty or "symbol" not in frame.columns or "trade_date" not in frame.columns:
            return []
        data = frame.copy()
        data["symbol"] = data["symbol"].astype(str)
        data["trade_date"] = data["trade_date"].map(self._date_text)
        data = data[data["trade_date"].ne("") & data["trade_date"].le(target_trade_date[:10])]
        peers: list[dict[str, Any]] = []
        for peer_symbol, metadata in candidate_by_symbol.items():
            digits = peer_symbol.split(".")[0]
            rows = data[data["symbol"].map(lambda value: value == peer_symbol or value.split(".")[0] == digits)]
            if rows.empty:
                continue
            row = rows.sort_values("trade_date").iloc[-1]
            pe = self._coerce_number(row.get("pe_ttm"))
            peer_cap = self._coerce_number(row.get("total_market_cap"))
            if pe is None or peer_cap is None or pe <= 0 or peer_cap <= 0:
                continue
            scale_ratio = peer_cap / target_market_cap
            if not 0.25 <= scale_ratio <= 4.0:
                continue
            provider = str(row.get("provider") or "tushare")
            trade_date = str(row.get("trade_date"))
            peers.append({
                "symbol": peer_symbol,
                "name": str(metadata.get("name") or ""),
                "industry": industry,
                "business_structure": business_structure,
                "business_structure_source_ref": str(metadata["business_structure_source_ref"]),
                "target_business_structure_source_ref": str(target["business_structure_source_ref"]),
                "pe": pe,
                "pb": self._optional_positive(row.get("pb")),
                "ps": self._optional_positive(row.get("ps_ttm")),
                "market_cap": peer_cap,
                "scale_ratio": scale_ratio,
                "as_of": trade_date,
                "multiple_type": "pe_ttm",
                "currency": "CNY",
                "accounting_standard": "PRC_ASBE",
                "selection_basis": "same_industry_business_structure_and_market_cap_0.25x_to_4x",
                "source_ref": f"{provider}:valuation_history:{peer_symbol}:{trade_date}",
            })
        return sorted(peers, key=lambda row: row["symbol"])

    def _target_industry(self, symbol: str) -> str:
        target = next((row for row in self.peer_universe if row.get("symbol") == symbol), None)
        return str(target.get("industry") or "") if target else ""

    @staticmethod
    def _optional_positive(value: Any) -> float | None:
        number = P1DataLoader._coerce_number(value)
        return number if number is not None and number > 0 else None

    @staticmethod
    def _coerce_number(value: Any) -> float | None:
        try:
            number = pd.to_numeric(value, errors="coerce")
            if P1DataLoader._is_missing(number):
                return None
            number = float(number)
            return number if math.isfinite(number) else None
        except (OverflowError, TypeError, ValueError):
            return None

    @staticmethod
    def _is_missing(value: Any) -> bool:
        """Handle scalar and malformed provider values without array ambiguity."""
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict, set)):
            return True
        try:
            result = pd.isna(value)
        except (TypeError, ValueError):
            return False
        if isinstance(result, bool):
            return result
        item = getattr(result, "item", None)
        if callable(item):
            try:
                return bool(item())
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _subtract_days(date_str: str, days: int) -> str:
        """从日期字符串减去指定天数。

        Args:
            date_str: YYYY-MM-DD 格式日期
            days: 要减去的天数

        Returns:
            YYYY-MM-DD 格式的新日期
        """
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return (dt - timedelta(days=days)).strftime("%Y-%m-%d")
        except Exception:
            return date_str

    @staticmethod
    def _date_text(value: Any) -> str:
        """将 YYYYMMDD 或 ISO 日期统一为 YYYY-MM-DD；无效值返回空串。"""
        if P1DataLoader._is_missing(value):
            return ""
        text = str(value).strip()
        if not text:
            return ""
        digits = text.replace("-", "")[:8]
        if len(digits) != 8 or not digits.isdigit():
            return ""
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"

    @staticmethod
    def _clean_text(value: Any) -> str:
        """清理 provider 文本字段，避免把 NaN/NA 当成真实内容。"""
        if P1DataLoader._is_missing(value):
            return ""
        return str(value).strip()


# ---------------------------------------------------------------------------
# 合成数据工厂（仅供 50 公司认证冒烟测试使用）
# ---------------------------------------------------------------------------

class SyntheticDataFactory:
    """合成数据工厂，用于 50 公司认证冒烟测试。

    **严格限定用途**：
    - 仅用于 tests/test_p1_50_companies_acceptance.py 的冒烟测试
    - 生成的所有数据 source_ref 以 "synthetic_" 前缀标识
    - **绝不进入生产环境**：生产环境应使用 P1DataLoader 加载真实数据
    - 不输出买入/卖出/仓位/止盈/止损等投资建议

    设计理由（P1 §6 失败停止规则 + AGENTS.md）：
    - 不得为了达到 20 家门槛人工造数（生产环境）
    - 但测试框架需要合成数据验证 CfaReportBuilder 在完整数据下的端到端流程
    - 因此合成数据仅用于测试，明确标注，不冒充真实数据
    """

    SYNTHETIC_PREFIX: str = "synthetic_smoke_test"

    @classmethod
    def build_synthetic_statement(
        cls,
        symbol: str,
        as_of: str,
        period: str = "2024-12-31",
    ) -> NormalizedFinancialStatement:
        """构造合成 NormalizedFinancialStatement。

        Args:
            symbol: 公司代码
            as_of: PIT 时点
            period: 报告期，默认 2024-12-31

        Returns:
            合成的 NormalizedFinancialStatement（带 synthetic_ 前缀 source_ref）
        """
        source_ref = f"{cls.SYNTHETIC_PREFIX}:statement:{symbol}:{period}"
        currency = "CNY"
        unit = "元"

        def _line(name: str, value: float) -> StatementLine:
            """构造合成 StatementLine。"""
            return StatementLine(
                metric_name=name,
                value=value,
                unit=unit,
                currency=currency,
                source_ref=source_ref,
                grounding_status="grounding_ok",
                reported_value=value,
            )

        return NormalizedFinancialStatement(
            symbol=symbol,
            period=period,
            published_at=period,
            available_at=period,
            currency=currency,
            unit=unit,
            income_statement=[
                _line("revenue", 10_000_000_000.0),
                _line("operating_cost", 6_000_000_000.0),
                _line("gross_profit", 4_000_000_000.0),
                _line("operating_profit", 2_000_000_000.0),
                _line("net_profit", 1_500_000_000.0),
            ],
            balance_sheet=[
                _line("total_assets", 20_000_000_000.0),
                _line("total_liabilities", 8_000_000_000.0),
                _line("total_equity", 12_000_000_000.0),
                _line("cash_and_equivalents", 2_000_000_000.0),
            ],
            cash_flow_statement=[
                _line("operating_cash_flow", 1_800_000_000.0),
                _line("investing_cash_flow", -800_000_000.0),
                _line("financing_cash_flow", -500_000_000.0),
            ],
            adjustments=[],
            accounting_policy_refs=[],
            restatement_version=1,
            source_trace_id=source_ref,
            schema_version="normalized_statement_v1",
        )

    @classmethod
    def build_synthetic_operating_model(
        cls,
        symbol: str,
        as_of: str,
        period: str = "2024-12-31",
    ) -> OperatingModel:
        """构造合成 OperatingModel。

        Args:
            symbol: 公司代码
            as_of: PIT 时点
            period: 报告期

        Returns:
            合成的 OperatingModel（带 synthetic_ 前缀 source_ref）
        """
        source_ref = f"{cls.SYNTHETIC_PREFIX}:driver:{symbol}:{period}"
        driver = OperatingDriver(
            name="主力产品销量",
            kind=DRIVER_KIND_PRODUCT_REVENUE,
            value=1_000_000.0,
            unit="件",
            currency="CNY",
            period=period,
            source_ref=source_ref,
            observation_kind="historical_disclosed",
            grounding_status="grounding_ok",
            reported_value=1_000_000.0,
            segment_name="主营业务",
            note="合成数据，仅供冒烟测试",
        )
        asp_driver = OperatingDriver(
            name="主力产品 ASP",
            kind=DRIVER_KIND_ASP,
            value=10_000.0,
            unit="元/件",
            currency="CNY",
            period=period,
            source_ref=source_ref,
            observation_kind="historical_disclosed",
            grounding_status="grounding_ok",
            reported_value=10_000.0,
            segment_name="主营业务",
            note="合成数据，仅供冒烟测试",
        )
        gross_margin_driver = OperatingDriver(
            name="毛利率",
            kind=DRIVER_KIND_GROSS_MARGIN,
            value=0.40,
            unit="ratio",
            currency="CNY",
            period=period,
            source_ref=source_ref,
            observation_kind="historical_disclosed",
            grounding_status="grounding_ok",
            reported_value=0.40,
            segment_name="主营业务",
            note="合成数据，仅供冒烟测试",
        )
        share_count_driver = OperatingDriver(
            name="总股本",
            kind=DRIVER_KIND_SHARE_COUNT,
            value=1_000_000_000.0,
            unit="股",
            currency="",
            period=period,
            source_ref=source_ref,
            observation_kind="historical_disclosed",
            grounding_status="grounding_ok",
            reported_value=1_000_000_000.0,
            segment_name="",
            note="合成数据，仅供冒烟测试",
        )
        forward_assumption = ForwardAssumption(
            name="主力产品销量预测",
            kind=DRIVER_KIND_PRODUCT_REVENUE,
            value=1_100_000.0,
            unit="件",
            currency="CNY",
            period="2025-12-31",
            source_ref=f"{cls.SYNTHETIC_PREFIX}:assumption:{symbol}:2025-12-31",
            assumption_text="2025 年销量同比增长 10%（合成假设）",
            basis="synthetic_smoke_test",
        )
        segment = Segment(
            segment_name="主营业务",
            products=["主力产品"],
            drivers=[driver, asp_driver, gross_margin_driver, share_count_driver],
        )
        return OperatingModel(
            symbol=symbol,
            period=period,
            business_model=BUSINESS_MODEL_MANUFACTURING,
            segments=[segment],
            drivers=[driver, asp_driver, gross_margin_driver, share_count_driver],
            assumptions=[forward_assumption],
            currency="CNY",
            unit="元",
            source_trace_id=source_ref,
            schema_version="operating_model_v1",
        )

    @classmethod
    def build_synthetic_valuation_cases(
        cls,
        symbol: str,
        as_of: str,
        period: str = "2024-12-31",
        industry: str = "",
    ) -> list[ValuationCase]:
        """构造合成估值案例（至少两种适用估值方法）。

        按行业过滤规则：
        - 金融业（银行/保险/证券）仅生成 HISTORICAL_PE / HISTORICAL_PB
        - 非金融业生成 HISTORICAL_PE / HISTORICAL_PB / EV_EBITDA

        Args:
            symbol: 公司代码
            as_of: PIT 时点
            period: 报告期
            industry: 行业标签（用于金融业过滤）

        Returns:
            合成的 ValuationCase 列表（带 synthetic_ 前缀 source_ref）
        """
        source_ref = f"{cls.SYNTHETIC_PREFIX}:valuation:{symbol}:{period}"
        # 关键假设（CAPM 风险溢价 + Beta）
        wacc_assumption = ValuationAssumption(
            name="wacc",
            value=0.10,
            unit="ratio",
            source_ref=source_ref,
            source_kind="synthetic",
            as_of=as_of,
            note="合成 WACC 假设，仅供冒烟测试",
        )

        # 历史 PE 案例（适用所有行业）
        pe_case = ValuationCase(
            company_symbol=symbol,
            method=ValuationMethod.HISTORICAL_PE.value,
            as_of=as_of,
            scenario="base",
            output_interval=Interval(low=20.0, high=30.0),
            output_per_share=Interval(low=20.0, high=30.0),
            computability=Computability.COMPUTABLE.value,
            source_refs=[source_ref],
            assumptions=[wacc_assumption],
            limitations=[],
            method_specific_metrics={"percentile": 0.5},
            model_version="v1",
        )

        # 历史 PB 案例（适用所有行业）
        pb_case = ValuationCase(
            company_symbol=symbol,
            method=ValuationMethod.HISTORICAL_PB.value,
            as_of=as_of,
            scenario="base",
            output_interval=Interval(low=2.0, high=4.0),
            output_per_share=Interval(low=2.0, high=4.0),
            computability=Computability.COMPUTABLE.value,
            source_refs=[source_ref],
            assumptions=[wacc_assumption],
            limitations=[],
            method_specific_metrics={"percentile": 0.5},
            model_version="v1",
        )

        cases: list[ValuationCase] = [pe_case, pb_case]

        # 非金融业追加 EV/EBITDA（金融业不适用）
        financial_keywords = ("银行", "保险", "证券", "bank", "insurance", "securities")
        is_financial = any(kw in (industry or "").lower() for kw in financial_keywords)
        if not is_financial:
            ev_ebitda_case = ValuationCase(
                company_symbol=symbol,
                method=ValuationMethod.EV_EBITDA.value,
                as_of=as_of,
                scenario="base",
                output_interval=Interval(low=8.0, high=14.0),
                output_per_share=Interval(low=8.0, high=14.0),
                computability=Computability.COMPUTABLE.value,
                source_refs=[source_ref],
                assumptions=[wacc_assumption],
                limitations=[],
                method_specific_metrics={"ttm_ebitda": 5_000_000_000.0},
                model_version="v1",
            )
            cases.append(ev_ebitda_case)

        return cases

    @classmethod
    def build_synthetic_valuation_report(
        cls,
        symbol: str,
        as_of: str,
        period: str = "2024-12-31",
        industry: str = "",
    ) -> ValuationReport:
        """构造合成估值报告。

        Args:
            symbol: 公司代码
            as_of: PIT 时点
            period: 报告期
            industry: 行业标签

        Returns:
            合成的 ValuationReport
        """
        cases = cls.build_synthetic_valuation_cases(symbol, as_of, period, industry)
        computable_cases = [c for c in cases if c.is_computable()]
        # overall_range = [min(low), max(high)]
        lows = [c.output_interval.low for c in computable_cases]
        highs = [c.output_interval.high for c in computable_cases]
        overall_low = min(lows) if lows else 0.0
        overall_high = max(highs) if highs else 0.0
        return ValuationReport(
            company_symbol=symbol,
            as_of=as_of,
            scenario="base",
            cases=cases,
            method_differences=[
                {
                    "method_a": ValuationMethod.HISTORICAL_PE.value,
                    "method_b": ValuationMethod.HISTORICAL_PB.value,
                    "difference_note": "PE 与 PB 反映不同盈利与账面价值维度，不简单平均",
                }
            ],
            overall_range=Interval(low=overall_low, high=overall_high),
            overall_range_method="min_max",
            limitations=[],
            notice="研究数据产物，不包含交易动作建议。",
            model_version="v1",
        )

    @classmethod
    def populate_repositories(
        cls,
        repos: dict[str, Any],
        symbols: list[str],
        as_of: str,
        period: str = "2024-12-31",
        company_meta: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, int]:
        """用合成数据填充领域仓储（仅供冒烟测试）。

        Args:
            repos: 领域仓储字典，键为 fundamentals_repo / operating_repo /
                valuation_repo 等
            symbols: 公司代码列表
            as_of: PIT 时点
            period: 报告期
            company_meta: 公司元数据字典，键为 symbol，值为 {"industry": ...,
                "name": ...}；用于行业过滤

        Returns:
            填充统计字典，键为仓库名，值为填充数量
        """
        stats: dict[str, int] = {
            "statements": 0,
            "operating_models": 0,
            "valuation_cases": 0,
            "valuation_reports": 0,
        }
        fundamentals_repo = repos.get("fundamentals_repo")
        operating_repo = repos.get("operating_repo")
        valuation_repo = repos.get("valuation_repo")
        meta = company_meta or {}

        for symbol in symbols:
            industry = meta.get(symbol, {}).get("industry", "")
            if fundamentals_repo is not None:
                stmt = cls.build_synthetic_statement(symbol, as_of, period)
                try:
                    fundamentals_repo.save(stmt)
                    stats["statements"] += 1
                except ValueError:
                    # 已存在则跳过（幂等）
                    pass
            if operating_repo is not None:
                model = cls.build_synthetic_operating_model(symbol, as_of, period)
                try:
                    operating_repo.save(model, version=1)
                    stats["operating_models"] += 1
                except ValueError:
                    pass
            if valuation_repo is not None:
                cases = cls.build_synthetic_valuation_cases(
                    symbol, as_of, period, industry=industry,
                )
                for case in cases:
                    try:
                        valuation_repo.save_case(case)
                        stats["valuation_cases"] += 1
                    except Exception:
                        pass
                report = cls.build_synthetic_valuation_report(
                    symbol, as_of, period, industry=industry,
                )
                try:
                    valuation_repo.save_report(report)
                    stats["valuation_reports"] += 1
                except Exception:
                    pass

        return stats


# ---------------------------------------------------------------------------
# 公共导出
# ---------------------------------------------------------------------------

__all__ = [
    "P1DataLoader",
    "SyntheticDataFactory",
    "LoadResult",
    "COMPANY_SAMPLE",
    "INDUSTRY_DISTRIBUTION",
]
