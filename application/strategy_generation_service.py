"""
策略生成应用服务

作为应用层门面，将自然语言策略描述转换为可热重载的策略文件：
- 封装 Prompt 工程、LLM 调用、后处理、落盘等全部细节
- 对外只暴露 create_strategy 等高阶接口
- server.services.strategy_generator 只做薄转发，不再直接依赖 core.quant 模板
"""

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from core.quant.strategies.templates.prompt_template import build_prompt
from core.utils.llm_client import AquaLLM


# 内置 fallback 模板 — 当 LLM 不可用时保证生成流程不断
_FALLBACK_STRATEGY_CODE = '''"""
本地模板策略 (LLM 不可用时自动启用)

策略逻辑: 5 日均线上穿 20 日均线买入, 下穿卖出, 默认持仓 5 天。
该模板仅作为占位策略, 不构成任何投资建议。
"""
from typing import Dict, List, Any
import pandas as pd

from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig


class {class_name}(AIStrategyBase):
    """本地模板策略 — LLM 不可用时的安全降级方案"""

    strategy_name = "{strategy_name}"

    def __init__(self, config: Optional[AIStrategyConfig] = None, name: str | None = None):
        if config is None:
            config = AIStrategyConfig(params={{
                "fast_period": 5,
                "slow_period": 20,
                "max_holding_days": 5,
            }})
        super().__init__(config, name)
        self.holding_state: Dict[str, Dict[str, Any]] = {{}}

    def get_required_indicators(self) -> List[Dict[str, Any]]:
        return [
            {{"name": "MA", "period": self.config.params.get("fast_period", 5)}},
            {{"name": "MA", "period": self.config.params.get("slow_period", 20)}},
        ]

    def _generate_signals_impl(self, current_date, stock_pool: pd.DataFrame) -> Dict[str, str]:
        signals: Dict[str, str] = {{}}
        fast_col = "MA_{{}}".format(self.config.params.get("fast_period", 5))
        slow_col = "MA_{{}}".format(self.config.params.get("slow_period", 20))
        max_holding = int(self.config.params.get("max_holding_days", 5))

        for _, row in stock_pool.iterrows():
            code = row.get("stock_code") or row.get("symbol_code")
            if not code:
                continue
            fast = row.get(fast_col)
            slow = row.get(slow_col)
            if fast is None or slow is None:
                signals[code] = "hold"
                continue

            state = self.holding_state.get(code, {{"holding_days": 0}})
            if fast > slow and state["holding_days"] == 0:
                signals[code] = "buy"
                self.holding_state[code] = {{"holding_days": 1}}
            elif fast < slow or state["holding_days"] >= max_holding:
                signals[code] = "sell"
                self.holding_state[code] = {{"holding_days": 0}}
            else:
                state["holding_days"] += 1
                self.holding_state[code] = state
                signals[code] = "hold"
        return signals
'''


class StrategyGenerator:
    """
    策略生成器（应用层门面）

    使用示例:
        generator = StrategyGenerator()
        result = generator.create_strategy(
            user_description="写一个策略：股价突破20日均线买入，RSI大于70卖出",
            strategy_name="AI突破策略",
            use_fallback=True,
        )
        # result -> {"filename", "filepath", "strategy_id", "code", "fallback", "message"}
    """

    def __init__(self):
        """初始化策略生成器"""
        self.llm = AquaLLM()
        logger.info("策略生成器已初始化")

    def create_strategy(
        self,
        user_description: str,
        strategy_name: str = "AI策略",
        use_fallback: bool = False,
    ) -> Dict[str, Any]:
        """
        生成策略文件并保存

        参数:
            user_description: 用户的自然语言描述
            strategy_name: 策略名称
            use_fallback: LLM 失败时是否自动使用本地模板策略

        返回:
            dict: {
                filename: 生成的 .py 文件名,
                filepath: 完整磁盘路径,
                strategy_id: 策略ID,
                code: 写入文件的代码内容,
                fallback: 是否使用本地模板,
                message: 人类可读的状态描述,
            }

        异常:
            Exception: 当 LLM 失败且 use_fallback=False 时抛出
        """
        timestamp = int(time.time())
        strategy_id = f"ai_gen_{timestamp}"
        class_name = f"Strategy_ai_gen_{timestamp}"

        logger.info(
            f"开始生成策略: {strategy_name} (ID: {strategy_id}, Class: {class_name}, "
            f"use_fallback={use_fallback})"
        )

        # 1. 调用 LLM (允许失败, 由 use_fallback 控制后续行为)
        code: Optional[str] = None
        used_fallback = False
        try:
            enhanced_description = self._build_enhanced_description(
                user_description, strategy_id, strategy_name, class_name
            )
            full_prompt = build_prompt(enhanced_description)
            logger.info("正在调用 LLM 生成策略代码...")
            code = self.llm.generate_code(
                user_prompt=full_prompt,
                system_prompt="你是一个专业的量化策略代码生成器。严格按照用户提供的规范生成代码。",
            )
            if not code:
                raise RuntimeError("LLM 返回为空")
        except Exception as llm_err:
            logger.warning(f"LLM 调用失败: {llm_err}")
            if not use_fallback:
                raise Exception(f"策略生成失败: LLM 调用失败 - {llm_err}") from llm_err
            used_fallback = True
            code = _FALLBACK_STRATEGY_CODE.format(
                class_name=class_name, strategy_name=strategy_name
            )
            logger.info("已切换为本地模板策略 (LLM 不可用)")

        # 2. 后处理：保证类名 / strategy_name 正确
        if not used_fallback:
            code = self._post_process_code(code, strategy_id, strategy_name, class_name)

        # 3. 落盘到 core/quant/strategies/user/
        filename = f"ai_gen_{timestamp}.py"
        save_dir = self._user_strategies_dir()
        save_dir.mkdir(parents=True, exist_ok=True)
        file_path = save_dir / filename

        try:
            file_path.write_text(code, encoding="utf-8")
            logger.info(f"策略已保存至: {file_path}")
        except Exception as write_err:
            logger.error(f"保存策略文件失败: {write_err}")
            raise Exception(f"保存策略文件失败: {write_err}") from write_err

        message = (
            "已切换为本地模板策略 (LLM 不可用)" if used_fallback
            else f"策略已成功生成并保存为 {filename}"
        )
        return {
            "filename": filename,
            "filepath": str(file_path),
            "strategy_id": strategy_id,
            "code": code,
            "fallback": used_fallback,
            "message": message,
        }

    @staticmethod
    def _user_strategies_dir() -> Path:
        """返回用户策略保存目录（core/quant/strategies/user）"""
        return Path(__file__).parent.parent / "core" / "quant" / "strategies" / "user"

    @staticmethod
    def _build_enhanced_description(
        user_description: str,
        strategy_id: str,
        strategy_name: str,
        class_name: str,
    ) -> str:
        """构建增强版用户描述，注入 ID、名称、类名等元信息"""
        return f"""
策略ID: {strategy_id}
策略名称: {strategy_name}
类名: {class_name}

用户需求:
{user_description}

请生成完整的策略代码，类名必须使用 {class_name}（避免类名冲突），strategy_name 设置为 "{strategy_name}"。
"""

    @staticmethod
    def _post_process_code(
        code: str,
        strategy_id: str,
        strategy_name: str,
        class_name: str,
    ) -> str:
        """
        后处理生成的代码，确保包含正确的类名和策略名称

        参数:
            code: 生成的代码
            strategy_id: 策略ID
            strategy_name: 策略名称
            class_name: 策略类名（避免冲突）

        返回:
            str: 处理后的代码
        """
        _ = strategy_id  # 保留参数，便于后续扩展

        code = re.sub(
            r'class\s+AIGeneratedStrategy\s*\(',
            f'class {class_name}(',
            code,
        )
        code = re.sub(
            r'class\s+\w+Strategy\s*\([^)]*AIStrategyBase',
            f'class {class_name}(AIStrategyBase',
            code,
        )

        if 'strategy_name' in code:
            code = re.sub(
                r'strategy_name\s*=\s*["\'][^"\']*["\']',
                f'strategy_name = "{strategy_name}"',
                code,
            )
        else:
            lines = code.split('\n')
            for i, line in enumerate(lines):
                if line.strip().startswith('class ') and 'AIStrategyBase' in line:
                    indent = len(line) - len(line.lstrip())
                    lines.insert(i + 1, ' ' * (indent + 4) + f'strategy_name = "{strategy_name}"')
                    break
            code = '\n'.join(lines)

        return code
