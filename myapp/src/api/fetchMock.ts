import {
  createMockStrategyDetail,
  MOCK_STRATEGIES,
  markMockResponse,
  mockLatestPrices,
} from './mockDataRegistry';

/**
 * Fetch Mock 拦截器
 * 拦截原生 fetch 请求，返回模拟数据
 * 用于项目中未使用 Axios 的地方
 */

// 模拟数据存储
const FETCH_MOCK_DATA: Record<string, any> = {
  // 启动状态检查
  '/api/startup-status': {
    ready: true,
    phase: 'ready',
    message: 'Mock 模式：后端服务已就绪',
    progress: 100,
  },

  // 启动开始
  '/api/startup-begin': {
    success: true,
    message: 'Mock 模式：启动流程已开始',
    taskId: 'mock-task-' + Date.now(),
  },

  // 策略列表
  '/api/strategies': {
    success: true,
    data: [
      {
        id: 'strategy_001',
        name: '双均线策略',
        description: '基于5日和20日均线金叉死叉的交易策略',
        category: '趋势跟踪',
        params: {
          fast_period: 5,
          slow_period: 20,
        },
      },
      {
        id: 'strategy_002',
        name: 'MACD策略',
        description: '基于MACD指标的交易策略',
        category: '动量策略',
        params: {
          fast_period: 12,
          slow_period: 26,
          signal_period: 9,
        },
      },
      {
        id: 'strategy_003',
        name: '布林带策略',
        description: '基于布林带突破的交易策略',
        category: '均值回归',
        params: {
          period: 20,
          std_dev: 2,
        },
      },
    ],
  },

  // K线数据 - 生成模拟数据
  '/api/kline': (url: string) => {
    const urlObj = new URL(url, 'http://localhost');
    const symbol = urlObj.searchParams.get('symbol') || '000300.SH'; // 默认沪深300
    const startParam = urlObj.searchParams.get('start');
    const endParam = urlObj.searchParams.get('end');
    
    // 生成252个交易日数据（约一年）
    const days = 252;
    const data = [];
    
    // 根据股票代码设置基础价格
    let basePrice = 3500; // 沪深300默认基准价
    if (symbol === '000001.SZ') basePrice = 12;
    if (symbol === '000002.SZ') basePrice = 15;
    if (symbol === '600519.SH') basePrice = 1688;
    if (symbol === '000858.SZ') basePrice = 145;
    if (symbol === '002415.SZ') basePrice = 32;
    if (symbol === '000300.SH') basePrice = 3500; // 沪深300
    if (symbol === '000905.SH') basePrice = 5500; // 中证500
    if (symbol === '000852.SH') basePrice = 6200; // 中证1000
    if (symbol === '399006.SZ') basePrice = 2100; // 创业板指
    if (symbol === '000001.SH') basePrice = 3100; // 上证指数

    // 计算起始日期
    let startDate = new Date();
    if (startParam) {
      startDate = new Date(startParam);
    } else {
      startDate.setDate(startDate.getDate() - days);
    }

    for (let i = 0; i < days; i++) {
      const date = new Date(startDate);
      date.setDate(date.getDate() + i);

      // 根据指数类型调整波动率
      let volatility = 0.015; // 默认波动率
      if (symbol === '000300.SH' || symbol === '000905.SH' || symbol === '000852.SH') {
        volatility = 0.012; // 指数波动率较小
      }
      
      const change = (Math.random() - 0.48) * 2 * volatility; // 略微偏向上涨
      basePrice = basePrice * (1 + change);

      const open = basePrice * (1 + (Math.random() - 0.5) * 0.005);
      const close = basePrice;
      const high = Math.max(open, close) * (1 + Math.random() * 0.008);
      const low = Math.min(open, close) * (1 - Math.random() * 0.008);

      data.push({
        date: date.toISOString().split('T')[0],
        open: Number(open.toFixed(2)),
        high: Number(high.toFixed(2)),
        low: Number(low.toFixed(2)),
        close: Number(close.toFixed(2)),
        volume: Math.floor(Math.random() * 100000000) + 50000000, // 指数成交量更大
      });
    }

    // 返回符合后端API格式的数据
    return {
      success: true,
      data: data,
      symbol: symbol,
      total: data.length
    };
  },

  // 股票舆情数据
  '/api/research/stock_sentiment': {
    success: true,
    data: [
      {
        symbol: '000001.SZ',
        stockCode: '000001',
        stockName: '平安银行',
        totalPosts: 1250,
        totalClicks: 50000,
        totalComments: 3200,
        bullishCount: 800,
        bearishCount: 300,
        neutralCount: 150,
        sentimentScore: 0.65,
        lastPostTime: '2024-01-15 14:30:00',
        activeDays: 30,
      },
      {
        symbol: '000002.SZ',
        stockCode: '000002',
        stockName: '万科A',
        totalPosts: 980,
        totalClicks: 35000,
        totalComments: 2100,
        bullishCount: 400,
        bearishCount: 450,
        neutralCount: 130,
        sentimentScore: -0.15,
        lastPostTime: '2024-01-15 13:45:00',
        activeDays: 28,
      },
      {
        symbol: '600519.SH',
        stockCode: '600519',
        stockName: '贵州茅台',
        totalPosts: 2100,
        totalClicks: 120000,
        totalComments: 8500,
        bullishCount: 1500,
        bearishCount: 350,
        neutralCount: 250,
        sentimentScore: 0.78,
        lastPostTime: '2024-01-15 15:00:00',
        activeDays: 30,
      },
    ],
  },

  // 最新价格
  '/api/latest_price': (url: string) => {
    const urlObj = new URL(url, 'http://localhost');
    const symbolsParam = urlObj.searchParams.get('symbols');
    const dateParam = urlObj.searchParams.get('date') || '2024-06-15';
    
    const priceMap: Record<string, { price: number; date: string; name?: string }> = {
      '000001': { price: 12.58, date: dateParam, name: '平安银行' },
      '000002': { price: 15.32, date: dateParam, name: '万科A' },
      '600519': { price: 1688.88, date: dateParam, name: '贵州茅台' },
      '000858': { price: 145.60, date: dateParam, name: '五粮液' },
      '002415': { price: 32.45, date: dateParam, name: '海康威视' },
      '000300': { price: 3580.50, date: dateParam, name: '沪深300' },
      '000905': { price: 5620.30, date: dateParam, name: '中证500' },
    };
    
    // 如果指定了symbols，只返回对应的价格
    if (symbolsParam) {
      const symbols = symbolsParam.split(',');
      const result: Record<string, { price: number; date: string; name?: string }> = {};
      symbols.forEach(symbol => {
        const code = symbol.replace(/\.SZ|\.SH/g, '');
        if (priceMap[code]) {
          result[symbol] = priceMap[code];
        }
      });
      return result;
    }
    
    return priceMap;
  },

  // 回测结果
  '/api/run_backtest': {
    success: true,
    data: {
      task_id: 'mock-backtest-' + Date.now(),
      message: 'Mock 回测任务已创建',
    },
  },

  // 策略详情
  '/api/strategy/': (url: string) => {
    const match = url.match(/\/strategy\/([^/]+)/);
    const versionId = match ? match[1] : 'unknown';

    // 生成月度收益数据
    const monthlyReturns: Record<string, number> = {};
    const months = ['2024-01', '2024-02', '2024-03', '2024-04', '2024-05', '2024-06', '2024-07', '2024-08', '2024-09', '2024-10', '2024-11', '2024-12'];
    months.forEach((month) => {
      monthlyReturns[month] = Number(((Math.random() - 0.4) * 0.13).toFixed(4));
    });

    // 生成模拟交易记录
    const stockPool = [
      { code: '000001', name: '平安银行', symbol: '000001.SZ' },
      { code: '000002', name: '万科A', symbol: '000002.SZ' },
      { code: '600519', name: '贵州茅台', symbol: '600519.SH' },
      { code: '000858', name: '五粮液', symbol: '000858.SZ' },
      { code: '002415', name: '海康威视', symbol: '002415.SZ' },
    ];

    const trades = [];
    const baseDate = new Date('2024-01-15');
    let tradeId = 1;

    // 生成20笔交易记录
    for (let i = 0; i < 10; i++) {
      const stock = stockPool[i % stockPool.length];
      const buyDate = new Date(baseDate);
      buyDate.setDate(buyDate.getDate() + i * 15);
      
      const sellDate = new Date(buyDate);
      sellDate.setDate(sellDate.getDate() + Math.floor(Math.random() * 20) + 5);
      
      const buyPrice = 50 + Math.random() * 100;
      const sellPrice = buyPrice * (1 + (Math.random() - 0.35) * 0.15);
      const volume = Math.floor(Math.random() * 500 + 100);
      const pnl = (sellPrice - buyPrice) * volume;

      // 买入记录
      trades.push({
        id: `trade-${tradeId++}`,
        date: buyDate.toISOString().split('T')[0],
        entryDate: buyDate.toISOString().split('T')[0],
        stockCode: stock.code,
        stockName: stock.name,
        symbol: stock.symbol,
        symbolCode: stock.code,
        action: 'buy',
        price: Number(buyPrice.toFixed(2)),
        entryPrice: Number(buyPrice.toFixed(2)),
        volume: volume,
        quantity: volume,
        amount: Number((buyPrice * volume).toFixed(2)),
        commission: Number((buyPrice * volume * 0.0003).toFixed(2)),
      });

      // 卖出记录
      trades.push({
        id: `trade-${tradeId++}`,
        date: sellDate.toISOString().split('T')[0],
        exitDate: sellDate.toISOString().split('T')[0],
        entryDate: buyDate.toISOString().split('T')[0],
        stockCode: stock.code,
        stockName: stock.name,
        symbol: stock.symbol,
        symbolCode: stock.code,
        action: 'sell',
        price: Number(sellPrice.toFixed(2)),
        exitPrice: Number(sellPrice.toFixed(2)),
        entryPrice: Number(buyPrice.toFixed(2)),
        volume: volume,
        quantity: volume,
        amount: Number((sellPrice * volume).toFixed(2)),
        commission: Number((sellPrice * volume * 0.0003).toFixed(2)),
        pnl: Number(pnl.toFixed(2)),
        profitLoss: Number(pnl.toFixed(2)),
        profit: Number(pnl.toFixed(2)),
      });
    }

    // 生成持仓数据
    const holdingPeriods = [];
    for (let i = 0; i < 3; i++) {
      const stock = stockPool[i];
      const entryDate = new Date(baseDate);
      entryDate.setDate(entryDate.getDate() + i * 10);
      
      const entryPrice = 50 + Math.random() * 100;
      const currentPrice = entryPrice * (1 + (Math.random() - 0.3) * 0.1);
      const quantity = Math.floor(Math.random() * 300 + 100);
      
      holdingPeriods.push({
        positionId: `pos-${i + 1}`,
        position_id: `pos-${i + 1}`,
        symbolCode: stock.code,
        symbol_code: stock.code,
        symbol: stock.symbol,
        symbolName: stock.name,
        symbol_name: stock.name,
        entryDate: entryDate.toISOString().split('T')[0],
        entry_date: entryDate.toISOString().split('T')[0],
        exitDate: null,
        exit_date: null,
        entryPrice: Number(entryPrice.toFixed(2)),
        entry_price: Number(entryPrice.toFixed(2)),
        exitPrice: undefined,
        exit_price: undefined,
        quantity: quantity,
        shares: quantity,
        profit: Number(((currentPrice - entryPrice) * quantity).toFixed(2)),
        pnl: Number(((currentPrice - entryPrice) * quantity).toFixed(2)),
        holdingDays: Math.floor(Math.random() * 30) + 5,
        days: Math.floor(Math.random() * 30) + 5,
      });
    }

    // 生成权益曲线
    const equityCurve = [];
    let equity = 1000000;
    const startDate = new Date('2024-01-01');
    for (let i = 0; i < 252; i++) {
      const date = new Date(startDate);
      date.setDate(date.getDate() + i);
      const change = (Math.random() - 0.48) * 0.02;
      equity = equity * (1 + change);
      equityCurve.push({
        date: date.toISOString().split('T')[0],
        equity: Number(equity.toFixed(2)),
      });
    }

    // 生成雷达图分数
    const radarScores = {
      excessReturn: 0.75,
      riskConsistency: 0.68,
      maxDrawdown: 0.82,
      tradingQuality: 0.71,
      antiOverfitting: 0.65,
    };

    // 直接返回数据（与后端实际返回格式一致）
    return {
      versionId: versionId,
      versionName: '双均线策略 V1.0',
      strategyName: '双均线策略',
      equityCurve: equityCurve,
      equitySeries: equityCurve,
      trades: trades,
      holdingPeriods: holdingPeriods,
      metrics: {
        total_return: 15.5,
        annual_return: 15.5,
        sharpe_ratio: 1.2,
        max_drawdown: -8.5,
        win_rate: 0.58,
        profit_factor: 1.8,
        total_trades: trades.length,
        avg_holding_days: 12.5,
        monthly_returns: monthlyReturns,
      },
      radarScores: radarScores,
      startDate: '2024-01-01',
      endDate: '2024-12-31',
      initialCapital: 1000000,
      finalCapital: equityCurve[equityCurve.length - 1]?.equity || 1155000,
    };
  },

  // 参数搜索结果
  '/api/strategy/.*/parameters': {
    success: true,
    data: [],
  },

  // 策略参数
  '/api/strategies/.*/params': {
    success: true,
    data: {
      params: [
        { name: 'fast_period', type: 'int', default: 5, min: 3, max: 20 },
        { name: 'slow_period', type: 'int', default: 20, min: 10, max: 60 },
      ],
    },
  },

  // 策略预设
  '/api/strategies/.*/profiles': {
    success: true,
    data: [],
  },

  // 词云数据
  '/api/research/stock_sentiment_words': {
    success: true,
    data: {
      symbol: '000001.SZ',
      stockCode: '000001',
      stockName: '平安银行',
      totalPosts: 1250,
      totalClicks: 50000,
      totalComments: 3200,
      overallSentiment: 0.65,
      words: [
        { word: '看涨', weight: 100, positiveWeight: 80, negativeWeight: 20, count: 150 },
        { word: '下跌', weight: 80, positiveWeight: 30, negativeWeight: 70, count: 120 },
        { word: '震荡', weight: 60, positiveWeight: 40, negativeWeight: 40, count: 90 },
      ],
    },
  },

  // 情感趋势
  '/api/research/sentiment_trends': {
    success: true,
    data: Array.from({ length: 30 }, (_, i) => {
      const date = new Date();
      date.setDate(date.getDate() - (30 - i));
      return {
        date: date.toISOString().split('T')[0],
        post_count: Math.floor(Math.random() * 1000) + 500,
        avg_sentiment: Math.random() * 2 - 1,
      };
    }),
  },

  // LDA 主题
  '/api/research/lda_topics': {
    success: true,
    data: {
      topics: ['业绩', '分红', '政策', '市场情绪', '技术指标'],
      scores: [0.3, 0.25, 0.2, 0.15, 0.1],
    },
  },

  // 散点图数据
  '/api/scatter_data': {
    success: true,
    data: Array.from({ length: 50 }, (_, i) => ({
      symbol: `00000${i + 1}.SZ`,
      name: `股票${i + 1}`,
      sentiment: Math.random() * 2 - 1,
      comment_count: Math.floor(Math.random() * 10000),
      comment_count_normalized: Math.random(),
      market_cap: Math.floor(Math.random() * 100000000000),
    })),
  },

  // 个股情感时间线
  '/api/research/stock_sentiment_timeline': {
    success: true,
    data: Array.from({ length: 30 }, (_, i) => {
      const date = new Date();
      date.setDate(date.getDate() - (30 - i));
      return {
        time: date.toISOString().split('T')[0],
        bullishCount: Math.floor(Math.random() * 100),
        bearishCount: Math.floor(Math.random() * 100),
        neutralCount: Math.floor(Math.random() * 50),
        totalCount: 0,
      };
    }),
  },

  // 预加载
  '/api/preload': {
    success: true,
    task_id: 'mock-preload-' + Date.now(),
    status: 'completed',
    strategy_name: 'MockStrategy',
    start_date: '2024-01-01',
    end_date: '2024-12-31',
  },

  // 预加载状态
  '/api/preload/status/': (url: string) => {
    const match = url.match(/\/preload\/status\/(.+)/);
    const taskId = match ? match[1] : 'unknown';
    return {
      success: true,
      task_id: taskId,
      status: 'completed',
      strategy_name: 'MockStrategy',
      start_date: '2024-01-01',
      end_date: '2024-12-31',
      cache_key: 'mock-cache-key',
    };
  },

  // 导出 PDF
  '/api/export/pdf': {
    success: true,
    data: {
      url: 'data:application/pdf;base64,mock-pdf-content',
      filename: 'mock-report.pdf',
    },
  },

  // 导出 Excel
  '/api/export/excel': {
    success: true,
    data: {
      url: 'data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,mock-excel-content',
      filename: 'mock-report.xlsx',
    },
  },

  // 分析报告
  '/api/analyze_report': {
    success: true,
    data: {
      analysis: 'Mock 分析报告：这是一个模拟的分析结果。',
      recommendations: ['建议1', '建议2', '建议3'],
    },
  },

  // 基准数据
  '/api/benchmark/': (url: string) => {
    return {
      success: true,
      data: Array.from({ length: 252 }, (_, i) => ({
        date: `2024-${String(Math.floor(i / 21) + 1).padStart(2, '0')}-${String((i % 21) + 1).padStart(2, '0')}`,
        equity: 1000000 + Math.random() * 100000 - 50000,
      })),
    };
  },
};

/**
 * 查找匹配的 Mock 路由
 */
FETCH_MOCK_DATA['/api/strategies'] = markMockResponse({
  success: true,
  data: MOCK_STRATEGIES,
});
FETCH_MOCK_DATA['/api/latest_price'] = markMockResponse(mockLatestPrices());
FETCH_MOCK_DATA['/api/strategy/'] = (url: string) => {
  const match = url.match(/\/strategy\/([^/]+)/)
  return createMockStrategyDetail(match ? decodeURIComponent(match[1]) : 'MockStrategy')
}

function findMockKey(url: string): string | null {
  // 精确匹配
  if (FETCH_MOCK_DATA[url]) return url;

  // 模式匹配
  for (const key of Object.keys(FETCH_MOCK_DATA)) {
    // 处理正则表达式模式
    if (key.includes('.*')) {
      const regex = new RegExp(key.replace(/\//g, '\\/').replace(/\.\*/g, '.*'));
      if (regex.test(url)) {
        return key;
      }
    }
    // 前缀匹配
    else if (url.includes(key)) {
      return key;
    }
  }

  return null;
}

/**
 * 获取 Mock 响应数据
 */
function getMockResponse(url: string): any {
  const mockKey = findMockKey(url);

  if (!mockKey) return null;

  const mockValue = FETCH_MOCK_DATA[mockKey];

  // 如果是函数，执行并返回结果
  if (typeof mockValue === 'function') {
    return markMockResponse(mockValue(url));
  }

  return markMockResponse(mockValue);
}

/**
 * 设置 Fetch Mock 拦截器
 */
export function setupFetchMock(): void {
  // 只在开发环境且开启 Mock 时生效
  if (import.meta.env.VITE_USE_MOCK !== 'true') {
    console.log('[FetchMock] Mock 模式未开启');
    return;
  }

  console.log('[FetchMock] ✅ Fetch Mock 模式已开启');

  // 保存原始 fetch
  const originalFetch = window.fetch;

  // 重写 fetch
  window.fetch = async function (
    input: RequestInfo | URL,
    init?: RequestInit
  ): Promise<Response> {
    const url = input.toString();

    // 检查是否有匹配的 Mock 数据
    const mockData = getMockResponse(url);

    if (mockData !== null) {
      console.log(`[FetchMock] 拦截请求: ${url}`);

      // 模拟网络延迟 (100-300ms)
      await new Promise((resolve) => setTimeout(resolve, 100 + Math.random() * 200));

      // 返回模拟响应
      return new Response(JSON.stringify(mockData), {
        status: 200,
        statusText: 'OK',
        headers: {
          'Content-Type': 'application/json',
          'X-Mock-Response': 'true',
        },
      });
    }

    // 没有 Mock 数据，使用原始 fetch
    return originalFetch(input, init);
  };

  console.log('[FetchMock] Fetch Mock 拦截器已安装');
}

/**
 * 检查 Fetch Mock 模式是否开启
 */
export function isFetchMockEnabled(): boolean {
  return import.meta.env.VITE_USE_MOCK === 'true';
}
