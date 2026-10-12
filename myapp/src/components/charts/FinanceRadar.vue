<!--
  FinanceRadar - 6 维度财务雷达图

  设计原则:
    1. 数据驱动: 仅渲染 has_data=true 时才绘制雷达, value=null 维度自动跳过
    2. 降级透明: 维度缺失时显示 "数据缺失" 角标 + "—" 数值
    3. demo 模式: 右上加 "Demo / 数据源未接入" 角标, 不伪造财务字段
    4. 中文 DocString + 字段说明, 模板下方注明数据源/计算式
    5. 不画"形式自洽但无业务意义"的雷达
-->
<template>
  <div class="finance-radar">
    <!-- 标题栏 + 角标 -->
    <header class="finance-radar__head">
      <div class="finance-radar__title-block">
        <h3 class="finance-radar__title">财务雷达</h3>
        <p v-if="stockName" class="finance-radar__subtitle">
          {{ stockName }} <span class="finance-radar__symbol">({{ symbol }})</span>
        </p>
        <p v-else class="finance-radar__subtitle">{{ symbol }}</p>
      </div>
      <div class="finance-radar__badges">
        <span v-if="data?.demo_mode" class="badge badge--demo" title="数据源未完全接入, 估值维度由本地日线计算">
          Demo / 数据源未接入
        </span>
        <span v-if="dataDate" class="badge badge--date">数据日期 {{ dataDate }}</span>
      </div>
    </header>

    <!-- 雷达图本体 -->
    <div v-if="hasAnyData" ref="chartContainer" class="finance-radar__chart"></div>

    <!-- 全部缺失: EmptyState -->
    <EmptyState
      v-else
      title="暂无本地财务证据"
      description="财务数据源未接入, 当前无任何维度可绘制"
      :hint="emptyHint"
    />

    <!-- 维度摘要 + 字段说明 -->
    <section v-if="hasAnyData" class="finance-radar__dimensions">
      <article
        v-for="dim in dimensionList"
        :key="dim.key"
        class="dim-card"
        :class="{ 'dim-card--missing': dim.value === null }"
      >
        <header class="dim-card__head">
          <h4>{{ dim.label }}</h4>
          <span v-if="dim.value === null" class="dim-card__missing-flag">数据缺失</span>
          <span v-else class="dim-card__value">{{ formatScore(dim.value) }}</span>
        </header>
        <p v-if="dim.value === null" class="dim-card__formula">
          {{ formatMissingFormula(dim) }}
        </p>
        <ul v-else class="dim-card__fields">
          <li v-for="(field, fieldName) in dim.fields" :key="fieldName">
            <code class="dim-card__field-name">{{ fieldName }}</code>
            <span class="dim-card__field-value">
              {{ formatFieldValue(fieldName, field) }}
            </span>
            <small class="dim-card__field-formula">{{ field.formula }}</small>
            <small class="dim-card__field-source">数据源: {{ field.source }}</small>
          </li>
        </ul>
      </article>
    </section>

    <!-- 边界声明: 不构成投资建议 -->
    <p v-if="hasAnyData" class="finance-radar__disclaimer">
      本雷达仅展示本地结构化证据与公式, 不构成买入、卖出、仓位、止盈或止损建议。
    </p>
  </div>
</template>

<script setup lang="ts">
/**
 * FinanceRadar - 财务雷达图组件
 *
 * Props:
 *  - symbol:    股票代码 (用于展示, 实际数据由 props.data 传入)
 *  - stockName: 股票名称 (可选, 缺省时仅展示 symbol)
 *  - data:      财务雷达响应 (来自 apiService.getFinanceRadar)
 *
 * 行为约定:
 *  - 全部维度 value=null → 显示 EmptyState ("暂无本地财务证据")
 *  - 至少 1 个维度有值 → 渲染雷达, 缺失顶点自动不渲染
 *  - demo_mode=true → 右上角 "Demo / 数据源未接入" 角标
 */
import { computed, markRaw, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import EmptyState from '../common/EmptyState.vue'

interface FinanceField {
  value: number | null
  current?: number | null
  sample_size?: number
  source: string
  formula: string
}

interface FinanceDimension {
  value: number | null
  fields: Record<string, FinanceField>
  trade_date?: string | null
}

interface FinanceRadarPayload {
  symbol: string
  name: string | null
  dimensions: Record<string, FinanceDimension>
  has_data: boolean
  demo_mode: boolean
  missing_dimensions: string[]
  present_dimensions: string[]
}

const props = defineProps<{
  symbol: string
  stockName?: string | null
  data: FinanceRadarPayload | null
}>()

// 维度展示顺序 (与后端 DIMENSION_KEYS 保持一致)
const DIMENSION_ORDER: Array<{ key: string; label: string }> = [
  { key: 'growth', label: '成长性' },
  { key: 'profitability', label: '盈利能力' },
  { key: 'valuation', label: '估值' },
  { key: 'cashflow', label: '现金流' },
  { key: 'solvency', label: '偿债能力' },
  { key: 'quality', label: '质量' },
]

const chartContainer = ref<HTMLDivElement | null>(null)
let chartInstance: echarts.ECharts | null = null

const hasAnyData = computed(() => {
  if (!props.data) return false
  return props.data.has_data === true && props.data.present_dimensions.length > 0
})

const dataDate = computed(() => {
  if (!props.data) return ''
  const v = props.data.dimensions?.valuation?.trade_date
  return v ? String(v) : ''
})

const dimensionList = computed(() =>
  DIMENSION_ORDER.map(({ key, label }) => {
    const dim = props.data?.dimensions?.[key] ?? { value: null, fields: {} }
    return {
      key,
      label,
      value: dim.value ?? null,
      fields: dim.fields ?? {},
    }
  })
)

const emptyHint = computed(() => {
  if (!props.data) return ''
  const missing = props.data.missing_dimensions?.length ?? 0
  return missing > 0
    ? `当前缺失 ${missing} 个维度, 数据源未接入`
    : '当前数据源暂无任何记录'
})

/** 把维度分值格式化为 2 位小数 + " 分" */
function formatScore(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${v.toFixed(2)} 分`
}

/** 把单字段值格式化为可读字符串 */
function formatFieldValue(name: string, field: FinanceField): string {
  if (field.value === null || field.value === undefined) return '—'
  if (name.endsWith('_quantile')) {
    const pct = (field.value * 100).toFixed(1)
    const current = field.current !== null && field.current !== undefined && !Number.isNaN(field.current as number)
      ? ` (当前 ${(field.current as number).toFixed(2)})`
      : ''
    return `分位 ${pct}%${current}`
  }
  if (typeof field.value === 'number') {
    return field.value.toFixed(2)
  }
  return String(field.value)
}

/** 维度缺失时, 把 formula 字段降级为简短说明 */
function formatMissingFormula(dim: { key: string; fields: Record<string, FinanceField> }): string {
  const firstField = Object.values(dim.fields)[0]
  if (!firstField) return '数据源未接入'
  return firstField.formula
}

/** 初始化 echarts 雷达图 — 仅在有数据时调用 */
function initChart() {
  if (!chartContainer.value) return
  if (chartContainer.value.clientWidth === 0) {
    // 容器尚未布局, 延后重试
    setTimeout(initChart, 80)
    return
  }
  if (chartInstance) {
    chartInstance.dispose()
  }
  chartInstance = markRaw(echarts.init(chartContainer.value))
  updateChart()
}

function updateChart() {
  if (!chartInstance || !props.data) return
  const present = props.data.present_dimensions
  if (present.length === 0) return

  // 构造 indicator + value, 缺失维度用 null (echarts 会跳过)
  const indicator: echarts.EChartsOption['radar'] = {
    indicator: DIMENSION_ORDER.map(({ key, label }) => ({
      name: label,
      max: 100,
    })),
    center: ['50%', '55%'],
    radius: '68%',
    axisName: {
      fontSize: 12,
      color: '#94a3b8',
      // 缺失维度名称变灰
      formatter: (raw: string) => {
        const dimKey = DIMENSION_ORDER.find((d) => d.label === raw)?.key
        if (!dimKey) return raw
        const dim = props.data?.dimensions?.[dimKey]
        if (!dim || dim.value === null) {
          return `{missing|${raw}}`
        }
        return `{present|${raw}}`
      },
      rich: {
        present: { color: '#cbd5e1' },
        missing: { color: '#64748b', fontStyle: 'italic' },
      },
    },
    splitLine: { lineStyle: { color: '#334155' } },
    splitArea: {
      show: true,
      areaStyle: {
        color: ['rgba(255,255,255,0.04)', 'rgba(255,255,255,0.02)'],
      },
    },
  }

  const values = DIMENSION_ORDER.map(({ key }) => {
    const dim = props.data?.dimensions?.[key]
    return dim?.value ?? null
  })

  const series: any[] = [
    {
      name: '财务雷达',
      type: 'radar',
      // value 中允许 null — echarts 会跳过该顶点
      value: values,
      symbol: 'circle',
      symbolSize: 6,
      lineStyle: { color: '#06b6d4', width: 2 },
      itemStyle: { color: '#06b6d4' },
      areaStyle: {
        color: new echarts.graphic.RadialGradient(0.5, 0.5, 1, [
          { offset: 0, color: 'rgba(6, 182, 212, 0.15)' },
          { offset: 1, color: 'rgba(6, 182, 212, 0.35)' },
        ]),
      },
    },
  ]

  chartInstance.setOption({
    radar: indicator,
    series,
    tooltip: {
      trigger: 'item',
      formatter: (params: any) => {
        const lines = ['<div style="font-size:12px">']
        DIMENSION_ORDER.forEach(({ key, label }, idx) => {
          const v = values[idx]
          const dim = props.data?.dimensions?.[key]
          if (v === null || v === undefined) {
            lines.push(`<div>${label}: <span style="color:#94a3b8">数据缺失</span></div>`)
          } else {
            const fields = dim?.fields ? Object.keys(dim.fields).join(', ') : ''
            lines.push(
              `<div>${label}: <strong>${v.toFixed(2)}</strong> 分 <span style="color:#94a3b8;font-size:11px">(${fields})</span></div>`
            )
          }
        })
        lines.push('</div>')
        return lines.join('')
      },
    },
    legend: {
      show: false,
    },
  })
}

function handleResize() {
  chartInstance?.resize()
}

watch(
  () => [props.data, props.symbol],
  () => {
    if (hasAnyData.value) {
      nextTick(() => {
        if (!chartInstance) {
          initChart()
        } else {
          updateChart()
        }
      })
    } else if (chartInstance) {
      chartInstance.dispose()
      chartInstance = null
    }
  },
  { deep: true }
)

onMounted(() => {
  nextTick(() => {
    if (hasAnyData.value) initChart()
    window.addEventListener('resize', handleResize)
  })
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chartInstance?.dispose()
  chartInstance = null
})
</script>

<style scoped>
.finance-radar {
  display: flex;
  flex-direction: column;
  gap: 16px;
  width: 100%;
}

.finance-radar__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.finance-radar__title-block { display: flex; flex-direction: column; gap: 4px; }
.finance-radar__title { font-size: 18px; font-weight: 600; color: #e2e8f0; margin: 0; }
.finance-radar__subtitle { font-size: 13px; color: #94a3b8; margin: 0; }
.finance-radar__symbol { color: #64748b; font-family: ui-monospace, monospace; }

.finance-radar__badges { display: flex; flex-wrap: wrap; gap: 6px; }
.badge {
  display: inline-flex; align-items: center;
  padding: 3px 10px; border-radius: 999px;
  font-size: 11px; font-weight: 600;
  white-space: nowrap;
}
.badge--demo {
  color: #92400e; background: #fef3c7; border: 1px solid #fde68a;
}
.badge--date {
  color: #475569; background: #f1f5f9; border: 1px solid #e2e8f0;
}

.finance-radar__chart {
  width: 100%;
  height: 360px;
  background: #0f172a;
  border-radius: 12px;
  border: 1px solid #1e293b;
}

.finance-radar__dimensions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.dim-card {
  padding: 12px 14px;
  border: 1px solid #1e293b;
  border-radius: 10px;
  background: #0f172a;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dim-card--missing {
  background: #0b1220;
  border-style: dashed;
  opacity: 0.85;
}
.dim-card__head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px;
}
.dim-card__head h4 {
  margin: 0; font-size: 14px; font-weight: 600; color: #e2e8f0;
}
.dim-card__value { font-size: 14px; font-weight: 700; color: #06b6d4; font-family: ui-monospace, monospace; }
.dim-card__missing-flag {
  font-size: 11px; color: #94a3b8;
  padding: 2px 8px; border-radius: 999px; background: #1e293b; border: 1px solid #334155;
}
.dim-card__formula {
  margin: 0; font-size: 12px; color: #94a3b8; line-height: 1.55;
}

.dim-card__fields {
  margin: 0; padding: 0; list-style: none;
  display: flex; flex-direction: column; gap: 8px;
}
.dim-card__fields li {
  display: flex; flex-direction: column; gap: 2px;
  padding: 6px 8px; border-radius: 6px; background: #0b1220;
  border: 1px solid #1e293b;
}
.dim-card__field-name {
  font-family: ui-monospace, monospace; font-size: 11px;
  color: #67e8f9; font-weight: 600;
}
.dim-card__field-value {
  font-size: 13px; color: #f1f5f9; font-weight: 600;
}
.dim-card__field-formula, .dim-card__field-source {
  font-size: 11px; color: #64748b; line-height: 1.4;
}

.finance-radar__disclaimer {
  margin: 8px 0 0; text-align: center;
  font-size: 12px; color: #64748b;
}
</style>
