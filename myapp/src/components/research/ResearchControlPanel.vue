<template>
  <section class="research-control">
    <header class="research-control__header">
      <div><h2>研究控制台</h2><p>任务、证据血缘、状态迁移、复核、反馈和校准统一审计</p></div>
      <button type="button" :disabled="loading" @click="loadAll">{{ loading ? '加载中…' : '刷新' }}</button>
    </header>
    <nav class="research-tabs" aria-label="研究控制台标签">
      <button v-for="tab in tabs" :key="tab.key" type="button" :class="{ active: activeTab === tab.key }" @click="activeTab = tab.key">
        {{ tab.label }} <span>{{ countFor(tab.key) }}</span>
      </button>
    </nav>
    <p v-if="error" class="research-error">{{ error }}</p>

    <div v-if="activeTab === 'tasks'" class="research-table-wrap">
      <table><thead><tr><th>任务</th><th>来源</th><th>优先级</th><th>状态</th><th>进度</th><th>创建时间</th></tr></thead>
        <tbody><tr v-for="item in tasks" :key="item.task_id"><td>{{ item.task_type }}</td><td>{{ formatSafe(item.trigger_source, '未知') }}</td><td>{{ formatScore(item.priority) }}</td><td>{{ statusLabel(item.status) }}</td><td>{{ formatSafe(item.progress?.message, '—') }}</td><td>{{ formatTime(item.created_at) }}</td></tr></tbody>
      </table><p v-if="!tasks.length" class="empty">暂无研究任务</p>
    </div>

    <div v-else-if="activeTab === 'claims'" class="research-table-wrap">
      <table><thead><tr><th>Claim</th><th>关系</th><th>状态</th><th>证据置信度</th><th>映射置信度</th><th>冲突</th></tr></thead>
        <tbody><tr v-for="item in claims" :key="item.claim_id" class="clickable" @click="selectClaim(item.claim_id)"><td>{{ item.subject_name || item.claim_id }}</td><td>{{ item.relation }} → {{ item.object_name || '未知' }}</td><td>{{ statusLabel(item.research_status) }}</td><td>{{ formatPercent(item.evidence_confidence) }}</td><td>{{ formatPercent(item.mapping_confidence) }}</td><td>{{ item.conflict_status || '无' }}</td></tr></tbody>
      </table><p v-if="!claims.length" class="empty">暂无统一 Claim</p>
    </div>

    <div v-else-if="activeTab === 'lineage'" class="claim-detail">
      <p v-if="!claimDetail.claim">请在“证据血缘”或“状态迁移”页选择 Claim；当前默认显示首条。</p>
      <template v-else>
        <h3>{{ claimDetail.claim.subject_name || claimDetail.claim.claim_id }} · {{ claimDetail.claim.relation }}</h3>
        <article v-for="item in claimDetail.evidence || []" :key="item.evidence_id" class="evidence-card">
          <div><strong>{{ stanceLabel(item.stance) }}</strong><span>血缘：{{ formatSafe(item.lineage_id, '未知') }}</span><span>来源质量：{{ formatScore(item.source_quality) }}</span></div>
          <blockquote>{{ item.quote || '暂无本地证据' }}</blockquote>
          <small>发布时间：{{ formatTime(item.published_at) }}　检索时间：{{ formatTime(item.retrieved_at) }}</small>
        </article><p v-if="!(claimDetail.evidence || []).length" class="empty">暂无本地证据</p>
      </template>
    </div>

    <div v-else-if="activeTab === 'transitions'" class="research-table-wrap">
      <table><thead><tr><th>原状态</th><th>新状态</th><th>原因</th><th>策略版本</th><th>时间</th></tr></thead>
        <tbody><tr v-for="item in claimDetail.transitions || []" :key="item.transition_id"><td>{{ statusLabel(item.from_status) }}</td><td>{{ statusLabel(item.to_status) }}</td><td>{{ item.reason }}</td><td>{{ item.policy_version }}</td><td>{{ formatTime(item.created_at) }}</td></tr></tbody>
      </table><p v-if="!(claimDetail.transitions || []).length" class="empty">暂无状态迁移记录</p>
    </div>

    <div v-else-if="activeTab === 'reviews'" class="review-list">
      <article v-for="item in reviews" :key="item.review_id"><div><strong>{{ item.reason }}</strong><span>{{ item.priority }}</span><span class="audit-tag">{{ reviewAuditLabel(item) }}</span></div><p>{{ item.required_action }}</p>
        <footer><button @click="review(item.review_id, 'accept')">接受</button><button @click="review(item.review_id, 'reject')">拒绝</button><button @click="review(item.review_id, 'request_evidence')">要求补证</button></footer></article>
      <p v-if="!reviews.length" class="empty">暂无待复核项目</p>
    </div>

    <div v-else-if="activeTab === 'feedback'" class="research-table-wrap">
      <table><thead><tr><th>三域反馈</th><th>事件</th><th>目标</th><th>指标</th><th>时间</th></tr></thead><tbody><tr v-for="item in feedback" :key="item.feedback_id"><td>{{ domainLabel(item.domain) }}</td><td>{{ item.event_type }}</td><td>{{ item.target_id || '—' }}</td><td>{{ JSON.stringify(item.metrics || {}) }}</td><td>{{ formatTime(item.created_at) }}</td></tr></tbody></table><p v-if="!feedback.length" class="empty">暂无反馈归因记录</p>
    </div>

    <div v-else-if="activeTab === 'calibrations'" class="research-table-wrap">
      <table><thead><tr><th>域</th><th>策略版本</th><th>状态</th><th>shadow-active 校准版本</th><th>样本数</th><th>walk-forward</th><th>创建时间</th></tr></thead><tbody><tr v-for="item in calibrations" :key="item.calibration_id"><td>{{ item.domain }}</td><td>{{ item.policy_version || '待分配' }}</td><td>{{ statusLabel(item.status) }}</td><td>{{ formatSafe(item.shadow_active_version, '—') }}</td><td>{{ formatNumber(item.sample_size, 0) }}</td><td>{{ formatScore(Number(item.metrics?.walk_forward_score || 0)) }}</td><td>{{ formatTime(item.created_at) }}</td></tr></tbody></table><p v-if="!calibrations.length" class="empty">暂无校准版本</p>
    </div>

    <div v-else-if="activeTab === 'fact_outcomes'" class="research-table-wrap">
      <table><thead><tr><th>记录 ID</th><th>Claim</th><th>晋升类型</th><th>事实标签</th><th>策略版本</th><th>晋升时间</th><th>resolve 时间</th></tr></thead>
        <tbody><tr v-for="item in factOutcomes" :key="item.record_id"><td>{{ item.record_id }}</td><td>{{ item.claim_id || '—' }}</td><td>{{ promotionLabel(item.promotion_type) }}</td><td>{{ factLabel(item.fact_label) }}</td><td>{{ formatSafe(item.policy_version, '—') }}</td><td>{{ formatTime(item.promoted_at) }}</td><td>{{ formatTime(item.resolved_at) }}</td></tr></tbody>
      </table><p v-if="!factOutcomes.length" class="empty">暂无事实结果记录</p>
    </div>

    <div v-else-if="activeTab === 'shadow_features'" class="research-table-wrap">
      <div class="shadow-meta">
        <span>特征版本：{{ formatSafe(shadowMeta.feature_version, '未知') }}</span>
        <span>策略版本：{{ formatSafe(shadowMeta.policy_version, '未知') }}</span>
        <span>symbol：{{ formatSafe(shadowMeta.symbol, '—') }}</span>
        <span>as_of：{{ formatSafe(shadowMeta.as_of, '—') }}</span>
      </div>
      <table><thead><tr><th>特征名</th><th>中文描述</th><th>当前值</th></tr></thead>
        <tbody><tr v-for="name in shadowMeta.feature_names || []" :key="name"><td>{{ name }}</td><td>{{ formatSafe(shadowMeta.descriptions?.[name], name) }}</td><td>—</td></tr></tbody>
      </table><p v-if="!(shadowMeta.feature_names || []).length" class="empty">暂无 shadow 特征</p>
    </div>

    <div v-else-if="activeTab === 'market_session'" class="market-session">
      <div class="session-card">
        <div class="session-row"><span class="session-label">当前时段</span><strong :class="{ trading: marketSession.is_trading_time }">{{ marketSession.session_label || '未知' }}</strong></div>
        <div class="session-row"><span class="session-label">是否交易日</span><span>{{ marketSession.is_trading_day ? '是' : '否' }}</span></div>
        <div class="session-row"><span class="session-label">可下单时段</span><span>{{ marketSession.is_trading_time ? '是' : '否' }}</span></div>
        <div class="session-row"><span class="session-label">下一时段</span><span>{{ nextSessionLabel(marketSession.next_session) }}</span></div>
        <div class="session-row"><span class="session-label">市场时间</span><span>{{ formatTime(marketSession.market_time) }}</span></div>
      </div>
      <p v-if="!marketSession.session" class="empty">市场时段加载失败</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import {
  getClaimDetail,
  getFactOutcomes,
  getMarketSession,
  getResearchCalibrations,
  getResearchClaims,
  getResearchFeedback,
  getResearchReviews,
  getResearchTasks,
  getShadowFeatures,
  resolveReview,
} from '@/api/researchControl';
import { formatPercent, formatScore, formatTime, formatNumber, formatSafe } from '@/utils/format';

const tabs = [
  { key: 'tasks', label: '研究任务' }, { key: 'claims', label: 'Claim' }, { key: 'lineage', label: '证据血缘' },
  { key: 'transitions', label: '状态迁移' }, { key: 'reviews', label: '人工复核' }, { key: 'feedback', label: '反馈归因' }, { key: 'calibrations', label: '校准版本' },
  { key: 'fact_outcomes', label: '事实结果' }, { key: 'shadow_features', label: 'shadow 特征' }, { key: 'market_session', label: '盘中状态' },
] as const;
type TabKey = typeof tabs[number]['key'];
const activeTab = ref<TabKey>('tasks'); const loading = ref(false); const error = ref('');
const tasks = ref<any[]>([]); const claims = ref<any[]>([]); const reviews = ref<any[]>([]); const feedback = ref<any[]>([]); const calibrations = ref<any[]>([]); const claimDetail = ref<Record<string, any>>({});
const factOutcomes = ref<any[]>([]);
const shadowMeta = ref<Record<string, any>>({});
const marketSession = ref<Record<string, any>>({});

function countFor(key: TabKey) {
  if (key === 'tasks') return tasks.value.length;
  if (key === 'claims') return claims.value.length;
  if (key === 'reviews') return reviews.value.length;
  if (key === 'feedback') return feedback.value.length;
  if (key === 'calibrations') return calibrations.value.length;
  if (key === 'fact_outcomes') return factOutcomes.value.length;
  if (key === 'shadow_features') return (shadowMeta.value.feature_names || []).length;
  if (key === 'market_session') return marketSession.value.session ? 1 : 0;
  if (key === 'lineage') return (claimDetail.value.evidence || []).length;
  return (claimDetail.value.transitions || []).length;
}

function statusLabel(v: string) { return ({ queued: '等待执行', running: '执行中', retrying: '重试中', succeeded: '成功', partial: '部分完成', failed: '失败', cancelled: '已取消', shadow: '影子运行', active: '已启用', rolled_back: '已回滚', DISCOVERED: '已发现', CANDIDATE: '候选', SUPPORTED: '有支持', ACTIVE: '活跃', TRUSTED: '可信', CONFLICTED: '冲突', REJECTED: '已否定', DORMANT: '休眠', PRUNED: '已剪枝' } as Record<string, string>)[v] || v || '未知'; }
function stanceLabel(v: string) { return ({ support: '支持', counter: '反证', neutral: '中性', stale: '过期', retracted: '撤回' } as Record<string, string>)[v] || '未知'; }
function domainLabel(v: string) { return ({ knowledge: '知识域', retrieval: '检索域', alpha: 'Alpha 域' } as Record<string, string>)[v] || v || '未知'; }
function factLabel(v: string) { return ({ pending: '待判定', confirmed: '已确认', denied: '已否定', expired: '已过期', conflicted: '冲突' } as Record<string, string>)[v] || v || '未知'; }
function promotionLabel(v: string) { return ({ claim: 'Claim 晋升', candidate_node: '候选节点晋升', candidate_edge: '候选边晋升' } as Record<string, string>)[v] || v || '—'; }
function nextSessionLabel(v: string) { return ({ closed: '休市', pre_market: '盘前', call_auction: '集合竞价', morning: '上午连续交易', lunch_break: '午休', afternoon: '下午连续交易', post_market: '盘后' } as Record<string, string>)[v] || v || '未知'; }
function reviewAuditLabel(item: any) {
  // 复核项审计标签：resolution 存在表示已处置
  if (!item) return '审计：未知';
  if (item.status === 'resolved') return `审计：已处置（${item.resolution || '—'}）`;
  return '审计：待处置';
}

async function selectClaim(id: string) { try { claimDetail.value = await getClaimDetail(id); activeTab.value = 'lineage'; } catch { error.value = 'Claim 详情加载失败'; } }
async function review(id: string, action: 'accept' | 'reject' | 'request_evidence') { try { await resolveReview(id, action); await loadAll(); } catch { error.value = '审核操作失败，请检查状态与权限'; } }

async function loadAll() {
  loading.value = true; error.value = '';
  try {
    [tasks.value, claims.value, reviews.value, feedback.value, calibrations.value] = await Promise.all([
      getResearchTasks({ limit: 100 }),
      getResearchClaims({ limit: 100 }),
      getResearchReviews({ status: 'pending', limit: 100 }),
      getResearchFeedback({ limit: 100 }),
      getResearchCalibrations({ limit: 100 }),
    ]);
    if (claims.value.length && !claimDetail.value.claim) claimDetail.value = await getClaimDetail(claims.value[0].claim_id);
  } catch {
    error.value = '研究控制台暂不可用，现有产业链页面不受影响';
  } finally {
    loading.value = false;
  }
}

async function loadPhase8Data(tab: TabKey) {
  try {
    if (tab === 'fact_outcomes' && !factOutcomes.value.length) {
      factOutcomes.value = await getFactOutcomes({ status: 'pending', limit: 100 });
    } else if (tab === 'shadow_features' && !shadowMeta.value.feature_names) {
      shadowMeta.value = await getShadowFeatures({});
    } else if (tab === 'market_session') {
      marketSession.value = await getMarketSession();
    }
  } catch {
    error.value = '阶段8 数据加载失败，请稍后重试';
  }
}

watch(activeTab, (tab) => {
  if ((tab === 'lineage' || tab === 'transitions') && !claimDetail.value.claim && claims.value.length) {
    selectClaim(claims.value[0].claim_id);
  }
  loadPhase8Data(tab);
});
onMounted(loadAll);
</script>

<style scoped>
.research-control{margin-top:24px;padding:20px;border:1px solid #263449;border-radius:14px;background:#0d1522;color:#dce7f5}.research-control__header{display:flex;justify-content:space-between;gap:16px;align-items:center}.research-control h2{margin:0;font-size:20px}.research-control p{color:#91a4bc}.research-control button{border:1px solid #345078;background:#14243a;color:#dce7f5;border-radius:7px;padding:7px 12px;cursor:pointer}.research-tabs{display:flex;gap:8px;overflow:auto;margin:18px 0}.research-tabs button{white-space:nowrap}.research-tabs button.active{border-color:#4aa3ff;background:#17385d}.research-tabs span{margin-left:5px;color:#7fbfff}.research-table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px;border-bottom:1px solid #243249;text-align:left;white-space:nowrap}th{color:#8eb4dc}.clickable{cursor:pointer}.clickable:hover{background:#14243a}.evidence-card,.review-list article{margin:10px 0;padding:13px;border:1px solid #293c56;border-radius:9px;background:#101d2d}.evidence-card div,.review-list article>div{display:flex;gap:14px;flex-wrap:wrap}.evidence-card blockquote{margin:10px 0;padding-left:12px;border-left:3px solid #4aa3ff;color:#d9e5f2}.review-list footer{display:flex;gap:8px}.research-error{color:#ff8f8f}.empty{text-align:center;padding:18px;color:#8293a8}.boundary{font-size:12px}
.audit-tag{margin-left:auto;padding:2px 8px;border-radius:5px;background:#1b2c44;color:#91a4bc;font-size:12px}
.shadow-meta{display:flex;gap:18px;flex-wrap:wrap;padding:10px 4px;color:#8eb4dc;font-size:13px}
.market-session .session-card{padding:18px;border:1px solid #293c56;border-radius:10px;background:#101d2d}
.market-session .session-row{display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px dashed #243249}
.market-session .session-label{color:#8eb4dc}
.market-session .trading{color:#5fd679}
</style>
