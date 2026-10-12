"""
文件监听器 - 监听策略文件变化并触发重载

使用 watchdog 库监听文件系统事件
支持递归监听 core/strategies/ 目录（包含子目录）
"""

import logging
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
from .loader import StrategyLoader

logger = logging.getLogger(__name__)

STRATEGIES_BASE_DIR = Path(__file__).parent.parent


class StrategyFileHandler(FileSystemEventHandler):
    """策略文件变化处理器"""
    
    def __init__(self, debounce_seconds: float = 1.0):
        """
        初始化
        
        参数：
            debounce_seconds: 防抖延迟（秒），避免频繁重载
        """
        super().__init__()
        self.debounce_seconds = debounce_seconds
        self._last_reload_time = {}
    
    def on_modified(self, event):
        """文件修改事件"""
        self._handle_file_event(event, "修改")
    
    def on_created(self, event):
        """文件创建事件"""
        self._handle_file_event(event, "创建")
    
    def _handle_file_event(self, event, event_type: str):
        """
        处理文件事件
        
        参数:
            event: 文件系统事件
            event_type: 事件类型（修改/创建）
        """
        if event.is_directory:
            return
        
        if not event.src_path.endswith('.py'):
            return
        
        if '__pycache__' in event.src_path or event.src_path.endswith('__init__.py'):
            return
        
        file_path = event.src_path
        
        current_time = time.time()
        last_reload = self._last_reload_time.get(file_path, 0)
        
        if current_time - last_reload < self.debounce_seconds:
            logger.debug(f"防抖跳过: {file_path}")
            return
        
        self._last_reload_time[file_path] = current_time
        
        logger.info(f"🔔 检测到文件{event_type}: {file_path}")
        
        try:
            strategy_id = StrategyLoader.reload_by_path(file_path)
            if strategy_id:
                logger.info(f"✅ 自动重载成功: {file_path} -> {strategy_id}")
            else:
                logger.debug(f"文件不在策略监听范围内: {file_path}")
        except Exception as e:
            logger.error(f"❌ 自动重载失败: {file_path}, 错误: {e}")


class StrategyWatcher:
    """策略文件监听器 - 支持递归监听"""
    
    def __init__(
        self,
        watch_dir: str = None,
        debounce_seconds: float = 1.0,
        recursive: bool = True
    ):
        """
        初始化监听器
        
        参数：
            watch_dir: 监听目录（默认 = core/strategies/）
            debounce_seconds: 防抖延迟
            recursive: 是否递归监听子目录（默认 True）
        """
        if watch_dir is None:
            watch_dir = str(STRATEGIES_BASE_DIR)
        
        self.watch_dir = watch_dir
        self.debounce_seconds = debounce_seconds
        self.recursive = recursive
        
        self.observer = Observer()
        self.handler = StrategyFileHandler(debounce_seconds=debounce_seconds)
        
        self._running = False
    
    def start(self):
        """启动监听器"""
        if self._running:
            logger.warning("监听器已在运行")
            return
        
        self.observer.schedule(
            self.handler,
            self.watch_dir,
            recursive=self.recursive
        )
        
        self.observer.start()
        self._running = True
        
        logger.info(f"👁️  策略文件监听器已启动: {self.watch_dir}")
        logger.info(f"   防抖延迟: {self.debounce_seconds}s")
        logger.info(f"   递归监听: {self.recursive}")
        
        if self.recursive:
            self._log_watched_directories()
    
    def _log_watched_directories(self):
        """记录被监听的目录"""
        watch_path = Path(self.watch_dir)
        if watch_path.exists():
            subdirs = [d for d in watch_path.iterdir() if d.is_dir() and not d.name.startswith('_')]
            if subdirs:
                logger.info(f"   监听子目录: {', '.join(d.name for d in subdirs[:5])}")
    
    def stop(self):
        """停止监听器"""
        if not self._running:
            return
        
        self.observer.stop()
        self.observer.join(timeout=5)
        self._running = False
        
        logger.info("🛑 策略文件监听器已停止")
    
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running
    
    def trigger_reload(self, strategy_id: str = None, file_path: str = None) -> bool:
        """
        手动触发热重载
        
        参数:
            strategy_id: 策略ID（可选）
            file_path: 文件路径（可选）
        
        返回:
            bool: 是否成功触发重载
        """
        try:
            if file_path:
                result = StrategyLoader.reload_by_path(file_path)
                return result is not None
            elif strategy_id:
                StrategyLoader.reload_strategy(strategy_id)
                return True
            else:
                StrategyLoader.discover_strategies(force_refresh=True)
                logger.info("已刷新策略发现缓存")
                return True
        except Exception as e:
            logger.error(f"手动重载失败: {e}")
            return False


_watcher_instance = None


def get_watcher(auto_start: bool = False) -> StrategyWatcher:
    """
    获取全局监听器实例
    
    参数:
        auto_start: 是否自动启动
    
    返回:
        StrategyWatcher: 监听器实例
    """
    global _watcher_instance
    if _watcher_instance is None:
        _watcher_instance = StrategyWatcher(recursive=True)
        if auto_start:
            _watcher_instance.start()
    return _watcher_instance


def start_watcher() -> StrategyWatcher:
    """启动全局监听器"""
    watcher = get_watcher()
    if not watcher.is_running():
        watcher.start()
    return watcher


def stop_watcher() -> None:
    """停止全局监听器"""
    global _watcher_instance
    if _watcher_instance and _watcher_instance.is_running():
        _watcher_instance.stop()
