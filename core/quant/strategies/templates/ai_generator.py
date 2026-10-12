"""
AI 策略代码生成器

将用户的自然语言描述转换为符合 AIStrategyBase 规范的 Python 代码。

支持多种 LLM 后端：
- OpenAI API
- 本地模型（通过 transformers）
- 其他兼容 OpenAI 格式的 API
"""

import re
from typing import Optional, Dict, Any, Callable
from core.quant.strategies.templates.prompt_template import build_prompt
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    record_llm_usage,
)


class AIStrategyGenerator:
    """
    AI 策略代码生成器

    使用示例：
        generator = AIStrategyGenerator()

        # 方式1: 使用 OpenAI API
        generator.set_openai_api(api_key="sk-...", model="gpt-4")
        code = generator.generate("股价突破20日均线买入，RSI大于70卖出")

        # 方式2: 使用自定义 LLM 函数
        def my_llm_call(prompt: str) -> str:
            # 调用你的 LLM API
            return response

        generator.set_custom_llm(my_llm_call)
        code = generator.generate("写一个策略：...")
    """

    def __init__(self):
        self.llm_function: Optional[Callable[[str], str]] = None
        self.openai_client = None
        self.openai_model = "gpt-4"

    def set_openai_api(self, api_key: str, model: str = "gpt-4", base_url: Optional[str] = None):
        """
        设置 OpenAI API

        参数：
            api_key: OpenAI API Key
            model: 模型名称（如 "gpt-4", "gpt-3.5-turbo"）
            base_url: 自定义 API 地址（可选，用于兼容其他服务）
        """
        try:
            import openai
            self.openai_client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url
            )
            self.openai_model = model

            def openai_call(prompt: str) -> str:
                response = self.openai_client.chat.completions.create(
                    model=self.openai_model,
                    messages=[
                        {"role": "system", "content": "你是一个专业的 Python 代码生成器。"},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,  # 低温度，确保代码准确性
                    max_tokens=2000
                )
                return response.choices[0].message.content

            def tracked_openai_call(prompt: str) -> str:
                messages = [
                    {"role": "system", "content": "Python code generator"},
                    {"role": "user", "content": prompt},
                ]
                prompt_tokens = estimate_messages_tokens(messages)
                check_llm_budget_or_raise(prompt_tokens, 2000, source="AIStrategyGenerator.openai_call")
                content = openai_call(prompt) or ""
                record_llm_usage(
                    source="AIStrategyGenerator.openai_call",
                    provider="custom",
                    model=self.openai_model,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=estimate_tokens(content),
                    success=True,
                )
                return content

            self.llm_function = tracked_openai_call
        except ImportError:
            raise ImportError("请安装 openai 库: pip install openai")

    def set_custom_llm(self, llm_function: Callable[[str], str]):
        """
        设置自定义 LLM 函数

        参数：
            llm_function: 函数，接收 prompt (str)，返回生成的代码 (str)
        """
        self.llm_function = llm_function

    def generate(
        self,
        user_description: str,
        validate: bool = True
    ) -> str:
        """
        生成策略代码

        参数：
            user_description: 用户的自然语言描述
            validate: 是否验证生成的代码

        返回：
            str: 生成的 Python 代码

        异常：
            ValueError: 如果未设置 LLM 函数
            SyntaxError: 如果生成的代码有语法错误（validate=True 时）
        """
        if self.llm_function is None:
            raise ValueError(
                "请先设置 LLM 函数。使用 set_openai_api() 或 set_custom_llm()"
            )

        # 构建 Prompt
        prompt = build_prompt(user_description)

        # 调用 LLM
        raw_code = self.llm_function(prompt)

        # 清理代码（移除 markdown 标记等）
        cleaned_code = self._clean_code(raw_code)

        # 验证代码
        if validate:
            self._validate_code(cleaned_code)

        return cleaned_code

    def _clean_code(self, raw_code: str) -> str:
        """
        清理生成的代码

        移除：
        - Markdown 代码块标记 (```python ... ```)
        - 解释性文字
        - 多余的空行
        """
        code = raw_code.strip()

        # 移除 markdown 代码块
        if code.startswith("```"):
            # 找到第一个 ``` 和最后一个 ```
            lines = code.split("\n")
            start_idx = 0
            end_idx = len(lines)

            for i, line in enumerate(lines):
                if line.strip().startswith("```"):
                    if start_idx == 0:
                        start_idx = i + 1
                    else:
                        end_idx = i
                        break

            code = "\n".join(lines[start_idx:end_idx])

        # 移除 Python 标记
        code = re.sub(r'^```python\s*', '', code, flags=re.MULTILINE)
        code = re.sub(r'^```\s*$', '', code, flags=re.MULTILINE)

        # 移除解释性文字（通常在代码块前后）
        lines = code.split("\n")
        cleaned_lines = []
        in_code = False

        for line in lines:
            # 如果遇到 import 或 class 或 def，说明进入代码区域
            if re.match(r'^\s*(import|from|class|def)', line):
                in_code = True

            if in_code:
                cleaned_lines.append(line)
            elif line.strip() and not line.strip().startswith("#"):
                # 非注释的非空行，可能是解释文字，跳过
                continue

        code = "\n".join(cleaned_lines)

        # 移除多余空行（保留最多2个连续空行）
        code = re.sub(r'\n{3,}', '\n\n', code)

        return code.strip()

    def _validate_code(self, code: str):
        """
        验证生成的代码

        检查：
        1. 语法是否正确
        2. 是否包含必需的类和方法
        3. 是否符合 AIStrategyBase 规范
        """
        # 1. 语法检查
        try:
            compile(code, '<string>', 'exec')
        except SyntaxError as e:
            raise SyntaxError(f"生成的代码有语法错误: {e}")

        # 2. 检查必需的导入
        if "from core.quant.strategies.templates.ai_base import" not in code:
            raise ValueError(
                "生成的代码缺少必需的导入: "
                "from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig"
            )

        # 3. 检查是否继承 AIStrategyBase
        if "AIStrategyBase" not in code:
            raise ValueError("生成的代码必须继承 AIStrategyBase")

        # 4. 检查必需的方法
        if "get_required_indicators" not in code:
            raise ValueError("生成的代码必须实现 get_required_indicators() 方法")

        if "_generate_signals_impl" not in code:
            raise ValueError("生成的代码必须实现 _generate_signals_impl() 方法")

        # 5. 检查是否使用了禁止的函数（指标计算）
        forbidden_patterns = [
            r'talib\.',
            r'\.rolling\(',
            r'\.mean\(',
            r'\.std\(',
            r'\.ewm\(',
        ]

        for pattern in forbidden_patterns:
            if re.search(pattern, code):
                raise ValueError(
                    f"生成的代码中禁止使用指标计算函数。"
                    f"检测到: {pattern}。"
                    f"请使用 get_required_indicators() 声明指标，"
                    f"然后在 _generate_signals_impl() 中使用 stock_pool 中已有的指标列。"
                )

    def generate_and_save(
        self,
        user_description: str,
        output_path: str,
        validate: bool = True
    ) -> str:
        """
        生成代码并保存到文件

        参数：
            user_description: 用户的自然语言描述
            output_path: 输出文件路径
            validate: 是否验证代码

        返回：
            str: 生成的代码
        """
        code = self.generate(user_description, validate=validate)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(code)

        return code


# 便捷函数
def generate_strategy_code(
    user_description: str,
    llm_function: Optional[Callable[[str], str]] = None,
    openai_api_key: Optional[str] = None,
    openai_model: str = "gpt-4",
    validate: bool = True
) -> str:
    """
    便捷函数：快速生成策略代码

    参数：
        user_description: 用户的自然语言描述
        llm_function: 自定义 LLM 函数（可选）
        openai_api_key: OpenAI API Key（可选）
        openai_model: OpenAI 模型名称（默认 "gpt-4"）
        validate: 是否验证代码（默认 True）

    返回：
        str: 生成的 Python 代码

    示例：
        # 使用 OpenAI
        code = generate_strategy_code(
            "股价突破20日均线买入，RSI大于70卖出",
            openai_api_key="sk-..."
        )

        # 使用自定义 LLM
        def my_llm(prompt):
            return "生成的代码..."

        code = generate_strategy_code(
            "写一个策略...",
            llm_function=my_llm
        )
    """
    generator = AIStrategyGenerator()

    if llm_function:
        generator.set_custom_llm(llm_function)
    elif openai_api_key:
        generator.set_openai_api(openai_api_key, openai_model)
    else:
        raise ValueError("必须提供 llm_function 或 openai_api_key")

    return generator.generate(user_description, validate=validate)



