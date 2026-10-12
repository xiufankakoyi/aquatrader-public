"""
飞书机器人事件处理器 (core/feishu_bot/handlers.py)
提供常用的消息和事件处理器
"""
import json
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable

logger = logging.getLogger(__name__)


class BaseHandler(ABC):
    """
    处理器基类
    """
    
    @abstractmethod
    def handle(self, data: Any, bot) -> None:
        """
        处理事件
        
        Args:
            data: 事件数据
            bot: 机器人实例
        """
        pass


class MessageHandler(BaseHandler):
    """
    消息处理器
    
    支持关键词匹配、正则匹配、命令处理等
    """
    
    def __init__(self):
        self._handlers: list[tuple[Callable[[str], bool], Callable]] = []
        self._default_handler: Optional[Callable] = None
    
    def on_keyword(self, keyword: str, handler: Callable) -> 'MessageHandler':
        """
        注册关键词处理器
        
        Args:
            keyword: 关键词
            handler: 处理函数 (message_info, bot) -> None
            
        Returns:
            self，支持链式调用
        """
        def matcher(text: str) -> bool:
            return keyword in text
        
        self._handlers.append((matcher, handler))
        return self
    
    def on_command(self, command: str, handler: Callable) -> 'MessageHandler':
        """
        注册命令处理器
        
        Args:
            command: 命令（如 '/help'）
            handler: 处理函数
            
        Returns:
            self
        """
        def matcher(text: str) -> bool:
            return text.strip().startswith(command)
        
        self._handlers.append((matcher, handler))
        return self
    
    def on_pattern(self, pattern: str, handler: Callable) -> 'MessageHandler':
        """
        注册正则匹配处理器
        
        Args:
            pattern: 正则表达式字符串
            handler: 处理函数
            
        Returns:
            self
        """
        import re
        compiled = re.compile(pattern)
        
        def matcher(text: str) -> bool:
            return bool(compiled.search(text))
        
        self._handlers.append((matcher, handler))
        return self
    
    def default(self, handler: Callable) -> 'MessageHandler':
        """
        设置默认处理器（当没有匹配时调用）
        
        Args:
            handler: 处理函数
            
        Returns:
            self
        """
        self._default_handler = handler
        return self
    
    def handle(self, message_info: Dict[str, Any], bot) -> None:
        """
        处理消息事件
        
        Args:
            message_info: 消息信息
            bot: 机器人实例
        """
        content = message_info.get('content', '')
        content_type = message_info.get('content_type', '')
        
        if content_type != 'text':
            logger.debug(f"跳过非文本消息: {content_type}")
            return
        
        try:
            content_json = json.loads(content)
            text = content_json.get('text', '')
        except (json.JSONDecodeError, TypeError):
            text = content
        
        for matcher, handler in self._handlers:
            try:
                if matcher(text):
                    handler(message_info, bot, text)
                    return
            except Exception as e:
                logger.error(f"消息匹配器执行失败: {e}", exc_info=True)
        
        if self._default_handler:
            try:
                self._default_handler(message_info, bot, text)
            except Exception as e:
                logger.error(f"默认处理器执行失败: {e}", exc_info=True)


class EventHandler(BaseHandler):
    """
    通用事件处理器
    """
    
    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
    
    def on(self, event_type: str, handler: Callable) -> 'EventHandler':
        """
        注册事件处理器
        
        Args:
            event_type: 事件类型
            handler: 处理函数
            
        Returns:
            self
        """
        self._handlers[event_type] = handler
        return self
    
    def handle(self, data: Any, bot) -> None:
        """
        处理事件
        
        Args:
            data: 事件数据
            bot: 机器人实例
        """
        event_type = getattr(data, 'header', {}).get('event_type', 'unknown')
        
        handler = self._handlers.get(event_type)
        if handler:
            try:
                handler(data, bot)
            except Exception as e:
                logger.error(f"事件处理器执行失败: {e}", exc_info=True)
        else:
            logger.debug(f"未注册的事件类型: {event_type}")


def create_echo_handler() -> Callable:
    """
    创建回声处理器（调试用）
    
    Returns:
        处理函数
    """
    def handler(message_info: Dict[str, Any], bot, text: str) -> None:
        chat_id = message_info.get('chat_id')
        if chat_id:
            bot.send_text(chat_id, f"收到: {text}")
    
    return handler


def create_help_handler(commands: Dict[str, str]) -> Callable:
    """
    创建帮助处理器
    
    Args:
        commands: 命令字典 {命令: 描述}
        
    Returns:
        处理函数
    """
    def handler(message_info: Dict[str, Any], bot, text: str) -> None:
        chat_id = message_info.get('chat_id')
        if not chat_id:
            return
            
        help_text = "🤖 **可用命令**\n\n"
        for cmd, desc in commands.items():
            help_text += f"- `{cmd}`: {desc}\n"
        
        bot.send_markdown(chat_id, "帮助", help_text)
    
    return handler
