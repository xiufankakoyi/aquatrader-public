/**
 * K 线数据管理器
 *
 * 提供 K 线数据的本地缓存、服务器拉取与历史数据加载能力。
 * 默认对接 /api/kline 公开接口，未找到后端时返回空数组。
 */
import { request } from '../services/api';

export interface KlineDataPoint {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

interface KlineQuery {
  symbol: string;
  timeframe: string;
  startDate: string;
  endDate: string;
  forceRefresh?: boolean;
}

interface CacheEntry {
  data: KlineDataPoint[];
  timestamp: number;
}

const CACHE_TTL_MS = 5 * 60 * 1000;
const cache = new Map<string, CacheEntry>();

function cacheKey(symbol: string, timeframe: string): string {
  return `${symbol}:${timeframe}`;
}

/**
 * 从本地缓存读取 K 线数据
 */
async function getCache(
  symbol: string,
  timeframe: string
): Promise<CacheEntry | undefined> {
  const key = cacheKey(symbol, timeframe);
  const entry = cache.get(key);
  if (!entry) return undefined;
  if (Date.now() - entry.timestamp > CACHE_TTL_MS) {
    cache.delete(key);
    return undefined;
  }
  return entry;
}

/**
 * 写入本地缓存
 */
function setCache(symbol: string, timeframe: string, data: KlineDataPoint[]): void {
  cache.set(cacheKey(symbol, timeframe), {
    data,
    timestamp: Date.now(),
  });
}

/**
 * 清空指定 symbol + timeframe 的缓存
 */
function clearCache(symbol: string, timeframe: string): void {
  cache.delete(cacheKey(symbol, timeframe));
}

/**
 * 从服务器拉取公开 K 线数据
 */
async function fetchFromServerPublic(
  symbol: string,
  timeframe: string,
  startDate: string,
  endDate: string
): Promise<KlineDataPoint[]> {
  try {
    const res = await request<KlineDataPoint[]>(
      `/kline/${encodeURIComponent(symbol)}?timeframe=${encodeURIComponent(timeframe)}&start=${startDate}&end=${endDate}`
    );
    if (res.ok && Array.isArray(res.data)) {
      return res.data;
    }
  } catch (e) {
    console.warn('[DataManager] fetchFromServerPublic failed:', e);
  }
  return [];
}

/**
 * 获取 K 线数据：优先缓存，后台刷新
 */
async function getKlineData(query: KlineQuery): Promise<KlineDataPoint[]> {
  const { symbol, timeframe, startDate, endDate, forceRefresh } = query;

  if (!forceRefresh) {
    const cached = await getCache(symbol, timeframe);
    if (cached && cached.data.length > 0) {
      // 后台尝试刷新，但立即返回缓存
      fetchFromServerPublic(symbol, timeframe, startDate, endDate)
        .then((fresh) => {
          if (fresh.length > 0) setCache(symbol, timeframe, fresh);
        })
        .catch(() => {});
      return cached.data;
    }
  }

  const fresh = await fetchFromServerPublic(symbol, timeframe, startDate, endDate);
  if (fresh.length > 0) {
    setCache(symbol, timeframe, fresh);
  }
  return fresh;
}

export const dataManager = {
  getCache,
  setCache,
  clearCache,
  fetchFromServerPublic,
  getKlineData,
};

export type { KlineQuery };
