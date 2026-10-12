<!--
  FinanceRadarPage - 财务雷达页面
  接入通用状态组件 + DebugButton, 严格遵守 AGENTS.md:
    - 不输出投资建议
    - 不伪造财务数据
    - 字段缺失降级
    - 概念映射默认本地结构化证据, 缺则 "暂无本地证据"
-->
<template>
  <main class="finance-radar-page">
    <DebugButton
      :data-date="dataDate"
      :demo-mode="demoMode"
      :stale="false"
      :partial-success="partialSuccess"
    />

    <header class="page-header">
      <div>
        <h1 class="page-title">财务雷达</h1>
        <p class="page-subtitle">
          6 维度结构化分析 — 成长性 / 盈利能力 / 估值 / 现金流 / 偿债 / 质量
        </p>
      </div>

      <div class="search-block">
        <input
          v-model="symbolInput"
          type="text"
          class="search-input"
          placeholder="输入股票代码 (如 000001.SZ)"
          @keydown.enter="onSearch"
        />
        <button class="search-btn" :disabled="isLoading" @click="onSearch">
          <i class="fas fa-search" :class="{ 'animate-spin': isLoading }"></i>
          <span>查询</span>
        </button>
      </div>
    </header>

    <!-- 通用 demo banner -->
    <section v-if="payload?.demo_mode" class="demo-banner">
      <strong>Demo / 数据源未接入</strong>
      <span>
        当前仅估值维度由本地日线计算 (PE/PB/PS 分位); 成长性、盈利能力、现金流、偿债、质量
        需要财务三表数据源, 暂未接入。
      </span>
    </section>

    <!-- 状态: 加载中 -->
    <LoadingState
      v-if="isLoading"
      title="正在拉取财务雷达数据"
      description="本地 DuckDB 查询 + 估值分位计算中"
      :elapsed-ms="elapsedMs"
      :timeout-ms="REQUEST_TIMEOUT_MS"
    />

    <!-- 状态: 超时 (ApiError.code === 'TIMEOUT') -->
    <TimeoutState
      v-else-if="errorKind === 'timeout'"
      :endpoint="`/api/finance/radar?symbol=${currentSymbol}`"
      :elapsed-ms="elapsedMs"
      :query-params="{ symbol: currentSymbol }"
      @retry="onSearch"
    />

    <!-- 状态: 业务错误 (4xx/5xx, 含参数错误 / 数据缺失) -->
    <ErrorState
      v-else-if="errorKind === 'error' && errorMessage"
      :title="errorTitle"
      :message="errorMessage"
      :endpoint="errorEndpoint"
      :status="errorStatus"
      :code="errorCode"
      :retryable="errorRetryable"
      @retry="onSearch"
    />

    <!-- 状态: 网络错误 (fetch reject) -->
    <ErrorState
      v-else-if="errorKind === 'network'"
      title="网络请求失败"
      :message="errorMessage || '请检查后端服务是否启动'"
      :endpoint="`/api/finance/radar?symbol=${currentSymbol}`"
      :status="0"
      code="NETWORK"
      :retryable="true"
      @retry="onSearch"
    />

    <!-- 主内容: 雷达组件 -->
    <section v-else-if="payload" class="radar-section">
      <FinanceRadar
        :symbol="currentSymbol"
        :stock-name="payload.name"
        :data="payload"
      />
    </section>

    <!-- 边界声明 -->
    <p class="boundary">
      本页面仅展示本地结构化财务证据, 不构成买入、卖出、仓位、止盈或止损建议。
    </p>
  </main>
</template>

<script setup lang="ts">
/**
 * FinanceRadarPage - 财务雷达页面
 *
 * 数据流:
 *   user input (symbol)
 *     -> apiService.getFinanceRadar(symbol)
 *       -> server/routes/finance_routes.py GET /api/finance/radar
 *         -> stock_daily.parquet (估值序列) + stock_info.parquet (名称)
 *
 * 错误降级:
 *   - ApiError.code === 'TIMEOUT'           -> TimeoutState
 *   - ApiError.code === 'NETWORK'           -> ErrorState (network)
 *   - HTTP 4xx/5xx (含 FINANCE_RADAR_*)     -> ErrorState (error)
 *   - has_data=false (空响应)                -> 由 FinanceRadar 内部 EmptyState 处理
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiService, ApiError } from '../services/api'
import FinanceRadar from '../components/charts/FinanceRadar.vue'
import DebugButton from '../components/common/DebugButton.vue'
import LoadingState from '../components/common/LoadingState.vue'
import ErrorState from '../components/common/ErrorState.vue'
import TimeoutState from '../components/common/TimeoutState.vue'

interface FinanceRadarPayload {
  symbol: string
  name: string | null
  dimensions: Record<string, {
    value: number | null
    fields: Record<string, {
      value: number | null
      current?: number | null
      sample_size?: number
      source: string
      formula: string
    }>
    trade_date?: string | null
  }>
  has_data: boolean
  demo_mode: boolean
  missing_dimensions: string[]
  present_dimensions: string[]
}

const REQUEST_TIMEOUT_MS = 15000
// 默认查询: 本地有 1 条 demo 持仓 000001.SZ + 估值数据, 用于快速验证
const DEFAULT_SYMBOL = '000001.SZ'

const symbolInput = ref(DEFAULT_SYMBOL)
const currentSymbol = ref(DEFAULT_SYMBOL)
const payload = ref<FinanceRadarPayload | null>(null)
const isLoading = ref(false)
const errorKind = ref<'timeout' | 'error' | 'network' | null>(null)
const errorMessage = ref('')
const errorEndpoint = ref('')
const errorStatus = ref<number | string>('')
const errorCode = ref('')
const errorRetryable = ref(true)
const errorTitle = ref('加载失败')
const elapsedMs = ref(0)

let timer: ReturnType<typeof setInterval> | null = null
let timerStart = 0

const demoMode = computed(() => payload.value?.demo_mode === true)
const dataDate = computed(() => {
  const d = payload.value?.dimensions?.valuation?.trade_date
  return d ? String(d) : ''
})
// 至少 1 个维度缺失即视为 "部分成功"
const partialSuccess = computed(() => {
  if (!payload.value) return false
  return payload.value.missing_dimensions.length > 0 && payload.value.present_dimensions.length > 0
})

onMounted(() => {
  // 默认加载 demo 持仓
  loadRadar(DEFAULT_SYMBOL)
})

onBeforeUnmount(() => {
  stopTimer()
})

function onSearch() {
  const symbol = symbolInput.value.trim()
  if (!symbol) {
    errorKind.value = 'error'
    errorMessage.value = '请输入股票代码'
    errorCode.value = 'FINANCE_RADAR_INVALID_PARAMS'
    errorStatus.value = 400
    errorRetryable.value = false
    errorTitle.value = '参数错误'
    errorEndpoint.value = '/api/finance/radar'
    payload.value = null
    return
  }
  loadRadar(symbol)
}

async function loadRadar(symbol: string) {
  currentSymbol.value = symbol
  resetState()
  isLoading.value = true
  startTimer()

  try {
    const data = await apiService.getFinanceRadar(symbol)
    payload.value = data
    // 成功加载后清理 errorKind
    errorKind.value = null
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.code === 'TIMEOUT') {
        errorKind.value = 'timeout'
        errorMessage.value = err.message
      } else if (err.code === 'NETWORK' || (err.status ?? 0) === 0) {
        errorKind.value = 'network'
        errorMessage.value = err.message
      } else {
        errorKind.value = 'error'
        errorMessage.value = err.message
        errorEndpoint.value = err.endpoint ?? `/api/finance/radar?symbol=${symbol}`
        errorStatus.value = err.status ?? ''
        errorCode.value = err.code ?? ''
        errorRetryable.value = !!err.retryable
        errorTitle.value = err.code?.startsWith('FINANCE_RADAR_')
          ? mapFinanceErrorTitle(err.code)
          : '加载失败'
      }
    } else {
      errorKind.value = 'error'
      errorMessage.value = err instanceof Error ? err.message : '未知错误'
      errorEndpoint.value = `/api/finance/radar?symbol=${symbol}`
      errorTitle.value = '加载失败'
    }
    payload.value = null
  } finally {
    isLoading.value = false
    stopTimer()
  }
}

/** 把业务错误码翻译为中文标题 */
function mapFinanceErrorTitle(code: string): string {
  switch (code) {
    case 'FINANCE_RADAR_INVALID_PARAMS':
      return '参数错误'
    case 'FINANCE_RADAR_DATA_MISSING':
      return '数据缺失'
    case 'FINANCE_INTERNAL_ERROR':
      return '服务器内部错误'
    default:
      return '加载失败'
  }
}

function resetState() {
  errorKind.value = null
  errorMessage.value = ''
  errorEndpoint.value = ''
  errorStatus.value = ''
  errorCode.value = ''
  errorRetryable.value = true
  errorTitle.value = '加载失败'
  elapsedMs.value = 0
}

function startTimer() {
  timerStart = performance.now()
  stopTimer()
  timer = setInterval(() => {
    elapsedMs.value = Math.round(performance.now() - timerStart)
  }, 100)
}

function stopTimer() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}
</script>

<style scoped>
.finance-radar-page {
  min-height: 100vh;
  padding: 24px;
  background: #05070d;
  color: #e2e8f0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.page-title { font-size: 24px; font-weight: 700; margin: 0; color: #f1f5f9; }
.page-subtitle { font-size: 13px; color: #94a3b8; margin: 6px 0 0; }

.search-block { display: flex; align-items: center; gap: 8px; }
.search-input {
  width: 260px;
  padding: 8px 12px;
  border: 1px solid #1e293b;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 8px;
  font-size: 13px;
  font-family: ui-monospace, monospace;
}
.search-input:focus { outline: 2px solid #06b6d4; outline-offset: 1px; }
.search-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 16px;
  border: 0; border-radius: 8px;
  background: #06b6d4; color: #fff; font-weight: 600; font-size: 13px;
  cursor: pointer;
  transition: background 0.12s;
}
.search-btn:hover:not(:disabled) { background: #0891b2; }
.search-btn:disabled { opacity: 0.6; cursor: not-allowed; }

.demo-banner {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 14px;
  border: 1px solid #fde68a;
  background: rgba(254, 243, 199, 0.08);
  color: #fcd34d;
  border-radius: 10px;
  font-size: 13px;
  line-height: 1.5;
}
.demo-banner strong {
  color: #fbbf24; white-space: nowrap;
}

.radar-section {
  background: #0f172a;
  border: 1px solid #1e293b;
  border-radius: 12px;
  padding: 20px;
}

.boundary {
  margin: 8px 0 0; text-align: center;
  font-size: 12px; color: #64748b;
}
</style>
