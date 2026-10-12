"""
策略加载器 - 动态加载和重载策略类

功能：
1. 动态导入策略模块（无需硬编码映射表）
2. 自动发现 user/ 目录下的策略文件
3. 安全的 importlib.reload
4. 策略实例缓存
"""

import importlib
import sys
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any, List
from .config_manager import get_config_manager

logger = logging.getLogger(__name__)

STRATEGIES_BASE_DIR = Path(__file__).parent.parent
USER_STRATEGIES_DIR = STRATEGIES_BASE_DIR / "user"


class StrategyLoader:
    """策略动态加载器 - 支持自动发现和热重载"""
    
    _cache: Dict[str, tuple] = {}
    _discovered_modules: Dict[str, str] = {}
    
    @classmethod
    def discover_strategies(cls, force_refresh: bool = False) -> Dict[str, str]:
        """
        自动发现所有策略模块
        
        返回:
            Dict[str, str]: {strategy_id: module_path}
        """
        if cls._discovered_modules and not force_refresh:
            return cls._discovered_modules
        
        cls._discovered_modules.clear()
        
        cls._scan_directory(STRATEGIES_BASE_DIR, "core.quant.strategies", is_root=True)
        
        if USER_STRATEGIES_DIR.exists():
            cls._scan_directory(USER_STRATEGIES_DIR, "core.quant.strategies.user", is_root=False, prefix="user")
        
        logger.info(f"🔍 发现 {len(cls._discovered_modules)} 个策略模块")
        return cls._discovered_modules
    
    @classmethod
    def _scan_directory(
        cls, 
        directory: Path, 
        module_prefix: str, 
        is_root: bool = False,
        prefix: str = ""
    ) -> None:
        """
        扫描目录下的策略文件
        
        参数:
            directory: 要扫描的目录
            module_prefix: 模块路径前缀
            is_root: 是否是根目录（跳过子目录）
            prefix: 策略ID前缀
        """
        if not directory.exists():
            return
        
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix == '.py':
                if entry.name.startswith('_') or entry.name.startswith('.'):
                    continue
                
                module_name = entry.stem
                module_path = f"{module_prefix}.{module_name}"
                
                strategy_id = cls._generate_strategy_id(module_name, prefix)
                
                if strategy_id not in cls._discovered_modules:
                    cls._discovered_modules[strategy_id] = module_path
                    logger.debug(f"发现策略: {strategy_id} -> {module_path}")
            
            elif entry.is_dir() and not is_root:
                if entry.name.startswith('_') or entry.name.startswith('.'):
                    continue
                if entry.name in ('hot_reload', 'templates', 'utils', '__pycache__'):
                    continue
                
                sub_module_prefix = f"{module_prefix}.{entry.name}"
                sub_prefix = f"{prefix}_{entry.name}" if prefix else entry.name
                cls._scan_directory(entry, sub_module_prefix, is_root=False, prefix=sub_prefix)
    
    @classmethod
    def _generate_strategy_id(cls, module_name: str, prefix: str = "") -> str:
        """
        根据模块名生成策略ID
        
        参数:
            module_name: 模块文件名（不含扩展名）
            prefix: 前缀（如 "user"）
        
        返回:
            str: 策略ID
        """
        if prefix:
            return f"{prefix}_{module_name}"
        return module_name
    
    @classmethod
    def get_module_path(cls, strategy_id: str) -> Optional[str]:
        """
        获取策略模块路径
        
        参数:
            strategy_id: 策略ID
        
        返回:
            Optional[str]: 模块路径，未找到返回 None
        """
        discovered = cls.discover_strategies()
        return discovered.get(strategy_id)
    
    @classmethod
    def list_strategies(cls) -> List[str]:
        """列出所有已发现的策略ID"""
        discovered = cls.discover_strategies()
        return list(discovered.keys())
    
    @classmethod
    def load_strategy(
        cls,
        strategy_id: str,
        config: Optional[Dict] = None,
        force_reload: bool = False
    ):
        """
        加载策略（动态导入+配置化）
        
        参数：
            strategy_id: 策略ID
            config: 配置参数（None = 从配置文件加载）
            force_reload: 是否强制重载
        
        返回：
            策略实例
        """
        discovered = cls.discover_strategies()
        module_path = discovered.get(strategy_id)
        
        if not module_path:
            cls.discover_strategies(force_refresh=True)
            module_path = cls._discovered_modules.get(strategy_id)
            
            if not module_path:
                available = list(cls._discovered_modules.keys())
                raise ValueError(f"未知策略ID: {strategy_id}。可用策略: {available}")
        
        if not force_reload and strategy_id in cls._cache:
            logger.debug(f"从缓存加载策略: {strategy_id}")
            _, strategy_class, _ = cls._cache[strategy_id]
        else:
            try:
                if module_path in sys.modules and force_reload:
                    logger.info(f"🔄 重载模块: {module_path}")
                    module = importlib.reload(sys.modules[module_path])
                else:
                    logger.info(f"📦 导入模块: {module_path}")
                    module = importlib.import_module(module_path)
            except Exception as e:
                logger.error(f"导入模块失败: {module_path}, 错误: {e}")
                raise
            
            strategy_class = cls._extract_strategy_class(module)
            if not strategy_class:
                raise ValueError(f"模块中未找到策略类: {module_path}")
        
        if config is None:
            config_manager = get_config_manager()
            config = config_manager.load_config(strategy_id)
        
        try:
            if config:
                if hasattr(strategy_class, '__annotations__'):
                    import inspect
                    module = inspect.getmodule(strategy_class)
                    
                    config_class = None
                    for name, obj in inspect.getmembers(module):
                        if name.endswith('Config') or name.endswith('Configpro'):
                            if inspect.isclass(obj) and hasattr(obj, '__dataclass_fields__'):
                                config_class = obj
                                break
                    
                    if config_class:
                        logger.debug(f"使用配置类: {config_class.__name__}")
                        config_instance = config_class(**config)
                        strategy_instance = strategy_class(config=config_instance)
                    else:
                        strategy_instance = strategy_class(config=config)
                else:
                    strategy_instance = strategy_class(**config)
            else:
                strategy_instance = strategy_class()
            
            cls._cache[strategy_id] = (module, strategy_class, strategy_instance)
            
            logger.info(f"✅ 策略加载成功: {strategy_id}")
            return strategy_instance
            
        except Exception as e:
            logger.error(f"策略实例化失败: {strategy_id}, 错误: {e}")
            raise
    
    @classmethod
    def reload_strategy(cls, strategy_id: str, config: Optional[Dict] = None):
        """
        热重载策略（用于文件变更后）
        
        参数：
            strategy_id: 策略ID
            config: 新配置（None = 从配置文件加载）
        
        返回：
            新的策略实例
        """
        logger.info(f"🔥 热重载策略: {strategy_id}")
        
        cls._cache.pop(strategy_id, None)
        
        config_manager = get_config_manager()
        config_manager.clear_cache(strategy_id)
        
        cls.discover_strategies(force_refresh=True)
        
        return cls.load_strategy(strategy_id, config=config, force_reload=True)
    
    @classmethod
    def reload_by_path(cls, file_path: str) -> Optional[str]:
        """
        根据文件路径重载策略
        
        参数：
            file_path: 策略文件路径
        
        返回:
            Optional[str]: 重载的策略ID，如果无法识别则返回 None
        """
        file_path = Path(file_path)
        
        if not file_path.exists():
            logger.warning(f"文件不存在: {file_path}")
            return None
        
        module_name = file_path.stem
        
        try:
            relative_path = file_path.relative_to(STRATEGIES_BASE_DIR)
            parts = list(relative_path.parts)
            
            if 'user' in parts:
                prefix = "user"
                strategy_id = f"user_{module_name}"
            else:
                strategy_id = module_name
            
            cls.discover_strategies(force_refresh=True)
            
            module_path = cls._discovered_modules.get(strategy_id)
            
            if not module_path:
                for sid, mpath in cls._discovered_modules.items():
                    if module_name in mpath:
                        strategy_id = sid
                        module_path = mpath
                        break
            
            if module_path:
                cls.reload_strategy(strategy_id)
                logger.info(f"✅ 自动重载成功: {file_path} -> {strategy_id}")
                return strategy_id
            else:
                logger.debug(f"无法识别策略文件（非热重载策略）: {file_path}")
                return None
                
        except Exception as e:
            logger.error(f"❌ 自动重载失败: {file_path}, 错误: {e}")
            return None
    
    @classmethod
    def _extract_strategy_class(cls, module):
        """
        从模块中提取策略类
        
        策略类的特征：
        1. 继承自 StrategyBase 或 VectorizedStrategyBase
        2. 有 strategy_id 属性
        """
        from core.quant.strategies.strategy_framework import StrategyBase
        from core.quant.strategies.vectorized_base import VectorizedStrategyBase
        
        for name in dir(module):
            obj = getattr(module, name)
            
            if not isinstance(obj, type):
                continue
            
            if issubclass(obj, (StrategyBase, VectorizedStrategyBase)):
                if obj not in (StrategyBase, VectorizedStrategyBase):
                    return obj
        
        return None
    
    @classmethod
    def register_strategy(cls, strategy_id: str, module_path: str):
        """
        注册新策略映射
        
        参数:
            strategy_id: 策略ID
            module_path: 模块路径
        """
        cls._discovered_modules[strategy_id] = module_path
        logger.info(f"注册策略: {strategy_id} -> {module_path}")
    
    @classmethod
    def unregister_strategy(cls, strategy_id: str) -> bool:
        """
        取消注册策略
        
        参数:
            strategy_id: 策略ID
        
        返回:
            bool: 是否成功取消注册
        """
        if strategy_id in cls._discovered_modules:
            del cls._discovered_modules[strategy_id]
            cls._cache.pop(strategy_id, None)
            logger.info(f"取消注册策略: {strategy_id}")
            return True
        return False
    
    @classmethod
    def clear_cache(cls) -> None:
        """清除所有缓存"""
        cls._cache.clear()
        cls._discovered_modules.clear()
        logger.info("已清除所有策略缓存")
