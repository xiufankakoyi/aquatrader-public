<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue';
import { storeToRefs } from 'pinia';
import { useStrategyStore, type ParameterSearchResult } from "@/store/strategyStore";
import { useOptimization } from '@/composables/useOptimization';
import { useAlgorithmParams } from '@/composables/useAlgorithmParams';
import { useDateRange } from '@/composables/useDateRange';
import { useStrategyParams } from '@/composables/useStrategyParams';
import OptimizationVisualizer from '@/components/OptimizationVisualizer.vue';
import ThreeSegmentTimeSlider from '@/components/ThreeSegmentTimeSlider.vue';
import EquityCurve from '../components/EquityCurve.vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';
import { 
  CUSTOM_PARAM_CONFIG, 
  ALGO_CONFIGS, 
  type AlgorithmKey 
} from '@/config/strategyConfig';
import {
  getParamUnit,
  toUiValue,
  toBackendValue,
  formatParamDisplayValue,
  getParamLabel
} from '@/utils/paramUtils';

type SearchResult = ParameterSearchResult;
type TabMode = 'auto' | 'manual';

// ==================== Tab State ====================
const activeTab = ref<TabMode>('auto');

// ==================== Auto Mode: Grid Search ====================
const {
  isOptimizing,
  progress,
  optimizationStatus,
  evaluatedCount,
  totalIterations,
  bestMetric,
  bestParams,
  evaluationResults,
  optimizationHistory,
  candidates,
  finalSelected,
  startOptimization: startOpt,
  stopOptimization: stopOpt,
  setupSocketListeners,
  cleanup: cleanupOptimization,
} = useOptimization();

const selectedStrategy = ref('');
const {
  availableParams,
  selectedParamKeys,
  paramLocked,
  paramRangeValues,
  isLoadingParams,
  fetchStrategyParams,
} = useStrategyParams(selectedStrategy);

const selectedAlgorithm = ref<AlgorithmKey>('ga');
const {
  algorithmParams,
  currentAlgoConfig,
  currentAlgoParams,
  loadAlgorithmParams,
} = useAlgorithmParams(selectedAlgorithm);

const {
  startDate,
  endDate,
  trainStartDate,
  trainEndDate,
  valStartDate,
  valEndDate,
  testStartDate,
  testEndDate,
  applyInitialDates,
} = useDateRange();

const strategyStore = useStrategyStore();
const { parameterSearchResults } = storeToRefs(strategyStore);
const searchResults = parameterSearchResults;

const maxIterations = ref(100);
const populationSize = ref(50);
const selectedMetric = ref('sharpeRatio');
const optimizationMode = ref<'quick_explore' | 'robust' | 'aggressive'>('robust');
const optimizationObjective = ref<'robust_trend_score' | 'rr_risk_score' | 'multi_period_robust' | 'calmar' | 'sharpe'>('robust_trend_score');
const selectedResult = ref<SearchResult | null>(null);
const candidateDetail = ref<any | null>(null);
const showSensitivity = ref(false);
const sensitivityParamKey = ref('');
const sensitivityPoints = ref<Array<{ value: number; score: number }>>([]);
const sensitivityChartContainer = ref<HTMLElement | null>(null);
let sensitivityChartInstance: echarts.ECharts | null = null;
const enableDiversity = ref(true);
const diversityThreshold = ref(0.15);
const maxDiverseResults = ref(10);

// ==================== Manual Mode: Param Compare ====================
const originalParams = ref<Record<string, number | string | boolean>>({});
const newParams = ref<Record<string, number | string | boolean>>({});
const isBacktesting = ref(false);
const backtestProgress = ref(0);

interface BacktestMetrics {
  totalReturn: number;
  annualizedReturn: number;
  sharpeRatio: number;
  maxDrawdown: number;
  winRate: number;
  profitFactor: number;
  tradesCount: number;
  calmarRatio?: number;
  sortinoRatio?: number;
  volatility?: number;
}

interface BacktestResultData {
  metrics: BacktestMetrics;
  equityCurve: Array<{ date: string; equity: number }>;
  benchmarkCurve?: Array<{ date: string; equity: number }>;
  trades?: Array<any>;
}

const originalResult = ref<BacktestResultData | null>(null);
const newResult = ref<BacktestResultData | null>(null);
const currentBacktestType = ref<'original' | 'new' | null>(null);
const originalAbortController = ref<AbortController | null>(null);
const newAbortController = ref<AbortController | null>(null);

// ==================== Common State ====================
const availableStrategies = ref<Array<{id: string, name: string}>>([]);
const isLoadingStrategies = ref(false);
const errorMessage = ref('');
const successMessage = ref('');
const confirmationDialog = ref({
  visible: false,
  title: '',
  message: '',
  confirmAction: () => {},
  cancelAction: () => {}
});

// ==================== Utility Functions ====================
const toNumeric = (value: any): number => {
  if (typeof value === 'number') return value;
  if (value === null || value === undefined || value === '') return NaN;
  const n = Number(value);
  return Number.isNaN(n) ? NaN : n;
};

const formatNumber = (value: any, digits = 2): string => {
  const n = toNumeric(value);
  if (Number.isNaN(n)) return '—';
  return n.toFixed(digits);
};

const formatPercent = (value: any, digits = 2): string => {
  const n = toNumeric(value);
  if (Number.isNaN(n)) return '—';
  return `${n.toFixed(digits)}%`;
};

const formatScore = (value: any, digits = 4): string => {
  const n = toNumeric(value);
  if (Number.isNaN(n)) return '—';
  if (n < -10000) return '无效';
  return n.toFixed(digits);
};

const isValidScore = (value: any): boolean => {
  const n = toNumeric(value);
  if (Number.isNaN(n)) return false;
  return n > -10000;
};

const showError = (msg: string) => {
  errorMessage.value = msg;
  setTimeout(() => errorMessage.value = '', 8000);
};

const showSuccess = (msg: string) => {
  successMessage.value = msg;
  setTimeout(() => successMessage.value = '', 5000);
};

// ==================== API Functions ====================
const fetchStrategies = async () => {
  isLoadingStrategies.value = true;
  try {
    const res = await request<any[]>('/strategies');
    if (res.ok && Array.isArray(res.data)) {
      availableStrategies.value = res.data.map((s: any) => ({
        id: String(s.id).trim(),
        name: String(s.name).trim(),
      }));
      if (availableStrategies.value.length > 0 && !selectedStrategy.value) {
        const first = availableStrategies.value[0];
        if (first && typeof first.id === 'string') {
          selectedStrategy.value = first.id;
        }
      }
    }
  } catch (e) {
    showError('获取策略列表失败');
  } finally {
    isLoadingStrategies.value = false;
  }
};

// ==================== Auto Mode Functions ====================
const syncCommonControls = (algo?: AlgorithmKey) => {
  const key = algo || selectedAlgorithm.value;
  const params = currentAlgoParams.value;
  if (key === 'ga') {
    populationSize.value = params.pop_size ?? 50;
    maxIterations.value = params.generations ?? 100;
  } else if (key === 'pso') {
    populationSize.value = params.particle_count ?? 40;
    maxIterations.value = params.iterations ?? 100;
  } else if (key === 'cmaes') {
    populationSize.value = params.population ?? 30;
    maxIterations.value = params.max_evaluations ?? 200;
  } else if (key === 'simulatedAnnealing') {
    populationSize.value = params.steps_per_temp ?? 10;
    maxIterations.value = params.steps_per_temp ?? 10;
  } else if (key === 'bayesian') {
    populationSize.value = params.random_init_points ?? 10;
    maxIterations.value = params.iterations ?? 50;
  } else if (key === 'grid') {
    populationSize.value = params.grid_density ?? 10;
    maxIterations.value = params.grid_density ?? 10;
  }
};

const isAllSelected = computed(() => selectedParamKeys.value.length === availableParams.value.length);
const MAX_OPTIMIZABLE_PARAMS = 4;

watch(selectedParamKeys, (newKeys) => {
  if (newKeys.length > MAX_OPTIMIZABLE_PARAMS) {
    const trimmed = newKeys.slice(0, MAX_OPTIMIZABLE_PARAMS);
    selectedParamKeys.value = trimmed;
    showConfirmationDialog(
      '参数数量过多',
      `一次优化太多参数极易过拟合，最多只能选择 ${MAX_OPTIMIZABLE_PARAMS} 个。`,
      () => {}
    );
  }
});

const algorithmSummary = computed(() => {
  const alg = selectedAlgorithm.value;
  const params = currentAlgoParams.value;
  if (alg === 'ga') {
    return `代数 ${params.generations ?? 100} × 种群 ${params.pop_size ?? 50}`;
  }
  if (alg === 'pso') {
    return `迭代 ${params.iterations ?? 100} × 粒子 ${params.particle_count ?? 40}`;
  }
  if (alg === 'cmaes') {
    return `评估 ${params.max_evaluations ?? 200} / 种群 ${params.population ?? 30}`;
  }
  if (alg === 'simulatedAnnealing') {
    return `初温 ${params.initial_temp ?? 100} · 每温迭代 ${params.steps_per_temp ?? 10}`;
  }
  if (alg === 'bayesian') {
    return `随机初始 ${params.random_init_points ?? 10} + 迭代 ${params.iterations ?? 50}`;
  }
  if (alg === 'grid') {
    return `网格密度 ${params.grid_density ?? 10}（在参数空间中设置步长）`;
  }
  return '';
});

const paramGroups = computed(() => {
  const groups = new Map<string, { name: string; displayName: string; params: any[] }>();
  const DEFAULT_GROUP_NAME = 'default';
  const DEFAULT_GROUP_DISPLAY_NAME = '基本参数';
  
  groups.set(DEFAULT_GROUP_NAME, {
    name: DEFAULT_GROUP_NAME,
    displayName: DEFAULT_GROUP_DISPLAY_NAME,
    params: []
  });
  
  availableParams.value.forEach(param => {
    const groupName = param.group || DEFAULT_GROUP_NAME;
    const displayName = param.group_label || 
                        (groupName === DEFAULT_GROUP_NAME ? DEFAULT_GROUP_DISPLAY_NAME : groupName);
    
    if (!groups.has(groupName)) {
      groups.set(groupName, {
        name: groupName,
        displayName: displayName,
        params: []
      });
    }
    
    groups.get(groupName)!.params.push(param);
  });
  
  const result = Array.from(groups.values());
  return result.sort((a, b) => {
    if (a.name === DEFAULT_GROUP_NAME) return -1;
    if (b.name === DEFAULT_GROUP_NAME) return 1;
    return a.displayName.localeCompare(b.displayName);
  });
});

const isGroupSelected = (groupName: string) => {
  const group = paramGroups.value.find(g => g.name === groupName);
  if (!group) return false;
  return group.params.every(param => selectedParamKeys.value.includes(param.key));
};

const getSelectedCountInGroup = (groupName: string) => {
  const group = paramGroups.value.find(g => g.name === groupName);
  if (!group) return 0;
  return group.params.filter(param => selectedParamKeys.value.includes(param.key)).length;
};

const toggleGroupParams = (groupName: string) => {
  const group = paramGroups.value.find(g => g.name === groupName);
  if (!group) return;
  
  const isCurrentlySelected = isGroupSelected(groupName);
  
  if (isCurrentlySelected) {
    group.params.forEach(param => {
      const index = selectedParamKeys.value.indexOf(param.key);
      if (index > -1) {
        selectedParamKeys.value.splice(index, 1);
      }
    });
  } else {
    group.params.forEach(param => {
      if (!selectedParamKeys.value.includes(param.key)) {
        selectedParamKeys.value.push(param.key);
      }
    });
  }
};

const isDateRangeValid = computed(() => {
  if (!trainStartDate.value && !valStartDate.value && !testStartDate.value) {
    return true;
  }
  const toNum = (d: string) => (d ? Number(d.replace(/-/g, '')) : NaN);
  const ts = toNum(trainStartDate.value || startDate.value);
  const te = toNum(trainEndDate.value || endDate.value);
  const vs = valStartDate.value ? toNum(valStartDate.value) : NaN;
  const ve = valEndDate.value ? toNum(valEndDate.value) : NaN;
  const ss = testStartDate.value ? toNum(testStartDate.value) : NaN;
  const se = testEndDate.value ? toNum(testEndDate.value) : NaN;

  if (!(ts <= te)) return false;
  if (!Number.isNaN(vs) && !Number.isNaN(ve)) {
    if (!(te <= vs && vs <= ve)) return false;
  }
  if (!Number.isNaN(ss) && !Number.isNaN(se)) {
    const left = !Number.isNaN(ve) ? ve : te;
    if (!(left <= ss && ss <= se)) return false;
  }
  return true;
});

const totalDateRangeDays = computed(() => {
  if (!startDate.value || !endDate.value) return 0;
  const start = new Date(startDate.value);
  const end = new Date(endDate.value);
  const diffTime = end.getTime() - start.getTime();
  const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
  return diffDays;
});

const isDateRangeSufficient = computed(() => {
  return totalDateRangeDays.value >= 90;
});

const dateRangeWarning = computed(() => {
  if (totalDateRangeDays.value === 0) return '';
  if (totalDateRangeDays.value < 90) {
    return '优化需要至少3个月（90天）的数据才能产生有效结果。';
  }
  return '';
});

const canStartOptimization = computed(() => {
  if (!isDateRangeValid.value) return false;
  if (!isDateRangeSufficient.value) return false;
  return (
    selectedParamKeys.value.length > 0 &&
    !isOptimizing.value &&
    selectedStrategy.value &&
    startDate.value &&
    endDate.value
  );
});

const showConfirmationDialog = (
  title: string,
  message: string,
  confirmAction: () => void,
  cancelAction: () => void = () => {},
) => {
  confirmationDialog.value = {
    visible: true,
    title,
    message,
    confirmAction,
    cancelAction,
  };
};

const confirmDialog = () => {
  confirmationDialog.value.confirmAction();
  confirmationDialog.value.visible = false;
};

const cancelDialog = () => {
  confirmationDialog.value.cancelAction();
  confirmationDialog.value.visible = false;
};

const toggleAllParams = () => {
  if (isAllSelected.value) {
    selectedParamKeys.value = [];
  } else {
    selectedParamKeys.value = availableParams.value.map((p) => p.key);
  }
};

const startOptimization = () => {
  if (!canStartOptimization.value) {
    if (!isDateRangeValid.value) {
      showError('三段区间顺序或重叠不合法，请检查训练/验证/测试日期设置。');
    }
    return;
  }

  showConfirmationDialog(
    '确认开始优化',
    `将对 ${selectedParamKeys.value.length} 个参数进行优化（模式：${optimizationMode.value}）。`,
    () => {
      const method = currentAlgoConfig.value?.method || 'ga';
      const algoParamPayload = algorithmParams.value[selectedAlgorithm.value] || {};

      const searchSpace = availableParams.value
        .filter((p: any) => selectedParamKeys.value.includes(p.key))
        .map((p: any) => {
          const range = paramRangeValues.value[p.key] || { min: p.min, max: p.max };
          return {
            name: p.key,
            type: p.type || 'float',
            min: range.min,
            max: range.max,
          };
        });

      const metricMap: Record<string, string> = {
        sharpeRatio: 'sharpe',
        totalReturn: 'returns',
        maxDrawdown: 'max_drawdown',
        calmarRatio: 'calmar',
      };

      const optimizationPayload = {
        task_id: `opt_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
        strategy_name: selectedStrategy.value,
        optimization_engine: {
          method,
          params: algoParamPayload,
        },
        search_space: searchSpace,
        objective: {
          target: optimizationObjective.value,
          metric: metricMap[selectedMetric.value] || selectedMetric.value,
          constraints: {},
        },
        backtest: {
          start_date: startDate.value,
          end_date: endDate.value,
          train_start_date: trainStartDate.value || startDate.value,
          train_end_date: trainEndDate.value || endDate.value,
          val_start_date: valStartDate.value || '',
          val_end_date: valEndDate.value || '',
          test_start_date: testStartDate.value || '',
          test_end_date: testEndDate.value || '',
        },
        validation: {
          enabled: true,
          mode: optimizationMode.value,
        },
        mode: optimizationMode.value,
        enabled_params: [...selectedParamKeys.value],
      };

      startOpt(optimizationPayload);
      showSuccess('优化已开始');
    },
  );
};

const stopOptimization = () => {
  if (!isOptimizing.value) return;
  showConfirmationDialog(
    '确认停止优化',
    '正在进行的优化将会被中止，是否继续？',
    () => {
      stopOpt();
    },
  );
};

const updateAlgoField = (fieldKey: string, rawValue: any) => {
  const algoKey = selectedAlgorithm.value;
  const params = { ...(algorithmParams.value[algoKey] || {}) };
  const field = currentAlgoConfig.value?.fields.find(f => f.key === fieldKey);

  if (field?.type === 'select') {
    params[fieldKey] = rawValue;
  } else {
    const numeric = typeof rawValue === 'string' ? Number(rawValue) : rawValue;
    params[fieldKey] = Number.isNaN(numeric) ? rawValue : numeric;
  }

  algorithmParams.value[algoKey] = params;
};

const robustnessLevel = (c: any): 'good' | 'medium' | 'bad' => {
  if (!c) return 'medium';
  const t = Number(c.score_train ?? 0);
  const v = Number(c.score_val ?? t);
  const gap = Math.abs(v - t);
  const scale = Math.max(Math.abs(t), 1);
  const r = scale === 0 ? 0 : gap / scale;
  if (r <= 0.1) return 'good';
  if (r <= 0.3) return 'medium';
  return 'bad';
};

const robustnessClass = (c: any): string => {
  const level = robustnessLevel(c);
  if (level === 'good') return 'text-emerald-400';
  if (level === 'medium') return 'text-amber-300';
  return 'text-rose-400';
};

const robustnessIcon = (c: any): string => {
  const level = robustnessLevel(c);
  if (level === 'good') return '🟢';
  if (level === 'medium') return '🟡';
  return '🔴';
};

const robustnessText = (c: any): string => {
  const level = robustnessLevel(c);
  if (level === 'good') return '稳健';
  if (level === 'medium') return '中等';
  return '疑似过拟合';
};

const buildSensitivityPoints = () => {
  const source = finalSelected.value || (candidates.value.length ? candidates.value[0] : null);
  if (!source) {
    sensitivityPoints.value = [];
    updateSensitivityChart();
    return;
  }
  const base = Number(source.score_train ?? 0);
  const deltas = [-0.2, -0.1, 0, 0.1, 0.2];
  sensitivityPoints.value = deltas.map((d) => ({
    value: d,
    score: base * (1 - 0.2 * Math.abs(d)),
  }));
  updateSensitivityChart();
};

function updateSensitivityChart() {
  if (!sensitivityChartContainer.value || sensitivityPoints.value.length === 0) {
    if (sensitivityChartInstance) {
      sensitivityChartInstance.dispose();
      sensitivityChartInstance = null;
    }
    return;
  }
  
  if (!sensitivityChartInstance) {
    sensitivityChartInstance = echarts.init(sensitivityChartContainer.value);
  }
  
  const xData = sensitivityPoints.value.map(p => `Δ${(p.value * 100).toFixed(0)}%`);
  const yData = sensitivityPoints.value.map(p => p.score);
  
  const option: EChartsOption = {
    backgroundColor: 'transparent',
    grid: {
      top: '10%',
      right: '5%',
      bottom: '15%',
      left: '10%',
      containLabel: true
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: '#334155',
      textStyle: {
        color: '#f1f5f9',
        fontSize: 12
      },
      formatter: (params: any) => {
        const p = params[0];
        if (!p) return '';
        const index = p.dataIndex;
        const point = sensitivityPoints.value[index];
        if (!point) return '';
        return `
          <div style="padding: 8px;">
            <div style="font-weight: bold; margin-bottom: 4px;">${p.name}</div>
            <div>参数变化: ${(point.value * 100).toFixed(0)}%</div>
            <div>目标得分: <span style="color: #10b981; font-weight: bold;">${point.score.toFixed(4)}</span></div>
          </div>
        `;
      }
    },
    xAxis: {
      type: 'category',
      data: xData,
      axisLine: {
        lineStyle: {
          color: '#334155'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        fontSize: 11
      }
    },
    yAxis: {
      type: 'value',
      name: '目标得分',
      axisLine: {
        lineStyle: {
          color: '#334155'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        fontSize: 11
      },
      nameTextStyle: {
        color: '#94a3b8',
        fontSize: 11
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: 'rgba(148, 163, 184, 0.1)',
          type: 'dashed'
        }
      }
    },
    series: [{
      name: '目标得分',
      type: 'line',
      data: yData,
      smooth: true,
      lineStyle: {
        color: '#6366f1',
        width: 2
      },
      itemStyle: {
        color: '#6366f1',
        borderColor: '#fff',
        borderWidth: 2
      },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(99, 102, 241, 0.3)' },
          { offset: 1, color: 'rgba(99, 102, 241, 0)' }
        ])
      },
      symbol: 'circle',
      symbolSize: 8,
      emphasis: {
        itemStyle: {
          color: '#8b5cf6',
          borderColor: '#fff',
          borderWidth: 3,
          shadowBlur: 10,
          shadowColor: '#6366f1'
        }
      }
    }],
    animationDuration: 500,
    animationEasing: 'cubicOut'
  };
  
  sensitivityChartInstance.setOption(option, { notMerge: true, lazyUpdate: false });
}

const openCandidateDetail = (c: any) => {
  candidateDetail.value = c;
};

const closeCandidateDetail = () => {
  candidateDetail.value = null;
};

// ==================== Manual Mode Functions ====================
const hasParamChanges = computed(() => {
  return availableParams.value.some(p => originalParams.value[p.key] !== newParams.value[p.key]);
});

const changedParams = computed(() => {
  return availableParams.value.filter(p => originalParams.value[p.key] !== newParams.value[p.key]);
});

const resetNewParams = () => {
  newParams.value = { ...originalParams.value };
};

const runBacktest = async (type: 'original' | 'new') => {
  if (!selectedStrategy.value || !startDate.value || !endDate.value) {
    showError('请选择策略和日期范围');
    return;
  }

  currentBacktestType.value = type;
  backtestProgress.value = type === 'original' ? 20 : 60;
  const params = type === 'original' ? originalParams.value : newParams.value;

  const controller = new AbortController();
  const signal = controller.signal;
  if (type === 'original') {
    originalAbortController.value = controller;
  } else {
    newAbortController.value = controller;
  }

  try {
    const res = await request<any>('/run_backtest', {
      method: 'POST',
      signal,
      body: JSON.stringify({
        strategy_name: selectedStrategy.value,
        start_date: startDate.value,
        end_date: endDate.value,
        params
      }),
      timeoutMs: 60000,
    });
    if (!res.ok) {
      throw new Error(res.message || '回测失败');
    }

    const data = res.data || {};
    const m = data.metrics || {};
    let equityCurvePoints: Array<{ date: string; equity: number }> = [];
    let benchmarkCurvePoints: Array<{ date: string; equity: number }> = [];

    if (Array.isArray(data.equityCurve) && data.equityCurve.length > 0) {
      equityCurvePoints = data.equityCurve
        .map((p: { date: string; equity: number }) => ({
          date: p.date,
          equity: typeof p.equity === 'number' ? p.equity : Number(p.equity ?? 0),
        }))
        .filter((p: { date: string; equity: number }) => !!p.date);
    } else if (
      data.equityCurveData &&
      Array.isArray(data.equityCurveData.dates) &&
      Array.isArray(data.equityCurveData.strategyReturns)
    ) {
      equityCurvePoints = data.equityCurveData.dates.map((date: string, idx: number) => ({
        date,
        equity: Number(data.equityCurveData.strategyReturns[idx] ?? 0),
      }));

      if (Array.isArray(data.equityCurveData.benchmarkReturns)) {
        benchmarkCurvePoints = data.equityCurveData.dates.map((date: string, idx: number) => ({
          date,
          equity: Number(data.equityCurveData.benchmarkReturns[idx] ?? 0),
        }));
      }
    }

    const actualStart: string | undefined = data.actualStartDate;
    const actualEnd: string | undefined = data.actualEndDate;
    if (actualStart && actualEnd && (actualStart !== startDate.value || actualEnd !== endDate.value)) {
      startDate.value = actualStart;
      endDate.value = actualEnd;
      showSuccess(`回测日期已自动调整为 ${actualStart} ~ ${actualEnd}（数据库可用范围）`);
    }

    const normalizedResult: BacktestResultData = {
      metrics: {
        totalReturn: m.totalReturn ?? m.total_return ?? 0,
        annualizedReturn: m.annualizedReturn ?? m.annualized_return ?? 0,
        sharpeRatio: m.sharpeRatio ?? m.sharpe_ratio ?? 0,
        maxDrawdown: m.maxDrawdown ?? m.max_drawdown ?? 0,
        winRate: m.winRate ?? m.win_rate ?? 0,
        profitFactor: m.profitFactor ?? m.profit_factor ?? 0,
        tradesCount: m.tradesCount ?? m.trades_count ?? 0,
        calmarRatio: m.calmarRatio ?? m.calmar_ratio,
        sortinoRatio: m.sortinoRatio ?? m.sortino_ratio,
        volatility: m.volatility,
      },
      equityCurve: equityCurvePoints,
      benchmarkCurve: benchmarkCurvePoints.length ? benchmarkCurvePoints : undefined,
      trades: data.trades ?? []
    };

    if (type === 'original') {
      originalResult.value = normalizedResult;
      backtestProgress.value = 50;
      showSuccess('原参数回测完成');
    } else {
      newResult.value = normalizedResult;
      backtestProgress.value = 100;
      showSuccess('新参数回测完成');
    }
  } catch (e: any) {
    if (e?.name === 'AbortError') {
      return;
    }
    showError(e?.message || '回测失败');
    throw e;
  }
};

const runComparison = async () => {
  if (!selectedStrategy.value || !startDate.value || !endDate.value) {
    showError('请选择策略和日期范围');
    return;
  }

  originalResult.value = null;
  newResult.value = null;
  isBacktesting.value = true;
  backtestProgress.value = 0;

  if (originalAbortController.value) {
    originalAbortController.value.abort();
    originalAbortController.value = null;
  }
  if (newAbortController.value) {
    newAbortController.value.abort();
    newAbortController.value = null;
  }

  try {
    const tasks: Promise<void>[] = [];
    tasks.push(runBacktest('original'));
    if (hasParamChanges.value) {
      tasks.push(runBacktest('new'));
    }

    await Promise.all(tasks);
    backtestProgress.value = 100;
  } catch {
    // 错误已经在 runBacktest 中提示
  } finally {
    isBacktesting.value = false;
    currentBacktestType.value = null;
  }
};

const stopComparison = () => {
  if (!isBacktesting.value) return;

  if (originalAbortController.value) {
    originalAbortController.value.abort();
    originalAbortController.value = null;
  }
  if (newAbortController.value) {
    newAbortController.value.abort();
    newAbortController.value = null;
  }

  isBacktesting.value = false;
  currentBacktestType.value = null;
  backtestProgress.value = 0;
  showSuccess('已停止当前对比回测');
};

const getMetricDiff = (key: keyof BacktestMetrics) => {
  if (!originalResult.value?.metrics || !newResult.value?.metrics) return null;
  const orig = originalResult.value.metrics[key] as number;
  const newVal = newResult.value.metrics[key] as number;
  if (orig === undefined || newVal === undefined) return null;
  return newVal - orig;
};

const isImproved = (key: keyof BacktestMetrics) => {
  const diff = getMetricDiff(key);
  if (diff === null) return null;
  if (key === 'maxDrawdown') return diff < 0;
  return diff > 0;
};

const metricsConfig = [
  { key: 'totalReturn', label: '总收益率', format: 'percent' },
  { key: 'annualizedReturn', label: '年化收益', format: 'percent' },
  { key: 'sharpeRatio', label: '夏普比率', format: 'number' },
  { key: 'maxDrawdown', label: '最大回撤', format: 'percent', inverse: true },
  { key: 'winRate', label: '胜率', format: 'percent' },
  { key: 'profitFactor', label: '盈亏比', format: 'number' },
  { key: 'tradesCount', label: '交易次数', format: 'integer' },
  { key: 'calmarRatio', label: '卡尔玛比率', format: 'number' },
  { key: 'sortinoRatio', label: '索提诺比率', format: 'number' },
];

const equityVersions = computed(() => {
  const versions: Array<{ versionId: string; versionName: string; data: { date: string; equity: number }[] }> = [];

  if (originalResult.value?.equityCurve?.length) {
    versions.push({
      versionId: 'original',
      versionName: '原参数',
      data: originalResult.value.equityCurve,
    });
  }

  if (newResult.value?.equityCurve?.length) {
    versions.push({
      versionId: 'new',
      versionName: '新参数',
      data: newResult.value.equityCurve,
    });
  }

  return versions;
});

const benchmarkCurve = computed(() => {
  const source = newResult.value?.benchmarkCurve?.length
    ? newResult.value
    : originalResult.value?.benchmarkCurve?.length
      ? originalResult.value
      : null;

  if (!source || !source.benchmarkCurve || !source.benchmarkCurve.length) {
    return [] as Array<{ date: string; equity: number }>;
  }

  return source.benchmarkCurve;
});

// ==================== Watchers ====================
watch(selectedAlgorithm, (newAlgorithm) => {
  const key = newAlgorithm;
  if (!algorithmParams.value[key]) {
    const cfg = ALGO_CONFIGS[key];
    if (cfg) {
      const defaults: Record<string, any> = {};
      cfg.fields.forEach((field) => {
        defaults[field.key] = field.default;
      });
      if (key === 'grid') {
        defaults.grid_density = 10;
      }
      algorithmParams.value[key] = defaults;
    }
  }
  syncCommonControls(key);
});

watch(populationSize, (newValue) => {
  const key = selectedAlgorithm.value;
  const params = { ...(algorithmParams.value[key] || {}) };
  if (key === 'ga') params.pop_size = newValue;
  else if (key === 'pso') params.particle_count = newValue;
  else if (key === 'cmaes') params.population = newValue;
  else if (key === 'bayesian') params.random_init_points = newValue;
  else if (key === 'grid') params.grid_density = newValue;
  algorithmParams.value[key] = params;
});

watch(maxIterations, (newValue) => {
  const key = selectedAlgorithm.value;
  const params = { ...(algorithmParams.value[key] || {}) };
  if (key === 'ga') params.generations = newValue;
  else if (key === 'pso') params.iterations = newValue;
  else if (key === 'cmaes') params.max_evaluations = newValue;
  else if (key === 'simulatedAnnealing') params.steps_per_temp = newValue;
  else if (key === 'bayesian') params.iterations = newValue;
  else if (key === 'grid') params.grid_density = newValue;
  algorithmParams.value[key] = params;
});

watch(selectedStrategy, () => {
  fetchStrategyParams();
  // Reset manual mode params
  originalParams.value = {};
  newParams.value = {};
  availableParams.value.forEach((p: any) => {
    const defaultVal = p.default ?? p.min ?? 0;
    originalParams.value[p.key] = defaultVal;
    newParams.value[p.key] = defaultVal;
  });
  // Clear results
  originalResult.value = null;
  newResult.value = null;
});

watch(() => sensitivityPoints.value, () => {
  updateSensitivityChart();
}, { deep: true });

watch(() => showSensitivity.value, (newVal) => {
  if (newVal) {
    nextTick(() => {
      updateSensitivityChart();
    });
  }
});

// ==================== Lifecycle ====================
onMounted(() => {
  applyInitialDates();
  fetchStrategies();
  loadAlgorithmParams();
  syncCommonControls();

  setupSocketListeners({
    onComplete: () => {
      buildSensitivityPoints();
    },
    onError: (data: any) => {
      showError(data?.message || '优化过程中出现错误');
    },
  });
});

onUnmounted(() => {
  if (sensitivityChartInstance) {
    sensitivityChartInstance.dispose();
    sensitivityChartInstance = null;
  }
  cleanupOptimization();
});
</script>

<template>
  <div class="h-screen flex flex-col bg-[#0A0A0A] text-[#d1d4dc] overflow-hidden">
    <!-- 顶部工具栏 -->
    <div class="h-10 flex items-center bg-[#0A0A0A] border-b border-[#2a2e39] px-4 space-x-6 overflow-hidden select-none flex-shrink-0">
      <!-- 左侧：标题 -->
      <div class="flex items-center space-x-2 border-r border-[#2a2e39] pr-6 h-full">
        <i class="fas fa-sliders-h text-[#2962ff] text-sm"></i>
        <span class="text-[10px] font-bold text-[#d1d4dc] uppercase tracking-wider">参数优化</span>
      </div>
      
      <!-- Tab 切换 -->
      <div class="flex items-center space-x-1 bg-[#1e222d] rounded p-0.5">
        <button
          @click="activeTab = 'auto'"
          class="px-3 py-1 text-[10px] font-medium rounded transition-colors"
          :class="activeTab === 'auto' ? 'bg-[#2962ff] text-white' : 'text-[#787b86] hover:text-[#d1d4dc]'"
        >
          <i class="fas fa-robot mr-1"></i>自动优化
        </button>
        <button
          @click="activeTab = 'manual'"
          class="px-3 py-1 text-[10px] font-medium rounded transition-colors"
          :class="activeTab === 'manual' ? 'bg-[#2962ff] text-white' : 'text-[#787b86] hover:text-[#d1d4dc]'"
        >
          <i class="fas fa-hand-pointer mr-1"></i>手动调参
        </button>
      </div>
      
      <!-- 中间：描述 -->
      <div class="flex-1 overflow-hidden">
        <span class="text-[9px] text-[#787b86]">
          {{ activeTab === 'auto' ? '通过智能算法自动搜索最优参数组合' : '手动修改参数后与原参数进行对比分析' }}
        </span>
      </div>
      
      <!-- 右侧：操作按钮 -->
      <div class="flex items-center space-x-3 pl-6 border-l border-[#2a2e39] h-full">
        <template v-if="activeTab === 'auto'">
          <button
            class="px-3 py-1 text-[10px] font-medium text-[#787b86] bg-[#2a2e39] border border-[#363a45] rounded hover:text-[#d1d4dc] hover:bg-[#363a45] transition-colors disabled:opacity-50"
            @click="stopOptimization"
            :disabled="!isOptimizing"
          >
            停止优化
          </button>
          <button
            class="px-3 py-1 text-[10px] font-medium text-white bg-[#2962ff] rounded hover:bg-[#1e53e5] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            @click="startOptimization"
            :disabled="!canStartOptimization"
          >
            {{ isOptimizing ? '优化中...' : '开始优化' }}
          </button>
        </template>
        <template v-else>
          <button
            v-if="isBacktesting"
            @click="stopComparison"
            class="px-3 py-1 text-[10px] font-medium text-[#787b86] bg-[#2a2e39] border border-[#363a45] rounded hover:text-[#d1d4dc] hover:bg-[#363a45] transition-colors"
          >
            <i class="fas fa-stop mr-1"></i>停止
          </button>
          <button
            @click="runComparison"
            :disabled="isBacktesting || !selectedStrategy || !startDate || !endDate"
            class="px-3 py-1 text-[10px] font-medium text-white bg-[#2962ff] rounded hover:bg-[#1e53e5] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <i v-if="isBacktesting" class="fas fa-spinner fa-spin mr-1"></i>
            <i v-else class="fas fa-play mr-1"></i>
            {{ isBacktesting ? '回测中...' : '开始对比' }}
          </button>
        </template>
      </div>
    </div>
    
    <!-- 消息提示 -->
    <div v-if="errorMessage" class="px-4 py-1 bg-[#f23645]/10 border-b border-[#f23645]/30 text-[10px] text-[#f23645]">
      <i class="fas fa-exclamation-circle mr-1"></i>{{ errorMessage }}
    </div>
    <div v-if="successMessage" class="px-4 py-1 bg-[#089981]/10 border-b border-[#089981]/30 text-[10px] text-[#089981]">
      <i class="fas fa-check-circle mr-1"></i>{{ successMessage }}
    </div>
    <div v-if="dateRangeWarning && activeTab === 'auto'" class="px-4 py-1 bg-[#f23645]/10 border-b border-[#f23645]/30 text-[10px] text-[#f23645]">
      <i class="fas fa-exclamation-triangle mr-1"></i>{{ dateRangeWarning }}
    </div>

    <!-- 主内容区 -->
    <div class="flex-1 flex overflow-hidden">
      <!-- ==================== 自动优化模式 ==================== -->
      <template v-if="activeTab === 'auto'">
        <div class="flex-1 flex flex-col overflow-y-auto p-4 space-y-4">
          <!-- 基础设置面板 -->
          <div class="bg-[#1e222d] border border-[#2a2e39] rounded">
            <div class="flex items-center justify-between px-3 py-2 border-b border-[#2a2e39] bg-[#1e222d]">
              <div class="flex items-center space-x-2">
                <i class="fas fa-cog text-[#2962ff] text-[10px]"></i>
                <span class="text-[10px] font-bold text-[#d1d4dc] uppercase tracking-wider">基础设置</span>
              </div>
              <span class="text-[9px] text-[#787b86]">选择策略、时间区间与算法，定义优化目标与模式</span>
            </div>

            <div class="p-3 grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
              <label class="space-y-1 text-[10px]">
                <span class="text-[#787b86]">选择策略</span>
                <select
                  v-model="selectedStrategy"
                  class="w-full h-7 px-2 text-[10px] bg-[#0A0A0A] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                >
                  <option value="" disabled>请选择策略</option>
                  <option v-for="s in availableStrategies" :key="s.id" :value="s.id">
                    {{ s.name }}
                  </option>
                </select>
              </label>

              <label class="space-y-1 text-[10px]">
                <span class="text-[#787b86]">主回测开始日期</span>
                <input
                  type="date"
                  v-model="startDate"
                  class="w-full h-7 px-2 text-[10px] bg-[#131722] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                />
              </label>

              <label class="space-y-1 text-[10px]">
                <span class="text-[#787b86]">主回测结束日期</span>
                <input
                  type="date"
                  v-model="endDate"
                  class="w-full h-7 px-2 text-[10px] bg-[#131722] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                />
              </label>

              <label class="space-y-1 text-[10px]">
                <span class="text-[#787b86]">优化算法</span>
                <select
                  v-model="selectedAlgorithm"
                  class="w-full h-7 px-2 text-[10px] bg-[#131722] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                >
                  <option value="ga">遗传算法</option>
                  <option value="pso">粒子群优化</option>
                  <option value="cmaes">CMA-ES</option>
                  <option value="simulatedAnnealing">模拟退火</option>
                  <option value="grid">网格搜索</option>
                  <option value="bayesian">贝叶斯优化</option>
                </select>
              </label>

              <label class="space-y-1 text-[10px]">
                <span class="text-[#787b86]">回测指标</span>
                <select
                  v-model="selectedMetric"
                  class="w-full h-7 px-2 text-[10px] bg-[#131722] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                >
                  <option value="sharpeRatio">夏普比率</option>
                  <option value="totalReturn">总收益率</option>
                  <option value="maxDrawdown">最大回撤</option>
                  <option value="calmarRatio">Calmar 比率</option>
                </select>
              </label>

              <label class="space-y-1 text-[10px]">
                <span class="text-[#787b86]">优化模式</span>
                <select
                  v-model="optimizationMode"
                  class="w-full h-7 px-2 text-[10px] bg-[#131722] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                >
                  <option value="quick_explore">快速探索模式（仅训练区）</option>
                  <option value="robust">稳健优化模式（训练+验证）【推荐】</option>
                  <option value="aggressive">高风险最大化（仅训练区）⚠</option>
                </select>
              </label>

              <label class="space-y-1 text-[10px]">
                <span class="text-[#787b86]">优化目标</span>
                <select
                  v-model="optimizationObjective"
                  class="w-full h-7 px-2 text-[10px] bg-[#131722] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                >
                  <option value="robust_trend_score">稳健收益比（趋势策略默认）</option>
                  <option value="rr_risk_score">风险调整盈亏比（低胜率策略专用）</option>
                  <option value="multi_period_robust">多周期稳健性</option>
                  <option value="calmar">Calmar 比率</option>
                  <option value="sharpe">夏普比率（传统，可能不适合主升浪）</option>
                </select>
              </label>
            </div>

            <!-- 优化区间设置 -->
            <div class="px-3 pb-3 border-t border-[#2a2e39]">
              <div class="flex items-center justify-between py-2">
                <div class="flex items-center space-x-2">
                  <i class="fas fa-calendar-alt text-[#787b86] text-[10px]"></i>
                  <span class="text-[10px] text-[#787b86]">优化区间设置（防过拟合）</span>
                </div>
                <span
                  class="text-[9px] px-2 py-0.5 rounded border"
                  :class="isDateRangeValid ? 'border-[#089981] text-[#089981]' : 'border-[#f23645] text-[#f23645]'"
                >
                  {{ isDateRangeValid ? '区间合法' : '区间有冲突' }}
                </span>
              </div>

              <ThreeSegmentTimeSlider
                :start-date="startDate"
                :end-date="endDate"
                :train-start-date="trainStartDate"
                :train-end-date="trainEndDate"
                :val-start-date="valStartDate"
                :val-end-date="valEndDate"
                :test-start-date="testStartDate"
                :test-end-date="testEndDate"
                @update:train-start-date="trainStartDate = $event"
                @update:train-end-date="trainEndDate = $event"
                @update:val-start-date="valStartDate = $event"
                @update:val-end-date="valEndDate = $event"
                @update:test-start-date="testStartDate = $event"
                @update:test-end-date="testEndDate = $event"
              />
            </div>
          </div>

          <!-- 算法引擎配置面板 -->
          <div class="bg-[#1e222d] border border-[#2a2e39] rounded">
            <div class="flex items-center justify-between px-3 py-2 border-b border-[#2a2e39]">
              <div class="flex items-center space-x-2">
                <i class="fas fa-microchip text-[#2962ff] text-[10px]"></i>
                <span class="text-[10px] font-bold text-[#d1d4dc] uppercase tracking-wider">算法配置</span>
              </div>
              <span class="text-[9px] text-[#787b86]">{{ algorithmSummary }}</span>
            </div>
            <div class="p-3">
              <div v-if="selectedAlgorithm === 'grid'" class="text-[10px] text-[#787b86] py-2">
                网格搜索不需要单独配置引擎参数，请在下方"可优化参数"区域设置每个参数的搜索范围。
              </div>
              <div v-else-if="currentAlgoConfig && currentAlgoConfig.fields.length" class="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
                <div v-for="field in currentAlgoConfig.fields" :key="field.key" class="space-y-1 text-[10px]">
                  <label class="text-[#787b86]">{{ field.label }}</label>
                  <input
                    v-if="field.type === 'int' || field.type === 'float'"
                    type="number"
                    :step="field.step ?? (field.type === 'float' ? 0.01 : 1)"
                    :min="field.min"
                    :max="field.max"
                    :value="currentAlgoParams[field.key] ?? field.default"
                    @input="updateAlgoField(field.key, ($event.target as HTMLInputElement).value)"
                    class="w-full h-7 px-2 text-[10px] bg-[#131722] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                  />
                  <select
                    v-else-if="field.type === 'select' && field.options"
                    :value="currentAlgoParams[field.key] ?? field.default"
                    @change="updateAlgoField(field.key, ($event.target as HTMLSelectElement).value)"
                    class="w-full h-7 px-2 text-[10px] bg-[#131722] border border-[#2a2e39] rounded text-[#d1d4dc] focus:border-[#2962ff] focus:outline-none"
                  >
                    <option v-for="option in field.options" :key="option.value" :value="option.value">
                      {{ option.label }}
                    </option>
                  </select>
                </div>
              </div>
              <div v-else class="py-4 text-center text-[10px] text-[#787b86]">
                请选择优化算法以显示配置选项。
              </div>
            </div>
          </div>

          <!-- 参数选择面板 -->
          <div class="bg-[#1e222d] border border-[#2a2e39] rounded">
            <div class="flex items-center justify-between px-3 py-2 border-b border-[#2a2e39]">
              <div class="flex items-center space-x-2">
                <i class="fas fa-check-square text-[#2962ff] text-[10px]"></i>
                <span class="text-[10px] font-bold text-[#d1d4dc] uppercase tracking-wider">选择优化参数</span>
              </div>
              <div class="flex items-center space-x-3">
                <span class="text-[9px] text-[#787b86]">
                  已选 {{ selectedParamKeys.length }} / {{ availableParams.length }} 个参数
                </span>
                <button
                  @click="toggleAllParams"
                  class="text-[9px] px-2 py-0.5 bg-[#2a2e39] hover:bg-[#363a45] rounded transition-colors"
                >
                  {{ isAllSelected ? '取消全选' : '全选' }}
                </button>
              </div>
            </div>

            <div class="p-3 space-y-3">
              <div
                v-for="group in paramGroups"
                :key="group.name"
                class="border border-[#2a2e39] rounded overflow-hidden"
              >
                <div
                  class="flex items-center justify-between px-3 py-2 bg-[#131722] cursor-pointer hover:bg-[#1e222d] transition-colors"
                  @click="toggleGroupParams(group.name)"
                >
                  <div class="flex items-center space-x-2">
                    <i
                      class="fas text-[10px] transition-transform"
                      :class="isGroupSelected(group.name) ? 'fa-check-square text-[#2962ff]' : 'fa-square text-[#787b86]'"></i>
                    <span class="text-[10px] font-medium text-[#d1d4dc]">{{ group.displayName }}</span>
                  </div>
                  <span class="text-[9px] text-[#787b86]">
                    {{ getSelectedCountInGroup(group.name) }} / {{ group.params.length }}
                  </span>
                </div>
                <div class="p-2 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2">
                  <label
                    v-for="param in group.params"
                    :key="param.key"
                    class="flex items-center space-x-2 p-2 rounded hover:bg-[#131722] cursor-pointer transition-colors"
                    :class="{ 'opacity-50': paramLocked[param.key] }"
                  >
                    <input
                      type="checkbox"
                      v-model="selectedParamKeys"
                      :value="param.key"
                      :disabled="paramLocked[param.key]"
                      class="w-3 h-3 rounded border-[#2a2e39] text-[#2962ff] focus:ring-[#2962ff] bg-[#0A0A0A]"
                    />
                    <div class="flex-1 min-w-0">
                      <div class="text-[10px] text-[#d1d4dc] truncate">{{ param.label }}</div>
                      <div class="text-[9px] text-[#787b86]">
                        {{ param.type === 'int' ? '整数' : '浮点' }} | 
                        {{ param.min }} ~ {{ param.max }}
                      </div>
                    </div>
                  </label>
                </div>
              </div>
            </div>
          </div>

          <!-- 优化结果可视化 -->
          <OptimizationVisualizer
            :optimizer="selectedAlgorithm"
            :progress="progress"
            :iteration="evaluatedCount"
            :total-iterations="totalIterations"
            :best-score="bestMetric"
            :history="optimizationHistory"
            metric-label="得分"
          />
        </div>
      </template>

      <!-- ==================== 手动调参模式 ==================== -->
      <template v-else>
        <div class="flex-1 flex flex-col overflow-y-auto p-4 space-y-4">
          <!-- 配置区域 -->
          <div class="bg-[#151925] rounded-xl border border-slate-800 p-5">
            <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
              <!-- 策略选择 -->
              <div>
                <label class="block text-sm font-medium text-slate-300 mb-2">选择策略</label>
                <select
                  v-model="selectedStrategy"
                  class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2.5 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                  :disabled="isLoadingStrategies || isBacktesting"
                >
                  <option value="" disabled>请选择策略</option>
                  <option v-for="s in availableStrategies" :key="s.id" :value="s.id">
                    {{ s.name }}
                  </option>
                </select>
              </div>
              
              <!-- 开始日期 -->
              <div>
                <label class="block text-sm font-medium text-slate-300 mb-2">开始日期</label>
                <input
                  v-model="startDate"
                  type="date"
                  class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2.5 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                  :disabled="isBacktesting"
                />
              </div>
              
              <!-- 结束日期 -->
              <div>
                <label class="block text-sm font-medium text-slate-300 mb-2">结束日期</label>
                <input
                  v-model="endDate"
                  type="date"
                  class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2.5 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                  :disabled="isBacktesting"
                />
              </div>
              
              <!-- 进度显示 -->
              <div class="flex items-end">
                <div v-if="isBacktesting" class="w-full">
                  <div class="flex items-center justify-between text-sm text-slate-400 mb-1">
                    <span>{{ hasParamChanges ? '两组参数并行回测中' : '原参数回测中' }}</span>
                    <span>{{ backtestProgress.toFixed(0) }}%</span>
                  </div>
                  <div class="h-2 bg-slate-700 rounded-full overflow-hidden">
                    <div 
                      class="h-full bg-gradient-to-r from-emerald-500 to-teal-500 transition-all duration-300"
                      :style="{ width: backtestProgress + '%' }"
                    ></div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          
          <!-- 参数对比区域 -->
          <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- 原参数 -->
            <div class="bg-[#151925] rounded-xl border border-slate-800 overflow-hidden">
              <div class="px-5 py-4 border-b border-slate-800 bg-slate-800/30">
                <div class="flex items-center justify-between">
                  <h2 class="text-lg font-semibold text-white flex items-center gap-2">
                    <i class="fas fa-cube text-slate-400"></i>
                    原参数（默认值）
                  </h2>
                  <span class="text-xs text-slate-500 bg-slate-700 px-2 py-1 rounded">只读</span>
                </div>
              </div>
              <div class="p-5 max-h-[500px] overflow-y-auto">
                <div v-if="isLoadingParams" class="flex items-center justify-center py-8 text-slate-400">
                  <i class="fas fa-spinner fa-spin mr-2"></i>
                  加载参数中...
                </div>
                <div v-else-if="availableParams.length === 0" class="text-center py-8 text-slate-500">
                  请先选择策略
                </div>
                <div v-else class="space-y-4">
                  <div v-for="group in paramGroups" :key="group.name" class="space-y-3">
                    <h3 class="text-sm font-medium text-slate-400 border-b border-slate-700 pb-2">
                      {{ group.displayName }}
                    </h3>
                    <div v-for="param in group.params" :key="param.key" class="flex items-center justify-between">
                      <label class="text-sm text-slate-300">{{ param.label }}</label>
                      <div class="bg-slate-700/50 px-3 py-1.5 rounded text-slate-300 text-sm min-w-[100px] text-right">
                        {{ originalParams[param.key] }}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            
            <!-- 新参数 -->
            <div class="bg-[#151925] rounded-xl border border-slate-800 overflow-hidden">
              <div class="px-5 py-4 border-b border-slate-800 bg-emerald-500/10">
                <div class="flex items-center justify-between">
                  <h2 class="text-lg font-semibold text-white flex items-center gap-2">
                    <i class="fas fa-edit text-emerald-400"></i>
                    新参数（可修改）
                  </h2>
                  <button
                    @click="resetNewParams"
                    class="text-xs text-slate-400 hover:text-white bg-slate-700 hover:bg-slate-600 px-2 py-1 rounded transition-colors"
                    :disabled="isBacktesting"
                  >
                    <i class="fas fa-undo mr-1"></i>
                    重置
                  </button>
                </div>
              </div>
              <div class="p-5 max-h-[500px] overflow-y-auto">
                <div v-if="isLoadingParams" class="flex items-center justify-center py-8 text-slate-400">
                  <i class="fas fa-spinner fa-spin mr-2"></i>
                  加载参数中...
                </div>
                <div v-else-if="availableParams.length === 0" class="text-center py-8 text-slate-500">
                  请先选择策略
                </div>
                <div v-else class="space-y-4">
                  <div v-for="group in paramGroups" :key="group.name" class="space-y-3">
                    <h3 class="text-sm font-medium text-slate-400 border-b border-slate-700 pb-2">
                      {{ group.displayName }}
                    </h3>
                    <div v-for="param in group.params" :key="param.key" class="flex items-center justify-between gap-4">
                      <label class="text-sm text-slate-300 flex-shrink-0">{{ param.label }}</label>
                      <div class="flex items-center gap-2">
                        <input
                          v-model.number="newParams[param.key]"
                          type="number"
                          :min="param.min"
                          :max="param.max"
                          :step="param.step || 0.01"
                          class="w-28 bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-white text-sm text-right focus:ring-2 focus:ring-emerald-500 focus:border-transparent"
                          :class="{ 'border-emerald-500 bg-emerald-500/10': originalParams[param.key] !== newParams[param.key] }"
                          :disabled="isBacktesting"
                        />
                        <span 
                          v-if="originalParams[param.key] !== newParams[param.key]"
                          class="text-emerald-400 text-xs"
                        >
                          <i class="fas fa-asterisk"></i>
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
              
              <!-- 变更摘要 -->
              <div v-if="changedParams.length > 0" class="px-5 py-3 border-t border-slate-800 bg-emerald-500/5">
                <div class="text-sm text-emerald-400">
                  <i class="fas fa-info-circle mr-1"></i>
                  已修改 {{ changedParams.length }} 个参数
                </div>
              </div>
            </div>
          </div>
          
          <!-- 结果对比区域 -->
          <div v-if="originalResult || newResult" class="space-y-6">
            <!-- 指标对比表格 -->
            <div class="bg-[#151925] rounded-xl border border-slate-800 overflow-hidden">
              <div class="px-5 py-4 border-b border-slate-800">
                <h2 class="text-lg font-semibold text-white flex items-center gap-2">
                  <i class="fas fa-chart-bar text-indigo-400"></i>
                  指标对比
                </h2>
              </div>
              <div class="overflow-x-auto">
                <table class="w-full">
                  <thead>
                    <tr class="bg-slate-800/50">
                      <th class="px-5 py-3 text-left text-sm font-medium text-slate-400">指标</th>
                      <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">原参数</th>
                      <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">新参数</th>
                      <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">变化</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-slate-800">
                    <tr v-for="metric in metricsConfig" :key="metric.key" class="hover:bg-slate-800/30">
                      <td class="px-5 py-3 text-sm text-slate-300">{{ metric.label }}</td>
                      <td class="px-5 py-3 text-sm text-right text-slate-300">
                        <template v-if="originalResult?.metrics">
                          <span v-if="metric.format === 'percent'">{{ formatPercent(originalResult.metrics[metric.key as keyof BacktestMetrics] as number) }}</span>
                          <span v-else-if="metric.format === 'integer'">{{ originalResult.metrics[metric.key as keyof BacktestMetrics] }}</span>
                          <span v-else>{{ formatNumber(originalResult.metrics[metric.key as keyof BacktestMetrics] as number) }}</span>
                        </template>
                        <span v-else class="text-slate-500">-</span>
                      </td>
                      <td class="px-5 py-3 text-sm text-right text-slate-300">
                        <template v-if="newResult?.metrics">
                          <span v-if="metric.format === 'percent'">{{ formatPercent(newResult.metrics[metric.key as keyof BacktestMetrics] as number) }}</span>
                          <span v-else-if="metric.format === 'integer'">{{ newResult.metrics[metric.key as keyof BacktestMetrics] }}</span>
                          <span v-else>{{ formatNumber(newResult.metrics[metric.key as keyof BacktestMetrics] as number) }}</span>
                        </template>
                        <span v-else class="text-slate-500">-</span>
                      </td>
                      <td class="px-5 py-3 text-sm text-right">
                        <template v-if="getMetricDiff(metric.key as keyof BacktestMetrics) !== null">
                          <span 
                            :class="[
                              isImproved(metric.key as keyof BacktestMetrics) ? 'text-emerald-400' : 'text-red-400',
                              'font-medium'
                            ]"
                          >
                            <i :class="isImproved(metric.key as keyof BacktestMetrics) ? 'fas fa-arrow-up' : 'fas fa-arrow-down'" class="mr-1"></i>
                            <span v-if="metric.format === 'percent'">{{ formatPercent(Math.abs(getMetricDiff(metric.key as keyof BacktestMetrics)!)) }}</span>
                            <span v-else-if="metric.format === 'integer'">{{ Math.abs(getMetricDiff(metric.key as keyof BacktestMetrics)!) }}</span>
                            <span v-else>{{ formatNumber(Math.abs(getMetricDiff(metric.key as keyof BacktestMetrics)!)) }}</span>
                          </span>
                        </template>
                        <span v-else class="text-slate-500">-</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
            
            <!-- 参数变化详情 -->
            <div v-if="changedParams.length > 0" class="bg-[#151925] rounded-xl border border-slate-800 overflow-hidden">
              <div class="px-5 py-4 border-b border-slate-800">
                <h2 class="text-lg font-semibold text-white flex items-center gap-2">
                  <i class="fas fa-sliders-h text-amber-400"></i>
                  参数变化详情
                </h2>
              </div>
              <div class="p-5">
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  <div 
                    v-for="param in changedParams" 
                    :key="param.key"
                    class="bg-slate-800/50 rounded-lg p-4 border border-slate-700"
                  >
                    <div class="text-sm text-slate-400 mb-2">{{ param.label }}</div>
                    <div class="flex items-center justify-between">
                      <div class="text-slate-500">
                        <span class="text-slate-300">{{ originalParams[param.key] }}</span>
                      </div>
                      <i class="fas fa-arrow-right text-slate-600 mx-3"></i>
                      <div class="text-emerald-400 font-medium">
                        {{ newParams[param.key] }}
                      </div>
                    </div>
                    <div class="mt-2 text-xs text-slate-500">
                      变化: {{ ((Number(newParams[param.key]) - Number(originalParams[param.key])) / Number(originalParams[param.key]) * 100).toFixed(1) }}%
                    </div>
                  </div>
                </div>
              </div>
            </div>
            
            <!-- 收益曲线对比 -->
            <div v-if="originalResult?.equityCurve?.length || newResult?.equityCurve?.length" class="bg-[#151925] rounded-xl border border-slate-800 overflow-hidden">
              <div class="px-5 py-4 border-b border-slate-800">
                <h2 class="text-lg font-semibold text-white flex items-center gap-2">
                  <i class="fas fa-chart-line text-blue-400"></i>
                  收益曲线对比
                </h2>
              </div>
              <div class="p-5">
                <div v-if="equityVersions.length > 0" class="h-80">
                  <EquityCurve
                    :versions="equityVersions"
                    :benchmark="benchmarkCurve"
                    :kline-data="[]"
                    :highlight-ranges="[]"
                    mode="equity"
                    scale="linear"
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
    
    <!-- 确认对话框 -->
    <div v-if="confirmationDialog.visible" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div class="bg-[#1e222d] border border-[#2a2e39] rounded-lg p-6 max-w-md w-full mx-4">
        <h3 class="text-lg font-semibold text-white mb-2">{{ confirmationDialog.title }}</h3>
        <p class="text-sm text-[#787b86] mb-4">{{ confirmationDialog.message }}</p>
        <div class="flex justify-end space-x-3">
          <button
            @click="cancelDialog"
            class="px-4 py-2 text-sm text-[#787b86] hover:text-white transition-colors"
          >
            取消
          </button>
          <button
            @click="confirmDialog"
            class="px-4 py-2 text-sm bg-[#2962ff] text-white rounded hover:bg-[#1e53e5] transition-colors"
          >
            确认
          </button>
        </div>
      </div>
    </div>
  </div>
</template>