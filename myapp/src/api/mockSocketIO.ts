/**
 * Mock Socket.IO 实现
 * 在 Mock 模式下模拟回测进度推送，无需后端服务
 */

import { ref } from 'vue'
import { createMockBacktestGenerator, MOCK_STOCKS } from './mockDataRegistry'

// Mock 状态
const status = ref('OPEN')
const mockId = 'mock-socket-' + Date.now()

// 事件监听器存储
const eventListeners: Map<string, Set<(data: any) => void>> = new Map()

// Mock 回测数据生成器
export function generateMockBacktestData(strategyName: string) {
  return createMockBacktestGenerator(strategyName)
}

function legacyGenerateMockBacktestData(strategyName: string) {
  const totalDays = 252
  let currentDay = 0
  let equity = 1000000
  let benchmarkEquity = 1000000

  // 生成完整的权益曲线数据（用于初始加载）
  const equityCurve: Array<{ date: string; equity: number; benchmark: number }> = []
  const trades: any[] = []

  for (let i = 0; i < totalDays; i++) {
    const date = new Date('2024-01-01')
    date.setDate(date.getDate() + i)
    const dateStr = date.toISOString().split('T')[0]

    // 策略收益：年化 25%，优于基准
    const strategyChange = (Math.random() - 0.45) * 0.025 // 策略波动更大但收益更高
    equity = equity * (1 + strategyChange)

    // 基准收益：年化 8%
    const benchmarkChange = (Math.random() - 0.48) * 0.015 // 基准波动较小
    benchmarkEquity = benchmarkEquity * (1 + benchmarkChange)

    equityCurve.push({
      date: dateStr,
      equity: Math.round(equity * 100) / 100,
      benchmark: Math.round(benchmarkEquity * 100) / 100,
    })

    // 生成交易信号 - 增加交易频率
    if (Math.random() < 0.15) { // 15% 概率每天产生交易，确保有足够多的交易
      const isBuy = Math.random() > 0.5
      const symbols = MOCK_STOCKS.map((stock) => stock.symbol)
      const symbol = symbols[Math.floor(Math.random() * symbols.length)]
      const price = 100 + Math.random() * 50
      const shares = Math.floor(Math.random() * 1000) + 100
      const profitLoss = isBuy ? (Math.random() - 0.3) * price * shares * 0.1 : (Math.random() - 0.7) * price * shares * 0.1

      trades.push({
        id: `trade-${i}`,
        symbol: symbol,
        action: isBuy ? 'BUY' : 'SELL',
        price: Number(price.toFixed(2)),
        shares: shares,
        date: dateStr,
        entryDate: dateStr,
        exitDate: dateStr,
        profitLoss: Number(profitLoss.toFixed(2)),
        profitLossPercent: Number(((profitLoss / (price * shares)) * 100).toFixed(2)),
      })
    }
  }

  return {
    equityCurve,
    trades,

    // 流式更新：逐日推送
    startStreaming: (onData: (data: any) => void) => {
      let day = 0
      const interval = setInterval(() => {
        if (day >= totalDays) {
          clearInterval(interval)
          return
        }

        const data = equityCurve[day]
        day++

        onData({
          date: data.date,
          equity: data.equity,
          benchmark: data.benchmark,
          day: day,
          totalDays: totalDays,
        })
      }, 50) // 每50ms推送一天数据

      return () => clearInterval(interval)
    },

    // 获取完整数据
    getFullData: () => ({
      equityCurve: equityCurve.map(d => ({ date: d.date, equity: d.equity })),
      benchmark: equityCurve.map(d => ({ date: d.date, equity: d.benchmark })),
      trades: trades,
    }),

    // 最终指标 - 使用驼峰命名与 BacktestMetrics 接口匹配
    getFinalMetrics: () => {
      const totalReturn = ((equity - 1000000) / 1000000)
      const benchmarkReturn = ((benchmarkEquity - 1000000) / 1000000)
      const totalTrades = trades.length
      const winRate = totalTrades > 0 ? 0.58 + Math.random() * 0.1 : 0
      const profitLossRatio = 1.8 + Math.random() * 0.5
      const avgHoldingDays = 5 + Math.random() * 10

      return {
        totalReturn: totalReturn,
        annualReturn: totalReturn, // 简化：年化收益等于总收益
        benchmarkReturn: benchmarkReturn,
        excessReturn: totalReturn - benchmarkReturn,
        sharpeRatio: 1.8 + Math.random() * 0.4,
        maxDrawdown: -(0.08 + Math.random() * 0.05),
        volatility: 0.22,
        benchmarkVolatility: 0.15, // 添加基准波动率
        winRate: winRate,
        profitLossRatio: profitLossRatio,
        totalTrades: totalTrades,
        avgHoldingDays: avgHoldingDays,
        calmarRatio: 2.1 + Math.random() * 0.5,
        sortinoRatio: 2.5 + Math.random() * 0.5,
        monthlyReturns: {},
      }
    },

    // 生成月度收益数据
    getMonthlyReturns: () => {
      const monthlyData = []
      const year = 2024
      const months = Array(12).fill(null)

      // 策略月度收益（优于基准）
      for (let i = 0; i < 12; i++) {
        months[i] = Number(((Math.random() - 0.35) * 8).toFixed(2))
      }

      monthlyData.push({
        year: year,
        months: months,
      })

      return monthlyData
    },
  }
}

/**
 * 生成模拟 K 线数据
 */
function generateMockKlineData(symbol: string, startDate?: string, endDate?: string) {
  const days = 252
  const data = []

  // 根据股票代码设置基础价格
  let basePrice = 3500 // 沪深300默认基准价
  if (symbol.includes('000001')) basePrice = 12
  if (symbol.includes('000002')) basePrice = 15
  if (symbol.includes('600519')) basePrice = 1688
  if (symbol.includes('000858')) basePrice = 145
  if (symbol.includes('002415')) basePrice = 32
  if (symbol.includes('000300')) basePrice = 3500
  if (symbol.includes('000905')) basePrice = 5500

  // 计算起始日期
  let start = new Date('2024-01-01')
  if (startDate) {
    start = new Date(startDate)
  }

  for (let i = 0; i < days; i++) {
    const date = new Date(start)
    date.setDate(date.getDate() + i)

    // 根据指数类型调整波动率
    let volatility = 0.015
    if (symbol.includes('000300') || symbol.includes('000905')) {
      volatility = 0.012
    }

    const change = (Math.random() - 0.48) * 2 * volatility
    basePrice = basePrice * (1 + change)

    const open = basePrice * (1 + (Math.random() - 0.5) * 0.005)
    const close = basePrice
    const high = Math.max(open, close) * (1 + Math.random() * 0.008)
    const low = Math.min(open, close) * (1 - Math.random() * 0.008)

    data.push({
      date: date.toISOString().split('T')[0],
      open: Number(open.toFixed(2)),
      high: Number(high.toFixed(2)),
      low: Number(low.toFixed(2)),
      close: Number(close.toFixed(2)),
      volume: Math.floor(Math.random() * 100000000) + 50000000,
    })
  }

  return data
}

/**
 * Mock Socket.IO 实例
 */
export const mockSocket = {
  id: mockId,
  connected: true,
  disconnected: false,

  // 模拟 emit
  emit: (eventName: string, data: any) => {
    console.log(`[MockSocket] emit: ${eventName}`, data)

    // 处理回测请求
    if (eventName === 'run_streaming_backtest') {
      simulateBacktest(data.strategy_name || 'MockStrategy')
    }

    // 处理优化请求
    if (eventName === 'run_optimization') {
      simulateOptimization(data.strategy_name || 'MockStrategy')
    }

    // 处理获取回测结果请求
    if (eventName === 'get_backtest_result') {
      const generator = generateMockBacktestData(data.strategy_name || 'MockStrategy')
      const fullData = generator.getFullData()

      // 发送初始化数据
      setTimeout(() => {
        triggerEvent('initialized', {
          strategy_name: data.strategy_name,
          total_days: 252,
        })
      }, 100)

      // 发送完整数据
      setTimeout(() => {
        triggerEvent('backtest_data', {
          equity_curve: fullData.equityCurve,
          benchmark: fullData.benchmark,
          trades: fullData.trades,
          metrics: generator.getFinalMetrics(),
          monthly_returns: generator.getMonthlyReturns(),
        })
      }, 200)
    }

    // 处理 K 线数据请求
    if (eventName === 'request_kline') {
      const { symbol_code, start_date, end_date, request_id } = data
      console.log(`[MockSocket] 获取K线数据: ${symbol_code}`)

      // 生成模拟K线数据
      const klineData = generateMockKlineData(symbol_code, start_date, end_date)

      // 发送K线数据响应
      setTimeout(() => {
        triggerEvent('kline_data', {
          request_id: request_id,
          data: klineData,
          symbol: symbol_code,
        })
      }, 100)
    }
  },

  // 模拟 on
  on: (eventName: string, callback: (data: any) => void) => {
    if (!eventListeners.has(eventName)) {
      eventListeners.set(eventName, new Set())
    }
    eventListeners.get(eventName)?.add(callback)
  },

  // 模拟 off
  off: (eventName: string, callback: (data: any) => void) => {
    eventListeners.get(eventName)?.delete(callback)
  },

  // 模拟 disconnect
  disconnect: () => {
    console.log('[MockSocket] 断开连接')
    status.value = 'CLOSED'
  },
}

/**
 * 触发事件
 */
function triggerEvent(eventName: string, data: any) {
  const listeners = eventListeners.get(eventName)
  if (listeners) {
    listeners.forEach((callback) => {
      try {
        callback(data)
      } catch (e) {
        console.error(`[MockSocket] 事件处理错误: ${eventName}`, e)
      }
    })
  }
}

/**
 * 模拟回测流程
 */
function simulateBacktest(strategyName: string) {
  console.log(`[MockSocket] 开始模拟回测: ${strategyName}`)

  const generator = generateMockBacktestData(strategyName)
  const fullData = generator.getFullData()

  // 发送开始事件
  setTimeout(() => {
    triggerEvent('backtest_start', { strategy: strategyName, timestamp: Date.now() })
  }, 100)

  // 发送初始化完成事件
  setTimeout(() => {
    triggerEvent('initialized', {
      strategy_name: strategyName,
      total_days: 252,
    })
  }, 200)

  // 模拟进度更新
  let currentDay = 0
  const interval = setInterval(() => {
    if (currentDay >= fullData.equityCurve.length) {
      clearInterval(interval)

      // 发送最终指标
      triggerEvent('final_metrics', generator.getFinalMetrics())

      // 发送风险数据（包含月度收益）
      triggerEvent('risk_data', {
        monthly_returns: generator.getMonthlyReturns(),
      })

      // 发送完成事件 - 包含交易数据
      setTimeout(() => {
        triggerEvent('stream_complete', {
          success: true,
          message: '回测完成',
          trades: fullData.trades,
          tradesCount: fullData.trades.length
        })
      }, 500)
      return
    }

    const data = fullData.equityCurve[currentDay]
    const benchmark = fullData.benchmark[currentDay]
    currentDay++

    // 发送每日权益更新（包含基准）
    triggerEvent('daily_update', {
      date: data.date,
      equity: data.equity,
      benchmark_equity: benchmark.equity,
      day: currentDay,
      totalDays: fullData.equityCurve.length,
    })

    // 发送进度
    const progress = Math.round((currentDay / fullData.equityCurve.length) * 100)
    triggerEvent('progress', { progress, message: `正在回测... ${progress}%` })

    // 偶尔发送指标更新
    if (currentDay % 30 === 0) {
      triggerEvent('metrics_update', {
        current_equity: data.equity,
        day: currentDay,
      })
    }
  }, 30) // 每30ms更新一次，模拟快速回测
}

/**
 * 模拟参数优化流程
 */
function simulateOptimization(strategyName: string) {
  console.log(`[MockSocket] 开始模拟优化: ${strategyName}`)

  const totalIterations = 50
  let currentIteration = 0

  // 发送开始事件
  setTimeout(() => {
    triggerEvent('optimization_start', { strategy: strategyName })
  }, 100)

  const interval = setInterval(() => {
    currentIteration++

    if (currentIteration <= totalIterations) {
      // 发送进度
      const progress = Math.round((currentIteration / totalIterations) * 100)
      triggerEvent('optimization_progress', {
        iteration: currentIteration,
        total: totalIterations,
        progress,
        best_score: 1.0 + currentIteration * 0.02 + Math.random() * 0.1,
        current_params: {
          fast_period: 5 + Math.floor(Math.random() * 10),
          slow_period: 20 + Math.floor(Math.random() * 20),
        },
      })
    } else {
      // 优化完成
      clearInterval(interval)

      triggerEvent('optimization_complete', {
        success: true,
        best_params: {
          fast_period: 8,
          slow_period: 35,
        },
        best_score: 2.5,
        total_iterations: totalIterations,
      })
    }
  }, 200) // 每 200ms 更新一次
}

/**
 * 检查是否为 Mock 模式
 */
export function isMockSocketEnabled(): boolean {
  return import.meta.env.VITE_USE_MOCK === 'true'
}

/**
 * 获取 Mock Socket 实例
 */
export function getMockSocket() {
  return mockSocket
}
