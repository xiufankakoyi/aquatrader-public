/**
 * StockSentimentPage - 静态 Demo Fixture (T4.1)
 *
 * 设计目标:
 *  - 当前端 fetch 全部失败 / 后端无数据时, 兜底使用本地结构化数据
 *  - 仅用于演示舆情面板布局, 严禁伪造真实股吧帖子内容
 *  - 通过 demo_mode=true / source="static_demo" 标识, 不参与 quantFlow 决策权重
 */
import type { StockSentimentItem, StockWordCloudResponse } from './backtestApi';

export interface DemoFlag {
  demo_mode: boolean;
  source: string;
  message: string;
}

/** Demo 标记默认值, 配合后端 _demo_wrap 行为 */
export const SENTIMENT_DEMO_FLAG: DemoFlag = {
  demo_mode: true,
  source: 'static_demo',
  message: '暂无真实股吧舆论数据, 当前为 Demo 展示',
};

/**
 * 静态 Demo - 股票舆情汇总列表
 * 字段结构与后端 StockSentimentItem 完全一致, 避免前端适配
 */
export const DEMO_STOCK_SENTIMENT_ITEMS: StockSentimentItem[] = [
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
];

/**
 * 静态 Demo - 单股词云 (无原始帖子内容, 仅有结构化字段, 词云为空数组)
 */
export function buildDemoStockWordCloud(symbol: string): StockWordCloudResponse {
  return {
    symbol,
    stockCode: symbol ? symbol.slice(-6) : '',
    stockName: '',
    totalPosts: 0,
    totalClicks: 0,
    totalComments: 0,
    overallSentiment: null,
    words: [],
  };
}

/**
 * 静态 Demo - 散点图点位 (供 ScatterPlotChart 渲染)
 * 与 guba_service._demo_scatter_points 字段保持一致
 */
export interface DemoScatterPoint {
  symbol: string;
  name: string;
  comment_count: number;
  comment_count_normalized: number;
  sentiment: number;
  market_cap: number;
}

export const DEMO_SCATTER_POINTS: DemoScatterPoint[] = [
  {
    symbol: '000001.SZ',
    name: '平安银行',
    comment_count: 3200,
    comment_count_normalized: 0.85,
    sentiment: 0.65,
    market_cap: 2.1e11,
  },
  {
    symbol: '000002.SZ',
    name: '万科A',
    comment_count: 2100,
    comment_count_normalized: 0.55,
    sentiment: -0.15,
    market_cap: 1.2e11,
  },
  {
    symbol: '600519.SH',
    name: '贵州茅台',
    comment_count: 8500,
    comment_count_normalized: 1.0,
    sentiment: 0.78,
    market_cap: 2.1e12,
  },
];
