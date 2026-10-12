"""回测运行服务：为 Web 路由提供同步/异步回测执行能力。"""

import threading
import time
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional

from core.quant.backtest.models import BacktestConfig
from core.quant.backtest.unified_engine import UnifiedBacktestEngine
from core.quant.strategies.strategy_factory import StrategyFactory
from data_svc.unified_data_query import UnifiedDataQuery


class BacktestErrorCode(Enum):
    """回测错误码，统一以 ``BACKTEST_`` 前缀对外暴露。"""

    MISSING_PARAMETERS = "BACKTEST_MISSING_PARAMETERS"
    STRATEGY_NOT_REGISTERED = "BACKTEST_STRATEGY_NOT_REGISTERED"
    STRATEGY_LOAD_FAILED = "BACKTEST_STRATEGY_LOAD_FAILED"
    INVALID_DATE_RANGE = "BACKTEST_INVALID_DATE_RANGE"
    INSUFFICIENT_DATA = "BACKTEST_INSUFFICIENT_DATA"
    DATA_QUERY_UNAVAILABLE = "BACKTEST_DATA_QUERY_UNAVAILABLE"
    MISSING_DATA_FIELDS = "BACKTEST_MISSING_DATA_FIELDS"
    LANCEDB_NO_DATA = "BACKTEST_LANCEDB_NO_DATA"
    MATRIX_CACHE_MISSING = "BACKTEST_MATRIX_CACHE_MISSING"
    RUNTIME_EXCEPTION = "BACKTEST_RUNTIME_EXCEPTION"
    TIMEOUT = "BACKTEST_TIMEOUT"


ERROR_SUGGESTIONS: Dict[str, str] = {
    BacktestErrorCode.MISSING_PARAMETERS.value: "请检查请求参数是否包含 strategy_id、start_date、end_date",
    BacktestErrorCode.STRATEGY_NOT_REGISTERED.value: "请先在策略工厂中注册该策略，或检查 strategy_id 拼写",
    BacktestErrorCode.STRATEGY_LOAD_FAILED.value: "策略加载失败，请检查策略配置或依赖",
    BacktestErrorCode.INVALID_DATE_RANGE.value: "请确保 start_date <= end_date，且格式为 YYYY-MM-DD",
    BacktestErrorCode.INSUFFICIENT_DATA.value: "所选股票在回测区间数据不足，请更换标的或日期范围",
    BacktestErrorCode.DATA_QUERY_UNAVAILABLE.value: "数据查询服务不可用，请检查数据连接",
    BacktestErrorCode.MISSING_DATA_FIELDS.value: "缺少必要数据字段，请检查数据 schema",
    BacktestErrorCode.LANCEDB_NO_DATA.value: "LanceDB 中无对应数据，请先同步数据",
    BacktestErrorCode.MATRIX_CACHE_MISSING.value: "矩阵缓存缺失，请重新生成或检查配置",
    BacktestErrorCode.RUNTIME_EXCEPTION.value: "回测执行异常，请检查策略逻辑或联系开发",
    BacktestErrorCode.TIMEOUT.value: "回测超时，请缩小日期范围或减少标的数量后重试",
}


def _error_response(
    code: BacktestErrorCode,
    message: str,
    detail: str = "",
    endpoint: str = "/api/backtest/run",
) -> Dict[str, Any]:
    """构造标准错误响应。"""
    return {
        "success": False,
        "data": None,
        "error": {
            "code": code.value,
            "message": message,
            "detail": detail,
            "retryable": code == BacktestErrorCode.RUNTIME_EXCEPTION,
            "suggestion": ERROR_SUGGESTIONS.get(code.value, ""),
            "endpoint": endpoint,
        },
    }


class BacktestJob:
    """回测任务记录。"""

    def __init__(
        self,
        strategy_id: str,
        start_date: str,
        end_date: str,
        initial_capital: float = 1_000_000.0,
        params: Optional[Dict[str, Any]] = None,
    ):
        self.job_id = str(uuid.uuid4())
        self.strategy_id = strategy_id
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.params = params or {}
        self.status = "pending"
        self.progress = 0
        self.message = "任务已创建"
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[Dict[str, Any]] = None
        self.created_at = datetime.now()
        self.updated_at = self.created_at
        self.elapsed_ms = 0

    def update(
        self,
        status: str,
        progress: int = 0,
        message: str = "",
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        """更新任务状态。"""
        self.status = status
        self.progress = progress
        self.message = message
        if result is not None:
            self.result = result
        if error is not None:
            self.error = error
        self.updated_at = datetime.now()

    def to_public_dict(self) -> Dict[str, Any]:
        """转为接口返回字典。"""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "stats": self.result.get("stats") if self.result else None,
            "equity_curve": self.result.get("equity_curve") if self.result else None,
            "trades": self.result.get("trades") if self.result else None,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


class JobRegistry:
    """内存回测任务注册表，完成后保留 1 小时。"""

    def __init__(self, ttl_seconds: int = 3600):
        self._jobs: Dict[str, BacktestJob] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def create(
        self,
        strategy_id: str,
        start_date: str,
        end_date: str,
        initial_capital: float = 1_000_000.0,
        params: Optional[Dict[str, Any]] = None,
    ) -> BacktestJob:
        """创建新任务。"""
        job = BacktestJob(strategy_id, start_date, end_date, initial_capital, params)
        with self._lock:
            self._cleanup()
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[BacktestJob]:
        """获取任务。"""
        with self._lock:
            self._cleanup()
            return self._jobs.get(job_id)

    def _cleanup(self) -> None:
        """清理过期任务。"""
        now = datetime.now()
        expired = [
            jid for jid, job in self._jobs.items()
            if now - job.updated_at > timedelta(seconds=self._ttl)
        ]
        for jid in expired:
            self._jobs.pop(jid, None)


# 全局注册表实例
_job_registry = JobRegistry()


def get_job_registry() -> JobRegistry:
    """返回全局任务注册表。"""
    return _job_registry


def _trade_to_dict(trade: Any) -> Dict[str, Any]:
    """将交易记录转为字典。"""
    return {
        "action": getattr(trade, "action", ""),
        "symbol": getattr(trade, "symbol", ""),
        "date": getattr(trade, "date", ""),
        "price": getattr(trade, "price", 0.0),
        "quantity": getattr(trade, "quantity", 0),
        "amount": getattr(trade, "amount", 0.0),
        "holding_days": getattr(trade, "holding_days", 0),
    }


def execute_backtest(
    strategy_id: str,
    start_date: str,
    end_date: str,
    symbols: Optional[list[str]] = None,
    initial_capital: float = 1_000_000.0,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    执行一次同步回测。

    Args:
        strategy_id: 策略标识。
        start_date: 回测起始日期 YYYY-MM-DD。
        end_date: 回测结束日期 YYYY-MM-DD。
        symbols: 标的代码列表，None 时使用策略默认标的。
        initial_capital: 初始资金。
        params: 策略参数覆盖。

    Returns:
        标准响应字典，包含 success / data / error。
    """
    try:
        factory = StrategyFactory()
        strategy = factory.create_strategy(strategy_id, **(params or {}))
    except Exception as e:
        return _error_response(
            BacktestErrorCode.STRATEGY_NOT_REGISTERED,
            "策略未注册或创建失败",
            detail=str(e),
        )

    try:
        data_query = UnifiedDataQuery()
        config = BacktestConfig()
        config.initial_capital = initial_capital

        engine = UnifiedBacktestEngine(data_query=data_query, config=config)
        events = list(engine.run_backtest(start_date, end_date, strategy))
    except Exception as e:
        return _error_response(
            BacktestErrorCode.RUNTIME_EXCEPTION,
            "回测执行异常",
            detail=str(e),
        )

    final = next(
        (e for e in reversed(events) if e.get("type") == "stream_complete"),
        None,
    )
    if final is None:
        final = next(
            (e for e in reversed(events) if e.get("type") == "final_metrics"),
            None,
        )

    if final is None:
        return _error_response(
            BacktestErrorCode.RUNTIME_EXCEPTION,
            "回测未返回最终指标",
            detail="engine 未产出 final_metrics/stream_complete 事件",
        )

    data = final.get("data", {})
    stats = {
        "totalReturn": data.get("totalReturn", 0.0),
        "annualizedReturn": data.get("annualizedReturn", 0.0),
        "maxDrawdown": data.get("maxDrawdown", 0.0),
        "sharpeRatio": data.get("sharpeRatio", 0.0),
        "sortinoRatio": data.get("sortinoRatio", 0.0),
        "volatility": data.get("volatility", 0.0),
        "winRate": data.get("winRate", 0.0),
        "profitFactor": data.get("profitFactor", 0.0),
        "totalTrades": data.get("totalTrades", 0),
        "avgTradeReturn": data.get("avgTradeReturn", 0.0),
        "avgHoldingDays": data.get("avgHoldingDays", 0.0),
        "benchmarkReturn": data.get("benchmarkReturn", 0.0),
    }

    return {
        "success": True,
        "data": {
            "stats": stats,
            "equity_curve": data.get("equityCurve", []),
            "trades": data.get("trades", []),
        },
        "error": None,
    }


def run_backtest_in_thread(job: BacktestJob, registry: JobRegistry, on_progress=None) -> None:
    """
    在线程中执行回测并更新任务状态。

    Args:
        job: 回测任务对象
        registry: 任务注册表
        on_progress: 可选的进度回调函数 (progress: int, message: str) -> None
    """
    started_at = time.time()
    job.update(status="running", progress=10, message="开始执行回测")
    if on_progress:
        try:
            on_progress(10, "开始执行回测")
        except Exception:
            pass

    try:
        result = execute_backtest(
            strategy_id=job.strategy_id,
            start_date=job.start_date,
            end_date=job.end_date,
            initial_capital=job.initial_capital,
            params=job.params,
        )
        elapsed_ms = int((time.time() - started_at) * 1000)
        job.elapsed_ms = elapsed_ms

        if result.get("success"):
            job.update(
                status="success",
                progress=100,
                message="回测完成",
                result=result.get("data"),
            )
            if on_progress:
                try:
                    on_progress(100, "回测完成")
                except Exception:
                    pass
        else:
            job.update(
                status="failed",
                progress=0,
                message=result.get("error", {}).get("message", "回测失败"),
                error=result.get("error"),
            )
    except Exception as e:
        elapsed_ms = int((time.time() - started_at) * 1000)
        job.elapsed_ms = elapsed_ms
        job.update(
            status="failed",
            progress=0,
            message="回测执行异常",
            error=_error_response(
                BacktestErrorCode.RUNTIME_EXCEPTION,
                "回测执行异常",
                detail=str(e),
            ).get("error"),
        )
