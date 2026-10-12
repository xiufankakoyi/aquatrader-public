"""
沙盒回测执行器

通过 multiprocessing.Process 在独立进程内执行回测，
实现崩溃隔离（用户代码崩溃不影响主服务进程）和超时强杀。

工作流程：
1. 主进程通过 Queue 接收子进程回测结果
2. 子进程执行 execute_backtest，结果通过 Queue 回传
3. 主进程等待 timeout 秒，超时则 terminate 子进程
4. 子进程崩溃（如 segfault）通过 Process.exitcode 检测

安全设计：
- 子进程继承主进程的 Python 环境（无 import os 等危险模块的需求）
- 用户代码必须先通过 AST 审计才能进入沙盒
- 子进程无网络/文件系统额外限制（依赖 AST 审计第一道防线）
"""

import multiprocessing as mp
import os
import platform
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from config.logger import get_logger

logger = get_logger(__name__)

# 默认超时 120 秒（聚宽式策略回测一般 < 30 秒）
DEFAULT_TIMEOUT_SECONDS = 120

# Windows 多进程需要 protectEntryPoint（if __name__ == "__main__"）
# 这里通过 spawn 方式启动子进程，确保子进程不继承主进程的 socket/线程状态
mp_context = mp.get_context("spawn") if platform.system() == "Windows" else mp.get_context("fork")


@dataclass
class SandboxResult:
    """沙盒执行结果"""

    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    elapsed_ms: int = 0
    timed_out: bool = False
    crashed: bool = False
    exitcode: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于 JSON 序列化）"""
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "timed_out": self.timed_out,
            "crashed": self.crashed,
            "exitcode": self.exitcode,
        }


def _backtest_worker(
    strategy_id: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
    params: Dict[str, Any],
    result_queue: mp.Queue,
    progress_queue: Optional[mp.Queue] = None,
) -> None:
    """
    沙盒子进程入口函数

    在独立进程中执行回测，结果通过 Queue 回传主进程。
    进程退出即释放所有资源（含内存泄漏）。
    """
    try:
        # 延迟导入避免主进程启动时加载回测引擎
        from server.services.backtest_runner_service import execute_backtest

        result = execute_backtest(
            strategy_id=strategy_id,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            params=params or {},
        )
        result_queue.put(result)
    except Exception as e:
        import traceback
        result_queue.put({
            "success": False,
            "error": {
                "code": "SANDBOX_WORKER_EXCEPTION",
                "message": f"沙盒子进程异常: {type(e).__name__}: {e}",
                "detail": traceback.format_exc(),
            },
        })


def run_backtest_in_sandbox(
    strategy_id: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 1_000_000.0,
    params: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> SandboxResult:
    """
    在独立进程中执行回测，提供崩溃隔离和超时强杀。

    Args:
        strategy_id: 策略 ID（如 user_simple_buy_sell_strategy）
        start_date: 回测开始日期 YYYY-MM-DD
        end_date: 回测结束日期 YYYY-MM-DD
        initial_capital: 初始资金
        params: 策略参数
        timeout_seconds: 超时秒数，超时后强杀子进程
        on_progress: 进度回调（暂未使用，预留接口）

    Returns:
        SandboxResult: 包含 success/data/error/elapsed_ms/timed_out/crashed
    """
    started_at = time.time()

    # 创建进程间通信队列
    result_queue: mp.Queue = mp_context.Queue()
    progress_queue: mp.Queue = mp_context.Queue() if on_progress else None

    # 启动子进程
    proc = mp_context.Process(
        target=_backtest_worker,
        args=(
            strategy_id,
            start_date,
            end_date,
            initial_capital,
            params or {},
            result_queue,
            progress_queue,
        ),
        name=f"sandbox-backtest-{strategy_id}",
        daemon=False,  # 非守护进程，便于主进程终止
    )

    logger.info(
        "[SandboxRunner] 启动沙盒进程: strategy=%s range=%s~%s timeout=%ds",
        strategy_id, start_date, end_date, timeout_seconds,
    )

    proc.start()

    # 主进程等待结果，带超时
    result_data: Optional[Dict[str, Any]] = None
    timed_out = False

    try:
        # 轮询队列（Queue.get 不能中途取消，用短超时循环）
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            # 检查子进程是否还活着（崩溃检测）
            if not proc.is_alive() and result_queue.empty():
                # 子进程已退出但没产出结果 → 崩溃
                break

            # 推送进度（如果子进程有发）
            if progress_queue:
                try:
                    while True:
                        prog = progress_queue.get_nowait()
                        if on_progress:
                            on_progress(prog.get("progress", 0), prog.get("message", ""))
                except queue.Empty:
                    pass

            # 检查结果
            try:
                result_data = result_queue.get(timeout=0.5)
                break
            except queue.Empty:
                continue
        else:
            timed_out = True

    except Exception as e:
        logger.error("[SandboxRunner] 主进程等待异常: %s", e)
        timed_out = True

    # 处理超时
    if timed_out:
        logger.warning(
            "[SandboxRunner] 沙盒超时 (%ds)，终止子进程 pid=%s",
            timeout_seconds, proc.pid,
        )
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            # terminate 失败，强杀
            try:
                proc.kill()
            except Exception:
                pass
            proc.join(timeout=2)

        elapsed_ms = int((time.time() - started_at) * 1000)
        return SandboxResult(
            success=False,
            error={
                "code": "SANDBOX_TIMEOUT",
                "message": f"回测超时（{timeout_seconds}秒），已终止",
                "retryable": False,
            },
            elapsed_ms=elapsed_ms,
            timed_out=True,
        )

    # 等待子进程退出
    proc.join(timeout=5)
    elapsed_ms = int((time.time() - started_at) * 1000)
    exitcode = proc.exitcode

    # 崩溃检测（exitcode != 0 表示异常退出）
    crashed = exitcode is not None and exitcode != 0
    if crashed:
        logger.error(
            "[SandboxRunner] 沙盒子进程崩溃 exitcode=%s pid=%s",
            exitcode, proc.pid,
        )
        return SandboxResult(
            success=False,
            error={
                "code": "SANDBOX_CRASHED",
                "message": f"回测进程崩溃 (exitcode={exitcode})",
                "detail": "可能是策略代码导致 segfault、内存越界或栈溢出",
                "retryable": False,
            },
            elapsed_ms=elapsed_ms,
            crashed=True,
            exitcode=exitcode,
        )

    # 解析结果
    if result_data is None:
        return SandboxResult(
            success=False,
            error={
                "code": "SANDBOX_NO_RESULT",
                "message": "沙盒未返回任何结果",
                "retryable": True,
            },
            elapsed_ms=elapsed_ms,
        )

    success = bool(result_data.get("success", False))
    return SandboxResult(
        success=success,
        data=result_data.get("data") if success else None,
        error=result_data.get("error") if not success else None,
        elapsed_ms=elapsed_ms,
        exitcode=exitcode,
    )
