# core/gpu_engine.py
"""
统一高性能回测引擎 - Polars + CuPy + Numba 架构

目标：亚毫秒级计算，支持分钟级回测

架构：
1. Polars: 快速数据加载，立即转换为 NumPy/CuPy 矩阵
2. CuPy: GPU 上一次性计算所有技术指标
3. Numba: JIT 编译的交易循环，纯数组操作，无字典

关键优化：
- 信号预映射到 NumPy 数组（循环中无字典）
- 批量指标计算（GPU 并行）
- 编译后的循环（Numba JIT）
"""
import numpy as np
try:
    import cupy as cp
    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False
    cp = None

try:
    import polars as pl
    POLARS_AVAILABLE = True
except ImportError:
    POLARS_AVAILABLE = False
    pl = None

try:
    from numba import jit
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    jit = lambda *args, **kwargs: lambda f: f

from typing import Dict, Any, Tuple, List, Optional
import time
import os
import pandas as pd


class GPUEngine:
    """统一高性能回测引擎"""
    
    def __init__(self, data_query, initial_capital=1_000_000, commission_rate=0.0005):
        self.data_query = data_query
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.min_commission = 5.0
        self.sell_tax = 0.001
        self.board_lot = 100
        
        if not POLARS_AVAILABLE:
            raise ImportError("Polars is required: pip install polars")
        if not NUMBA_AVAILABLE:
            raise ImportError("Numba is required: pip install numba")
    
    def load_data_polars(self, start_date: str, end_date: str, 
                        required_warmup: int = 60,
                        batch_size: int = 30) -> Tuple[np.ndarray, List[str], List[str]]:
        """
        使用 Polars Lazy API 分批加载数据并转换为 NumPy 矩阵（内存安全）
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            required_warmup: 预热天数
            batch_size: 批处理大小（天数），避免一次性加载所有数据
        
        Returns:
            (price_matrix, stock_codes, trading_dates)
            price_matrix: (T, N, 4) float32 - [open, high, low, close]
        """
        from config.logger import get_logger
        logger = get_logger(__name__)
        
        # 计算加载范围
        load_start = pd.to_datetime(start_date) - pd.Timedelta(days=required_warmup)
        load_start_str = load_start.strftime('%Y-%m-%d')
        
        # 获取交易日期
        trading_dates = self.data_query.get_trading_dates(start_date, end_date)
        all_dates = self.data_query.get_trading_dates(load_start_str, end_date)
        
        logger.info(f"加载数据: {load_start_str} 到 {end_date} (批大小: {batch_size} 天)")
        
        # 检查是否使用 LanceDB（支持 Lazy API）
        use_lancedb = hasattr(self.data_query, '_use_lancedb') and self.data_query._use_lancedb
        
        if use_lancedb and hasattr(self.data_query, 'lance_manager'):
            # 使用 LanceDB Lazy API（最优）
            return self._load_data_from_lancedb_lazy(
                start_date=load_start_str,
                end_date=end_date,
                trading_dates=trading_dates,
                batch_size=batch_size
            )
        else:
            # 回退：分批加载（避免一次性加载所有数据）
            return self._load_data_batched(
                all_dates=all_dates,
                trading_dates=trading_dates,
                batch_size=batch_size
            )
    
    def _load_data_from_lancedb_lazy(self,
                                     start_date: str,
                                     end_date: str,
                                     trading_dates: List[str],
                                     batch_size: int) -> Tuple[np.ndarray, List[str], List[str]]:
        """
        从 LanceDB 使用 Lazy API 加载数据
        """
        from config.logger import get_logger
        logger = get_logger(__name__)
        
        # 使用 Lazy API 一次性查询整个日期范围
        lazy_df = self.data_query.lance_manager.load_to_polars_lazy(
            start_date=start_date,
            end_date=end_date,
            columns=['stock_code', 'trade_date', 'open', 'high', 'low', 'close']
        )
        
        # 执行查询（此时才真正加载数据，但已经过优化）
        df = lazy_df.collect()
        
        if df.is_empty():
            raise ValueError("No data loaded from LanceDB")
        
        # 获取唯一代码和日期
        stock_codes = sorted(df['stock_code'].unique().to_list())
        dates = sorted(df['trade_date'].unique().to_list())
        
        # 只保留交易日期
        dates = [d for d in dates if d in trading_dates]
        dates = sorted(dates)
        
        T, N = len(dates), len(stock_codes)
        price_matrix = np.full((T, N, 4), np.nan, dtype=np.float32)
        
        # 使用 Polars 的 pivot（比 Pandas 更快，内存更安全）
        for col_idx, col_name in enumerate(['open', 'high', 'low', 'close']):
            if col_name in df.columns:
                # 使用 Polars pivot（延迟执行）
                pivot_lazy = (
                    df.lazy()
                    .select(['trade_date', 'stock_code', col_name])
                    .pivot(
                        index='trade_date',
                        columns='stock_code',
                        values=col_name,
                        aggregate_function='first'
                    )
                )
                pivot_df = pivot_lazy.collect().to_pandas()
                
                # 填充矩阵
                for t, date in enumerate(dates):
                    if date in pivot_df.index:
                        for n, code in enumerate(stock_codes):
                            if code in pivot_df.columns:
                                val = pivot_df.loc[date, code]
                                if pd.notna(val):
                                    price_matrix[t, n, col_idx] = float(val)
        
        logger.info(f"数据加载完成 (Lazy): {T} 交易日, {N} 股票")
        return price_matrix, stock_codes, dates
    
    def _load_data_batched(self,
                          all_dates: List[str],
                          trading_dates: List[str],
                          batch_size: int) -> Tuple[np.ndarray, List[str], List[str]]:
        """
        分批加载数据（避免一次性加载所有数据到内存）
        """
        from config.logger import get_logger
        logger = get_logger(__name__)
        
        # 分批处理日期
        all_batches = []
        for i in range(0, len(all_dates), batch_size):
            batch_dates = all_dates[i:i+batch_size]
            logger.debug(f"加载批次 {i//batch_size + 1}: {batch_dates[0]} ~ {batch_dates[-1]}")
            
            # 加载这一批数据
            batch_data = []
            for date in batch_dates:
                df_pd = self.data_query.get_stock_pool(date)
                if df_pd is not None and not df_pd.empty:
                    df_pd['trade_date'] = date
                    batch_data.append(df_pd)
            
            if batch_data:
                # 转换为 Polars LazyFrame（延迟执行）
                df_pd_batch = pd.concat(batch_data, ignore_index=True)
                df_batch = pl.from_pandas(df_pd_batch).lazy()
                all_batches.append(df_batch)
        
        if not all_batches:
            raise ValueError("No data loaded")
        
        # 合并所有批次（延迟执行）
        # 注意：concat 在 LazyFrame 上也是延迟的
        if len(all_batches) == 1:
            df_lazy = all_batches[0]
        else:
            df_lazy = pl.concat(all_batches)
        
        # 执行查询（此时才真正加载所有数据）
        df = df_lazy.collect()
        
        # 获取唯一代码和日期
        stock_codes = sorted(df['stock_code'].unique().to_list())
        dates = sorted(df['trade_date'].unique().to_list())
        
        # 只保留交易日期
        dates = [d for d in dates if d in trading_dates]
        dates = sorted(dates)
        
        T, N = len(dates), len(stock_codes)
        price_matrix = np.full((T, N, 4), np.nan, dtype=np.float32)
        
        # 使用 Polars pivot（内存安全）
        for col_idx, col_name in enumerate(['open', 'high', 'low', 'close']):
            if col_name in df.columns:
                pivot_lazy = (
                    df.lazy()
                    .select(['trade_date', 'stock_code', col_name])
                    .pivot(
                        index='trade_date',
                        columns='stock_code',
                        values=col_name,
                        aggregate_function='first'
                    )
                )
                pivot_df = pivot_lazy.collect().to_pandas()
                
                for t, date in enumerate(dates):
                    if date in pivot_df.index:
                        for n, code in enumerate(stock_codes):
                            if code in pivot_df.columns:
                                val = pivot_df.loc[date, code]
                                if pd.notna(val):
                                    price_matrix[t, n, col_idx] = float(val)
        
        logger.info(f"数据加载完成 (Batched): {T} 交易日, {N} 股票")
        return price_matrix, stock_codes, dates
    
    def calculate_indicators_gpu(self, price_matrix: np.ndarray) -> np.ndarray:
        """
        在 GPU 上一次性计算所有技术指标
        
        Args:
            price_matrix: (T, N, 4) - [open, high, low, close]
            
        Returns:
            indicator_matrix: (T, N, K) - K 个指标
        """
        if not CUPY_AVAILABLE:
            return self._calculate_indicators_cpu(price_matrix)
        
        # 移动到 GPU
        price_gpu = cp.asarray(price_matrix)
        T, N, _ = price_gpu.shape
        close = price_gpu[:, :, 3]
        high = price_gpu[:, :, 1]
        low = price_gpu[:, :, 2]
        volume = None  # 如果有成交量数据
        
        indicators = []
        
        # MA5, MA10, MA20, MA60
        for window in [5, 10, 20, 60]:
            ma = cp.full_like(close, cp.nan)
            for t in range(window-1, T):
                ma[t, :] = cp.mean(close[t-window+1:t+1, :], axis=0)
            indicators.append(ma)
        
        # RSI (14 period)
        rsi = cp.full_like(close, cp.nan)
        for t in range(14, T):
            delta = close[t-13:t+1, :] - close[t-14:t, :]
            gain = cp.where(delta > 0, delta, 0)
            loss = cp.where(delta < 0, -delta, 0)
            avg_gain = cp.mean(gain, axis=0)
            avg_loss = cp.mean(loss, axis=0)
            rs = cp.where(avg_loss > 0, avg_gain / avg_loss, 0)
            rsi[t, :] = 100 - (100 / (1 + rs))
        indicators.append(rsi)
        
        # 成交量比率（如果有成交量）
        # if volume is not None:
        #     volume_ratio = ...
        #     indicators.append(volume_ratio)
        
        # 堆叠所有指标
        indicator_matrix = cp.stack(indicators, axis=2)  # (T, N, K)
        
        # 移回 CPU
        return cp.asnumpy(indicator_matrix)
    
    def _calculate_indicators_cpu(self, price_matrix: np.ndarray) -> np.ndarray:
        """CPU 回退版本"""
        T, N, _ = price_matrix.shape
        close = price_matrix[:, :, 3]
        
        indicators = []
        for window in [5, 10, 20, 60]:
            ma = np.full_like(close, np.nan)
            for t in range(window-1, T):
                ma[t, :] = np.mean(close[t-window+1:t+1, :], axis=0)
            indicators.append(ma)
        
        return np.stack(indicators, axis=2)
    
    @staticmethod
    @jit(nopython=True, cache=True)
    def _fast_trading_loop(
        price_matrix: np.ndarray,      # (T, N, 4) - [open, high, low, close]
        signal_matrix: np.ndarray,     # (T, N) - 0=hold, 1=buy, 2=sell
        initial_cash: float,
        commission_rate: float,
        min_commission: float,
        sell_tax: float,
        board_lot: int,
        max_positions: int,
        position_ratio: float
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        """
        Numba JIT 编译的极速交易循环
        
        关键：只使用 NumPy 数组，无字典，无 Python 对象
        
        Returns:
            (cash_history, portfolio_value_history, trades, trade_count)
        """
        T, N, _ = price_matrix.shape
        
        # 初始化
        cash = initial_cash
        positions = np.zeros(N, dtype=np.float32)      # 持仓股数
        entry_prices = np.zeros(N, dtype=np.float32)   # 买入价格
        
        # 历史记录
        cash_history = np.zeros(T, dtype=np.float32)
        portfolio_value_history = np.zeros(T, dtype=np.float32)
        
        # 交易记录（预分配）
        max_trades = T * N
        trades = np.full((max_trades, 5), -1.0, dtype=np.float32)
        trade_count = 0
        
        for t in range(T):
            opens = price_matrix[t, :, 0]
            closes = price_matrix[t, :, 3]
            
            # === 处理卖出 ===
            for n in range(N):
                if signal_matrix[t, n] == 2 and positions[n] > 0:  # sell
                    shares = int(positions[n])
                    shares = (shares // board_lot) * board_lot
                    if shares < board_lot:
                        continue
                    
                    price = opens[n] if not np.isnan(opens[n]) else closes[n]
                    if np.isnan(price) or price <= 0:
                        continue
                    
                    revenue = shares * price
                    commission = max(revenue * commission_rate, min_commission)
                    tax = revenue * sell_tax
                    net_revenue = revenue - commission - tax
                    
                    cash += net_revenue
                    positions[n] -= shares
                    if positions[n] < 1e-6:
                        positions[n] = 0.0
                        entry_prices[n] = 0.0
                    
                    if trade_count < max_trades:
                        trades[trade_count, 0] = float(t)
                        trades[trade_count, 1] = float(n)
                        trades[trade_count, 2] = 2.0  # sell
                        trades[trade_count, 3] = price
                        trades[trade_count, 4] = float(shares)
                        trade_count += 1
            
            # === 处理买入 ===
            buy_signals = []
            for n in range(N):
                if signal_matrix[t, n] == 1 and positions[n] == 0:  # buy
                    buy_signals.append(n)
            
            # 限制持仓数
            if max_positions > 0:
                current_pos = 0
                for n in range(N):
                    if positions[n] > 0:
                        current_pos += 1
                buy_allowance = max_positions - current_pos
                if buy_allowance <= 0:
                    buy_signals = []
                elif len(buy_signals) > buy_allowance:
                    buy_signals = buy_signals[:buy_allowance]
            
            # 执行买入
            if buy_signals:
                # 计算总资产
                total_equity = cash
                for n in range(N):
                    if positions[n] > 0:
                        price = closes[n] if not np.isnan(closes[n]) else opens[n]
                        if not np.isnan(price) and price > 0:
                            total_equity += positions[n] * price
                
                # 计算目标投资额
                if position_ratio > 0:
                    target_per_stock = total_equity * position_ratio
                else:
                    target_per_stock = cash / len(buy_signals) if buy_signals else 0
                
                for n in buy_signals:
                    price = opens[n] if not np.isnan(opens[n]) else closes[n]
                    if np.isnan(price) or price <= 0:
                        continue
                    
                    investment = min(target_per_stock, cash)
                    if investment <= 0:
                        continue
                    
                    est_rate = max(commission_rate, min_commission / (investment + 1))
                    price_with_rate = price * (1 + est_rate)
                    max_shares = int(investment / price_with_rate)
                    shares = (max_shares // board_lot) * board_lot
                    
                    if shares < board_lot:
                        continue
                    
                    commission = max(shares * price * commission_rate, min_commission)
                    cost = shares * price + commission
                    
                    if cost > cash:
                        continue
                    
                    cash -= cost
                    positions[n] = float(shares)
                    entry_prices[n] = price
                    
                    if trade_count < max_trades:
                        trades[trade_count, 0] = float(t)
                        trades[trade_count, 1] = float(n)
                        trades[trade_count, 2] = 1.0  # buy
                        trades[trade_count, 3] = price
                        trades[trade_count, 4] = float(shares)
                        trade_count += 1
            
            # === 计算市值 ===
            portfolio_value = cash
            for n in range(N):
                if positions[n] > 0:
                    price = closes[n] if not np.isnan(closes[n]) else opens[n]
                    if not np.isnan(price) and price > 0:
                        portfolio_value += positions[n] * price
            
            cash_history[t] = cash
            portfolio_value_history[t] = portfolio_value
        
        return cash_history, portfolio_value_history, trades, trade_count
    
    def run_backtest(self, start_date: str, end_date: str, strategy,
                    signal_mapper: callable) -> Dict[str, Any]:
        """
        执行回测
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            strategy: 策略对象
            signal_mapper: 函数，将策略信号转换为信号矩阵
                          (signals_dict, stock_codes, date) -> signal_array (N,)
        
        Returns:
            回测结果字典
        """
        from config.logger import get_logger
        logger = get_logger(__name__)
        
        # 1. 加载数据
        price_matrix, stock_codes, trading_dates = self.load_data_polars(
            start_date, end_date, getattr(strategy, 'warmup_days', 60)
        )
        
        # 找到回测开始索引
        try:
            start_idx = trading_dates.index(start_date)
        except ValueError:
            start_idx = 0
        
        price_matrix = price_matrix[start_idx:]
        trading_dates = trading_dates[start_idx:]
        T, N = len(trading_dates), len(stock_codes)
        
        # 2. 计算指标（GPU）
        logger.info("计算技术指标（GPU）...")
        indicator_matrix = self.calculate_indicators_gpu(price_matrix)
        
        # 3. 生成信号矩阵（预映射）
        logger.info("生成信号矩阵...")
        signal_matrix = np.zeros((T, N), dtype=np.int32)
        code_to_idx = {code: idx for idx, code in enumerate(stock_codes)}
        
        for t, date in enumerate(trading_dates):
            stock_pool = self.data_query.get_stock_pool(date)
            if stock_pool is None or stock_pool.empty:
                continue
            
            signals = strategy.generate_signals(date, stock_pool, self.data_query)
            signal_array = signal_mapper(signals, stock_codes, date)
            signal_matrix[t, :] = signal_array
        
        # 4. 执行快速循环（Numba）
        logger.info("执行交易循环（Numba JIT）...")
        max_positions = getattr(strategy, 'max_positions', 3)
        position_ratio = getattr(strategy, 'position_ratio', 0.2)
        
        cash_history, portfolio_value_history, trades, trade_count = self._fast_trading_loop(
            price_matrix,
            signal_matrix,
            float(self.initial_capital),
            float(self.commission_rate),
            float(self.min_commission),
            float(self.sell_tax),
            int(self.board_lot),
            int(max_positions),
            float(position_ratio)
        )
        
        # 5. 转换结果
        idx_to_code = {idx: code for idx, code in enumerate(stock_codes)}
        trades_list = []
        for i in range(int(trade_count)):
            if i >= len(trades):
                break
            t = int(trades[i, 0])
            n = int(trades[i, 1])
            action_code = int(trades[i, 2])
            price = float(trades[i, 3])
            shares = float(trades[i, 4])
            
            if t < 0 or n < 0 or t >= len(trading_dates):
                continue
            
            trades_list.append({
                'date': trading_dates[t],
                'symbol': idx_to_code.get(n, f"UNKNOWN_{n}"),
                'action': 'buy' if action_code == 1 else 'sell',
                'price': price,
                'quantity': shares,
            })
        
        results_list = []
        for t, date in enumerate(trading_dates):
            results_list.append({
                'date': date,
                'total_value': float(portfolio_value_history[t]),
            })
        
        return {
            'trades': trades_list,
            'equity_curve': results_list,
            'final_value': float(portfolio_value_history[-1]),
        }

