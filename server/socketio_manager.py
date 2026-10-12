
"""
全局 Socket.IO 实例管理器

解决多 Socket.IO 实例问题，确保所有模块都使用同一个正确的实例
同时保存主事件循环引用，以便从后台线程发送异步事件
"""
from typing import Optional, Any
import asyncio
from config.logger import get_logger

logger = get_logger(__name__)

# 全局 Socket.IO 实例
_global_socketio: Optional[Any] = None

# 全局主事件循环
_global_loop: Optional[asyncio.AbstractEventLoop] = None


def set_global_socketio(sio: Any):
    """
    设置全局 Socket.IO 实例
    
    Args:
        sio: Socket.IO 实例 (可以是 Flask-SocketIO 或 AsyncServer)
    """
    global _global_socketio
    _global_socketio = sio
    logger.info(f"[SocketIO Manager] 全局 Socket.IO 实例已设置: {type(sio).__name__}")


def set_global_loop(loop: asyncio.AbstractEventLoop):
    """
    设置全局主事件循环
    
    Args:
        loop: 主事件循环
    """
    global _global_loop
    _global_loop = loop
    logger.info(f"[SocketIO Manager] 全局事件循环已设置: {loop}")


def get_global_socketio() -> Optional[Any]:
    """
    获取全局 Socket.IO 实例
    
    Returns:
        Socket.IO 实例，如果未设置则返回 None
    """
    return _global_socketio


def get_global_loop() -> Optional[asyncio.AbstractEventLoop]:
    """
    获取全局主事件循环
    
    Returns:
        主事件循环，如果未设置则返回 None
    """
    return _global_loop


def emit(event: str, data: Any, **kwargs):
    """
    发送 Socket.IO 事件（使用全局实例）
    
    自动处理 AsyncServer 和 Flask-SocketIO 的差异
    
    Args:
        event: 事件名称
        data: 事件数据
        **kwargs: 其他 emit 参数（如 room, namespace 等）
    """
    sio = get_global_socketio()
    if sio is None:
        logger.warning("[SocketIO Manager] 全局 Socket.IO 实例未设置，无法发送事件")
        return False
    
    try:
        # 检查是否是 AsyncServer（需要异步调用）
        if hasattr(sio, 'emit') and asyncio.iscoroutinefunction(sio.emit):
            # AsyncServer 模式
            loop = get_global_loop()
            
            async def emit_async():
                await sio.emit(event, data, **kwargs)
            
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(emit_async(), loop)
            else:
                asyncio.run(emit_async())
        else:
            # Flask-SocketIO 或同步模式
            sio.emit(event, data, **kwargs)
        
        logger.debug(f"[SocketIO Manager] 事件已发送: {event}")
        return True
    except Exception as e:
        logger.error(f"[SocketIO Manager] 发送事件失败: {event}, 错误: {e}", exc_info=True)
        return False


