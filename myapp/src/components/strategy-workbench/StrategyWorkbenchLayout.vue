<template>
  <div class="strategy-workbench">
    <!-- 顶部工具栏 -->
    <WorkbenchToolbar
      :strategy-name="strategyName"
      :is-running="isRunning"
      :has-backtest-data="hasBacktestData"
      @run="handleRunBacktest"
      @stop="handleStopBacktest"
      @save="handleSaveStrategy"
      @reset="handleReset"
    />

    <!-- 三栏主布局 -->
    <div class="workbench-body">
      <!-- 左侧面板：代码与配置 -->
      <div
        class="left-panel"
        :style="{ width: leftPanelWidth + '%' }"
      >
        <CodeEditorPanel
          v-model="strategyCode"
          :strategy-name="strategyName"
          :is-running="isRunning"
          :backtest-config="backtestConfig"
          @update:config="updateBacktestConfig"
          @format="formatCode"
          @insert-code="insertCodeSnippet"
        />
      </div>

      <!-- 拖拽调整条 -->
      <div
        class="resize-bar vertical"
        @mousedown="startResizeLeft"
      >
        <div class="resize-handle"></div>
      </div>

      <!-- 中间面板：图表区域 -->
      <div
        class="center-panel"
        :style="{ width: centerPanelWidth + '%' }"
      >
        <ChartPanel
          :equity-series="equitySeries"
          :benchmark-series="benchmarkSeries"
          :position-series="positionSeries"
          :drawdown-series="drawdownSeries"
          :trade-frequency-data="tradeFrequencyData"
          :trades="trades"
          :is-loading="isRunning"
          :sync-date="syncDate"
          @hover="handleChartHover"
          @date-select="handleDateSelect"
        />
      </div>

      <!-- 拖拽调整条 -->
      <div
        class="resize-bar vertical"
        @mousedown="startResizeRight"
      >
        <div class="resize-handle"></div>
      </div>

      <!-- 右侧面板：数据透视 -->
      <div
        class="right-panel"
        :style="{ width: rightPanelWidth + '%' }"
      >
        <DataPanel
          :trades="trades"
          :positions="positions"
          :logs="logs"
          :errors="errors"
          :metrics="metrics"
          :selected-date="selectedDate"
          @trade-click="handleTradeClick"
          @date-change="handleDateChange"
        />
      </div>
    </div>

    <!-- 底部状态栏 -->
    <WorkbenchStatusBar
      :api-status="apiStatus"
      :last-update="lastUpdate"
      :current-date="currentBacktestDate"
      :progress="backtestProgress"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue';
import WorkbenchToolbar from './WorkbenchToolbar.vue';
import CodeEditorPanel from './CodeEditorPanel.vue';
import ChartPanel from './ChartPanel.vue';
import DataPanel from './DataPanel.vue';
import WorkbenchStatusBar from './WorkbenchStatusBar.vue';
import { useSocketIO } from '../../composables/useSocketIO';
import type { BacktestConfig, Trade, Position, LogEntry, BacktestMetrics } from '../../types/backtest';

// ============================================
// 状态定义
// ============================================

// 面板宽度配置
const leftPanelWidth = ref(25);
const centerPanelWidth = ref(50);
const rightPanelWidth = computed(() => 100 - leftPanelWidth.value - centerPanelWidth.value);

// 拖拽状态
const isResizing = ref(false);
const resizeDirection = ref<'left' | 'right' | null>(null);

// 默认策略代码模板
const defaultStrategyCode = `from core.quant.strategies.strategy_framework import StrategyBase

class MyStrategy(StrategyBase):
    """
    我的策略 - 简单均线突破策略
    """
    strategy_name = "MyStrategy"
    
    def __init__(self, name=None):
        super().__init__(name)
    
    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        策略逻辑实现
        
        参数:
            current_date: 当前日期
            stock_pool_today: DataFrame，当日股票数据
            data_query: 数据查询对象
        
        返回:
            Dict[str, str]: {股票代码: 'buy'/'sell'/'hold'}
        """
        signals = {}
        
        for _, row in stock_pool_today.iterrows():
            code = row.get('stock_code') or row.get('symbol_code')
            if not code:
                continue
            
            close = row.get('close', 0)
            ma20 = row.get('ma20', 0)
            ma5 = row.get('ma5', 0)
            volume_ratio = row.get('volume_ratio', 1)
            is_st = row.get('is_st', False)
            is_limit_up = row.get('is_limit_up', False)
            
            # 买入条件：价格突破20日均线 + 放量 + 非ST
            if (close > ma20 and 
                volume_ratio > 1.5 and
                not is_st and
                not is_limit_up):
                signals[code] = 'buy'
            
            # 卖出条件：跌破5日均线
            elif close < ma5:
                signals[code] = 'sell'
            
            else:
                signals[code] = 'hold'
        
        return signals
`;

// 策略代码
const strategyCode = ref(defaultStrategyCode);
const strategyName = ref('MyStrategy');

// 获取默认日期范围（最近一年）
const getDefaultDateRange = () => {
  const end = new Date();
  const start = new Date();
  start.setFullYear(start.getFullYear() - 1);
  return {
    startDate: start.toISOString().split('T')[0],
    endDate: end.toISOString().split('T')[0],
  };
};

const defaultDates = getDefaultDateRange();

// 回测配置
const backtestConfig = ref<BacktestConfig>({
  initialCapital: 1000000,
  startDate: defaultDates.startDate,
  endDate: defaultDates.endDate,
  stockPool: 'all',
  benchmark: '000300.SH',
  commission: 0.0003,
  slippage: 0.001,
});

// 回测状态
const isRunning = ref(false);
const backtestProgress = ref(0);
const currentBacktestDate = ref('');

// 回测数据
const equitySeries = ref<Array<{ date: string; equity: number }>>([]);
const benchmarkSeries = ref<Array<{ date: string; equity: number }>>([]);
const positionSeries = ref<Array<{ date: string; position: number }>>([]);
const trades = ref<Trade[]>([]);
const positions = ref<Position[]>([]);
const logs = ref<LogEntry[]>([]);
const errors = ref<LogEntry[]>([]);
const metrics = ref<BacktestMetrics | null>(null);

const drawdownSeries = computed(() => {
  if (equitySeries.value.length === 0) return [];
  
  let peak = -Infinity;
  return equitySeries.value.map(item => {
    if (item.equity > peak) {
      peak = item.equity;
    }
    const drawdown = peak > 0 ? (item.equity / peak) - 1 : 0;
    return {
      date: item.date,
      drawdown: drawdown
    };
  });
});

const tradeFrequencyData = computed(() => {
  const counts: Record<string, number> = {};
  trades.value.forEach(t => {
    const date = t.date || t.entryDate;
    if (date) {
      counts[date] = (counts[date] || 0) + 1;
    }
  });
  return Object.entries(counts)
    .map(([date, count]) => ({ date, count }))
    .sort((a, b) => a.date.localeCompare(b.date));
});

// 同步与选择状态
const syncDate = ref('');
const selectedDate = ref('');

// API 状态
const apiStatus = ref<'connected' | 'disconnected'>('disconnected');
const lastUpdate = ref('');

// Socket.IO
const { onEvent, emitEvent: emit, connect, status: socketStatus } = useSocketIO();

// ============================================
// 计算属性
// ============================================

const hasBacktestData = computed(() => {
  return equitySeries.value.length > 0 && metrics.value !== null;
});

// 监听 Socket 状态
watch(socketStatus, (newStatus) => {
  if (newStatus === 'OPEN') {
    apiStatus.value = 'connected';
    lastUpdate.value = new Date().toLocaleTimeString();
  } else if (newStatus === 'CLOSED' || newStatus === 'ERROR') {
    apiStatus.value = 'disconnected';
  }
});

// ============================================
// 方法定义
// ============================================

// 拖拽调整面板宽度
function startResizeLeft(e: MouseEvent) {
  startResize(e, 'left');
}

function startResizeRight(e: MouseEvent) {
  startResize(e, 'right');
}

function startResize(e: MouseEvent, direction: 'left' | 'right') {
  isResizing.value = true;
  resizeDirection.value = direction;
  const startX = e.clientX;
  const startLeftWidth = leftPanelWidth.value;
  const startCenterWidth = centerPanelWidth.value;

  const handleMouseMove = (e: MouseEvent) => {
    if (!isResizing.value) return;
    
    const delta = ((e.clientX - startX) / window.innerWidth) * 100;
    
    if (direction === 'left') {
      // 调整左侧面板
      const newLeftWidth = Math.max(20, Math.min(40, startLeftWidth + delta));
      leftPanelWidth.value = newLeftWidth;
    } else {
      // 调整中间面板（影响中间和右侧）
      const newCenterWidth = Math.max(35, Math.min(60, startCenterWidth + delta));
      centerPanelWidth.value = newCenterWidth;
    }
  };

  const handleMouseUp = () => {
    isResizing.value = false;
    resizeDirection.value = null;
    document.removeEventListener('mousemove', handleMouseMove);
    document.removeEventListener('mouseup', handleMouseUp);
  };

  document.addEventListener('mousemove', handleMouseMove);
  document.addEventListener('mouseup', handleMouseUp);
}

// 回测配置更新
function updateBacktestConfig(config: Partial<BacktestConfig>) {
  backtestConfig.value = { ...backtestConfig.value, ...config };
}

// 运行回测
async function handleRunBacktest() {
  if (isRunning.value) return;
  
  isRunning.value = true;
  backtestProgress.value = 0;
  
  // 清空之前的数据
  equitySeries.value = [];
  benchmarkSeries.value = [];
  positionSeries.value = [];
  trades.value = [];
  logs.value = [];
  errors.value = [];
  
  logs.value.push({
    time: new Date().toLocaleTimeString(),
    message: '正在启动回测...',
    type: 'info',
  });
  
  try {
    emit('run_streaming_backtest', {
      strategy_name: strategyName.value || 'MyStrategy',
      start_date: backtestConfig.value.startDate,
      end_date: backtestConfig.value.endDate,
      benchmark_code: backtestConfig.value.benchmark,
      initial_capital: backtestConfig.value.initialCapital,
      commission: backtestConfig.value.commission,
      slippage: backtestConfig.value.slippage,
    });
  } catch (error) {
    console.error('启动回测失败:', error);
    isRunning.value = false;
    errors.value.push({
      time: new Date().toLocaleTimeString(),
      message: `启动回测失败: ${error}`,
      type: 'error',
    });
  }
}

// 停止回测
function handleStopBacktest() {
  emit('cancel_streaming_backtest');
  isRunning.value = false;
  logs.value.push({
    time: new Date().toLocaleTimeString(),
    message: '已发送停止请求',
    type: 'info',
  });
}

// 保存策略
async function handleSaveStrategy() {
  try {
    emit('save_strategy', {
      name: strategyName.value,
      code: strategyCode.value,
    });
    logs.value.push({
      time: new Date().toLocaleTimeString(),
      message: '策略已保存',
      type: 'info',
    });
  } catch (error) {
    errors.value.push({
      time: new Date().toLocaleTimeString(),
      message: `保存失败: ${error}`,
      type: 'error',
    });
  }
}

// 重置
function handleReset() {
  strategyCode.value = defaultStrategyCode;
  strategyName.value = 'MyStrategy';
  equitySeries.value = [];
  benchmarkSeries.value = [];
  positionSeries.value = [];
  trades.value = [];
  logs.value = [];
  errors.value = [];
  metrics.value = null;
  backtestProgress.value = 0;
  currentBacktestDate.value = '';
  
  const dates = getDefaultDateRange();
  backtestConfig.value = {
    initialCapital: 1000000,
    startDate: dates.startDate,
    endDate: dates.endDate,
    stockPool: 'all',
    benchmark: '000300.SH',
    commission: 0.0003,
    slippage: 0.001,
  };
  
  logs.value.push({
    time: new Date().toLocaleTimeString(),
    message: '已重置到默认状态',
    type: 'info',
  });
}

// 代码格式化
function formatCode() {
  // 调用 Monaco Editor 的格式化功能
}

// 插入代码片段
function insertCodeSnippet(code: string) {
  strategyCode.value += code;
}

// 图表悬停事件
function handleChartHover(data: { date: string; equity?: number }) {
  syncDate.value = data.date;
}

// 日期选择
function handleDateSelect(date: string) {
  selectedDate.value = date;
  syncDate.value = date;
}

// 交易点击
function handleTradeClick(trade: Trade) {
  selectedDate.value = trade.date;
  syncDate.value = trade.date;
}

// 日期变化
function handleDateChange(date: string) {
  selectedDate.value = date;
  syncDate.value = date;
}

// ============================================
// 生命周期
// ============================================

let unsubscribeProgress: (() => void) | null = null;
let unsubscribeComplete: (() => void) | null = null;
let unsubscribeError: (() => void) | null = null;
let unsubscribeEquity: (() => void) | null = null;
let unsubscribeTrade: (() => void) | null = null;
let unsubscribeRequestReceived: (() => void) | null = null;
let unsubscribeBacktestStart: (() => void) | null = null;
let unsubscribeCancelled: (() => void) | null = null;

onMounted(() => {
  // 使用相对路径 ''，让 Socket.IO 自动使用当前域名
  // 这样 Vite 代理可以正确代理 /socket.io 请求到后端
  connect('');

  unsubscribeRequestReceived = onEvent('request_received', (data: any) => {
    logs.value.push({
      time: new Date().toLocaleTimeString(),
      message: data.message || '回测请求已收到',
      type: 'info',
    });
  });

  unsubscribeBacktestStart = onEvent('backtest_start', (data: any) => {
    logs.value.push({
      time: new Date().toLocaleTimeString(),
      message: '回测开始运行...',
      type: 'info',
    });
  });

  unsubscribeProgress = onEvent('progress', (data: any) => {
    backtestProgress.value = data.progress || 0;
    currentBacktestDate.value = data.current_date || data.message || '';
    if (data.message) {
      logs.value.push({
        time: new Date().toLocaleTimeString(),
        message: data.message,
        type: 'info',
      });
    }
  });

  unsubscribeEquity = onEvent('daily_equity', (data: any) => {
    if (data.date) {
      if (data.equity !== undefined) {
        const existingIndex = equitySeries.value.findIndex(item => item.date === data.date);
        if (existingIndex >= 0) {
          equitySeries.value[existingIndex] = { date: data.date, equity: data.equity };
        } else {
          equitySeries.value.push({ date: data.date, equity: data.equity });
        }
        equitySeries.value.sort((a, b) => a.date.localeCompare(b.date));
      }
      if (data.benchmark_equity !== undefined || data.benchmarkReturn !== undefined) {
        const benchmarkValue = data.benchmark_equity ?? data.benchmarkReturn;
        const existingIndex = benchmarkSeries.value.findIndex(item => item.date === data.date);
        if (existingIndex >= 0) {
          benchmarkSeries.value[existingIndex] = { date: data.date, equity: benchmarkValue };
        } else {
          benchmarkSeries.value.push({ date: data.date, equity: benchmarkValue });
        }
        benchmarkSeries.value.sort((a, b) => a.date.localeCompare(b.date));
      }
      if (data.position !== undefined) {
        positionSeries.value.push({ date: data.date, position: data.position });
      }
    }
  });

  unsubscribeTrade = onEvent('new_trade', (data: any) => {
    if (data) {
      const trade: Trade = {
        id: data.id || `${data.date}-${data.symbolCode || data.symbol || data.symbol_code}`,
        symbol: data.symbol || data.symbol_code || '',
        symbolCode: data.symbolCode || data.symbol_code || data.symbol || '',
        stockCode: data.symbolCode || data.symbol_code || data.symbol || '',
        date: data.date || data.entryDate || '',
        action: (data.action || 'buy').toLowerCase(),
        price: data.price || data.entryPrice || 0,
        volume: data.volume || data.quantity || data.shares || 0,
        value: (data.price || data.entryPrice || 0) * (data.volume || data.quantity || 0),
        profitLoss: data.profitLoss ?? data.profit_loss ?? data.pnl ?? 0,
      };
      trades.value.push(trade);
      logs.value.push({
        time: new Date().toLocaleTimeString(),
        message: `交易: ${trade.action.toUpperCase()} ${trade.symbolCode} @ ${trade.price}`,
        type: 'info',
      });
    }
  });

  unsubscribeComplete = onEvent('stream_complete', (data: any) => {
    isRunning.value = false;
    if (data) {
      metrics.value = {
        totalReturn: data.totalReturn ?? data.total_return ?? 0,
        annualizedReturn: data.annualizedReturn ?? data.annualReturn ?? data.annual_return ?? 0,
        maxDrawdown: data.maxDrawdown ?? data.max_drawdown ?? 0,
        sharpeRatio: data.sharpeRatio ?? data.sharpe_ratio ?? 0,
        sortinoRatio: data.sortinoRatio ?? data.sortino_ratio ?? 0,
        volatility: data.volatility ?? 0,
        winRate: data.winRate ?? data.win_rate ?? 0,
        profitFactor: data.profitFactor ?? data.profit_factor ?? 0,
        tradesCount: data.totalTrades ?? data.total_trades ?? data.tradesCount ?? 0,
      };
    }
    
    const returnPercent = ((data?.totalReturn ?? data?.total_return ?? 0) * 100).toFixed(2);
    logs.value.push({
      time: new Date().toLocaleTimeString(),
      message: `回测完成 - 总收益: ${returnPercent}%`,
      type: 'success',
    });
  });

  unsubscribeCancelled = onEvent('backtest_cancelled', (data: any) => {
    isRunning.value = false;
    logs.value.push({
      time: new Date().toLocaleTimeString(),
      message: data.message || '回测已取消',
      type: 'warning',
    });
  });

  unsubscribeError = onEvent('backtest_error', (data: any) => {
    isRunning.value = false;
    errors.value.push({
      time: new Date().toLocaleTimeString(),
      message: data.message || '回测出错',
      type: 'error',
    });
  });
});

onBeforeUnmount(() => {
  unsubscribeProgress?.();
  unsubscribeComplete?.();
  unsubscribeError?.();
  unsubscribeEquity?.();
  unsubscribeTrade?.();
  unsubscribeRequestReceived?.();
  unsubscribeBacktestStart?.();
  unsubscribeCancelled?.();
});
</script>

<style scoped>
.strategy-workbench {
  display: flex;
  flex-direction: column;
  height: 100%;
  background-color: #131722;
  color: #d1d4dc;
  overflow: hidden;
  /* 定义策略工作台内部使用的 CSS 变量，确保子组件样式一致 */
  --bg-primary: #0A0A0A;
  --bg-secondary: #1e222d;
  --bg-tertiary: #2a2e39;
  --bg-hover: #3e3e42;
  --text-primary: #d1d4dc;
  --text-secondary: #b2b5be;
  --text-muted: #787b86;
  --border-color: #2a2e39;
  --border-hover: #3e3e42;
  --accent-primary: #2962ff;
  --accent-warning: #f5a623;
  --color-up: #089981;
  --color-up-light: #0db89a;
  --color-down: #f23645;
  --color-down-light: #f55763;
}

/* 主内容区 - 增加内边距，与全局导航栏产生视觉分离 */
.workbench-body {
  display: flex;
  flex: 1;
  overflow: hidden;
  padding: 8px;
  gap: 8px;
  background-color: #0d1117;
}

/* 左侧面板 - 增加圆角和阴影，做成卡片样式 */
.left-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background-color: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}

.center-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background-color: var(--bg-primary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}

.right-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background-color: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}

.resize-bar {
  width: 4px;
  background-color: transparent;
  cursor: col-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background-color 0.2s;
  z-index: 10;
  border-radius: 2px;
}

.resize-bar:hover {
  background-color: var(--accent-primary);
}

.resize-handle {
  width: 2px;
  height: 24px;
  background-color: var(--border-color);
  border-radius: 1px;
}

.resize-bar:hover .resize-handle {
  background-color: var(--text-primary);
}

/* 响应式适配 */
@media (max-width: 991px) {
  .left-panel {
    width: 30% !important;
  }

  .center-panel {
    width: 40% !important;
  }

  .right-panel {
    width: 30% !important;
  }
}

@media (max-width: 767px) {
  .workbench-body {
    flex-direction: column;
    padding: 4px;
    gap: 4px;
  }

  .left-panel,
  .center-panel,
  .right-panel {
    width: 100% !important;
    height: 33.33%;
  }

  .resize-bar.vertical {
    display: none;
  }
}
</style>
