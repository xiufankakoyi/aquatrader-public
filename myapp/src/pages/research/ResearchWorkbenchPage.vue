<template>
  <div class="research-workbench-page">
    <!-- 顶部标题 + 操作 -->
    <div class="page-header">
      <div class="header-title">
        <div class="title-bar"></div>
        <div>
          <h1 class="text-2xl font-bold text-white tracking-tight">机构级研究工作台</h1>
          <p class="text-sm text-[#787b86] mt-1">
            P3.7 · 8 大功能模块 · 传播治理 · 不输出交易动作 · 区分事实 / 观点 / 假设
          </p>
        </div>
      </div>
      <div class="header-actions">
        <a-button @click="refreshAll" :loading="loadingAll">
          <template #icon><i class="fas fa-sync"></i></template>
          全量刷新
        </a-button>
      </div>
    </div>

    <!-- 顶部状态条：工作台总览 -->
    <div class="overview-bar">
      <div class="overview-item">
        <i class="fas fa-shield-halved"></i>
        <span class="overview-label">P3 门禁</span>
        <span :class="`overview-value gate-${overview.p3_gate?.status || 'unknown'}`">
          {{ gateStatusLabel(overview.p3_gate?.status) }}
        </span>
      </div>
      <div class="overview-item">
        <i class="fas fa-code-branch"></i>
        <span class="overview-label">模型族</span>
        <span class="overview-value">{{ overview.model_registry_summary?.model_families ?? 0 }}</span>
      </div>
      <div class="overview-item">
        <i class="fas fa-layer-group"></i>
        <span class="overview-label">版本总数</span>
        <span class="overview-value">{{ overview.model_registry_summary?.total_versions ?? 0 }}</span>
      </div>
      <div class="overview-item">
        <i class="fas fa-clock"></i>
        <span class="overview-label">数据时点</span>
        <span class="overview-value mono">{{ formatTime(overview.as_of) }}</span>
      </div>
      <div class="overview-item">
        <i class="fas fa-tag"></i>
        <span class="overview-label">模型版本</span>
        <span class="overview-value mono">{{ overview.model_version || '-' }}</span>
      </div>
      <div class="overview-item" v-if="overview.p3_gate?.is_blocked">
        <span class="warning-tag">
          <i class="fas fa-triangle-exclamation"></i> P3 阻断中
        </span>
      </div>
    </div>

    <!-- 模块降级状态条 -->
    <div class="modules-bar">
      <div
        v-for="m in overview.modules || []"
        :key="m.key"
        class="module-tag"
        :class="{ 'module-degraded': m.degraded, 'module-ok': !m.degraded }"
        :title="m.label + (m.degraded ? '（降级）' : '（正常）')"
      >
        <i :class="moduleIcon(m.key)"></i>
        <span class="module-label">{{ m.label }}</span>
        <span v-if="m.degraded" class="module-status">降级</span>
        <span v-else class="module-status">正常</span>
      </div>
    </div>

    <!-- 传播治理提醒 -->
    <div class="governance-banner">
      <i class="fas fa-circle-info"></i>
      <span>本工作台遵循 P3.7 传播治理规则：每份材料含 as_of / 模型版本 / 来源说明 / 限制；区分事实、观点、假设；未完成复核内容带状态标签；不生成交易动作措辞。</span>
    </div>

    <!-- 加载态 -->
    <div v-if="loadingAll && !overviewLoaded" class="loading-state">
      <i class="fas fa-spinner fa-spin"></i>
      <span>正在加载工作台数据...</span>
    </div>

    <!-- 标签页布局：8 大模块组织为 4 个标签页 -->
    <a-tabs v-else v-model:activeKey="activeTab" type="card" class="workbench-tabs">
      <!-- 标签页 1：发现与公司 -->
      <a-tab-pane key="discovery">
        <template #tab>
          <span class="tab-label">
            <i class="fas fa-filter"></i> 发现与公司
          </span>
        </template>
        <WorkbenchDiscoveryFunnel
          :funnel-data="funnelData"
          :company-cards-data="companyCardsData"
          :profit-models-data="profitModelsData"
        />
      </a-tab-pane>

      <!-- 标签页 2：模型治理 -->
      <a-tab-pane key="governance">
        <template #tab>
          <span class="tab-label">
            <i class="fas fa-shield-halved"></i> 模型治理
          </span>
        </template>
        <WorkbenchModelGovernance
          :overview="overview"
          :data-health="dataHealthData"
          :models="modelsData"
          :loading-models="loadingModels"
          @refresh-models="refreshModels"
        />
      </a-tab-pane>

      <!-- 标签页 3：观点与投委会 -->
      <a-tab-pane key="vp">
        <template #tab>
          <span class="tab-label">
            <i class="fas fa-eye"></i> 观点与投委会
          </span>
        </template>
        <WorkbenchVariantPerception
          :vp-data="vpData"
          :bear-questions-data="bearQuestionsData"
        />
      </a-tab-pane>

      <!-- 标签页 4：风险与记分卡 -->
      <a-tab-pane key="risk">
        <template #tab>
          <span class="tab-label">
            <i class="fas fa-calendar-days"></i> 风险与记分卡
          </span>
        </template>
        <WorkbenchRiskCalendar
          :risk-calendar-data="riskCalendarData"
          :scorecard-data="scorecardData"
        />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
/**
 * ResearchWorkbenchPage 主页面。
 *
 * P3.7 机构级研究工作台入口，组织 8 大功能模块为 4 个标签页：
 * 1. 发现与公司：发现漏斗 + 公司研究卡 + 利润模型情景
 * 2. 模型治理：P3 门禁 + 模型版本管理 + 数据健康
 * 3. 观点与投委会：VariantPerception + 反方问题 + 投委会状态
 * 4. 风险与记分卡：风险验证日历 + 历史预测记分卡
 *
 * 设计原则：
 * - 全中文界面
 * - 不输出买入/卖出/仓位/止盈/止损等交易动作措辞
 * - 字段缺失时降级显示「暂无本地证据」
 * - 数值格式：百分比 2 位、分数 4 位、金额用 万/亿
 * - 未完成复核内容带明显水印或状态标签
 * - 每份数据展示包含 as_of 时间戳和模型版本
 */
import { ref, onMounted } from 'vue';
import { message } from 'ant-design-vue';
import WorkbenchDiscoveryFunnel from '@/components/research/WorkbenchDiscoveryFunnel.vue';
import WorkbenchModelGovernance from '@/components/research/WorkbenchModelGovernance.vue';
import WorkbenchVariantPerception from '@/components/research/WorkbenchVariantPerception.vue';
import WorkbenchRiskCalendar from '@/components/research/WorkbenchRiskCalendar.vue';
import {
  safeGetWorkbenchOverview,
  safeGetDiscoveryFunnel,
  safeGetCompanyCards,
  safeGetProfitModels,
  safeGetVariantPerception,
  safeGetBearQuestions,
  safeGetRiskCalendar,
  safeGetWorkbenchDataHealth,
  safeGetScorecard,
  safeListModels,
  type WorkbenchOverviewResponse,
  type DiscoveryFunnelResponse,
  type CompanyCardsResponse,
  type ProfitModelsResponse,
  type VariantPerceptionResponse,
  type BearQuestionsResponse,
  type RiskCalendarResponse,
  type DataHealthResponse,
  type ScorecardResponse,
  type ModelVersionRecord,
} from '@/api/researchWorkbench';
import { formatTime } from '@/utils/format';

/** 当前激活的标签页。 */
const activeTab = ref('discovery');

/** 工作台总览。 */
const overview = ref<WorkbenchOverviewResponse>(makeEmptyOverview());
const overviewLoaded = ref(false);

/** 各模块数据。 */
const funnelData = ref<DiscoveryFunnelResponse>(makeEmptyFunnel());
const companyCardsData = ref<CompanyCardsResponse>(makeEmptyCompanyCards());
const profitModelsData = ref<ProfitModelsResponse>(makeEmptyProfitModels());
const vpData = ref<VariantPerceptionResponse>(makeEmptyVp());
const bearQuestionsData = ref<BearQuestionsResponse>(makeEmptyBearQuestions());
const riskCalendarData = ref<RiskCalendarResponse>(makeEmptyRiskCalendar());
const dataHealthData = ref<DataHealthResponse>(makeEmptyDataHealth());
const scorecardData = ref<ScorecardResponse>(makeEmptyScorecard());
const modelsData = ref<ModelVersionRecord[]>([]);

/** 加载状态。 */
const loadingAll = ref(false);
const loadingModels = ref(false);

// ---------------------------------------------------------------------------
// 业务逻辑
// ---------------------------------------------------------------------------

/** 全量刷新所有模块数据。 */
async function refreshAll() {
  loadingAll.value = true;
  try {
    // 并行加载所有只读端点
    const [
      overviewResp,
      funnelResp,
      cardsResp,
      profitResp,
      vpResp,
      bearResp,
      riskResp,
      healthResp,
      scoreResp,
      modelsResp,
    ] = await Promise.all([
      safeGetWorkbenchOverview(),
      safeGetDiscoveryFunnel(),
      safeGetCompanyCards(),
      safeGetProfitModels(),
      safeGetVariantPerception(),
      safeGetBearQuestions(),
      safeGetRiskCalendar(),
      safeGetWorkbenchDataHealth(),
      safeGetScorecard(),
      safeListModels(),
    ]);

    overview.value = overviewResp;
    funnelData.value = funnelResp;
    companyCardsData.value = cardsResp;
    profitModelsData.value = profitResp;
    vpData.value = vpResp;
    bearQuestionsData.value = bearResp;
    riskCalendarData.value = riskResp;
    dataHealthData.value = healthResp;
    scorecardData.value = scoreResp;
    modelsData.value = modelsResp;
    overviewLoaded.value = true;
  } finally {
    loadingAll.value = false;
  }
}

/** 刷新模型版本列表。 */
async function refreshModels() {
  loadingModels.value = true;
  try {
    modelsData.value = await safeListModels();
    message.success(`已加载 ${modelsData.value.length} 条模型版本`);
  } finally {
    loadingModels.value = false;
  }
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

/** 门禁状态中文标签。 */
function gateStatusLabel(status: string | undefined): string {
  const map: Record<string, string> = {
    ok: '已通过',
    pending_p2: 'P2 待认证',
    pending_p1: 'P1 待认证',
    pending_p0: 'P0 待认证',
    unknown: '未知',
  };
  return map[status || 'unknown'] || '未知';
}

/** 模块图标。 */
function moduleIcon(key: string): string {
  const map: Record<string, string> = {
    discovery_funnel: 'fas fa-filter',
    company_cards: 'fas fa-id-card',
    profit_models: 'fas fa-chart-column',
    variant_perception: 'fas fa-eye',
    bear_questions: 'fas fa-circle-question',
    risk_calendar: 'fas fa-calendar-days',
    data_health: 'fas fa-heart-pulse',
    scorecard: 'fas fa-chart-line',
  };
  return map[key] || 'fas fa-circle-info';
}

// ---------------------------------------------------------------------------
// 空数据工厂
// ---------------------------------------------------------------------------

function makeEmptyOverview(): WorkbenchOverviewResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    p3_gate: {
      status: 'unknown',
      p2_overall_passed: false,
      reason: '尚未加载',
      artifact_path: '',
      is_blocked: true,
    },
    model_registry_summary: { model_families: 0, total_versions: 0 },
    modules: [],
  };
}

function makeEmptyFunnel(): DiscoveryFunnelResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    degraded: true,
    degradation_reason: '尚未加载',
    stages: [],
    total_passed: 0,
  };
}

function makeEmptyCompanyCards(): CompanyCardsResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    degraded: true,
    degradation_reason: '尚未加载',
    cards: [],
    total: 0,
    symbol_filter: '',
  };
}

function makeEmptyProfitModels(): ProfitModelsResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    degraded: true,
    degradation_reason: '尚未加载',
    scenarios: [],
    fact_basis: [],
    assumption_basis: [],
  };
}

function makeEmptyVp(): VariantPerceptionResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    degraded: true,
    degradation_reason: '尚未加载',
    perceptions: [],
    active_count: 0,
  };
}

function makeEmptyBearQuestions(): BearQuestionsResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    degraded: true,
    degradation_reason: '尚未加载',
    bear_questions: [],
    committee_status: {
      state: 'not_convened',
      pending_questions: 0,
      last_convened_at: '',
      members: [],
    },
  };
}

function makeEmptyRiskCalendar(): RiskCalendarResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    degraded: true,
    degradation_reason: '尚未加载',
    events: [],
    upcoming_count: 0,
  };
}

function makeEmptyDataHealth(): DataHealthResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    degraded: true,
    degradation_reason: '尚未加载',
    data_health: {},
    model_status_summary: {
      active_count: 0,
      suspended_count: 0,
      any_suspended: false,
    },
  };
}

function makeEmptyScorecard(): ScorecardResponse {
  return {
    as_of: '',
    model_version: '',
    source: '',
    limitations: [],
    degraded: true,
    degradation_reason: '尚未加载',
    scorecard: [],
    summary: {
      total_predictions: 0,
      hit_count: 0,
      miss_count: 0,
      coverage_rate: null,
      mean_absolute_error: null,
    },
  };
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------

onMounted(async () => {
  await refreshAll();
});
</script>

<style scoped>
.research-workbench-page {
  padding: 16px 24px;
  min-height: calc(100vh - 64px);
}

/* Header */
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}
.header-title {
  display: flex;
  align-items: center;
  gap: 12px;
}
.title-bar {
  width: 4px;
  height: 32px;
  background: linear-gradient(to bottom, #5470c6, #2962ff);
  border-radius: 2px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 总览状态条 */
.overview-bar {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.overview-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.overview-item i {
  color: #5470c6;
  font-size: 14px;
}
.overview-label {
  color: #787b86;
}
.overview-value {
  color: #d1d4dc;
  font-weight: 600;
  font-family: monospace;
  word-break: break-all;
}
.overview-value.gate-ok { color: #52c41a; }
.overview-value.gate-pending_p2,
.overview-value.gate-pending_p1,
.overview-value.gate-pending_p0,
.overview-value.gate-unknown { color: #ff4d4f; }
.warning-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  padding: 4px 8px;
  background: rgba(255, 77, 79, 0.18);
  color: #ff4d4f;
  border-radius: 4px;
  font-weight: 600;
}

/* 模块状态条 */
.modules-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.module-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  padding: 4px 8px;
  border-radius: 4px;
  border: 1px solid #2a2e39;
}
.module-tag i {
  font-size: 10px;
}
.module-tag.module-ok {
  background: rgba(82, 196, 26, 0.05);
  color: #52c41a;
  border-color: rgba(82, 196, 26, 0.2);
}
.module-tag.module-ok i {
  color: #52c41a;
}
.module-tag.module-degraded {
  background: rgba(250, 173, 20, 0.05);
  color: #faad14;
  border-color: rgba(250, 173, 20, 0.2);
}
.module-tag.module-degraded i {
  color: #faad14;
}
.module-label {
  font-weight: 500;
}
.module-status {
  font-size: 10px;
  padding: 1px 4px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.05);
}

/* 治理提醒 */
.governance-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(84, 112, 198, 0.1);
  border: 1px solid rgba(84, 112, 198, 0.3);
  border-radius: 6px;
  padding: 10px 16px;
  margin-bottom: 16px;
  color: #5470c6;
  font-size: 12px;
  line-height: 1.5;
}
.governance-banner i {
  font-size: 14px;
}

/* 加载态 */
.loading-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 64px;
  color: #787b86;
  font-size: 14px;
}
.loading-state i {
  font-size: 32px;
  color: #5470c6;
}

/* 标签页 */
.workbench-tabs :deep(.ant-tabs-nav) {
  margin-bottom: 16px;
}
.workbench-tabs :deep(.ant-tabs-tab) {
  background: #131722 !important;
  border-color: #2a2e39 !important;
}
.workbench-tabs :deep(.ant-tabs-tab-active) {
  background: rgba(84, 112, 198, 0.15) !important;
  border-color: #5470c6 !important;
}
.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.tab-label i {
  color: #5470c6;
}

.mono {
  font-family: monospace;
}
</style>
