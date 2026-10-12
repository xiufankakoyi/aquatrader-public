import { ref, onUnmounted } from 'vue';
import { useSocketIO } from './useSocketIO';
import { subscribe } from '../api/backtestApi';
import type { BacktestEvent } from '../types/api';
import type { Trade, HoldingPeriod } from '../types/backtest';
import { useBacktestStore } from '../store/backtestStore';
import { useErrorStore } from '../store/errorStore';
import { useErrorService } from '../services/errorService';
import { ERROR_CODES } from '../types/error';

export interface StreamingBacktestOptions {
  onStart?: () => void;
  onProgress?: (progress: number) => void;
  onComplete?: () => void;
  onError?: (error: Error) => void;
  onCancel?: () => void;
}

export function useStreamingBacktest() {
  const { emitEvent, connect, status: socketStatus } = useSocketIO();
  const backtestStore = useBacktestStore();
  const errorStore = useErrorStore();
  const {
    createError,
    parseBackendError,
    createNetworkError,
    createSocketError,
    validateBacktestParams
  } = useErrorService();

  const isRunning = ref(false);
  const progress = ref(0);
  const error = ref<Error | null>(null);
  const currentParams = ref<any>(null);

  let unsubscribe: (() => void) | null = null;
  let hasCompleted = false;

  function start(params: any, options: StreamingBacktestOptions = {}) {
    if (isRunning.value) {
      const validationError = createError(
        ERROR_CODES.FRONTEND_INVALID_PARAMS,
        { strategyName: params.strategy_name },
        { message: '回测已在运行中' },
        '请等待当前回测完成后再启动新的回测'
      );
      errorStore.setError(validationError);
      throw new Error('回测已在运行中');
    }

    const paramError = validateBacktestParams(params);
    if (paramError) {
      errorStore.setError(paramError);
      options.onError?.(new Error(paramError.message));
      return;
    }

    if (!navigator.onLine) {
      const networkError = createNetworkError({
        strategyName: params.strategy_name,
        startDate: params.start_date,
        endDate: params.end_date
      });
      errorStore.setError(networkError);
      options.onError?.(new Error(networkError.message));
      return;
    }

    currentParams.value = params;

    // 使用相对路径 ''，让 Socket.IO 自动使用当前域名
    // 这样 Vite 代理可以正确代理 /socket.io 请求到后端
    connect('');

    hasCompleted = false;
    error.value = null;
    isRunning.value = true;
    progress.value = 0;

    backtestStore.setRunning(true);
    backtestStore.setProgress(0);
    backtestStore.clearBacktestData();

    backtestStore.setLastRunParams({
      strategyName: params.strategy_name,
      startDate: params.start_date,
      endDate: params.end_date,
      benchmarkCode: params.benchmark_code || undefined
    });

    unsubscribe = subscribe((event: BacktestEvent) => {
      handleEvent(event, options);
    });

    emitEvent('run_streaming_backtest', params);
    console.log('[useStreamingBacktest] 已发送 run_streaming_backtest:', params);
    options.onStart?.();
  }

  function handleEvent(event: BacktestEvent, options: StreamingBacktestOptions) {
    console.log('[useStreamingBacktest] 收到事件:', event.type, event.data ? '有数据' : '无数据', typeof event.data);
    
    switch (event.type) {
      case 'backtest_start':
        backtestStore.clearBacktestData();
        backtestStore.setRunning(true);
        backtestStore.setProgress(0);
        break;

      case 'daily_equity':
        if (event.data) {
          console.log('[daily_equity] 原始数据:', JSON.stringify(event.data).substring(0, 200));
          // 处理 MsgPack 格式数据
          let data = event.data;
          if (data._msgpack && data._data) {
            console.log('[daily_equity] 检测到 MsgPack 数据，需要解包');
            // 数据已经在 backtestApi.ts 中解包了，这里直接使用
            data = data._data;
          }
          // 兼容多种字段名: equity 或 strategyReturn
          const equity = data.equity ?? data.strategyReturn;
          const benchmarkEquity = data.benchmark_equity ?? data.benchmarkReturn;
          console.log('[daily_equity] 解析后:', { date: data.date, equity, benchmarkEquity });
          if (data.date && equity !== undefined) {
            backtestStore.addEquityPoints([{ date: data.date, equity }]);
          }
          if (data.date && benchmarkEquity !== undefined) {
            backtestStore.addBenchmarkPoints([{ date: data.date, equity: benchmarkEquity }]);
          }
        }
        break;

      case 'new_trade':
        if (event.data) {
          console.log('[new_trade] 数据:', event.data);
          const trade: Trade = {
            id: event.data.id || `${event.data.date}-${event.data.symbol}-${event.data.action}`,
            symbol: event.data.symbol || '',
            symbolCode: event.data.symbolCode || event.data.symbol || '',
            date: event.data.date,
            action: event.data.action === 'buy' ? 'buy' : 'sell',
            price: event.data.price || 0,
            quantity: event.data.quantity || 0,
            value: (event.data.price || 0) * (event.data.quantity || 0),
            profitLoss: event.data.profitLoss ?? event.data.profit_loss ?? 0,
          };
          backtestStore.addTrades([trade]);
        }
        break;

      case 'final_metrics':
        if (event.data && !hasCompleted) {
          console.log('[final_metrics] 数据:', event.data);
          const metrics = {
            totalReturn: event.data.totalReturn ?? 0,
            annualizedReturn: event.data.annualizedReturn ?? 0,
            annualReturn: event.data.annualizedReturn ?? 0,
            maxDrawdown: event.data.maxDrawdown ?? 0,
            sharpeRatio: event.data.sharpeRatio ?? 0,
            sortinoRatio: event.data.sortinoRatio ?? 0,
            volatility: event.data.volatility ?? 0,
            winRate: event.data.winRate ?? 0,
            profitFactor: event.data.profitFactor ?? 0,
            profitLossRatio: event.data.profitFactor ?? 0,
            tradesCount: event.data.tradesCount ?? 0,
            totalTrades: event.data.tradesCount ?? 0,
            avgHoldingDays: event.data.avgHoldingDays ?? 0,
            calmarRatio: event.data.calmarRatio ?? 0,
            benchmarkReturn: event.data.benchmarkReturn ?? 0,
            rejectedOrders: event.data.rejectedOrders,
            slippageCost: event.data.slippageCost,
            filterStats: event.data.filterStats,
          };
          backtestStore.setMetrics(metrics);
        }
        break;

      case 'stream_complete':
        if (!hasCompleted) {
          console.log('[stream_complete] 数据:', event.data);
          hasCompleted = true;
          
          if (event.data) {
            // 处理权益曲线
            if (event.data.equityCurve && Array.isArray(event.data.equityCurve)) {
              console.log('[stream_complete] 权益曲线点数:', event.data.equityCurve.length);
              backtestStore.addEquityPoints(event.data.equityCurve);
            }
            
            // 【新增】处理基准曲线
            if (event.data.benchmarkCurve && Array.isArray(event.data.benchmarkCurve)) {
              console.log('[stream_complete] 基准曲线点数:', event.data.benchmarkCurve.length);
              backtestStore.addBenchmarkPoints(event.data.benchmarkCurve);
            }
            
            // 【新增】处理月度收益
            if (event.data.monthlyReturns && Array.isArray(event.data.monthlyReturns)) {
              console.log('[stream_complete] 月度收益数:', event.data.monthlyReturns.length);
              backtestStore.setMonthlyReturns(event.data.monthlyReturns);
            }
            
            // 处理交易记录
            if (event.data.trades && Array.isArray(event.data.trades)) {
              console.log('[stream_complete] 交易记录数:', event.data.trades.length);
              const trades: Trade[] = event.data.trades.map((t: any) => ({
                id: t.id || `${t.date}-${t.symbol || t.code}-${t.action}`,
                symbol: t.symbol || t.code || '',
                symbolCode: t.symbolCode || t.symbol || t.code || '',
                date: t.date,
                action: t.action === 'buy' ? 'buy' : 'sell',
                price: t.price || 0,
                quantity: t.quantity || t.shares || 0,
                value: (t.price || 0) * (t.quantity || t.shares || 0),
                profitLoss: t.profitLoss ?? t.profit_loss ?? 0,
              }));
              backtestStore.addTrades(trades);
            }
            
            // 处理指标
            const metrics = {
              totalReturn: event.data.totalReturn ?? 0,
              annualizedReturn: event.data.annualizedReturn ?? 0,
              annualReturn: event.data.annualizedReturn ?? 0,
              maxDrawdown: event.data.maxDrawdown ?? 0,
              sharpeRatio: event.data.sharpeRatio ?? 0,
              sortinoRatio: event.data.sortinoRatio ?? 0,
              volatility: event.data.volatility ?? 0,
              winRate: event.data.winRate ?? 0,
              profitFactor: event.data.profitFactor ?? 0,
              profitLossRatio: event.data.profitFactor ?? 0,
              tradesCount: event.data.totalTrades ?? event.data.tradesCount ?? 0,
              totalTrades: event.data.totalTrades ?? event.data.tradesCount ?? 0,
              avgHoldingDays: event.data.avgHoldingDays ?? 0,
              calmarRatio: event.data.calmarRatio ?? 0,
              benchmarkReturn: event.data.benchmarkReturn ?? 0,
              rejectedOrders: event.data.rejectedOrders,
              slippageCost: event.data.slippageCost,
              filterStats: event.data.filterStats,
            };
            backtestStore.setMetrics(metrics);
          }
          
          backtestStore.setRunning(false);
          backtestStore.setProgress(100);
          backtestStore.persistToStorage();
          isRunning.value = false;
          progress.value = 100;
          options.onComplete?.();
        }
        break;

      case 'progress':
        if (event.data && typeof event.data.progress === 'number') {
          progress.value = event.data.progress;
          backtestStore.setProgress(event.data.progress);
          options.onProgress?.(event.data.progress);
        }
        break;

      case 'error':
        const errorMsg = event.data?.message || '回测发生错误';
        error.value = new Error(errorMsg);
        backtestStore.setRunning(false);
        isRunning.value = false;
        options.onError?.(new Error(errorMsg));
        break;
    }
  }

  function stop() {
    if (unsubscribe) {
      unsubscribe();
      unsubscribe = null;
    }
    isRunning.value = false;
    progress.value = 0;
    backtestStore.setRunning(false);
  }

  onUnmounted(() => {
    stop();
  });

  return {
    start,
    stop,
    isRunning,
    progress,
    error
  };
}
