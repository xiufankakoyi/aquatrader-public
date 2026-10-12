<template>
  <div class="workbench-risk-calendar">
    <!-- 顶部元数据条 -->
    <div class="meta-bar">
      <div class="meta-item">
        <span class="meta-label">数据时点</span>
        <span class="meta-value mono">{{ formatTime(riskMeta.as_of) }}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">模型版本</span>
        <span class="meta-value mono">{{ riskMeta.model_version || '-' }}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">来源</span>
        <span class="meta-value">{{ riskMeta.source || '-' }}</span>
      </div>
      <div class="meta-item" v-if="riskMeta.degraded">
        <span class="status-badge status-degraded">降级</span>
      </div>
    </div>

    <!-- 降级提示 -->
    <div v-if="riskMeta.degraded" class="degraded-banner">
      <i class="fas fa-circle-info"></i>
      <span>风险日历处于降级状态：{{ riskMeta.degradation_reason || '未知原因' }}</span>
    </div>

    <!-- ============================================================ -->
    <!-- 模块 1：风险和验证日历 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-calendar-days"></i>
        <span>风险和验证日历</span>
        <span class="block-subtitle">未来验证事件、风险事件和催化事件</span>
      </div>

      <!-- 即将到来事件数 -->
      <div class="upcoming-summary">
        <div class="summary-item">
          <span class="summary-label">即将到来事件</span>
          <span class="summary-value">{{ riskCalendar.upcoming_count ?? 0 }}</span>
        </div>
        <div class="summary-item">
          <span class="summary-label">总事件数</span>
          <span class="summary-value">{{ riskEvents.length }}</span>
        </div>
      </div>

      <!-- 事件列表 -->
      <div v-if="!riskEvents.length" class="block-empty">暂无本地证据</div>
      <div v-else class="events-timeline">
        <div
          v-for="event in sortedRiskEvents.slice(0, 50)"
          :key="event.event_id"
          class="event-item"
          :class="`event-${event.type || 'unknown'}`"
        >
          <div class="event-date">
            <span class="date-day">{{ formatDay(event.date) }}</span>
            <span class="date-month">{{ formatMonth(event.date) }}</span>
          </div>
          <div class="event-content">
            <div class="event-header">
              <span class="event-type-tag" :class="`type-${event.type || 'unknown'}`">
                {{ eventTypeLabel(event.type) }}
              </span>
              <span class="event-title">{{ event.title || '未命名事件' }}</span>
              <span v-if="event.symbol" class="event-symbol mono">{{ event.symbol }}</span>
              <span :class="`event-status status-${event.status || 'pending'}`">
                {{ eventStatusLabel(event.status) }}
              </span>
            </div>
            <div v-if="event.description" class="event-description">{{ event.description }}</div>
            <div class="event-meta">
              <span class="meta-text">来源: {{ event.source || '-' }}</span>
              <span v-if="event.trade_action_free" class="trade-action-free">
                <i class="fas fa-shield-halved"></i> 无交易动作措辞
              </span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ============================================================ -->
    <!-- 模块 2：历史预测与实际兑现记分卡 -->
    <!-- ============================================================ -->
    <section class="block-card">
      <div class="block-title">
        <i class="fas fa-chart-line"></i>
        <span>历史预测与实际兑现记分卡</span>
        <span class="block-subtitle warning-tag">不构成未来预测能力的保证</span>
      </div>

      <!-- 记分卡摘要 -->
      <div class="scorecard-summary">
        <div class="summary-item">
          <span class="summary-label">预测总数</span>
          <span class="summary-value">{{ scorecardData.summary?.total_predictions ?? 0 }}</span>
        </div>
        <div class="summary-item">
          <span class="summary-label">命中数</span>
          <span class="summary-value hit">{{ scorecardData.summary?.hit_count ?? 0 }}</span>
        </div>
        <div class="summary-item">
          <span class="summary-label">未命中数</span>
          <span class="summary-value miss">{{ scorecardData.summary?.miss_count ?? 0 }}</span>
        </div>
        <div class="summary-item">
          <span class="summary-label">区间覆盖率</span>
          <span class="summary-value">
            {{ formatCoverageRate(scorecardData.summary?.coverage_rate) }}
          </span>
        </div>
        <div class="summary-item">
          <span class="summary-label">平均绝对误差</span>
          <span class="summary-value">
            {{ formatMae(scorecardData.summary?.mean_absolute_error) }}
          </span>
        </div>
      </div>

      <!-- 记分卡表格 -->
      <div v-if="!scorecardItems.length" class="block-empty">暂无本地证据</div>
      <div v-else class="scorecard-table">
        <table>
          <thead>
            <tr>
              <th>股票代码</th>
              <th>指标</th>
              <th>报告期</th>
              <th class="text-right">预测值</th>
              <th class="text-right">实际值</th>
              <th class="text-right">误差</th>
              <th class="text-right">覆盖率</th>
              <th>命中状态</th>
              <th>预测时间</th>
              <th>兑现时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in scorecardItems.slice(0, 50)" :key="item.prediction_id">
              <td class="mono">{{ item.symbol }}</td>
              <td>{{ item.metric || '-' }}</td>
              <td>{{ item.period || '-' }}</td>
              <td class="text-right">{{ formatNumber(item.predicted) }}</td>
              <td class="text-right">{{ formatNumber(item.actual) }}</td>
              <td class="text-right">{{ formatNumber(item.error) }}</td>
              <td class="text-right">{{ formatPercent(item.coverage_rate) }}</td>
              <td>
                <span :class="`hit-badge hit-${item.hit_status || 'unknown'}`">
                  {{ hitStatusLabel(item.hit_status) }}
                </span>
              </td>
              <td>{{ formatTime(item.predicted_at || '') }}</td>
              <td>{{ formatTime(item.resolved_at || '') }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 命中分布 -->
      <div v-if="scorecardItems.length" class="hit-distribution">
        <div class="distribution-title">命中分布</div>
        <div class="distribution-bar">
          <div
            class="bar-segment hit"
            :style="{ width: hitRate + '%' }"
            :title="`命中 ${hitCount} 条 (${hitRate.toFixed(2)}%)`"
          ></div>
          <div
            class="bar-segment miss"
            :style="{ width: missRate + '%' }"
            :title="`未命中 ${missCount} 条 (${missRate.toFixed(2)}%)`"
          ></div>
        </div>
        <div class="distribution-legend">
          <span class="legend-item">
            <span class="legend-color hit"></span> 命中 {{ hitCount }} 条 ({{ hitRate.toFixed(2) }}%)
          </span>
          <span class="legend-item">
            <span class="legend-color miss"></span> 未命中 {{ missCount }} 条 ({{ missRate.toFixed(2) }}%)
          </span>
        </div>
      </div>
    </section>

    <!-- 限制说明 -->
    <div v-if="riskMeta.limitations && riskMeta.limitations.length" class="limitations-bar">
      <span class="lim-label">限制:</span>
      <span v-for="(lim, idx) in riskMeta.limitations" :key="`lim-${idx}`" class="lim-tag">
        {{ lim }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * WorkbenchRiskCalendar 组件。
 *
 * P3.7 工作台验证治理模块：
 * 1. 风险和验证日历（事件时间线 + 状态徽章）
 * 2. 历史预测与实际兑现记分卡（命中率 + 区间覆盖率 + MAE）
 *
 * 严格遵循 P3.7 传播治理规则：
 * - 风险事件必须 trade_action_free=true，不输出交易动作
 * - 记分卡不构成未来预测能力的保证
 * - 缺数据时显示「暂无本地证据」，不让页面崩溃
 */
import { computed } from 'vue';
import {
  type RiskCalendarResponse,
  type ScorecardResponse,
  type WorkbenchMeta,
} from '@/api/researchWorkbench';
import { formatPercent, formatNumber, formatTime } from '@/utils/format';

/** Props：父组件传入的预加载数据。 */
const props = defineProps<{
  riskCalendarData: RiskCalendarResponse;
  scorecardData: ScorecardResponse;
}>();

/** 风险日历元数据。 */
const riskMeta = computed<WorkbenchMeta>(() => ({
  as_of: props.riskCalendarData.as_of || '',
  model_version: props.riskCalendarData.model_version || '',
  source: props.riskCalendarData.source || '',
  limitations: [
    ...(props.riskCalendarData.limitations || []),
    ...(props.scorecardData.limitations || []),
  ],
  degraded: props.riskCalendarData.degraded || props.scorecardData.degraded,
  degradation_reason: props.riskCalendarData.degradation_reason || props.scorecardData.degradation_reason,
}));

/** 风险日历事件列表。 */
const riskEvents = computed(() => props.riskCalendarData.events || []);

/** 按日期升序排列事件。 */
const sortedRiskEvents = computed(() => {
  const events = [...riskEvents.value];
  events.sort((a, b) => {
    const da = a.date || '';
    const db = b.date || '';
    if (da < db) return -1;
    if (da > db) return 1;
    return 0;
  });
  return events;
});

/** 记分卡条目。 */
const scorecardItems = computed(() => props.scorecardData.scorecard || []);

/** 命中分布派生。 */
const totalCount = computed(() => props.scorecardData.summary?.total_predictions ?? 0);
const hitCount = computed(() => props.scorecardData.summary?.hit_count ?? 0);
const missCount = computed(() => props.scorecardData.summary?.miss_count ?? 0);
const hitRate = computed(() => totalCount.value > 0 ? (hitCount.value / totalCount.value) * 100 : 0);
const missRate = computed(() => totalCount.value > 0 ? (missCount.value / totalCount.value) * 100 : 0);

/** 事件类型中文标签。 */
function eventTypeLabel(type: string | undefined): string {
  const map: Record<string, string> = {
    earnings_release: '财报披露',
    policy_announcement: '政策公告',
    product_launch: '产品发布',
    management_change: '管理层变动',
    risk_event: '风险事件',
    validation_event: '验证事件',
    catalyst: '催化事件',
    invalidation: '失效事件',
  };
  return map[type || 'unknown'] || '其他';
}

/** 事件状态中文标签。 */
function eventStatusLabel(status: string | undefined): string {
  const map: Record<string, string> = {
    pending: '待发生',
    scheduled: '已排期',
    occurred: '已发生',
    cancelled: '已取消',
    missed: '已错过',
    resolved: '已解决',
  };
  return map[status || 'pending'] || '待发生';
}

/** 命中状态中文标签。 */
function hitStatusLabel(status: string | undefined): string {
  const map: Record<string, string> = {
    hit: '命中',
    miss: '未命中',
    partial: '部分命中',
    pending: '待兑现',
    unknown: '未知',
  };
  return map[status || 'unknown'] || '未知';
}

/** 格式化日期中的「日」。 */
function formatDay(date: string | undefined): string {
  if (!date) return '-';
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return '-';
  return String(d.getDate()).padStart(2, '0');
}

/** 格式化日期中的「月」。 */
function formatMonth(date: string | undefined): string {
  if (!date) return '';
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return '';
  const months = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];
  return months[d.getMonth()];
}

/** 覆盖率格式化（null 时显示「暂无」）。 */
function formatCoverageRate(v: number | null | undefined): string {
  if (v === null || v === undefined) return '暂无';
  return formatPercent(v);
}

/** MAE 格式化（null 时显示「暂无」）。 */
function formatMae(v: number | null | undefined): string {
  if (v === null || v === undefined) return '暂无';
  return formatNumber(v);
}
</script>

<style scoped>
.workbench-risk-calendar {
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

/* 摘要 */
.upcoming-summary,
.scorecard-summary {
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
.summary-value.hit { color: #52c41a; }
.summary-value.miss { color: #ff4d4f; }

/* 事件时间线 */
.events-timeline {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.event-item {
  display: grid;
  grid-template-columns: 56px 1fr;
  gap: 12px;
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-left: 3px solid #5470c6;
  border-radius: 4px;
  padding: 10px;
}
.event-item.event-earnings_release { border-left-color: #5470c6; }
.event-item.event-policy_announcement { border-left-color: #9d67ff; }
.event-item.event-risk_event { border-left-color: #ff4d4f; }
.event-item.event-invalidation { border-left-color: #ff4d4f; }
.event-item.event-catalyst { border-left-color: #52c41a; }
.event-item.event-validation_event { border-left-color: #faad14; }
.event-date {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.3);
  border-radius: 4px;
  padding: 4px;
}
.date-day {
  font-size: 20px;
  font-weight: 700;
  color: #5470c6;
  font-family: monospace;
}
.date-month {
  font-size: 11px;
  color: #787b86;
}
.event-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.event-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.event-type-tag {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 500;
}
.type-earnings_release { background: rgba(84, 112, 198, 0.18); color: #5470c6; }
.type-policy_announcement { background: rgba(157, 103, 255, 0.18); color: #9d67ff; }
.type-risk_event { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.type-invalidation { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.type-catalyst { background: rgba(82, 196, 26, 0.18); color: #52c41a; }
.type-validation_event { background: rgba(250, 173, 20, 0.18); color: #faad14; }
.event-title {
  font-size: 13px;
  color: #fff;
  font-weight: 600;
  flex: 1;
}
.event-symbol {
  font-family: monospace;
  font-size: 11px;
  color: #5470c6;
  background: rgba(84, 112, 198, 0.15);
  padding: 1px 6px;
  border-radius: 3px;
}
.event-status {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 3px;
}
.event-status.status-pending, .event-status.status-scheduled { background: rgba(250, 173, 20, 0.18); color: #faad14; }
.event-status.status-occurred, .event-status.status-resolved { background: rgba(82, 196, 26, 0.18); color: #52c41a; }
.event-status.status-cancelled, .event-status.status-missed { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.event-description {
  font-size: 12px;
  color: #d1d4dc;
  line-height: 1.5;
}
.event-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 11px;
  color: #787b86;
  flex-wrap: wrap;
}
.trade-action-free {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: #52c41a;
  font-weight: 500;
}
.meta-text {
  color: #787b86;
}

/* 记分卡表 */
.scorecard-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.scorecard-table th,
.scorecard-table td {
  padding: 6px 8px;
  text-align: left;
  border-bottom: 1px solid #1f2430;
}
.scorecard-table th {
  color: #787b86;
  font-weight: 600;
  background: rgba(20, 30, 50, 0.3);
}
.scorecard-table td {
  color: #d1d4dc;
}
.text-right {
  text-align: right;
  font-family: monospace;
}

/* 命中徽章 */
.hit-badge {
  display: inline-block;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 500;
}
.hit-hit { background: rgba(82, 196, 26, 0.18); color: #52c41a; }
.hit-miss { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.hit-partial { background: rgba(250, 173, 20, 0.18); color: #faad14; }
.hit-pending { background: rgba(120, 123, 134, 0.18); color: #787b86; }
.hit-unknown { background: rgba(120, 123, 134, 0.18); color: #787b86; }

/* 命中分布 */
.hit-distribution {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px dashed #1f2430;
}
.distribution-title {
  font-size: 12px;
  color: #d1d4dc;
  font-weight: 600;
  margin-bottom: 8px;
}
.distribution-bar {
  display: flex;
  height: 10px;
  border-radius: 5px;
  overflow: hidden;
  background: rgba(120, 123, 134, 0.2);
}
.bar-segment {
  transition: width 0.3s ease;
}
.bar-segment.hit {
  background: #52c41a;
}
.bar-segment.miss {
  background: #ff4d4f;
}
.distribution-legend {
  display: flex;
  gap: 16px;
  margin-top: 8px;
  font-size: 11px;
  color: #787b86;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
}
.legend-color {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
}
.legend-color.hit { background: #52c41a; }
.legend-color.miss { background: #ff4d4f; }

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
