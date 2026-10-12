"""
优化相关业务逻辑
包含 GA 优化任务管理
"""
import uuid
import threading
from typing import Dict, Any
from config.logger import get_logger

logger = get_logger(__name__)

# GA 任务管理
ga_tasks: Dict[str, Dict[str, Any]] = {}  # 内存任务表，用于存储异步GA优化任务


def ga_worker(task_id: str, args: dict):
    """
    GA优化工作线程
    在后台执行GA优化并更新任务状态
    
    Args:
        task_id: 任务ID
        args: GA优化参数
    """
    ga_tasks[task_id]["status"] = "running"
    try:
        # 导入run_ga_optimization函数
        from tools.ga_optimize_strategy import run_ga_optimization
        
        # 执行GA优化
        result = run_ga_optimization(**args)
        
        # 更新任务状态和结果
        ga_tasks[task_id]["status"] = "finished"
        ga_tasks[task_id]["result"] = result
        logger.info(f"GA优化任务完成 (task_id: {task_id})")
        
    except Exception as e:
        # 捕获错误并更新任务状态
        ga_tasks[task_id]["status"] = "error"
        ga_tasks[task_id]["error"] = str(e)
        logger.error(f"GA优化任务失败 (task_id: {task_id}): {e}", exc_info=True)
        import traceback
        traceback.print_exc()

