/**
 * 统一数值/时间格式化工具。
 *
 * 项目内所有前端页面共用此模块，避免重复实现导致格式不一致。
 * 遵循 AGENTS.md：百分比 2 位、分数 4 位、金额用 万/亿。
 * 字段缺失统一降级到 fallback 字符串，不让页面崩溃。
 */

/** 默认降级文案。 */
const DEFAULT_FALLBACK = '未知';

/** 默认「无证据」降级文案。 */
const NO_EVIDENCE_FALLBACK = '暂无本地证据';

/** 万元阈值：低于此值用「元」表示。 */
const WAN_THRESHOLD = 10_000;

/** 亿元阈值：高于此值用「亿」表示。 */
const YI_THRESHOLD = 100_000_000;

/** 数字转 Number，非法时返回 NaN。 */
function toNumber(v: unknown): number {
  if (v === null || v === undefined || v === '') return NaN;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : NaN;
}

/**
 * 百分比格式化（默认 2 位小数）。
 *
 * @param v 数值，通常为 0~1 的小数（如 0.1234 表示 12.34%）。
 * @param digits 小数位数，默认 2。
 * @returns 形如 "12.34%"；缺值返回 "未知"。
 */
export function formatPercent(v: number, digits = 2): string {
  const n = toNumber(v);
  if (Number.isNaN(n)) return DEFAULT_FALLBACK;
  return `${(n * 100).toFixed(digits)}%`;
}

/**
 * 分数格式化（默认 4 位小数）。
 *
 * @param v 数值（0~1）。
 * @param digits 小数位数，默认 4。
 * @returns 形如 "0.8234"；缺值返回 "-"。
 */
export function formatScore(v: number, digits = 4): string {
  const n = toNumber(v);
  if (Number.isNaN(n)) return '-';
  return n.toFixed(digits);
}

/**
 * 金额格式化，统一使用 万/亿 单位。
 *
 * @param v 金额（元）。
 * @returns 形如 "1.23 万"、"4.56 亿"；缺值返回 "未知"。
 */
export function formatAmount(v: number): string {
  const n = toNumber(v);
  if (Number.isNaN(n)) return DEFAULT_FALLBACK;
  const abs = Math.abs(n);
  if (abs >= YI_THRESHOLD) return `${(n / YI_THRESHOLD).toFixed(2)} 亿`;
  if (abs >= WAN_THRESHOLD) return `${(n / WAN_THRESHOLD).toFixed(2)} 万`;
  return `${n.toFixed(2)} 元`;
}

/**
 * 通用数字格式化。
 *
 * @param v 数值。
 * @param digits 小数位数，默认 2。
 * @returns 形如 "1,234.56"；缺值返回 "未知"。
 */
export function formatNumber(v: number, digits = 2): string {
  const n = toNumber(v);
  if (Number.isNaN(n)) return DEFAULT_FALLBACK;
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/**
 * 时间格式化（zh-CN 本地化）。
 *
 * @param v ISO 字符串或可被 Date 解析的时间值。
 * @returns 形如 "2026/7/14 10:30:00"；缺值返回 "未知"。
 */
export function formatTime(v: string): string {
  if (!v) return DEFAULT_FALLBACK;
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return DEFAULT_FALLBACK;
  return d.toLocaleString('zh-CN', { hour12: false });
}

/**
 * 安全格式化：字段缺失时返回 fallback。
 *
 * @param v 任意值。
 * @param fallback 缺失时的降级文案，默认 "未知"。
 * @returns 非空字符串原值；否则返回 fallback。
 */
export function formatSafe(v: unknown, fallback: string = DEFAULT_FALLBACK): string {
  if (v === null || v === undefined || v === '') return fallback;
  const s = String(v).trim();
  return s === '' ? fallback : s;
}

/** 「暂无本地证据」专用降级文案，用于无证据场景。 */
export function noEvidence(): string {
  return NO_EVIDENCE_FALLBACK;
}

export default {
  formatPercent,
  formatScore,
  formatAmount,
  formatNumber,
  formatTime,
  formatSafe,
  noEvidence,
};
