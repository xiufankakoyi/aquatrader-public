"""
回测执行层

负责交易执行、分红、订单过滤。

P0-6 修复：fallback 路径委托 SharedExecutionPolicy.evaluate() 统一成交规则；
vectorized 卖出路径补齐 ST 过滤、容量约束和冲击成本。
"""

from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

# 可选依赖导入
try:
    import polars as pl
    POLARS_AVAILABLE = True
except ImportError:
    POLARS_AVAILABLE = False
    pl = None

from config.logger import get_logger
from .models import TradeRecord

logger = get_logger(__name__)


@dataclass
class _Bar:
    """行情数据包装器，供 SharedExecutionPolicy.evaluate() 使用。

    将 data_dict 中的字典数据包装为带属性的对象。
    """

    open: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    is_suspended: bool = False
    is_limit_up: bool = False
    is_limit_down: bool = False
    is_st: bool = False


class ExecutionLayer:
    """
    回测执行层

    负责交易执行、分红、订单过滤。
    """

    def __init__(self, engine):
        """
        初始化执行层。

        Args:
            engine: UnifiedBacktestEngine 实例，作为共享状态容器。
        """
        self.engine = engine

    def __getattr__(self, name):
        """将未在层中定义的属性访问委托给引擎。"""
        return getattr(self.engine, name)

    def __setattr__(self, name, value):
        """将属性赋值委托给引擎，仅保留 engine 引用在层自身。"""
        if name == 'engine':
            super().__setattr__(name, value)
        else:
            setattr(self.engine, name, value)

    def _execute_trades(
        self,
        current_time: pd.Timestamp,
        stock_pool: Any,
        signals: Dict[str, Any],
        portfolio: Dict[str, int],
        cash: float,
        position_info: Dict[str, Dict[str, Any]],
        data_dict: Dict[str, Dict[str, Any]]
    ) -> Tuple[Dict[str, int], float, List[TradeRecord]]:
        """
        执行交易 - 向量化优化版本

        性能优化：
        1. 使用 Polars DataFrame 批量处理信号
        2. 向量化计算交易参数（佣金、税费、盈亏）
        3. 减少循环次数，O(n) -> O(1) 批量操作
        """
        new_portfolio = portfolio.copy()
        new_cash = cash
        trades = []
        date_str = current_time.strftime("%Y-%m-%d")

        if not signals:
            return new_portfolio, new_cash, trades

        # =========================================================================
        # [向量化优化] 将信号和数据转换为 DataFrame 批量处理
        # =========================================================================
        if POLARS_AVAILABLE and len(signals) > 5:
            return self._execute_trades_vectorized(
                current_time, signals, portfolio, cash, position_info, data_dict, date_str
            )

        # 回退到原始逻辑（小规模信号）
        signal_data = {code: data_dict.get(code, {}) for code in signals.keys() if code in data_dict}

        # 阶段1: 处理卖出（P0-6：委托 SharedExecutionPolicy.evaluate() 统一成交规则）
        for code, signal in signals.items():
            sig_type = signal.get('action') if isinstance(signal, dict) else signal
            if sig_type not in ('sell', 'exit'):
                continue

            current_position = new_portfolio.get(code, 0)
            if current_position <= 0:
                continue

            if code not in signal_data:
                continue

            data = signal_data[code]
            bar = _Bar(
                open=float(data.get('open', 0)),
                close=float(data.get('close', 0)),
                volume=float(data.get('volume', 0)),
                is_suspended=bool(data.get('is_suspended', 0)),
                is_limit_up=bool(data.get('is_limit_up', 0)),
                is_limit_down=bool(data.get('is_limit_down', 0)),
                is_st=bool(data.get('is_st', 0)),
            )

            fill = self._shared_policy.evaluate(
                bar, side="sell",
                quantity=current_position,
                average_volume=float(data.get('volume', 0)),
            )

            if not fill.allowed:
                # volume_cap：尝试截断到 max_quantity
                if fill.rejection_reason == "volume_cap" and fill.max_quantity >= 100:
                    sell_qty = (fill.max_quantity // 100) * 100
                    fill = self._shared_policy.evaluate(
                        bar, side="sell",
                        quantity=sell_qty,
                        average_volume=float(data.get('volume', 0)),
                    )
                    if not fill.allowed:
                        self._rejected_stats.volume += 1
                        continue
                else:
                    if fill.rejection_reason == "suspended":
                        self._rejected_stats.suspended += 1
                    elif fill.rejection_reason == "limit_down":
                        self._rejected_stats.limit_down += 1
                    elif fill.rejection_reason == "st_filtered":
                        self._rejected_stats.st += 1
                    elif fill.rejection_reason == "invalid_price":
                        self._rejected_stats.invalid_price += 1
                    else:
                        self._rejected_stats.volume += 1
                    continue
            else:
                sell_qty = current_position

            filled_price = fill.fill_price
            impact_cost = fill.impact_cost
            # 滑点成本统计
            raw_price = float(data.get('open', 0))
            self._slippage_cost_total += sell_qty * raw_price * self.config.slippage_rate

            # 执行卖出（commission/tax 拆分记录，impact_cost 扣减净收入）
            revenue = sell_qty * filled_price
            commission = max(revenue * self.config.commission_rate, self.config.min_commission)
            tax = revenue * self.config.sell_tax
            net_revenue = revenue - commission - tax - impact_cost

            # 计算盈亏
            pos_data = position_info.get(code, {})
            avg_cost = pos_data.get('cost', filled_price)
            entry_date = pos_data.get('entry_date', date_str)
            cost_basis = avg_cost * sell_qty
            profit_loss = net_revenue - cost_basis
            roi = (profit_loss / cost_basis * 100) if cost_basis > 0 else 0

            # 计算持有天数
            try:
                d1 = datetime.strptime(entry_date, "%Y-%m-%d")
                d2 = datetime.strptime(date_str, "%Y-%m-%d")
                holding_days = (d2 - d1).days
            except:
                holding_days = 0

            new_cash += net_revenue
            del new_portfolio[code]
            if code in position_info:
                del position_info[code]

            position_id = f"{entry_date}-{code}"

            trades.append(TradeRecord(
                date=date_str,
                code=code,
                action="sell",
                shares=sell_qty,
                price=filled_price,
                amount=net_revenue,
                commission=commission,
                tax=tax,
                profit_loss=profit_loss,
                roi=roi,
                entry_price=avg_cost,
                entry_date=entry_date,
                exit_price=filled_price,
                exit_date=date_str,
                holding_days=holding_days,
                position_id=position_id,
                indicators=signal.get('indicators', {}) if isinstance(signal, dict) else {}
            ))

        # 阶段2: 处理买入
        buy_signals = {code: sig for code, sig in signals.items()
                      if (sig.get('action') if isinstance(sig, dict) else sig) in ('buy', 'enter')}

        # 检查持仓限制
        current_positions_count = len(new_portfolio)
        max_positions = self.config.max_positions

        if max_positions is not None and current_positions_count >= max_positions:
            buy_signals = {}
        elif max_positions is not None:
            can_buy = max_positions - current_positions_count
            buy_signals = dict(list(buy_signals.items())[:can_buy])

        # 检查单日买入限制
        max_stocks_per_day = self.config.max_stocks_per_day
        if max_stocks_per_day is not None:
            buy_signals = dict(list(buy_signals.items())[:max_stocks_per_day])

        for code, signal in buy_signals.items():
            if new_portfolio.get(code, 0) > 0:
                continue

            if code not in signal_data:
                continue

            data = signal_data[code]
            volume = float(data.get('volume', 0))
            bar = _Bar(
                open=float(data.get('open', 0)),
                close=float(data.get('close', 0)),
                volume=volume,
                is_suspended=bool(data.get('is_suspended', 0)),
                is_limit_up=bool(data.get('is_limit_up', 0)),
                is_limit_down=bool(data.get('is_limit_down', 0)),
                is_st=bool(data.get('is_st', 0)),
            )

            # P0-6：先用 evaluate() 做校验（停牌/涨跌停/ST/价格）
            check_fill = self._shared_policy.evaluate(
                bar, side="buy", quantity=0, average_volume=volume,
            )
            if not check_fill.allowed and check_fill.rejection_reason != "min_trade_amount":
                if check_fill.rejection_reason == "suspended":
                    self._rejected_stats.suspended += 1
                elif check_fill.rejection_reason == "limit_up":
                    self._rejected_stats.limit_up += 1
                elif check_fill.rejection_reason == "st_filtered":
                    self._rejected_stats.st += 1
                elif check_fill.rejection_reason == "invalid_price":
                    self._rejected_stats.invalid_price += 1
                continue

            # 计算买入金额
            target_investment = new_cash * self.config.position_ratio
            if target_investment > new_cash:
                target_investment = new_cash

            if target_investment < self.config.min_trade_amount:
                self._rejected_stats.insufficient_cash += 1
                continue

            # 滑点价（与 evaluate() 一致）
            filled_price = check_fill.fill_price if check_fill.fill_price > 0 else bar.open * (1 + self.config.slippage_rate)

            # 计算股数（100股整数倍）
            shares = int(target_investment / (filled_price * (1 + self.config.commission_rate)))
            shares = (shares // 100) * 100

            if shares < 100:
                self._rejected_stats.insufficient_cash += 1
                continue

            # 成交量约束：用 evaluate() 的 max_quantity
            max_shares = check_fill.max_quantity
            if max_shares > 0:
                max_shares = (max_shares // 100) * 100
                if shares > max_shares:
                    if max_shares < 100:
                        self._rejected_stats.volume += 1
                        continue
                    shares = max_shares

            cost = shares * filled_price
            commission = max(cost * self.config.commission_rate, self.config.min_commission)
            # P0-6：冲击成本计入买入总支出
            impact_cost = self._shared_policy.policy.compute_impact_cost(shares, filled_price, volume)
            total_outlay = cost + commission + impact_cost

            if total_outlay <= new_cash:
                new_portfolio[code] = shares
                new_cash -= total_outlay
                self._slippage_cost_total += shares * bar.open * self.config.slippage_rate

                position_id = f"{date_str}-{code}"

                position_info[code] = {
                    'cost': total_outlay / shares,
                    'entry_date': date_str,
                    'position_id': position_id
                }

                trades.append(TradeRecord(
                    date=date_str,
                    code=code,
                    action="buy",
                    shares=shares,
                    price=filled_price,
                    amount=total_outlay,
                    commission=commission,
                    holding_days=0,
                    position_id=position_id,
                    indicators=signal.get('indicators', {}) if isinstance(signal, dict) else {}
                ))
            else:
                self._rejected_stats.insufficient_cash += 1

        return new_portfolio, new_cash, trades

    def _execute_trades_vectorized(
        self,
        current_time: pd.Timestamp,
        signals: Dict[str, Any],
        portfolio: Dict[str, int],
        cash: float,
        position_info: Dict[str, Dict[str, Any]],
        data_dict: Dict[str, Dict[str, Any]],
        date_str: str
    ) -> Tuple[Dict[str, int], float, List[TradeRecord]]:
        """
        向量化交易执行 - 使用 Polars 批量处理

        性能优势：
        - 批量筛选可交易信号
        - 向量化计算佣金、税费、盈亏
        - 减少循环开销
        """
        new_portfolio = portfolio.copy()
        new_cash = cash
        trades = []

        signal_rows = []
        for code, signal in signals.items():
            sig_type = signal.get('action') if isinstance(signal, dict) else signal
            if sig_type in ('buy', 'enter', 'sell', 'exit'):
                signal_rows.append({
                    'code': code,
                    'action': 'sell' if sig_type in ('sell', 'exit') else 'buy',
                    'indicators': signal.get('indicators', {}) if isinstance(signal, dict) else {}
                })

        if not signal_rows:
            return new_portfolio, new_cash, trades

        signals_df = pl.DataFrame(signal_rows)

        if self._factor_matrix is not None and isinstance(data_dict, dict) and '_factor_slice' in data_dict:
            factor_slice = data_dict['_factor_slice']
            fm = self._factor_matrix
            market_df = self.engine.data_layer._build_market_df_from_matrix(factor_slice, fm)
        else:
            market_rows = []
            for code, data in data_dict.items():
                market_rows.append({
                    'code': code,
                    'open': float(data.get('open') or 0),
                    'close': float(data.get('close') or 0),
                    'is_suspended': bool(data.get('is_suspended') or 0),
                    'is_limit_up': bool(data.get('is_limit_up') or 0),
                    'is_limit_down': bool(data.get('is_limit_down') or 0),
                    'is_st': bool(data.get('is_st') or 0),
                    'volume': float(data.get('volume') or 0),
                    'adj_factor': float(data.get('adj_factor') or 1.0),
                    'total_mv': float(data.get('total_mv') or 0)
                })

            market_df = pl.DataFrame(market_rows)

        # =========================================================================
        # Step 3: 构建持仓 DataFrame
        # =========================================================================
        position_rows = []
        for code, shares in portfolio.items():
            pos_data = position_info.get(code, {})
            position_rows.append({
                'code': code,
                'shares': shares,
                'cost': pos_data.get('cost', 0),
                'entry_date': pos_data.get('entry_date', date_str)
            })

        positions_df = pl.DataFrame(position_rows) if position_rows else pl.DataFrame({
            'code': [], 'shares': [], 'cost': [], 'entry_date': []
        }).with_columns([
            pl.col('shares').cast(pl.Int64),
            pl.col('cost').cast(pl.Float64),
            pl.col('entry_date').cast(pl.Utf8)
        ])

        # =========================================================================
        # Step 4: 向量化处理卖出信号（P0-6：补 ST 过滤 + volume_cap + impact_cost）
        # =========================================================================
        sell_signals = signals_df.filter(pl.col('action') == 'sell')

        if not sell_signals.is_empty() and not positions_df.is_empty():
            # 关联持仓数据（P0-6：添加 is_st 和 volume 列）
            sell_market_cols = ['code', 'open', 'is_suspended', 'is_limit_down', 'volume']
            if self.config.exclude_st:
                sell_market_cols.append('is_st')
            sell_with_position = sell_signals.join(
                positions_df, on='code', how='inner'
            ).join(
                market_df.select(sell_market_cols),
                on='code', how='inner'
            )

            # 统计被拒绝的原因
            try:
                self._rejected_stats.suspended += int(
                    sell_with_position.filter(pl.col('is_suspended') == True).height
                )
                self._rejected_stats.limit_down += int(
                    sell_with_position.filter(pl.col('is_limit_down') == True).height
                )
                self._rejected_stats.invalid_price += int(
                    sell_with_position.filter(pl.col('open') <= 0).height
                )
                if self.config.exclude_st and 'is_st' in sell_with_position.columns:
                    self._rejected_stats.st += int(
                        sell_with_position.filter(pl.col('is_st') == True).height
                    )
            except Exception:
                pass

            # 筛选可卖出的股票（P0-6：补 ST 过滤）
            sellable_filter = (
                (pl.col('shares') > 0) &
                (pl.col('open') > 0) &
                (pl.col('is_suspended') == False) &
                (pl.col('is_limit_down') == False)
            )
            if self.config.exclude_st and 'is_st' in sell_with_position.columns:
                sellable_filter = sellable_filter & (pl.col('is_st') == False)
            sellable = sell_with_position.filter(sellable_filter)

            if not sellable.is_empty():
                # P0-6：volume_cap 约束卖出数量
                if self.config.volume_cap_ratio > 0 and 'volume' in sellable.columns:
                    sellable = sellable.with_columns([
                        ((pl.col('volume') * self.config.volume_cap_ratio)
                         .floor().cast(pl.Int64) // 100 * 100).alias('volume_cap_shares')
                    ]).with_columns([
                        pl.min_horizontal([pl.col('shares'), pl.col('volume_cap_shares')]).alias('sell_qty')
                    ])
                    # volume_cap_shares < 100 的被拒绝
                    try:
                        self._rejected_stats.volume += int(
                            sellable.filter(
                                (pl.col('volume') > 0) & (pl.col('volume_cap_shares') < 100)
                            ).height
                        )
                    except Exception:
                        pass
                    sellable = sellable.filter(
                        (pl.col('volume') <= 0) | (pl.col('sell_qty') >= 100)
                    )
                else:
                    sellable = sellable.with_columns([
                        pl.col('shares').alias('sell_qty')
                    ])

                if not sellable.is_empty():
                    # 应用滑点：卖出按 (1 - slippage_rate) 减价
                    slippage = self.config.slippage_rate
                    sellable = sellable.with_columns([
                        (pl.col('open') * (1.0 - slippage)).alias('filled_price'),
                    ])
                    # 累计滑点成本
                    try:
                        slippage_sum = (sellable['open'] * slippage * sellable['sell_qty']).sum()
                        self._slippage_cost_total += float(slippage_sum)
                    except Exception:
                        pass

                    # 向量化计算卖出参数（P0-6：用 sell_qty 替代 shares + impact_cost）
                    impact_rate = self._shared_policy.policy.impact_cost_rate
                    sellable = sellable.with_columns([
                        (pl.col('sell_qty') * pl.col('filled_price')).alias('revenue'),
                    ]).with_columns([
                        pl.max_horizontal([
                            pl.col('revenue') * self.config.commission_rate,
                            self.config.min_commission
                        ]).alias('commission'),
                        (pl.col('revenue') * self.config.sell_tax).alias('tax'),
                    ])

                    # P0-6：冲击成本
                    if impact_rate > 0 and 'volume' in sellable.columns:
                        sellable = sellable.with_columns([
                            (pl.col('sell_qty') * pl.col('filled_price') * impact_rate *
                             pl.min_horizontal([
                                 pl.when(pl.col('volume') > 0)
                                   .then(pl.col('sell_qty') / pl.col('volume'))
                                   .otherwise(0.0),
                                 pl.lit(1.0)
                             ])).alias('impact_cost')
                        ])
                    else:
                        sellable = sellable.with_columns([
                            pl.lit(0.0).alias('impact_cost')
                        ])

                    sellable = sellable.with_columns([
                        (pl.col('revenue') - pl.col('commission') - pl.col('tax') - pl.col('impact_cost')).alias('net_revenue'),
                        (pl.col('cost') * pl.col('sell_qty')).alias('cost_basis'),
                    ]).with_columns([
                        (pl.col('net_revenue') - pl.col('cost_basis')).alias('profit_loss'),
                    ]).with_columns([
                        pl.when(pl.col('cost_basis') > 0)
                          .then(pl.col('profit_loss') / pl.col('cost_basis') * 100)
                          .otherwise(0).alias('roi')
                    ])

                    # 计算持有天数
                    current_date = datetime.strptime(date_str, "%Y-%m-%d")
                    sellable = sellable.with_columns([
                        pl.col('entry_date').map_elements(
                            lambda d: (current_date - datetime.strptime(d, "%Y-%m-%d")).days
                            if d else 0,
                            return_dtype=pl.Int64
                        ).alias('holding_days')
                    ])

                    # 执行卖出并创建交易记录
                    total_net_revenue = sellable.select(pl.col('net_revenue').sum()).item()
                    new_cash += total_net_revenue

                    for row in sellable.iter_rows(named=True):
                        code = row['code']
                        del new_portfolio[code]
                        if code in position_info:
                            del position_info[code]

                        position_id = f"{row['entry_date']}-{code}"

                        trades.append(TradeRecord(
                            date=date_str,
                            code=code,
                            action="sell",
                            shares=int(row['sell_qty']),
                            price=row['filled_price'],
                            amount=row['net_revenue'],
                            commission=row['commission'],
                            tax=row['tax'],
                            profit_loss=row['profit_loss'],
                            roi=row['roi'],
                            entry_price=row['cost'],
                            entry_date=row['entry_date'],
                            exit_price=row['filled_price'],
                            exit_date=date_str,
                            holding_days=int(row['holding_days']),
                            position_id=position_id,
                            indicators=row.get('indicators', {})
                        ))

        # =========================================================================
        # Step 5: 向量化处理买入信号
        # =========================================================================
        buy_signals = signals_df.filter(pl.col('action') == 'buy')

        if not buy_signals.is_empty():
            # 检查持仓限制
            current_positions_count = len(new_portfolio)
            max_positions = self.config.max_positions

            if max_positions is not None:
                if current_positions_count >= max_positions:
                    buy_signals = buy_signals.head(0)
                else:
                    can_buy = max_positions - current_positions_count
                    buy_signals = buy_signals.head(can_buy)

            # 检查单日买入限制
            max_stocks_per_day = self.config.max_stocks_per_day
            if max_stocks_per_day is not None:
                buy_signals = buy_signals.head(max_stocks_per_day)

            # 过滤已持仓股票
            if not positions_df.is_empty():
                existing_codes = set(positions_df['code'].to_list())
                buy_signals = buy_signals.filter(
                    ~pl.col('code').is_in(existing_codes)
                )

            # 关联市场数据
            market_cols = ['code', 'close', 'open', 'is_suspended', 'is_limit_up']
            if self.config.exclude_st:
                market_cols.append('is_st')
            if self.config.volume_cap_ratio > 0:
                market_cols.append('volume')
            buyable = buy_signals.join(
                market_df.select(market_cols),
                on='code', how='inner'
            )

            # 统计被拒绝原因（在主过滤之前）
            try:
                self._rejected_stats.suspended += int(
                    buyable.filter(pl.col('is_suspended') == True).height
                )
                self._rejected_stats.limit_up += int(
                    buyable.filter(pl.col('is_limit_up') == True).height
                )
                if self.config.exclude_st and 'is_st' in buyable.columns:
                    self._rejected_stats.st += int(
                        buyable.filter(pl.col('is_st') == True).height
                    )
            except Exception:
                pass

            # 主体可买过滤
            buy_filter_expr = (
                ((pl.col('close') > 0) | (pl.col('open') > 0)) &
                (pl.col('is_suspended') == False) &
                (pl.col('is_limit_up') == False)
            )
            if self.config.exclude_st and 'is_st' in buyable.columns:
                buy_filter_expr = buy_filter_expr & (pl.col('is_st') == False)
            buyable = buyable.filter(buy_filter_expr)

            if not buyable.is_empty():
                # 向量化计算买入参数 - 使用 close 价格（如果 open 为 0）
                target_investment = new_cash * self.config.position_ratio

                # 应用滑点：买入按 (1 + slippage_rate) 加价
                slippage = self.config.slippage_rate

                buyable = buyable.with_columns([
                    pl.when(pl.col('open') > 0)
                    .then(pl.col('open'))
                    .otherwise(pl.col('close'))
                    .alias('trade_price'),
                ]).with_columns([
                    (pl.col('trade_price') * (1.0 + slippage)).alias('filled_price'),
                ])

                # 成交量约束：单笔买入股数 <= 当日成交量 × volume_cap_ratio
                if self.config.volume_cap_ratio > 0 and 'volume' in buyable.columns:
                    buyable = buyable.with_columns([
                        ((pl.col('volume') * self.config.volume_cap_ratio).floor().cast(pl.Int64) // 100 * 100).alias('volume_cap_shares'),
                    ])
                    # 当 volume_cap_shares < 100 视为被成交量限制
                    try:
                        self._rejected_stats.volume += int(
                            buyable.filter(
                                (pl.col('volume') > 0) & (pl.col('volume_cap_shares') < 100)
                            ).height
                        )
                    except Exception:
                        pass
                    buyable = buyable.filter(
                        (pl.col('volume') <= 0) | (pl.col('volume_cap_shares') >= 100)
                    )

                buyable = buyable.with_columns([
                    (target_investment / (pl.col('filled_price') * (1 + self.config.commission_rate)))
                    .floor().alias('raw_shares'),
                ]).with_columns([
                    ((pl.col('raw_shares') // 100) * 100).alias('shares'),
                ])

                # 成交量上限钳制
                if self.config.volume_cap_ratio > 0 and 'volume_cap_shares' in buyable.columns:
                    buyable = buyable.with_columns([
                        pl.min_horizontal([pl.col('shares'), pl.col('volume_cap_shares')]).alias('shares'),
                    ])

                # 资金不足统计（在过滤之前）
                try:
                    self._rejected_stats.insufficient_cash += int(
                        buyable.filter(pl.col('shares') < 100).height
                    )
                except Exception:
                    pass

                buyable = buyable.filter(
                    (pl.col('shares') >= 100) &
                    (pl.col('shares') * pl.col('filled_price') <= new_cash)
                )

                if not buyable.is_empty():
                    buyable = buyable.with_columns([
                        (pl.col('shares') * pl.col('filled_price')).alias('cost'),
                    ]).with_columns([
                        pl.max_horizontal([
                            pl.col('cost') * self.config.commission_rate,
                            self.config.min_commission
                        ]).alias('commission'),
                    ])

                    # P0-6：冲击成本计入买入总支出
                    impact_rate = self._shared_policy.policy.impact_cost_rate
                    if impact_rate > 0 and 'volume' in buyable.columns:
                        buyable = buyable.with_columns([
                            (pl.col('shares') * pl.col('filled_price') * impact_rate *
                             pl.min_horizontal([
                                 pl.when(pl.col('volume') > 0)
                                   .then(pl.col('shares') / pl.col('volume'))
                                   .otherwise(0.0),
                                 pl.lit(1.0)
                             ])).alias('impact_cost')
                        ])
                    else:
                        buyable = buyable.with_columns([
                            pl.lit(0.0).alias('impact_cost')
                        ])

                    buyable = buyable.with_columns([
                        (pl.col('cost') + pl.col('commission') + pl.col('impact_cost')).alias('total_outlay'),
                    ])

                    # 累计滑点成本
                    try:
                        slippage_sum = (buyable['trade_price'] * slippage * buyable['shares']).sum()
                        self._slippage_cost_total += float(slippage_sum)
                    except Exception:
                        pass

                    # 按可用资金排序，选择可负担的交易
                    buyable = buyable.sort('total_outlay')

                    for row in buyable.iter_rows(named=True):
                        total_outlay = row['total_outlay']
                        if total_outlay > new_cash:
                            continue

                        code = row['code']
                        shares = int(row['shares'])

                        position_id = f"{date_str}-{code}"

                        new_portfolio[code] = shares
                        new_cash -= total_outlay

                        position_info[code] = {
                            'cost': total_outlay / shares,
                            'entry_date': date_str,
                            'position_id': position_id
                        }

                        trades.append(TradeRecord(
                            date=date_str,
                            code=code,
                            action="buy",
                            shares=shares,
                            price=row['filled_price'],
                            amount=total_outlay,
                            commission=row['commission'],
                            holding_days=0,
                            position_id=position_id,
                            indicators=row.get('indicators', {})
                        ))

        return new_portfolio, new_cash, trades

    def _process_dividends(
        self,
        current_time: pd.Timestamp,
        portfolio: Dict[str, int],
        prev_day_data: Dict[str, Dict[str, Any]],
        current_day_data: Dict[str, Dict[str, Any]],
        cash: float
    ) -> Tuple[float, List[Dict]]:
        """
        处理分红和送转股 - 向量化优化版本

        动态复权核心逻辑：
        1. 现金分红：增加账户现金
        2. 送转股：更新持仓数量和成本价

        性能优化：
        1. 使用 NumPy 批量计算复权因子变化
        2. 向量化计算分红金额
        3. 减少循环次数
        """
        if not portfolio or not prev_day_data:
            return cash, []

        dividends = []
        date_str = current_time.strftime("%Y-%m-%d")

        # =========================================================================
        # [向量化优化] 批量处理分红
        # =========================================================================
        codes = list(portfolio.keys())
        shares_arr = np.array([portfolio[c] for c in codes], dtype=np.float64)

        # 提取数据
        prev_factors = np.array([
            float(prev_day_data.get(c, {}).get('adj_factor', 1.0)) for c in codes
        ])
        curr_factors = np.array([
            float(current_day_data.get(c, {}).get('adj_factor', 1.0)) for c in codes
        ])
        prev_closes = np.array([
            float(prev_day_data.get(c, {}).get('close', 0)) for c in codes
        ])
        prev_mvs = np.array([
            float(prev_day_data.get(c, {}).get('total_mv', 0)) for c in codes
        ])
        curr_mvs = np.array([
            float(current_day_data.get(c, {}).get('total_mv', 0)) for c in codes
        ])
        curr_closes = np.array([
            float(current_day_data.get(c, {}).get('close', 0)) for c in codes
        ])

        # 计算复权因子变化
        factor_change_mask = np.abs(curr_factors - prev_factors) > 1e-6

        if not np.any(factor_change_mask):
            return cash, []

        # 计算股本变化比例
        with np.errstate(divide='ignore', invalid='ignore'):
            prev_shares_total = np.where(prev_closes > 0, prev_mvs / prev_closes, 0)
            curr_shares_total = np.where(curr_closes > 0, curr_mvs / curr_closes, 0)
            shares_change_ratio = np.where(
                prev_shares_total > 0,
                np.abs(curr_shares_total / prev_shares_total - 1),
                0
            )

        # 现金分红条件：股本变化比例 < 5%
        cash_dividend_mask = factor_change_mask & (shares_change_ratio < 0.05)

        # 计算每股分红金额
        with np.errstate(divide='ignore', invalid='ignore'):
            dividend_per_share = prev_closes * (1 - prev_factors / curr_factors)

        dividend_per_share = np.where(dividend_per_share > 0, dividend_per_share, 0)
        total_dividends = shares_arr * dividend_per_share * cash_dividend_mask

        # 累加现金分红
        cash += float(np.sum(total_dividends))

        # 记录现金分红
        for i, code in enumerate(codes):
            if cash_dividend_mask[i] and total_dividends[i] > 0:
                dividends.append({
                    "date": date_str,
                    "code": code,
                    "type": "cash",
                    "dividend": float(total_dividends[i]),
                    "dividend_per_share": float(dividend_per_share[i])
                })

        # 送转/拆股条件：股本变化比例 >= 5%
        split_mask = factor_change_mask & (shares_change_ratio >= 0.05)

        for i, code in enumerate(codes):
            if split_mask[i]:
                old_shares = int(shares_arr[i])
                # 持仓数量按复权因子比例调整
                adj_ratio = curr_factors[i] / prev_factors[i]
                new_shares = int(round(old_shares * adj_ratio))

                if new_shares != old_shares and new_shares > 0:
                    # 更新持仓数量
                    portfolio[code] = new_shares

                    # 更新持仓成本价（按比例调整）
                    if code in self._position_info:
                        old_cost = self._position_info[code].get('cost', 0)
                        if old_cost > 0:
                            self._position_info[code]['cost'] = old_cost / adj_ratio

                    dividends.append({
                        "date": date_str,
                        "code": code,
                        "type": "split",
                        "old_shares": old_shares,
                        "new_shares": new_shares,
                        "adj_ratio": adj_ratio
                    })

        return cash, dividends
