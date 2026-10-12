# utils/strategy_preparation.py
"""
策略执行准备模块 - 实现后端数据引擎的动态指标注入

核心功能：
1. 询问策略需要什么指标（通过 get_required_indicators 方法）
2. 批量计算指标（利用 GPU/Vectorized 计算器）
3. 将指标注入到数据引擎中（通过缓存机制）

设计理念：
- 在回测循环前一次性计算所有指标，避免在循环中重复计算
- 利用向量化计算，性能优异
- 通过数据引擎的缓存机制注入指标，对上层透明
"""

import pandas as pd
from typing import Dict, List, Optional, Any
from config.logger import get_logger


def prepare_strategy_execution(
    strategy,
    raw_data_engine,
    start_date: str,
    end_date: str,
    logger=None
) -> Optional[Any]:
    """
    在开始回测前执行策略准备，动态计算指标并注入到数据中
    
    参数:
        strategy: 策略实例，需要实现 get_required_indicators 方法（可选）
        raw_data_engine: 原始数据引擎（如 OptimizedStockDataQuery）
        start_date: 回测开始日期
        end_date: 回测结束日期
        logger: 日志记录器（可选）
    
    返回:
        enhanced_data_engine: 增强的数据引擎（如果成功），否则返回 None
    
    伪代码流程:
        1. 询问策略：你需要什么指标？
        2. 调用计算器批量计算（利用 GPU/Vectorized）
        3. 数据融合（Merge）- 将算好的指标拼接到原始行情宽表中
        4. 通过缓存机制注入到数据引擎
    """
    if logger is None:
        logger = get_logger(__name__)
    
    # 1. 询问策略：你需要什么指标？
    if hasattr(strategy, 'get_required_indicators'):
        try:
            requirements = strategy.get_required_indicators()
            if not isinstance(requirements, list):
                logger.warning(f"策略 {strategy.name} 的 get_required_indicators 返回了非列表类型，忽略")
                requirements = []
        except Exception as e:
            logger.warning(f"调用策略 {strategy.name} 的 get_required_indicators 失败: {e}")
            requirements = []
    else:
        requirements = []
        logger.debug(f"策略 {strategy.name} 未实现 get_required_indicators，跳过指标计算")
    
    # 如果没有指标需求，直接返回
    if not requirements:
        logger.debug("策略无需额外指标，跳过指标计算")
        return None
    
    logger.info(f"策略 {strategy.name} 需要 {len(requirements)} 个指标: {[r.get('name', r.get('type', 'unknown')) for r in requirements]}")
    
    # 2. 获取原始数据（一次性加载整个回测期间的数据）
    try:
        if hasattr(raw_data_engine, 'get_all_daily_data_for_period'):
            raw_data = raw_data_engine.get_all_daily_data_for_period(
                start_date=start_date,
                end_date=end_date
            )
        else:
            logger.warning("数据引擎不支持 get_all_daily_data_for_period，无法进行指标计算")
            return None
        
        if raw_data is None or raw_data.empty:
            logger.warning("获取原始数据为空，跳过指标计算")
            return None
        
        logger.debug(f"获取原始数据: {len(raw_data)} 行, {len(raw_data.columns)} 列")
    except Exception as e:
        logger.error(f"获取原始数据失败: {e}", exc_info=True)
        return None
    
    # 3. 调用计算器批量计算（利用 GPU/Vectorized）
    try:
        from core.utils.indicator_calculator import IndicatorCalculator
        
        calculator = IndicatorCalculator(enable_cache=True)
        
        # 按股票分组计算指标（确保每只股票的指标独立计算）
        logger.info(f"开始批量计算 {len(requirements)} 个指标...")
        enhanced_data = calculator.calculate_batch(
            df=raw_data,
            indicators=requirements,
            group_by='stock_code'  # 按股票代码分组，确保每只股票的指标独立计算
        )
        
        logger.info(f"指标计算完成: 原始数据 {len(raw_data)} 行, 增强后 {len(enhanced_data)} 行")
        logger.debug(f"新增列: {set(enhanced_data.columns) - set(raw_data.columns)}")
        
    except Exception as e:
        logger.error(f"指标计算失败: {e}", exc_info=True)
        return None
    
    # 4. 数据融合（Merge）- 将算好的指标注入到数据引擎的缓存中
    try:
        # 通过数据引擎的缓存机制注入指标数据
        # 这样后续查询时，数据引擎会自动返回包含指标的数据
        if hasattr(raw_data_engine, '_cache'):
            # 构建缓存键（与数据引擎的缓存键格式一致）
            cache_key = f"all_data_{start_date}_{end_date}"
            
            # 更新缓存，注入指标数据
            raw_data_engine._cache[cache_key] = enhanced_data
            
            logger.info(f"指标已注入到数据引擎缓存: {cache_key}")
            
            # 同时，如果数据引擎有预加载数据，也需要更新
            if hasattr(raw_data_engine, '_preloaded_data') and raw_data_engine._preloaded_data is not None:
                # 更新预加载数据（按日期分组的数据）
                # 注意：这里需要将增强后的数据按日期拆分回原来的格式
                if isinstance(raw_data_engine._preloaded_data, dict):
                    # 按日期分组更新
                    for date, df in raw_data_engine._preloaded_data.items():
                        if df is not None and not df.empty:
                            # 获取该日期的增强数据
                            date_enhanced = enhanced_data[enhanced_data['trade_date'] == date]
                            if not date_enhanced.empty:
                                # 合并指标列
                                indicator_cols = set(enhanced_data.columns) - set(raw_data.columns)
                                if indicator_cols:
                                    # 使用 merge 合并指标
                                    df_merged = df.merge(
                                        date_enhanced[['stock_code', 'trade_date'] + list(indicator_cols)],
                                        on=['stock_code', 'trade_date'],
                                        how='left'
                                    )
                                    raw_data_engine._preloaded_data[date] = df_merged
                    
                    logger.info("预加载数据已更新，包含新计算的指标")
            
            return raw_data_engine
        else:
            logger.warning("数据引擎不支持缓存机制，无法注入指标")
            return None
            
    except Exception as e:
        logger.error(f"指标注入失败: {e}", exc_info=True)
        return None

