"""
LLM 客户端 - 系统与 AI 对话的桥梁

支持连接到本地 LLM 服务（如 LM Studio）或 OpenAI API。
会自动处理 Markdown 格式清洗，确保生成的代码能直接用。
"""

import openai
import logging
import re
from urllib.parse import urlparse
from config.config import Config
from core.utils.llm_usage import (
    LLMUsageStats,
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    load_llm_providers,
    record_llm_usage,
)

logger = logging.getLogger(__name__)

_LOCAL_LLM_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _provider_api_key(provider) -> str:
    """Return a usable OpenAI-compatible key or reject unsafe remote config."""
    api_key = str(provider.api_key or "").strip()
    if api_key:
        return api_key

    host = (urlparse(str(provider.api_base or "")).hostname or "").lower()
    if host in _LOCAL_LLM_HOSTS:
        # The OpenAI SDK requires a non-empty value even when the local server
        # intentionally does not authenticate requests.
        return "not-needed"

    raise ValueError(
        f"LLM provider {provider.name!r} has no API key; "
        "configure it through the secrets/settings layer"
    )


class AquaLLM:
    """
    AquaTrade LLM 客户端
    
    使用示例:
        llm = AquaLLM()
        code = llm.generate_code(
            user_prompt="写一个策略：股价突破20日均线买入",
            system_prompt="你是一个专业的量化策略代码生成器。"
        )
    """
    
    def __init__(self):
        """
        初始化 LLM 客户端，连接到本地 LLM 服务或 OpenAI API
        """
        # 初始化 OpenAI 客户端，连接到本地 LM Studio 或 OpenAI
        self.providers = load_llm_providers(
            Config.LLM_API_BASE,
            Config.LLM_API_KEY,
            Config.LLM_MODEL_NAME,
        )
        # Clients are created per request so provider fallback remains lazy.
        self.client = None
        self.usage_stats = LLMUsageStats()
        self.logger = logging.getLogger("AquaLLM")
        
        self.logger.info(
            f"LLM 客户端已初始化: base_url={Config.LLM_API_BASE}, "
            f"model={Config.LLM_MODEL_NAME}"
        )
    
    def generate_code(
        self, 
        user_prompt: str, 
        system_prompt: str = None
    ) -> str:
        """
        专门用于生成代码的接口，会自动清洗 Markdown 标记和思考过程
        
        参数:
            user_prompt: 用户提示词
            system_prompt: 系统提示词（可选，有默认值）
        
        返回:
            str: 清洗后的代码
        """
        if not system_prompt:
            system_prompt = (
                "你是一个专业的量化交易策略代码生成器。"
                "只输出 Python 代码，不要输出任何解释。"
            )
        
        return self._generate_content(user_prompt, system_prompt)
    
    def generate_report(
        self, 
        user_prompt: str, 
        system_prompt: str = None
    ) -> str:
        """
        专门用于生成报告的接口 (非流式)
        """
        if not system_prompt:
            system_prompt = (
                "你是一位拥有20年经验的量化基金风控总监。"
                "你的任务是阅读用户的策略和回测数据，生成一份专业的投资分析报告。"
            )
        
        return self._generate_content(user_prompt, system_prompt)

    def generate_report_stream(
        self, 
        user_prompt: str, 
        system_prompt: str = None
    ):
        """
        流式生成报告接口
        
        返回:
            Generator yielding cleaned text chunks
        """
        if not system_prompt:
            system_prompt = (
                "你是一位拥有20年经验的量化基金风控总监。"
                "你的任务是阅读用户的策略和回测数据，生成一份专业的投资分析报告。"
            )
        
        return self._generate_content_stream(user_prompt, system_prompt)
    
    @staticmethod
    def _extract_message_text(message) -> str:
        """
        从 message / delta 中提取有效文本

        兼容字段：
        - content（标准输出）
        - reasoning_content（OpenAI o 系列等）
        - reasoning（Qwen 等部分 API）

        优先使用 content；content 为空时回退到 reasoning。

        Args:
            message: ChatCompletionMessage 或 ChoiceDelta 对象

        Returns:
            str: 提取到的文本
        """
        content = getattr(message, "content", None) or ""
        reasoning = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None) or ""
        return content.strip() if content.strip() else reasoning.strip()

    def _generate_content(
        self, 
        user_prompt: str, 
        system_prompt: str
    ) -> str:
        """
        核心生成方法 (非流式)
        """
        try:
            self.logger.info(f"调用 LLM 生成内容 (同步)，长度: {len(user_prompt)}")
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
            text = self._chat_completion_text(
                messages=messages,
                max_tokens=Config.LLM_MAX_TOKENS,
                source="AquaLLM._generate_content",
            )
            return self._clean_code(text)
            
        except Exception as e:
            self.logger.error(f"LLM 调用失败: {e}", exc_info=True)
            raise Exception(f"LLM 调用失败: {e}")

    def _generate_content_stream(
        self, 
        user_prompt: str, 
        system_prompt: str
    ):
        """
        核心生成方法 (流式)
        """
        try:
            self.logger.info(f"调用 LLM 生成内容 (流式)，长度: {len(user_prompt)}")
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
            stream, provider_name, model_name, prompt_tokens = self._chat_completion_stream(
                messages=messages,
                max_tokens=Config.LLM_MAX_TOKENS,
                source="AquaLLM._generate_content_stream",
            )
            completion_parts = []
            
            # 由于流式无法直接使用 _clean_code（因为它需要完整文本）
            # 我们这里采取简单处理：实时 yield 原始文本块
            # 复杂的 <think> 过滤需要更精细的流处理，这里先保持简单
            for chunk in stream:
                text = self._extract_message_text(chunk.choices[0].delta)
                if text:
                    completion_parts.append(text)
                    yield text
            completion_tokens = estimate_tokens("".join(completion_parts))
            self.usage_stats.add(prompt_tokens, completion_tokens, True)
            record_llm_usage(
                source="AquaLLM._generate_content_stream",
                provider=provider_name,
                model=model_name,
                prompt_tokens_est=prompt_tokens,
                completion_tokens_est=completion_tokens,
                success=True,
            )
                    
        except Exception as e:
            self.logger.error(f"LLM 流式调用失败: {e}", exc_info=True)
            yield f"\n[ERROR] LLM 流式调用失败: {str(e)}"
    
    def _chat_completion_text(
        self,
        messages: list[dict],
        max_tokens: int,
        source: str,
    ) -> str:
        if not self.providers:
            raise RuntimeError("No LLM provider configured")
        prompt_tokens = estimate_messages_tokens(messages)
        check_llm_budget_or_raise(prompt_tokens, max_tokens, source=source)
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                client = openai.OpenAI(
                    base_url=provider.api_base,
                    api_key=_provider_api_key(provider),
                )
                response = client.chat.completions.create(
                    model=provider.model_name,
                    messages=messages,
                    temperature=Config.LLM_TEMPERATURE,
                    max_tokens=max_tokens,
                    stream=False,
                )
                text = self._extract_message_text(response.choices[0].message)
                completion_tokens = estimate_tokens(text)
                self.usage_stats.add(prompt_tokens, completion_tokens, True)
                record_llm_usage(
                    source=source,
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=completion_tokens,
                    success=True,
                )
                return text
            except Exception as exc:
                last_error = exc
                self.usage_stats.add(prompt_tokens, 0, False)
                record_llm_usage(
                    source=source,
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
                self.logger.warning("LLM provider failed: %s %s", provider.name, exc)
        raise RuntimeError(last_error or "all LLM providers failed")

    def _chat_completion_stream(
        self,
        messages: list[dict],
        max_tokens: int,
        source: str,
    ):
        if not self.providers:
            raise RuntimeError("No LLM provider configured")
        prompt_tokens = estimate_messages_tokens(messages)
        check_llm_budget_or_raise(prompt_tokens, max_tokens, source=source)
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                client = openai.OpenAI(
                    base_url=provider.api_base,
                    api_key=_provider_api_key(provider),
                )
                stream = client.chat.completions.create(
                    model=provider.model_name,
                    messages=messages,
                    temperature=Config.LLM_TEMPERATURE,
                    max_tokens=max_tokens,
                    stream=True,
                )
                return stream, provider.name, provider.model_name, prompt_tokens
            except Exception as exc:
                last_error = exc
                self.usage_stats.add(prompt_tokens, 0, False)
                record_llm_usage(
                    source=source,
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
                self.logger.warning("LLM provider failed: %s %s", provider.name, exc)
        raise RuntimeError(last_error or "all LLM providers failed")

    def _clean_code(self, raw_text: str) -> str:
        """
        清洗 LLM 输出，去除思考过程和 Markdown 标记
        
        处理内容:
        1. 去除 DeepSeek R1 的思考标签 <think>...</think>
        2. 去除其他可能的思考标签（如 <think>...</think>）
        3. 去除 Markdown 代码块标记 (```python ... ```)
        4. 去除多余的空行
        
        参数:
            raw_text: 原始 LLM 输出
        
        返回:
            str: 清洗后的代码
        """
        if not raw_text:
            return ""
        
        clean = raw_text.strip()
        
        # 1. 去除 DeepSeek R1 的思考标签 <think>...</think>
        clean = re.sub(
            r'<think>.*?</think>', 
            '', 
            clean, 
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # 2. 去除其他可能的思考标签
        clean = re.sub(
            r'<think>.*?</think>', 
            '', 
            clean, 
            flags=re.DOTALL | re.IGNORECASE
        )
        
        clean = re.sub(
            r'<reasoning>.*?</reasoning>', 
            '', 
            clean, 
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # 2.1 去除中文思考标签
        clean = re.sub(
            r'思索过程.*?实际代码',
            '实际代码',
            clean,
            flags=re.DOTALL
        )
        
        clean = re.sub(
            r'推理过程.*?实际代码',
            '实际代码',
            clean,
            flags=re.DOTALL
        )

        # 2.2 去除 Qwen 风格 Thinking Process 中的思考章节，保留最终正文
        if re.search(r'Thinking Process', clean, flags=re.IGNORECASE):
            # 优先提取最后一个代码块（代码类任务）
            code_blocks = re.findall(r'```[\w]*\n(.*?)```', clean, flags=re.DOTALL)
            if code_blocks:
                clean = code_blocks[-1].strip()
            else:
                # 否则取最后一个包含中文的实质性段落
                paragraphs = [p.strip() for p in re.split(r'\n{2,}', clean) if p.strip()]
                candidate = ""
                for p in reversed(paragraphs):
                    # 跳过纯元信息段落（选项、草稿、分析等）
                    if re.match(r'^(\*|\d+\.|Option|Draft|Analyze|Thinking|Character|Final)', p, flags=re.IGNORECASE):
                        continue
                    if re.search(r'[\u4e00-\u9fff]', p) or len(p) >= 20:
                        candidate = p
                        break
                clean = candidate if candidate else clean

        # 3. 去除 Markdown 代码块标记
        clean = clean.strip()
        if clean.startswith("```"):
            lines = clean.split('\n')
            # 去掉第一行 (```python 或 ```)
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            # 去掉最后一行 (```)
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            clean = "\n".join(lines)
        
        # 4. 去除多余的空行（保留最多2个连续空行）
        clean = re.sub(r'\n{3,}', '\n\n', clean)
        
        # 5. 去除行首行尾空白
        clean = clean.strip()
        
        return clean


