<template>
  <main class="dashboard">
    <DebugButton />
    <header class="hero card">
      <div>
        <p class="eyebrow">AQUATRADE RESEARCH</p>
        <h1>策略回测仪表盘</h1>
        <div class="meta">
          <span>{{ displayStrategyName }}</span>
          <span>{{ displayDateRange }}</span>
          <span>基准 {{ benchmark }}</span>
          <DataStatusBadge :status="dataHealth?.status" />
        </div>
      </div>
      <div class="actions">
        <button class="secondary" @click="activeTab = 'brief'">AI 复盘</button>
        <button class="primary" :disabled="isBacktestRunning" @click="runBacktest">
          {{ isBacktestRunning ? `运行中 ${progress}%` : '运行回测' }}
        </button>
      </div>
    </header>

    <section v-if="fromWorkbench" class="workbench-source-banner">
      <i class="fas fa-code"></i>
      <span v-if="workbenchLoading">正在从策略工作台加载回测结果...</span>
      <span v-else-if="workbenchError">加载失败：{{ workbenchError }}</span>
      <span v-else-if="hasBacktestData">来自策略工作台的回测结果<span v-if="backtestIdFromQuery"> · 任务 ID: {{ backtestIdFromQuery }}</span></span>
      <span v-else>未找到回测数据，请返回策略工作台重新运行</span>
    </section>

    <section v-if="mockVisible" class="mock-banner">
      当前展示的是 Mock 回测数据，不代表真实策略结果。
    </section>
    <section v-if="matrixCacheMissing" class="cache-warning">
      <span>策略回测性能提示：回测缓存缺失，可点击重建。今日工作台仍可正常使用。</span>
      <button class="link-button" :disabled="cacheRebuilding" @click="rebuildMatrixCache">
        {{ cacheRebuilding ? '重建中' : '重建缓存' }}
      </button>
    </section>

    <section class="filters card">
      <label>开始日期<input v-model="startDate" type="date"></label>
      <label>结束日期<input v-model="endDate" type="date"></label>
      <label>基准<select v-model="benchmark"><option value="000300.SH">沪深300</option><option value="000905.SH">中证500</option></select></label>
      <button class="link-button" @click="refreshWorkbench">刷新数据状态</button>
    </section>

    <ErrorState v-if="pageError" :message="pageError" @retry="refreshWorkbench" />
    <LoadingState v-else-if="pageLoading" title="正在加载研究工作台" />

    <template v-else>
      <section class="kpis">
        <article class="card kpi"><span>累计收益</span><strong>{{ formatPercent(metrics?.totalReturn) }}</strong><small>回测区间累计</small></article>
        <article class="card kpi"><span>最大回撤</span><strong class="risk">{{ formatPercent(metrics?.maxDrawdown) }}</strong><small>峰值至谷值</small></article>
        <article class="card kpi"><span>夏普比率</span><strong>{{ formatScore(metrics?.sharpeRatio) }}</strong><small>风险调整后指标</small></article>
        <article class="card kpi"><span>{{ tradeCount ? '胜率' : '风险状态' }}</span><strong>{{ tradeCount ? formatPercent(metrics?.winRate) : 'N/A' }}</strong><small>{{ tradeCount ? `盈亏比 ${profitRatioText}` : '无交易记录，无法计算' }}</small></article>
      </section>

      <section class="main-grid">
        <article class="card chart-card">
          <div class="section-title"><div><h2>净值曲线 vs 基准</h2><p>主图展示真实回测结果；无结果时不自动回退到 Mock。</p></div><DataStatusBadge :status="hasBacktestData ? 'ok' : 'unknown'" :label="hasBacktestData ? '已有回测结果' : '尚未运行'" /></div>
          <div class="chart-body">
            <EquityCurve v-if="hasBacktestData" :versions="equityVersions" :benchmark="backtestStore.benchmarkEquitySeries" />
            <EmptyState v-else title="尚未运行回测" description="请检查策略与日期后点击“运行回测”" />
          </div>
        </article>

        <aside class="card quant-card">
          <div class="section-title"><div><h2>QuantFlow</h2><p>最近一次五分钟投研流水线</p></div><button class="link-button" :disabled="quantRunning" @click="runQuantFlow">{{ quantRunning ? '运行中' : '立即运行' }}</button></div>
          <LoadingState v-if="quantLoading" title="正在读取流水线" />
          <ErrorState v-else-if="quantError" :message="quantError" @retry="loadQuantFlow" />
          <EmptyState v-else-if="!quantFlow?.run_at" title="尚未运行 QuantFlow" description="点击“立即运行”生成本地研究简报" />
          <template v-else>
            <p class="run-time">最近运行：{{ formatDateTime(quantFlow.run_at) }}</p>
            <ul class="stage-list">
              <li v-for="stage in quantFlow.stages" :key="stage.stage">
                <DataStatusBadge :status="stage.status === 'skipped' ? 'unknown' : stage.status" :label="stageLabel(stage.stage)" />
                <span>{{ stage.summary }}</span>
              </li>
            </ul>
            <p class="brief">{{ quantFlow.final_brief?.summary }}</p>
          </template>
        </aside>
      </section>

      <section class="card authenticity-card">
        <div class="section-title">
          <div>
            <h2>今日真实性摘要</h2>
            <p>只展示 QuantFlow 与真实回测已经返回的字段，缺失项不补零。</p>
          </div>
          <DataStatusBadge :status="quantFlow?.status || 'unknown'" />
        </div>
        <div class="truth-grid">
          <dl class="truth-list">
            <div><dt>今日数据日期</dt><dd>{{ quantFlow?.global_latest_trade_date || 'unavailable' }}</dd></div>
            <div><dt>过期模块</dt><dd>{{ formatList(quantFlow?.stale_modules) }}</dd></div>
            <div><dt>DragonEye 完整度</dt><dd>{{ formatScore(dragonEvidence?.completeness_score) }}</dd></div>
            <div><dt>缺失证据</dt><dd>{{ formatList(dragonEvidence?.missing_parts) }}</dd></div>
            <div><dt>题材流向</dt><dd>{{ dragonThemeFlow }}</dd></div>
          </dl>
          <dl class="truth-list">
            <div><dt>策略扫描状态</dt><dd>{{ signalStage?.status || 'unavailable' }}</dd></div>
            <div><dt>今日强信号</dt><dd>{{ strongSignalLabel }}</dd></div>
            <div><dt>无信号原因</dt><dd>{{ noSignalReason }}</dd></div>
            <div><dt>拒单总数</dt><dd>{{ rejectedOrderTotal ?? 'unavailable' }}</dd></div>
            <div><dt>滑点成本</dt><dd>{{ formatOptionalAmount(slippageCost) }}</dd></div>
          </dl>
        </div>
        <div class="filter-summary">
          <strong>真实性参数</strong>
          <span v-if="filterStatItems.length === 0">unavailable</span>
          <span v-for="item in filterStatItems" :key="item.key">{{ item.label }}：{{ item.value }}</span>
        </div>
      </section>

      <section class="support-grid">
        <article class="card support-card"><h2>月度收益</h2><div class="month-grid" v-if="backtestStore.monthlyReturns.length"><template v-for="row in backtestStore.monthlyReturns" :key="row.year"><span v-for="(value, index) in row.months" :key="`${row.year}-${index}`">{{ value == null ? 'N/A' : formatPercent(value) }}</span></template></div><EmptyState v-else description="暂无月度收益数据" /></article>
        <article class="card support-card"><h2>回撤曲线</h2><div class="support-chart"><DrawdownChart v-if="hasBacktestData" :equity-series="backtestStore.equitySeries" /><EmptyState v-else description="尚未运行回测" /></div></article>
        <article class="card support-card"><h2>风险指标</h2><dl class="risk-list"><div><dt>波动率</dt><dd>{{ formatPercent(metrics?.volatility) }}</dd></div><div><dt>交易次数</dt><dd>{{ tradeCount }}</dd></div><div><dt>盈亏比</dt><dd>{{ profitRatioText }}</dd></div><div><dt>数据状态</dt><dd>{{ dataHealth?.status || 'unknown' }}</dd></div></dl></article>
      </section>

      <section class="card details">
        <nav class="tabs">
          <button v-for="tab in tabs" :key="tab.key" :class="{ active: activeTab === tab.key }" @click="activeTab = tab.key">{{ tab.label }}</button>
        </nav>
        <div class="tab-body">
          <div v-if="activeTab === 'trades'">
            <table v-if="backtestStore.trades.length"><thead><tr><th>日期</th><th>代码</th><th>动作</th><th>价格</th><th>数量</th><th>损益</th></tr></thead><tbody><tr v-for="trade in backtestStore.trades" :key="trade.id"><td>{{ trade.date }}</td><td>{{ trade.symbolCode || trade.symbol }}</td><td>{{ trade.action }}</td><td>{{ formatAmount(trade.price) }}</td><td>{{ trade.quantity || 0 }}</td><td>{{ formatAmount(trade.profitLoss || 0) }}</td></tr></tbody></table>
            <EmptyState v-else title="暂无交易明细" description="无交易记录，胜率和盈亏比无法计算" />
          </div>
          <EmptyState v-else-if="activeTab === 'positions'" title="暂无当前持仓" description="当前回测结果未返回持仓快照" />
          <EmptyState v-else-if="activeTab === 'signals'" title="暂无信号日志" description="策略信号接口未返回本地证据" />
          <dl v-else-if="activeTab === 'metrics'" class="metric-explain"><div><dt>累计收益</dt><dd>回测结束净值相对初始净值的变化。</dd></div><div><dt>最大回撤</dt><dd>回测期间从峰值到后续谷值的最大跌幅。</dd></div><div><dt>胜率与盈亏比</dt><dd>仅在存在交易记录时计算。</dd></div></dl>
          <div v-else-if="activeTab === 'data'"><DataStatusBadge :status="dataHealth?.status" /><ul class="data-list"><li v-for="item in dataHealth?.datasets || []" :key="item.name"><strong>{{ item.name }}</strong><span>最新 {{ item.latest_date || 'N/A' }} · 行数 {{ formatInteger(item.row_count) }} · {{ item.message }}</span></li></ul></div>
          <div v-else><p class="brief">{{ quantFlow?.final_brief?.summary || '暂无本地证据' }}</p></div>
        </div>
      </section>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useBacktestStore } from '../store/backtestStore'
import { useStrategyStore } from '../store/strategyStore'
import { useStreamingBacktest } from '../composables/useStreamingBacktest'
import { request, ApiError, apiService } from '../services/api'
import EquityCurve from '../components/EquityCurve.vue'
import DrawdownChart from '../components/charts/DrawdownChart.vue'
import LoadingState from '../components/common/LoadingState.vue'
import EmptyState from '../components/common/EmptyState.vue'
import ErrorState from '../components/common/ErrorState.vue'
import DataStatusBadge from '../components/common/DataStatusBadge.vue'
import DebugButton from '../components/common/DebugButton.vue'

const route = useRoute()
const backtestStore = useBacktestStore()
const strategyStore = useStrategyStore()
const { start, isRunning: isBacktestRunning, progress } = useStreamingBacktest()
const today = new Date()
const endDate = ref(today.toISOString().slice(0, 10))
const startDate = ref(new Date(today.getFullYear() - 1, today.getMonth(), today.getDate()).toISOString().slice(0, 10))
const benchmark = ref('000300.SH')
const pageLoading = ref(true)
const pageError = ref('')
const quantLoading = ref(false)
const quantRunning = ref(false)
const quantError = ref('')
const cacheRebuilding = ref(false)
const dataHealth = ref<any>(null)
const quantFlow = ref<any>(null)
const mockVisible = ref(false)
const activeTab = ref('trades')

// 来自策略工作台的回测结果标识
const fromWorkbench = computed(() => route.query.from === 'workbench')
const backtestIdFromQuery = computed(() => (route.query.backtest_id as string) || '')
const workbenchLoading = ref(false)
const workbenchError = ref('')
const tabs = [
  { key: 'trades', label: '交易明细' }, { key: 'positions', label: '当前持仓' },
  { key: 'signals', label: '信号日志' }, { key: 'metrics', label: '指标解释' },
  { key: 'data', label: '数据状态' }, { key: 'brief', label: '投研简报' },
]
const metrics = computed(() => backtestStore.metrics)
const tradeCount = computed(() => metrics.value?.tradesCount ?? metrics.value?.totalTrades ?? backtestStore.trades.length)
const hasBacktestData = computed(() => backtestStore.equitySeries.length > 0)

// 当从策略工作台跳转过来时，优先显示 lastRunParams 中的策略名和日期
const displayStrategyName = computed(() => {
  if (fromWorkbench.value && backtestStore.lastRunParams?.strategyName) {
    return backtestStore.lastRunParams.strategyName
  }
  return strategyStore.currentVersion?.name || '未选择策略'
})
const displayDateRange = computed(() => {
  if (fromWorkbench.value && backtestStore.lastRunParams?.startDate) {
    return `${backtestStore.lastRunParams.startDate} 至 ${backtestStore.lastRunParams.endDate}`
  }
  return `${startDate.value} 至 ${endDate.value}`
})
const profitRatioText = computed(() => tradeCount.value > 0 ? formatScore(metrics.value?.profitLossRatio ?? metrics.value?.profitFactor) : 'N/A')
const equityVersions = computed(() => hasBacktestData.value ? [{ versionId: 'current', versionName: strategyStore.currentVersion?.name || '当前策略', data: backtestStore.equitySeries }] : [])
const matrixCacheMissing = computed(() => {
  const cache = dataHealth.value?.datasets?.find((item: any) => item.name === 'matrix_cache')
  return cache?.status === 'backtest_cache_missing'
})
const stageMap = computed<Record<string, any>>(() => Object.fromEntries(
  (quantFlow.value?.stages || []).map((stage: any) => [stage.stage, stage]),
))
const dragonStage = computed(() => stageMap.value.dragon_eye_summary)
const signalStage = computed(() => stageMap.value.strategy_signal_scan)
const dragonEvidence = computed(() => {
  const data = dragonStage.value?.data
  if (!data) return null
  const partial = data.latest_partial_status
  if (partial?.evidence_date === quantFlow.value?.global_latest_trade_date) return partial
  return data
})
const dragonThemeFlow = computed(() => {
  const data = dragonStage.value?.data
  return data?.theme_flow || data?.sentiment?.theme_flow || 'unavailable'
})
const signalData = computed(() => signalStage.value?.data || {})
const strongSignalCount = computed(() => (
  (signalData.value.buy?.length || 0) + (signalData.value.sell?.length || 0)
))
const strongSignalLabel = computed(() => {
  if (!signalStage.value || ['error', 'skipped'].includes(signalStage.value.status)) return 'unavailable'
  return strongSignalCount.value > 0 ? `有信号（${strongSignalCount.value} 条）` : '无信号'
})
const noSignalReason = computed(() => {
  if (strongSignalCount.value > 0) return '不适用'
  if (!signalStage.value) return 'unavailable'
  return signalData.value.blocked_reason
    || signalData.value.diagnosis?.top_blocked_reasons?.[0]?.reason
    || signalStage.value.reason
    || 'unavailable'
})
const rejectedOrders = computed(() => backtestStore.metrics?.rejectedOrders)
const rejectedOrderTotal = computed<number | null>(() => {
  const data = rejectedOrders.value
  if (!data) return null
  if (Number.isFinite(data.total)) return data.total
  return Object.entries(data)
    .filter(([key, value]) => key !== 'total' && Number.isFinite(value))
    .reduce((sum, [, value]) => sum + Number(value), 0)
})
const slippageCost = computed(() => backtestStore.metrics?.slippageCost)
const filterStatItems = computed(() => {
  const stats = backtestStore.metrics?.filterStats
  if (!stats) return []
  const labels: Record<string, string> = {
    slippageRate: '滑点率',
    excludeSt: '过滤 ST',
    volumeCapRatio: '成交量上限',
    minTradeAmount: '最小成交额',
  }
  return Object.entries(stats).map(([key, value]) => ({
    key,
    label: labels[key] || key,
    value: key.toLowerCase().includes('rate') || key.toLowerCase().includes('ratio')
      ? formatPercent(typeof value === 'number' ? value : null)
      : typeof value === 'boolean' ? (value ? '是' : '否') : String(value ?? 'unavailable'),
  }))
})

function formatPercent(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return 'N/A'
  const normalized = Math.abs(value) <= 1 ? value * 100 : value
  return `${normalized.toFixed(2)}%`
}
function formatScore(value?: number | null) { return value == null || !Number.isFinite(value) ? 'N/A' : value.toFixed(4) }
function formatAmount(value: number) { const abs = Math.abs(value); if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`; if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`; return value.toFixed(2) }
function formatOptionalAmount(value?: number | null) { return value == null || !Number.isFinite(value) ? 'unavailable' : formatAmount(value) }
function formatInteger(value: number | null) { return value == null ? 'N/A' : value.toLocaleString('zh-CN') }
function formatDateTime(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function formatList(value?: unknown[]) {
  if (!Array.isArray(value)) return 'unavailable'
  return value.length ? value.join('、') : '无'
}
function stageLabel(stage: string) { return ({ data_health_check: '数据健康', market_regime_detect: '市场状态', dragon_eye_summary: '市场雷达', stock_screener_candidates: '研究候选', strategy_signal_scan: '策略信号', portfolio_risk_check: '持仓风险', final_research_brief: '最终简报' } as Record<string, string>)[stage] || stage }

async function loadStrategies() {
  const res = await request<any[]>('/strategies')
  if (!res.ok) throw new ApiError(res.message, res.status, res.code, { retryable: res.retryable, endpoint: res.endpoint, detail: res.detail })
  const data = res.data
  const versions = (Array.isArray(data) ? data : []).map((item: any) => ({ id: item.id, name: item.name, version: item.version || 'local', createdAt: item.createdAt || new Date().toISOString(), description: item.description || '' }))
  strategyStore.setAvailableVersions(versions)
  if (!strategyStore.currentVersionId && versions.length) strategyStore.setCurrentVersion(versions[0].id)
}
async function loadDataHealth() { 
  const res = await request<any>('/data/health')
  if (!res.ok) throw new ApiError(res.message, res.status, res.code, { retryable: res.retryable, endpoint: res.endpoint, detail: res.detail })
  dataHealth.value = res.data 
}
async function rebuildMatrixCache() {
  cacheRebuilding.value = true
  pageError.value = ''
  try {
    const res = await request<any>('/data/matrix-cache/rebuild', { method: 'POST', body: JSON.stringify({ start_date: startDate.value, end_date: endDate.value }) })
    if (!res.ok) throw new ApiError(res.message, res.status, res.code, { retryable: res.retryable, endpoint: res.endpoint, detail: res.detail })
    await loadDataHealth()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '回测缓存重建失败'
  } finally {
    cacheRebuilding.value = false
  }
}
async function loadQuantFlow() {
  quantLoading.value = true; quantError.value = ''
  try { 
    const res = await request<any>('/quant-flow/latest')
    if (!res.ok) throw new ApiError(res.message, res.status, res.code, { retryable: res.retryable, endpoint: res.endpoint, detail: res.detail })
    quantFlow.value = res.data 
  } catch (error) { quantError.value = error instanceof Error ? error.message : 'QuantFlow 加载失败' } finally { quantLoading.value = false }
}
async function refreshWorkbench() {
  pageLoading.value = true; pageError.value = ''
  try { await Promise.all([loadStrategies(), loadDataHealth(), loadQuantFlow()]); backtestStore.hydrateFromStorage() } catch (error) { pageError.value = error instanceof Error ? error.message : '工作台加载失败' } finally { pageLoading.value = false }
}

/**
 * 当用户从策略工作台跳转过来且 backtestStore 为空时，
 * 尝试通过 backtest_id 从后端拉取回测结果。
 * 后端 GET /api/backtest/jobs/<id> 返回 stats 和 equity_curve，
 * 但不返回 trades 列表（交易明细仅在 Socket.IO 完成事件中推送）。
 */
async function fetchBacktestById(jobId: string) {
  workbenchLoading.value = true; workbenchError.value = ''
  try {
    const job = await apiService.getBacktestJob(jobId)
    if (job.status === 'success') {
      if (job.equity_curve && job.equity_curve.length) {
        backtestStore.addEquityPoints(job.equity_curve.map(p => ({ date: p.date, equity: p.equity })))
      }
      if (job.stats) {
        const s = job.stats
        backtestStore.setMetrics({
          totalReturn: s.total_return ?? s.totalReturn ?? 0,
          annualizedReturn: s.annual_return ?? s.annualReturn ?? 0,
          maxDrawdown: s.max_drawdown ?? s.maxDrawdown ?? 0,
          sharpeRatio: s.sharpe_ratio ?? s.sharpeRatio ?? 0,
          volatility: s.volatility ?? 0,
          winRate: s.win_rate ?? s.winRate ?? 0,
          profitLossRatio: s.profit_loss_ratio ?? s.profitLossRatio ?? 0,
          tradesCount: s.total_trades ?? s.totalTrades ?? 0,
        } as any)
      }
      backtestStore.persistToStorage()
    } else if (job.status === 'failed') {
      workbenchError.value = job.message || '回测任务失败'
    }
  } catch (error) {
    workbenchError.value = error instanceof Error ? error.message : '拉取回测结果失败'
  } finally {
    workbenchLoading.value = false
  }
}
async function loadMockBacktest() {
  const { generateMockBacktestData } = await import('../api/mockSocketIO')
  const mock = generateMockBacktestData(strategyStore.currentVersion?.name || 'MockStrategy')
  const full = mock.getFullData(); const result = mock.getFinalMetrics()
  backtestStore.clearBacktestData()
  backtestStore.addEquityPoints(full.equityCurve)
  backtestStore.addBenchmarkPoints(full.benchmark)
  backtestStore.setTrades(full.trades)
  backtestStore.setMetrics({ totalReturn: result.totalReturn, annualizedReturn: result.annualReturn, maxDrawdown: result.maxDrawdown, sharpeRatio: result.sharpeRatio, volatility: result.volatility, winRate: result.winRate, profitFactor: result.profitLossRatio, profitLossRatio: result.profitLossRatio, tradesCount: result.totalTrades })
  backtestStore.setMonthlyReturns(mock.getMonthlyReturns())
  mockVisible.value = true
}
function runBacktest() {
  pageError.value = ''; mockVisible.value = false
  if (!strategyStore.currentVersion) { pageError.value = '暂无可运行策略'; return }
  if (import.meta.env.DEV && import.meta.env.VITE_USE_MOCK === 'true') {
    void loadMockBacktest().catch((error) => {
      pageError.value = error instanceof Error ? error.message : '加载 Mock 回测失败'
    })
    return
  }
  start({ strategy_name: strategyStore.currentVersion.name, start_date: startDate.value, end_date: endDate.value, benchmark_code: benchmark.value, initial_capital: 1000000, commission: 0.0003, slippage: 0.001 }, {
    onError: (error) => { pageError.value = error.message },
  })
}
async function runQuantFlow() {
  quantRunning.value = true; quantError.value = ''
  try { 
    const res = await request<any>('/quant-flow/run', { method: 'POST', body: '{}' })
    if (!res.ok) throw new ApiError(res.message, res.status, res.code, { retryable: res.retryable, endpoint: res.endpoint, detail: res.detail })
    quantFlow.value = res.data
    await loadDataHealth() 
  } catch (error) { quantError.value = error instanceof Error ? error.message : 'QuantFlow 运行失败' } finally { quantRunning.value = false }
}
onMounted(async () => {
  await refreshWorkbench()
  // 如果从策略工作台跳转过来，且 backtestStore 为空但有 backtest_id，
  // 尝试从后端拉取回测结果
  if (fromWorkbench.value && backtestIdFromQuery.value && !backtestStore.hasData) {
    await fetchBacktestById(backtestIdFromQuery.value)
  }
})
</script>

<style scoped>
.dashboard { min-height: 100%; padding: 22px; color: #172033; background: #f3f1eb; overflow: auto; }
.card { background: rgba(255,255,255,.92); border: 1px solid #e6e2d8; border-radius: 16px; box-shadow: 0 8px 24px rgba(51,65,85,.06); }
.hero { display: flex; justify-content: space-between; gap: 24px; padding: 24px 26px; }
.eyebrow { margin: 0 0 6px; color: #4774b8; font-size: 11px; font-weight: 800; letter-spacing: .16em; }
h1,h2,p { margin-top: 0; } h1 { margin-bottom: 10px; font-size: 28px; } h2 { margin-bottom: 5px; font-size: 16px; }
.meta,.actions,.filters { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }.meta { color: #64748b; font-size: 13px; }
button { border: 0; border-radius: 10px; padding: 10px 16px; font-weight: 700; cursor: pointer; } button:disabled { opacity: .55; cursor: default; }
.primary { color: white; background: #3569ad; }.secondary { color: #315f98; background: #e8f0fa; }.link-button { color: #315f98; background: #edf3fa; padding: 8px 12px; }
.filters { margin-top: 14px; padding: 14px 18px; }.filters label { display: grid; gap: 4px; color: #64748b; font-size: 12px; }.filters input,.filters select { min-width: 150px; padding: 8px 10px; border: 1px solid #d9dee7; border-radius: 8px; color: #334155; background: white; }
.mock-banner { margin-top: 14px; padding: 12px 16px; border: 1px solid #f4c86d; border-radius: 12px; color: #7c4b00; background: #fff7df; font-weight: 700; }
.workbench-source-banner { display: flex; align-items: center; gap: 10px; margin-top: 14px; padding: 12px 16px; border: 1px solid #3569ad; border-radius: 12px; color: #1e3a5f; background: #e8f0fa; font-weight: 600; font-size: 13px; }
.workbench-source-banner i { color: #3569ad; font-size: 16px; }
.cache-warning { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 14px; padding: 12px 16px; border: 1px solid #f4c86d; border-radius: 12px; color: #7c4b00; background: #fff7df; font-weight: 700; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-top: 16px; }.kpi { padding: 18px; }.kpi span,.kpi small { display: block; color: #7b8799; }.kpi strong { display: block; margin: 8px 0 4px; font-size: 25px; color: #244e83; }.kpi strong.risk { color: #b45353; }
.main-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(300px, .8fr); gap: 14px; margin-top: 14px; }.chart-card,.quant-card,.support-card,.details { padding: 18px; }.section-title { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.section-title p { color: #8a94a4; font-size: 12px; }.chart-body { height: 430px; }.run-time,.brief { color: #64748b; font-size: 13px; line-height: 1.7; }.stage-list { display: grid; gap: 9px; padding: 0; list-style: none; }.stage-list li { display: grid; grid-template-columns: auto 1fr; align-items: center; gap: 8px; color: #526174; font-size: 12px; }
.authenticity-card { margin-top: 14px; padding: 18px; }.truth-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px; }.truth-list { margin: 0; }.truth-list div { display: grid; grid-template-columns: 120px 1fr; gap: 12px; padding: 9px 0; border-bottom: 1px solid #edf0f4; }.truth-list dt { color: #64748b; }.truth-list dd { margin: 0; color: #27364a; font-weight: 700; overflow-wrap: anywhere; }.filter-summary { display: flex; flex-wrap: wrap; gap: 10px 18px; margin-top: 14px; padding: 12px; border-radius: 10px; color: #526174; background: #f7f8fa; font-size: 12px; }.filter-summary strong { color: #27364a; }
.support-grid { display: grid; grid-template-columns: 1.05fr 1.4fr .8fr; gap: 14px; margin-top: 14px; }.support-chart { height: 210px; }.month-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }.month-grid span { padding: 8px; border-radius: 8px; text-align: center; color: #475569; background: #f1f5f9; font-size: 12px; }.risk-list,.metric-explain { display: grid; gap: 0; }.risk-list div,.metric-explain div { display: flex; justify-content: space-between; gap: 16px; padding: 11px 0; border-bottom: 1px solid #edf0f4; }.risk-list dt,.metric-explain dt { color: #64748b; }.risk-list dd,.metric-explain dd { margin: 0; font-weight: 700; }
.details { margin-top: 14px; }.tabs { display: flex; gap: 6px; border-bottom: 1px solid #e5e7eb; }.tabs button { border-radius: 8px 8px 0 0; color: #64748b; background: transparent; }.tabs button.active { color: #24578f; background: #eaf1f9; }.tab-body { min-height: 190px; padding-top: 16px; } table { width: 100%; border-collapse: collapse; } th,td { padding: 10px; border-bottom: 1px solid #edf0f4; text-align: left; font-size: 12px; }.data-list { display: grid; gap: 8px; padding: 0; list-style: none; }.data-list li { display: flex; justify-content: space-between; gap: 14px; padding: 10px 0; border-bottom: 1px solid #edf0f4; color: #64748b; }.data-list strong { color: #334155; }
@media (max-width: 1050px) { .kpis,.support-grid { grid-template-columns: repeat(2, 1fr); }.main-grid { grid-template-columns: 1fr; } }
@media (max-width: 680px) { .dashboard { padding: 12px; }.hero { flex-direction: column; }.kpis,.support-grid,.truth-grid { grid-template-columns: 1fr; }.chart-body { height: 320px; } }
</style>
