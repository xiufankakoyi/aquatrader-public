"""
因子库使用示例

演示如何在策略中使用 FactorLoader 简化代码
"""

import numpy as np
from core.quant.strategies.vectorized_base import VectorizedStrategyBase
from core.quant.strategies.utils import FactorLoader as FL


class ExampleFactorStrategy(VectorizedStrategyBase):
    """示例策略：展示因子库的简洁用法"""

    strategy_id = "example_factor"
    strategy_name = "因子库示例策略"

    def generate_signals_vectorized(self, price_matrix, trading_dates, stock_codes, data_query, preloaded_data=None):
        """
        使用因子库重写的策略逻辑 - 代码量减少 80%+
        """
        # 1. 准备数据（基类方法）
        self.prepare_data(preloaded_data, trading_dates, stock_codes, price_matrix)

        T, N = len(trading_dates), len(stock_codes)

        # ==================================================================
        # 2. 一行代码获取因子（自动判断数据库/计算）
        # ==================================================================

        # 从数据库直接获取（无需计算）
        ma5 = FL.get_factor('ma5', self)           # 5日均线
        ma20 = FL.get_factor('ma20', self)         # 20日均线
        rsi = FL.get_factor('rsi_14', self)        # RSI指标
        volume_ratio = FL.get_factor('volume_ratio', self)  # 量比

        # 动态计算（按需）
        gain_3d = FL.get_factor('gain_3d', self)   # 3日涨幅
        gain_5d = FL.get_factor('gain_5d', self)   # 5日涨幅
        volatility = FL.get_factor('volatility_20', self)  # 20日波动率
        sharpe = FL.get_factor('sharpe_20', self)  # 20日夏普率

        # 自定义参数（覆盖默认值）
        gain_custom = FL.get_factor('gain_3d', self, window=7)  # 7日涨幅

        # ==================================================================
        # 3. 纯粹的交易逻辑（极简）
        # ==================================================================

        # 买入条件：
        # - 短期均线上穿长期均线（金叉）
        # - RSI < 30（超卖）
        # - 3日涨幅 > 2%
        # - 量比 > 2
        # - 市值在20-60亿之间
        buy_condition = (
            (self.close > ma5) &
            (ma5 > ma20) &
            (rsi < 30) &
            (gain_3d > 2.0) &
            (volume_ratio > 2.0) &
            (self.total_mv >= 20 * 10000) &
            (self.total_mv <= 60 * 10000) &
            (self.days_listed >= 60) &
            (self.is_st == 0)
        )

        # 卖出条件：
        # - 跌破5日均线
        # - 或RSI > 70（超买）
        # - 或5日涨幅 < -10%（止损）
        sell_condition = (
            (self.close < ma5) |
            (rsi > 70) |
            (gain_5d < -10.0)
        )

        # ==================================================================
        # 4. 生成信号矩阵
        # ==================================================================
        signal_matrix = np.zeros((T, N), dtype=np.int32)
        signal_matrix[buy_condition] = 1
        signal_matrix[sell_condition] = 2  # 卖出优先级更高

        # T+1 逻辑
        signal_matrix[1:] = signal_matrix[:-1]

        return signal_matrix


# ==================================================================
# 对比：传统写法 vs 因子库写法
# ==================================================================

def traditional_way():
    """传统写法（需要自己计算每个因子）"""
    # 需要手动实现 MA5, MA20, RSI, 涨幅, 波动率等计算
    # 代码量 ~300 行

    # 计算 MA5
    ma5 = np.full((T, N), np.nan)
    for t in range(5, T):
        for n in range(N):
            ma5[t, n] = np.mean(close[t-5:t, n])

    # 计算 MA20
    ma20 = np.full((T, N), np.nan)
    for t in range(20, T):
        for n in range(N):
            ma20[t, n] = np.mean(close[t-20:t, n])

    # ... 重复 10+ 次类似代码
    pass


def factor_library_way():
    """因子库写法（1行代码）"""
    # 代码量 ~30 行

    ma5 = FL.get_factor('ma5', self)
    ma20 = FL.get_factor('ma20', self)
    rsi = FL.get_factor('rsi_14', self)
    gain_3d = FL.get_factor('gain_3d', self)

    # 直接写交易逻辑
    buy = (close > ma5) & (rsi < 30) & (gain_3d > 2)


if __name__ == '__main__':
    print("=" * 60)
    print("因子库使用示例")
    print("=" * 60)

    # 列出所有可用因子
    from core.quant.strategies.utils import FactorLoader

    print("\n📊 所有可用因子：\n")
    factors = FactorLoader.list_available_factors()

    db_factors = [k for k, v in factors.items() if v == 'database']
    compute_factors = [k for k, v in factors.items() if v == 'compute']

    print(f"✅ 数据库因子（{len(db_factors)} 个）：")
    for name in sorted(db_factors)[:10]:
        print(f"   - {name}")
    if len(db_factors) > 10:
        print(f"   ... 还有 {len(db_factors) - 10} 个")

    print(f"\n🔧 计算因子（{len(compute_factors)} 个）：")
    for name in sorted(compute_factors):
        print(f"   - {name}")

    print("\n" + "=" * 60)
    print("代码对比：传统写法 vs 因子库")
    print("=" * 60)
    print("传统写法：~300 行代码，需要手动实现所有计算")
    print("因子库写法：~30 行代码，一行获取一个因子")
    print("代码减少：90%+")
    print("=" * 60)
