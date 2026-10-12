import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { request, ApiError } from '../services/api';

export interface Position {
  id?: number;
  stock_code: string;
  stock_name: string;
  buy_price: number;
  shares: number;
  cost: number;
  buy_date: string;
  stop_loss?: number;
  take_profit?: number;
  notes?: string;
  is_active: boolean;
  current_price?: number;
  market_value?: number;
  profit_loss?: number;
  profit_loss_pct?: number;
  weight?: number;
}

/**
 * 持仓历史记录接口
 */
export interface PositionHistory {
  id?: number;
  position_id: number;
  stock_code: string;
  stock_name: string;
  action: 'buy' | 'sell' | 'add' | 'reduce';
  shares: number;
  price: number;
  amount: number;
  date: string;
  notes?: string;
  created_at?: string;
}

/**
 * 持仓历史统计接口
 */
export interface PositionHistoryStats {
  total_trades: number;
  buy_count: number;
  sell_count: number;
  add_count: number;
  reduce_count: number;
  total_buy_amount: number;
  total_sell_amount: number;
}

export interface Signal {
  stock_code: string;
  stock_name: string;
  signal_date: string;
  signal_type: 'buy' | 'sell' | 'watch';
  signal_name: string;
  signal_strength: number;
  price_at_signal: number;
  details: string;
}

export interface PortfolioSummary {
  total_market_value: number;
  total_cost: number;
  total_profit_loss: number;
  total_profit_loss_pct: number;
  position_count: number;
  active_count: number;
}

export interface IndustryDistribution {
  [industry: string]: number;
}

/**
 * 条件项接口
 */
export interface Condition {
  id: string;
  indicator: string;
  operator: string;
  value: number | null;
  period: number;
}

/**
 * 条件组接口
 */
export interface ConditionGroup {
  id: string;
  logic: 'AND' | 'OR';
  conditions: Condition[];
}

/**
 * 信号配置接口
 */
export interface SignalConfig {
  signalType: string;
  conditionGroups: ConditionGroup[];
}

/**
 * 交易记录 (T2.7)
 * - 后端字段为 snake_case, 前端直接使用
 * - demo_mode=true 时表示数据来自 fallback (position_history.parquet) 或空结果
 * - source: "trade_records" | "position_history" | "empty"
 */
export interface TradeRecord {
  id: number;
  backtest_id: number | null;
  position_id: number | null;
  stock_code: string;
  stock_name: string;
  action: 'buy' | 'sell' | 'add' | 'reduce' | string;
  shares: number;
  price: number;
  amount: number;
  profit_loss: number | null;
  date: string;
  notes?: string;
  created_at?: string | null;
}

/**
 * 交易记录分页响应 (T2.7)
 */
export interface TradeRecordsResponse {
  items: TradeRecord[];
  total: number;
  page: number;
  page_size: number;
  demo_mode: boolean;
  source: 'trade_records' | 'position_history' | 'empty';
  start_date: string;
  end_date: string;
}

const API_BASE = '/portfolio';

export const usePortfolioStore = defineStore('portfolio', () => {
  const positions = ref<Position[]>([]);
  const signals = ref<{ buy: Signal[]; sell: Signal[]; watch: Signal[] }>({
    buy: [],
    sell: [],
    watch: []
  });
  const summary = ref<PortfolioSummary>({
    total_market_value: 0,
    total_cost: 0,
    total_profit_loss: 0,
    total_profit_loss_pct: 0,
    position_count: 0,
    active_count: 0
  });
  const industryDistribution = ref<IndustryDistribution>({});
  const rules = ref<Record<string, any>>({});
  const isLoading = ref(false);
  const error = ref<string | null>(null);
  
  // Signal configuration state
  const signalConfig = ref<SignalConfig | null>(null);
  
  // Position history state
  const positionHistory = ref<PositionHistory[]>([]);
  const positionHistoryStats = ref<PositionHistoryStats | null>(null);

  // T2.7: 交易记录分页 state
  // trades: 当前页数据; tradesMeta: 来源 / 总数 / demo 标记
  // tradesLoading / tradesError / tradesTimeoutMs: UI 状态
  const trades = ref<TradeRecord[]>([]);
  const tradesMeta = ref<{
    total: number;
    page: number;
    page_size: number;
    source: TradeRecordsResponse['source'];
    demo_mode: boolean;
    start_date: string;
    end_date: string;
    elapsed_ms?: number;
  } | null>(null);
  const tradesLoading = ref(false);
  const tradesError = ref<string | null>(null);
  const tradesQueryParams = ref<Record<string, any>>({});
  // 默认 15s 触发 timeout, 与 apiService.request 默认对齐
  const tradesTimeoutMs = ref(15000);

  const profitPositions = computed(() => 
    positions.value.filter(p => (p.profit_loss_pct || 0) > 0)
  );

  const lossPositions = computed(() => 
    positions.value.filter(p => (p.profit_loss_pct || 0) < 0)
  );

  const totalProfitPct = computed(() => summary.value.total_profit_loss_pct);

  async function fetchPositions(activeOnly: boolean = true) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/positions?active_only=${activeOnly}`);
      if (res.ok) {
        positions.value = res.data;
      }
    } catch (e: any) {
      error.value = e.message || '获取持仓失败';
    } finally {
      isLoading.value = false;
    }
  }

  async function fetchAnalysis() {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/analysis`);
      if (res.ok) {
        const data = res.data;
        positions.value = data.positions;
        summary.value = data.summary;
        industryDistribution.value = data.industry_distribution || {};
      }
    } catch (e: any) {
      error.value = e.message || '获取分析失败';
    } finally {
      isLoading.value = false;
    }
  }

  /**
   * T3.2: 获取 Demo 模拟盘持仓
   *
   * 设计目标:
   *  - 后端返回 demo_mode=true / real_broker_connected=false
   *  - 浮盈亏完全由后端按真实当前价 - 成本价计算, 前端不构造虚假收益
   *  - 失败时抛 ApiError, 供 ErrorState / TimeoutState 组件使用
   *
   * @returns 成功返回 { demoMode, realBrokerConnected, positions, summary, equityCurve, dataDate, warnings }
   */
  async function fetchDemoPortfolio(timeoutMs: number = 15000): Promise<{
    demoMode: boolean;
    realBrokerConnected: boolean;
    positions: any[];
    summary: any;
    equityCurve: number[];
    dataDate: string;
    warnings: string[];
  } | null> {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/demo`, { timeoutMs });
      if (res.ok) {
        const data = res.data;
        positions.value = data.positions || [];
        summary.value = data.summary || summary.value;
        industryDistribution.value = {};
        return {
          demoMode: !!data.demo_mode,
          realBrokerConnected: !!data.real_broker_connected,
          positions: data.positions || [],
          summary: data.summary || {},
          equityCurve: data.equity_curve || [],
          dataDate: data.data_date || '',
          warnings: data.warnings || [],
        };
      }
      error.value = res.message || '获取 demo 持仓失败';
      return null;
    } catch (e: any) {
      error.value = e?.message || '获取 demo 持仓失败';
      throw e;
    } finally {
      isLoading.value = false;
    }
  }

  async function addPosition(position: Position) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/positions`, {
        method: 'POST',
        body: JSON.stringify(position),
      });
      if (res.ok) {
        await fetchAnalysis();
        return res.data.id;
      }
    } catch (e: any) {
      error.value = e.message || '添加持仓失败';
    } finally {
      isLoading.value = false;
    }
    return null;
  }

  async function updatePosition(position: Position) {
    if (!position.id) return false;
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/positions/${position.id}`, {
        method: 'PUT',
        body: JSON.stringify(position),
      });
      if (res.ok) {
        await fetchAnalysis();
        return true;
      }
    } catch (e: any) {
      error.value = e.message || '更新持仓失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  async function deletePosition(positionId: number) {
    isLoading.value = true;
    error.value = null;
    try {
      console.log(`[deletePosition] 正在删除持仓 ID=${positionId}`);
      const res = await request<any>(`${API_BASE}/positions/${positionId}`, { method: 'DELETE' });
      console.log(`[deletePosition] 响应:`, res);
      if (res.ok) {
        await fetchAnalysis();
        console.log(`[deletePosition] 删除成功，已刷新数据`);
        return true;
      } else {
        error.value = res.message || '删除失败';
        console.error(`[deletePosition] 删除失败:`, res.message);
      }
    } catch (e: any) {
      error.value = e.message || '删除持仓失败';
      console.error(`[deletePosition] 请求错误:`, e);
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  async function fetchSignals(watchlist?: string) {
    isLoading.value = true;
    error.value = null;
    try {
      const endpoint = watchlist
        ? `${API_BASE}/signals?watchlist=${encodeURIComponent(watchlist)}`
        : `${API_BASE}/signals`;
      const res = await request<any>(endpoint);
      if (res.ok) {
        signals.value = res.data;
      }
    } catch (e: any) {
      error.value = e.message || '获取信号失败';
    } finally {
      isLoading.value = false;
    }
  }

  async function fetchRules() {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/rules`);
      if (res.ok) {
        rules.value = res.data;
      }
    } catch (e: any) {
      error.value = e.message || '获取规则失败';
    } finally {
      isLoading.value = false;
    }
  }

  async function updateRules(newRules: Record<string, any>) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/rules`, {
        method: 'POST',
        body: JSON.stringify(newRules),
      });
      if (res.ok) {
        rules.value = res.data;
        return true;
      }
    } catch (e: any) {
      error.value = e.message || '更新规则失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  async function generateReport() {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/report`);
      if (res.ok) {
        return res.data.report;
      }
    } catch (e: any) {
      error.value = e.message || '生成报告失败';
    } finally {
      isLoading.value = false;
    }
    return null;
  }

  async function pushToFeishu(webhookUrl?: string) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/push`, {
        method: 'POST',
        body: JSON.stringify({ webhook_url: webhookUrl }),
      });
      return res.ok;
    } catch (e: any) {
      error.value = e.message || '推送失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  async function fetchSuggestions(watchlist?: string) {
    isLoading.value = true;
    error.value = null;
    try {
      const endpoint = watchlist
        ? `${API_BASE}/suggestions?watchlist=${encodeURIComponent(watchlist)}`
        : `${API_BASE}/suggestions`;
      const res = await request<any>(endpoint);
      if (res.ok) {
        return res.data;
      }
    } catch (e: any) {
      error.value = e.message || '获取建议失败';
    } finally {
      isLoading.value = false;
    }
    return null;
  }

  /**
   * 更新信号配置
   * @param config - 信号配置
   */
  async function updateSignalConfig(config: SignalConfig) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/signal-config`, {
        method: 'POST',
        body: JSON.stringify(config),
      });
      if (res.ok) {
        signalConfig.value = config;
        return true;
      }
    } catch (e: any) {
      error.value = e.message || '更新信号配置失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  /**
   * 获取信号配置
   */
  async function fetchSignalConfig() {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/signal-config`);
      if (res.ok) {
        signalConfig.value = res.data;
      }
    } catch (e: any) {
      error.value = e.message || '获取信号配置失败';
    } finally {
      isLoading.value = false;
    }
  }

  /**
   * 获取持仓历史记录
   */
  async function fetchPositionHistory(positionId?: number) {
    isLoading.value = true;
    error.value = null;
    try {
      const endpoint = positionId
        ? `${API_BASE}/position-history?position_id=${positionId}`
        : `${API_BASE}/position-history`;
      const res = await request<any>(endpoint);
      if (res.ok) {
        positionHistory.value = res.data.history || [];
        positionHistoryStats.value = res.data.stats || null;
      }
    } catch (e: any) {
      error.value = e.message || '获取持仓历史失败';
    } finally {
      isLoading.value = false;
    }
  }

  /**
   * 添加持仓历史记录
   */
  async function addPositionHistory(history: PositionHistory) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/position-history`, {
        method: 'POST',
        body: JSON.stringify(history),
      });
      if (res.ok) {
        await fetchPositionHistory();
        return res.data.id;
      }
    } catch (e: any) {
      error.value = e.message || '添加持仓历史失败';
    } finally {
      isLoading.value = false;
    }
    return null;
  }

  /**
   * 删除持仓历史记录
   */
  async function deletePositionHistory(historyId: number) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/position-history/${historyId}`, { method: 'DELETE' });
      if (res.ok) {
        await fetchPositionHistory();
        return true;
      }
    } catch (e: any) {
      error.value = e.message || '删除持仓历史失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  /**
   * T2.7: 拉取交易记录分页数据
   *
   * 设计要点:
   *  - 默认 30 天窗口 / page=1 / page_size=50, 由后端兜底
   *  - 60s 前端超时; 失败抛 ApiError 供组件展示 ErrorState / TimeoutState
   *  - 成功但 total=0 → 不视为错误, 组件展示 EmptyState (demo_mode=true 时)
   *
   * @param params.start_date YYYY-MM-DD, 可选
   * @param params.end_date   YYYY-MM-DD, 可选
   * @param params.page       页码, 默认 1
   * @param params.page_size  每页条数, 默认 50, 最大 200 (后端硬限)
   * @returns TradeRecordsResponse 或 null (失败时)
   */
  async function fetchTrades(params?: {
    start_date?: string;
    end_date?: string;
    page?: number;
    page_size?: number;
    timeoutMs?: number;
  }): Promise<TradeRecordsResponse | null> {
    tradesLoading.value = true;
    tradesError.value = null;

    const query: Record<string, any> = {
      page: params?.page ?? 1,
      page_size: params?.page_size ?? 50,
    };
    if (params?.start_date) query.start_date = params.start_date;
    if (params?.end_date) query.end_date = params.end_date;
    tradesQueryParams.value = { ...query };

    const timeoutMs = params?.timeoutMs ?? 60000;
    tradesTimeoutMs.value = timeoutMs;

    const qs = new URLSearchParams(
      Object.entries(query).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => [k, String(v)])
    ).toString();
    const endpoint = `${API_BASE}/trades${qs ? `?${qs}` : ''}`;

    try {
      const res = await request<TradeRecordsResponse>(endpoint, { timeoutMs });

      if (res.ok) {
        const data = res.data;
        trades.value = data.items || [];
        tradesMeta.value = {
          total: data.total ?? 0,
          page: data.page ?? 1,
          page_size: data.page_size ?? 50,
          source: data.source ?? 'empty',
          demo_mode: !!data.demo_mode,
          start_date: data.start_date ?? '',
          end_date: data.end_date ?? '',
          elapsed_ms: res.elapsed_ms,
        };
        return data;
      }

      tradesError.value = res.message || '交易记录查询失败';
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    } catch (e: any) {
      if (e instanceof ApiError) {
        tradesError.value = tradesError.value || e.message;
        throw e;
      }
      tradesError.value = e?.message || '网络请求失败';
      throw new ApiError(
        e?.message || '网络请求失败',
        0,
        'NETWORK',
        { retryable: true, endpoint, detail: e },
      );
    } finally {
      tradesLoading.value = false;
    }
  }

  return {
    positions,
    signals,
    summary,
    industryDistribution,
    rules,
    isLoading,
    error,
    signalConfig,
    positionHistory,
    positionHistoryStats,
    // T2.7: 交易记录分页 state/action
    trades,
    tradesMeta,
    tradesLoading,
    tradesError,
    tradesQueryParams,
    tradesTimeoutMs,
    profitPositions,
    lossPositions,
    totalProfitPct,
    fetchPositions,
    fetchAnalysis,
    fetchDemoPortfolio,
    addPosition,
    updatePosition,
    deletePosition,
    fetchSignals,
    fetchRules,
    updateRules,
    generateReport,
    pushToFeishu,
    fetchSuggestions,
    updateSignalConfig,
    fetchSignalConfig,
    fetchPositionHistory,
    addPositionHistory,
    deletePositionHistory,
    fetchTrades,
  };
});

export interface WatchItem {
  id?: number;
  stock_code: string;
  stock_name: string;
  buy_target_price?: number;
  sell_target_price?: number;
  conditions?: Array<{
    key: string;
    enabled: boolean;
    params: Record<string, any>;
  }>;
  notes?: string;
  tags?: string[];
  is_active: boolean;
  feishu_notify: boolean;
  last_trigger_time?: string;
  last_trigger_condition?: string;
  last_notify_time?: string;
  created_at?: string;
  updated_at?: string;
  current_price?: number;
}

export interface FeishuWebhookConfig {
  webhook_url: string;
  masked_url: string;
  is_configured: boolean;
}

export interface SignalCheckResult {
  signals: Array<{
    stock_code: string;
    stock_name: string;
    current_price: number;
    condition_key: string;
    condition_name: string;
    message: string;
    severity: 'buy' | 'sell' | 'warning' | 'info';
  }>;
  notified: boolean;
  notification_count: number;
}

export const useWatchlistStore = defineStore('watchlist', () => {
  const watchlist = ref<WatchItem[]>([]);
  const feishuConfig = ref<FeishuWebhookConfig>({
    webhook_url: '',
    masked_url: '',
    is_configured: false
  });
  const isLoading = ref(false);
  const error = ref<string | null>(null);
  const lastCheckResult = ref<SignalCheckResult | null>(null);

  async function fetchWatchlist(activeOnly: boolean = true) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/watchlist?active_only=${activeOnly}`);
      if (res.ok) {
        watchlist.value = res.data;
      }
    } catch (e: any) {
      error.value = e.message || '获取监控列表失败';
    } finally {
      isLoading.value = false;
    }
  }

  async function addWatchlistItem(item: WatchItem) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/watchlist`, {
        method: 'POST',
        body: JSON.stringify(item),
      });
      if (res.ok) {
        await fetchWatchlist();
        return res.data.id;
      }
    } catch (e: any) {
      error.value = e.message || '添加监控股票失败';
    } finally {
      isLoading.value = false;
    }
    return null;
  }

  async function updateWatchlistItem(item: WatchItem) {
    if (!item.id) return false;
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/watchlist/${item.id}`, {
        method: 'PUT',
        body: JSON.stringify(item),
      });
      if (res.ok) {
        await fetchWatchlist();
        return true;
      }
    } catch (e: any) {
      error.value = e.message || '更新监控股票失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  async function deleteWatchlistItem(itemId: number) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/watchlist/${itemId}`, { method: 'DELETE' });
      if (res.ok) {
        await fetchWatchlist();
        return true;
      }
    } catch (e: any) {
      error.value = e.message || '删除监控股票失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  async function batchAddWatchlist(items: WatchItem[]) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/watchlist/batch`, {
        method: 'POST',
        body: JSON.stringify({ items }),
      });
      if (res.ok) {
        await fetchWatchlist();
        return res.data.added_count;
      }
    } catch (e: any) {
      error.value = e.message || '批量添加失败';
    } finally {
      isLoading.value = false;
    }
    return 0;
  }

  async function checkSignals(webhookUrl?: string, cooldownHours: number = 4) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/watchlist/check-signals`, {
        method: 'POST',
        body: JSON.stringify({
          webhook_url: webhookUrl,
          cooldown_hours: cooldownHours
        }),
      });
      if (res.ok) {
        lastCheckResult.value = res.data;
        return res.data;
      }
    } catch (e: any) {
      error.value = e.message || '检查信号失败';
    } finally {
      isLoading.value = false;
    }
    return null;
  }

  async function fetchFeishuConfig() {
    try {
      const res = await request<any>(`${API_BASE}/feishu-webhook`);
      if (res.ok) {
        feishuConfig.value = res.data;
      }
    } catch (e: any) {
      console.error('获取飞书配置失败:', e);
    }
  }

  async function updateFeishuConfig(webhookUrl: string) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/feishu-webhook`, {
        method: 'POST',
        body: JSON.stringify({ webhook_url: webhookUrl }),
      });
      if (res.ok) {
        await fetchFeishuConfig();
        return true;
      }
    } catch (e: any) {
      error.value = e.message || '保存飞书配置失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  async function testFeishuPush(webhookUrl?: string) {
    isLoading.value = true;
    error.value = null;
    try {
      const res = await request<any>(`${API_BASE}/feishu-webhook/test`, {
        method: 'POST',
        body: JSON.stringify({ webhook_url: webhookUrl }),
      });
      return res.ok;
    } catch (e: any) {
      error.value = e.message || '测试推送失败';
    } finally {
      isLoading.value = false;
    }
    return false;
  }

  return {
    watchlist,
    feishuConfig,
    isLoading,
    error,
    lastCheckResult,
    fetchWatchlist,
    addWatchlistItem,
    updateWatchlistItem,
    deleteWatchlistItem,
    batchAddWatchlist,
    checkSignals,
    fetchFeishuConfig,
    updateFeishuConfig,
    testFeishuPush
  };
});
