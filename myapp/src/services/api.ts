/**
 * 统一 API Client
 *
 * 设计目标:
 * 1. 统一错误/成功响应结构 (ApiResponse<T>), 便于前端组件统一渲染
 * 2. 内置 timeout + AbortController, 防止接口长时间挂起
 * 3. 保留向后兼容: fetch 完全失败时仍然 throw ApiError, 现有 try/catch 路径不破坏
 * 4. 暴露 useApiDebug() 调试缓冲, 供 ApiDebugPanel 面板与 window.__AQUA_API_DEBUG__ 使用
 * 5. apiService 内部从 ApiResponse.data 解包, 对外签名零变化
 */
import type {
  StrategyInfo,
  KlineData,
  KlineResponse,
  BacktestParams,
  ApiResponse as BackendApiResponse,
} from '../types/api';
import { useSocketIO } from '../composables/useSocketIO';

// 使用相对路径, 让 Vite 代理可以正确代理请求到后端
const API_BASE_URL = '/api';

/** 默认请求超时时间 (毫秒) */
const DEFAULT_TIMEOUT_MS = 15000;

/** 调试缓冲最大条数, 超过后按环形覆盖策略丢弃最旧记录 */
const DEBUG_BUFFER_MAX = 100;

/**
 * ApiError — 兼容既有错误对象, 扩展 retryable / endpoint / detail 字段
 *
 * 触发场景:
 *  - 网络断开 (fetch reject)
 *  - 超时 abort
 *  - 调用方主动 cancel
 */
export class ApiError extends Error {
  status?: number;
  code?: string;
  retryable: boolean;
  endpoint?: string;
  detail?: any;

  constructor(
    message: string,
    status?: number,
    code?: string,
    options?: { retryable?: boolean; endpoint?: string; detail?: any }
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.retryable = options?.retryable ?? false;
    this.endpoint = options?.endpoint;
    this.detail = options?.detail;
  }
}

/**
 * 统一响应结构 - 成功分支
 *  - ok=true 表达业务/传输都成功, 调用方直接取 data
 *  - elapsed_ms / endpoint 便于调试面板与超时提示使用
 */
export interface ApiSuccess<T> {
  ok: true;
  status: number;
  data: T;
  endpoint: string;
  elapsed_ms: number;
  meta?: Record<string, any>;
}

/**
 * 统一响应结构 - 失败分支
 *  - ok=false 但不 throw, 调用方根据状态码 / retryable 决定是否提示重试
 *  - code 透传后端业务错误码, 无则省略
 */
export interface ApiFailure {
  ok: false;
  status: number;
  message: string;
  endpoint: string;
  detail?: any;
  retryable: boolean;
  code?: string;
  elapsed_ms?: number;
}

/** 统一 ApiResponse 联合类型 */
export type ApiResult<T> = ApiSuccess<T> | ApiFailure;

/** request() 扩展选项 (覆盖 fetch 原生 RequestInit) */
export interface ApiRequestOptions extends Omit<RequestInit, 'signal'> {
  /** 超时毫秒, 默认 15000; 传 0 表示不超时 */
  timeoutMs?: number;
  /** 调用方主动传入的 AbortSignal, 与 timeoutMs 协同 */
  signal?: AbortSignal;
  /** 自定义 meta 字段, 写入 ApiSuccess.meta, 便于排查 */
  meta?: Record<string, any>;
}

/** 调试缓冲单条记录 */
export interface ApiDebugEntry {
  id: number;
  endpoint: string;
  method: string;
  status: number | string;
  ok: boolean;
  message?: string;
  code?: string;
  elapsed_ms: number;
  time: string;
  retryable?: boolean;
  meta?: Record<string, any>;
}

/** 调试缓冲订阅者回调签名 */
export type ApiDebugSubscriber = (entries: ApiDebugEntry[]) => void;

/**
 * ApiDebug 内部实现 - 单例环形缓冲
 *
 * 暴露给 useApiDebug() 包装层使用, 避免在模块加载时即污染 window
 */
class ApiDebugStore {
  private buffer: ApiDebugEntry[] = [];
  private subscribers: Set<ApiDebugSubscriber> = new Set();
  private nextId = 1;

  /** 推入一条记录 (环形覆盖, 最多 100 条) 并广播给订阅者 */
  push(entry: Omit<ApiDebugEntry, 'id' | 'time'>): void {
    const record: ApiDebugEntry = {
      ...entry,
      id: this.nextId++,
      time: new Date().toISOString(),
    };
    this.buffer.push(record);
    if (this.buffer.length > DEBUG_BUFFER_MAX) {
      this.buffer.shift();
    }
    this.emit();
  }

  /** 获取最近 N 条 (新→旧) */
  getRecent(limit = 10): ApiDebugEntry[] {
    const slice = this.buffer.slice(-limit);
    return slice.reverse();
  }

  /** 清空缓冲并广播 */
  clear(): void {
    this.buffer = [];
    this.emit();
  }

  /** 订阅变更, 返回取消订阅函数 */
  subscribe(fn: ApiDebugSubscriber): () => void {
    this.subscribers.add(fn);
    // 立即推送一次当前快照, 避免首次打开面板空白
    fn(this.getRecent(10));
    return () => {
      this.subscribers.delete(fn);
    };
  }

  private emit(): void {
    const snapshot = this.getRecent(10);
    this.subscribers.forEach((fn) => {
      try {
        fn(snapshot);
      } catch (e) {
        // 防御性: 订阅者抛错不能影响主流程
        // eslint-disable-next-line no-console
        console.error('[apiDebug] subscriber error:', e);
      }
    });
  }
}

const apiDebugStore = new ApiDebugStore();

/**
 * 暴露 ApiDebug 工具给业务代码 + 浏览器控制台调试使用
 *
 * 用法:
 *   const { getRecent, clear, subscribe } = useApiDebug()
 *   window.__AQUA_API_DEBUG__.getRecent(20)
 */
export function useApiDebug() {
  return {
    getRecent: (limit = 10) => apiDebugStore.getRecent(limit),
    clear: () => apiDebugStore.clear(),
    subscribe: (fn: ApiDebugSubscriber) => apiDebugStore.subscribe(fn),
  };
}

// 浏览器调试入口: window.__AQUA_API_DEBUG__.getRecent(20)
if (typeof window !== 'undefined') {
  (window as any).__AQUA_API_DEBUG__ = {
    getRecent: (limit?: number) => apiDebugStore.getRecent(limit ?? 10),
    clear: () => apiDebugStore.clear(),
  };
}

/**
 * 统一请求函数
 *
 * 行为约定:
 *  - 成功 (HTTP 2xx + 后端 success/ok) → 返回 ApiSuccess<T>
 *  - HTTP 4xx/5xx 或后端显式失败 → 返回 ApiFailure (不 throw)
 *  - 网络断开 / fetch reject / 超时 abort → throw ApiError (向后兼容)
 *  - 任意分支都会推入 ApiDebug 缓冲
 */
export async function request<T>(
  endpoint: string,
  options: ApiRequestOptions = {}
): Promise<ApiResult<T>> {
  const url = `${API_BASE_URL}${endpoint}`;
  const method = (options.method || 'GET').toUpperCase();

  // 合并 headers, 默认 JSON
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> | undefined),
  };

  // 构造 AbortController, 同时支持 timeout 与外部 signal
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  let timedOut = false;
  let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
  if (timeoutMs > 0) {
    timeoutHandle = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
  }

  // 外部 signal 联动: 任一上游 abort 都会中止 fetch
  const externalSignal = options.signal;
  const onExternalAbort = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener('abort', onExternalAbort);
    }
  }

  const startTime = performance.now();

  const recordAndReturn = (
    result: ApiResult<T>,
    elapsedMs: number
  ): ApiResult<T> => {
    const elapsed = Math.round(elapsedMs);
    apiDebugStore.push({
      endpoint,
      method,
      status: result.status,
      ok: result.ok,
      message: result.ok ? undefined : result.message,
      code: result.ok ? undefined : result.code,
      elapsed_ms: elapsed,
      retryable: result.ok ? false : result.retryable,
      meta: result.ok ? result.meta : undefined,
    });
    return result;
  };

  const cleanup = () => {
    if (timeoutHandle !== undefined) clearTimeout(timeoutHandle);
    if (externalSignal) externalSignal.removeEventListener('abort', onExternalAbort);
  };

  try {
    const response = await fetch(url, {
      ...options,
      headers,
      signal: controller.signal,
    });

    if (!response.ok) {
      // 尝试解析后端错误结构
      const errorData = await response.json().catch(() => ({}));
      const message =
        (errorData && (errorData.error || errorData.message)) ||
        `HTTP ${response.status}: ${response.statusText}`;
      const failure: ApiFailure = {
        ok: false,
        status: response.status,
        message,
        endpoint,
        detail: errorData,
        retryable: response.status >= 500 || response.status === 408 || response.status === 429,
        code: errorData?.code,
      };
      cleanup();
      return recordAndReturn(failure, performance.now() - startTime);
    }

    // 解析后端响应 (可能为多种 shape, 兼容 success/ok/data/result)
    const raw: BackendApiResponse<T> & {
      ok?: boolean;
      status?: string | number;
      result?: T;
      error?: string;
      message?: string;
      code?: string;
      meta?: Record<string, any>;
    } = await response.json().catch(() => ({} as any));

    const explicitFailure =
      raw.success === false ||
      raw.ok === false ||
      raw.status === 'error' ||
      raw.status === 'failed' ||
      (typeof raw.status === 'number' && raw.status >= 400);
    if (explicitFailure) {
      const failure: ApiFailure = {
        ok: false,
        status: response.status,
        message: raw.error || raw.message || '请求失败',
        endpoint,
        detail: raw,
        retryable: response.status >= 500 || response.status === 408,
        code: raw.code,
      };
      cleanup();
      return recordAndReturn(failure, performance.now() - startTime);
    }

    // 解包 data/result
    let payload: T;
    if (raw && Object.prototype.hasOwnProperty.call(raw, 'data')) {
      payload = raw.data as T;
    } else if (raw && Object.prototype.hasOwnProperty.call(raw, 'result')) {
      payload = raw.result as T;
    } else {
      payload = raw as unknown as T;
    }

    const success: ApiSuccess<T> = {
      ok: true,
      status: response.status,
      data: payload,
      endpoint,
      elapsed_ms: Math.round(performance.now() - startTime),
      meta: options.meta,
    };
    cleanup();
    return recordAndReturn(success, performance.now() - startTime);
  } catch (error) {
    cleanup();
    // 超时 abort
    if (timedOut) {
      const err = new ApiError(
        `请求超时 (${timeoutMs}ms): ${endpoint}`,
        408,
        'TIMEOUT',
        { retryable: true, endpoint, detail: { timeoutMs } }
      );
      apiDebugStore.push({
        endpoint,
        method,
        status: 408,
        ok: false,
        message: err.message,
        code: 'TIMEOUT',
        elapsed_ms: Math.round(performance.now() - startTime),
        retryable: true,
      });
      throw err;
    }
    // 外部 signal abort
    if (externalSignal?.aborted) {
      const err = new ApiError(`请求已取消: ${endpoint}`, 0, 'ABORTED', {
        retryable: false,
        endpoint,
      });
      apiDebugStore.push({
        endpoint,
        method,
        status: 0,
        ok: false,
        message: err.message,
        code: 'ABORTED',
        elapsed_ms: Math.round(performance.now() - startTime),
        retryable: false,
      });
      throw err;
    }
    // 其它网络错误
    const err = new ApiError(
      error instanceof Error ? error.message : '网络请求失败',
      0,
      'NETWORK',
      { retryable: true, endpoint, detail: error }
    );
    apiDebugStore.push({
      endpoint,
      method,
      status: 0,
      ok: false,
      message: err.message,
      code: 'NETWORK',
      elapsed_ms: Math.round(performance.now() - startTime),
      retryable: true,
    });
    throw err;
  }
}

export const apiService = {
  /**
   * 获取策略列表 — 404 时降级返回空数组, 符合项目"字段缺失降级"原则
   */
  async getStrategies(): Promise<StrategyInfo[]> {
    const res = await request<StrategyInfo[]>('/strategies');
    if (!res.ok) {
      if (res.status === 404) return [];
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    return res.data ?? [];
  },

  /**
   * 同步回测 - 直接返回后端业务数据
   */
  async runBacktest(params: BacktestParams): Promise<any> {
    const res = await request<any>('/run_backtest', {
      method: 'POST',
      body: JSON.stringify(params),
      meta: { kind: 'runBacktest', strategy: params.strategy_name },
    });
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    return res.data;
  },

  /**
   * T2.5 真实回测入口 - POST /api/backtest/run
   *
   * 后端契约:
   *   - 30s 内能完成 → 同步返回完整 stats / equity_curve
   *   - 超过 30s → 返回 job_id, 前端轮询 /api/backtest/jobs/<id>
   *   - 失败时按 BACKTEST_* 业务码分类, 字段缺失一律降级
   */
  async runBacktestJob(params: {
    strategy_id: string
    start_date: string
    end_date: string
    initial_capital?: number
    params?: Record<string, any>
    timeoutMs?: number
  }): Promise<{
    success: boolean
    mode: 'sync' | 'async'
    job_id: string | null
    status: 'pending' | 'running' | 'success' | 'failed' | 'queued'
    progress: number
    stats: Record<string, any> | null
    equity_curve: Array<{ date: string; equity: number }> | null
    message: string
    endpoint: string
    elapsed_ms: number
    error?: {
      code: string
      message: string
      detail?: string
      retryable: boolean
      suggestion?: string
      endpoint?: string
    } | null
  }> {
    const res = await request<{
      job_id: string
      status: string
      progress: number
      stats?: Record<string, any>
      equity_curve?: Array<{ date: string; equity: number }>
      message?: string
      endpoint?: string
      elapsed_ms?: number
    }>('/backtest/run', {
      method: 'POST',
      body: JSON.stringify({
        strategy_id: params.strategy_id,
        start_date: params.start_date,
        end_date: params.end_date,
        initial_capital: params.initial_capital ?? 1_000_000,
        params: params.params ?? {},
      }),
      timeoutMs: params.timeoutMs ?? 35_000, // 30s 后端 + 5s 网络缓冲
      meta: { kind: 'runBacktestJob', strategy: params.strategy_id },
    });
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    const data = (res.data as any) || {};
    const meta = (res as any).meta || {};
    return {
      success: true,
      mode: meta.mode || 'sync',
      job_id: data.job_id || null,
      status: data.status || 'pending',
      progress: data.progress ?? 0,
      stats: data.stats || null,
      equity_curve: data.equity_curve || null,
      message: data.message || '',
      endpoint: data.endpoint || '/api/backtest/run',
      elapsed_ms: data.elapsed_ms ?? res.elapsed_ms ?? 0,
      error: (res as any).error || null,
    };
  },

  /**
   * T2.5 轮询 - GET /api/backtest/jobs/<id>
   *
   * 用于异步模式下前端每 3s 轮询, 最长 5 分钟
   */
  async getBacktestJob(jobId: string): Promise<{
    success: boolean
    job_id: string
    status: 'pending' | 'running' | 'success' | 'failed'
    progress: number
    message: string
    stats: Record<string, any> | null
    equity_curve: Array<{ date: string; equity: number }> | null
    endpoint: string
    elapsed_ms: number
    error: any
  }> {
    const res = await request<{
      job_id: string
      status: string
      progress: number
      message?: string
      stats?: Record<string, any>
      equity_curve?: Array<{ date: string; equity: number }>
      endpoint?: string
      elapsed_ms?: number
      error?: any
    }>(`/backtest/jobs/${encodeURIComponent(jobId)}`, {
      method: 'GET',
      timeoutMs: 10_000,
      meta: { kind: 'getBacktestJob', job_id: jobId },
    });
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    const data = (res.data as any) || {};
    return {
      success: true,
      job_id: data.job_id || jobId,
      status: (data.status as any) || 'pending',
      progress: data.progress ?? 0,
      message: data.message || '',
      stats: data.stats || null,
      equity_curve: data.equity_curve || null,
      endpoint: data.endpoint || '/api/backtest/run',
      elapsed_ms: data.elapsed_ms ?? 0,
      error: data.error || null,
    };
  },

  /**
   * 流式回测 - 支持实时更新收益曲线
   * @param params 回测参数
   * @param callbacks 回调函数对象
   * @returns Promise 在回测完成时 resolve
   */
  async runStreamingBacktest(
    params: BacktestParams & { initial_capital?: number; commission?: number; slippage?: number },
    callbacks: {
      onStart?: (data: any) => void;
      onProgress?: (data: any) => void;
      onDailyUpdate?: (data: any) => void;
      onNewTrade?: (data: any) => void;
      onMetricsUpdate?: (data: any) => void;
      onRiskUpdate?: (data: any) => void;
      onComplete?: (data: any) => void;
      onError?: (error: any) => void;
    }
  ): Promise<any> {
    const { emitEvent, onEvent } = useSocketIO();

    return new Promise((resolve, reject) => {
      let result: any = null;
      let hasError = false;

      // 监听回测开始
      const unsubscribeStart = onEvent('backtest_start', (data) => {
        // eslint-disable-next-line no-console
        console.log('[流式回测] 开始:', data);
        callbacks.onStart?.(data);
      });

      // 监听进度更新
      const unsubscribeProgress = onEvent('progress', (data) => {
        callbacks.onProgress?.(data);
      });

      // 监听每日权益曲线更新 - 这是流式更新的关键
      const unsubscribeDailyUpdate = onEvent('daily_update', (data) => {
        // 处理 MsgPack 编码的数据
        if (data._msgpack && data._data) {
          try {
            // 解码 base64 数据
            const binaryString = atob(data._data);
            const bytes = new Uint8Array(binaryString.length);
            for (let i = 0; i < binaryString.length; i++) {
              bytes[i] = binaryString.charCodeAt(i);
            }
            // 这里需要 msgpack 解码, 暂时直接传递原始数据
            callbacks.onDailyUpdate?.({ date: data.date, equity: data.equity });
          } catch (e) {
            callbacks.onDailyUpdate?.(data);
          }
        } else {
          callbacks.onDailyUpdate?.(data);
        }
      });

      // 监听新交易
      const unsubscribeNewTrade = onEvent('new_trade', (data) => {
        callbacks.onNewTrade?.(data);
      });

      // 监听指标更新
      const unsubscribeMetrics = onEvent('metrics_update', (data) => {
        callbacks.onMetricsUpdate?.(data);
      });

      // 监听风险更新
      const unsubscribeRisk = onEvent('risk_update', (data) => {
        callbacks.onRiskUpdate?.(data);
      });

      // 监听完成
      const unsubscribeComplete = onEvent('stream_complete', (data) => {
        // eslint-disable-next-line no-console
        console.log('[流式回测] 完成:', data);
        // 清理所有监听器
        unsubscribeStart();
        unsubscribeProgress();
        unsubscribeDailyUpdate();
        unsubscribeNewTrade();
        unsubscribeMetrics();
        unsubscribeRisk();
        unsubscribeComplete();
        unsubscribeError();
        unsubscribeCancelled();

        if (!hasError) {
          resolve(result || data);
        }
      });

      // 监听错误
      const unsubscribeError = onEvent('backtest_error', (error) => {
        // eslint-disable-next-line no-console
        console.error('[流式回测] 错误:', error);
        hasError = true;
        callbacks.onError?.(error);
        reject(new ApiError(error.message || '回测失败', error.status, error.code, { retryable: true }));
      });

      // 监听取消
      const unsubscribeCancelled = onEvent('backtest_cancelled', (data) => {
        // eslint-disable-next-line no-console
        console.log('[流式回测] 已取消:', data);
        reject(new ApiError('回测已取消', 0, 'CANCELLED', { retryable: false }));
      });

      // 发送流式回测请求
      emitEvent('run_streaming_backtest', {
        strategy_name: params.strategy_name,
        start_date: params.start_date,
        end_date: params.end_date,
        initial_capital: params.initial_capital || 1000000,
        commission: params.commission || 0.0003,
        slippage: params.slippage || 0.001,
      });
    });
  },

  /**
   * K线数据走 Socket.IO, 不走 fetch request; 30s 自管超时
   */
  async getKlineData(
    symbolCode: string,
    startDate: string,
    endDate: string
  ): Promise<{ data: KlineData[]; perf?: { query_ms: number; total_ms: number } }> {
    const { emitEvent, onEvent } = useSocketIO();

    return new Promise((resolve, reject) => {
      const requestId = `kline_${Date.now()}_${Math.random()}`;
      let timeout: ReturnType<typeof setTimeout>;

      const unsubscribe = onEvent('kline_data', (response: KlineResponse & { perf?: { query_ms: number; total_ms: number } }) => {
        // eslint-disable-next-line no-console
        console.log('[API] 收到 kline_data 事件:', {
          response_request_id: response?.request_id,
          expected_request_id: requestId,
          matched: response?.request_id === requestId,
          has_data: !!response?.data,
          data_length: response?.data?.length,
          has_error: !!response?.error,
          perf: response?.perf
        });

        if (response.request_id === requestId) {
          clearTimeout(timeout);
          unsubscribe();

          if (response.error) {
            reject(new ApiError(response.error, response.status, response.code, { retryable: true }));
          } else {
            const data = response.data || [];
            // eslint-disable-next-line no-console
            console.log('[API] 解析 K 线数据成功, 长度:', data.length);
            resolve({
              data,
              perf: response.perf
            });
          }
        } else {
          // eslint-disable-next-line no-console
          console.warn('[API] request_id 不匹配, 忽略此响应:', {
            expected: requestId,
            received: response.request_id
          });
        }
      });

      timeout = setTimeout(() => {
        // eslint-disable-next-line no-console
        console.error('[API] K线数据请求超时:', {
          request_id: requestId,
          symbol_code: symbolCode,
          start_date: startDate,
          end_date: endDate
        });
        unsubscribe();
        reject(new ApiError('K线数据请求超时', 408, 'TIMEOUT', { retryable: true, detail: { symbolCode, startDate, endDate } }));
      }, 30000);

      emitEvent('request_kline', {
        request_id: requestId,
        symbol_code: symbolCode,
        start_date: startDate,
        end_date: endDate,
      });
    });
  },

  async restartBackend(): Promise<void> {
    const res = await request<void>('/restart-backend', { method: 'POST' });
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
  },

  async generateStrategy(
    description: string,
    name?: string,
    options?: { use_fallback?: boolean; tags?: string[] }
  ): Promise<{
    filename: string;
    filepath?: string;
    strategy_id?: string;
    code?: string;
    fallback?: boolean;
    reload_ok?: boolean;
    import_ok?: boolean;
    smoke_ok?: boolean | null;
    import_error?: string;
    smoke_error?: string;
    message: string;
  }> {
    const res = await request<{
      filename: string;
      filepath?: string;
      strategy_id?: string;
      code?: string;
      fallback?: boolean;
      reload_ok?: boolean;
      import_ok?: boolean;
      smoke_ok?: boolean | null;
      import_error?: string;
      smoke_error?: string;
      message: string;
    }>('/strategies/generate', {
      method: 'POST',
      body: JSON.stringify({
        description,
        name: name || 'AI策略',
        use_fallback: options?.use_fallback ?? true,
        tags: options?.tags,
      }),
    });
    if (!res.ok) {
      // 失败时尝试读取 data.import_error / data.smoke_error 增强提示
      const detail = (res as any).detail?.data || {};
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: { ...(res.detail || {}), import_error: detail.import_error, smoke_error: detail.smoke_error },
      });
    }
    return res.data as any;
  },

  /**
   * Smoke 验证 — 对指定策略做最小烟雾测试 (干跑)
   * @param strategyId 策略ID
   * @param filePath 可选, 指定策略文件绝对路径
   */
  async smokeStrategy(
    strategyId: string,
    filePath?: string
  ): Promise<{
    strategy_id: string;
    filepath?: string;
    import_ok: boolean;
    smoke_ok: boolean;
    import_error?: string;
    smoke_error?: string;
    message?: string;
  }> {
    const res = await request<{
      strategy_id: string;
      filepath?: string;
      import_ok: boolean;
      smoke_ok: boolean;
      import_error?: string;
      smoke_error?: string;
      message?: string;
    }>(`/strategies/${encodeURIComponent(strategyId)}/smoke`, {
      method: 'POST',
      body: JSON.stringify(filePath ? { file_path: filePath } : {}),
    });
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    return res.data;
  },

  async reloadStrategies(options?: {
    strategy_id?: string;
    file_path?: string;
    refresh_all?: boolean;
  }): Promise<{ action: string; message: string; strategies?: string[]; count?: number }> {
    const res = await request<{ action: string; message: string; strategies?: string[]; count?: number }>('/strategies/reload', {
      method: 'POST',
      body: JSON.stringify(options || {}),
    });
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    return res.data;
  },

  async getDiscoveredStrategies(): Promise<{ strategies: Array<{ id: string; module_path: string }>; count: number }> {
    const res = await request<{ strategies: Array<{ id: string; module_path: string }>; count: number }>('/strategies/discovered');
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    return res.data;
  },

  /**
   * 保存策略文件
   * @param params 策略参数
   * @returns 保存结果
   */
  async saveStrategy(params: {
    name: string;
    description?: string;
    code: string;
    temp?: boolean;
  }): Promise<{ success: boolean; filename?: string; message?: string }> {
    const res = await request<{ success: boolean; filename?: string; message?: string }>('/strategies/save', {
      method: 'POST',
      body: JSON.stringify(params),
    });
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    return res.data;
  },

  /**
   * 获取单只股票的 6 维度财务雷达数据
   *
   * 响应结构 (与 server/routes/finance_routes.py 保持一致):
   *   {
   *     symbol, name,
   *     dimensions: {
   *       growth:        { value: number|null, fields: { [fieldName]: { value, source, formula, ... } } },
   *       profitability: { ... },
   *       valuation:     { ... },
   *       cashflow:      { ... },
   *       solvency:      { ... },
   *       quality:       { ... },
   *     },
   *     has_data, demo_mode,
   *     missing_dimensions: string[],
   *     present_dimensions: string[],
   *   }
   *
   * 行为约定:
   *   - 字段缺失一律降级为 null (前端不画假图)
   *   - 全部维度缺失时 has_data=false
   *   - demo_mode=true 时表示数据源未完全接入, 前端展示 "Demo / 数据源未接入" 角标
   *   - 4xx/5xx 抛 ApiError, 字段保留 endpoint / status / code 便于排查
   */
  async getFinanceRadar(symbol: string): Promise<{
    symbol: string;
    name: string | null;
    dimensions: Record<string, {
      value: number | null;
      fields: Record<string, {
        value: number | null;
        current?: number | null;
        sample_size?: number;
        source: string;
        formula: string;
      }>;
      trade_date?: string | null;
    }>;
    has_data: boolean;
    demo_mode: boolean;
    missing_dimensions: string[];
    present_dimensions: string[];
  }> {
    const endpoint = `/finance/radar?symbol=${encodeURIComponent(symbol)}`;
    const res = await request<any>(endpoint, {
      meta: { kind: 'getFinanceRadar', symbol },
    });
    if (!res.ok) {
      throw new ApiError(res.message, res.status, res.code, {
        retryable: res.retryable,
        endpoint: res.endpoint,
        detail: res.detail,
      });
    }
    // 兼容后端将 data 直接挂在根 (research_routes 风格) 的情况
    const payload = (res.data && typeof res.data === 'object' && 'dimensions' in res.data)
      ? res.data
      : res.data;
    return payload;
  },
};
