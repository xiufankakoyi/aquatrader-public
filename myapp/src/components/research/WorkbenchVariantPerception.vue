<template>
  <div class="workbench-variant-perception">
    <!-- 顶部元数据条 -->
    <div class="meta-bar">
      <div class="meta-item">
        <span class="meta-label">数据时点</span>
        <span class="meta-value mono">{{ formatTime(vpMeta.as_of) }}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">模型版本</span>
        <span class="meta-value mono">{{ vpMeta.model_version || '-' }}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">活跃 VP 数</span>
        <span class="meta-value">{{ vpData.active_count ?? 0 }}</span>
      </div>
      <div class="meta-item" v-if="vpMeta.degraded">
        <span class="status-badge status-degraded">降级</span>
      </div>
    </div>

    <!-- 降级提示 -->
    <div v-if="vpMeta.degraded" class="degraded-banner">
      <i class="fas fa-circle-info"></i>
      <span>VariantPerception 处于降级状态：{{ vpMeta.degradation_reason || '未知原因' }}</span>
    </div>

    <!-- ============================================================ -->
    <!-- 模块 1：VariantPerception 卡片 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-eye"></i>
        <span>VariantPerception 差异化观点</span>
        <span class="block-subtitle">市场观点 vs 内部模型 vs 驱动差异</span>
      </div>
      <div v-if="!perceptions.length" class="block-empty">暂无本地证据</div>
      <div v-else class="vp-grid">
        <div
          v-for="vp in perceptions.slice(0, 12)"
          :key="vp.perception_id"
          class="vp-card"
          :class="`status-${vp.review_status || 'pending'}`"
        >
          <!-- 卡片头部 -->
          <div class="vp-header">
            <span class="vp-symbol mono">{{ vp.symbol }}</span>
            <span class="vp-name">{{ vp.company_name || '未披露' }}</span>
            <span class="vp-status-tag" :class="`tag-${vp.review_status || 'pending'}`">
              {{ reviewStatusLabel(vp.review_status) }}
            </span>
          </div>

          <!-- 卡片元数据 -->
          <div class="vp-meta">
            <span class="meta-text">PIT: {{ vp.as_of || '-' }}</span>
            <span class="meta-text">状态: {{ vp.status || '-' }}</span>
            <span class="meta-text">版本: <span class="mono">{{ vp.version || '-' }}</span></span>
          </div>

          <!-- 三视图分离 -->
          <div class="vp-views">
            <div class="view-block">
              <div class="view-label opinion">
                <i class="fas fa-users"></i> 市场观点
              </div>
              <div class="view-body">
                <pre class="view-json">{{ formatJson(vp.market_view) }}</pre>
              </div>
            </div>
            <div class="view-arrow">
              <i class="fas fa-arrows-left-right"></i>
            </div>
            <div class="view-block">
              <div class="view-label verified">
                <i class="fas fa-microscope"></i> 内部模型
              </div>
              <div class="view-body">
                <pre class="view-json">{{ formatJson(vp.internal_view) }}</pre>
              </div>
            </div>
          </div>

          <!-- 驱动差异 -->
          <div class="vp-section">
            <div class="section-label">
              <i class="fas fa-code-compare"></i> 驱动差异
              <span class="count">{{ vp.driver_differences?.length || 0 }}</span>
            </div>
            <div class="section-body">
              <div v-if="!vp.driver_differences?.length" class="section-empty">暂无</div>
              <div v-for="(d, i) in (vp.driver_differences || []).slice(0, 5)" :key="`dd-${i}`" class="diff-item">
                {{ safeString(d?.metric || d?.variable_name || d?.name, '未命名') }}
              </div>
            </div>
          </div>

          <!-- 催化剂 -->
          <div class="vp-section">
            <div class="section-label">
              <i class="fas fa-bolt"></i> 催化事件
              <span class="count">{{ vp.catalysts?.length || 0 }}</span>
            </div>
            <div class="section-body">
              <div v-if="!vp.catalysts?.length" class="section-empty">暂无</div>
              <div v-for="(c, i) in (vp.catalysts || []).slice(0, 5)" :key="`cat-${i}`" class="catalyst-item">
                <span class="catalyst-text">{{ safeString(c?.event_type || c?.title || c?.description, '未命名事件') }}</span>
                <span v-if="c?.scheduled_at" class="catalyst-date">{{ c.scheduled_at }}</span>
              </div>
            </div>
            <div class="vp-disclaimer">催化事件仅描述验证事件，不输出交易动作</div>
          </div>

          <!-- 失效条件 -->
          <div class="vp-section">
            <div class="section-label warning-label">
              <i class="fas fa-triangle-exclamation"></i> 失效条件
              <span class="count">{{ vp.invalidation?.length || 0 }}</span>
            </div>
            <div class="section-body">
              <div v-if="!vp.invalidation?.length" class="section-empty">暂无</div>
              <div v-for="(inv, i) in (vp.invalidation || []).slice(0, 5)" :key="`inv-${i}`" class="invalidation-item">
                {{ safeString(inv?.condition || inv?.description || inv, '未命名') }}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ============================================================ -->
    <!-- 模块 2：反方问题 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-circle-question"></i>
        <span>反方问题</span>
        <span class="block-subtitle">投委会视角的逆向验证问题</span>
      </div>
      <div v-if="!bearQuestions.length" class="block-empty">暂无本地证据</div>
      <div v-else class="questions-list">
        <div
          v-for="q in bearQuestions.slice(0, 30)"
          :key="q.question_id"
          class="question-item"
          :class="`q-status-${q.status || 'open'}`"
        >
          <div class="q-header">
            <span class="q-symbol mono">{{ q.symbol }}</span>
            <span class="q-status" :class="`tag-${q.status || 'open'}`">
              {{ questionStatusLabel(q.status) }}
            </span>
          </div>
          <div class="q-text">{{ q.question }}</div>
          <div class="q-meta">
            <span class="meta-text">提问人: {{ q.raised_by || '-' }}</span>
            <span class="meta-text">提问时间: {{ formatTime(q.raised_at || '') }}</span>
          </div>
          <div v-if="q.answer" class="q-answer">
            <div class="answer-label">回答:</div>
            <div class="answer-text">{{ q.answer }}</div>
            <div class="q-meta">
              <span class="meta-text">回答人: {{ q.answered_by || '-' }}</span>
              <span class="meta-text">回答时间: {{ formatTime(q.answered_at || '') }}</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ============================================================ -->
    <!-- 模块 3：投委会状态 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-users-rectangle"></i>
        <span>投委会状态</span>
        <span class="block-subtitle">研究决策治理委员会</span>
      </div>
      <div class="committee-grid">
        <div class="committee-block">
          <div class="block-name">委员会状态</div>
          <span :class="`committee-badge committee-${committeeStatus.state}`">
            {{ committeeStateLabel(committeeStatus.state) }}
          </span>
        </div>
        <div class="committee-block">
          <div class="block-name">待处理问题</div>
          <div class="block-value">{{ committeeStatus.pending_questions ?? 0 }}</div>
        </div>
        <div class="committee-block">
          <div class="block-name">最近召开时间</div>
          <div class="block-value">
            <span v-if="committeeStatus.last_convened_at">{{ formatTime(committeeStatus.last_convened_at) }}</span>
            <span v-else class="empty-text">未召开</span>
          </div>
        </div>
        <div class="committee-block">
          <div class="block-name">委员会成员</div>
          <div class="block-value">{{ committeeStatus.members?.length ?? 0 }} 人</div>
        </div>
      </div>

      <!-- 成员列表 -->
      <div v-if="committeeStatus.members && committeeStatus.members.length" class="members-list">
        <div class="members-title">成员列表</div>
        <div class="members-grid">
          <div v-for="(m, idx) in committeeStatus.members" :key="`member-${idx}`" class="member-item">
            <i class="fas fa-user"></i>
            <span class="member-name">{{ safeString(m?.name || m?.member_id || m?.user_id, '未命名') }}</span>
            <span v-if="m?.role" class="member-role">{{ m.role }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 限制说明 -->
    <div v-if="vpMeta.limitations && vpMeta.limitations.length" class="limitations-bar">
      <span class="lim-label">限制:</span>
      <span v-for="(lim, idx) in vpMeta.limitations" :key="`lim-${idx}`" class="lim-tag">
        {{ lim }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * WorkbenchVariantPerception 组件。
 *
 * P3.7 工作台观点治理模块：
 * 1. VariantPerception 卡片（市场观点 vs 内部模型 vs 驱动差异）
 * 2. 反方问题列表（投委会视角的逆向验证）
 * 3. 投委会状态（委员会召开情况 + 成员）
 *
 * 严格遵循 P3.7 传播治理规则：
 * - 催化事件仅描述验证事件，不输出交易动作
 * - 市场观点不可由系统臆测，缺数据时显示「暂无本地证据」
 * - 未完成复核的内容带明显状态标签（待复核/已复核/已驳回）
 */
import { computed } from 'vue';
import {
  type VariantPerceptionResponse,
  type BearQuestionsResponse,
  type WorkbenchMeta,
} from '@/api/researchWorkbench';
import { formatTime, formatSafe } from '@/utils/format';

/** Props：父组件传入的预加载数据。 */
const props = defineProps<{
  vpData: VariantPerceptionResponse;
  bearQuestionsData: BearQuestionsResponse;
}>();

/** VP 元数据。 */
const vpMeta = computed<WorkbenchMeta>(() => ({
  as_of: props.vpData.as_of || '',
  model_version: props.vpData.model_version || '',
  source: props.vpData.source || '',
  limitations: props.vpData.limitations || [],
  degraded: props.vpData.degraded || props.bearQuestionsData.degraded,
  degradation_reason: props.vpData.degradation_reason || props.bearQuestionsData.degradation_reason,
}));

/** VariantPerception 列表。 */
const perceptions = computed(() => props.vpData.perceptions || []);

/** 反方问题列表。 */
const bearQuestions = computed(() => props.bearQuestionsData.bear_questions || []);

/** 投委会状态。 */
const committeeStatus = computed(() => props.bearQuestionsData.committee_status || {
  state: 'not_convened',
  pending_questions: 0,
  last_convened_at: '',
  members: [],
});

/** 复核状态中文标签。 */
function reviewStatusLabel(status: string | undefined): string {
  const map: Record<string, string> = {
    pending: '待复核',
    in_review: '复核中',
    approved: '已复核',
    rejected: '已驳回',
    superseded: '已替代',
    invalidated: '已失效',
  };
  return map[status || 'pending'] || '待复核';
}

/** 问题状态中文标签。 */
function questionStatusLabel(status: string | undefined): string {
  const map: Record<string, string> = {
    open: '待回答',
    answered: '已回答',
    closed: '已关闭',
    escalated: '已升级',
  };
  return map[status || 'open'] || '待回答';
}

/** 投委会状态中文标签。 */
function committeeStateLabel(state: string): string {
  const map: Record<string, string> = {
    not_convened: '未召开',
    scheduled: '已排期',
    in_session: '召开中',
    concluded: '已结束',
    cancelled: '已取消',
  };
  return map[state] || state;
}

/** 安全字符串。 */
function safeString(v: unknown, fallback = '-'): string {
  if (v === null || v === undefined || v === '') return fallback;
  return String(v);
}

/** 格式化 JSON 输出。 */
function formatJson(obj: unknown): string {
  if (obj === null || obj === undefined) return '暂无';
  try {
    if (typeof obj === 'string') return obj;
    return JSON.stringify(obj, null, 2);
  } catch {
    return '{}';
  }
}
</script>

<style scoped>
.workbench-variant-perception {
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
.block-empty {
  color: #787b86;
  font-size: 13px;
  text-align: center;
  padding: 24px 0;
}

/* VP 卡片 */
.vp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
  gap: 12px;
}
@media (max-width: 720px) {
  .vp-grid {
    grid-template-columns: 1fr;
  }
}
.vp-card {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 12px;
}
.vp-card.status-pending {
  border-color: rgba(250, 173, 20, 0.3);
}
.vp-card.status-approved {
  border-color: rgba(82, 196, 26, 0.3);
}
.vp-card.status-rejected {
  border-color: rgba(255, 77, 79, 0.3);
}
.vp-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid #2a2e39;
}
.vp-symbol {
  font-family: monospace;
  font-size: 13px;
  color: #5470c6;
  background: rgba(84, 112, 198, 0.15);
  padding: 2px 6px;
  border-radius: 3px;
}
.vp-name {
  font-size: 14px;
  color: #fff;
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vp-status-tag {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 500;
}
.tag-pending { background: rgba(250, 173, 20, 0.18); color: #faad14; }
.tag-in_review { background: rgba(84, 112, 198, 0.18); color: #5470c6; }
.tag-approved { background: rgba(82, 196, 26, 0.18); color: #52c41a; }
.tag-rejected { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.tag-superseded, .tag-invalidated { background: rgba(120, 123, 134, 0.18); color: #787b86; }

.vp-meta {
  display: flex;
  gap: 12px;
  font-size: 11px;
  color: #787b86;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

/* 三视图分离 */
.vp-views {
  display: grid;
  grid-template-columns: 1fr 24px 1fr;
  gap: 8px;
  margin-bottom: 12px;
}
@media (max-width: 720px) {
  .vp-views {
    grid-template-columns: 1fr;
  }
  .view-arrow {
    display: none;
  }
}
.view-block {
  background: rgba(0, 0, 0, 0.2);
  border-radius: 4px;
  padding: 8px;
}
.view-label {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.view-label.opinion {
  color: #faad14;
}
.view-label.verified {
  color: #52c41a;
}
.view-body {
  max-height: 160px;
  overflow: auto;
}
.view-json {
  font-family: monospace;
  font-size: 10px;
  color: #d1d4dc;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.view-arrow {
  display: flex;
  align-items: center;
  justify-content: center;
  color: #5470c6;
  font-size: 14px;
}

/* VP 子区段 */
.vp-section {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #1f2430;
}
.section-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #d1d4dc;
  font-weight: 600;
  margin-bottom: 4px;
}
.section-label.warning-label {
  color: #faad14;
}
.section-label i {
  color: #5470c6;
}
.warning-label i {
  color: #faad14;
}
.count {
  font-size: 11px;
  color: #787b86;
  font-family: monospace;
  background: rgba(120, 123, 134, 0.1);
  padding: 1px 6px;
  border-radius: 8px;
}
.section-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.section-empty {
  font-size: 11px;
  color: #787b86;
}
.diff-item {
  font-size: 11px;
  color: #d1d4dc;
  padding: 2px 0;
}
.catalyst-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  color: #d1d4dc;
  padding: 2px 0;
  gap: 8px;
}
.catalyst-text {
  flex: 1;
}
.catalyst-date {
  color: #787b86;
  font-family: monospace;
}
.vp-disclaimer {
  font-size: 10px;
  color: #787b86;
  font-style: italic;
  margin-top: 4px;
  padding: 4px 6px;
  background: rgba(120, 123, 134, 0.08);
  border-radius: 3px;
}
.invalidation-item {
  font-size: 11px;
  color: #faad14;
  padding: 2px 0;
}

/* 反方问题 */
.questions-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.question-item {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-left: 3px solid #5470c6;
  border-radius: 4px;
  padding: 10px;
}
.question-item.q-status-open {
  border-left-color: #faad14;
}
.question-item.q-status-answered {
  border-left-color: #52c41a;
}
.question-item.q-status-closed {
  border-left-color: #787b86;
  opacity: 0.7;
}
.q-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}
.q-symbol {
  font-family: monospace;
  font-size: 12px;
  color: #5470c6;
  background: rgba(84, 112, 198, 0.15);
  padding: 2px 6px;
  border-radius: 3px;
}
.q-status {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 3px;
}
.q-text {
  font-size: 13px;
  color: #d1d4dc;
  margin-bottom: 6px;
}
.q-meta {
  display: flex;
  gap: 12px;
  font-size: 11px;
  color: #787b86;
  flex-wrap: wrap;
}
.meta-text {
  color: #787b86;
}
.q-answer {
  margin-top: 6px;
  padding: 6px 8px;
  background: rgba(82, 196, 26, 0.05);
  border-left: 2px solid #52c41a;
  border-radius: 0 4px 4px 0;
}
.answer-label {
  font-size: 11px;
  color: #52c41a;
  font-weight: 600;
  margin-bottom: 4px;
}
.answer-text {
  font-size: 12px;
  color: #d1d4dc;
  margin-bottom: 4px;
}

/* 投委会状态 */
.committee-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 10px;
}
.committee-block {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 10px;
  text-align: center;
}
.committee-block .block-name {
  font-size: 11px;
  color: #787b86;
  margin-bottom: 6px;
}
.committee-block .block-value {
  font-size: 16px;
  color: #d1d4dc;
  font-weight: 600;
  word-break: break-all;
}
.committee-badge {
  display: inline-block;
  font-size: 13px;
  padding: 4px 10px;
  border-radius: 4px;
  font-weight: 600;
}
.committee-not_convened { background: rgba(120, 123, 134, 0.18); color: #787b86; }
.committee-scheduled { background: rgba(84, 112, 198, 0.18); color: #5470c6; }
.committee-in_session { background: rgba(250, 173, 20, 0.18); color: #faad14; }
.committee-concluded { background: rgba(82, 196, 26, 0.18); color: #52c41a; }
.committee-cancelled { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.empty-text {
  color: #787b86;
  font-style: italic;
}

.members-list {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px dashed #1f2430;
}
.members-title {
  font-size: 12px;
  color: #d1d4dc;
  font-weight: 600;
  margin-bottom: 8px;
}
.members-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 6px;
}
.member-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  background: rgba(255, 255, 255, 0.02);
  border-radius: 4px;
  font-size: 12px;
  color: #d1d4dc;
}
.member-item i {
  color: #5470c6;
  font-size: 10px;
}
.member-name {
  flex: 1;
}
.member-role {
  font-size: 10px;
  color: #787b86;
  background: rgba(120, 123, 134, 0.1);
  padding: 1px 4px;
  border-radius: 3px;
}

/* 状态徽章 */
.status-badge {
  display: inline-block;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 500;
}
.status-degraded { background: rgba(250, 173, 20, 0.18); color: #faad14; }

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
