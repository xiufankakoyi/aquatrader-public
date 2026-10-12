<template>
  <div class="workbench-discovery-funnel">
    <!-- 顶部元数据条 -->
    <div class="meta-bar">
      <div class="meta-item">
        <span class="meta-label">数据时点</span>
        <span class="meta-value mono">{{ formatTime(meta.as_of) }}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">模型版本</span>
        <span class="meta-value mono">{{ meta.model_version || '-' }}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">来源</span>
        <span class="meta-value">{{ meta.source || '-' }}</span>
      </div>
      <div class="meta-item" v-if="meta.degraded">
        <span class="status-badge status-degraded">降级</span>
      </div>
    </div>

    <!-- 降级提示 -->
    <div v-if="meta.degraded" class="degraded-banner">
      <i class="fas fa-circle-info"></i>
      <span>发现漏斗处于降级状态：{{ meta.degradation_reason || '未知原因' }}</span>
    </div>

    <!-- ============================================================ -->
    <!-- 模块 1：全市场发现漏斗 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-filter"></i>
        <span>全市场发现漏斗</span>
        <span class="block-subtitle">universe → liquidity → fundamental → readiness</span>
      </div>
      <div v-if="!funnelStages.length" class="block-empty">暂无本地证据</div>
      <div v-else class="funnel-stages">
        <div
          v-for="stage in funnelStages"
          :key="stage.stage"
          class="funnel-stage"
          :class="{ 'stage-passed': stage.passed, 'stage-blocked': !stage.passed }"
        >
          <div class="stage-label">{{ stage.label }}</div>
          <div class="stage-count">{{ stage.count }}</div>
          <div class="stage-name mono">{{ stage.stage }}</div>
        </div>
      </div>
      <div class="block-footer">
        <span class="footer-label">最终通过</span>
        <span class="footer-value">{{ funnelTotalPassed }} 家</span>
      </div>
    </section>

    <!-- ============================================================ -->
    <!-- 模块 2：公司研究卡和历史版本 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-id-card"></i>
        <span>公司研究卡和历史版本</span>
        <span class="block-subtitle">同输入重复运行生成新版本，不覆盖旧版本</span>
      </div>
      <div v-if="!companyCards.length" class="block-empty">暂无本地证据</div>
      <div v-else class="cards-grid">
        <div
          v-for="card in companyCards.slice(0, 12)"
          :key="card.symbol"
          class="company-card"
        >
          <div class="card-header">
            <span class="card-symbol mono">{{ card.symbol }}</span>
            <span class="card-name">{{ card.company_name || '未披露' }}</span>
          </div>
          <div class="card-meta">
            <div class="kv-item">
              <span class="kv-label">行业</span>
              <span class="kv-value">{{ card.industry || '未披露' }}</span>
            </div>
            <div class="kv-item">
              <span class="kv-label">最新 PIT</span>
              <span class="kv-value">{{ card.latest_as_of || '-' }}</span>
            </div>
            <div class="kv-item">
              <span class="kv-label">版本数</span>
              <span class="kv-value">{{ card.version_count }}</span>
            </div>
            <div class="kv-item">
              <span class="kv-label">最新状态</span>
              <span :class="`status-badge status-${card.latest_status}`">
                {{ statusLabel(card.latest_status) }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ============================================================ -->
    <!-- 模块 3：产品利润模型和情景 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-chart-column"></i>
        <span>产品利润模型和情景</span>
        <span class="block-subtitle warning-tag">仅展示假设，不构成预测或交易动作</span>
      </div>
      <div v-if="!profitScenarios.length" class="block-empty">暂无本地证据</div>
      <div v-else class="scenarios-table">
        <table>
          <thead>
            <tr>
              <th>情景名称</th>
              <th class="text-right">基准情景</th>
              <th class="text-right">乐观情景</th>
              <th class="text-right">悲观情景</th>
              <th>关键假设</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(s, idx) in profitScenarios.slice(0, 20)" :key="`sc-${idx}`">
              <td>{{ s.name || '未命名情景' }}</td>
              <td class="text-right">{{ formatNumber(s.base_case) }}</td>
              <td class="text-right">{{ formatNumber(s.bull_case) }}</td>
              <td class="text-right">{{ formatNumber(s.bear_case) }}</td>
              <td class="assumptions-cell">
                <span v-for="(a, i) in (s.assumptions || []).slice(0, 3)" :key="i" class="assumption-tag">
                  {{ a }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <!-- 事实 / 假设 分离 -->
      <div class="evidence-separation">
        <div class="evidence-block">
          <span class="evidence-label verified">事实依据</span>
          <span class="evidence-count">{{ profitFactBasis.length }} 条</span>
        </div>
        <div class="evidence-block">
          <span class="evidence-label assumption">假设依据</span>
          <span class="evidence-count">{{ profitAssumptionBasis.length }} 条</span>
        </div>
      </div>
    </section>

    <!-- 限制说明 -->
    <div v-if="meta.limitations && meta.limitations.length" class="limitations-bar">
      <span class="lim-label">限制:</span>
      <span v-for="(lim, idx) in meta.limitations" :key="`lim-${idx}`" class="lim-tag">
        {{ lim }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * WorkbenchDiscoveryFunnel 组件。
 *
 * P3.7 工作台三大发现模块：
 * 1. 全市场发现漏斗（4 阶段）
 * 2. 公司研究卡和历史版本
 * 3. 产品利润模型和情景（仅假设，不构成交易动作）
 *
 * 字段缺失时显示「暂无本地证据」；降级状态显示明显徽章。
 */
import { computed } from 'vue';
import {
  safeGetDiscoveryFunnel,
  safeGetCompanyCards,
  safeGetProfitModels,
  type DiscoveryFunnelResponse,
  type CompanyCardsResponse,
  type ProfitModelsResponse,
  type WorkbenchMeta,
} from '@/api/researchWorkbench';
import { formatAmount, formatNumber, formatTime, formatSafe } from '@/utils/format';

/** Props：父组件传入的预加载数据。 */
const props = defineProps<{
  funnelData: DiscoveryFunnelResponse;
  companyCardsData: CompanyCardsResponse;
  profitModelsData: ProfitModelsResponse;
}>();

/** 元数据（取自任一响应，三者一致）。 */
const meta = computed<WorkbenchMeta>(() => ({
  as_of: props.funnelData.as_of || props.companyCardsData.as_of || props.profitModelsData.as_of || '',
  model_version: props.funnelData.model_version || '',
  source: props.funnelData.source || '',
  limitations: props.funnelData.limitations || [],
  degraded: props.funnelData.degraded || props.companyCardsData.degraded || props.profitModelsData.degraded,
  degradation_reason: props.funnelData.degradation_reason || props.companyCardsData.degradation_reason || props.profitModelsData.degradation_reason,
}));

/** 漏斗阶段。 */
const funnelStages = computed(() => props.funnelData.stages || []);
const funnelTotalPassed = computed(() => props.funnelData.total_passed || 0);

/** 公司研究卡。 */
const companyCards = computed(() => props.companyCardsData.cards || []);

/** 利润模型情景。 */
const profitScenarios = computed(() => props.profitModelsData.scenarios || []);
const profitFactBasis = computed(() => props.profitModelsData.fact_basis || []);
const profitAssumptionBasis = computed(() => props.profitModelsData.assumption_basis || []);

/** 状态中文标签。 */
function statusLabel(status: string): string {
  const map: Record<string, string> = {
    ok: '完整',
    partial: '部分',
    blocked: '阻断',
    failed: '失败',
    not_computable: '不可计算',
    degraded: '降级',
    verified: '已验证',
    rejected: '已拒绝',
    missing: '缺失',
    empty: '空',
    draft: '草稿',
    active: '已激活',
    suspended: '已暂停',
    retired: '已退役',
    approved: '已批准',
    shadow: '影子运行',
    unit_validated: '单元验证',
    independent_validated: '独立验证',
  };
  return map[status] || status || '-';
}

// 暴露给父组件：默认导出便于 onMounted 时按需刷新
defineExpose({
  refresh: async () => {
    const [funnel, cards, profit] = await Promise.all([
      safeGetDiscoveryFunnel(),
      safeGetCompanyCards(),
      safeGetProfitModels(),
    ]);
    return { funnel, cards, profit };
  },
});
</script>

<style scoped>
.workbench-discovery-funnel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 元数据条 */
.meta-bar {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 10px;
}
.meta-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.meta-label {
  font-size: 11px;
  color: #787b86;
}
.meta-value {
  font-size: 13px;
  color: #d1d4dc;
  word-break: break-all;
}
.mono {
  font-family: monospace;
}

/* 降级提示 */
.degraded-banner {
  background: rgba(250, 173, 20, 0.1);
  border: 1px solid #faad14;
  border-radius: 6px;
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: #faad14;
  font-size: 13px;
}

/* 模块卡片 */
.block-card {
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 8px;
  padding: 16px;
}
.block-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.block-title i {
  color: #5470c6;
}
.block-subtitle {
  font-size: 11px;
  color: #787b86;
  font-weight: 400;
  margin-left: 4px;
}
.warning-tag {
  font-size: 11px;
  padding: 2px 6px;
  background: rgba(250, 173, 20, 0.18);
  color: #faad14;
  border-radius: 3px;
  font-weight: 500;
  margin-left: 4px;
}
.block-empty {
  color: #787b86;
  font-size: 13px;
  text-align: center;
  padding: 24px 0;
}
.block-footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px solid #1f2430;
}
.footer-label {
  font-size: 12px;
  color: #787b86;
}
.footer-value {
  font-size: 14px;
  color: #5470c6;
  font-weight: 600;
  font-family: monospace;
}

/* 漏斗 */
.funnel-stages {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
@media (max-width: 900px) {
  .funnel-stages {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 500px) {
  .funnel-stages {
    grid-template-columns: 1fr;
  }
}
.funnel-stage {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 12px;
  text-align: center;
  position: relative;
}
.funnel-stage.stage-passed {
  border-color: rgba(82, 196, 26, 0.3);
}
.funnel-stage.stage-blocked {
  border-color: rgba(255, 77, 79, 0.3);
}
.stage-label {
  font-size: 13px;
  color: #d1d4dc;
  font-weight: 600;
  margin-bottom: 6px;
}
.stage-count {
  font-size: 24px;
  font-weight: 700;
  color: #5470c6;
  font-family: monospace;
}
.stage-name {
  font-size: 11px;
  color: #787b86;
  margin-top: 4px;
}

/* 公司卡 */
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.company-card {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 10px;
}
.card-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid #2a2e39;
}
.card-symbol {
  font-family: monospace;
  font-size: 13px;
  color: #5470c6;
  background: rgba(84, 112, 198, 0.15);
  padding: 2px 6px;
  border-radius: 3px;
}
.card-name {
  font-size: 14px;
  color: #fff;
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}
.kv-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.kv-label {
  font-size: 11px;
  color: #787b86;
}
.kv-value {
  font-size: 12px;
  color: #d1d4dc;
}

/* 状态徽章 */
.status-badge {
  display: inline-block;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 500;
}
.status-ok, .status-verified, .status-active, .status-approved { background: rgba(82, 196, 26, 0.18); color: #52c41a; }
.status-partial, .status-degraded, .status-shadow, .status-unit_validated, .status-independent_validated { background: rgba(250, 173, 20, 0.18); color: #faad14; }
.status-blocked, .status-failed, .status-rejected, .status-suspended { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.status-not_computable, .status-missing, .status-empty, .status-draft, .status-retired { background: rgba(120, 123, 134, 0.18); color: #787b86; }

/* 情景表 */
.scenarios-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.scenarios-table th,
.scenarios-table td {
  padding: 8px;
  text-align: left;
  border-bottom: 1px solid #1f2430;
}
.scenarios-table th {
  color: #787b86;
  font-weight: 600;
  background: rgba(20, 30, 50, 0.3);
}
.scenarios-table td {
  color: #d1d4dc;
}
.text-right {
  text-align: right;
  font-family: monospace;
}
.assumptions-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.assumption-tag {
  font-size: 10px;
  padding: 2px 6px;
  background: rgba(84, 112, 198, 0.1);
  color: #5470c6;
  border-radius: 3px;
  border: 1px solid rgba(84, 112, 198, 0.2);
}

/* 事实/假设分离 */
.evidence-separation {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px dashed #1f2430;
}
.evidence-block {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 8px;
  background: rgba(255, 255, 255, 0.02);
  border-radius: 4px;
}
.evidence-label {
  font-size: 12px;
  font-weight: 600;
}
.evidence-label.verified {
  color: #52c41a;
}
.evidence-label.assumption {
  color: #5470c6;
}
.evidence-count {
  font-size: 12px;
  color: #d1d4dc;
  font-family: monospace;
}

/* 限制说明 */
.limitations-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  padding: 8px 12px;
  background: rgba(120, 123, 134, 0.08);
  border-radius: 4px;
}
.lim-label {
  font-size: 11px;
  color: #faad14;
  font-weight: 600;
}
.lim-tag {
  font-size: 10px;
  padding: 2px 6px;
  background: rgba(250, 173, 20, 0.1);
  color: #faad14;
  border-radius: 3px;
  border: 1px solid rgba(250, 173, 20, 0.2);
}
</style>
