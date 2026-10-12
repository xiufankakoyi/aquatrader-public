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

    <!-- 主体：左侧活动栏+文件树 / 编辑器 / 图表 / 数据 -->
    <div class="workbench-body">
      <!-- VS Code 风格活动栏（最左侧图标栏） -->
      <div class="activity-bar">
        <button
          class="activity-icon"
          :class="{ active: activityView === 'explorer' }"
          title="资源管理器"
          @click="toggleActivityView('explorer')"
        >
          <i class="fas fa-folder-tree"></i>
        </button>
        <button
          class="activity-icon"
          :class="{ active: activityView === 'search' }"
          title="搜索"
          @click="toggleActivityView('search')"
        >
          <i class="fas fa-magnifying-glass"></i>
        </button>
        <button
          class="activity-icon"
          :class="{ active: activityView === 'run' }"
          title="运行与调试"
          @click="toggleActivityView('run')"
        >
          <i class="fas fa-play"></i>
        </button>
        <div class="activity-bottom">
          <button
            class="activity-icon"
            title="设置"
            @click="toggleActivityView('settings')"
          >
            <i class="fas fa-gear"></i>
          </button>
        </div>
      </div>

      <!-- 文件资源管理器（VS Code 风格） -->
      <div
        v-show="activityView === 'explorer'"
        class="sidebar-panel"
      >
        <FileExplorer
          ref="fileExplorerRef"
          :active-path="activeFilePath"
          @open-file="handleOpenFile"
          @new-file="handleNewFile"
          @delete-file="handleDeleteFile"
        />
      </div>

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
          @save="handleSaveStrategy"
          @run="handleRunBacktest"
        />
      </div>

      <!-- 拖拽调整条 -->
      <div
        class="resize-bar vertical"
        @mousedown="startResizeLeft"
      >
        <div class="resize-handle"></div>
      </div>

      <!-- 中间+右侧+底部容器 -->
      <div class="center-right-container">
        <div class="center-right-row">
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

        <!-- 底部输出终端（VS Code 风格） -->
        <div
          class="bottom-panel"
          :style="{ height: outputCollapsed ? '32px' : outputHeight + 'px' }"
        >
          <OutputTerminal
            :logs="outputLogs"
            :problems="problems"
            :collapsed="outputCollapsed"
            @toggle="outputCollapsed = !outputCollapsed"
          />
        </div>
      </div>
    </div>

    <!-- 底部状态栏 -->
    <WorkbenchStatusBar
      :api-status="apiStatus"
      :last-update="lastUpdate"
      :current-date="currentBacktestDate"
      :progress="backtestProgress"
    />

    <!-- 跳转 Dashboard 浮层按钮（回测完成后显示） -->
    <transition name="fade">
      <button
        v-if="showDashboardJump"
        class="dashboard-jump-btn"
        @click="jumpToDashboard"
      >
        <i class="fas fa-chart-line"></i>
        <span>查看回测 Dashboard</span>
        <i class="fas fa-arrow-right"></i>
      </button>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue';
import { useRouter } from 'vue-router';
import WorkbenchToolbar from '../strategy-workbench/WorkbenchToolbar.vue';
import CodeEditorPanel from '../strategy-workbench/CodeEditorPanel.vue';
import ChartPanel from '../strategy-workbench/ChartPanel.vue';
import DataPanel from '../strategy-workbench/DataPanel.vue';
import WorkbenchStatusBar from '../strategy-workbench/WorkbenchStatusBar.vue';
import FileExplorer from '../strategy-workbench/FileExplorer.vue';
import OutputTerminal from '../strategy-workbench/OutputTerminal.vue';
import { useSocketIO } from '../../composables/useSocketIO';
import { apiService } from '../../services/api';
import { useBacktestStore } from '../../store/backtestStore';
import type { BacktestConfig, Trade, Position, LogEntry, BacktestMetrics } from '../../types/backtest';

defineOptions({
  name: 'StrategyWorkbench'
});

// ============================================
// 状态定义
// ============================================

const router = useRouter();
const backtestStore = useBacktestStore();

// 面板宽度配置
const leftPanelWidth = ref(25);
const centerPanelWidth = ref(50);
const rightPanelWidth = computed(() => 100 - leftPanelWidth.value - centerPanelWidth.value);

// 拖拽状态
const isResizing = ref(false);
const resizeDirection = ref<'left' | 'right' | null>(null);

// VS Code 风格活动栏状态
const activityView = ref<'explorer' | 'search' | 'run' | 'settings' | null>('explorer');
const fileExplorerRef = ref<InstanceType<typeof FileExplorer>>();
const activeFilePath = ref<string>('');

// 底部输出终端状态
const outputCollapsed = ref(false);
const outputHeight = ref(180);
const outputLogs = ref<LogEntry[]>([]);
const problems = ref<Array<{ message: string; severity: 'error' | 'warning'; code?: string }>>([]);

// 跳转 Dashboard 浮层按钮
const showDashboardJump = ref(false);
const lastJobId = ref<string | null>(null);

// 默认策略代码模板：最简单的买入/卖出策略
// - 第一个交易日买入第一只股票
// - 持有 5 天后卖出
// 适合用来验证回测链路是否打通（用时、数据、参数）
// 注意：strategy_id 必须与文件名对应（user_<filename>），否则 StrategyFactory 无法识别
const defaultStrategyCode = `from core.quant.strategies.strategy_framework import StrategyBase


class SimpleBuySellStrategy(StrategyBase):
    """
    最简单的买入卖出策略 - 用于验证回测链路
    逻辑：
        1. 第一个交易日买入股票池第一只股票
        2. 持有 5 天后卖出
        3. 其他情况 hold
    """
    # strategy_id 必须与文件名对应：user_<filename>
    # 保存接口会把代码写到 user/simple_buy_sell_strategy.py
    # 因此 strategy_id = "user_simple_buy_sell_strategy"
    strategy_id = "user_simple_buy_sell_strategy"
    strategy_name = "SimpleBuySellStrategy"

    def __init__(self, name=None):
        super().__init__(name)
        self._first_day = True
        self._buy_date = None

    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        简单买卖逻辑

        参数:
            current_date: 当前回测日期 (str)
            stock_pool_today: DataFrame，当日股票池
            data_query: 数据查询对象

        返回:
            Dict[str, str]: {股票代码: 'buy'/'sell'/'hold'}
        """
        signals = {}

        if stock_pool_today is None or len(stock_pool_today) == 0:
            return signals

        # 取第一只股票
        first_row = stock_pool_today.iloc[0]
        code = first_row.get('stock_code') or first_row.get('symbol_code')
        if not code:
            return signals

        if self._first_day:
            # 第一天买入
            signals[code] = 'buy'
            self._first_day = False
            self._buy_date = current_date
        elif self._buy_date and self._holding_days(current_date, self._buy_date) >= 5:
            # 持有 5 天后卖出
            signals[code] = 'sell'
            self._buy_date = None
        else:
            signals[code] = 'hold'

        return signals

    @staticmethod
    def _holding_days(current_date: str, buy_date: str) -> int:
        """简单估算持有天数（日历日差）。"""
        from datetime import datetime
        try:
            d1 = datetime.strptime(current_date[:10], '%Y-%m-%d')
            d2 = datetime.strptime(buy_date[:10], '%Y-%m-%d')
            return (d1 - d2).days
        except Exception:
            return 0
`;

// 策略代码
const strategyCode = ref(defaultStrategyCode);
const strategyName = ref('SimpleBuySellStrategy');

// 已保存到服务器的 strategy_id（保存接口返回）
// user 目录下的策略文件，strategy_id 形如 "user_<filename>"
const savedStrategyId = ref<string | null>(null);
// 上次保存时使用的代码内容，避免重复保存
const lastSavedCode = ref<string>('');

// 回测配置
const backtestConfig = ref<BacktestConfig>({
  startDate: '2024-01-01',
  endDate: '2024-12-31',
  initialCapital: 100000,
  positionSize: 0.1,
  maxPositions: 10,
  commission: 0.0003,
  slippage: 0.001
});

// 回测状态
const isRunning = ref(false);
const backtestProgress = ref(0);
const currentBacktestDate = ref<string>('');
// 回测开始时间戳（ms），用于在完成时计算总用时
const backtestStartTime = ref<number | null>(null);

// 回测数据
const equitySeries = ref<{ date: string; value: number }[]>([]);
const benchmarkSeries = ref<{ date: string; value: number }[]>([]);
const positionSeries = ref<{ date: string; value: number }[]>([]);
const drawdownSeries = ref<{ date: string; value: number }[]>([]);
const tradeFrequencyData = ref<{ date: string; count: number }[]>([]);
const trades = ref<Trade[]>([]);
const positions = ref<Position[]>([]);
const logs = ref<LogEntry[]>([]);
const errors = ref<string[]>([]);
const metrics = ref<BacktestMetrics | null>(null);

// 同步状态
const syncDate = ref<string>('');
const selectedDate = ref<string>('');

// API 状态
const apiStatus = ref<'connected' | 'disconnected' | 'error'>('disconnected');
const lastUpdate = ref<Date | null>(null);

// Socket.io
const { isConnected, connect, disconnect, onEvent, emitEvent } = useSocketIO();

// 计算属性
const hasBacktestData = computed(() => {
  return equitySeries.value.length > 0 || trades.value.length > 0;
});

// ============================================
// 面板拖拽调整
// ============================================

const startResizeLeft = (e: MouseEvent) => {
  isResizing.value = true;
  resizeDirection.value = 'left';
  document.addEventListener('mousemove', handleResize);
  document.addEventListener('mouseup', stopResize);
};

const startResizeRight = (e: MouseEvent) => {
  isResizing.value = true;
  resizeDirection.value = 'right';
  document.addEventListener('mousemove', handleResize);
  document.addEventListener('mouseup', stopResize);
};

const handleResize = (e: MouseEvent) => {
  if (!isResizing.value) return;
  
  const containerWidth = window.innerWidth;
  const x = e.clientX;
  const percentage = (x / containerWidth) * 100;
  
  if (resizeDirection.value === 'left') {
    leftPanelWidth.value = Math.max(15, Math.min(40, percentage));
    centerPanelWidth.value = 100 - leftPanelWidth.value - rightPanelWidth.value;
  } else if (resizeDirection.value === 'right') {
    const rightPct = 100 - percentage;
    rightPanelWidth.value = Math.max(15, Math.min(40, rightPct));
    centerPanelWidth.value = 100 - leftPanelWidth.value - rightPanelWidth.value;
  }
};

const stopResize = () => {
  isResizing.value = false;
  resizeDirection.value = null;
  document.removeEventListener('mousemove', handleResize);
  document.removeEventListener('mouseup', stopResize);
};

// ============================================
// 回测控制
// ============================================

/** 推送输出到终端 */
function pushOutput(message: string, level: 'info' | 'warning' | 'error' | 'success' | 'debug' = 'info') {
  const now = new Date();
  const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
  outputLogs.value.push({ time, message, level });
  // 限制最多 500 条避免内存膨胀
  if (outputLogs.value.length > 500) {
    outputLogs.value = outputLogs.value.slice(-300);
  }
}

/** 添加问题到问题面板 */
function pushProblem(message: string, severity: 'error' | 'warning' = 'error', code?: string) {
  problems.value.push({ message, severity, code });
}

/** 切换活动栏视图 */
function toggleActivityView(view: 'explorer' | 'search' | 'run' | 'settings') {
  activityView.value = activityView.value === view ? null : view;
}

/** 打开策略文件 */
async function handleOpenFile(file: { name: string; path: string; is_user: boolean }) {
  try {
    // 通过 path 读取文件内容（API 暂未提供，用 fetch）
    const res = await fetch(`/api/strategies/file?path=${encodeURIComponent(file.path)}`).catch(() => null);
    if (res && res.ok) {
      const data = await res.json();
      if (data.code) {
        strategyCode.value = data.code;
        strategyName.value = file.name.replace(/\.py$/, '');
        activeFilePath.value = file.path;
        savedStrategyId.value = file.is_user ? `user_${file.name.replace(/\.py$/, '')}` : '';
        lastSavedCode.value = data.code;
        pushOutput(`已打开策略: ${file.name}`, 'info');
      }
    }
  } catch (e) {
    pushOutput(`打开文件失败: ${(e as Error).message}`, 'error');
  }
}

/** 新建策略文件 */
function handleNewFile() {
  strategyCode.value = defaultStrategyCode;
  strategyName.value = 'NewStrategy';
  activeFilePath.value = '';
  savedStrategyId.value = '';
  lastSavedCode.value = '';
  pushOutput('新建策略，请编写代码后按 Ctrl+S 保存', 'info');
}

/** 删除策略文件 */
async function handleDeleteFile(file: { name: string; path: string }) {
  if (!confirm(`确认删除策略 ${file.name}？`)) return;
  try {
    await fetch(`/api/strategies/file?path=${encodeURIComponent(file.path)}`, { method: 'DELETE' });
    fileExplorerRef.value?.refresh();
    pushOutput(`已删除策略: ${file.name}`, 'warning');
  } catch (e) {
    pushOutput(`删除失败: ${(e as Error).message}`, 'error');
  }
}

/** 回测完成后跳转到 Dashboard */
function jumpToDashboard() {
  // 把当前回测结果存入 store，供 Dashboard 使用
  if (equitySeries.value.length > 0) {
    backtestStore.setLastRunParams({
      strategyName: strategyName.value,
      startDate: backtestConfig.value.startDate,
      endDate: backtestConfig.value.endDate,
      initialCapital: backtestConfig.value.initialCapital,
    });
    backtestStore.setMetrics(metrics.value as any);
    backtestStore.setTrades(trades.value as Trade[]);
    backtestStore.addEquityPoints(equitySeries.value.map(p => ({ date: p.date, equity: p.value })));
    if (benchmarkSeries.value.length > 0) {
      backtestStore.addBenchmarkPoints(benchmarkSeries.value.map(p => ({ date: p.date, equity: p.value })));
    }
    backtestStore.setLastUpdated(new Date().toISOString());
    backtestStore.persistToStorage();
  }
  // 携带 job_id 跳转
  router.push({
    path: '/dashboard',
    query: lastJobId.value ? { backtest_id: lastJobId.value, from: 'workbench' } : { from: 'workbench' },
  });
}

const handleRunBacktest = async () => {
  if (isRunning.value) return;

  isRunning.value = true;
  backtestProgress.value = 0;
  errors.value = [];
  outputLogs.value = [];  // 清空终端输出
  problems.value = [];
  showDashboardJump.value = false;
  // 标记回测开始时间，便于后续展示用时
  backtestStartTime.value = Date.now();
  pushOutput(`开始回测: ${strategyName.value} (${backtestConfig.value.startDate} ~ ${backtestConfig.value.endDate})`, 'info');

  try {
    // 1. 先把代码保存到服务器，拿到 strategy_id
    //    如果代码自上次保存后未变化，直接复用 savedStrategyId
    const strategyId = await ensureStrategySaved();

    // 2. 通过 Socket.IO 触发 run_backtest 事件
    //    后端 socketio_handlers.py 接收 strategy_id / start_date / end_date / initial_capital
    emitEvent('run_backtest', {
      strategy_id: strategyId,
      start_date: backtestConfig.value.startDate,
      end_date: backtestConfig.value.endDate,
      initial_capital: backtestConfig.value.initialCapital,
      params: {}
    });

    logs.value.push({
      level: 'info',
      message: `已提交回测任务：strategy_id=${strategyId}, 区间=${backtestConfig.value.startDate} ~ ${backtestConfig.value.endDate}`,
      timestamp: new Date().toISOString()
    });
  } catch (err) {
    console.error('启动回测失败:', err);
    errors.value.push('启动回测失败: ' + String(err));
    isRunning.value = false;
    backtestStartTime.value = null;
  }
};

const handleStopBacktest = () => {
  emitEvent('stop_backtest');
  isRunning.value = false;
};

/**
 * 把当前编辑器中的代码保存到服务器，返回 strategy_id
 * - 调用后端 POST /api/strategies/save，文件会被写到 core/quant/strategies/user/<name>.py
 * - 通过 StrategyLoader.reload_by_path 热重载，strategy_id 形如 "user_<filename>"
 * - 若代码自上次保存后未变化，直接复用缓存的 strategy_id，避免重复写盘
 */
const ensureStrategySaved = async (): Promise<string> => {
  // 代码未变化且已有 strategy_id → 直接复用
  if (savedStrategyId.value && lastSavedCode.value === strategyCode.value) {
    return savedStrategyId.value;
  }

  const res = await apiService.saveStrategy({
    name: strategyName.value || 'SimpleBuySellStrategy',
    code: strategyCode.value,
    temp: false
  });

  // 保存接口返回 filename（不含 .py），user 目录下的 strategy_id = "user_" + filename
  const filename = res.filename?.replace(/\.py$/, '') || strategyName.value;
  const strategyId = `user_${filename}`;

  savedStrategyId.value = strategyId;
  lastSavedCode.value = strategyCode.value;

  logs.value.push({
    level: 'success',
    message: `策略已保存到服务器：${res.filename} (strategy_id=${strategyId})`,
    timestamp: new Date().toISOString()
  });

  return strategyId;
};

const handleSaveStrategy = async () => {
  try {
    await ensureStrategySaved();
  } catch (err) {
    errors.value.push('保存策略失败: ' + String(err));
  }
};

const handleReset = () => {
  strategyCode.value = defaultStrategyCode;
  strategyName.value = 'SimpleBuySellStrategy';
  savedStrategyId.value = null;
  lastSavedCode.value = '';
  backtestStartTime.value = null;
  equitySeries.value = [];
  benchmarkSeries.value = [];
  positionSeries.value = [];
  drawdownSeries.value = [];
  tradeFrequencyData.value = [];
  trades.value = [];
  positions.value = [];
  logs.value = [];
  errors.value = [];
  metrics.value = null;
};

// ============================================
// 代码编辑
// ============================================

const formatCode = () => {
  // 简单的代码格式化（缩进处理）
  const lines = strategyCode.value.split('\n');
  let indent = 0;
  const formatted = lines.map(line => {
    const trimmed = line.trim();
    if (trimmed.endsWith(':')) {
      const result = '  '.repeat(indent) + trimmed;
      indent++;
      return result;
    } else if (trimmed.startsWith('return') || trimmed.startsWith('pass')) {
      indent = Math.max(0, indent - 1);
      return '  '.repeat(indent) + trimmed;
    }
    return '  '.repeat(indent) + trimmed;
  });
  strategyCode.value = formatted.join('\n');
};

const insertCodeSnippet = (snippet: string) => {
  const textarea = document.querySelector('.code-editor textarea') as HTMLTextAreaElement;
  if (textarea) {
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const value = strategyCode.value;
    strategyCode.value = value.substring(0, start) + snippet + value.substring(end);
  }
};

const updateBacktestConfig = (config: Partial<BacktestConfig>) => {
  backtestConfig.value = { ...backtestConfig.value, ...config };
};

// ============================================
// 图表交互
// ============================================

const handleChartHover = (date: string) => {
  syncDate.value = date;
};

const handleDateSelect = (date: string) => {
  selectedDate.value = date;
};

const handleDateChange = (date: string) => {
  selectedDate.value = date;
  syncDate.value = date;
};

const handleTradeClick = (trade: Trade) => {
  selectedDate.value = trade.date;
  syncDate.value = trade.date;
};

// ============================================
// Socket.io 事件监听
// ============================================

onMounted(() => {
  connect(import.meta.env.VITE_API_URL || 'http://localhost:5000');

  onEvent('connect', () => {
    apiStatus.value = 'connected';
    lastUpdate.value = new Date();
  });

  onEvent('disconnect', () => {
    apiStatus.value = 'disconnected';
  });

  onEvent('backtest_progress', (data: { progress: number; message?: string }) => {
    backtestProgress.value = data.progress || 0;
    if (data.message) {
      currentBacktestDate.value = data.message;
      pushOutput(`进度 ${data.progress}%: ${data.message}`, 'debug');
    }
  });

  // 后端 socketio_handlers.py 推送的完成事件名为 backtest_complete
  // 携带 { job_id, stats, equity_curve, trades, message, endpoint, elapsed_ms }
  onEvent('backtest_complete', (data: any) => {
    isRunning.value = false;

    // 计算并展示总用时（前端独立计时，与后端 elapsed_ms 互相印证）
    const elapsedMs = backtestStartTime.value
      ? Date.now() - backtestStartTime.value
      : data.elapsed_ms || 0;
    backtestStartTime.value = null;

    // equity_curve 后端格式: [{date, equity}, ...] 或 [{date, value}, ...]
    if (Array.isArray(data.equity_curve)) {
      equitySeries.value = data.equity_curve.map((p: any) => ({
        date: p.date,
        value: p.equity ?? p.value ?? 0
      }));
    }
    if (Array.isArray(data.trades)) {
      trades.value = data.trades;
    }
    // stats 里的字段映射到 BacktestMetrics
    if (data.stats) {
      const s = data.stats;
      metrics.value = {
        totalReturn: s.total_return ?? s.totalReturn ?? 0,
        annualReturn: s.annual_return ?? s.annualReturn ?? 0,
        sharpeRatio: s.sharpe_ratio ?? s.sharpeRatio ?? 0,
        maxDrawdown: s.max_drawdown ?? s.maxDrawdown ?? 0,
        volatility: s.volatility ?? 0,
        winRate: s.win_rate ?? s.winRate ?? 0,
        profitLossRatio: s.profit_loss_ratio ?? s.profitLossRatio ?? 0,
        totalTrades: s.total_trades ?? s.totalTrades ?? 0,
        avgHoldingDays: s.avg_holding_days ?? s.avgHoldingDays ?? 0,
        monthlyReturns: s.monthly_returns ?? {}
      } as BacktestMetrics;
    }

    logs.value.push({
      level: 'success',
      message: `回测完成 | 用时 ${(elapsedMs / 1000).toFixed(2)}s | ${data.message || ''}`,
      timestamp: new Date().toISOString()
    });

    pushOutput(`回测完成 ✓ 用时 ${(elapsedMs / 1000).toFixed(2)}s | 交易 ${trades.value.length} 笔`, 'success');
    if (metrics.value.sharpeRatio !== undefined) {
      pushOutput(`  累计收益: ${(metrics.value.totalReturn * 100).toFixed(2)}% | 夏普: ${metrics.value.sharpeRatio.toFixed(2)} | 最大回撤: ${(metrics.value.maxDrawdown * 100).toFixed(2)}%`, 'info');
    }

    // 记录 job_id 并显示跳转按钮
    if (data.job_id) {
      lastJobId.value = data.job_id;
    }
    showDashboardJump.value = true;

    lastUpdate.value = new Date();
  });

  // 后端会在任务启动时推送 backtest_started，可用于拿到 job_id
  onEvent('backtest_started', (data: any) => {
    logs.value.push({
      level: 'info',
      message: `回测任务已启动 (job_id=${data.job_id})`,
      timestamp: new Date().toISOString()
    });
    pushOutput(`回测任务已启动 (job_id=${data.job_id})`, 'info');
    if (data.job_id) {
      lastJobId.value = data.job_id;
    }
  });

  onEvent('backtest_error', (data: any) => {
    const errMsg = data.message || data.error || '回测失败';
    errors.value.push(errMsg);
    if (data.detail) {
      errors.value.push(data.detail);
    }
    isRunning.value = false;
    backtestStartTime.value = null;

    pushOutput(`回测失败: ${errMsg}`, 'error');
    if (data.detail) pushOutput(`  详情: ${data.detail}`, 'error');
    if (data.sandbox) {
      if (data.timed_out) pushProblem('回测超时被强杀', 'error', data.code);
      if (data.crashed) pushProblem('回测进程崩溃', 'error', data.code);
    }
    if (data.code === 'CODE_AUDIT_FAILED') {
      pushProblem(data.detail || '代码安全审计未通过', 'error', data.code);
    }
  });

  onEvent('backtest_log', (data: { level: string; message: string; timestamp: string }) => {
    logs.value.push({
      level: data.level,
      message: data.message,
      timestamp: data.timestamp
    });
  });
});

onBeforeUnmount(() => {
  disconnect();
  
  // 清理事件监听
  document.removeEventListener('mousemove', handleResize);
  document.removeEventListener('mouseup', stopResize);
});

// 监听连接状态
watch(() => isConnected(), (connected) => {
  apiStatus.value = connected ? 'connected' : 'disconnected';
});
</script>

<style scoped>
.strategy-workbench {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #1e1e1e;
  overflow: hidden;
}

.workbench-body {
  display: flex;
  flex: 1;
  overflow: hidden;
}

/* ============================================ */
/* VS Code 风格活动栏（最左侧图标栏，48px 宽） */
/* ============================================ */
.activity-bar {
  width: 48px;
  flex-shrink: 0;
  background: #333333;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 6px 0;
  border-right: 1px solid #1e1e1e;
}

.activity-icon {
  width: 48px;
  height: 48px;
  background: transparent;
  border: none;
  color: #858585;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  position: relative;
  transition: color 0.15s;
}

.activity-icon:hover {
  color: #ffffff;
}

.activity-icon.active {
  color: #ffffff;
}

.activity-icon.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 2px;
  background: #ffffff;
}

.activity-bottom {
  margin-top: auto;
}

/* ============================================ */
/* 侧边栏面板（文件资源管理器容器） */
/* ============================================ */
.sidebar-panel {
  width: 240px;
  flex-shrink: 0;
  background: #252526;
  border-right: 1px solid #1e1e1e;
  overflow: hidden;
}

/* ============================================ */
/* 中间+右侧+底部容器 */
/* ============================================ */
.center-right-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

.center-right-row {
  flex: 1;
  display: flex;
  overflow: hidden;
  min-height: 0;
}

.left-panel,
.center-panel,
.right-panel {
  height: 100%;
  overflow: hidden;
}

.left-panel {
  min-width: 200px;
}

.center-panel {
  min-width: 300px;
  flex: 1;
}

.right-panel {
  min-width: 200px;
}

/* ============================================ */
/* 底部输出终端面板 */
/* ============================================ */
.bottom-panel {
  flex-shrink: 0;
  background: #1e1e1e;
  border-top: 1px solid #3c3c3c;
  overflow: hidden;
}

/* ============================================ */
/* 拖拽调整条 */
/* ============================================ */
.resize-bar {
  width: 4px;
  background: #1e1e1e;
  cursor: col-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s;
  flex-shrink: 0;
}

.resize-bar:hover {
  background: #2962ff;
}

.resize-handle {
  width: 2px;
  height: 30px;
  background: #3a3e49;
  border-radius: 1px;
}

.resize-bar:hover .resize-handle {
  background: #fff;
}

/* ============================================ */
/* 跳转 Dashboard 浮层按钮（回测完成后显示） */
/* ============================================ */
.dashboard-jump-btn {
  position: fixed;
  right: 24px;
  bottom: 48px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  background: linear-gradient(135deg, #2962ff 0%, #007acc 100%);
  color: #ffffff;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  box-shadow: 0 4px 16px rgba(41, 98, 255, 0.4);
  z-index: 1000;
  transition: transform 0.2s, box-shadow 0.2s;
}

.dashboard-jump-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(41, 98, 255, 0.6);
}

.dashboard-jump-btn:active {
  transform: translateY(0);
}

.dashboard-jump-btn i:first-child {
  font-size: 16px;
}

.dashboard-jump-btn i:last-child {
  font-size: 12px;
  opacity: 0.8;
}

/* 淡入淡出动画 */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s, transform 0.3s;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: translateY(20px);
}
</style>
