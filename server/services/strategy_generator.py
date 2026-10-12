"""
策略生成服务（server 层薄封装）

具体的 Prompt 工程、LLM 调用、代码后处理与落盘逻辑已下沉到应用层门面：
    application.strategy_generation_service.StrategyGenerator

本文件仅做转发，避免 server 服务直接侵入 core.quant 模板细节。
"""

from application.strategy_generation_service import StrategyGenerator


__all__ = ["StrategyGenerator"]
