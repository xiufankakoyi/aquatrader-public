export interface MockStock {
  code: string
  symbol: string
  name: string
  market: 'SZ' | 'SH'
  price: number
}

export const MOCK_STOCKS: MockStock[] = [
  { code: '000001', symbol: '000001.SZ', name: '平安银行', market: 'SZ', price: 12.58 },
  { code: '000002', symbol: '000002.SZ', name: '万科A', market: 'SZ', price: 15.32 },
  { code: '600519', symbol: '600519.SH', name: '贵州茅台', market: 'SH', price: 1688.88 },
  { code: '000858', symbol: '000858.SZ', name: '五粮液', market: 'SZ', price: 145.60 },
  { code: '002415', symbol: '002415.SZ', name: '海康威视', market: 'SZ', price: 32.45 },
]

export const MOCK_STRATEGIES = [
  {
    id: 'strategy_001',
    name: '双均线策略',
    description: 'Mock 策略，仅用于界面联调',
    category: '趋势跟踪',
    params: { fast_period: 5, slow_period: 20 },
  },
  {
    id: 'strategy_002',
    name: 'MACD策略',
    description: 'Mock 策略，仅用于界面联调',
    category: '动量策略',
    params: { fast_period: 12, slow_period: 26, signal_period: 9 },
  },
]

export function mockLatestPrices(date = '2024-06-15') {
  return Object.fromEntries(
    MOCK_STOCKS.map((stock) => [
      stock.symbol,
      { price: stock.price, date, name: stock.name },
    ]),
  )
}

export function createMockBacktestResult(strategyName = 'MockStrategy') {
  const initialCapital = 1_000_000
  let equity = initialCapital
  let benchmarkEquity = initialCapital
  const equityCurve: Array<{ date: string; equity: number; benchmark: number }> = []
  const trades: any[] = []

  for (let i = 0; i < 252; i++) {
    const date = new Date('2024-01-01')
    date.setDate(date.getDate() + i)
    equity *= 1 + (Math.random() - 0.45) * 0.025
    benchmarkEquity *= 1 + (Math.random() - 0.48) * 0.015
    const dateText = date.toISOString().split('T')[0]

    equityCurve.push({
      date: dateText,
      equity: Number(equity.toFixed(2)),
      benchmark: Number(benchmarkEquity.toFixed(2)),
    })

    if (Math.random() < 0.15) {
      const stock = MOCK_STOCKS[Math.floor(Math.random() * MOCK_STOCKS.length)]
      const price = 100 + Math.random() * 50
      const shares = Math.floor(Math.random() * 1000) + 100
      const isBuy = Math.random() > 0.5
      const profitLoss = (
        isBuy ? Math.random() - 0.3 : Math.random() - 0.7
      ) * price * shares * 0.1

      trades.push({
        id: `trade-${i}`,
        symbol: stock.symbol,
        stockName: stock.name,
        action: isBuy ? 'BUY' : 'SELL',
        price: Number(price.toFixed(2)),
        shares,
        date: dateText,
        entryDate: dateText,
        exitDate: dateText,
        profitLoss: Number(profitLoss.toFixed(2)),
        profitLossPercent: Number(((profitLoss / (price * shares)) * 100).toFixed(2)),
      })
    }
  }

  const totalReturn = (equity - initialCapital) / initialCapital
  const benchmarkReturn = (benchmarkEquity - initialCapital) / initialCapital
  const monthlyReturns = [{
    year: 2024,
    months: Array.from(
      { length: 12 },
      () => Number(((Math.random() - 0.35) * 8).toFixed(2)),
    ),
  }]

  return {
    source: 'mock' as const,
    is_mock: true as const,
    strategyName,
    equityCurve,
    trades,
    monthlyReturns,
    metrics: {
      totalReturn,
      annualReturn: totalReturn,
      benchmarkReturn,
      excessReturn: totalReturn - benchmarkReturn,
      sharpeRatio: 1.8 + Math.random() * 0.4,
      maxDrawdown: -(0.08 + Math.random() * 0.05),
      volatility: 0.22,
      benchmarkVolatility: 0.15,
      winRate: trades.length > 0 ? 0.58 + Math.random() * 0.1 : 0,
      profitLossRatio: trades.length > 0 ? 1.8 + Math.random() * 0.5 : 0,
      totalTrades: trades.length,
      avgHoldingDays: trades.length > 0 ? 5 + Math.random() * 10 : 0,
      calmarRatio: 2.1 + Math.random() * 0.5,
      sortinoRatio: 2.5 + Math.random() * 0.5,
      monthlyReturns: {},
    },
  }
}

export function createMockBacktestGenerator(strategyName = 'MockStrategy') {
  const result = createMockBacktestResult(strategyName)
  const totalDays = result.equityCurve.length

  return {
    source: result.source,
    is_mock: result.is_mock,
    equityCurve: result.equityCurve,
    trades: result.trades,
    startStreaming: (onData: (data: any) => void) => {
      let day = 0
      const interval = setInterval(() => {
        if (day >= totalDays) {
          clearInterval(interval)
          return
        }
        const data = result.equityCurve[day]
        day += 1
        onData({ ...data, day, totalDays, source: 'mock', is_mock: true })
      }, 50)
      return () => clearInterval(interval)
    },
    getFullData: () => ({
      equityCurve: result.equityCurve.map(({ date, equity }) => ({ date, equity })),
      benchmark: result.equityCurve.map(({ date, benchmark }) => ({
        date,
        equity: benchmark,
      })),
      trades: result.trades,
      source: result.source,
      is_mock: result.is_mock,
    }),
    getFinalMetrics: () => result.metrics,
    getMonthlyReturns: () => result.monthlyReturns,
  }
}

export function createMockStrategyDetail(versionId: string) {
  const result = createMockBacktestResult(versionId || 'MockStrategy')
  return markMockResponse({
    versionId,
    versionName: `${result.strategyName} Mock`,
    strategyName: result.strategyName,
    equityCurve: result.equityCurve.map(({ date, equity }) => ({ date, equity })),
    equitySeries: result.equityCurve.map(({ date, equity }) => ({ date, equity })),
    trades: result.trades,
    holdingPeriods: [],
    metrics: {
      total_return: result.metrics.totalReturn,
      annual_return: result.metrics.annualReturn,
      sharpe_ratio: result.metrics.sharpeRatio,
      max_drawdown: result.metrics.maxDrawdown,
      win_rate: result.metrics.winRate,
      profit_factor: result.metrics.profitLossRatio,
      total_trades: result.metrics.totalTrades,
      avg_holding_days: result.metrics.avgHoldingDays,
      monthly_returns: result.monthlyReturns,
    },
    radarScores: {
      excessReturn: 0.75,
      riskConsistency: 0.68,
      maxDrawdown: 0.82,
      tradingQuality: 0.71,
      antiOverfitting: 0.65,
    },
    startDate: '2024-01-01',
    endDate: '2024-12-31',
    initialCapital: 1_000_000,
    finalCapital: result.equityCurve.at(-1)?.equity ?? 1_000_000,
  })
}

export function markMockResponse<T>(value: T): T {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return {
      ...(value as Record<string, unknown>),
      source: 'mock',
      is_mock: true,
    } as T
  }
  return value
}
