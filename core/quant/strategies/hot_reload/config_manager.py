"""
配置管理器 - 策略参数的加载、保存和验证

功能：
1. 从 JSON/YAML 加载策略配置
2. 配置验证与默认值
3. 配置变更通知
"""

import json
import os
from pathlib import Path
from typing import Dict, Optional, Any
from dataclasses import fields, is_dataclass, asdict
import logging

logger = logging.getLogger(__name__)


class ConfigManager:
    """策略配置管理器"""
    
    # 配置文件目录
    CONFIG_DIR = Path(__file__).parent.parent / "configs"
    
    def __init__(self):
        """初始化配置管理器"""
        # 确保配置目录存在
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        # 配置缓存 {strategy_id: config_dict}
        self._cache: Dict[str, Dict] = {}
    
    @classmethod
    def get_config_path(cls, strategy_id: str) -> Path:
        """获取策略配置文件路径"""
        return cls.CONFIG_DIR / f"{strategy_id}.json"
    
    def load_config(self, strategy_id: str) -> Optional[Dict]:
        """
        加载策略配置
        
        参数：
            strategy_id: 策略ID
        
        返回：
            配置字典，如果不存在返回 None
        """
        # 检查缓存
        if strategy_id in self._cache:
            logger.debug(f"从缓存加载配置: {strategy_id}")
            return self._cache[strategy_id].copy()
        
        # 从文件加载
        config_path = self.get_config_path(strategy_id)
        
        if not config_path.exists():
            logger.warning(f"配置文件不存在: {config_path}")
            return None
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
            
            # 提取参数部分
            params = config_data.get('parameters', {})
            
            # 缓存
            self._cache[strategy_id] = params
            
            logger.info(f"✅ 加载配置成功: {strategy_id} ({len(params)} 个参数)")
            return params.copy()
            
        except Exception as e:
            logger.error(f"加载配置失败: {config_path}, 错误: {e}")
            return None
    
    def save_config(
        self,
        strategy_id: str,
        params: Dict[str, Any],
        metadata: Optional[Dict] = None
    ) -> bool:
        """
        保存策略配置
        
        参数：
            strategy_id: 策略ID
            params: 参数字典
            metadata: 元数据（描述、作者等）
        
        返回：
            是否保存成功
        """
        config_path = self.get_config_path(strategy_id)
        
        # 构建完整配置
        config_data = {
            "strategy_id": strategy_id,
            "version": "1.0",
            "parameters": params,
            "metadata": metadata or {
                "description": f"Configuration for {strategy_id}",
                "last_modified": self._get_timestamp()
            }
        }
        
        try:
            # 保存到文件
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            
            # 更新缓存
            self._cache[strategy_id] = params.copy()
            
            logger.info(f"✅ 保存配置成功: {config_path}")
            return True
            
        except Exception as e:
            logger.error(f"保存配置失败: {config_path}, 错误: {e}")
            return False
    
    def get_default_config(self, strategy_class) -> Dict:
        """
        从策略类提取默认配置
        
        参数：
            strategy_class: 策略类（需要有 config 属性）
        
        返回：
            默认配置字典
        """
        try:
            # 尝试实例化获取默认配置
            instance = strategy_class()
            
            if hasattr(instance, 'config') and is_dataclass(instance.config):
                # 从 dataclass 提取
                config_dict = asdict(instance.config)
                
                # 过滤掉 metadata
                filtered_config = {
                    k: v for k, v in config_dict.items()
                    if not k.startswith('_')
                }
                
                logger.info(f"✅ 提取默认配置: {strategy_class.__name__}")
                return filtered_config
            
            elif hasattr(instance, '__dict__'):
                # fallback: 从实例属性提取
                config_dict = {
                    k: v for k, v in instance.__dict__.items()
                    if not k.startswith('_') and isinstance(v, (int, float, str, bool))
                }
                return config_dict
            
            else:
                logger.warning(f"无法提取默认配置: {strategy_class.__name__}")
                return {}
                
        except Exception as e:
            logger.error(f"提取默认配置失败: {strategy_class.__name__}, 错误: {e}")
            return {}
    
    def validate_config(self, strategy_class, config: Dict) -> tuple[bool, str]:
        """
        验证配置有效性
        
        参数：
            strategy_class: 策略类
            config: 配置字典
        
        返回：
            (是否有效, 错误消息)
        """
        try:
            # 尝试用配置实例化策略
            if hasattr(strategy_class, 'config') and is_dataclass(strategy_class.config):
                # 创建配置实例
                config_class = strategy_class.config
                config_instance = config_class(**config)
                
                # 尝试创建策略实例
                strategy_instance = strategy_class(config=config_instance)
                
                return True, "配置有效"
            
            else:
                # fallback: 直接实例化
                strategy_instance = strategy_class(**config)
                return True, "配置有效"
                
        except TypeError as e:
            return False, f"参数类型错误: {e}"
        except ValueError as e:
            return False, f"参数值错误: {e}"
        except Exception as e:
            return False, f"配置验证失败: {e}"
    
    def clear_cache(self, strategy_id: Optional[str] = None):
        """清除配置缓存"""
        if strategy_id:
            self._cache.pop(strategy_id, None)
            logger.debug(f"清除缓存: {strategy_id}")
        else:
            self._cache.clear()
            logger.debug("清除所有配置缓存")
    
    def list_configs(self) -> list[str]:
        """列出所有可用的配置文件"""
        config_files = list(self.CONFIG_DIR.glob("*.json"))
        return [f.stem for f in config_files]
    
    @staticmethod
    def _get_timestamp() -> str:
        """获取当前时间戳"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 全局单例
_config_manager = None

def get_config_manager() -> ConfigManager:
    """获取全局配置管理器实例"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager
