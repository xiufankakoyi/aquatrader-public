<template>
  <div class="data-panel">
    <!-- Tab 导航 -->
    <div class="tab-nav">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        class="tab-btn"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >
        <i :class="tab.icon"></i>
        <span>{{ tab.label }}</span>
        <span v-if="tab.badge" class="tab-badge" :class="tab.badgeType">
          {{ tab.badge }}
        </span>
      </button>
    </div>

    <!-- Tab 内容 -->
    <div class="tab-content">
      <!-- 交易流水 Tab -->
      <div v-show="activeTab === 'trades'" class="tab-pane">
        <div class="table-container">
          <table class="data-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>代码</th>
                <th>动作</th>
                <th>价格</th>
                <th>数量</th>
                <th>盈亏</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="trade in sortedTrades"
                :key="trade.id"
                :class="{ 'highlight': selectedTrade?.id === trade.id }"
                @click="selectTrade(trade)"
              >
                <td class="font-mono">{{ trade.date }}</td>
                <td class="font-mono">{{ trade.stockCode }}</td>
                <td>
                  <span
                    class="action-badge"
                    :class="trade.action"
                  >
                    {{ trade.action === 'buy' ? '买入' : '卖出' }}
                  </span>
                </td>
                <td class="font-mono number">{{ formatPrice(trade.price) }}</td>
                <td class="font-mono number">{{ trade.volume }}</td>
                <td
                  class="font-mono number"
                  :class="getPnLClass(trade.pnl)"
                >
                  {{ formatPnL(trade.pnl) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        
        <div v-if="sortedTrades.length === 0" class="empty-tab">
          <i class="fas fa-exchange-alt"></i>
          <p>暂无交易记录</p>
        </div>
      </div>

      <!-- 每日持仓 Tab -->
      <div v-show="activeTab === 'positions'" class="tab-pane">
        <div class="date-selector">
          <label>选择日期:</label>
          <input
            v-model="selectedPositionDate"
            type="date"
            class="date-input"
            @change="updatePositionsByDate"
          />
        </div>
        
        <div class="table-container">
          <table class="data-table">
            <thead>
              <tr>
                <th>代码</th>
                <th>名称</th>
                <th>持仓量</th>
                <th>成本价</th>
                <th>市值</th>
                <th>盈亏</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="pos in filteredPositions" :key="pos.stockCode">
                <td class="font-mono">{{ pos.stockCode }}</td>
                <td>{{ pos.stockName }}</td>
                <td class="font-mono number">{{ pos.volume }}</td>
                <td class="font-mono number">{{ formatPrice(pos.costPrice) }}</td>
                <td class="font-mono number">{{ formatPrice(pos.marketValue) }}</td>
                <td
                  class="font-mono number"
                  :class="getPnLClass(pos.pnl)"
                >
                  {{ formatPnL(pos.pnl) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        
        <div v-if="filteredPositions.length === 0" class="empty-tab">
          <i class="fas fa-briefcase"></i>
          <p>该日期无持仓</p>
        </div>
      </div>

      <!-- 运行日志 Tab -->
      <div v-show="activeTab === 'logs'" class="tab-pane">
        <div class="log-filters">
          <button
            class="filter-btn"
            :class="{ active: logFilter === 'all' }"
            @click="logFilter = 'all'"
          >
            全部
          </button>
          <button
            class="filter-btn"
            :class="{ active: logFilter === 'info' }"
            @click="logFilter = 'info'"
          >
            信息
          </button>
          <button
            class="filter-btn"
            :class="{ active: logFilter === 'success' }"
            @click="logFilter = 'success'"
          >
            成功
          </button>
          <button
            class="filter-btn"
            :class="{ active: logFilter === 'error' }"
            @click="logFilter = 'error'"
          >
            错误
          </button>
        </div>
        
        <div ref="logContainer" class="log-container">
          <div
            v-for="(log, index) in filteredLogs"
            :key="index"
            class="log-item"
            :class="log.type"
          >
            <span class="log-time">{{ log.time }}</span>
            <span class="log-type-badge" :class="log.type">
              {{ getLogTypeLabel(log.type) }}
            </span>
            <span class="log-message">{{ log.message }}</span>
          </div>
        </div>
        
        <div v-if="filteredLogs.length === 0" class="empty-tab">
          <i class="fas fa-terminal"></i>
          <p>暂无日志</p>
        </div>
      </div>

      <!-- 统计报告 Tab -->
      <div v-show="activeTab === 'stats'" class="tab-pane">
        <div v-if="metrics" class="stats-content">
          <!-- 雷达图 -->
          <div class="stats-section">
            <h4 class="section-title">策略表现雷达图</h4>
            <div ref="radarChartRef" class="radar-chart"></div>
          </div>
          
          <!-- 关键指标 -->
          <div class="stats-section">
            <h4 class="section-title">关键指标</h4>
            <div class="metrics-grid">
              <div class="metric-card">
                <span class="metric-label">总收益率</span>
                <span
                  class="metric-value"
                  :class="metrics.totalReturn >= 0 ? 'positive' : 'negative'"
                >
                  {{ formatPercent(metrics.totalReturn) }}
                </span>
              </div>
              <div class="metric-card">
                <span class="metric-label">年化收益</span>
                <span
                  class="metric-value"
                  :class="metrics.annualReturn >= 0 ? 'positive' : 'negative'"
                >
                  {{ formatPercent(metrics.annualReturn) }}
                </span>
              </div>
              <div class="metric-card">
                <span class="metric-label">夏普比率</span>
                <span class="metric-value">{{ metrics.sharpeRatio.toFixed(2) }}</span>
              </div>
              <div class="metric-card">
                <span class="metric-label">最大回撤</span>
                <span class="metric-value negative">
                  {{ formatPercent(metrics.maxDrawdown) }}
                </span>
              </div>
              <div class="metric-card">
                <span class="metric-label">胜率</span>
                <span class="metric-value">{{ formatPercent(metrics.winRate) }}</span>
              </div>
              <div class="metric-card">
                <span class="metric-label">盈亏比</span>
                <span class="metric-value">{{ metrics.profitLossRatio.toFixed(2) }}</span>
              </div>
              <div class="metric-card">
                <span class="metric-label">交易次数</span>
                <span class="metric-value">{{ metrics.totalTrades }}</span>
              </div>
              <div class="metric-card">
                <span class="metric-label">平均持仓</span>
                <span class="metric-value">{{ metrics.avgHoldingDays }}天</span>
              </div>
            </div>
          </div>
          
          <!-- 月度回报表 -->
          <div class="stats-section">
            <h4 class="section-title">月度回报</h4>
            <div class="monthly-returns">
              <div
                v-for="(ret, month) in metrics.monthlyReturns"
                :key="month"
                class="month-item"
              >
                <span class="month-label">{{ month }}</span>
                <div class="month-bar-container">
                  <div
                    class="month-bar"
                    :class="ret >= 0 ? 'positive' : 'negative'"
                    :style="{ width: Math.abs(ret * 100) + '%' }"
                  ></div>
                </div>
                <span
                  class="month-value"
                  :class="ret >= 0 ? 'positive' : 'negative'"
                >
                  {{ formatPercent(ret) }}
                </span>
              </div>
            </div>
          </div>
        </div>
        
        <div v-else class="empty-tab">
          <i class="fas fa-chart-pie"></i>
          <p>运行回测后查看统计报告</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick } from 'vue';
import * as echarts from 'echarts';
import type { ECharts } from 'echarts';
import type { Trade, Position, LogEntry, BacktestMetrics } from '../../types/backtest';

// ============================================
// Props & Emits
// ============================================
interface Props {
  trades: Trade[];
  positions: Position[];
  logs: LogEntry[];
  errors: LogEntry[];
  metrics: BacktestMetrics | null;
  selectedDate: string;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  'trade-click': [trade: Trade];
  'date-change': [date: string];
}>();

// ============================================
// 状态
// ============================================
const activeTab = ref('trades');
const selectedTrade = ref<Trade | null>(null);
const selectedPositionDate = ref(props.selectedDate);
const logFilter = ref<'all' | 'info' | 'success' | 'error'>('all');
const logContainer = ref<HTMLElement>();
const radarChartRef = ref<HTMLElement>();
let radarChart: ECharts | null = null;

const tabs = [
  { id: 'trades', label: '交易流水', icon: 'fas fa-exchange-alt' },
  { id: 'positions', label: '每日持仓', icon: 'fas fa-briefcase' },
  { id: 'logs', label: '运行日志', icon: 'fas fa-terminal', badge: computed(() => props.errors.length), badgeType: 'error' },
  { id: 'stats', label: '统计报告', icon: 'fas fa-chart-pie' },
];

// ============================================
// 计算属性
// ============================================
const sortedTrades = computed(() => {
  return [...props.trades].sort((a, b) => 
    new Date(b.date).getTime() - new Date(a.date).getTime()
  );
});

const filteredPositions = computed(() => {
  if (!selectedPositionDate.value) return props.positions;
  return props.positions.filter(p => p.date === selectedPositionDate.value);
});

const filteredLogs = computed(() => {
  let logs = [...props.logs, ...props.errors];
  if (logFilter.value !== 'all') {
    logs = logs.filter(l => l.type === logFilter.value);
  }
  return logs.sort((a, b) => 
    new Date(b.time).getTime() - new Date(a.time).getTime()
  );
});

// ============================================
// 方法
// ============================================
function selectTrade(trade: Trade) {
  selectedTrade.value = trade;
  emit('trade-click', trade);
}

function updatePositionsByDate() {
  emit('date-change', selectedPositionDate.value);
}

function formatPrice(price: number): string {
  if (price === undefined || price === null) return '--';
  return price.toFixed(2);
}

function formatPnL(pnl: number): string {
  if (pnl === undefined || pnl === null) return '--';
  const sign = pnl >= 0 ? '+' : '';
  return `${sign}${pnl.toFixed(2)}`;
}

function getPnLClass(pnl: number): string {
  if (pnl === undefined || pnl === null) return '';
  return pnl >= 0 ? 'positive' : 'negative';
}

function formatPercent(value: number): string {
  if (value === undefined || value === null) return '--';
  const sign = value >= 0 ? '+' : '';
  return `${sign}${(value * 100).toFixed(2)}%`;
}

function getLogTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    info: '信息',
    success: '成功',
    error: '错误',
    warning: '警告',
  };
  return labels[type] || type;
}

// 初始化雷达图
function initRadarChart() {
  if (!radarChartRef.value || !props.metrics) return;
  
  radarChart = echarts.init(radarChartRef.value);
  
  const option = {
    backgroundColor: 'transparent',
    radar: {
      indicator: [
        { name: '收益率', max: 100 },
        { name: '夏普比率', max: 3 },
        { name: '胜率', max: 100 },
        { name: '盈亏比', max: 5 },
        { name: '稳定性', max: 100 },
        { name: '流动性', max: 100 },
      ],
      shape: 'polygon',
      splitNumber: 4,
      axisName: {
        color: '#787b86',
        fontSize: 10,
      },
      splitLine: {
        lineStyle: {
          color: '#2a2e39',
        },
      },
      splitArea: {
        areaStyle: {
          color: ['transparent', 'rgba(41, 98, 255, 0.05)'],
        },
      },
      axisLine: {
        lineStyle: {
          color: '#2a2e39',
        },
      },
    },
    series: [{
      type: 'radar',
      data: [{
        value: [
          props.metrics.totalReturn * 100,
          props.metrics.sharpeRatio,
          props.metrics.winRate * 100,
          props.metrics.profitLossRatio,
          80, // 稳定性 (示例)
          70, // 流动性 (示例)
        ],
        name: '策略表现',
        areaStyle: {
          color: 'rgba(41, 98, 255, 0.3)',
        },
        lineStyle: {
          color: '#2962ff',
          width: 2,
        },
        itemStyle: {
          color: '#2962ff',
        },
      }],
    }],
  };
  
  radarChart.setOption(option);
}

// 自动滚动日志到底部
function scrollToBottom() {
  if (logContainer.value) {
    logContainer.value.scrollTop = logContainer.value.scrollHeight;
  }
}

// ============================================
// 监听
// ============================================
watch(() => props.selectedDate, (date) => {
  selectedPositionDate.value = date;
});

watch(() => props.logs, () => {
  nextTick(scrollToBottom);
}, { deep: true });

watch(() => props.metrics, () => {
  if (activeTab.value === 'stats') {
    nextTick(initRadarChart);
  }
}, { deep: true });

watch(activeTab, (tab) => {
  if (tab === 'stats' && props.metrics) {
    nextTick(initRadarChart);
  }
});

// ============================================
// 生命周期
// ============================================
onMounted(() => {
  if (activeTab.value === 'stats' && props.metrics) {
    initRadarChart();
  }
  
  window.addEventListener('resize', () => {
    radarChart?.resize();
  });
});
</script>

<style scoped>
.data-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}

/* Tab 导航 */
.tab-nav {
  display: flex;
  height: 36px;
  background-color: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
  overflow-x: auto;
}

.tab-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 14px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
}

.tab-btn:hover {
  color: var(--text-primary);
  background-color: var(--bg-hover);
}

.tab-btn.active {
  color: var(--accent-primary);
  border-bottom-color: var(--accent-primary);
  background-color: var(--bg-tertiary);
}

.tab-btn i {
  font-size: 10px;
}

.tab-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  background-color: var(--color-down);
  color: white;
  font-size: 9px;
  border-radius: 8px;
}

/* Tab 内容 */
.tab-content {
  flex: 1;
  overflow: hidden;
}

.tab-pane {
  height: 100%;
  display: flex;
  flex-direction: column;
}

/* 表格样式 */
.table-container {
  flex: 1;
  overflow: auto;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}

.data-table th {
  position: sticky;
  top: 0;
  padding: 8px 10px;
  background-color: var(--bg-secondary);
  color: var(--text-muted);
  font-weight: 500;
  text-align: left;
  text-transform: uppercase;
  font-size: 9px;
  letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border-color);
  white-space: nowrap;
}

.data-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-color);
  color: var(--text-primary);
  white-space: nowrap;
}

.data-table tbody tr {
  cursor: pointer;
  transition: background-color 0.15s;
}

.data-table tbody tr:hover {
  background-color: var(--bg-hover);
}

.data-table tbody tr.highlight {
  background-color: rgba(41, 98, 255, 0.1);
}

.action-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 6px;
  border-radius: 3px;
  font-size: 9px;
  font-weight: 500;
  text-transform: uppercase;
}

.action-badge.buy {
  background-color: rgba(8, 153, 129, 0.15);
  color: var(--color-up);
}

.action-badge.sell {
  background-color: rgba(242, 54, 69, 0.15);
  color: var(--color-down);
}

.font-mono {
  font-family: 'JetBrains Mono', monospace;
}

.number {
  font-variant-numeric: tabular-nums;
}

.positive {
  color: var(--color-up);
}

.negative {
  color: var(--color-down);
}

/* 日期选择器 */
.date-selector {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
}

.date-selector label {
  font-size: 11px;
  color: var(--text-muted);
}

.date-input {
  height: 26px;
  padding: 0 8px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  color: var(--text-primary);
  font-size: 11px;
  outline: none;
}

/* 日志样式 */
.log-filters {
  display: flex;
  gap: 4px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
}

.filter-btn {
  padding: 4px 10px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  color: var(--text-secondary);
  font-size: 10px;
  cursor: pointer;
  transition: all 0.2s;
}

.filter-btn:hover {
  border-color: var(--border-hover);
  color: var(--text-primary);
}

.filter-btn.active {
  background-color: var(--accent-primary);
  border-color: var(--accent-primary);
  color: white;
}

.log-container {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}

.log-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--border-color);
}

.log-time {
  color: var(--text-muted);
  font-size: 10px;
  flex-shrink: 0;
}

.log-type-badge {
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 9px;
  text-transform: uppercase;
  flex-shrink: 0;
}

.log-type-badge.info {
  background-color: rgba(41, 98, 255, 0.15);
  color: var(--accent-primary);
}

.log-type-badge.success {
  background-color: rgba(8, 153, 129, 0.15);
  color: var(--color-up);
}

.log-type-badge.error {
  background-color: rgba(242, 54, 69, 0.15);
  color: var(--color-down);
}

.log-message {
  color: var(--text-primary);
  word-break: break-all;
}

/* 统计报告 */
.stats-content {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}

.stats-section {
  margin-bottom: 20px;
}

.section-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 12px;
}

.radar-chart {
  height: 200px;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.metric-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 4px;
}

.metric-label {
  font-size: 10px;
  color: var(--text-muted);
}

.metric-value {
  font-size: 16px;
  font-weight: 600;
  font-family: 'JetBrains Mono', monospace;
}

.monthly-returns {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.month-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.month-label {
  width: 50px;
  font-size: 10px;
  color: var(--text-muted);
  font-family: 'JetBrains Mono', monospace;
}

.month-bar-container {
  flex: 1;
  height: 16px;
  background-color: var(--bg-tertiary);
  border-radius: 3px;
  overflow: hidden;
}

.month-bar {
  height: 100%;
  border-radius: 3px;
  min-width: 2px;
}

.month-bar.positive {
  background-color: var(--color-up);
}

.month-bar.negative {
  background-color: var(--color-down);
}

.month-value {
  width: 50px;
  text-align: right;
  font-size: 10px;
  font-family: 'JetBrains Mono', monospace;
}

/* 空状态 */
.empty-tab {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
}

.empty-tab i {
  font-size: 32px;
  opacity: 0.3;
}

.empty-tab p {
  font-size: 12px;
}

/* 响应式 */
@media (max-width: 767px) {
  .metrics-grid {
    grid-template-columns: 1fr;
  }
  
  .tab-btn span {
    display: none;
  }
  
  .tab-btn {
    padding: 0 10px;
  }
}
</style>
