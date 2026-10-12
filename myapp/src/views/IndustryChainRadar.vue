<template>
  <main class="industry-chain-page">
    <!-- T3.4: 吸顶 DebugButton (dataDate / demoMode / stale / partialSuccess) -->
    <DebugButton
      :data-date="dataDate"
      :demo-mode="demoMode"
      :stale="stale"
      :partial-success="partialSuccess"
    />

    <IndustryHeader
      :chain="currentChain"
      :summary="summary"
      :loading="loading"
      :using-fallback="usingFallback"
    />

    <DataSourceStatusBar
      :status="dataSourceStatus"
      :loading="statusLoading"
      :structure-loaded="Boolean(graphData?.nodes?.length)"
      :using-fallback="usingFallback"
    />

    <IndustryToolbar
      v-model:keyword="searchKeyword"
      :chains="chains"
      :current-chain-id="currentChainId"
      :message="searchMessage"
      @select-chain="selectChain"
      @search="runSearch"
    />

    <!-- T3.4: 过期 / 兜底 角标 -->
    <div v-if="statusBadges.length" class="status-badges">
      <span
        v-for="badge in statusBadges"
        :key="badge.key"
        class="status-badge"
        :class="`status-badge--${badge.tone}`"
        :title="badge.tooltip"
      >
        <i v-if="badge.icon" :class="badge.icon" aria-hidden="true"></i>
        {{ badge.text }}
      </span>
    </div>

    <!-- T3.4: 加载 / 超时 / 错误 状态展示 -->
    <LoadingState
      v-if="loading"
      title="正在加载题材支持网"
      description="按优先级拉取 DragonEye / 涨停题材 / stock_info / 本地证据 / LanceDB"
      :elapsed-ms="loadingElapsed"
      :timeout-ms="TIMEOUT_MS"
    />
    <TimeoutState
      v-else-if="timedOut"
      :endpoint="`/api/industry-chain/graph?chain_id=${currentChainId}`"
      :elapsed-ms="loadingElapsed"
      :query-params="{ data_date: dataDate, theme: currentChainId }"
      @retry="reloadCurrent"
    />
    <ErrorState
      v-else-if="apiError"
      :title="apiError.title"
      :message="apiError.message"
      :detail="apiError.detail"
      :endpoint="apiError.endpoint"
      :status="apiError.status"
      :code="apiError.code"
      :retryable="apiError.retryable"
      @retry="reloadCurrent"
    />
    <!-- T3.4: 数据完全缺失 → EmptyState, 提示最近可用日期 -->
    <EmptyState
      v-else-if="!graphData?.nodes?.length"
      title="今日题材流缺失，可查看最近可用日期"
      :description="emptyDescription"
      :hint="emptyHint"
    />

    <div v-show="!loading && !timedOut && !apiError && graphData?.nodes?.length" class="main-layout">
      <section class="canvas-area">
        <IndustryChainCanvas
          :graph-data="graphData"
          :loading="loading"
          :selected-node-id="selectedNode?.id || ''"
          @select-node="selectNode"
        />
        <IndustryLayerLegend :layers="graphData?.layers || []" />
        <p v-if="usingFallback" class="state-message">
          当前展示示例数据 / 非真实证据，仅用于开发联调。
        </p>
        <!-- T3.4: 时间轴 slider，可切换 data_date -->
        <div class="time-slider">
          <div class="time-slider__caption">
            <span>数据日期：{{ dataDate || '—' }}</span>
            <span v-if="dataDateOptions.length" class="time-slider__range">
              {{ dataDateOptions[0] }} ~ {{ dataDateOptions[dataDateOptions.length - 1] }}
            </span>
          </div>
          <input
            v-if="dataDateOptions.length > 1"
            type="range"
            class="time-slider__input"
            :min="0"
            :max="dataDateOptions.length - 1"
            :value="dataDateIndex"
            @input="onSliderInput($event)"
          />
          <div v-else class="time-slider__single">{{ dataDate || '暂无历史日期' }}</div>
        </div>
      </section>

      <!-- T3.4: 右侧抽屉（点击节点展开） -->
      <IndustryNodePanel
        :node="selectedNode"
        :node-detail="nodeDetail"
        :loading="nodeLoading"
        class="drawer-panel"
        :class="{ 'is-open': drawerOpen }"
      />
    </div>

    <div v-show="!loading && !timedOut && !apiError && graphData?.nodes?.length">
      <IndustryStockTable
        :stocks="nodeStocks"
        :message="stockMessage"
        :loading="stockLoading"
        :include-candidates="includeCandidates"
        :verified-only="verifiedOnly"
        :sort-by="sortBy"
        @update:include-candidates="includeCandidates = $event"
        @update:verified-only="verifiedOnly = $event"
        @update:sort-by="sortBy = $event; loadNodeStocks()"
      />
    </div>

    <ResearchControlPanel />

    <p class="boundary">本页仅展示本地维护的产业链结构、证据和市场统计，不构成买入、卖出、仓位、止盈或止损建议。</p>
  </main>
</template>

<script setup lang="ts">
/**
 * IndustryChainRadar - 题材支持网（T3.4 动态化）
 *
 * 主要改造点（对应 spec browser-runtime-green T3.4）:
 *  1. 数据源优先级: DragonEye theme_flow > 涨停题材 > stock_info 行业 >
 *     本地 evidence > LanceDB 派生。命中后端已统一在 /graph 端点处理, 本页按
 *     GraphMeta.missing_sources / source / demo_mode 透传。
 *  2. 节点 / 边字段遵循新规范 (ChainNode / ChainEdge), 由 IndustryChainCanvas 渲染。
 *  3. 时间轴: 底部 slider, 切换 data_date 并自动重新拉取。
 *  4. stale warning: data_date 与今天相差 > 1 个交易日即显示黄色角标,
 *     并透传到 DebugButton → ApiDebugPanel。
 *  5. EmptyState: 数据完全缺失时显示"今日题材流缺失，可查看最近可用日期"。
 *  6. 抽屉: 点击节点 → 右侧 IndustryNodePanel 抽屉显示节点详情 + 相关股票 +
 *     证据 (来自 getNodeStocks / EvidenceService)。
 *  7. LoadingState / TimeoutState / ErrorState 接入, 沿用统一 API Client。
 *  8. 防御性: 字段缺失一律降级, demo_mode 走 FALLBACK_GRAPH 仅在 DEV / MOCK 下启用。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { ApiError } from '@/services/api';
import type {
  ChainEdge,
  ChainGraphData,
  ChainInfo,
  ChainNode,
  ChainStock,
  DataSourceStatus,
  GraphMeta,
  GraphResponse,
  NodeDetail,
} from '@/api/industryChain';
import {
  getChainGraph,
  getDataSourcesStatus,
  getNodeDetail,
  getNodeStocks,
  isStale,
  listChains,
} from '@/api/industryChain';
import {
  createFallbackNodeDetail,
  FALLBACK_CHAIN,
  FALLBACK_GRAPH,
} from '@/api/industryChainFallback';
import IndustryChainCanvas from '@/components/industry/IndustryChainCanvas.vue';
import IndustryHeader from '@/components/industry/IndustryHeader.vue';
import IndustryLayerLegend from '@/components/industry/IndustryLayerLegend.vue';
import IndustryNodePanel from '@/components/industry/IndustryNodePanel.vue';
import IndustryStockTable from '@/components/industry/IndustryStockTable.vue';
import IndustryToolbar from '@/components/industry/IndustryToolbar.vue';
import DataSourceStatusBar from '@/components/industry/DataSourceStatusBar.vue';
import DebugButton from '@/components/common/DebugButton.vue';
import EmptyState from '@/components/common/EmptyState.vue';
import ErrorState from '@/components/common/ErrorState.vue';
import LoadingState from '@/components/common/LoadingState.vue';
import TimeoutState from '@/components/common/TimeoutState.vue';
import ResearchControlPanel from '@/components/research/ResearchControlPanel.vue';

const DEFAULT_CHAIN_ID = 'optical_communication';
const DEFAULT_THEME = ''; // 空主题走 DragonEye 优先级链
const EMPTY_STOCK_MESSAGE = '暂无本地证据 / 自动调度尚未生成相关股票';
const TIMEOUT_MS = 15000;
const TIME_SLIDER_DAYS = 14;

const fallbackAllowed = import.meta.env.DEV || import.meta.env.VITE_USE_MOCK === 'true';
const DEFAULT_NODE_IDS = ['optical_module', 'optical_communication'];

interface ApiErrorView {
  title: string;
  message: string;
  detail: string;
  endpoint: string;
  status: number | string;
  code: string;
  retryable: boolean;
}

const chains = ref<ChainInfo[]>([]);
const currentChainId = ref(DEFAULT_CHAIN_ID);
const currentChain = ref<ChainInfo | null>(fallbackAllowed ? FALLBACK_CHAIN : null);
const graphData = ref<ChainGraphData | null>(null);
const summary = ref<ChainGraphData['summary'] | null>(null);
const selectedNode = ref<ChainNode | null>(null);
const nodeDetail = ref<NodeDetail | null>(null);
const nodeStocks = ref<ChainStock[]>([]);
const stockMessage = ref(EMPTY_STOCK_MESSAGE);
const loading = ref(false);
const timedOut = ref(false);
const loadingElapsed = ref(0);
const loadingStartedAt = ref(0);
const apiError = ref<ApiErrorView | null>(null);
const nodeLoading = ref(false);
const stockLoading = ref(false);
const searchKeyword = ref('');
const searchMessage = ref('');
const usingFallback = ref(false);
const drawerOpen = ref(false);

const dataSourceStatus = ref<DataSourceStatus | null>(null);
const statusLoading = ref(false);

const includeCandidates = ref(true);
const verifiedOnly = ref(false);
const sortBy = ref('system_relevance_score');

// T3.4 meta / 时间轴
const meta = ref<GraphMeta | null>(null);
const dataDate = ref<string>('');
const dataDateOptions = ref<string[]>([]);
const dataDateIndex = ref(0);
let loadingTimer: ReturnType<typeof setInterval> | null = null;

const activeSource = computed(() => meta.value?.source || graphData.value?.source || '');
const demoMode = computed(() => Boolean(meta.value?.demo_mode ?? graphData.value?.demo_mode ?? usingFallback.value));
const partialSuccess = computed(() => Boolean(meta.value?.partial_success));
const stale = computed(() => Boolean(meta.value?.stale) || isStale(dataDate.value));
const missingSources = computed(() => meta.value?.missing_sources || graphData.value?.missing_sources || []);

const statusBadges = computed(() => {
  const out: { key: string; text: string; tone: string; icon?: string; tooltip?: string }[] = [];
  if (stale.value) {
    out.push({
      key: 'stale',
      text: `数据过期 (${dataDate.value || '未知'})`,
      tone: 'warn',
      icon: 'fas fa-exclamation-triangle',
      tooltip: 'data_date 与当前日期相差 > 1 个交易日',
    });
  }
  if (demoMode.value) {
    out.push({
      key: 'demo',
      text: 'demo_mode',
      tone: 'info',
      icon: 'fas fa-flask',
      tooltip: '当前数据来自内置结构 / 兜底, 字段可能不完整',
    });
  }
  if (partialSuccess.value) {
    out.push({
      key: 'partial',
      text: '部分字段缺失',
      tone: 'warn',
      icon: 'fas fa-adjust',
      tooltip: missingSources.value.length
        ? `缺失数据源: ${missingSources.value.join(', ')}`
        : '部分字段缺失',
    });
  }
  if (activeSource.value) {
    out.push({
      key: 'source',
      text: `数据源: ${activeSource.value}`,
      tone: 'neutral',
      icon: 'fas fa-database',
      tooltip: '实际命中的数据源',
    });
  }
  return out;
});

const emptyDescription = computed(() => {
  if (dataDate.value) {
    return `${dataDate.value} 当日题材流缺失`;
  }
  return '今日题材流缺失';
});

const emptyHint = computed(() => {
  if (dataDateOptions.value.length) {
    return `最近可用日期: ${dataDateOptions.value[dataDateOptions.value.length - 1]}`;
  }
  return '建议运行 python tools/update_industry_data_daily.py --all 触发数据更新';
});

onMounted(async () => {
  buildDateOptions();
  await loadChains();
  await reloadCurrent();
  loadDataSourceStatus();
});

onUnmounted(() => {
  stopLoadingTimer();
});

/**
 * 构造最近 N 个交易日的候选日期 (粗略按自然日, 周六周日保留)。
 */
function buildDateOptions() {
  const options: string[] = [];
  const today = new Date();
  for (let i = 0; i < TIME_SLIDER_DAYS; i += 1) {
    const d = new Date(today.getTime() - i * 24 * 60 * 60 * 1000);
    if (d.getDay() === 0 || d.getDay() === 6) continue; // 跳过周末
    options.push(d.toISOString().slice(0, 10));
  }
  dataDateOptions.value = options;
  dataDateIndex.value = 0;
  dataDate.value = options[0] || '';
}

function startLoadingTimer() {
  loadingStartedAt.value = Date.now();
  loadingElapsed.value = 0;
  timedOut.value = false;
  stopLoadingTimer();
  loadingTimer = setInterval(() => {
    loadingElapsed.value = Date.now() - loadingStartedAt.value;
    if (loadingElapsed.value >= TIMEOUT_MS) {
      timedOut.value = true;
      stopLoadingTimer();
    }
  }, 250);
}

function stopLoadingTimer() {
  if (loadingTimer) {
    clearInterval(loadingTimer);
    loadingTimer = null;
  }
}

async function loadChains() {
  try {
    const remoteChains = await listChains();
    chains.value = ensureFallbackChain(remoteChains);
  } catch (err) {
    console.error('加载产业链列表失败:', err);
    chains.value = fallbackAllowed ? [FALLBACK_CHAIN] : [];
  }
}

function ensureFallbackChain(remoteChains: ChainInfo[]): ChainInfo[] {
  if (!fallbackAllowed) return remoteChains;
  if (!remoteChains.length) return [FALLBACK_CHAIN];
  if (remoteChains.some((item) => item.chain_id === DEFAULT_CHAIN_ID)) return remoteChains;
  return [FALLBACK_CHAIN, ...remoteChains];
}

async function loadDataSourceStatus() {
  statusLoading.value = true;
  try {
    dataSourceStatus.value = await getDataSourcesStatus();
  } catch (err) {
    dataSourceStatus.value = null;
  } finally {
    statusLoading.value = false;
  }
}

/**
 * T3.4: 重新加载当前 chain / theme / data_date 的图谱
 */
async function reloadCurrent() {
  await selectChain(currentChainId.value, true);
}

async function selectChain(chainId: string, keepDate = false) {
  currentChainId.value = chainId || DEFAULT_CHAIN_ID;
  currentChain.value =
    chains.value.find((c) => c.chain_id === currentChainId.value) ||
    (currentChainId.value === DEFAULT_CHAIN_ID ? FALLBACK_CHAIN : null);
  selectedNode.value = null;
  nodeDetail.value = null;
  nodeStocks.value = [];
  nodeLoading.value = false;
  stockLoading.value = false;
  stockMessage.value = EMPTY_STOCK_MESSAGE;
  searchMessage.value = '';
  usingFallback.value = false;
  apiError.value = null;
  timedOut.value = false;
  drawerOpen.value = false;

  if (!keepDate) {
    dataDateIndex.value = 0;
    dataDate.value = dataDateOptions.value[0] || '';
  }

  loading.value = true;
  startLoadingTimer();
  try {
    const resp = await getChainGraph({
      chainId: currentChainId.value === DEFAULT_CHAIN_ID ? undefined : currentChainId.value,
      theme: DEFAULT_THEME || undefined,
      dataDate: dataDate.value || undefined,
    });
    stopLoadingTimer();
    applyGraphResponse(resp);
  } catch (err) {
    stopLoadingTimer();
    handleGraphError(err);
  } finally {
    loading.value = false;
  }
}

function applyGraphResponse(resp: GraphResponse) {
  if (!resp.success || !resp.data) {
    handleGraphError(new ApiError(resp.error?.message || '图谱加载失败', 500, resp.error?.code, {
      retryable: Boolean(resp.error?.retryable),
      endpoint: '/api/industry-chain/graph',
    }));
    return;
  }
  meta.value = resp.meta;
  if (resp.data.data_date) {
    dataDate.value = resp.data.data_date.slice(0, 10);
  }
  const nodes = (resp.data.nodes || []) as ChainNode[];
  const links = (resp.data.links || []) as ChainEdge[];
  graphData.value = {
    nodes,
    edges: links,
    layers: resp.data.layers || [],
    summary: resp.data.summary || {
      chain_id: currentChainId.value,
      chain_name: currentChainId.value,
      hot_score: 0,
      market_strength: '暂无',
      top_node: '',
      top_node_name: '',
      limit_up_count: 0,
      turnover_change: 0,
      node_count: nodes.length,
      stock_count: nodes.filter((n) => n.type === 'stock').length,
    },
    source: resp.data.source,
    is_fallback: Boolean(resp.data.demo_mode),
    data_date: resp.data.data_date,
    has_data: resp.data.has_data,
    demo_mode: resp.data.demo_mode,
    missing_sources: resp.data.missing_sources || [],
  };
  summary.value = graphData.value.summary;
  usingFallback.value = Boolean(resp.data.demo_mode);

  if (!nodes.length) {
    selectedNode.value = null;
    nodeDetail.value = null;
    return;
  }
  const defaultNode =
    DEFAULT_NODE_IDS.map((id) => nodes.find((node) => node.id === id)).find(Boolean) ||
    nodes[0] ||
    null;
  if (defaultNode) {
    selectNode(defaultNode);
  }
}

function handleGraphError(err: unknown) {
  if (err instanceof ApiError) {
    if (err.code === 'TIMEOUT') {
      timedOut.value = true;
      return;
    }
    apiError.value = {
      title: '产业链图谱加载失败',
      message: err.message,
      detail: typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail || {}),
      endpoint: err.endpoint || '/api/industry-chain/graph',
      status: err.status ?? '',
      code: err.code || '',
      retryable: Boolean(err.retryable),
    };
  } else {
    // 处理 AxiosError：从 response 中提取后端结构化错误信息
    const axiosErr = err as any;
    const resp = axiosErr?.response;
    const respData = resp?.data;
    const errorObj = respData?.error;
    apiError.value = {
      title: '产业链图谱加载失败',
      message: errorObj?.message || (err instanceof Error ? err.message : '未知错误'),
      detail: '',
      endpoint: '/api/industry-chain/graph',
      status: resp?.status ?? '',
      code: errorObj?.code || '',
      retryable: Boolean(errorObj?.retryable ?? false),
    };
  }
  graphData.value = null;
  summary.value = null;
  selectedNode.value = null;
  nodeDetail.value = null;
  nodeStocks.value = [];
  if (fallbackAllowed && currentChainId.value === DEFAULT_CHAIN_ID) {
    // 静默降级到内置结构
    currentChain.value = FALLBACK_CHAIN;
    applyGraphData(FALLBACK_GRAPH, true);
    apiError.value = null;
  }
}

function applyGraphData(data: ChainGraphData, fallback: boolean) {
  graphData.value = data;
  summary.value = data.summary;
  usingFallback.value = fallback;
  if (data.data_date) dataDate.value = data.data_date.slice(0, 10);
  const defaultNode =
    DEFAULT_NODE_IDS.map((id) => data.nodes.find((node) => node.id === id)).find(Boolean) ||
    data.nodes[0] ||
    null;
  if (defaultNode) {
    selectNode(defaultNode);
  }
}

async function selectNode(node: ChainNode) {
  selectedNode.value = node;
  nodeDetail.value = null;
  nodeStocks.value = [];
  stockMessage.value = EMPTY_STOCK_MESSAGE;
  drawerOpen.value = true;

  if (!currentChainId.value || !node.id || !graphData.value) return;

  if (usingFallback.value) {
    nodeDetail.value = createFallbackNodeDetail(graphData.value, node);
    nodeLoading.value = false;
    stockLoading.value = false;
    return;
  }

  nodeLoading.value = true;
  try {
    nodeDetail.value = await getNodeDetail(currentChainId.value, node.id);
  } catch (err) {
    console.error('加载节点详情失败:', err);
    if (graphData.value) {
      nodeDetail.value = createFallbackNodeDetail(graphData.value, node);
    }
  } finally {
    nodeLoading.value = false;
  }

  await loadNodeStocks();
}

async function loadNodeStocks() {
  if (!currentChainId.value || !selectedNode.value?.id) return;
  if (usingFallback.value) {
    nodeStocks.value = [];
    stockMessage.value = EMPTY_STOCK_MESSAGE;
    return;
  }

  stockLoading.value = true;
  try {
    const result = await getNodeStocks(currentChainId.value, selectedNode.value.id, {
      include_candidates: includeCandidates.value,
      verified_only: verifiedOnly.value,
      sort_by: sortBy.value,
    });
    nodeStocks.value = result.rows || [];
    stockMessage.value = nodeStocks.value.length ? '' : (result.message || EMPTY_STOCK_MESSAGE);
  } catch (err) {
    console.error('加载相关公司失败:', err);
    nodeStocks.value = [];
    stockMessage.value = EMPTY_STOCK_MESSAGE;
  } finally {
    stockLoading.value = false;
  }
}

watch([includeCandidates, verifiedOnly], () => {
  loadNodeStocks();
});

function runSearch() {
  const keyword = searchKeyword.value.trim();
  if (!keyword || !graphData.value) return;

  const normalized = keyword.toLowerCase();
  const found = graphData.value.nodes.find((node) => {
    const haystack = [
      node.name,
      node.id,
      ...(node.aliases || []),
      ...(node.keywords || []),
      node.description || '',
    ].join(' ').toLowerCase();
    return haystack.includes(normalized);
  });

  if (found) {
    searchMessage.value = '';
    selectNode(found);
    return;
  }

  searchMessage.value = '未在当前产业链中找到相关节点';
}

/**
 * T3.4 时间轴: 用户拖动滑块 → 重新拉取图谱
 */
async function onSliderInput(event: Event) {
  const target = event.target as HTMLInputElement;
  const index = Number(target.value);
  if (Number.isNaN(index)) return;
  dataDateIndex.value = index;
  dataDate.value = dataDateOptions.value[index] || dataDate.value;
  await selectChain(currentChainId.value, true);
}
</script>

<style scoped>
.industry-chain-page {
  position: relative;
  min-height: 100vh;
  padding: 24px;
  background: #05070d;
  color: #e2e8f0;
}

.status-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0 4px;
}

.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
}

.status-badge--warn {
  background: rgba(146, 64, 14, 0.32);
  color: #fbbf24;
  border: 1px solid rgba(217, 119, 6, 0.45);
}
.status-badge--info {
  background: rgba(30, 64, 175, 0.32);
  color: #93c5fd;
  border: 1px solid rgba(59, 130, 246, 0.45);
}
.status-badge--neutral {
  background: rgba(51, 65, 85, 0.45);
  color: #cbd5e1;
  border: 1px solid rgba(100, 116, 139, 0.45);
}

.main-layout {
  display: grid;
  grid-template-columns: minmax(0, 7fr) minmax(320px, 3fr);
  gap: 16px;
  margin-top: 16px;
  align-items: stretch;
}

.canvas-area {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 640px;
}

/* T3.4: 抽屉样式 — 在小屏 / 移动端变为右滑抽屉 */
.drawer-panel {
  transition: transform 0.24s ease, box-shadow 0.24s ease;
}
.drawer-panel.is-open {
  box-shadow: -4px 0 16px rgba(15, 23, 42, 0.6);
}

.state-message {
  margin: 4px 0 0;
  padding: 8px 12px;
  border: 1px solid #f0d8aa;
  border-radius: 8px;
  background: #1f1303;
  color: #fbbf24;
  font-size: 12px;
}

.boundary {
  margin-top: 16px;
  text-align: center;
  color: #64748b;
  font-size: 12px;
}

/* T3.4 时间轴 */
.time-slider {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid rgba(71, 85, 105, 0.45);
  border-radius: 8px;
  background: #0f172a;
}
.time-slider__caption {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: #94a3b8;
}
.time-slider__range {
  color: #64748b;
}
.time-slider__input {
  width: 100%;
  accent-color: #38bdf8;
}
.time-slider__single {
  font-size: 12px;
  color: #94a3b8;
  text-align: center;
}

@media (max-width: 1200px) {
  .main-layout {
    grid-template-columns: 1fr;
  }
  .drawer-panel {
    position: fixed;
    right: 0;
    top: 0;
    bottom: 0;
    width: min(420px, 92vw);
    z-index: 8500;
    transform: translateX(100%);
    box-shadow: -4px 0 24px rgba(0, 0, 0, 0.6);
  }
  .drawer-panel.is-open {
    transform: translateX(0);
  }
}

@media (max-width: 768px) {
  .industry-chain-page {
    padding: 16px;
  }
}
</style>
