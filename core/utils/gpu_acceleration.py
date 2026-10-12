"""
GPU 加速工具模块

使用 CUDA 加速技术指标计算和数据处理
支持自动检测 CUDA 可用性，如果不可用则回退到 CPU
"""

import numpy as np
import pandas as pd
from typing import Optional, Union, List
import warnings

# 尝试导入 GPU 库
try:
    import cupy as cp
    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False
    cp = None

try:
    import cudf
    CUDF_AVAILABLE = True
except ImportError:
    CUDF_AVAILABLE = False
    cudf = None

try:
    from numba import cuda, types
    from numba.cuda import is_available as numba_cuda_available
    NUMBA_CUDA_AVAILABLE = numba_cuda_available()
except ImportError:
    NUMBA_CUDA_AVAILABLE = False
    cuda = None

# 全局标志：是否启用 GPU
USE_GPU = False

def check_gpu_availability() -> dict:
    """
    检查 GPU 可用性
    
    Returns:
        dict: {
            'cupy': bool,
            'cudf': bool,
            'numba_cuda': bool,
            'any_available': bool,
            'device_count': int
        }
    """
    result = {
        'cupy': False,
        'cudf': False,
        'numba_cuda': False,
        'any_available': False,
        'device_count': 0,
    }
    
    if CUPY_AVAILABLE:
        try:
            result['cupy'] = True
            result['device_count'] = cp.cuda.runtime.getDeviceCount()
        except Exception:
            pass
    
    if CUDF_AVAILABLE:
        try:
            result['cudf'] = True
            if result['device_count'] == 0:
                result['device_count'] = cudf.utils.utils.get_rmm().get_device_count()
        except Exception:
            pass
    
    if NUMBA_CUDA_AVAILABLE:
        result['numba_cuda'] = True
        if result['device_count'] == 0:
            try:
                result['device_count'] = len(cuda.gpus)
            except Exception:
                pass
    
    result['any_available'] = any([
        result['cupy'],
        result['cudf'],
        result['numba_cuda']
    ])
    
    return result

def enable_gpu(force: bool = False) -> bool:
    """
    启用 GPU 加速
    
    Args:
        force: 如果为 True，即使检测不到 GPU 也启用（用于测试）
    
    Returns:
        bool: 是否成功启用
    """
    global USE_GPU
    gpu_info = check_gpu_availability()
    
    if gpu_info['any_available'] or force:
        USE_GPU = True
        if gpu_info['any_available']:
            print(f"✅ GPU 加速已启用 (设备数量: {gpu_info['device_count']})")
            if gpu_info['cupy']:
                print("  - CuPy: 可用")
            if gpu_info['cudf']:
                print("  - cuDF: 可用")
            if gpu_info['numba_cuda']:
                print("  - Numba CUDA: 可用")
        else:
            print("[WARN] GPU 加速已启用（强制模式，但未检测到 GPU）")
        return True
    else:
        print("[INFO] 未检测到 GPU，使用 CPU 模式")
        USE_GPU = False
        return False

def disable_gpu():
    """禁用 GPU 加速"""
    global USE_GPU
    USE_GPU = False
    print("GPU 加速已禁用")

def is_gpu_enabled() -> bool:
    """检查是否启用了 GPU"""
    return USE_GPU and check_gpu_availability()['any_available']


def rolling_mean_gpu(data: Union[np.ndarray, 'cp.ndarray'], window: int, min_periods: int = None) -> Union[np.ndarray, 'cp.ndarray']:
    """
    使用 GPU 加速的滚动均值计算
    
    Args:
        data: 输入数组（CPU 或 GPU）
        window: 窗口大小
        min_periods: 最小周期数
    
    Returns:
        滚动均值数组
    """
    if not is_gpu_enabled() or not CUPY_AVAILABLE:
        # 回退到 CPU
        if isinstance(data, pd.Series):
            return data.rolling(window=window, min_periods=min_periods).mean().values
        return np.convolve(data, np.ones(window)/window, mode='valid')
    
    # 使用 GPU
    if not isinstance(data, cp.ndarray):
        data_gpu = cp.asarray(data)
    else:
        data_gpu = data
    
    # CuPy 的滚动窗口计算
    result = cp.zeros_like(data_gpu)
    for i in range(len(data_gpu)):
        start = max(0, i - window + 1)
        end = i + 1
        if end - start >= (min_periods or window):
            result[i] = cp.mean(data_gpu[start:end])
        else:
            result[i] = cp.nan
    
    # 转换回 CPU（如果需要）
    if isinstance(data, np.ndarray):
        return cp.asnumpy(result)
    return result


def calculate_ma_indicators_gpu(
    df: pd.DataFrame,
    columns: List[str],
    windows: List[int],
    group_by: Optional[str] = None
) -> pd.DataFrame:
    """
    使用 GPU 加速计算多个移动平均指标
    
    Args:
        df: 输入 DataFrame
        columns: 要计算的列名列表
        windows: 对应的窗口大小列表
        group_by: 分组列名（如 'stock_code'）
    
    Returns:
        包含计算结果的 DataFrame
    """
    if not is_gpu_enabled() or not CUDF_AVAILABLE:
        # 回退到 CPU pandas
        result_df = df.copy()
        if group_by:
            for col, window in zip(columns, windows):
                result_df[f'ma{window}_{col}'] = result_df.groupby(group_by)[col].transform(
                    lambda x: x.rolling(window=window, min_periods=1).mean()
                )
        else:
            for col, window in zip(columns, windows):
                result_df[f'ma{window}_{col}'] = result_df[col].rolling(window=window, min_periods=1).mean()
        return result_df
    
    # 使用 GPU cuDF
    try:
        gdf = cudf.from_pandas(df)
        
        if group_by:
            for col, window in zip(columns, windows):
                gdf[f'ma{window}_{col}'] = gdf.groupby(group_by)[col].rolling(window=window, min_periods=1).mean().reset_index(level=0, drop=True)
        else:
            for col, window in zip(columns, windows):
                gdf[f'ma{window}_{col}'] = gdf[col].rolling(window=window, min_periods=1).mean()
        
        return gdf.to_pandas()
    except Exception as e:
        warnings.warn(f"GPU 计算失败，回退到 CPU: {e}")
        # 回退到 CPU（递归调用会无限循环，直接使用 CPU 逻辑）
        result_df = df.copy()
        if group_by:
            for col, window in zip(columns, windows):
                result_df[f'ma{window}_{col}'] = result_df.groupby(group_by)[col].transform(
                    lambda x: x.rolling(window=window, min_periods=1).mean()
                )
        else:
            for col, window in zip(columns, windows):
                result_df[f'ma{window}_{col}'] = result_df[col].rolling(window=window, min_periods=1).mean()
        return result_df


# Numba CUDA 内核函数（条件编译）
if NUMBA_CUDA_AVAILABLE:
    @cuda.jit
    def rolling_mean_cuda_kernel(data, result, window):
        """Numba CUDA 内核：计算滚动均值"""
        idx = cuda.grid(1)
        n = data.shape[0]
        
        if idx < n:
            start = max(0, idx - window + 1)
            end = idx + 1
            total = 0.0
            count = 0
            for i in range(start, end):
                total += data[i]
                count += 1
            if count > 0:
                result[idx] = total / count
            else:
                result[idx] = 0.0
else:
    def rolling_mean_cuda_kernel(data, result, window):
        """占位函数（CUDA 不可用时）"""
        pass


def rolling_mean_numba_cuda(data: np.ndarray, window: int) -> np.ndarray:
    """
    使用 Numba CUDA 计算滚动均值
    
    Args:
        data: 输入数组
        window: 窗口大小
    
    Returns:
        滚动均值数组
    """
    if not is_gpu_enabled() or not NUMBA_CUDA_AVAILABLE:
        # 回退到 CPU
        return pd.Series(data).rolling(window=window, min_periods=1).mean().values
    
    # 传输到 GPU
    data_gpu = cuda.to_device(data)
    result_gpu = cuda.device_array_like(data_gpu)
    
    # 配置线程块
    threads_per_block = 256
    blocks_per_grid = (len(data) + threads_per_block - 1) // threads_per_block
    
    # 启动内核
    rolling_mean_cuda_kernel[blocks_per_grid, threads_per_block](data_gpu, result_gpu, window)
    
    # 复制回 CPU
    return result_gpu.copy_to_host()


def batch_calculate_indicators_gpu(
    stock_data_dict: dict,
    indicators: List[dict]
) -> dict:
    """
    批量计算技术指标（GPU 加速）
    
    Args:
        stock_data_dict: {stock_code: DataFrame} 格式的字典
        indicators: 指标配置列表，每个元素为 {
            'name': str,
            'column': str,
            'window': int,
            'type': 'ma' | 'ema' | 'std'
        }
    
    Returns:
        更新后的 stock_data_dict
    """
    if not is_gpu_enabled():
        # 回退到 CPU
        for code, df in stock_data_dict.items():
            for indicator in indicators:
                col = indicator['column']
                window = indicator['window']
                if indicator['type'] == 'ma':
                    df[f"ma{window}_{col}"] = df[col].rolling(window=window, min_periods=1).mean()
                elif indicator['type'] == 'ema':
                    df[f"ema{window}_{col}"] = df[col].ewm(span=window, adjust=False).mean()
                elif indicator['type'] == 'std':
                    df[f"std{window}_{col}"] = df[col].rolling(window=window, min_periods=1).std()
        return stock_data_dict
    
    # 使用 GPU 批量处理
    if CUDF_AVAILABLE:
        try:
            # 将所有数据合并到一个 GPU DataFrame
            all_dfs = []
            for code, df in stock_data_dict.items():
                df_copy = df.copy()
                df_copy['_stock_code'] = code
                all_dfs.append(df_copy)
            
            combined_df = pd.concat(all_dfs, ignore_index=True)
            gdf = cudf.from_pandas(combined_df)
            
            # 按股票代码分组计算
            for indicator in indicators:
                col = indicator['column']
                window = indicator['window']
                if indicator['type'] == 'ma':
                    gdf[f"ma{window}_{col}"] = gdf.groupby('_stock_code')[col].transform(
                        lambda x: x.rolling(window=window, min_periods=1).mean()
                    )
                elif indicator['type'] == 'ema':
                    gdf[f"ema{window}_{col}"] = gdf.groupby('_stock_code')[col].transform(
                        lambda x: x.ewm(span=window, adjust=False).mean()
                    )
                elif indicator['type'] == 'std':
                    gdf[f"std{window}_{col}"] = gdf.groupby('_stock_code')[col].transform(
                        lambda x: x.rolling(window=window, min_periods=1).std()
                    )
            
            # 转换回 pandas 并拆分
            combined_df = gdf.to_pandas()
            for code in stock_data_dict.keys():
                mask = combined_df['_stock_code'] == code
                stock_data_dict[code] = combined_df[mask].drop(columns=['_stock_code'])
            
            return stock_data_dict
        except Exception as e:
            warnings.warn(f"GPU 批量计算失败，回退到 CPU: {e}")
            return batch_calculate_indicators_gpu(stock_data_dict, indicators)
    
    return stock_data_dict


# 初始化时检查 GPU
if __name__ == "__main__":
    gpu_info = check_gpu_availability()
    print("GPU 可用性检查:")
    print(f"  CuPy: {gpu_info['cupy']}")
    print(f"  cuDF: {gpu_info['cudf']}")
    print(f"  Numba CUDA: {gpu_info['numba_cuda']}")
    print(f"  设备数量: {gpu_info['device_count']}")
    print(f"  任何可用: {gpu_info['any_available']}")

