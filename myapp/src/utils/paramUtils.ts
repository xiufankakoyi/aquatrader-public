/**
 * 策略参数工具函数
 *
 * 统一处理策略参数在前端展示、编辑、回传后端时的单位换算与格式化。
 */
import { CUSTOM_PARAM_CONFIG, PARAM_DISPLAY } from '@/config/strategyConfig';

/** 默认数值参数范围 */
export interface ParamRange {
  min: number;
  max: number;
}

/**
 * 获取参数单位
 * @param key 参数键名
 * @returns 单位字符串，无则返回 undefined
 */
export function getParamUnit(key: string): string | undefined {
  return PARAM_DISPLAY[key]?.unit;
}

/**
 * 后端原始值 -> UI 显示值
 * @param value 后端原始值
 * @param key 参数键名
 * @returns UI 显示值
 */
export function toUiValue(value: number, key: string): number {
  const scale = PARAM_DISPLAY[key]?.scale;
  if (scale && scale !== 0) {
    return value / scale;
  }
  return value;
}

/**
 * UI 显示值 -> 后端原始值
 * @param value UI 显示值
 * @param key 参数键名
 * @returns 后端原始值
 */
export function toBackendValue(value: number, key: string): number {
  const scale = PARAM_DISPLAY[key]?.scale;
  if (scale && scale !== 0) {
    return value * scale;
  }
  return value;
}

/**
 * 格式化参数显示文本
 * @param value 数值
 * @param key 参数键名
 * @returns 带单位的格式化字符串
 */
export function formatParamDisplayValue(value: number, key: string): string {
  const unit = getParamUnit(key);
  const num = Number.isFinite(value) ? value.toLocaleString('zh-CN') : String(value);
  return unit ? `${num} ${unit}` : num;
}

/**
 * 获取参数可读标签
 * @param key 参数键名
 * @returns 中文标签
 */
export function getParamLabel(key: string): string {
  const known: Record<string, string> = {
    market_cap_min: '最小市值',
    market_cap_max: '最大市值',
    pe_ratio_min: '最小市盈率',
    pe_ratio_max: '最大市盈率',
    pb_ratio_min: '最小市净率',
    pb_ratio_max: '最大市净率',
    roe_min: '最小净资产收益率',
    revenue_growth_min: '最小营收增速',
    profit_growth_min: '最小利润增速',
  };
  return known[key] ?? key;
}

/**
 * 根据参数类型生成默认搜索范围
 * @param param 后端返回的参数元信息
 * @returns 默认 [min, max]
 */
export function getDefaultRange(param: { key?: string; type?: string; min?: number; max?: number }): ParamRange {
  const key = param.key ?? '';
  const config = CUSTOM_PARAM_CONFIG[key];

  if (config) {
    return {
      min: config.overrideMin ?? 0,
      max: config.overrideMax ?? 100,
    };
  }

  if (param.min !== undefined && param.max !== undefined) {
    return { min: param.min, max: param.max };
  }

  // 按参数名推断常见范围
  if (key.includes('market_cap')) {
    return { min: 0, max: 50000000 };
  }
  if (key.includes('ratio') || key.includes('rate') || key.includes('pct')) {
    return { min: 0, max: 1 };
  }
  if (key.includes('days') || key.includes('period') || key.includes('window')) {
    return { min: 1, max: 252 };
  }

  return { min: 0, max: 100 };
}
