<template>
  <div class="candidate-tab">
    <div class="candidate-controls">
      <a-space>
        <a-input-search
          v-model:value="searchSymbol"
          placeholder="输入股票代码"
          style="width: 200px"
          @search="loadCandidates"
        />
        <a-select v-model:value="filterStatus" style="width: 120px" placeholder="状态">
          <a-select-option value="">全部</a-select-option>
          <a-select-option value="active">活跃</a-select-option>
          <a-select-option value="degraded">降级</a-select-option>
          <a-select-option value="expired">过期</a-select-option>
        </a-select>
        <a-button @click="loadCandidates">刷新</a-button>
      </a-space>
    </div>

    <a-empty v-if="!candidates.length && !loading" description="暂无候选对象，构建后将显示在此" />

    <a-spin :spinning="loading">
      <div class="candidate-list" v-if="candidates.length">
        <a-card
          v-for="c in candidates"
          :key="c.candidate_id"
          class="candidate-card"
          @click="selectCandidate(c.candidate_id)"
        >
          <template #title>
            <span class="card-title">
              {{ c.symbol }}
              <a-tag :color="statusColor(c.status)">{{ statusLabel(c.status) }}</a-tag>
              <a-tag v-if="c.thesis_status" color="blue">{{ thesisStatusLabel(c.thesis_status) }}</a-tag>
            </span>
          </template>
          <template #extra>
            <span class="score-summary">
              <span class="score-item research">研究 {{ formatScore(c.research_score) }}</span>
              <span class="score-item alpha">Alpha {{ formatScore(c.alpha_score) }}</span>
              <span class="score-item core">核心 {{ formatScore(c.core_score) }}</span>
            </span>
          </template>

          <div class="card-body">
            <div class="info-row">
              <span class="label">板块:</span> {{ c.sector_id || '未指定' }}
            </div>
            <div class="info-row" v-if="c.weakest_assumption">
              <span class="label">最弱假设:</span> {{ c.weakest_assumption }}
            </div>
            <div class="info-row" v-if="c.catalyst_window">
              <span class="label">催化窗口:</span> {{ c.catalyst_window }}
            </div>
            <div class="info-row">
              <span class="label">下次复核:</span> {{ c.next_review_at || '未设置' }}
            </div>
          </div>
        </a-card>
      </div>
    </a-spin>

    <a-drawer
      v-model:open="drawerVisible"
      title="候选详情"
      width="70%"
      placement="right"
    >
      <template v-if="selectedCandidate">
        <a-descriptions :column="2" bordered size="small">
          <a-descriptions-item label="股票代码">{{ selectedCandidate.symbol }}</a-descriptions-item>
          <a-descriptions-item label="板块">{{ selectedCandidate.sector_id }}</a-descriptions-item>
          <a-descriptions-item label="PIT 时点">{{ selectedCandidate.as_of }}</a-descriptions-item>
          <a-descriptions-item label="观点状态">{{ thesisStatusLabel(selectedCandidate.thesis_status) }}</a-descriptions-item>
          <a-descriptions-item label="有效期至">{{ selectedCandidate.expires_at || '未设置' }}</a-descriptions-item>
          <a-descriptions-item label="下次复核">{{ selectedCandidate.next_review_at || '未设置' }}</a-descriptions-item>
        </a-descriptions>

        <a-divider>三类分数</a-divider>
        <div class="score-grid">
          <div class="score-box research">
            <div class="score-title">研究可信度</div>
            <div class="score-value">{{ formatScore(selectedCandidate.research_score) }}</div>
            <div class="score-computability">{{ computabilityLabel(selectedCandidate.research_score.computability) }}</div>
            <div class="score-components">
              <div v-for="(val, key) in selectedCandidate.research_score.components" :key="key">
                {{ key }}: {{ (val * 100).toFixed(1) }}%
              </div>
            </div>
          </div>
          <div class="score-box alpha">
            <div class="score-title">Alpha 预期差</div>
            <div class="score-value">{{ formatScore(selectedCandidate.alpha_score) }}</div>
            <div class="score-computability">{{ computabilityLabel(selectedCandidate.alpha_score.computability) }}</div>
            <div class="score-components">
              <div v-for="(val, key) in selectedCandidate.alpha_score.components" :key="key">
                {{ key }}: {{ (val * 100).toFixed(1) }}%
              </div>
            </div>
          </div>
          <div class="score-box core">
            <div class="score-title">产业核心</div>
            <div class="score-value">{{ formatScore(selectedCandidate.core_score) }}</div>
            <div class="score-computability">{{ computabilityLabel(selectedCandidate.core_score.computability) }}</div>
            <div class="score-components">
              <div v-for="(val, key) in selectedCandidate.core_score.components" :key="key">
                {{ key }}: {{ (val * 100).toFixed(1) }}%
              </div>
            </div>
          </div>
        </div>

        <a-divider>关键信息</a-divider>
        <a-descriptions :column="1" bordered size="small">
          <a-descriptions-item label="最弱假设">{{ selectedCandidate.weakest_assumption || '暂无' }}</a-descriptions-item>
          <a-descriptions-item label="失效条件">
            <div v-for="(cond, i) in selectedCandidate.invalidation_conditions" :key="i">- {{ cond }}</div>
          </a-descriptions-item>
          <a-descriptions-item label="估值风险">
            [{{ selectedCandidate.valuation_risk.low.toFixed(2) }}, {{ selectedCandidate.valuation_risk.high.toFixed(2) }}]
          </a-descriptions-item>
          <a-descriptions-item label="不确定性">
            [{{ selectedCandidate.uncertainty_score.low.toFixed(2) }}, {{ selectedCandidate.uncertainty_score.high.toFixed(2) }}]
          </a-descriptions-item>
        </a-descriptions>

        <a-divider>来源追踪</a-divider>
        <a-tag v-for="trace in selectedCandidate.source_trace" :key="trace" color="cyan">{{ trace }}</a-tag>

        <a-divider>决策追踪</a-divider>
        <div class="decision-trace">
          <div v-for="(trace, i) in selectedCandidate.decision_trace" :key="i">{{ trace }}</div>
        </div>

        <a-divider>人类可读报告</a-divider>
        <a-spin :spinning="reportLoading">
          <div v-if="report" class="report-sections">
            <div v-for="(content, section) in report.report_sections" :key="section" class="report-section">
              <h4>{{ sectionLabel(section as string) }}</h4>
              <pre>{{ content }}</pre>
            </div>
          </div>
          <a-empty v-else description="暂无报告" />
        </a-spin>
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { message } from 'ant-design-vue';
import {
  listCandidates,
  getCandidate,
  getCandidateReport,
  type ResearchCandidate,
  type CandidateReport,
} from '@/api/candidate';

const candidates = ref<ResearchCandidate[]>([]);
const loading = ref(false);
const searchSymbol = ref('');
const filterStatus = ref('');
const drawerVisible = ref(false);
const selectedCandidate = ref<ResearchCandidate | null>(null);
const report = ref<CandidateReport | null>(null);
const reportLoading = ref(false);

async function loadCandidates() {
  loading.value = true;
  try {
    candidates.value = await listCandidates(searchSymbol.value, filterStatus.value, 100);
  } catch (e) {
    message.error('候选列表加载失败');
  } finally {
    loading.value = false;
  }
}

async function selectCandidate(id: string) {
  drawerVisible.value = true;
  report.value = null;
  try {
    selectedCandidate.value = await getCandidate(id);
    reportLoading.value = true;
    report.value = await getCandidateReport(id);
  } catch {
    message.error('候选详情加载失败');
  } finally {
    reportLoading.value = false;
  }
}

function formatScore(sb: { score: { low: number; high: number } }): string {
  if (!sb?.score) return 'N/A';
  const mid = (sb.score.low + sb.score.high) / 2;
  return mid.toFixed(3);
}

function statusColor(status: string): string {
  const map: Record<string, string> = { active: 'green', degraded: 'orange', expired: 'red', archived: 'default', draft: 'blue' };
  return map[status] || 'default';
}

function statusLabel(status: string): string {
  const map: Record<string, string> = { active: '活跃', degraded: '降级', expired: '过期', archived: '归档', draft: '草稿' };
  return map[status] || status;
}

function thesisStatusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: '草稿', evidence_pending: '待验证', active: '活跃',
    high_conviction: '高确信', conflicted: '冲突',
    invalidated: '失效', expired: '过期', archived: '归档',
  };
  return map[status] || status;
}

function computabilityLabel(c: string): string {
  const map: Record<string, string> = { computable: '可计算', degraded: '降级', not_computable: '不可计算' };
  return map[c] || c;
}

function sectionLabel(section: string): string {
  const map: Record<string, string> = {
    entry_reason: '入选理由', industry_position: '产业链位置', key_facts: '关键事实',
    earnings_impact: '盈利影响', market_expectation: '市场预期', expectation_gap: '预期差',
    catalysts: '催化剂', weakest_assumption: '最弱假设', contradictions: '反证',
    invalidation: '失效条件', next_validation: '下一步验证', scores: '分数明细',
  };
  return map[section] || section;
}

onMounted(() => { loadCandidates(); });
</script>

<style scoped>
.candidate-tab { padding: 8px 0; }
.candidate-controls { margin-bottom: 16px; }
.candidate-list { display: flex; flex-direction: column; gap: 12px; }
.candidate-card { cursor: pointer; }
.card-title { font-weight: 600; }
.score-summary { display: flex; gap: 12px; font-size: 12px; }
.score-item { font-weight: 600; }
.score-item.research { color: #1677ff; }
.score-item.alpha { color: #fa541c; }
.score-item.core { color: #52c41a; }
.info-row { margin: 2px 0; font-size: 13px; }
.info-row .label { color: #888; margin-right: 4px; }
.score-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
.score-box { padding: 16px; border-radius: 8px; text-align: center; }
.score-box.research { background: #e6f4ff; }
.score-box.alpha { background: #fff2e8; }
.score-box.core { background: #f6ffed; }
.score-title { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
.score-value { font-size: 24px; font-weight: 700; }
.score-computability { font-size: 12px; color: #888; margin: 4px 0; }
.score-components { font-size: 11px; color: #666; text-align: left; }
.score-components div { margin: 2px 0; }
.report-sections { max-height: 600px; overflow-y: auto; }
.report-section { margin-bottom: 16px; }
.report-section h4 { font-size: 14px; font-weight: 600; margin-bottom: 4px; color: #1677ff; }
.report-section pre { white-space: pre-wrap; font-size: 13px; margin: 0; }
.decision-trace { font-size: 13px; line-height: 1.8; font-family: monospace; }
</style>
