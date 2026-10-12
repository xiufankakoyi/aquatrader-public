<template>
  <div class="strategy-config-panel h-full flex flex-col">
    <!-- 面板标题 -->
    <div class="px-4 py-3 border-b border-[#2a2e39]">
      <div class="flex items-center justify-between">
        <span class="tv-pane-label">回测配置 (CONFIG)</span>
        <span class="text-[10px] text-[#787b86]" :class="{ 'text-[#f23645]': !currentStrategy }">
          {{ currentStrategy?.name || '未选择策略' }}
        </span>
      </div>
    </div>

    <!-- 配置表单 -->
    <div class="flex-1 overflow-y-auto no-scrollbar p-4 space-y-5">
      <!-- 时间范围 -->
      <div class="config-section">
        <div class="flex items-center gap-2 mb-3">
          <i class="fas fa-calendar-alt text-[#2962ff] text-xs"></i>
          <span class="text-[11px] font-semibold text-[#d1d4dc] uppercase tracking-wider">时间范围</span>
        </div>
        <div class="space-y-2">
          <div class="flex items-center gap-2">
            <span class="text-[10px] text-[#787b86] w-10">开始</span>
            <input
              v-model="config.startDate"
              type="date"
              class="flex-1 bg-[#1e222d] border border-[#2a2e39] rounded px-2 py-1.5 text-[11px] text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none transition-colors"
            />
          </div>
          <div class="flex items-center gap-2">
            <span class="text-[10px] text-[#787b86] w-10">结束</span>
            <input
              v-model="config.endDate"
              type="date"
              class="flex-1 bg-[#1e222d] border border-[#2a2e39] rounded px-2 py-1.5 text-[11px] text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none transition-colors"
            />
          </div>
        </div>
      </div>

      <!-- 资金配置 -->
      <div class="config-section">
        <div class="flex items-center gap-2 mb-3">
          <i class="fas fa-wallet text-[#2962ff] text-xs"></i>
          <span class="text-[11px] font-semibold text-[#d1d4dc] uppercase tracking-wider">资金配置</span>
        </div>
        <div class="space-y-3">
          <div>
            <div class="flex justify-between mb-1.5">
              <span class="text-[10px] text-[#787b86]">初始资金</span>
              <span class="text-[10px] text-[#d1d4dc]">¥{{ formatNumber(config.initialCapital) }}</span>
            </div>
            <input
              v-model.number="config.initialCapital"
              type="range"
              min="100000"
              max="10000000"
              step="100000"
              class="w-full h-1 bg-[#2a2e39] rounded-lg appearance-none cursor-pointer accent-[#2962ff]"
            />
            <div class="flex justify-between mt-1">
              <span class="text-[9px] text-[#787b86]">10万</span>
              <span class="text-[9px] text-[#787b86]">1000万</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 交易成本 -->
      <div class="config-section">
        <div class="flex items-center gap-2 mb-3">
          <i class="fas fa-percentage text-[#2962ff] text-xs"></i>
          <span class="text-[11px] font-semibold text-[#d1d4dc] uppercase tracking-wider">交易成本</span>
        </div>
        <div class="space-y-3">
          <div>
            <div class="flex justify-between mb-1.5">
              <span class="text-[10px] text-[#787b86]">佣金率</span>
              <span class="text-[10px] text-[#d1d4dc]">{{ (config.commission * 100).toFixed(3) }}%</span>
            </div>
            <input
              v-model.number="config.commission"
              type="range"
              min="0.0001"
              max="0.003"
              step="0.0001"
              class="w-full h-1 bg-[#2a2e39] rounded-lg appearance-none cursor-pointer accent-[#2962ff]"
            />
          </div>
          <div>
            <div class="flex justify-between mb-1.5">
              <span class="text-[10px] text-[#787b86]">滑点</span>
              <span class="text-[10px] text-[#d1d4dc]">{{ (config.slippage * 100).toFixed(2) }}%</span>
            </div>
            <input
              v-model.number="config.slippage"
              type="range"
              min="0"
              max="0.01"
              step="0.0001"
              class="w-full h-1 bg-[#2a2e39] rounded-lg appearance-none cursor-pointer accent-[#2962ff]"
            />
          </div>
        </div>
      </div>

      <!-- 基准对比 -->
      <div class="config-section">
        <div class="flex items-center gap-2 mb-3">
          <i class="fas fa-chart-line text-[#2962ff] text-xs"></i>
          <span class="text-[11px] font-semibold text-[#d1d4dc] uppercase tracking-wider">基准对比</span>
        </div>
        <select
          v-model="config.benchmark"
          class="w-full bg-[#1e222d] border border-[#2a2e39] rounded px-2 py-1.5 text-[11px] text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none transition-colors"
        >
          <option value="000300.SH">沪深300 (000300.SH)</option>
          <option value="000905.SH">中证500 (000905.SH)</option>
          <option value="000852.SH">中证1000 (000852.SH)</option>
          <option value="399006.SZ">创业板指 (399006.SZ)</option>
          <option value="000001.SH">上证指数 (000001.SH)</option>
        </select>
      </div>

      <!-- 策略参数摘要 -->
      <div class="config-section" v-if="strategyParams.length > 0">
        <div class="flex items-center gap-2 mb-3">
          <i class="fas fa-sliders-h text-[#2962ff] text-xs"></i>
          <span class="text-[11px] font-semibold text-[#d1d4dc] uppercase tracking-wider">策略参数</span>
        </div>
        <div class="bg-[#1e222d] rounded border border-[#2a2e39] p-3 space-y-2">
          <div
            v-for="param in strategyParams.slice(0, 5)"
            :key="param.key"
            class="flex justify-between items-center"
          >
            <span class="text-[10px] text-[#787b86]">{{ param.label }}</span>
            <span class="text-[10px] text-[#d1d4dc] font-mono">{{ formatParamValue(param) }}</span>
          </div>
          <div v-if="strategyParams.length > 5" class="text-center pt-1">
            <span class="text-[9px] text-[#787b86]">还有 {{ strategyParams.length - 5 }} 个参数...</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 操作按钮 -->
    <div class="p-4 border-t border-[#2a2e39] space-y-2">
      <button
        @click="handleRunBacktest"
        :disabled="isRunning || !canRun"
        :title="buttonDisabledReason"
        class="w-full py-2.5 bg-[#2962ff] hover:bg-[#1e4bd8] disabled:bg-[#2a2e39] disabled:text-[#787b86] text-white text-[11px] font-bold uppercase tracking-wider rounded transition-colors flex items-center justify-center gap-2"
      >
        <i v-if="isRunning" class="fas fa-spinner fa-spin"></i>
        <i v-else class="fas fa-play"></i>
        {{ isRunning ? '回测运行中...' : '运行回测' }}
      </button>
      <!-- 禁用原因提示 -->
      <div v-if="!isRunning && !canRun" class="text-[10px] text-[#787b86] text-center">
        {{ buttonDisabledReason }}
      </div>
      <div class="flex gap-2">
        <button
          @click="resetConfig"
          class="flex-1 py-2 bg-[#2a2e39] hover:bg-[#363a45] text-[#787b86] hover:text-[#d1d4dc] text-[10px] font-medium rounded transition-colors"
        >
          <i class="fas fa-undo mr-1"></i> 重置
        </button>
        <button
          @click="saveConfig"
          class="flex-1 py-2 bg-[#2a2e39] hover:bg-[#363a45] text-[#787b86] hover:text-[#d1d4dc] text-[10px] font-medium rounded transition-colors"
        >
          <i class="fas fa-save mr-1"></i> 保存
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { useStrategyStore } from '../store/strategyStore';
import { useBacktestStore } from '../store/backtestStore';
import { useDashboardStore } from '../store/dashboardStore';
import { DEFAULT_DATES } from '../config/strategyConfig';

/**
 * 策略配置面板组件
 * 用于在回测前配置时间范围、资金、交易成本等参数
 */

defineOptions({
  name: 'StrategyConfigPanel'
});

interface Config {
  startDate: string;
  endDate: string;
  initialCapital: number;
  commission: number;
  slippage: number;
  benchmark: string;
}

interface StrategyParam {
  key: string;
  label: string;
  value: number | string | boolean;
  unit?: string;
}

const props = defineProps<{
  isRunning?: boolean;
}>();

const emit = defineEmits<{
  (e: 'run', config: Config): void;
  (e: 'save', config: Config): void;
}>();

const strategyStore = useStrategyStore();
const backtestStore = useBacktestStore();
const dashboardStore = useDashboardStore();

// 当前策略版本（优先使用 dashboardStore，因为它与 TopBar 同步）
const currentStrategy = computed(() => dashboardStore.selectedStrategy);

// 默认配置
const defaultConfig: Config = {
  startDate: DEFAULT_DATES.startDate,
  endDate: DEFAULT_DATES.endDate,
  initialCapital: 1000000,
  commission: 0.0003,
  slippage: 0.001,
  benchmark: '000300.SH'
};

// 当前配置
const config = ref<Config>({ ...defaultConfig });

// 从上次回测参数恢复
onMounted(() => {
  if (backtestStore.lastRunParams) {
    config.value.startDate = backtestStore.lastRunParams.startDate || defaultConfig.startDate;
    config.value.endDate = backtestStore.lastRunParams.endDate || defaultConfig.endDate;
  }
});

// 按钮禁用原因
const buttonDisabledReason = computed(() => {
  if (props.isRunning) return '回测运行中...';
  if (!currentStrategy.value) return '请先选择策略（在顶部导航栏选择）';
  if (!config.value.startDate) return '请设置开始日期';
  if (!config.value.endDate) return '请设置结束日期';
  if (new Date(config.value.startDate) >= new Date(config.value.endDate)) {
    return '开始日期必须早于结束日期';
  }
  return '';
});

// 是否可以运行回测
const canRun = computed(() => {
  const hasStrategy = !!currentStrategy.value;
  const hasStartDate = !!config.value.startDate;
  const hasEndDate = !!config.value.endDate;
  const validDateRange = hasStartDate && hasEndDate &&
    new Date(config.value.startDate) < new Date(config.value.endDate);

  // 调试日志
  if (!hasStrategy) {
    console.log('[StrategyConfigPanel] 无法运行: 未选择策略');
  }
  if (!hasStartDate) {
    console.log('[StrategyConfigPanel] 无法运行: 未设置开始日期');
  }
  if (!hasEndDate) {
    console.log('[StrategyConfigPanel] 无法运行: 未设置结束日期');
  }
  if (hasStartDate && hasEndDate && !validDateRange) {
    console.log('[StrategyConfigPanel] 无法运行: 日期范围无效');
  }

  return hasStrategy && hasStartDate && hasEndDate && validDateRange;
});

// 策略参数列表
const strategyParams = computed<StrategyParam[]>(() => {
  // 尝试从 dashboardStore 获取策略参数
  const strategy = currentStrategy.value;
  if (!strategy) return [];
  
  // 如果策略对象有 params 属性，使用它
  const params = (strategy as any).params;
  if (!params) return [];

  return Object.entries(params).map(([key, value]) => ({
    key,
    label: formatParamLabel(key),
    value: value as number | string | boolean,
    unit: getParamUnit(key)
  }));
});

// 格式化参数标签
function formatParamLabel(key: string): string {
  const labelMap: Record<string, string> = {
    // 均线策略参数
    fast_period: '快线周期',
    slow_period: '慢线周期',
    fast_ma: '快线周期',
    slow_ma: '慢线周期',
    signal_period: '信号周期',
    // MACD策略参数
    fast_period_macd: '快线周期',
    slow_period_macd: '慢线周期',
    signal_period_macd: '信号周期',
    // 布林带参数
    bb_period: '布林带周期',
    bb_std: '标准差倍数',
    period: '周期',
    std_dev: '标准差',
    // 通用参数
    market_cap_min: '最小市值',
    market_cap_max: '最大市值',
    pe_ratio_min: '最小PE',
    pe_ratio_max: '最大PE',
    pb_ratio_min: '最小PB',
    pb_ratio_max: '最大PB',
    lookback_period: '回看周期',
    holding_period: '持仓周期',
    stop_loss: '止损比例',
    take_profit: '止盈比例',
    position_size: '仓位比例',
    max_positions: '最大持仓数',
    // RSI参数
    rsi_period: 'RSI周期',
    rsi_overbought: '超买阈值',
    rsi_oversold: '超卖阈值',
    // KDJ参数
    k_period: 'K周期',
    d_period: 'D周期',
    j_period: 'J周期'
  };
  return labelMap[key] || key;
}

// 获取参数单位
function getParamUnit(key: string): string | undefined {
  if (key.includes('market_cap')) return '万';
  if (key.includes('ratio')) return '倍';
  if (key.includes('period')) return '天';
  if (key.includes('loss') || key.includes('profit') || key.includes('size')) return '%';
  return undefined;
}

// 格式化参数值
function formatParamValue(param: StrategyParam): string {
  if (typeof param.value === 'boolean') return param.value ? '是' : '否';
  if (typeof param.value === 'number') {
    const num = param.value;
    if (Math.abs(num) >= 10000) {
      return (num / 10000).toFixed(1) + (param.unit ? '' : '万');
    }
    return num.toFixed(2) + (param.unit || '');
  }
  return String(param.value);
}

// 格式化数字
function formatNumber(num: number): string {
  return num.toLocaleString('zh-CN');
}

// 运行回测
function handleRunBacktest() {
  if (!canRun.value || props.isRunning) return;
  emit('run', { ...config.value });
}

// 保存配置
function saveConfig() {
  emit('save', { ...config.value });
  // 可以添加本地存储逻辑
  localStorage.setItem('strategy_config', JSON.stringify(config.value));
}

// 重置配置
function resetConfig() {
  config.value = { ...defaultConfig };
}

// 监听配置变化，自动保存到本地
watch(config, (newConfig) => {
  localStorage.setItem('strategy_config_draft', JSON.stringify(newConfig));
}, { deep: true });
</script>

<style scoped>
.strategy-config-panel {
  background: #0A0A0A;
}

/* 自定义滑块样式 */
input[type="range"]::-webkit-slider-thumb {
  appearance: none;
  width: 12px;
  height: 12px;
  background: #2962ff;
  border-radius: 50%;
  cursor: pointer;
  transition: transform 0.15s ease;
}

input[type="range"]::-webkit-slider-thumb:hover {
  transform: scale(1.2);
}

input[type="range"]::-moz-range-thumb {
  width: 12px;
  height: 12px;
  background: #2962ff;
  border-radius: 50%;
  cursor: pointer;
  border: none;
}

/* 日期选择器样式 */
input[type="date"]::-webkit-calendar-picker-indicator {
  filter: invert(0.5);
  cursor: pointer;
}

input[type="date"]::-webkit-calendar-picker-indicator:hover {
  filter: invert(0.7);
}

/* 配置区块 */
.config-section {
  position: relative;
}

.config-section:not(:last-child)::after {
  content: '';
  position: absolute;
  bottom: -10px;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, #2a2e39, transparent);
}
</style>
