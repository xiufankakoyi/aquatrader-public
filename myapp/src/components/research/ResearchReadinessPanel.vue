<template>
  <div class="readiness-panel">
    <div class="panel-header">
      <div class="panel-title">
        <i class="fas fa-shield-halved"></i>
        研究 Readiness 八维面板
      </div>
      <div class="panel-subtitle">
        每项必须同时返回 status / score / missing_items / evidence_refs；score=null 表示不可计算
      </div>
    </div>

    <div v-if="!hasDimensions" class="readiness-empty">
      <i class="fas fa-circle-exclamation"></i>
      <span>暂无本地证据</span>
    </div>

    <div v-else class="readiness-grid">
      <div
        v-for="dim in dimensions"
        :key="dim.key"
        class="readiness-card"
        :class="`status-${dim.status}`"
        :title="dim.tooltip"
      >
        <div class="card-header">
          <span class="card-title">{{ dim.label }}</span>
          <span class="card-status" :class="`badge-${dim.status}`">{{ statusLabel(dim.status) }}</span>
        </div>
        <div class="card-score">
          <span v-if="dim.score === null || dim.score === undefined" class="score-na">不可计算</span>
          <span v-else class="score-value">{{ formatScore(dim.score) }}</span>
        </div>
        <div class="card-bar" v-if="dim.score !== null && dim.score !== undefined">
          <div
            class="bar-fill"
            :class="`bar-${dim.status}`"
            :style="{ width: `${Math.min(100, Math.max(0, dim.score * 100))}%` }"
          ></div>
        </div>
        <div class="card-missing" v-if="dim.missing_items.length">
          <div class="missing-label">缺口 ({{ dim.missing_items.length }})</div>
          <div class="missing-tags">
            <span v-for="item in dim.missing_items.slice(0, 4)" :key="item" class="missing-tag">
              {{ item }}
            </span>
            <span v-if="dim.missing_items.length > 4" class="missing-tag more">
              +{{ dim.missing_items.length - 4 }}
            </span>
          </div>
        </div>
        <div class="card-evidence" v-if="dim.evidence_refs.length">
          <div class="evidence-label">证据 {{ dim.evidence_refs.length }} 条</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { formatScore } from '@/utils/format';
import type { ResearchReadiness, ReadinessDimension } from '@/api/companyResearch';

interface Props {
  /** 八维 readiness 数据；为空时显示「暂无本地证据」。 */
  readiness?: ResearchReadiness | null | undefined;
}

const props = defineProps<Props>();

/** 八维度的展示元信息：key -> label + tooltip。 */
const DIMENSION_META: { key: string; label: string; tooltip: string }[] = [
  { key: 'fact_completeness', label: '事实完整度', tooltip: '已验证事实/观点/假设的覆盖度；技术状态不参与此项。' },
  { key: 'source_traceability', label: '来源可追溯率', tooltip: '来源字段（provider/source/source_trace_id）覆盖比例。' },
  { key: 'source_independence', label: '来源独立性', tooltip: '独立来源数量与发布渠道多样性。' },
  { key: 'pit_integrity', label: 'PIT 完整性', tooltip: '未来数据是否被严格排除；记录时间是否清晰。' },
  { key: 'forecast_input_completeness', label: '前瞻驱动完整度', tooltip: '产能/销量/ASP/份额/成本等前瞻证据；市场隐含场景不参与此项。' },
  { key: 'model_computability', label: '模型可计算性', tooltip: '盈利模型可计算性；市场隐含场景只能提升 market_expectation_computability 子项。' },
  { key: 'valuation_computability', label: '估值可计算性', tooltip: '估值输入是否足以计算 PE/PB/市值等指标。' },
  { key: 'research_uncertainty', label: '研究不确定性', tooltip: '不确定性综合评估；所有维度均缺失时 score=1.0，不返回中性 0.5。' },
];

/** 合并 readiness 与元信息，过滤空值。 */
const dimensions = computed<Array<ReadinessDimension & { key: string; label: string; tooltip: string }>>(() => {
  const readiness = props.readiness;
  if (!readiness || typeof readiness !== 'object') return [];
  const list: Array<ReadinessDimension & { key: string; label: string; tooltip: string }> = [];
  for (const meta of DIMENSION_META) {
    const dim = readiness[meta.key];
    if (!dim || typeof dim !== 'object') continue;
    list.push({
      key: meta.key,
      label: meta.label,
      tooltip: meta.tooltip,
      status: String(dim.status || 'not_computable'),
      score: dim.score === null || dim.score === undefined ? null : Number(dim.score),
      missing_items: Array.isArray(dim.missing_items) ? dim.missing_items.map(String) : [],
      evidence_refs: Array.isArray(dim.evidence_refs) ? dim.evidence_refs.map(String) : [],
    });
  }
  return list;
});

const hasDimensions = computed(() => dimensions.value.length > 0);

/** 状态中文标签。 */
function statusLabel(status: string): string {
  const map: Record<string, string> = {
    ok: '完整',
    partial: '部分',
    blocked: '阻断',
    not_computable: '不可计算',
  };
  return map[status] || status;
}
</script>

<style scoped>
.readiness-panel {
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 8px;
  padding: 16px;
}

.panel-header {
  margin-bottom: 14px;
}

.panel-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
}

.panel-title i {
  color: #52c41a;
}

.panel-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: #787b86;
  line-height: 1.5;
}

.readiness-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px;
  color: #787b86;
  font-size: 14px;
}

.readiness-empty i {
  font-size: 24px;
  color: #faad14;
}

.readiness-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

@media (max-width: 1440px) {
  .readiness-grid {
    grid-template-columns: repeat(4, 1fr);
  }
}

@media (max-width: 1280px) {
  .readiness-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 768px) {
  .readiness-grid {
    grid-template-columns: 1fr;
  }
}

.readiness-card {
  background: rgba(20, 30, 50, 0.5);
  border: 1px solid #2a2e39;
  border-left-width: 3px;
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: all 0.2s ease;
}

.readiness-card:hover {
  background: rgba(20, 30, 50, 0.8);
  border-color: #3a3e49;
}

.readiness-card.status-ok {
  border-left-color: #52c41a;
}

.readiness-card.status-partial {
  border-left-color: #faad14;
}

.readiness-card.status-blocked {
  border-left-color: #ff4d4f;
}

.readiness-card.status-not_computable {
  border-left-color: #787b86;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.card-title {
  font-size: 13px;
  color: #d1d4dc;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-status {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 500;
  white-space: nowrap;
}

.badge-ok {
  background: rgba(82, 196, 26, 0.18);
  color: #52c41a;
}

.badge-partial {
  background: rgba(250, 173, 20, 0.18);
  color: #faad14;
}

.badge-blocked {
  background: rgba(255, 77, 79, 0.18);
  color: #ff4d4f;
}

.badge-not_computable {
  background: rgba(120, 123, 134, 0.18);
  color: #787b86;
}

.card-score {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.score-value {
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  font-variant-numeric: tabular-nums;
}

.score-na {
  font-size: 14px;
  color: #787b86;
  font-weight: 500;
}

.card-bar {
  height: 4px;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 2px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.3s ease;
}

.bar-ok {
  background: linear-gradient(90deg, #52c41a, #95de64);
}

.bar-partial {
  background: linear-gradient(90deg, #faad14, #ffd666);
}

.bar-blocked {
  background: linear-gradient(90deg, #ff4d4f, #ff7875);
}

.bar-not_computable {
  background: linear-gradient(90deg, #787b86, #a0a4b0);
}

.card-missing {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.missing-label {
  font-size: 11px;
  color: #787b86;
}

.missing-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.missing-tag {
  font-size: 11px;
  padding: 1px 6px;
  background: rgba(255, 77, 79, 0.1);
  color: #ffa39e;
  border-radius: 3px;
  border: 1px solid rgba(255, 77, 79, 0.2);
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.missing-tag.more {
  background: rgba(120, 123, 134, 0.1);
  color: #a0a4b0;
  border-color: rgba(120, 123, 134, 0.2);
}

.card-evidence {
  display: flex;
  align-items: center;
}

.evidence-label {
  font-size: 11px;
  color: #5470c6;
}
</style>
