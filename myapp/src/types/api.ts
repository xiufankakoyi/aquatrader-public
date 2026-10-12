export interface StrategyInfo {
  id: string;
  name: string;
  description?: string;
  createdDate?: string;
  lastUpdated?: string;
  performance?: number;
  status?: 'active' | 'inactive';
}

export interface KlineData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface KlineResponse {
  request_id?: string;
  symbol_code: string;
  symbolCode: string;
  symbol_name: string;
  data: KlineData[];
  error?: string;
}

export interface BacktestParams {
  strategy_name: string;
  start_date: string;
  end_date: string;
  benchmark_code?: string | null;
}

export interface BacktestStartEvent {
  type: 'backtest_start';
  data: {
    strategy_name: string;
    start_date: string;
    end_date: string;
    benchmark_code?: string;
  };
}

export interface DailyEquityEvent {
  type: 'daily_equity';
  data: {
    date: string;
    strategyReturn: number;
    benchmarkReturn?: number;
    equity?: number;
    benchmark_equity?: number;
    totalReturn?: number;
    drawdown?: number;
  };
}

export interface NewTradeEvent {
  type: 'new_trade';
  data: {
    id?: string;
    date: string;
    symbol: string;
    symbolCode?: string;
    symbol_code?: string;
    action: 'buy' | 'sell';
    price: number;
    quantity: number;
    profitLoss?: number;
    profit_loss?: number;
    cumulativePnL?: number;
    cumulative_pnl?: number;
    positionId?: string;
    position_id?: string;
    entryDate?: string;
    entry_date?: string;
    exitDate?: string;
    exit_date?: string;
  };
}

export interface MetricsUpdateEvent {
  type: 'metrics_update' | 'final_metrics';
  data: {
    totalReturn?: number;
    total_return?: number;
    annualizedReturn?: number;
    annualized_return?: number;
    maxDrawdown?: number;
    max_drawdown?: number;
    sharpeRatio?: number;
    sharpe_ratio?: number;
    sortinoRatio?: number;
    sortino_ratio?: number;
    volatility?: number;
    winRate?: number;
    win_rate?: number;
    profitFactor?: number;
    profit_factor?: number;
    tradesCount?: number;
    trades_count?: number;
    avgTradeReturn?: number;
    avg_trade_return?: number;
    maxWinningStreak?: number;
    max_winning_streak?: number;
    maxLosingStreak?: number;
    max_losing_streak?: number;
  };
}

export interface RiskDataEvent {
  type: 'risk_data';
  data: {
    monthlyReturns?: Array<{
      year: number;
      months: (number | null)[];
    }>;
    monthly_returns?: Array<{
      year: number;
      months: (number | null)[];
    }>;
    holdingPeriods?: Array<{
      position_id: string;
      symbol_code: string;
      symbol_name: string;
      entry_date: string;
      exit_date: string;
      entry_price?: number;
      exit_price?: number;
      quantity?: number;
    }>;
    drawdowns?: Array<{
      date: string;
      drawdown: number;
      peak?: number;
      valley?: number;
    }>;
  };
}

export interface StreamCompleteEvent {
  type: 'stream_complete';
  data: {
    message?: string;
  };
}

export interface ProgressEvent {
  type: 'progress';
  data: {
    progress: number;
    message?: string;
  };
}

export interface ErrorEvent {
  type: 'error';
  data: {
    message: string;
    code?: string;
  };
}

export interface InitializingEvent {
  type: 'initializing';
  data: {
    message: string;
    progress: number;
  };
}

export interface InitializedEvent {
  type: 'initialized';
  data: {
    message: string;
    progress: number;
  };
}

export type BacktestEvent =
  | BacktestStartEvent
  | DailyEquityEvent
  | NewTradeEvent
  | MetricsUpdateEvent
  | RiskDataEvent
  | StreamCompleteEvent
  | ProgressEvent
  | ErrorEvent
  | InitializingEvent
  | InitializedEvent;

export interface ApiResponse<T = any> {
  success: boolean;
  data?: T;
  error?: string;
}

