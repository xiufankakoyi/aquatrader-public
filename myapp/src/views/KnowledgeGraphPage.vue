<template>
  <div class="knowledge-graph-page">
    <!-- Header -->
    <div class="page-header">
      <div class="flex items-center gap-3">
        <div class="w-1 h-8 bg-gradient-to-b from-cyan-500 to-blue-600 rounded-full"></div>
        <div>
          <h1 class="text-2xl font-bold text-white tracking-tight">证据边知识图谱</h1>
          <p class="text-sm text-[#787b86] mt-1">新词雷达 · 候选概念 · 证据边图谱 · 按需搜索验证闭环</p>
        </div>
      </div>
      <div class="header-actions">
        <a-button :loading="runningBrief" @click="triggerDailyBrief" type="primary">
          <template #icon><ReloadOutlined /></template>
          生成今日日报
        </a-button>
        <a-button @click="refreshAll" :loading="loading">
          <template #icon><SyncOutlined /></template>
          刷新
        </a-button>
      </div>
    </div>

    <!-- 调度器状态条 -->
    <div class="scheduler-bar" v-if="schedulerStatus">
      <div class="scheduler-info">
        <span class="label">调度器:</span>
        <a-tag :color="schedulerStatus.enabled ? 'green' : 'default'">
          {{ schedulerStatus.enabled ? '已启用' : '已禁用' }}
        </a-tag>
        <a-tag :color="schedulerStatus.running ? 'processing' : 'default'">
          {{ schedulerStatus.running ? '运行中' : '空闲' }}
        </a-tag>
        <span class="schedule-text">
          每日 {{ String(schedulerStatus.schedule.hour).padStart(2, '0') }}:{{ String(schedulerStatus.schedule.minute).padStart(2, '0') }}
          <span v-if="schedulerStatus.schedule.skip_weekends">（跳过周末）</span>
        </span>
        <span class="next-run" v-if="schedulerStatus.next_run_at">
          下次运行: {{ formatTime(schedulerStatus.next_run_at) }}
        </span>
        <span class="last-status" v-if="schedulerStatus.last_status && schedulerStatus.last_status !== 'idle'">
          上次状态: {{ schedulerStatus.last_status }}
        </span>
      </div>
    </div>

    <!-- Tab 切换 -->
    <a-tabs v-model:activeKey="activeTab" class="page-tabs dark-tabs">
      <!-- Tab 1: 知识链增长 -->
      <a-tab-pane key="growth" tab="知识链增长">
        <!-- 最终结果 banner -->
        <div v-if="growthData" class="final-result-banner">
          <div class="final-result-title">🎯 今日最终结果</div>
          <div class="final-result-body">
            <div class="final-result-item">
              <span class="final-result-label">冷门新词</span>
              <span class="final-result-value highlight">{{ selectedNewTerm || '暂无' }}</span>
            </div>
            <div class="final-result-item">
              <span class="final-result-label">类型</span>
              <span class="final-result-value">{{ selectedTermType || '-' }}</span>
            </div>
            <div class="final-result-item">
              <span class="final-result-label">主线</span>
              <a-tag :color="rootThemeColor(selectedRootTheme)" class="final-tag">{{ selectedRootTheme || '-' }}</a-tag>
            </div>
            <div class="final-result-item">
              <span class="final-result-label">证据等级</span>
              <span class="final-evidence-badge" :class="'evidence-' + (selectedEvidenceLevel || 'NONE')">{{ selectedEvidenceLevel || '-' }}</span>
            </div>
            <div class="final-result-item">
              <span class="final-result-label">概念评分</span>
              <span class="final-result-value">{{ selectedConceptScore.toFixed(2) }}</span>
            </div>
            <div class="final-result-item">
              <span class="final-result-label">生成时间</span>
              <span class="final-result-value">{{ formatTime(growthData.generated_at) }}</span>
            </div>
          </div>
        </div>

        <!-- 统计卡片 -->
        <div v-if="growthData" class="growth-summary-bar">
          <a-row :gutter="16">
            <a-col :span="4">
              <div class="growth-stat-card">
                <div class="growth-stat-value">{{ growthData.layers[1]?.count || 0 }}</div>
                <div class="growth-stat-label">原始文档</div>
              </div>
            </a-col>
            <a-col :span="4">
              <div class="growth-stat-card">
                <div class="growth-stat-value">{{ growthData.layers[2]?.count || 0 }}</div>
                <div class="growth-stat-label">新词术语</div>
              </div>
            </a-col>
            <a-col :span="4">
              <div class="growth-stat-card">
                <div class="growth-stat-value">{{ growthData.layers[3]?.count || 0 }}</div>
                <div class="growth-stat-label">候选概念</div>
              </div>
            </a-col>
            <a-col :span="4">
              <div class="growth-stat-card">
                <div class="growth-stat-value">{{ growthData.layers[4]?.count || 0 }}</div>
                <div class="growth-stat-label">证据边</div>
              </div>
            </a-col>
            <a-col :span="4">
              <div class="growth-stat-card">
                <div class="growth-stat-value">{{ growthData.layers[6]?.count || 0 }}</div>
                <div class="growth-stat-label">SAG 概念</div>
              </div>
            </a-col>
            <a-col :span="4">
              <div class="growth-stat-card">
                <div class="growth-stat-value">{{ growthData.promotion_candidates.length }}</div>
                <div class="growth-stat-label">晋升候选</div>
              </div>
            </a-col>
          </a-row>
        </div>

        <!-- 增长过程时间线 -->
        <a-spin :spinning="growthLoading">
          <div v-if="growthData" class="growth-timeline-panel">
            <div class="panel-title">知识链增长过程</div>
            <a-timeline mode="left" class="growth-timeline">
              <a-timeline-item
                v-for="layer in growthData.layers"
                :key="layer.layer"
                :color="layerStatusColor(layer.status)"
              >
                <template #dot>
                  <div class="growth-dot" :class="'growth-dot-' + layer.status">
                    <span>{{ layer.layer }}</span>
                  </div>
                </template>
                <div class="growth-layer-card" :class="'growth-layer-' + layer.status">
                  <div class="growth-layer-header">
                    <span class="growth-layer-title">{{ layer.title }}</span>
                    <a-tag :color="layerStatusColor(layer.status)">{{ layerStatusText(layer.status) }}</a-tag>
                  </div>
                  <div class="growth-layer-count">{{ layer.count.toLocaleString() }}</div>
                  <div class="growth-layer-detail">{{ layer.detail }}</div>
                  <div v-if="layer.breakdown" class="growth-layer-breakdown">
                    <span
                      v-for="(count, name) in layer.breakdown"
                      :key="name"
                      class="breakdown-tag"
                    >
                      {{ name }}: {{ count }}
                    </span>
                  </div>
                </div>
              </a-timeline-item>
            </a-timeline>
          </div>
        </a-spin>

        <!-- 晋升候选表格 -->
        <div v-if="growthData?.promotion_candidates.length" class="growth-candidates">
          <div class="panel-title">晋升候选（SAG 输出）</div>
          <a-table
            :columns="promotionColumns"
            :data-source="growthData.promotion_candidates"
            size="small"
            :pagination="{ pageSize: 10 }"
            row-key="candidate_id"
            class="dark-table"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'promotion_score'">
                <a-tag :color="record.promotion_score >= 0.7 ? 'success' : record.promotion_score >= 0.4 ? 'processing' : 'default'">
                  {{ Math.round(record.promotion_score * 100) }}%
                </a-tag>
              </template>
              <template v-else-if="column.key === 'source_types'">
                <span v-for="st in (record.source_types || [])" :key="st" class="source-type-tag">{{ st }}</span>
              </template>
              <template v-else-if="column.key === 'event_edge_key'">
                <span class="edge-key-text">{{ formatEdgeKey(record.event_edge_key) }}</span>
              </template>
              <template v-else-if="column.key === 'proposed_tail'">
                <span :class="record.proposed_tail ? 'entity-text' : 'empty-text'">{{ record.proposed_tail || '-' }}</span>
              </template>
            </template>
          </a-table>
        </div>
      </a-tab-pane>

      <!-- Tab 2: 证据边图谱 -->
      <a-tab-pane key="graph" tab="证据边图谱">
        <div class="graph-controls">
          <a-space>
            <span class="control-label">关系类型:</span>
            <a-select v-model:value="graphFilter.relation" style="width: 160px" @change="loadGraph">
              <a-select-option value="">全部</a-select-option>
              <a-select-option value="belongs_to">belongs_to</a-select-option>
              <a-select-option value="co_occurs_with">co_occurs_with</a-select-option>
            </a-select>
            <span class="control-label">最少证据数:</span>
            <a-input-number v-model:value="graphFilter.minEvidence" :min="0" :max="20" @change="loadGraph" />
            <span class="control-label">边数上限:</span>
            <a-input-number v-model:value="graphFilter.limit" :min="50" :max="1000" :step="50" @change="loadGraph" />
          </a-space>
          <div class="graph-stats" v-if="graphMeta">
            <a-tag color="blue">节点 {{ graphMeta.nodes }}</a-tag>
            <a-tag color="cyan">边 {{ graphMeta.filtered_edges }}</a-tag>
            <a-tag>总边数 {{ graphMeta.total_edges }}</a-tag>
          </div>
        </div>
        <a-spin :spinning="graphLoading">
          <div ref="graphChartRef" class="graph-chart"></div>
        </a-spin>
        <div class="graph-legend" v-if="graphData">
          <span v-for="(cat, idx) in graphData.categories" :key="idx" class="legend-item">
            <span class="legend-dot" :style="{ background: categoryColors[idx % categoryColors.length] }"></span>
            {{ cat.name }}
          </span>
        </div>
      </a-tab-pane>

      <!-- Tab 3: 候选概念 -->
      <a-tab-pane key="concepts" tab="候选概念">
        <div class="concepts-controls">
          <a-space>
            <span class="control-label">主线:</span>
            <a-select v-model:value="conceptsFilter.rootTheme" style="width: 120px" @change="loadConcepts">
              <a-select-option value="">全部</a-select-option>
              <a-select-option value="光">光</a-select-option>
              <a-select-option value="存">存</a-select-option>
              <a-select-option value="算">算</a-select-option>
              <a-select-option value="电">电</a-select-option>
            </a-select>
            <span class="control-label">数量:</span>
            <a-input-number v-model:value="conceptsFilter.limit" :min="10" :max="500" :step="10" @change="loadConcepts" />
          </a-space>
        </div>
        <a-table
          :columns="conceptColumns"
          :data-source="conceptsData"
          :loading="conceptsLoading"
          :pagination="{ pageSize: 20, showSizeChanger: true }"
          row-key="concept_id"
          size="small"
          :scroll="{ x: 1200 }"
          class="dark-table"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'normalized_name'">
              <a-tooltip :title="record.discovery_reason">
                <span class="concept-name">{{ record.normalized_name }}</span>
              </a-tooltip>
            </template>
            <template v-else-if="column.key === 'root_theme'">
              <a-tag :color="rootThemeColor(record.root_theme)">{{ record.root_theme }}</a-tag>
            </template>
            <template v-else-if="column.key === 'evidence_level'">
              <a-tag :color="evidenceLevelColor(record.evidence_level)">{{ record.evidence_level }}</a-tag>
            </template>
            <template v-else-if="column.key === 'co_terms'">
              <span v-for="term in (record.co_terms || []).slice(0, 5)" :key="term" class="co-term-tag">
                {{ term }}
              </span>
            </template>
            <template v-else-if="column.key === 'evidence_count'">
              {{ (record.evidence_document_ids || []).length }}
            </template>
            <template v-else-if="column.key === 'llm_explanation'">
              <a-tooltip :title="record.llm_explanation">
                <span class="llm-explanation">{{ (record.llm_explanation || '').slice(0, 40) }}{{ (record.llm_explanation || '').length > 40 ? '...' : '' }}</span>
              </a-tooltip>
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- Tab 4: 新词雷达 -->
      <a-tab-pane key="radar" tab="新词雷达">
        <a-table
          :columns="termColumns"
          :data-source="termsData"
          :loading="termsLoading"
          :pagination="{ pageSize: 20 }"
          row-key="term"
          size="small"
          class="dark-table"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'burst_score'">
              <a-progress :percent="Math.min(100, record.burst_score * 5)" size="small" :show-info="false"
                :stroke-color="record.burst_score > 10 ? '#ff4d4f' : record.burst_score > 5 ? '#faad14' : '#52c41a'" />
              <span style="margin-left: 8px">{{ record.burst_score }}</span>
            </template>
            <template v-else-if="column.key === 'co_terms'">
              <span v-for="term in (record.co_terms || []).slice(0, 5)" :key="term" class="co-term-tag">
                {{ term }}
              </span>
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- Tab 5: 今日日报 -->
      <a-tab-pane key="brief" tab="今日日报">
        <div class="brief-controls">
          <a-space>
            <span class="control-label" v-if="briefData">生成时间: {{ formatTime(briefData.generated_at) }}</span>
            <a-button size="small" @click="loadBrief">刷新日报</a-button>
          </a-space>
        </div>
        <div class="brief-content" v-if="briefData" v-html="renderedBrief"></div>
        <a-empty v-else description="暂无日报，请点击右上角生成今日日报" />
      </a-tab-pane>

      <!-- Tab 6: 盈利映射 -->
      <a-tab-pane key="earnings" tab="盈利映射">
        <EarningsMappingTab />
      </a-tab-pane>
      <a-tab-pane key="expectations" tab="预期差与研究观点">
        <ExpectationsThesisTab />
      </a-tab-pane>
      <a-tab-pane key="adaptation" tab="四域归因与校准">
        <FourDomainAttributionTab />
      </a-tab-pane>
      <a-tab-pane key="candidate" tab="研究候选输出">
        <CandidateTab />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount, nextTick, computed } from 'vue';
import { message } from 'ant-design-vue';
import { ReloadOutlined, SyncOutlined } from '@ant-design/icons-vue';
import * as echarts from 'echarts';
import EarningsMappingTab from '@/components/earnings/EarningsMappingTab.vue';
import ExpectationsThesisTab from '@/components/expectations/ExpectationsThesisTab.vue';
import FourDomainAttributionTab from '@/components/adaptation/FourDomainAttributionTab.vue';
import CandidateTab from '@/components/candidate/CandidateTab.vue';
import {
  getEvidenceEdges,
  getCandidateConcepts,
  getRawTerms,
  getDailyBrief,
  getSchedulerStatus,
  getGrowthProcess,
  runDailyBrief,
  type EvidenceGraphData,
  type CandidateConcept,
  type RawTerm,
  type SchedulerStatus,
  type GrowthProcessData,
  type GrowthLayer,
} from '@/api/perilla';

// Tab 状态：默认展示知识链增长，让用户直观看到增长过程
const activeTab = ref('growth');

// 图谱状态
const graphChartRef = ref<HTMLElement>();
let chartInstance: echarts.ECharts | null = null;
const graphData = ref<EvidenceGraphData | null>(null);
const graphMeta = ref<{ nodes: number; filtered_edges: number; total_edges: number } | null>(null);
const graphLoading = ref(false);
const graphFilter = reactive({
  relation: '',
  minEvidence: 0,
  limit: 200,
});

// 候选概念状态
const conceptsData = ref<CandidateConcept[]>([]);
const conceptsLoading = ref(false);
const conceptsFilter = reactive({
  rootTheme: '',
  limit: 50,
});

// 新词雷达状态
const termsData = ref<RawTerm[]>([]);
const termsLoading = ref(false);

// 日报状态
const briefData = ref<{ markdown: string; generated_at: string } | null>(null);
const runningBrief = ref(false);

// 调度器状态
const schedulerStatus = ref<SchedulerStatus | null>(null);

// 知识链增长过程状态
const growthData = ref<GrowthProcessData | null>(null);
const growthLoading = ref(false);

const loading = computed(() => graphLoading.value || conceptsLoading.value || termsLoading.value || growthLoading.value);

// 颜色配置
const categoryColors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452', '#9a60b4'];

// 最终结果：直接使用后端选出的冷门新词，不再误用晋升候选第一个
const selectedNewTerm = computed(() => growthData.value?.selected_new_term?.normalized_name || '');
const selectedTermType = computed(() => growthData.value?.selected_new_term?.term_type || '');
const selectedRootTheme = computed(() => growthData.value?.selected_new_term?.root_theme || '');
const selectedEvidenceLevel = computed(() => growthData.value?.selected_new_term?.evidence_level || '');
const selectedConceptScore = computed(() => growthData.value?.selected_new_term?.concept_score || 0);

// Markdown 简易渲染
const renderedBrief = computed(() => {
  if (!briefData.value) return '';
  return briefData.value.markdown
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]+?<\/li>)/g, '<ul>$1</ul>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/^(?!<[hul])(.+)$/gm, '<p>$1</p>');
});

// 表格列定义
const conceptColumns = [
  { title: '概念名', key: 'normalized_name', width: 180, fixed: 'left' },
  { title: '类型', dataIndex: 'term_type', key: 'term_type', width: 120 },
  { title: '主线', key: 'root_theme', width: 80 },
  { title: '层级', dataIndex: 'chain_layer', key: 'chain_layer', width: 140 },
  { title: '证据等级', key: 'evidence_level', width: 160 },
  { title: '文档数', key: 'evidence_count', width: 80 },
  { title: '评分', dataIndex: 'concept_score', key: 'concept_score', width: 80, sorter: (a: CandidateConcept, b: CandidateConcept) => a.concept_score - b.concept_score },
  { title: '共现术语', key: 'co_terms', width: 240 },
  { title: 'LLM解释', key: 'llm_explanation', width: 200 },
];

const termColumns = [
  { title: '术语', dataIndex: 'term', key: 'term', width: 180, fixed: 'left' },
  { title: '类型', dataIndex: 'term_type', key: 'term_type', width: 120 },
  { title: '突增分数', key: 'burst_score', width: 200, sorter: (a: RawTerm, b: RawTerm) => a.burst_score - b.burst_score },
  { title: '源数', dataIndex: 'source_count', key: 'source_count', width: 80 },
  { title: '近期数', dataIndex: 'recent_count', key: 'recent_count', width: 80 },
  { title: '共现', key: 'co_terms', width: 300 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 120 },
];

// 晋升候选表格列（对齐后端实际字段）
const promotionColumns = [
  { title: '概念名', dataIndex: 'proposed_head', key: 'proposed_head', width: 140 },
  { title: '关系', dataIndex: 'proposed_relation', key: 'proposed_relation', width: 120 },
  { title: 'Tail', key: 'proposed_tail', width: 120 },
  { title: '事件类型', key: 'event_edge_key', width: 180 },
  { title: '晋升分', key: 'promotion_score', width: 120 },
  { title: '来源类型', key: 'source_types', width: 160 },
  { title: '证据数', dataIndex: 'source_count', key: 'source_count', width: 80 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 110 },
];

// 工具函数
function rootThemeColor(theme: string): string {
  const map: Record<string, string> = { '光': 'blue', '存': 'green', '算': 'orange', '电': 'red' };
  return map[theme] || 'default';
}

function evidenceLevelColor(level: string): string {
  const map: Record<string, string> = {
    'HYPOTHESIS': 'default',
    'RUMOR_ONLY': 'warning',
    'EARLY_SIGNAL': 'processing',
    'CONCEPT_CLAIM_ONLY': 'error',
  };
  return map[level] || 'default';
}

// 知识链增长层状态样式
function layerStatusColor(status: string): string {
  const map: Record<string, string> = { pending: 'default', running: 'processing', done: 'success' };
  return map[status] || 'default';
}

function layerStatusText(status: string): string {
  const map: Record<string, string> = { pending: '未开始', running: '进行中', done: '已完成' };
  return map[status] || status;
}

function formatEdgeKey(key: string[] | undefined): string {
  if (!Array.isArray(key) || key.length < 3) return '-';
  return `${key[0]} / ${key[1]} / ${key[2] || '-'}`;
}

function formatTime(iso: string): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return iso;
  }
}

// 加载图谱
async function loadGraph() {
  graphLoading.value = true;
  try {
    const { data, meta } = await getEvidenceEdges({
      limit: graphFilter.limit,
      relation: graphFilter.relation || undefined,
      min_evidence: graphFilter.minEvidence,
    });
    graphData.value = data;
    graphMeta.value = meta as { nodes: number; filtered_edges: number; total_edges: number };
    await nextTick();
    renderGraph();
  } catch (e: any) {
    message.error('加载图谱失败: ' + (e?.message || e));
  } finally {
    graphLoading.value = false;
  }
}

function renderGraph() {
  if (!graphChartRef.value || !graphData.value) return;
  if (!chartInstance) {
    chartInstance = echarts.init(graphChartRef.value);
  }
  const option: echarts.EChartsOption = {
    tooltip: {
      formatter: (params: any) => {
        if (params.dataType === 'edge') {
          const d = params.data;
          return `<b>${d.source} → ${d.target}</b><br/>关系: ${d.relation}<br/>置信度: ${d.confidence_level}<br/>证据数: ${d.evidence_count}${d.evidence_quotes?.length ? '<br/>证据: ' + d.evidence_quotes[0].slice(0, 80) : ''}`;
        }
        return `<b>${params.name}</b><br/>连接数: ${params.value || 0}`;
      },
    },
    legend: {
      data: (graphData.value.categories || []).map((c) => c.name),
      textStyle: { color: '#787b86' },
      top: 10,
    },
    series: [
      {
        type: 'graph',
        layout: 'force',
        data: graphData.value.nodes,
        links: graphData.value.links,
        categories: graphData.value.categories,
        roam: true,
        label: { show: true, position: 'right', color: '#d1d4dc', fontSize: 11 },
        force: { repulsion: 200, edgeLength: [80, 200], gravity: 0.1 },
        lineStyle: { color: 'source', curveness: 0.1, opacity: 0.6 },
        emphasis: {
          focus: 'adjacency',
          lineStyle: { width: 3 },
          label: { fontSize: 13, fontWeight: 'bold' },
        },
      },
    ],
  };
  chartInstance.setOption(option, true);
}

// 加载候选概念
async function loadConcepts() {
  conceptsLoading.value = true;
  try {
    const { data } = await getCandidateConcepts({
      limit: conceptsFilter.limit,
      root_theme: conceptsFilter.rootTheme || undefined,
    });
    conceptsData.value = data;
  } catch (e: any) {
    message.error('加载候选概念失败: ' + (e?.message || e));
  } finally {
    conceptsLoading.value = false;
  }
}

// 加载新词雷达
async function loadTerms() {
  termsLoading.value = true;
  try {
    const { data } = await getRawTerms({ limit: 100 });
    termsData.value = data;
  } catch (e: any) {
    message.error('加载新词雷达失败: ' + (e?.message || e));
  } finally {
    termsLoading.value = false;
  }
}

// 加载日报
async function loadBrief() {
  try {
    briefData.value = await getDailyBrief();
  } catch {
    briefData.value = null;
  }
}

// 加载知识链增长过程
async function loadGrowthProcess() {
  growthLoading.value = true;
  try {
    const { data } = await getGrowthProcess();
    growthData.value = data;
  } catch (e: any) {
    message.error('加载知识链增长过程失败: ' + (e?.message || e));
  } finally {
    growthLoading.value = false;
  }
}

// 加载调度器状态
async function loadSchedulerStatus() {
  try {
    schedulerStatus.value = await getSchedulerStatus();
  } catch {
    // ignore
  }
}

// 触发日报生成
async function triggerDailyBrief() {
  runningBrief.value = true;
  try {
    const res = await runDailyBrief({ execute_search: true, use_llm: true });
    message.success(`日报生成任务已提交，task_id: ${res.task_id}`);
    // 30秒后刷新
    setTimeout(() => {
      loadBrief();
      loadSchedulerStatus();
    }, 30000);
  } catch (e: any) {
    message.error('触发日报失败: ' + (e?.message || e));
  } finally {
    runningBrief.value = false;
  }
}

// 刷新全部
async function refreshAll() {
  await Promise.all([
    loadGraph(),
    loadConcepts(),
    loadTerms(),
    loadBrief(),
    loadSchedulerStatus(),
    loadGrowthProcess(),
  ]);
}

// 窗口 resize
function handleResize() {
  chartInstance?.resize();
}

onMounted(() => {
  refreshAll();
  window.addEventListener('resize', handleResize);
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize);
  chartInstance?.dispose();
  chartInstance = null;
});
</script>

<style scoped>
.knowledge-graph-page {
  padding: 16px 24px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.header-actions {
  display: flex;
  gap: 8px;
}
.scheduler-bar {
  background: rgba(20, 30, 50, 0.6);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 8px 16px;
  margin-bottom: 16px;
}
.scheduler-info {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 13px;
  color: #d1d4dc;
}
.scheduler-info .label {
  color: #787b86;
}
.schedule-text {
  color: #52c41a;
}
.next-run, .last-status {
  color: #787b86;
}
.graph-controls {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
}
.control-label {
  color: #787b86;
  font-size: 13px;
}
.graph-stats {
  display: flex;
  gap: 8px;
}
.graph-chart {
  width: 100%;
  height: 600px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 6px;
}
.graph-legend {
  display: flex;
  gap: 16px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #d1d4dc;
  font-size: 13px;
}
.legend-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
}
.concepts-controls {
  margin-bottom: 12px;
}
.concept-name {
  color: #5470c6;
  font-weight: 500;
}
.co-term-tag {
  display: inline-block;
  background: rgba(84, 112, 198, 0.15);
  border: 1px solid rgba(84, 112, 198, 0.3);
  border-radius: 3px;
  padding: 1px 6px;
  margin: 1px 2px;
  font-size: 12px;
  color: #91cc75;
}
.llm-explanation {
  color: #787b86;
  font-size: 12px;
}
.brief-controls {
  margin-bottom: 12px;
}
.brief-content {
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 24px;
  color: #d1d4dc;
  line-height: 1.8;
  font-size: 14px;
}
.brief-content :deep(h1) {
  font-size: 22px;
  color: #fff;
  border-bottom: 1px solid #2a2e39;
  padding-bottom: 8px;
  margin: 16px 0;
}
.brief-content :deep(h2) {
  font-size: 18px;
  color: #5470c6;
  margin: 16px 0 8px;
}
.brief-content :deep(h3) {
  font-size: 15px;
  color: #91cc75;
  margin: 12px 0 6px;
}
.brief-content :deep(ul) {
  padding-left: 20px;
}
.brief-content :deep(li) {
  margin: 4px 0;
}
.brief-content :deep(strong) {
  color: #fac858;
}

/* 知识链增长过程样式 */
.final-result-banner {
  background: linear-gradient(90deg, rgba(20, 30, 50, 0.95), rgba(30, 50, 80, 0.9));
  border: 1px solid #2a5a8a;
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 16px;
}
.final-result-title {
  font-size: 15px;
  font-weight: 700;
  color: #fff;
  margin-bottom: 12px;
}
.final-result-body {
  display: flex;
  gap: 24px;
  align-items: center;
  flex-wrap: wrap;
}
.final-result-item {
  display: flex;
  align-items: center;
  gap: 8px;
}
.final-result-label {
  font-size: 13px;
  color: #787b86;
}
.final-result-value {
  font-size: 14px;
  color: #d1d4dc;
  font-weight: 500;
}
.final-result-value.highlight {
  font-size: 20px;
  color: #52c41a;
  font-weight: 700;
}
:deep(.final-tag) {
  color: #fff !important;
  border: none !important;
  font-weight: 500;
}
.final-evidence-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 500;
  color: #fff;
  background: #4a4f5c;
}
.final-evidence-badge.evidence-EARLY_SIGNAL {
  background: #1677ff;
}
.final-evidence-badge.evidence-RUMOR_ONLY {
  background: #faad14;
  color: #000;
}
.final-evidence-badge.evidence-HYPOTHESIS {
  background: #4a4f5c;
}
.final-evidence-badge.evidence-CONCEPT_CLAIM_ONLY {
  background: #ff4d4f;
}
.growth-summary-bar {
  margin-bottom: 16px;
}
.growth-stat-card {
  background: rgba(20, 30, 50, 0.7);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 12px 8px;
  text-align: center;
}
.growth-stat-value {
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  line-height: 1.2;
}
.growth-stat-label {
  font-size: 12px;
  color: #a0a4b0;
  margin-top: 4px;
}
.growth-timeline-panel {
  padding: 16px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 6px;
  margin-bottom: 16px;
}
.panel-title {
  font-size: 15px;
  font-weight: 600;
  color: #fff;
  margin-bottom: 16px;
}
.growth-timeline {
  padding-left: 8px;
}
.growth-dot {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
}
.growth-dot-pending {
  background: #787b86;
}
.growth-dot-running {
  background: #faad14;
}
.growth-dot-done {
  background: #52c41a;
}
.growth-layer-card {
  background: rgba(20, 30, 50, 0.8);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 12px 16px;
  margin-left: 8px;
}
.growth-layer-pending {
  border-left: 3px solid #787b86;
}
.growth-layer-running {
  border-left: 3px solid #faad14;
}
.growth-layer-done {
  border-left: 3px solid #52c41a;
}
.growth-layer-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.growth-layer-title {
  font-size: 14px;
  font-weight: 600;
  color: #fff;
}
.growth-layer-count {
  font-size: 24px;
  font-weight: 700;
  color: #fff;
  margin-bottom: 4px;
}
.growth-layer-detail {
  font-size: 12px;
  color: #a0a4b0;
  margin-bottom: 8px;
}
.growth-layer-breakdown {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.breakdown-tag {
  display: inline-block;
  background: rgba(84, 112, 198, 0.2);
  border: 1px solid rgba(84, 112, 198, 0.4);
  border-radius: 3px;
  padding: 2px 6px;
  font-size: 11px;
  color: #b8c5e8;
}
.growth-candidates {
  padding: 16px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 6px;
}
.source-type-tag {
  display: inline-block;
  background: rgba(84, 112, 198, 0.2);
  border: 1px solid rgba(84, 112, 198, 0.4);
  border-radius: 3px;
  padding: 1px 6px;
  margin: 1px 2px;
  font-size: 12px;
  color: #b8c5e8;
}
.entity-text {
  color: #d1d4dc;
}
.empty-text {
  color: #787b86;
}
.edge-key-text {
  color: #d1d4dc;
  font-size: 12px;
}

/* Ant Design 表格深色主题覆盖 */
:deep(.dark-table .ant-table) {
  background: transparent;
  color: #d1d4dc;
}
:deep(.dark-table .ant-table-thead > tr > th) {
  background: #1a1f2e;
  color: #fff;
  border-bottom: 1px solid #2a2e39;
}
:deep(.dark-table .ant-table-tbody > tr > td) {
  background: #131722;
  color: #d1d4dc;
  border-bottom: 1px solid #1f2430;
}
:deep(.dark-table .ant-table-tbody > tr:hover > td) {
  background: #1a1f2e;
}
:deep(.dark-table .ant-table-cell-fix-left) {
  background: #131722;
}
:deep(.dark-table .ant-table-cell-fix-left::after) {
  box-shadow: inset 10px 0 8px -8px rgba(0, 0, 0, 0.3);
}
:deep(.dark-table .ant-pagination-item-link),
:deep(.dark-table .ant-pagination-item) {
  color: #d1d4dc;
  background: #1a1f2e;
  border-color: #2a2e39;
}
:deep(.dark-table .ant-pagination-item-active) {
  border-color: #1677ff;
}
:deep(.dark-table .ant-pagination-item-active a) {
  color: #1677ff;
}
:deep(.dark-table .ant-empty-description) {
  color: #787b86;
}
:deep(.dark-table .ant-tag) {
  color: #fff;
  border: none;
}
:deep(.dark-table .ant-tag-success) {
  background: #52c41a;
}
:deep(.dark-table .ant-tag-processing) {
  background: #1677ff;
}
:deep(.dark-table .ant-tag-warning) {
  background: #faad14;
}
:deep(.dark-table .ant-tag-error) {
  background: #ff4d4f;
}
:deep(.dark-table .ant-tag-default) {
  background: #4a4f5c;
}

/* Tab 深色样式 */
:deep(.dark-tabs .ant-tabs-tab) {
  color: #787b86;
}
:deep(.dark-tabs .ant-tabs-tab-active) {
  color: #fff;
}
:deep(.dark-tabs .ant-tabs-ink-bar) {
  background: #1677ff;
}
</style>
