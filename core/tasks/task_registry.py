"""任务类型注册表

定义所有可用的任务类型、参数校验规则和任务分类（重型/轻型）。
用于在提交任务前进行参数校验，以及防止并发重型任务。
"""

from __future__ import annotations

from typing import Any

from config.logger import get_logger

logger = get_logger(__name__)


# 重型任务类型集合：批量操作，不允许并发执行
HEAVY_TASK_TYPES: set[str] = {
    "run_perilla_batch_fast",
}

# 所有合法的任务类型及其参数 Schema
_TASK_SCHEMAS: dict[str, dict[str, Any]] = {
    "run_perilla_topic_fast": {
        "description": "Perilla 主题快速扫描",
        "params": {
            "topic": {"type": "string", "required": False, "description": "主题关键词"},
            "limit": {"type": "integer", "required": False, "description": "扫描数量上限"},
        },
    },
    "run_perilla_batch_fast": {
        "description": "Perilla 批量快速扫描（重型任务）",
        "params": {
            "symbols": {"type": "array", "required": False, "description": "股票代码列表"},
            "batch_size": {"type": "integer", "required": False, "description": "每批数量"},
        },
    },
    "run_daily_brief": {
        "description": "每日研究简报生成",
        "params": {
            "date": {"type": "string", "required": False, "description": "日期 YYYY-MM-DD"},
            "sections": {"type": "array", "required": False, "description": "简报板块列表"},
        },
    },
    "refresh_watchlist": {
        "description": "刷新关注列表数据",
        "params": {
            "watchlist_id": {"type": "string", "required": False, "description": "关注列表 ID"},
        },
    },
    "generate_single_stock_evidence_pack": {
        "description": "生成单股证据包",
        "params": {
            "symbol": {"type": "string", "required": True, "description": "股票代码"},
            "depth": {"type": "string", "required": False, "description": "深度: quick / full"},
        },
    },
}


class TaskRegistry:
    """任务类型注册表

    提供任务类型的查询、参数校验和分类判断能力。
    所有方法均为类方法，无需实例化。
    """

    @classmethod
    def get_task_types(cls) -> list[str]:
        """获取所有可用的任务类型列表

        Returns:
            任务类型名称列表
        """
        return list(_TASK_SCHEMAS.keys())

    @classmethod
    def get_task_schema(cls, task_type: str) -> dict[str, Any]:
        """获取指定任务类型的参数 Schema

        Args:
            task_type: 任务类型名称

        Returns:
            参数 Schema 字典，不存在时返回空字典
        """
        schema = _TASK_SCHEMAS.get(task_type)
        if schema is None:
            return {}
        return schema

    @classmethod
    def is_heavy_task(cls, task_type: str) -> bool:
        """判断指定任务类型是否为重型任务

        重型任务不允许并发执行，同一时刻只能运行一个。

        Args:
            task_type: 任务类型名称

        Returns:
            True 表示重型任务
        """
        return task_type in HEAVY_TASK_TYPES

    @classmethod
    def validate_params(cls, task_type: str, params: dict[str, Any]) -> tuple[bool, str]:
        """校验任务参数是否符合 Schema 定义

        Args:
            task_type: 任务类型名称
            params: 待校验的参数字典

        Returns:
            (是否合法, 错误信息)，合法时错误信息为空字符串
        """
        if task_type not in _TASK_SCHEMAS:
            return False, f"未知的任务类型: {task_type}"

        schema = _TASK_SCHEMAS[task_type]
        param_defs = schema.get("params", {})

        # 检查必填参数
        for param_name, param_def in param_defs.items():
            if param_def.get("required", False) and param_name not in params:
                return False, f"缺少必填参数: {param_name}"

        # 检查参数类型（宽松校验，仅对已提供的参数做基本类型检查）
        _TYPE_MAP = {"string": str, "integer": int, "array": list, "object": dict}
        for param_name, value in params.items():
            if param_name not in param_defs:
                # 允许额外参数透传，仅记录日志
                logger.debug("任务 %s 收到未定义参数: %s", task_type, param_name)
                continue
            expected_type = _TYPE_MAP.get(param_defs[param_name].get("type", ""))
            if expected_type and not isinstance(value, expected_type):
                return False, (
                    f"参数 {param_name} 类型错误: 期望 {expected_type.__name__}, "
                    f"实际 {type(value).__name__}"
                )

        return True, ""
