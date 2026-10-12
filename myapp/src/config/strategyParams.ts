// 策略参数元数据
export interface StrategyParamMeta {
  key: string;
  label: string;
  type: 'number' | 'boolean' | 'string';
  min?: number;
  max?: number;
  step?: number;
  default: number | boolean | string;
  defaultValue?: number | boolean | string;
  description?: string;
}

// 策略参数值映射
export interface StrategyParamValueMap {
  [key: string]: number | boolean | string;
}

// 策略参数配置
export interface StrategyParamsConfig {
  strategyId?: string | null;
  strategyName?: string;
  params: StrategyParamMeta[];
}

// 默认策略参数配置
export const strategyParamConfigs: StrategyParamsConfig[] = [
  // 可以在这里添加各个策略的参数配置
  // 例如：
  // {
  //   strategyId: 'v3_strategy',
  //   strategyName: 'V3策略',
  //   params: [
  //     { key: 'param1', label: '参数1', type: 'number', min: 0, max: 100, step: 1, default: 50 }
  //   ]
  // }
];

