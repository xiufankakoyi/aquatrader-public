<template>
  <div class="mobile-workbench">
    <!-- 顶部策略栏 -->
    <header class="mw-header">
      <button class="mw-icon-btn" @click="showFileDrawer = true" title="文件">
        <i class="fa-solid fa-folder-open"></i>
      </button>
      <div class="mw-title">
        <input
          v-model="strategyName"
          class="mw-name-input"
          placeholder="策略名称"
        />
      </div>
      <button
        class="mw-run-btn"
        :disabled="isRunning"
        @click="handleRun"
      >
        <i :class="isRunning ? 'fa-solid fa-spinner fa-spin' : 'fa-solid fa-play'"></i>
        <span>{{ isRunning ? '运行中' : '运行' }}</span>
      </button>
    </header>

    <!-- 回测参数（可折叠） -->
    <section class="mw-params" :class="{ collapsed: paramsCollapsed }">
      <button class="mw-params-toggle" @click="paramsCollapsed = !paramsCollapsed">
        <i class="fa-solid fa-sliders"></i>
        <span>回测参数</span>
        <i :class="paramsCollapsed ? 'fa-solid fa-chevron-down' : 'fa-solid fa-chevron-up'"></i>
      </button>
      <div v-show="!paramsCollapsed" class="mw-params-grid">
        <label class="mw-field">
          <span>开始</span>
          <input v-model="config.startDate" type="date" />
        </label>
        <label class="mw-field">
          <span>结束</span>
          <input v-model="config.endDate" type="date" />
        </label>
        <label class="mw-field">
          <span>资金(¥)</span>
          <input v-model.number="config.initialCapital" type="number" step="10000" />
        </label>
        <label class="mw-field">
          <span>手续费</span>
          <input v-model.number="config.commission" type="number" step="0.0001" />
        </label>
      </div>
    </section>

    <!-- 代码编辑器（简化版：textarea + 行号） -->
    <section class="mw-editor-wrap">
      <div class="mw-editor-tabs">
        <span class="mw-tab active">
          <i class="fa-solid fa-file-code"></i>
          <span>{{ strategyName || 'strategy' }}.py</span>
        </span>
        <button class="mw-icon-btn" @click="handleSave" title="保存">
          <i class="fa-solid fa-floppy-disk"></i>
        </button>
      </div>
      <div class="mw-editor-body">
        <div class="mw-line-numbers">{{ lineNumbers }}</div>
        <textarea
          ref="textareaRef"
          v-model="code"
          class="mw-textarea"
          spellcheck="false"
          autoCapitalize="off"
          autoCorrect="off"
          placeholder="# 在此编写策略代码"
          @keydown="handleTabKey"
        ></textarea>
      </div>
    </section>

    <!-- 输出终端（可折叠） -->
    <section class="mw-terminal" :class="{ collapsed: terminalCollapsed }">
      <button class="mw-terminal-toggle" @click="terminalCollapsed = !terminalCollapsed">
        <i class="fa-solid fa-terminal"></i>
        <span>输出</span>
        <span v-if="logs.length" class="mw-badge">{{ logs.length }}</span>
        <i :class="terminalCollapsed ? 'fa-solid fa-chevron-up' : 'fa-solid fa-chevron-down'"></i>
      </button>
      <div v-show="!terminalCollapsed" class="mw-terminal-body">
        <div v-if="!logs.length" class="mw-empty">终端就绪。点击"运行"开始回测。</div>
        <div
          v-for="(line, i) in logs.slice(-100)"
          :key="i"
          class="mw-log-line"
          :class="`level-${line.level}`"
        >
          <span class="mw-log-time">{{ line.time }}</span>
          <span class="mw-log-msg">{{ line.message }}</span>
        </div>
      </div>
    </section>

    <!-- 问题提示 -->
    <section v-if="problems.length" class="mw-problems">
      <div
        v-for="(p, i) in problems"
        :key="i"
        class="mw-problem-item"
        :class="`sev-${p.severity}`"
      >
        <i :class="p.severity === 'error' ? 'fa-solid fa-circle-xmark' : 'fa-solid fa-triangle-exclamation'"></i>
        <span>{{ p.message }}</span>
      </div>
    </section>

    <!-- 跳转 Dashboard 按钮（回测完成后显示） -->
    <transition name="mw-fade">
      <button
        v-if="showDashboardJump"
        class="mw-dashboard-jump"
        @click="jumpToDashboard"
      >
        <i class="fa-solid fa-chart-line"></i>
        <span>查看回测结果</span>
        <i class="fa-solid fa-arrow-right"></i>
      </button>
    </transition>

    <!-- 文件抽屉（从左滑出） -->
    <transition name="mw-drawer">
      <div v-if="showFileDrawer" class="mw-drawer-mask" @click.self="showFileDrawer = false">
        <aside class="mw-drawer">
          <div class="mw-drawer-header">
            <span>策略文件</span>
            <button class="mw-icon-btn" @click="showFileDrawer = false">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </div>
          <div class="mw-drawer-body">
            <div class="mw-drawer-section">
              <div class="mw-drawer-title">我的策略</div>
              <ul class="mw-file-list">
                <li
                  v-for="f in userFiles"
                  :key="f.path"
                  class="mw-file-item"
                  @click="openFile(f)"
                >
                  <i class="fa-solid fa-file-code"></i>
                  <span>{{ f.name }}</span>
                </li>
                <li v-if="!userFiles.length" class="mw-empty">暂无策略，编写代码后保存</li>
              </ul>
            </div>
            <div class="mw-drawer-section">
              <div class="mw-drawer-title">系统策略</div>
              <ul class="mw-file-list">
                <li
                  v-for="f in systemFiles"
                  :key="f.path"
                  class="mw-file-item readonly"
                  @click="openFile(f)"
                >
                  <i class="fa-solid fa-file-code"></i>
                  <span>{{ f.name }}</span>
                  <i class="fa-solid fa-lock"></i>
                </li>
              </ul>
            </div>
          </div>
        </aside>
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileStrategyWorkbench - 移动端策略工作台
 *
 * 简化版 VS Code 风格：
 * - 顶部策略名 + 运行按钮
 * - 中间 textarea 代码编辑器（移动端 Monaco 体验差，改用 textarea + 行号）
 * - 底部输出终端
 * - 左侧滑出文件抽屉
 *
 * 安全沙盒：复用桌面端 Socket.IO 链路（AST 审计 + multiprocessing 隔离）
 */
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useSocketIO } from '../../composables/useSocketIO';
import { apiService } from '../../services/api';
import { useBacktestStore } from '../../store/backtestStore';
import type { BacktestConfig, Trade, BacktestMetrics } from '../../types/backtest';

interface StrategyFile {
  name: string;
  path: string;
  is_user: boolean;
}

interface LogEntry {
  time: string;
  message: string;
  level: 'info' | 'warning' | 'error' | 'success' | 'debug';
}

interface Problem {
  message: string;
  severity: 'error' | 'warning';
  code?: string;
}

const router = useRouter();
const backtestStore = useBacktestStore();
const { connect, disconnect, onEvent, emitEvent } = useSocketIO();

// ============================================
// 状态
// ============================================

const strategyName = ref('SimpleBuySellStrategy');
const code = ref(`from core.quant.strategies.strategy_framework import StrategyBase


class SimpleBuySellStrategy(StrategyBase):
    """最简单的买入卖出策略 - 用于验证回测链路"""
    strategy_id = "user_simple_buy_sell_strategy"
    strategy_name = "SimpleBuySellStrategy"

    def __init__(self, name=None):
        super().__init__(name)
        self._first_day = True
        self._buy_date = None

    def generate_signals(self, current_date, stock_pool_today, data_query):
        """简单买卖逻辑"""
        signals = {}
        if stock_pool_today is None or len(stock_pool_today) == 0:
            return signals
        first_row = stock_pool_today.iloc[0]
        code = first_row.get('stock_code') or first_row.get('symbol_code')
        if not code:
            return signals
        if self._first_day:
            signals[code] = 'buy'
            self._first_day = False
            self._buy_date = current_date
        elif self._buy_date and self._holding_days(current_date, self._buy_date) >= 5:
            signals[code] = 'sell'
            self._buy_date = None
        else:
            signals[code] = 'hold'
        return signals

    @staticmethod
    def _holding_days(current_date: str, buy_date: str) -> int:
        from datetime import datetime
        try:
            d1 = datetime.strptime(current_date[:10], '%Y-%m-%d')
            d2 = datetime.strptime(buy_date[:10], '%Y-%m-%d')
            return (d1 - d2).days
        except Exception:
            return 0
`);

const config = ref<BacktestConfig>({
  startDate: '2024-01-01',
  endDate: '2024-12-31',
  initialCapital: 100000,
  stockPool: 'all',
  benchmark: '000300.SH',
  commission: 0.0003,
  slippage: 0.001,
});

const savedStrategyId = ref<string | null>(null);
const lastSavedCode = ref<string>('');

const isRunning = ref(false);
const backtestStartTime = ref<number | null>(null);

const paramsCollapsed = ref(true);
const terminalCollapsed = ref(false);
const showFileDrawer = ref(false);
const showDashboardJump = ref(false);
const lastJobId = ref<string | null>(null);

const logs = ref<LogEntry[]>([]);
const problems = ref<Problem[]>([]);
const userFiles = ref<StrategyFile[]>([]);
const systemFiles = ref<StrategyFile[]>([]);
const textareaRef = ref<HTMLTextAreaElement | null>(null);

// ============================================
// 计算属性
// ============================================

const lineNumbers = computed(() => {
  const count = code.value.split('\n').length;
  return Array.from({ length: count }, (_, i) => i + 1).join('\n');
});

// ============================================
// 工具方法
// ============================================

function pushLog(message: string, level: LogEntry['level'] = 'info') {
  const now = new Date();
  const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
  logs.value.push({ time, message, level });
  if (logs.value.length > 300) logs.value = logs.value.slice(-200);
}

function pushProblem(message: string, severity: 'error' | 'warning' = 'error', code?: string) {
  problems.value.push({ message, severity, code });
}

/** Tab 键插入 4 空格（避免 textarea 切换焦点） */
function handleTabKey(e: KeyboardEvent) {
  if (e.key === 'Tab') {
    e.preventDefault();
    const ta = textareaRef.value;
    if (!ta) return;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    code.value = code.value.substring(0, start) + '    ' + code.value.substring(end);
    // 恢复光标位置
    requestAnimationFrame(() => {
      ta.selectionStart = ta.selectionEnd = start + 4;
    });
  }
}

// ============================================
// 文件操作
// ============================================

async function refreshFileList() {
  try {
    const res = await apiService.getDiscoveredStrategies();
    const strategies = res.strategies || [];
    userFiles.value = strategies
      .filter((s: any) => s.id?.startsWith('user_'))
      .map((s: any) => ({
        name: s.id.replace('user_', '') + '.py',
        path: s.file_path || s.module_path || s.id,
        is_user: true,
      }));
    systemFiles.value = strategies
      .filter((s: any) => !s.id?.startsWith('user_'))
      .map((s: any) => ({
        name: (s.id || 'unknown') + '.py',
        path: s.file_path || s.module_path || s.id,
        is_user: false,
      }));
  } catch (e) {
    console.error('[MobileWorkbench] 加载策略列表失败', e);
  }
}

async function openFile(file: StrategyFile) {
  try {
    const res = await fetch(`/api/strategies/file?path=${encodeURIComponent(file.path)}`).catch(() => null);
    if (res && res.ok) {
      const data = await res.json();
      if (data.code) {
        code.value = data.code;
        strategyName.value = file.name.replace(/\.py$/, '');
        savedStrategyId.value = file.is_user ? `user_${file.name.replace(/\.py$/, '')}` : '';
        lastSavedCode.value = data.code;
        pushLog(`已打开: ${file.name}`, 'info');
        showFileDrawer.value = false;
      }
    }
  } catch (e) {
    pushLog(`打开失败: ${(e as Error).message}`, 'error');
  }
}

// ============================================
// 保存 & 运行
// ============================================

async function ensureSaved(): Promise<string> {
  if (savedStrategyId.value && lastSavedCode.value === code.value) {
    return savedStrategyId.value;
  }
  const res = await apiService.saveStrategy({
    name: strategyName.value || 'SimpleBuySellStrategy',
    code: code.value,
    temp: false,
  });
  const filename = res.filename?.replace(/\.py$/, '') || strategyName.value;
  const sid = `user_${filename}`;
  savedStrategyId.value = sid;
  lastSavedCode.value = code.value;
  return sid;
}

async function handleSave() {
  try {
    await ensureSaved();
    pushLog(`已保存: ${strategyName.value}.py`, 'success');
    refreshFileList();
  } catch (e) {
    pushLog(`保存失败: ${(e as Error).message}`, 'error');
  }
}

async function handleRun() {
  if (isRunning.value) return;
  isRunning.value = true;
  logs.value = [];
  problems.value = [];
  showDashboardJump.value = false;
  backtestStartTime.value = Date.now();
  pushLog(`开始回测: ${strategyName.value}`, 'info');
  pushLog(`区间: ${config.value.startDate} ~ ${config.value.endDate}`, 'debug');

  try {
    const sid = await ensureSaved();
    emitEvent('run_backtest', {
      strategy_id: sid,
      start_date: config.value.startDate,
      end_date: config.value.endDate,
      initial_capital: config.value.initialCapital,
      params: {},
    });
  } catch (err) {
    pushLog(`启动失败: ${String(err)}`, 'error');
    isRunning.value = false;
    backtestStartTime.value = null;
  }
}

function jumpToDashboard() {
  // 存入 store 供 Dashboard 使用
  router.push({
    path: '/mobile/dashboard',
    query: lastJobId.value ? { backtest_id: lastJobId.value, from: 'workbench' } : { from: 'workbench' },
  });
}

// ============================================
// Socket.IO 事件
// ============================================

onMounted(() => {
  refreshFileList();
  connect(import.meta.env.VITE_API_URL || 'http://localhost:5000');

  onEvent('backtest_started', (data: any) => {
    pushLog(`任务已启动 (job_id=${data.job_id})`, 'info');
    if (data.job_id) lastJobId.value = data.job_id;
  });

  onEvent('backtest_progress', (data: { progress: number; message?: string }) => {
    if (data.message) {
      pushLog(`进度 ${data.progress}%: ${data.message}`, 'debug');
    }
  });

  onEvent('backtest_complete', (data: any) => {
    isRunning.value = false;
    const elapsedMs = backtestStartTime.value ? Date.now() - backtestStartTime.value : data.elapsed_ms || 0;
    backtestStartTime.value = null;

    if (Array.isArray(data.equity_curve)) {
      backtestStore.addEquityPoints(data.equity_curve.map((p: any) => ({
        date: p.date,
        equity: p.equity ?? p.value ?? 0,
      })));
    }
    if (Array.isArray(data.trades)) {
      backtestStore.setTrades(data.trades as Trade[]);
    }
    if (data.stats) {
      const s = data.stats;
      backtestStore.setMetrics({
        totalReturn: s.total_return ?? s.totalReturn ?? 0,
        annualReturn: s.annual_return ?? s.annualReturn ?? 0,
        sharpeRatio: s.sharpe_ratio ?? s.sharpeRatio ?? 0,
        maxDrawdown: s.max_drawdown ?? s.maxDrawdown ?? 0,
        volatility: s.volatility ?? 0,
        winRate: s.win_rate ?? s.winRate ?? 0,
        profitLossRatio: s.profit_loss_ratio ?? s.profitLossRatio ?? 0,
        totalTrades: s.total_trades ?? s.totalTrades ?? 0,
      } as BacktestMetrics);
    }
    backtestStore.setLastRunParams({
      strategyName: strategyName.value,
      startDate: config.value.startDate,
      endDate: config.value.endDate,
    });
    backtestStore.persistToStorage();

    pushLog(`回测完成 ✓ 用时 ${(elapsedMs / 1000).toFixed(2)}s`, 'success');
    if (data.job_id) lastJobId.value = data.job_id;
    showDashboardJump.value = true;
  });

  onEvent('backtest_error', (data: any) => {
    isRunning.value = false;
    backtestStartTime.value = null;
    const errMsg = data.message || data.error || '回测失败';
    pushLog(`回测失败: ${errMsg}`, 'error');
    if (data.detail) pushLog(`  详情: ${data.detail}`, 'error');
    if (data.sandbox) {
      if (data.timed_out) pushProblem('回测超时被强杀', 'error', data.code);
      if (data.crashed) pushProblem('回测进程崩溃', 'error', data.code);
    }
    if (data.code === 'CODE_AUDIT_FAILED') {
      pushProblem(data.detail || '代码安全审计未通过', 'error', data.code);
    }
  });
});

onBeforeUnmount(() => {
  disconnect();
});

// 监听代码变化更新行号
watch(code, () => {
  // 行号通过 computed 自动更新
});
</script>

<style scoped>
.mobile-workbench {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: calc(100vh - 108px);
  min-height: calc(100dvh - 108px);
  background: #1e1e1e;
  color: #cccccc;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  position: relative;
  overflow: hidden;
}

/* ============================================ */
/* 顶部策略栏 */
/* ============================================ */
.mw-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #2d2d2d;
  border-bottom: 1px solid #1e1e1e;
  flex-shrink: 0;
}

.mw-icon-btn {
  background: transparent;
  border: none;
  color: #cccccc;
  font-size: 16px;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}

.mw-icon-btn:active {
  background: #3c3c3c;
}

.mw-title {
  flex: 1;
  min-width: 0;
}

.mw-name-input {
  width: 100%;
  background: transparent;
  border: none;
  color: #ffffff;
  font-size: 15px;
  font-weight: 600;
  padding: 6px 4px;
  outline: none;
  border-bottom: 1px solid transparent;
}

.mw-name-input:focus {
  border-bottom-color: #007acc;
}

.mw-run-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  background: #007acc;
  color: #ffffff;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  flex-shrink: 0;
  -webkit-tap-highlight-color: transparent;
}

.mw-run-btn:active {
  background: #005a8b;
}

.mw-run-btn:disabled {
  background: #3c3c3c;
  color: #888888;
  cursor: default;
}

/* ============================================ */
/* 回测参数 */
/* ============================================ */
.mw-params {
  background: #252526;
  border-bottom: 1px solid #1e1e1e;
  flex-shrink: 0;
}

.mw-params-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px 12px;
  background: transparent;
  border: none;
  color: #cccccc;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}

.mw-params-toggle span {
  flex: 1;
}

.mw-params-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  padding: 8px 12px 12px;
}

.mw-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mw-field span {
  font-size: 11px;
  color: #888888;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.mw-field input {
  background: #1e1e1e;
  border: 1px solid #3c3c3c;
  border-radius: 4px;
  color: #ffffff;
  font-size: 13px;
  padding: 6px 8px;
  outline: none;
}

.mw-field input:focus {
  border-color: #007acc;
}

/* ============================================ */
/* 代码编辑器 */
/* ============================================ */
.mw-editor-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 200px;
  overflow: hidden;
}

.mw-editor-tabs {
  display: flex;
  align-items: center;
  background: #2d2d2d;
  border-bottom: 1px solid #1e1e1e;
  padding: 0 4px 0 0;
  flex-shrink: 0;
}

.mw-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  color: #ffffff;
  font-size: 12px;
  background: #1e1e1e;
  border-right: 1px solid #1e1e1e;
  flex: 1;
  min-width: 0;
}

.mw-tab span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mw-editor-body {
  flex: 1;
  display: flex;
  overflow: hidden;
  position: relative;
}

.mw-line-numbers {
  flex-shrink: 0;
  width: 36px;
  padding: 8px 4px 8px 0;
  text-align: right;
  background: #1e1e1e;
  color: #6e6e6e;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre;
  overflow: hidden;
  user-select: none;
  border-right: 1px solid #2d2d2d;
}

.mw-textarea {
  flex: 1;
  background: #1e1e1e;
  color: #d4d4d4;
  border: none;
  outline: none;
  resize: none;
  padding: 8px 10px;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.5;
  tab-size: 4;
  -moz-tab-size: 4;
  white-space: pre;
  overflow: auto;
  -webkit-overflow-scrolling: touch;
}

/* ============================================ */
/* 输出终端 */
/* ============================================ */
.mw-terminal {
  background: #1e1e1e;
  border-top: 1px solid #3c3c3c;
  max-height: 35vh;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.mw-terminal.collapsed {
  max-height: 36px;
}

.mw-terminal-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  background: #2d2d2d;
  border: none;
  color: #cccccc;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  flex-shrink: 0;
  -webkit-tap-highlight-color: transparent;
}

.mw-terminal-toggle span:nth-child(2) {
  flex: 1;
}

.mw-badge {
  background: #4e4e4e;
  color: #ffffff;
  padding: 1px 6px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
  min-width: 18px;
  text-align: center;
}

.mw-terminal-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 11px;
  -webkit-overflow-scrolling: touch;
}

.mw-log-line {
  display: flex;
  gap: 8px;
  padding: 1px 0;
  white-space: pre-wrap;
  word-break: break-all;
}

.mw-log-time {
  color: #858585;
  flex-shrink: 0;
}

.mw-log-msg {
  flex: 1;
}

.level-info .mw-log-msg { color: #cccccc; }
.level-warning .mw-log-msg { color: #cca700; }
.level-error .mw-log-msg { color: #f48771; }
.level-success .mw-log-msg { color: #89d185; }
.level-debug .mw-log-msg { color: #569cd6; }

.mw-empty {
  color: #6e6e6e;
  font-style: italic;
  text-align: center;
  padding: 12px 0;
}

/* ============================================ */
/* 问题面板 */
/* ============================================ */
.mw-problems {
  background: #1e1e1e;
  padding: 8px 12px;
  flex-shrink: 0;
}

.mw-problem-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 12px;
}

.sev-error .fa-circle-xmark {
  color: #f48771;
}

.sev-warning .fa-triangle-exclamation {
  color: #cca700;
}

/* ============================================ */
/* 跳转按钮 */
/* ============================================ */
.mw-dashboard-jump {
  position: fixed;
  right: 16px;
  bottom: calc(80px + env(safe-area-inset-bottom, 0px));
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: linear-gradient(135deg, #2962ff 0%, #007acc 100%);
  color: #ffffff;
  border: none;
  border-radius: 24px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  box-shadow: 0 4px 16px rgba(41, 98, 255, 0.5);
  z-index: 50;
  -webkit-tap-highlight-color: transparent;
}

.mw-dashboard-jump:active {
  transform: scale(0.95);
}

.mw-dashboard-jump i:first-child {
  font-size: 14px;
}

.mw-dashboard-jump i:last-child {
  font-size: 11px;
  opacity: 0.8;
}

/* ============================================ */
/* 文件抽屉 */
/* ============================================ */
.mw-drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 200;
}

.mw-drawer {
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  width: 80%;
  max-width: 320px;
  background: #252526;
  display: flex;
  flex-direction: column;
}

.mw-drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: #2d2d2d;
  border-bottom: 1px solid #1e1e1e;
  color: #ffffff;
  font-size: 14px;
  font-weight: 600;
}

.mw-drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}

.mw-drawer-section {
  border-bottom: 1px solid #1e1e1e;
}

.mw-drawer-title {
  padding: 8px 16px;
  font-size: 11px;
  font-weight: 600;
  color: #888888;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.mw-file-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.mw-file-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  cursor: pointer;
  color: #cccccc;
  font-size: 13px;
  -webkit-tap-highlight-color: transparent;
}

.mw-file-item:active {
  background: #37373d;
}

.mw-file-item.readonly {
  opacity: 0.7;
}

.mw-file-item .fa-file-code {
  color: #4ec9b0;
  font-size: 14px;
}

.mw-file-item .fa-lock {
  color: #888888;
  font-size: 10px;
  margin-left: auto;
}

.mw-file-item span {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ============================================ */
/* 动画 */
/* ============================================ */
.mw-fade-enter-active,
.mw-fade-leave-active {
  transition: opacity 0.3s, transform 0.3s;
}

.mw-fade-enter-from,
.mw-fade-leave-to {
  opacity: 0;
  transform: translateY(20px);
}

.mw-drawer-enter-active,
.mw-drawer-leave-active {
  transition: opacity 0.25s;
}

.mw-drawer-enter-active .mw-drawer,
.mw-drawer-leave-active .mw-drawer {
  transition: transform 0.25s cubic-bezier(0.22, 1, 0.36, 1);
}

.mw-drawer-enter-from,
.mw-drawer-leave-to {
  opacity: 0;
}

.mw-drawer-enter-from .mw-drawer,
.mw-drawer-leave-to .mw-drawer {
  transform: translateX(-100%);
}
</style>
