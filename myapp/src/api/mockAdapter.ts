/**
 * Mock 数据拦截器 - Axios 版本
 * 在开发环境中模拟后端 API 响应，无需启动后端服务即可预览界面
 */

import type { AxiosInstance, InternalAxiosRequestConfig } from 'axios';
import type { StrategyInfo, KlineData } from '../types/api';
import {
  createMockBacktestResult,
  MOCK_STRATEGIES,
  markMockResponse,
  mockLatestPrices,
} from './mockDataRegistry';

// 模拟一些核心数据，让你能看到界面
const MOCK_DATA: Record<string, any> = {
  // 健康检查
  '/api/health': { status: 'ok', version: '1.0.0-mock' },

  // 用户信息
  '/api/user/info': {
    id: 1,
    username: 'AquaTrader',
    role: 'admin',
    avatar: 'https://api.dicebear.com/7.x/avataaars/svg?seed=Felix',
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
    ] as StrategyInfo[],
  },

  // 模拟 K 线数据，让你的图表能动起来
  '/api/kline': Array.from({ length: 100 }, (_, i) => ({
    time: Date.now() / 1000 - (100 - i) * 60,
    open: 100 + Math.random() * 10,
    high: 110 + Math.random() * 10,
    low: 90 + Math.random() * 10,
    close: 105 + Math.random() * 10,
    volume: Math.floor(Math.random() * 10000),
  })),

  // K线数据 - 生成模拟数据 (用于 /api/kline 端点)
  '/api/kline/data': (params: { symbol?: string; start?: string; end?: string }) => {
    const days = 100;
    const data: KlineData[] = [];
    const symbol = params?.symbol || '000300.SH'; // 默认沪深300
    
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

    for (let i = 0; i < days; i++) {
      const date = new Date();
      date.setDate(date.getDate() - (days - i));

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

    return data;
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
  '/api/latest_price': {
    '000001.SZ': { price: 12.58, date: '2024-01-15' },
    '000002.SZ': { price: 15.32, date: '2024-01-15' },
    '600519.SH': { price: 1688.88, date: '2024-01-15' },
  },

  // 回测结果
  '/api/backtest_result': {
    success: true,
    data: {
      equity_curve: Array.from({ length: 252 }, (_, i) => ({
        date: `2024-${String(Math.floor(i / 21) + 1).padStart(2, '0')}-${String((i % 21) + 1).padStart(2, '0')}`,
        equity: 1000000 + Math.random() * 200000 - 100000,
      })),
      trades: [],
      metrics: {
        total_return: 15.5,
        annual_return: 15.5,
        sharpe_ratio: 1.2,
        max_drawdown: -8.5,
        win_rate: 0.58,
        profit_factor: 1.8,
      },
    },
  },

  // 股票搜索 - 根据代码或名称搜索股票
  '/api/stocks/search': (params: { keyword?: string }) => {
    const keyword = params?.keyword || '';
    const searchTerm = keyword.toLowerCase().trim();

    // 模拟股票数据库
    const stockDatabase = [
      { code: '000001', name: '平安银行', symbol: '000001.SZ', market: 'SZ' },
      { code: '000002', name: '万科A', symbol: '000002.SZ', market: 'SZ' },
      { code: '000063', name: '中兴通讯', symbol: '000063.SZ', market: 'SZ' },
      { code: '000100', name: 'TCL科技', symbol: '000100.SZ', market: 'SZ' },
      { code: '000333', name: '美的集团', symbol: '000333.SZ', market: 'SZ' },
      { code: '000538', name: '云南白药', symbol: '000538.SZ', market: 'SZ' },
      { code: '000568', name: '泸州老窖', symbol: '000568.SZ', market: 'SZ' },
      { code: '000651', name: '格力电器', symbol: '000651.SZ', market: 'SZ' },
      { code: '000725', name: '京东方A', symbol: '000725.SZ', market: 'SZ' },
      { code: '000768', name: '中航西飞', symbol: '000768.SZ', market: 'SZ' },
      { code: '000858', name: '五粮液', symbol: '000858.SZ', market: 'SZ' },
      { code: '000895', name: '双汇发展', symbol: '000895.SZ', market: 'SZ' },
      { code: '002001', name: '新和成', symbol: '002001.SZ', market: 'SZ' },
      { code: '002007', name: '华兰生物', symbol: '002007.SZ', market: 'SZ' },
      { code: '002024', name: '苏宁易购', symbol: '002024.SZ', market: 'SZ' },
      { code: '002027', name: '分众传媒', symbol: '002027.SZ', market: 'SZ' },
      { code: '002049', name: '紫光国微', symbol: '002049.SZ', market: 'SZ' },
      { code: '002120', name: '韵达股份', symbol: '002120.SZ', market: 'SZ' },
      { code: '002142', name: '宁波银行', symbol: '002142.SZ', market: 'SZ' },
      { code: '002230', name: '科大讯飞', symbol: '002230.SZ', market: 'SZ' },
      { code: '002236', name: '大华股份', symbol: '002236.SZ', market: 'SZ' },
      { code: '002271', name: '东方雨虹', symbol: '002271.SZ', market: 'SZ' },
      { code: '002304', name: '洋河股份', symbol: '002304.SZ', market: 'SZ' },
      { code: '002352', name: '顺丰控股', symbol: '002352.SZ', market: 'SZ' },
      { code: '002415', name: '海康威视', symbol: '002415.SZ', market: 'SZ' },
      { code: '002460', name: '赣锋锂业', symbol: '002460.SZ', market: 'SZ' },
      { code: '002475', name: '立讯精密', symbol: '002475.SZ', market: 'SZ' },
      { code: '002594', name: '比亚迪', symbol: '002594.SZ', market: 'SZ' },
      { code: '002714', name: '牧原股份', symbol: '002714.SZ', market: 'SZ' },
      { code: '002812', name: '恩捷股份', symbol: '002812.SZ', market: 'SZ' },
      { code: '300003', name: '乐普医疗', symbol: '300003.SZ', market: 'SZ' },
      { code: '300014', name: '亿纬锂能', symbol: '300014.SZ', market: 'SZ' },
      { code: '300015', name: '爱尔眼科', symbol: '300015.SZ', market: 'SZ' },
      { code: '300033', name: '同花顺', symbol: '300033.SZ', market: 'SZ' },
      { code: '300059', name: '东方财富', symbol: '300059.SZ', market: 'SZ' },
      { code: '300122', name: '智飞生物', symbol: '300122.SZ', market: 'SZ' },
      { code: '300124', name: '汇川技术', symbol: '300124.SZ', market: 'SZ' },
      { code: '300142', name: '沃森生物', symbol: '300142.SZ', market: 'SZ' },
      { code: '300274', name: '阳光电源', symbol: '300274.SZ', market: 'SZ' },
      { code: '300408', name: '三环集团', symbol: '300408.SZ', market: 'SZ' },
      { code: '300413', name: '芒果超媒', symbol: '300413.SZ', market: 'SZ' },
      { code: '300433', name: '蓝思科技', symbol: '300433.SZ', market: 'SZ' },
      { code: '300498', name: '温氏股份', symbol: '300498.SZ', market: 'SZ' },
      { code: '300750', name: '宁德时代', symbol: '300750.SZ', market: 'SZ' },
      { code: '300760', name: '迈瑞医疗', symbol: '300760.SZ', market: 'SZ' },
      { code: '300999', name: '金龙鱼', symbol: '300999.SZ', market: 'SZ' },
      { code: '600000', name: '浦发银行', symbol: '600000.SH', market: 'SH' },
      { code: '600009', name: '上海机场', symbol: '600009.SH', market: 'SH' },
      { code: '600016', name: '民生银行', symbol: '600016.SH', market: 'SH' },
      { code: '600028', name: '中国石化', symbol: '600028.SH', market: 'SH' },
      { code: '600030', name: '中信证券', symbol: '600030.SH', market: 'SH' },
      { code: '600031', name: '三一重工', symbol: '600031.SH', market: 'SH' },
      { code: '600036', name: '招商银行', symbol: '600036.SH', market: 'SH' },
      { code: '600048', name: '保利发展', symbol: '600048.SH', market: 'SH' },
      { code: '600050', name: '中国联通', symbol: '600050.SH', market: 'SH' },
      { code: '600104', name: '上汽集团', symbol: '600104.SH', market: 'SH' },
      { code: '600276', name: '恒瑞医药', symbol: '600276.SH', market: 'SH' },
      { code: '600309', name: '万华化学', symbol: '600309.SH', market: 'SH' },
      { code: '600332', name: '白云山', symbol: '600332.SH', market: 'SH' },
      { code: '600340', name: '华夏幸福', symbol: '600340.SH', market: 'SH' },
      { code: '600406', name: '国电南瑞', symbol: '600406.SH', market: 'SH' },
      { code: '600436', name: '片仔癀', symbol: '600436.SH', market: 'SH' },
      { code: '600438', name: '通威股份', symbol: '600438.SH', market: 'SH' },
      { code: '600519', name: '贵州茅台', symbol: '600519.SH', market: 'SH' },
      { code: '600585', name: '海螺水泥', symbol: '600585.SH', market: 'SH' },
      { code: '600588', name: '用友网络', symbol: '600588.SH', market: 'SH' },
      { code: '600690', name: '海尔智家', symbol: '600690.SH', market: 'SH' },
      { code: '600703', name: '三安光电', symbol: '600703.SH', market: 'SH' },
      { code: '600745', name: '闻泰科技', symbol: '600745.SH', market: 'SH' },
      { code: '600809', name: '山西汾酒', symbol: '600809.SH', market: 'SH' },
      { code: '600837', name: '海通证券', symbol: '600837.SH', market: 'SH' },
      { code: '600887', name: '伊利股份', symbol: '600887.SH', market: 'SH' },
      { code: '600900', name: '长江电力', symbol: '600900.SH', market: 'SH' },
      { code: '601012', name: '隆基绿能', symbol: '601012.SH', market: 'SH' },
      { code: '601088', name: '中国神华', symbol: '601088.SH', market: 'SH' },
      { code: '601111', name: '中国国航', symbol: '601111.SH', market: 'SH' },
      { code: '601138', name: '工业富联', symbol: '601138.SH', market: 'SH' },
      { code: '601166', name: '兴业银行', symbol: '601166.SH', market: 'SH' },
      { code: '601211', name: '国泰君安', symbol: '601211.SH', market: 'SH' },
      { code: '601288', name: '农业银行', symbol: '601288.SH', market: 'SH' },
      { code: '601318', name: '中国平安', symbol: '601318.SH', market: 'SH' },
      { code: '601336', name: '新华保险', symbol: '601336.SH', market: 'SH' },
      { code: '601398', name: '工商银行', symbol: '601398.SH', market: 'SH' },
      { code: '601601', name: '中国太保', symbol: '601601.SH', market: 'SH' },
      { code: '601628', name: '中国人寿', symbol: '601628.SH', market: 'SH' },
      { code: '601633', name: '长城汽车', symbol: '601633.SH', market: 'SH' },
      { code: '601668', name: '中国建筑', symbol: '601668.SH', market: 'SH' },
      { code: '601688', name: '华泰证券', symbol: '601688.SH', market: 'SH' },
      { code: '601888', name: '中国中免', symbol: '601888.SH', market: 'SH' },
      { code: '601899', name: '紫金矿业', symbol: '601899.SH', market: 'SH' },
      { code: '601933', name: '永辉超市', symbol: '601933.SH', market: 'SH' },
      { code: '601939', name: '建设银行', symbol: '601939.SH', market: 'SH' },
      { code: '601988', name: '中国银行', symbol: '601988.SH', market: 'SH' },
      { code: '601989', name: '中国重工', symbol: '601989.SH', market: 'SH' },
      { code: '603288', name: '海天味业', symbol: '603288.SH', market: 'SH' },
      { code: '603501', name: '韦尔股份', symbol: '603501.SH', market: 'SH' },
      { code: '603659', name: '璞泰来', symbol: '603659.SH', market: 'SH' },
      { code: '603986', name: '兆易创新', symbol: '603986.SH', market: 'SH' },
      { code: '603993', name: '洛阳钼业', symbol: '603993.SH', market: 'SH' },
      { code: '688001', name: '华兴源创', symbol: '688001.SH', market: 'SH' },
      { code: '688008', name: '澜起科技', symbol: '688008.SH', market: 'SH' },
      { code: '688009', name: '中国通号', symbol: '688009.SH', market: 'SH' },
      { code: '688012', name: '中微公司', symbol: '688012.SH', market: 'SH' },
      { code: '688036', name: '传音控股', symbol: '688036.SH', market: 'SH' },
      { code: '688111', name: '金山办公', symbol: '688111.SH', market: 'SH' },
      { code: '688169', name: '石头科技', symbol: '688169.SH', market: 'SH' },
      { code: '688981', name: '中芯国际', symbol: '688981.SH', market: 'SH' },
    ];

    // 如果搜索词为空，返回空数组
    if (!searchTerm) {
      return { success: true, data: [] };
    }

    // 根据关键词过滤股票
    const results = stockDatabase.filter(stock => {
      const codeMatch = stock.code.includes(searchTerm);
      const nameMatch = stock.name.toLowerCase().includes(searchTerm);
      return codeMatch || nameMatch;
    });

    // 最多返回10条结果
    return {
      success: true,
      data: results.slice(0, 10)
    };
  },
};

/**
 * 设置 Mock 拦截器
 * @param axiosInstance Axios 实例
 */
MOCK_DATA['/api/strategies'] = markMockResponse({
  success: true,
  data: MOCK_STRATEGIES,
});
MOCK_DATA['/api/latest_price'] = markMockResponse(mockLatestPrices());
MOCK_DATA['/api/backtest_result'] = () => {
  const result = createMockBacktestResult()
  return markMockResponse({
    success: true,
    data: result,
  })
}

export function setupMock(axiosInstance: AxiosInstance): void {
  // 添加请求拦截器
  axiosInstance.interceptors.request.use((config: InternalAxiosRequestConfig) => {
    // 只有在开启 Mock 且是开发环境时才拦截
    if (import.meta.env.VITE_USE_MOCK === 'true') {
      console.log(`[Mock Intercept] ${config.url}`);

      // 简单的路由匹配逻辑
      const mockKey = Object.keys(MOCK_DATA).find((key) =>
        config.url?.includes(key)
      );

      if (mockKey) {
        // 使用 adapter 机制拦截请求
        config.adapter = async () => {
          // 模拟网络延迟 (100-300ms)
          await new Promise((resolve) =>
            setTimeout(resolve, 100 + Math.random() * 200)
          );

          const mockValue = MOCK_DATA[mockKey];

          // 如果是函数，执行并返回结果
          const data = markMockResponse(
            typeof mockValue === 'function'
              ? mockValue(config.params || {})
              : mockValue
          );

          console.log(`[Mock Response] ${config.url} ->`, data);

          return {
            data: data,
            status: 200,
            statusText: 'OK',
            headers: { 'X-Mock-Response': 'true' },
            config: config,
          };
        };
      }
    }
    return config;
  });
}

/**
 * 检查 Mock 模式是否开启
 */
export function isMockEnabled(): boolean {
  return import.meta.env.VITE_USE_MOCK === 'true';
}
