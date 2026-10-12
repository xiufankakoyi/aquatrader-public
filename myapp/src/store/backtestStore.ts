import { defineStore } from 'pinia';
import { ref, computed, shallowRef } from 'vue';
import type { Metrics, MonthlyReturn, Trade } from '../types/backtest';

interface HoldingPeriod {
  positionId: string;
  symbolCode: string;
  symbolName: string;
  entryDate: string;
  exitDate: string;
  entryPrice?: number;
  exitPrice?: number;
  quantity?: number;
  profit?: number;
  holdingDays?: number;
}

interface LastRunParams {
  strategyName: string;
  startDate: string;
  endDate: string;
  benchmarkCode?: string;
}

const STORAGE_KEY = 'quantflow.backtest.v1';
const HISTORY_KEY = 'backtest_history';
// 避免 localStorage 爆满后持续疯狂写入：一旦检测到配额超限，就关闭历史写入
let historyDisabled = false;

export const useBacktestStore = defineStore('backtest', () => {
  const equitySeries = ref<Array<{ date: string; equity: number }>>([]);
  const benchmarkEquitySeries = ref<Array<{ date: string; equity: number }>>([]);
  const metrics = ref<Metrics | null>(null);
  const monthlyReturns = shallowRef<MonthlyReturn[]>([]);
  const holdingPeriods = shallowRef<HoldingPeriod[]>([]);
  const trades = shallowRef<Trade[]>([]);
  const lastRunParams = ref<LastRunParams | null>(null);
  const lastUpdated = ref<string>('');
  const running = ref<boolean>(false);
  const progress = ref<number>(0);
  const isInitializing = ref<boolean>(false);
  const playbackCursor = ref<string>(''); // Current playback date (YYYY-MM-DD)
  const playbackMode = ref<boolean>(false); // Whether playback mode is active
  const playbackSpeed = ref<number>(1); // Playback speed multiplier
  const playbackSnap = ref<boolean>(true); // Whether to auto-scroll trade table (Snap Effect)
  const excludedTradeIds = ref<Set<string>>(new Set()); // IDs of trades "un-done" in sandbox mode
  const autoExcludeAlphaLoss = ref<boolean>(false); // NEW: Auto-exclude trades where stock fell more than benchmark

  // CHANGED: 使用 localStorage 替代 sessionStorage，实现冷存储
  function hydrateFromStorage() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (!stored) return;
      const data = JSON.parse(stored);
      if (data.equitySeries) equitySeries.value = data.equitySeries;
      if (data.benchmarkEquitySeries) benchmarkEquitySeries.value = data.benchmarkEquitySeries;
      if (data.metrics) metrics.value = data.metrics;
      if (data.monthlyReturns) monthlyReturns.value = data.monthlyReturns;
      if (data.holdingPeriods) holdingPeriods.value = data.holdingPeriods;
      if (data.trades) trades.value = data.trades;
      if (data.lastRunParams) lastRunParams.value = data.lastRunParams;
      if (data.lastUpdated) lastUpdated.value = data.lastUpdated;
      // CHANGED: 不恢复 running 和 progress 状态，因为这些是临时状态，不应该被持久化
      // 初始状态应该始终是 false/0
      running.value = false;
      progress.value = 0;
      isInitializing.value = false;
    } catch (e) {
    }
  }

  function persistToStorage() {
    try {
      const data = {
        equitySeries: equitySeries.value,
        benchmarkEquitySeries: benchmarkEquitySeries.value,
        metrics: metrics.value,
        monthlyReturns: monthlyReturns.value,
        holdingPeriods: holdingPeriods.value,
        trades: trades.value,
        lastRunParams: lastRunParams.value,
        lastUpdated: lastUpdated.value
        // CHANGED: 不保存 running 和 progress 状态，因为这些是临时状态
        // running: running.value,
        // progress: progress.value
      };
      // CHANGED: 使用 localStorage 实现冷存储
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch (e) {
    }
  }

  // CHANGED: 保存回测结果到历史记录
  function saveToHistory() {
    if (historyDisabled) {
      return;
    }
    try {
      const history = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');

      // 这里只保存【精简版】历史记录，避免把整条权益曲线和所有成交明细塞进 localStorage
      const historyRecord = {
        id: `backtest_${Date.now()}`,
        strategyName: lastRunParams.value?.strategyName || 'Unknown',
        dateRange: `${lastRunParams.value?.startDate || ''} ~ ${lastRunParams.value?.endDate || ''}`,
        createdAt: new Date().toISOString(),
        status: 'completed' as const,
        metrics: metrics.value ? {
          totalReturn: metrics.value.totalReturn,
          annualizedReturn: metrics.value.annualizedReturn,
          maxDrawdown: metrics.value.maxDrawdown,
          sharpeRatio: metrics.value.sharpeRatio,
          winRate: metrics.value.winRate,
          profitFactor: metrics.value.profitFactor
        } : undefined
      };

      // 添加到历史记录开头
      history.unshift(historyRecord);

      // 限制历史记录数量（保留最近100条）
      if (history.length > 100) {
        history.splice(100);
      }

      localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
    } catch (e) {
      // 如果是配额超限错误，则关闭后续历史写入，避免前端被日志刷爆 / 一直卡顿
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('QuotaExceededError')) {
        historyDisabled = true;
        console.warn('保存历史记录失败：localStorage 已满，本次会话内将不再写入 backtest_history。');
      } else {
        console.error('保存历史记录失败:', e);
      }
    }
  }

  function clearBacktestData() {
    equitySeries.value = [];
    benchmarkEquitySeries.value = [];
    metrics.value = null;
    monthlyReturns.value = [];
    holdingPeriods.value = [];
    trades.value = [];
    // 【修复】同时清除 localStorage 中的旧数据
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (e) {
      console.warn('清除 localStorage 失败:', e);
    }
  }

  function addTrade(trade: Trade) {
    const existingIndex = trades.value.findIndex(t => t.id === trade.id);
    if (existingIndex >= 0) {
      trades.value[existingIndex] = trade;
      trades.value = [...trades.value]; // 触发 shallowRef 响应性
    } else {
      trades.value = [...trades.value, trade]; // 触发 shallowRef 响应性
    }
    persistToStorage();
  }

  function addTrades(newTrades: Trade[]) {
    if (newTrades.length === 0) return;
    const currentTrades = [...trades.value];
    newTrades.forEach(trade => {
      const existingIndex = currentTrades.findIndex(t => t.id === trade.id);
      if (existingIndex >= 0) {
        currentTrades[existingIndex] = trade;
      } else {
        currentTrades.push(trade);
      }
    });
    trades.value = currentTrades;
    persistToStorage();
  }

  function setTrades(newTrades: Trade[]) {
    trades.value = newTrades;
    persistToStorage();
  }

  function setRunning(value: boolean) {
    running.value = value;
    persistToStorage();
  }

  function setProgress(value: number) {
    progress.value = value;
  }

  // CHANGED: 设置初始化状态
  function setInitializing(value: boolean) {
    isInitializing.value = value;
  }

  function setLastUpdated(value: string) {
    lastUpdated.value = value;
    persistToStorage();
  }

  function setMetrics(value: Metrics, skipHistory: boolean = false) {
    metrics.value = value;
    persistToStorage();
    // CHANGED: 当指标更新时，保存到历史记录（流式回测时跳过，只在最终完成时保存）
    if (value && lastRunParams.value && !skipHistory && !running.value) {
      saveToHistory();
    }
  }

  function setMonthlyReturns(value: MonthlyReturn[]) {
    monthlyReturns.value = value;
    persistToStorage();
  }

  function setHoldingPeriods(value: HoldingPeriod[]) {
    holdingPeriods.value = value;
    persistToStorage();
  }

  function setLastRunParams(value: LastRunParams) {
    lastRunParams.value = value;
    persistToStorage();
  }

  function addEquityPoint(date: string, equity: number) {
    const existingIndex = equitySeries.value.findIndex(item => item.date === date);
    let newSeries = [...equitySeries.value];
    if (existingIndex >= 0) {
      newSeries[existingIndex] = { date, equity };
    } else {
      newSeries.push({ date, equity });
    }
    newSeries.sort((a, b) => a.date.localeCompare(b.date));
    equitySeries.value = newSeries; // 触发 shallowRef 响应性
    persistToStorage();
  }

  function addEquityPoints(points: Array<{ date: string; equity: number }>) {
    if (points.length === 0) return;
    console.log('[backtestStore] addEquityPoints:', points.length, 'points, first:', points[0]?.date);
    const newSeries = [...equitySeries.value];
    points.forEach(p => {
      const existingIndex = newSeries.findIndex(item => item.date === p.date);
      if (existingIndex >= 0) {
        newSeries[existingIndex] = p;
      } else {
        newSeries.push(p);
      }
    });
    newSeries.sort((a, b) => a.date.localeCompare(b.date));
    equitySeries.value = newSeries;
    // 【优化】流式模式下不频繁持久化，只在回测完成时持久化
    // persistToStorage();
  }

  function addBenchmarkPoint(date: string, equity: number) {
    const existingIndex = benchmarkEquitySeries.value.findIndex(item => item.date === date);
    let newSeries = [...benchmarkEquitySeries.value];
    if (existingIndex >= 0) {
      newSeries[existingIndex] = { date, equity };
    } else {
      newSeries.push({ date, equity });
    }
    newSeries.sort((a, b) => a.date.localeCompare(b.date));
    benchmarkEquitySeries.value = newSeries; // 触发 shallowRef 响应性
    persistToStorage();
  }

  function addBenchmarkPoints(points: Array<{ date: string; equity: number }>) {
    if (points.length === 0) return;
    console.log('[backtestStore] addBenchmarkPoints:', points.length, 'points, first:', points[0]?.date, 'equity:', points[0]?.equity);
    const newSeries = [...benchmarkEquitySeries.value];
    points.forEach(p => {
      const existingIndex = newSeries.findIndex(item => item.date === p.date);
      if (existingIndex >= 0) {
        newSeries[existingIndex] = p;
      } else {
        newSeries.push(p);
      }
    });
    newSeries.sort((a, b) => a.date.localeCompare(b.date));
    benchmarkEquitySeries.value = newSeries;
    // 【优化】流式模式下不频繁持久化
    // persistToStorage();
  }

  const hasData = computed(() => {
    return equitySeries.value.length > 0 || metrics.value !== null;
  });

  // --- Replay Analytics & Sandbox Logic ---

  // Shadow Equity Curve: Recalculated by reversing P/L of excluded trades
  const shadowEquitySeries = computed(() => {
    const isNoManualExclusions = excludedTradeIds.value.size === 0;
    const isNoAutoExclusions = !autoExcludeAlphaLoss.value;

    if (isNoManualExclusions && isNoAutoExclusions) return [];
    if (equitySeries.value.length === 0) return [];

    // Create a copy of the equity series
    const series = equitySeries.value.map(p => ({ ...p }));
    const bSeries = benchmarkEquitySeries.value;

    // Logic for identifying trades to exclude
    const finalExcludedTrades = trades.value.filter(t => {
      if (t.action !== 'sell' || !t.profitLoss) return false;

      // Manual exclusion
      if (excludedTradeIds.value.has(t.id)) return true;

      // Auto alpha loss exclusion
      if (autoExcludeAlphaLoss.value && t.profitLoss < 0) {
        const bIdx = bSeries.findIndex(p => p.date === t.date);
        if (bIdx > 0) {
          const bRet = (bSeries[bIdx].equity / bSeries[bIdx - 1].equity - 1) * 100;
          // Alpha Loss: Stock down while benchmark is stable or less down
          if (bRet >= -0.5) return true;
        }
      }
      return false;
    });

    if (finalExcludedTrades.length === 0) return [];

    // Sort by date
    finalExcludedTrades.sort((a, b) => a.date.localeCompare(b.date));

    // Apply adjustments iteratively
    let cumulativeAdjustment = 0;
    let nextTradeIndex = 0;

    for (let i = 0; i < series.length; i++) {
      const currentDate = series[i].date;
      while (nextTradeIndex < finalExcludedTrades.length && finalExcludedTrades[nextTradeIndex].date <= currentDate) {
        cumulativeAdjustment -= (finalExcludedTrades[nextTradeIndex].profitLoss || 0);
        nextTradeIndex++;
      }
      series[i].equity += cumulativeAdjustment;
    }

    return series;
  });

  // Instruction C: Concentration Heatmap Data (Optimized O(N))
  const dailyConcentration = computed(() => {
    if (equitySeries.value.length === 0) return [];

    // 1. Create a delta map for position changes: O(H)
    const deltas: Record<string, number> = {};
    holdingPeriods.value.forEach(hp => {
      const entry = hp.entryDate;
      const exit = hp.exitDate; // if undefined, it's open until the end

      if (entry) {
        deltas[entry] = (deltas[entry] || 0) + 1;
      }
      if (exit) {
        // Position ends on exitDate, so concentration decreases on the NEXT day
        // But since we map to equitySeries dates, we need to be careful.
        // Simplified: decrement on the day AFTER exit.
        // We will handle date matching in the second pass.
        // Note: For now, we'll store the exact exit date and handle decrement logic during the scan.
        // Actually, easiest is to map dates to simple string keys.
        // Let's use a simpler approach: 
        // Iterate equity series, maintain a count of active holdings.
        // But checking active holdings for every day is O(N*H).
        // Delta approach is best.
        // We need to find the "next day" for decrement. 
        // Since we don't strictly know "next day" without the calendar, we accumulate deltas on exact dates.
        // If exitDate is T, it counts for T. So T+1 should have -1.
        // We will append a "decrement" instruction.
      }
    });

    // 2. Sort all holding events to perform a single linear scan? 
    // Actually, since we align with equitySeries, we can just look up deltas.
    // Issue: "exitDate + 1 day" might not be in equitySeries if it's a non-trading day?
    // Robust approach: Span coverage.
    // Converting date strings to timestamps for range checking might be faster if purely numeric.

    // Alternative Optimized:
    // Create an interval tree? Overkill.
    // Let's stick to the Delta Map but handled correctly.
    // A holding [start, end] adds +1 to [start, end].
    // We can iterate through holdings and mark ranges on a specific efficient structure if N is small.
    // But equitySeries is N (days), Holdings is M.
    // M is large (thousands of trades). N is small (~250 days/year).
    // The previous nested loop was O(N * M).
    // Optimization: Pre-process Holdings into start/end buckets.

    const changes: Record<string, number> = {};
    holdingPeriods.value.forEach(hp => {
      if (!hp.entryDate) return;
      changes[hp.entryDate] = (changes[hp.entryDate] || 0) + 1;

      if (hp.exitDate) {
        // We want to include the exit date in the count. So we decrement AFTER the exit date.
        // Since we iterate through the sorted equitySeries, we can handle the "decrement after" logic there.
        // We'll store the exit date in a separate map to decrement *after* processing that date?
        // Or simpler: change[exitDate] could include a "end of day" decrement? 
        // Let's just store "expires at end of"
      }
    });

    // Better Approach:
    // Filter holdings into 'entry' and 'exit' lists sorted by date.
    // Pointers for entry/exit lists.
    // Iterate equitySeries.

    const entries = holdingPeriods.value.map(h => h.entryDate).filter(d => d).sort();
    const exits = holdingPeriods.value.map(h => h.exitDate).filter(d => d).sort();

    let eIdx = 0;
    let xIdx = 0;
    let currentCount = 0;

    return equitySeries.value.map(p => {
      const today = p.date;

      // Add new positions that start on or before today (should be mostly today if sorted, but just in case)
      // Note: This assumes holdingPeriods entryDate matches equity dates or comes before.
      while (eIdx < entries.length && entries[eIdx]! <= today) {
        currentCount++;
        eIdx++;
      }

      // Remove positions that ended BEFORE today.
      // If exit is yesterday, it's gone today.
      // If exit is today, it is still held today (usually).
      while (xIdx < exits.length && exits[xIdx]! < today) {
        currentCount--;
        xIdx++;
      }

      return {
        date: today,
        count: currentCount,
        score: Math.min(1, currentCount / 10)
      };
    });
  });

  // Dynamic Risk Radar Scores based on current playbackCursor
  const liveRiskScores = computed(() => {
    const curTrades = filteredTrades.value; // Trades up to playbackCursor
    if (curTrades.length === 0) return { alpha: 50, resistance: 50, plRatio: 50, concentration: 50, efficiency: 50 };

    const sellTrades = curTrades.filter(t => t.action === 'sell');
    const bSeries = benchmarkEquitySeries.value;

    // 1. Alpha Strength: Percentage of non-Alpha-loss trades
    let alphaLossCount = 0;
    sellTrades.forEach(t => {
      if (t.profitLoss === undefined || t.profitLoss >= 0) return;
      // Get benchmark return for that day
      const bIdx = bSeries.findIndex(p => p.date === t.date);
      if (bIdx > 0) {
        const bRet = (bSeries[bIdx].equity / bSeries[bIdx - 1].equity - 1) * 100;
        if (bRet >= -0.5) alphaLossCount++; // Market was okay, but we lost
      }
    });
    const alphaStrength = sellTrades.length > 0 ? 100 * (1 - alphaLossCount / sellTrades.length) : 50;

    // 2. Resistance (抗回撤性): 100 - (Drawdown depth during benchmark dips)
    // Simplified: Correlation between equity and benchmark during dips
    const resistance = 60; // Placeholder for complex logic

    // 3. P/L Ratio: Win sum / Loss sum
    const wins = sellTrades.filter(t => (t.profitLoss || 0) > 0).reduce((s, t) => s + (t.profitLoss || 0), 0);
    const losses = Math.abs(sellTrades.filter(t => (t.profitLoss || 0) < 0).reduce((s, t) => s + (t.profitLoss || 0), 0));
    const plRatio = losses > 0 ? Math.min(100, (wins / losses) * 40) : (wins > 0 ? 100 : 50);

    // 4. Symbol Concentration: 100 - (Max single loss / Equity * Constant)
    const currentEquity = curTrades.length > 0 ? (equitySeries.value.find(p => p.date === playbackCursor.value)?.equity || 1) : 1;
    const symbolLosses: Record<string, number> = {};
    sellTrades.forEach(t => {
      const code = t.symbolCode || 'unknown';
      symbolLosses[code] = (symbolLosses[code] || 0) + (t.action === 'sell' ? (t.profitLoss || 0) : 0);
    });
    const maxLoss = Math.abs(Math.min(...Object.values(symbolLosses), 0));
    const concentration = Math.max(0, 100 - (maxLoss / currentEquity * 200));

    // 5. Execution Efficiency: Based on dummy slippage logic
    const slippageTrades = curTrades.filter(t => t.price > 100 && t.price % 1 === 0).length;
    const efficiency = Math.max(0, 100 - (slippageTrades / curTrades.length * 100));

    return {
      alpha: Math.round(alphaStrength),
      resistance: Math.round(resistance),
      plRatio: Math.round(plRatio),
      concentration: Math.round(concentration),
      efficiency: Math.round(efficiency)
    };
  });

  // --- Playback Getters ---

  // Filtered trades up to the playback cursor
  const filteredTrades = computed(() => {
    if (!playbackMode.value || !playbackCursor.value) return trades.value;
    const cursorTime = playbackCursor.value;
    // O(N) filtering - for large datasets (>10k), we could optimize with binary search if trades are sorted
    return trades.value.filter(t => t.date <= cursorTime);
  });

  // Trades occurring exactly on the playback cursor date
  const dailyFocusTrades = computed(() => {
    if (!playbackCursor.value) return [];
    const cursorTime = playbackCursor.value;
    return trades.value.filter(t => t.date === cursorTime);
  });

  // Progress based on cursor position in the timeline
  const playbackProgress = computed(() => {
    if (equitySeries.value.length === 0 || !playbackCursor.value) return 0;
    const index = equitySeries.value.findIndex(p => p.date === playbackCursor.value);
    if (index === -1) return 0;
    return Math.round((index / (equitySeries.value.length - 1)) * 100);
  });

  // --- Playback Actions ---

  function setPlaybackCursor(date: string) {
    playbackCursor.value = date;
  }

  function togglePlaybackMode(value?: boolean) {
    playbackMode.value = value !== undefined ? value : !playbackMode.value;
    if (playbackMode.value && !playbackCursor.value && equitySeries.value.length > 0) {
      // Default to last date if entering playback mode without a cursor
      playbackCursor.value = equitySeries.value[equitySeries.value.length - 1].date;
    }
  }

  function setPlaybackSpeed(speed: number) {
    playbackSpeed.value = speed;
  }

  function togglePlaybackSnap(value?: boolean) {
    playbackSnap.value = value !== undefined ? value : !playbackSnap.value;
  }

  function toggleTradeExclusion(tradeId: string) {
    if (excludedTradeIds.value.has(tradeId)) {
      excludedTradeIds.value.delete(tradeId);
    } else {
      excludedTradeIds.value.add(tradeId);
    }
    // Set is reactive but we might need to trigger update manually for some watchers
    excludedTradeIds.value = new Set(excludedTradeIds.value);
  }

  return {
    equitySeries,
    benchmarkEquitySeries,
    metrics,
    monthlyReturns,
    holdingPeriods,
    trades,
    lastRunParams,
    lastUpdated,
    running,
    progress,
    hasData,
    hydrateFromStorage,
    persistToStorage,
    clearBacktestData,
    setRunning,
    setProgress,
    setLastUpdated,
    setMetrics,
    setMonthlyReturns,
    setHoldingPeriods,
    setTrades,
    setLastRunParams,
    addEquityPoint,
    addEquityPoints,
    addBenchmarkPoint,
    addBenchmarkPoints,
    addTrade,
    addTrades,
    isInitializing,
    setInitializing,
    // Playback
    playbackCursor,
    playbackMode,
    playbackSpeed,
    filteredTrades,
    dailyFocusTrades,
    playbackProgress,
    setPlaybackCursor,
    togglePlaybackMode,
    setPlaybackSpeed,
    playbackSnap,
    togglePlaybackSnap,
    excludedTradeIds,
    autoExcludeAlphaLoss,
    shadowEquitySeries,
    liveRiskScores,
    dailyConcentration,
    toggleTradeExclusion,
    toggleAutoExcludeAlphaLoss(val?: boolean) {
      autoExcludeAlphaLoss.value = val !== undefined ? val : !autoExcludeAlphaLoss.value;
    }
  };
});

