"""
配置加载器 - 从 YAML/JSON 文件加载策略配置

支持的功能：
1. 从 YAML/JSON 文件加载配置
2. 配置验证和默认值填充
3. 配置模板继承
4. 配置保存和导出

使用示例：
    loader = StrategyConfigLoader()
    
    # 从 YAML 加载
    config = loader.load("strategies/configs/dual_ma_strategy.yaml")
    
    # 从字典加载
    config = loader.load_from_dict({"strategy_id": "test", ...})
    
    # 保存配置
    loader.save(config, "output.yaml")
"""

import json
import yaml
from pathlib import Path
from typing import Dict, Any, Optional, Union
from datetime import datetime

from .strategy_config import StrategyConfig


class StrategyConfigLoader:
    """
    策略配置加载器
    
    负责从各种来源加载策略配置
    """
    
    def __init__(self, config_dir: Optional[str] = None):
        """
        初始化配置加载器
        
        参数：
            config_dir: 配置文件目录，默认为 core/strategies/configs
        """
        if config_dir is None:
            # 默认配置目录
            self.config_dir = Path(__file__).parent.parent / "configs"
        else:
            self.config_dir = Path(config_dir)
    
    def load(self, path: Union[str, Path]) -> StrategyConfig:
        """
        从文件加载策略配置
        
        参数：
            path: 配置文件路径（相对或绝对路径）
        
        返回：
            StrategyConfig: 策略配置对象
        """
        path = Path(path)
        
        # 如果是相对路径，在配置目录中查找
        if not path.is_absolute():
            path = self.config_dir / path
        
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        
        # 根据文件扩展名选择解析器
        suffix = path.suffix.lower()
        
        with open(path, 'r', encoding='utf-8') as f:
            if suffix in ('.yaml', '.yml'):
                data = yaml.safe_load(f)
            elif suffix == '.json':
                data = json.load(f)
            else:
                raise ValueError(f"不支持的文件格式: {suffix}")
        
        return self._parse_config(data)
    
    def load_from_dict(self, data: Dict[str, Any]) -> StrategyConfig:
        """
        从字典加载策略配置
        
        参数：
            data: 配置字典
        
        返回：
            StrategyConfig: 策略配置对象
        """
        return self._parse_config(data)
    
    def load_from_string(self, content: str, format: str = 'yaml') -> StrategyConfig:
        """
        从字符串加载策略配置
        
        参数：
            content: 配置内容字符串
            format: 格式 ('yaml' 或 'json')
        
        返回：
            StrategyConfig: 策略配置对象
        """
        if format.lower() in ('yaml', 'yml'):
            data = yaml.safe_load(content)
        elif format.lower() == 'json':
            data = json.loads(content)
        else:
            raise ValueError(f"不支持的格式: {format}")
        
        return self._parse_config(data)
    
    def _parse_config(self, data: Dict[str, Any]) -> StrategyConfig:
        """
        解析配置字典为 StrategyConfig 对象
        
        参数：
            data: 配置字典
        
        返回：
            StrategyConfig: 策略配置对象
        """
        # 处理可能的嵌套结构（如 strategy: {...}）
        if 'strategy' in data:
            data = data['strategy']
        
        # 确保必需的字段存在
        if 'strategy_id' not in data:
            raise ValueError("配置缺少必需的字段: strategy_id")
        if 'name' not in data:
            raise ValueError("配置缺少必需的字段: name")
        
        # 处理简化的 parameters 格式 (dict -> 空列表)
        # 简化格式: {"parameters": {"key": value}} -> 参数值存储
        # 标准格式: {"parameters": [{"name": "key", ...}]} -> ParameterConfig 列表
        if 'parameters' in data and isinstance(data['parameters'], dict):
            # 简化格式，将参数值存储到 extra_params 中，parameters 设为空列表
            data['_simplified_params'] = data.pop('parameters')
            data['parameters'] = []
        
        # 设置默认值
        defaults = {
            'version': '1.0',
            'parameters': [],
            'indicators': [],
            'rules': [],
        }
        
        for key, value in defaults.items():
            if key not in data or data[key] is None:
                data[key] = value
        
        # 添加时间戳
        if 'created_at' not in data:
            data['created_at'] = datetime.now().isoformat()
        data['updated_at'] = datetime.now().isoformat()
        
        return StrategyConfig(**data)
    
    def save(
        self,
        config: StrategyConfig,
        path: Union[str, Path],
        format: Optional[str] = None
    ):
        """
        保存策略配置到文件
        
        参数：
            config: 策略配置对象
            path: 保存路径
            format: 保存格式 ('yaml' 或 'json')，默认从文件扩展名推断
        """
        path = Path(path)
        
        # 确定格式
        if format is None:
            suffix = path.suffix.lower()
            if suffix in ('.yaml', '.yml'):
                format = 'yaml'
            elif suffix == '.json':
                format = 'json'
            else:
                format = 'yaml'  # 默认使用 YAML
        
        # 确保目录存在
        path.parent.mkdir(parents=True, exist_ok=True)
        
        # 更新更新时间
        config.updated_at = datetime.now().isoformat()
        
        # 转换为字典
        data = config.to_dict()
        
        # 包装在 strategy 键下
        output = {'strategy': data}
        
        # 写入文件
        with open(path, 'w', encoding='utf-8') as f:
            if format.lower() in ('yaml', 'yml'):
                yaml.dump(output, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
            elif format.lower() == 'json':
                json.dump(output, f, ensure_ascii=False, indent=2)
            else:
                raise ValueError(f"不支持的格式: {format}")
    
    def export_to_dict(self, config: StrategyConfig) -> Dict[str, Any]:
        """
        导出配置为字典
        
        参数：
            config: 策略配置对象
        
        返回：
            Dict[str, Any]: 配置字典
        """
        return {'strategy': config.to_dict()}
    
    def export_to_string(self, config: StrategyConfig, format: str = 'yaml') -> str:
        """
        导出配置为字符串
        
        参数：
            config: 策略配置对象
            format: 格式 ('yaml' 或 'json')
        
        返回：
            str: 配置字符串
        """
        data = self.export_to_dict(config)
        
        if format.lower() in ('yaml', 'yml'):
            return yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        elif format.lower() == 'json':
            return json.dumps(data, ensure_ascii=False, indent=2)
        else:
            raise ValueError(f"不支持的格式: {format}")
    
    def list_configs(self) -> list:
        """
        列出配置目录中的所有配置文件
        
        返回：
            list: 配置文件路径列表
        """
        if not self.config_dir.exists():
            return []
        
        configs = []
        for path in self.config_dir.iterdir():
            if path.suffix.lower() in ('.yaml', '.yml', '.json'):
                configs.append(path.name)
        
        return sorted(configs)


# ==================== 便捷函数 ====================

def load_strategy_config(path: Union[str, Path]) -> StrategyConfig:
    """
    便捷函数：从文件加载策略配置
    
    示例：
        config = load_strategy_config("strategies/configs/dual_ma.yaml")
    """
    loader = StrategyConfigLoader()
    return loader.load(path)


def save_strategy_config(
    config: StrategyConfig,
    path: Union[str, Path],
    format: Optional[str] = None
):
    """
    便捷函数：保存策略配置到文件
    
    示例：
        save_strategy_config(config, "output.yaml")
    """
    loader = StrategyConfigLoader()
    loader.save(config, path, format)
