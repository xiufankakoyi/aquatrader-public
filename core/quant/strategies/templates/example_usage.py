"""
AI 策略生成器使用示例

演示如何使用 AIStrategyGenerator 将自然语言描述转换为策略代码。
"""

from core.quant.strategies.templates import (
    AIStrategyGenerator,
    generate_strategy_code,
    build_prompt
)


def example_1_openai():
    """示例1: 使用 OpenAI API"""
    generator = AIStrategyGenerator()

    # 设置 OpenAI API
    generator.set_openai_api(
        api_key="sk-your-api-key-here",
        model="gpt-4"
    )

    # 生成代码
    user_description = "写一个策略：股价突破20日均线买入，RSI大于70卖出，最多持仓5天。"
    code = generator.generate(user_description)

    print("生成的代码：")
    print(code)

    # 保存到文件
    generator.generate_and_save(
        user_description,
        output_path="generated_strategy.py"
    )


def example_2_custom_llm():
    """示例2: 使用自定义 LLM 函数"""
    generator = AIStrategyGenerator()

    def my_llm_function(prompt: str) -> str:
        """
        自定义 LLM 调用函数

        这里可以调用：
        - 本地模型（transformers）
        - 其他 API（如 Claude、文心一言等）
        - 测试用的模拟函数
        """
        # 示例：模拟返回
        return '''
from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig

class MyStrategy(AIStrategyBase):
    strategy_name = "MyStrategy"

    def __init__(self, config: AIStrategyConfig = None):
        if config is None:
            config = AIStrategyConfig(params={
                "ma_period": 20,
                "rsi_sell_threshold": 70,
                "max_holding_days": 5,
            })
        super().__init__(config)

    def get_required_indicators(self):
        return [
            {"name": "MA", "period": self.config.params.get("ma_period", 20), "column": "close"},
            {"name": "RSI", "period": 14, "column": "close"},
        ]

    def _generate_signals_impl(self, current_date: str, stock_pool):
        signals = {}
        ma_period = self.config.params.get("ma_period", 20)
        rsi_sell_threshold = self.config.params.get("rsi_sell_threshold", 70)
        max_holding_days = self.config.params.get("max_holding_days", 5)

        for _, row in stock_pool.iterrows():
            code = row["stock_code"]

            # 卖出逻辑
            if code in self.current_portfolio:
                holding_days = self.holding_state.get(code, {}).get("holding_days", 0)
                if holding_days >= max_holding_days:
                    signals[code] = "sell"
                    continue

                if row["RSI"] > rsi_sell_threshold:
                    signals[code] = "sell"
                    continue

            # 买入逻辑
            ma_column = f"MA_{ma_period}"
            if ma_column in row.index and row["close"] > row[ma_column]:
                signals[code] = "buy"
            else:
                signals[code] = "hold"

        return signals
        '''

    generator.set_custom_llm(my_llm_function)

    code = generator.generate(
        "股价突破20日均线买入，RSI大于70卖出，最多持仓5天。"
    )

    print("生成的代码：")
    print(code)


def example_3_convenience_function():
    """示例3: 使用便捷函数"""
    # 方式1: 使用 OpenAI
    code = generate_strategy_code(
        "写一个策略：RSI小于30买入，大于70卖出",
        openai_api_key="sk-your-api-key-here",
        openai_model="gpt-4"
    )
    print(code)

    # 方式2: 使用自定义 LLM
    def my_llm(prompt):
        # 调用你的 LLM
        return "生成的代码..."

    code = generate_strategy_code(
        "股价突破20日均线买入",
        llm_function=my_llm
    )
    print(code)


def example_4_view_prompt():
    """示例4: 查看生成的 Prompt"""
    user_description = "写一个策略：股价突破20日均线买入，RSI大于70卖出，最多持仓5天。"

    prompt = build_prompt(user_description)

    print("生成的 Prompt：")
    print("=" * 80)
    print(prompt)
    print("=" * 80)


if __name__ == "__main__":
    # 运行示例
    print("示例1: 使用 OpenAI API")
    print("-" * 80)
    # example_1_openai()  # 需要真实的 API Key

    print("\n示例2: 使用自定义 LLM")
    print("-" * 80)
    example_2_custom_llm()

    print("\n示例3: 使用便捷函数")
    print("-" * 80)
    # example_3_convenience_function()  # 需要真实的 API Key

    print("\n示例4: 查看生成的 Prompt")
    print("-" * 80)
    example_4_view_prompt()



