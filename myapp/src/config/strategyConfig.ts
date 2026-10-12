// 策略参数显示配置
export interface ParamDisplayConfig {
  unit?: string;
  scale?: number;
}

export const PARAM_DISPLAY: Record<string, ParamDisplayConfig> = {
  market_cap_min: {
    unit: '亿',
    scale: 10000,
  },
  market_cap_max: {
    unit: '亿',
    scale: 10000,
  },
};

// 参数自定义覆盖表配置
export interface CustomParamConfig {
  overrideMin?: number;     // 物理轨道下限 (后端单位)
  overrideMax?: number;     // 物理轨道上限 (后端单位)
  defaultStart?: number;    // 默认把手起始位置 (后端单位)
  defaultEnd?: number;      // 默认把手结束位置 (后端单位)
  step?: number;            // 滑块步长 (UI单位)
  useSlider?: boolean;      // 是否强制使用双向滑块组件
  useLogScale?: boolean;    // 是否使用对数轴 (适合跨度极大的数值)
}

export const CUSTOM_PARAM_CONFIG: Record<string, CustomParamConfig> = {
  // === 市值参数配置 ===
  'market_cap_min': {
    overrideMin: 100,
    overrideMax: 50000000,    // 5000亿 (单位：万元)
    defaultStart: 200000,     // 20亿
    defaultEnd: 600000,       // 60亿（调整以匹配后端默认值）
    step: 0.1,
    useSlider: true,
    useLogScale: true
  },
  'market_cap_max': {
    overrideMin: 100,
    overrideMax: 50000000,
    defaultStart: 200000,     // 20亿
    defaultEnd: 1000000,      // 100亿（确保与min有合理差距，从60亿调整为100亿）
    step: 0.1,
    useSlider: true,
    useLogScale: true
  },
};

// 算法配置
export type AlgorithmKey = 'ga' | 'pso' | 'cmaes' | 'simulatedAnnealing' | 'bayesian' | 'grid';
export type FieldType = 'int' | 'float' | 'select';

export interface AlgorithmField {
  key: string;
  label: string;
  type: FieldType;
  default: number | string;
  step?: number;
  min?: number;
  max?: number;
  options?: Array<{ value: string; label: string }>;
  hint?: string;
}

export interface AlgorithmConfig {
  label: string;
  method: string;
  fields: AlgorithmField[];
  description?: string;
}

export const ALGO_CONFIGS: Record<AlgorithmKey, AlgorithmConfig> = {
  ga: {
    label: '遗传算法 (GA)',
    method: 'ga',
    description: '最常用的全局优化器，适合离散与连续混合空间',
    fields: [
      { key: 'pop_size', label: '种群大小', default: 50, type: 'int', min: 5 },
      { key: 'generations', label: '最大代数', default: 100, type: 'int', min: 10 },
      { key: 'mutation_rate', label: '变异率', default: 0.1, type: 'float', step: 0.01, min: 0, max: 1 },
      { key: 'crossover_rate', label: '交叉率', default: 0.8, type: 'float', step: 0.01, min: 0, max: 1 },
    ],
  },
  pso: {
    label: '粒子群优化 (PSO)',
    method: 'pso',
    description: '模拟鸟群觅食，收敛速度快',
    fields: [
      { key: 'particle_count', label: '粒子数量', default: 40, type: 'int', min: 10, max: 200, hint: '推荐 30-50' },
      { key: 'iterations', label: '最大迭代次数', default: 100, type: 'int', min: 10, max: 500 },
      { key: 'w', label: '惯性权重 (w)', default: 0.7, type: 'float', step: 0.01, min: 0.1, max: 1.2 },
      { key: 'c1', label: '自我学习因子 (c1)', default: 1.5, type: 'float', step: 0.1, min: 0.5, max: 3 },
      { key: 'c2', label: '社会学习因子 (c2)', default: 1.5, type: 'float', step: 0.1, min: 0.5, max: 3 },
    ],
  },
  cmaes: {
    label: 'CMA-ES',
    method: 'cma_es',
    description: '连续变量优化利器，参数很少',
    fields: [
      { key: 'population', label: '种群大小 (λ)', default: 30, type: 'int', min: 5, max: 200, hint: '推荐 20-50' },
      { key: 'sigma', label: '初始步长 (Sigma)', default: 0.5, type: 'float', step: 0.05, min: 0.01 },
      { key: 'max_evaluations', label: '最大评估次数', default: 200, type: 'int', min: 10 },
    ],
  },
  simulatedAnnealing: {
    label: '模拟退火 (SA)',
    method: 'sa',
    description: '防止陷入局部最优，探索性强',
    fields: [
      { key: 'initial_temp', label: '初始温度', default: 100, type: 'float', step: 1 },
      { key: 'min_temp', label: '最低温度', default: 0.01, type: 'float', step: 0.01, min: 0 },
      { key: 'cooling_rate', label: '冷却速率', default: 0.95, type: 'float', step: 0.01, min: 0, max: 1 },
      { key: 'steps_per_temp', label: '每温迭代次数', default: 10, type: 'int', min: 1 },
    ],
  },
  bayesian: {
    label: '贝叶斯优化',
    method: 'bayesian',
    description: '昂贵但聪明，适合昂贵评估函数',
    fields: [
      { key: 'random_init_points', label: '随机初始点', default: 10, type: 'int', min: 1 },
      { key: 'iterations', label: '迭代次数', default: 50, type: 'int', min: 5 },
      {
        key: 'acquisition_function',
        label: '采集函数',
        default: 'EI',
        type: 'select',
        options: [
          { value: 'EI', label: 'EI (期望提升)' },
          { value: 'PI', label: 'PI (概率提升)' },
          { value: 'UCB', label: 'UCB (置信上界)' },
        ],
      },
    ],
  },
  grid: {
    label: '网格搜索',
    method: 'grid',
    description: '无引擎参数，使用下方参数空间的步长/密度构造网格',
    fields: [],
  },
};

// 默认日期配置
export const DEFAULT_DATES = {
  startDate: '2024-05-20',
  endDate: '2025-01-20',
  trainStartDate: '2024-05-20',
  trainEndDate: '2025-01-20',
  valStartDate: '2025-02-01',
  valEndDate: '2025-05-30',
  testStartDate: '2025-06-01',
  testEndDate: '2025-11-04',
};



