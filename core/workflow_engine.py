# core/workflow_engine.py
"""
自动化工作流引擎
负责任务编排、流水线管理以及状态跟踪。
"""

import yaml
import time
import traceback
from typing import Dict, List, Any, Optional, Callable
from pathlib import Path
from datetime import datetime

from config.logger import get_logger
from core.research.research_note import ResearchNote # 修复导入路径

logger = get_logger(__name__)

class WorkflowTask:
    """任务基类"""
    def __init__(self, name: str, func: Callable, **kwargs):
        self.name = name
        self.func = func
        self.kwargs = kwargs
        self.status = "pending" # pending, running, success, failed
        self.result = None
        self.error = None
        self.start_time = None
        self.end_time = None

    def execute(self) -> Any:
        self.status = "running"
        self.start_time = time.time()
        logger.info(f"开始执行任务: {self.name}")
        try:
            self.result = self.func(**self.kwargs)
            self.status = "success"
            logger.info(f"任务执行成功: {self.name}")
        except Exception as e:
            self.status = "failed"
            self.error = traceback.format_exc()
            logger.error(f"任务执行失败: {self.name}\n{self.error}")
        finally:
            self.end_time = time.time()
        return self.result

class ResearchPipeline:
    """
    研究流水线管理类
    """
    def __init__(self, name: str):
        self.name = name
        self.tasks: List[WorkflowTask] = []
        self.context: Dict[str, Any] = {}
        self.start_time = None
        self.end_time = None

    def add_task(self, task_name: str, func: Callable, **kwargs):
        """手动添加任务"""
        task = WorkflowTask(task_name, func, **kwargs)
        self.tasks.append(task)
        return self

    def load_from_yaml(self, yaml_path: str, task_registry: Dict[str, Callable]):
        """从 YAML 配置文件加载流水线"""
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        self.name = config.get('name', self.name)
        tasks_config = config.get('tasks', [])

        for t_cfg in tasks_config:
            t_name = t_cfg['name']
            t_type = t_cfg['type']
            t_params = t_cfg.get('params', {})

            if t_type in task_registry:
                self.add_task(t_name, task_registry[t_type], **t_params)
            else:
                logger.error(f"任务类型 {t_type} 未在注册表中找到")

        return self

    def run(self) -> Dict[str, Any]:
        """执行整个流水线"""
        self.start_time = time.time()
        logger.info(f">>> 开始运行流水线: {self.name}")

        for task in self.tasks:
            # 可以在这里注入上下文变量到任务参数中
            # 例如支持 params: { start_date: "{{prev_task_output}}" }
            task.execute()
            if task.status == "failed":
                logger.error(f"流水线由于任务 {task.name} 失败而中断")
                break

        self.end_time = time.time()
        duration = self.end_time - self.start_time
        logger.info(f"<<< 流水线运行结束: {self.name} (耗时: {duration:.2f}s)")

        return self.get_summary()

    def get_summary(self) -> Dict[str, Any]:
        """平衡流水线执行摘要"""
        return {
            "pipeline_name": self.name,
            "status": "success" if all(t.status == "success" for t in self.tasks) else "failed",
            "duration": self.end_time - self.start_time if self.end_time else 0,
            "tasks": [
                {
                    "name": t.name,
                    "status": t.status,
                    "duration": (t.end_time - t.start_time) if t.end_time else 0
                } for t in self.tasks
            ]
        }

# --- 预定义任务示例 ---
def data_update_task(target_date: Optional[str] = None):
    logger.info(f"执行数据拉取任务, 目标日期: {target_date}")
    time.sleep(1) # 模拟
    return {"status": "updated", "date": target_date}

def backtest_task(strategy_id: str, params: Dict):
    logger.info(f"执行策略回测: {strategy_id}")
    time.sleep(2) # 模拟
    return {"sharpe": 2.5}

def report_task(results: Dict):
    logger.info(f"生成研究报告...")
    return "report_path_xyz.pdf"

if __name__ == "__main__":
    # 示例用法
    registry = {
        'data_update': data_update_task,
        'backtest': backtest_task,
        'report': report_task
    }

    pipeline = ResearchPipeline("每日量化研究流程")
    pipeline.add_task("数据下载", data_update_task, target_date="2024-01-10")
    pipeline.add_task("回测主策略", backtest_task, strategy_id="ma_cross", params={"fast": 5, "slow": 20})

    summary = pipeline.run()
    print(yaml.dump(summary, allow_unicode=True))
