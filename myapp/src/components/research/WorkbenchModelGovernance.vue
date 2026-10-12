<template>
  <div class="workbench-model-governance">
    <!-- 顶部元数据条 -->
    <div class="meta-bar">
      <div class="meta-item">
        <span class="meta-label">数据时点</span>
        <span class="meta-value mono">{{ formatTime(overview.as_of) }}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">模型版本</span>
        <span class="meta-value mono">{{ overview.model_version || '-' }}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">来源</span>
        <span class="meta-value">{{ overview.source || '-' }}</span>
      </div>
    </div>

    <!-- ============================================================ -->
    <!-- 模块 1：P3 门禁状态 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-shield-halved"></i>
        <span>P3 工程门禁</span>
        <span class="block-subtitle">前置 P0/P1/P2 认证未通过时阻断 P3 正式启用</span>
      </div>
      <div class="gate-grid">
        <div class="gate-block">
          <div class="block-name">门禁状态</div>
          <span :class="`gate-badge gate-${p3Gate.status}`">
            {{ gateStatusLabel(p3Gate.status) }}
          </span>
        </div>
        <div class="gate-block">
          <div class="block-name">P2 整体认证</div>
          <span :class="`gate-badge ${p3Gate.p2_overall_passed ? 'gate-ok' : 'gate-blocked'}`">
            {{ p3Gate.p2_overall_passed ? '已通过' : '未通过' }}
          </span>
        </div>
        <div class="gate-block">
          <div class="block-name">阻断状态</div>
          <span :class="`gate-badge ${p3Gate.is_blocked ? 'gate-blocked' : 'gate-ok'}`">
            {{ p3Gate.is_blocked ? '阻断中' : '已放行' }}
          </span>
        </div>
        <div class="gate-block">
          <div class="block-name">失败原因</div>
          <span class="gate-reason">{{ p3Gate.reason || '无' }}</span>
        </div>
      </div>
      <div class="gate-artifact">
        <i class="fas fa-link"></i>
        <span class="artifact-label">认证文件：</span>
        <span class="artifact-value mono">{{ p3Gate.artifact_path || '未配置' }}</span>
      </div>
    </section>

    <!-- ============================================================ -->
    <!-- 模块 2：模型版本管理 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-code-branch"></i>
        <span>模型版本管理</span>
        <span class="block-subtitle">draft → unit_validated → shadow → independent_validated → approved → active → suspended → retired</span>
        <a-button size="small" @click="$emit('refresh-models')" :loading="loadingModels">
          <template #icon><i class="fas fa-sync"></i></template>
          刷新
        </a-button>
      </div>

      <!-- 模型注册表摘要 -->
      <div class="registry-summary">
        <div class="summary-item">
          <span class="summary-label">模型族数</span>
          <span class="summary-value">{{ overview.model_registry_summary?.model_families ?? 0 }}</span>
        </div>
        <div class="summary-item">
          <span class="summary-label">版本总数</span>
          <span class="summary-value">{{ overview.model_registry_summary?.total_versions ?? 0 }}</span>
        </div>
        <div class="summary-item">
          <span class="summary-label">当前列表</span>
          <span class="summary-value">{{ models.length }} 条</span>
        </div>
      </div>

      <!-- 模型版本表 -->
      <div v-if="!models.length" class="block-empty">
        {{ loadingModels ? '加载中...' : '暂无本地证据' }}
      </div>
      <div v-else class="models-table">
        <table>
          <thead>
            <tr>
              <th>模型 ID</th>
              <th>域</th>
              <th>版本</th>
              <th>负责人</th>
              <th>状态</th>
              <th>生效自</th>
              <th>回滚版本</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in models.slice(0, 50)" :key="`${m.model_id}-${m.version}`">
              <td class="mono">{{ m.model_id }}</td>
              <td>{{ m.domain || '-' }}</td>
              <td class="mono">{{ m.version }}</td>
              <td>{{ m.owner || '-' }}</td>
              <td>
                <span :class="`status-badge status-${m.status}`">{{ modelStatusLabel(m.status) }}</span>
              </td>
              <td>{{ m.effective_from || '-' }}</td>
              <td class="mono">{{ m.rollback_version || '-' }}</td>
              <td>{{ formatTime(m.last_transition_at || m.created_at || '') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ============================================================ -->
    <!-- 模块 3：数据健康和降级状态 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-heart-pulse"></i>
        <span>数据健康和降级状态</span>
        <span v-if="dataHealth.degraded" class="warning-tag">降级中</span>
      </div>

      <!-- 模型状态聚合 -->
      <div class="model-status-grid">
        <div class="status-block">
          <div class="block-name">激活模型</div>
          <div class="block-value">{{ dataHealth.model_status_summary?.active_count ?? 0 }}</div>
        </div>
        <div class="status-block" :class="{ 'has-issue': (dataHealth.model_status_summary?.suspended_count ?? 0) > 0 }">
          <div class="block-name">暂停模型</div>
          <div class="block-value">{{ dataHealth.model_status_summary?.suspended_count ?? 0 }}</div>
        </div>
        <div class="status-block">
          <div class="block-name">存在暂停</div>
          <div class="block-value">
            <span :class="dataHealth.model_status_summary?.any_suspended ? 'warn-text' : ''">
              {{ dataHealth.model_status_summary?.any_suspended ? '是' : '否' }}
            </span>
          </div>
        </div>
      </div>

      <!-- 数据健康详情 -->
      <div v-if="dataHealth.degraded" class="degraded-banner">
        <i class="fas fa-circle-info"></i>
        <span>数据健康查询失败：{{ dataHealth.degradation_reason || '未知原因' }}</span>
      </div>
      <div v-else-if="!dataHealth.data_health || !Object.keys(dataHealth.data_health).length" class="block-empty">
        暂无本地证据
      </div>
      <div v-else class="data-health-detail">
        <pre class="health-json">{{ formatJson(dataHealth.data_health) }}</pre>
      </div>
    </section>

    <!-- 限制说明 -->
    <div v-if="overview.limitations && overview.limitations.length" class="limitations-bar">
      <span class="lim-label">限制:</span>
      <span v-for="(lim, idx) in overview.limitations" :key="`lim-${idx}`" class="lim-tag">
        {{ lim }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * WorkbenchModelGovernance 组件。
 *
 * P3.7 工作台治理模块：
 * 1. P3 工程门禁状态（前置 P0/P1/P2 认证）
 * 2. 模型版本管理（注册表 + 状态机）
 * 3. 数据健康和降级状态（来自 data_health_service）
 *
 * 字段缺失时显示「暂无本地证据」；门禁阻断时显示明显徽章。
 */
import { computed } from 'vue';
import {
  type WorkbenchOverviewResponse,
  type DataHealthResponse,
  type ModelVersionRecord,
  type P3GateResult,
} from '@/api/researchWorkbench';
import { formatTime } from '@/utils/format';

/** Props：父组件传入的预加载数据。 */
const props = defineProps<{
  overview: WorkbenchOverviewResponse;
  dataHealth: DataHealthResponse;
  models: ModelVersionRecord[];
  loadingModels?: boolean;
}>();

/** Emits：刷新模型列表。 */
defineEmits<{
  (e: 'refresh-models'): void;
}>();

/** P3 门禁结果。 */
const p3Gate = computed<P3GateResult>(() => props.overview.p3_gate || {
  status: 'unknown',
  p2_overall_passed: false,
  reason: '未查询到 P3 门禁状态',
  artifact_path: '',
  is_blocked: true,
});

/** 门禁状态中文标签。 */
function gateStatusLabel(status: string): string {
  const map: Record<string, string> = {
    ok: '已通过',
    pending_p2: 'P2 待认证',
    pending_p1: 'P1 待认证',
    pending_p0: 'P0 待认证',
    unknown: '未知',
  };
  return map[status] || status;
}

/** 模型状态中文标签。 */
function modelStatusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: '草稿',
    unit_validated: '单元验证',
    shadow: '影子运行',
    independent_validated: '独立验证',
    approved: '已批准',
    active: '已激活',
    suspended: '已暂停',
    retired: '已退役',
  };
  return map[status] || status;
}

/** 格式化 JSON 输出（用于数据健康详情）。 */
function formatJson(obj: unknown): string {
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return '{}';
  }
}
</script>

<style scoped>
.workbench-model-governance {
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
  flex: 1;
}
.warning-tag {
  font-size: 11px;
  padding: 2px 6px;
  background: rgba(250, 173, 20, 0.18);
  color: #faad14;
  border-radius: 3px;
  font-weight: 500;
}
.block-empty {
  color: #787b86;
  font-size: 13px;
  text-align: center;
  padding: 24px 0;
}

/* P3 门禁 */
.gate-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.gate-block {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 10px;
}
.block-name {
  font-size: 11px;
  color: #787b86;
  margin-bottom: 6px;
}
.gate-badge {
  display: inline-block;
  font-size: 13px;
  padding: 4px 10px;
  border-radius: 4px;
  font-weight: 600;
}
.gate-ok {
  background: rgba(82, 196, 26, 0.18);
  color: #52c41a;
}
.gate-blocked, .gate-pending_p2, .gate-pending_p1, .gate-pending_p0 {
  background: rgba(255, 77, 79, 0.18);
  color: #ff4d4f;
}
.gate-unknown {
  background: rgba(120, 123, 134, 0.18);
  color: #787b86;
}
.gate-reason {
  font-size: 12px;
  color: #d1d4dc;
  word-break: break-all;
  display: inline-block;
  max-width: 100%;
}
.gate-artifact {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px dashed #1f2430;
  font-size: 11px;
  color: #787b86;
}
.gate-artifact i {
  color: #5470c6;
}
.artifact-label {
  color: #787b86;
}
.artifact-value {
  color: #d1d4dc;
  word-break: break-all;
}

/* 摘要 */
.registry-summary {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.summary-item {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.summary-label {
  font-size: 11px;
  color: #787b86;
}
.summary-value {
  font-size: 18px;
  font-weight: 700;
  color: #5470c6;
  font-family: monospace;
}

/* 模型表 */
.models-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.models-table th,
.models-table td {
  padding: 6px 8px;
  text-align: left;
  border-bottom: 1px solid #1f2430;
}
.models-table th {
  color: #787b86;
  font-weight: 600;
  background: rgba(20, 30, 50, 0.3);
}
.models-table td {
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

/* 数据健康 */
.model-status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.status-block {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 10px;
  text-align: center;
}
.status-block.has-issue {
  border-color: rgba(255, 77, 79, 0.3);
  background: rgba(255, 77, 79, 0.05);
}
.status-block .block-value {
  font-size: 24px;
  font-weight: 700;
  color: #5470c6;
  font-family: monospace;
  margin-top: 4px;
}
.warn-text {
  color: #ff4d4f;
  font-weight: 700;
}

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
  margin-bottom: 12px;
}

.data-health-detail {
  background: rgba(0, 0, 0, 0.3);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 12px;
  max-height: 400px;
  overflow: auto;
}
.health-json {
  font-family: monospace;
  font-size: 11px;
  color: #d1d4dc;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
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
