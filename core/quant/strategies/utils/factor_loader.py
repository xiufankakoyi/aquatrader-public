"""
因子加载器 - 智能路由：数据库优先，按需计算，结果缓存

核心优势：
1. 自动判断数据库是否已有因子
2. 避免重复计算（如果数据库已有 ma5，直接用）
3. 计算型因子结果缓存，同一回测周期内只计算一次
4. 统一接口，策略代码简洁
"""

from typing import Optional, Any, Dict
import numpy as np
import json
import os
from pathlib import Path


class FactorLoader:
    """
    因子加载器：优先从数据库，按需计算，结果缓存

    使用方式：
        from core.quant.strategies.utils import FactorLoader as FL

        # 在策略的 generate_signals_vectorized 中
        ma5 = FL.get_factor('ma5', strategy_instance)  # 从数据库
        gain_3d = FL.get_factor('gain_3d', strategy_instance, window=3)  # 动态计算（带缓存）
    """

    # 数据库已有的因子映射（从 vectorized_base 实例属性获取）
    DB_FACTORS = {
        # 价格数据
        'close', 'open', 'high', 'low',

        # 成交量数据
        'volume', 'amount', 'volume_ratio', 'turnover_rate',

        # 市值与基本面
        'total_mv', 'float_mv', 'pe', 'pe_ttm', 'pb', 'ps', 'ps_ttm',

        # 技术指标 - 移动平均线
        'ma5', 'ma10', 'ma20', 'ma120', 'ma250',

        # 技术指标 - 动量类
        'rsi_14', 'kdj_k', 'kdj_d', 'kdj_j',
        'macd_dif', 'macd_dea', 'macd_histogram',

        # 技术指标 - 波动率类
        'atr_14', 'boll_upper', 'boll_mid', 'boll_lower',

        # 技术指标 - 偏离率
        'bias_5', 'bias_10', 'bias_20',

        # 股票属性
        'is_st', 'is_kc', 'is_cy', 'days_listed',
        'is_limit_up', 'is_limit_down', 'is_suspended'
    }

    # 因子注册表路径
    _registry_path = Path(__file__).parent / 'factor_registry.json'
    _registry_cache: Optional[Dict] = None

    # 计算型因子缓存：{(strategy_id, factor_name, param_hash): matrix}
    _compute_cache: Dict[str, np.ndarray] = {}

    @classmethod
    def clear_cache(cls):
        """清除计算型因子缓存（在回测开始时调用）"""
        cls._compute_cache.clear()

    @classmethod
    def _get_cache_key(cls, strategy_instance, factor_name: str, params: Dict) -> str:
        """生成缓存键"""
        # 使用策略实例 id 和因子参数生成唯一键
        strategy_id = id(strategy_instance)
        # 将参数转换为可哈希的字符串
        param_str = ','.join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{strategy_id}:{factor_name}:{param_str}"

    @classmethod
    def load_registry(cls) -> Dict:
        """加载因子注册表（延迟加载+缓存）"""
        if cls._registry_cache is not None:
            return cls._registry_cache

        if cls._registry_path.exists():
            with open(cls._registry_path, 'r', encoding='utf-8') as f:
                cls._registry_cache = json.load(f)
        else:
            cls._registry_cache = {}

        return cls._registry_cache

    @classmethod
    def get_factor(
        cls,
        factor_name: str,
        strategy_instance,
        **kwargs
    ) -> Optional[np.ndarray]:
        """
        智能获取因子：
        - 如果数据库有，直接返回实例属性
        - 如果没有，调用计算函数

        参数：
            factor_name: 因子名称，如 'ma5', 'rsi_14', 'gain_3d'
            strategy_instance: 策略实例（必须是 VectorizedStrategyBase 子类）
            **kwargs: 计算参数（仅在动态计算时使用）

        返回：
            np.ndarray: (T, N) 形状的因子矩阵，或 None

        示例：
            ma5 = FactorLoader.get_factor('ma5', self)
            gain_3d = FactorLoader.get_factor('gain_3d', self, window=3)
        """
        # 1. 优先从数据库（检查实例属性）
        if factor_name in cls.DB_FACTORS:
            factor_value = getattr(strategy_instance, factor_name, None)
            if factor_value is not None:
                return factor_value
            else:
                # 数据库应该有但实例没有，说明 prepare_data 未执行或数据缺失
                print(f"[FactorLoader] [WARN] {factor_name} 应在数据库中，但实例属性为空")
                return None

        # 2. 查询注册表，确定计算函数
        registry = cls.load_registry()
        factor_config = registry.get(factor_name)

        if factor_config is None:
            raise ValueError(
                f"因子 '{factor_name}' 未注册。\n"
                f"请在 {cls._registry_path} 中添加配置，或检查拼写是否正确。"
            )

        # 3. 根据配置调用计算函数
        source = factor_config.get('source', 'compute')

        if source == 'database':
            # 如果配置为 database 但走到这里，说明数据缺失
            print(f"[FactorLoader] [WARN] {factor_name} 配置为数据库因子，但未找到数据")
            return None

        elif source == 'compute':
            # 动态计算（带缓存）
            from .factor_compute import FactorCompute

            function_name = factor_config.get('function')
            if not function_name:
                raise ValueError(f"因子 '{factor_name}' 缺少 'function' 配置")

            compute_fn = getattr(FactorCompute, function_name, None)
            if compute_fn is None:
                raise ValueError(f"计算函数 'FactorCompute.{function_name}' 不存在")

            # 合并默认参数和用户参数
            default_params = factor_config.get('params', {})
            params = {**default_params, **kwargs}

            # 检查缓存
            cache_key = cls._get_cache_key(strategy_instance, factor_name, params)
            if cache_key in cls._compute_cache:
                return cls._compute_cache[cache_key]

            # 调用计算函数
            try:
                result = compute_fn(strategy_instance, **params)
                # 存入缓存
                cls._compute_cache[cache_key] = result
                return result
            except Exception as e:
                print(f"[FactorLoader] ❌ 计算 {factor_name} 失败: {e}")
                raise

        else:
            raise ValueError(f"未知的因子来源类型: {source}")

    @classmethod
    def list_available_factors(cls) -> Dict[str, str]:
        """
        列出所有可用因子

        返回：
            Dict: {因子名: 来源类型}
        """
        result = {}

        # 数据库因子
        for name in cls.DB_FACTORS:
            result[name] = 'database'

        # 注册表因子
        registry = cls.load_registry()
        for name, config in registry.items():
            result[name] = config.get('source', 'compute')

        return result
