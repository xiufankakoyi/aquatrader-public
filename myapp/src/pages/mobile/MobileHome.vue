<template>
  <div class="mobile-home">
    <!-- 下拉刷新指示器 -->
    <div v-if="isPulling" class="pull-indicator" :style="{ height: pullDistance + 'px' }">
      <span v-if="pullDistance < 60">下拉刷新</span>
      <span v-else>释放刷新</span>
    </div>
    <div v-if="isRefreshing" class="refresh-indicator">刷新中...</div>

    <!-- 加载状态 -->
    <div v-if="loading" class="home-loading">
      <a-spin size="large" />
      <span>加载中...</span>
    </div>

    <template v-else>
      <!-- 今日状态卡片 -->
      <MobileCard
        title="今日状态"
        :extra="summary?.date || ''"
        accent="blue"
        icon="fa-solid fa-chart-line"
        :delay="0"
      >
        <div class="status-row">
          <span class="status-label">最后更新</span>
          <span class="status-value">{{ formatTime(summary?.last_updated) }}</span>
        </div>
        <div v-if="summary?.inbox_stats" class="stats-grid">
          <div class="stat-item stat-item--primary">
            <span class="stat-value">{{ summary.inbox_stats.NEW || 0 }}</span>
            <span class="stat-label">新发现</span>
          </div>
          <div class="stat-item stat-item--watch">
            <span class="stat-value">{{ summary.inbox_stats.WATCHING || 0 }}</span>
            <span class="stat-label">观察中</span>
          </div>
          <div class="stat-item stat-item--ready">
            <span class="stat-value">{{ summary.inbox_stats.DEEP_RESEARCH_READY || 0 }}</span>
            <span class="stat-label">可深研</span>
          </div>
        </div>
      </MobileCard>

      <!-- 最新日报摘要 -->
      <MobileCard
        title="最新日报摘要"
        class="home-section"
        accent="purple"
        icon="fa-solid fa-newspaper"
        :delay="80"
      >
        <template v-if="summary?.latest_daily_brief">
          <div
            class="brief-body markdown-body"
            :class="{ 'brief-collapsed': !briefExpanded }"
            v-html="renderMarkdown(summary.latest_daily_brief)"
          />
          <button
            v-if="summary.latest_daily_brief.length > 120"
            class="expand-btn"
            @click="briefExpanded = !briefExpanded"
          >
            {{ briefExpanded ? '收起' : '展开全文' }}
          </button>
        </template>
        <p v-else class="empty-text">暂无日报</p>
        <template #footer>
          <button class="link-btn" @click="router.push('/mobile/daily')">
            查看完整日报 <i class="fa-solid fa-arrow-right" />
          </button>
        </template>
      </MobileCard>

      <!-- 高优先级候选 Top 10 -->
      <MobileCard
        title="高优先级候选"
        class="home-section"
        accent="gold"
        icon="fa-solid fa-crown"
        :delay="160"
      >
        <template #extra>
          <div class="market-toggle">
            <button
              class="toggle-btn"
              :class="{ 'toggle-active': marketFilter === 'all' }"
              @click="marketFilter = 'all'"
            >
              全部
            </button>
            <button
              class="toggle-btn"
              :class="{ 'toggle-active': marketFilter === 'ashare' }"
              @click="marketFilter = 'ashare'"
            >
              A股主板
            </button>
          </div>
        </template>
        <div v-if="topCandidates.length" class="candidate-list">
          <div
            v-for="(item, idx) in topCandidates"
            :key="idx"
            class="candidate-item"
            :style="{ animationDelay: `${240 + idx * 40}ms` }"
            @click="router.push('/mobile/inbox')"
          >
            <div class="candidate-rank" :class="`rank-${idx + 1}`">{{ idx + 1 }}</div>
            <div class="candidate-info">
              <div class="candidate-name">{{ item.company_name || item.symbol }}</div>
              <div class="candidate-meta">
                <span v-if="item.symbol" class="meta-symbol">{{ item.symbol }}</span>
                <span v-if="item.chain_node" class="meta-tag">{{ item.chain_node }}</span>
                <span v-if="item.concept" class="meta-tag">{{ item.concept }}</span>
              </div>
            </div>
            <div class="candidate-score">
              <span class="score-value">{{ item.research_priority_score ?? item.total_score ?? '-' }}</span>
              <MobileStatusBadge
                v-if="item.evidence_level"
                :status="item.evidence_level"
              />
            </div>
          </div>
        </div>
        <p v-else class="empty-text">暂无高优先级候选</p>
      </MobileCard>

      <!-- 任务状态 -->
      <MobileCard
        title="任务状态"
        class="home-section"
        accent="green"
        icon="fa-solid fa-list-check"
        :delay="240"
      >
        <div v-if="summary?.task_stats" class="stats-grid stats-grid--tasks">
          <div class="stat-item stat-item--queue">
            <span class="stat-value">{{ summary.task_stats.QUEUED || 0 }}</span>
            <span class="stat-label">排队中</span>
          </div>
          <div class="stat-item stat-item--running">
            <span class="stat-value">{{ summary.task_stats.RUNNING || 0 }}</span>
            <span class="stat-label">运行中</span>
          </div>
          <div class="stat-item stat-item--success">
            <span class="stat-value">{{ summary.task_stats.SUCCESS || 0 }}</span>
            <span class="stat-label">已完成</span>
          </div>
          <div class="stat-item stat-item--failed">
            <span class="stat-value">{{ summary.task_stats.FAILED || 0 }}</span>
            <span class="stat-label">失败</span>
          </div>
        </div>
        <p v-else class="empty-text">暂无任务信息</p>
        <template #footer>
          <button class="link-btn" @click="router.push('/mobile/command')">
            查看任务详情 <i class="fa-solid fa-arrow-right" />
          </button>
        </template>
      </MobileCard>

      <!-- 快捷命令 -->
      <MobileCard
        title="快捷操作"
        class="home-section"
        accent="red"
        icon="fa-solid fa-bolt"
        :delay="320"
      >
        <div class="quick-actions">
          <button class="quick-btn quick-btn--daily" @click="router.push('/mobile/daily')">
            <div class="quick-btn-icon">
              <i class="fa-solid fa-newspaper" />
            </div>
            <span>查看日报</span>
          </button>
          <button class="quick-btn quick-btn--inbox" @click="router.push('/mobile/inbox')">
            <div class="quick-btn-icon">
              <i class="fa-solid fa-inbox" />
            </div>
            <span>查看 Top 10</span>
          </button>
          <button class="quick-btn quick-btn--command" @click="router.push('/mobile/command')">
            <div class="quick-btn-icon">
              <i class="fa-solid fa-terminal" />
            </div>
            <span>查看任务</span>
          </button>
        </div>
      </MobileCard>
    </template>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileHome - 移动端首页
 * 展示今日状态、日报摘要、高优先级候选、任务状态和快捷操作
 */
import { ref, onMounted, computed } from 'vue';
import { useRouter } from 'vue-router';
import { getSummary } from '../../api/mobile';
import type { MobileSummary } from '../../api/mobile';
import MobileCard from '../../components/mobile/MobileCard.vue';
import MobileStatusBadge from '../../components/mobile/MobileStatusBadge.vue';
import { useMobileGestures } from '../../composables/useMobileGestures';
import { renderMarkdown } from '../../utils/markdown';

const router = useRouter();

const loading = ref(true);
const summary = ref<MobileSummary | null>(null);
const briefExpanded = ref(false);
const marketFilter = ref<'all' | 'ashare'>('all');

const { isRefreshing, pullDistance, isPulling } = useMobileGestures({
  onPullRefresh: async () => {
    await loadSummary();
  },
});

/** 高优先级候选原始列表（后端返回前 100 条，供前端筛选） */
const rawCandidates = ref<any[]>([]);

/** 按市场筛选后取前 10 的候选列表 */
const topCandidates = computed(() => {
  const filtered =
    marketFilter.value === 'all'
      ? rawCandidates.value
      : rawCandidates.value.filter((item) => isAShareMainBoard(item.symbol));
  return filtered.slice(0, 10);
});

/** 判断是否为 A股主板代码（6 位数字，60/00/001/002/003 开头；可选 .SZ/.SH/.BJ 后缀） */
function isAShareMainBoard(symbol?: string): boolean {
  if (!symbol) return false;
  return /^(60\d{4}|000\d{3}|001\d{3}|002\d{3}|003\d{3})(\.(SZ|SH|BJ))?$/i.test(symbol.trim());
}

/** 加载首页数据 */
async function loadSummary() {
  loading.value = true;
  try {
    const res = await getSummary();
    summary.value = res?.data || res;
    rawCandidates.value = summary.value?.top_candidates || [];
  } catch (err) {
    console.error('[MobileHome] 加载摘要失败:', err);
  } finally {
    loading.value = false;
  }
}

/** 格式化时间 */
function formatTime(timeStr?: string): string {
  if (!timeStr) return '--';
  try {
    const d = new Date(timeStr);
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`;
  } catch {
    return timeStr;
  }
}

onMounted(() => {
  loadSummary();
});
</script>

<style scoped>
.mobile-home {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.home-section {
  margin-top: 0;
}

.home-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 60px 0;
  color: var(--text-muted);
  font-size: 13px;
}

/* 状态行 */
.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding: 0 2px;
}

.status-label {
  font-size: 12px;
  color: var(--text-muted);
}

.status-value {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: 'JetBrains Mono', monospace;
}

/* 统计网格 */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.stats-grid--tasks {
  grid-template-columns: repeat(4, 1fr);
}

.stat-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 12px 4px;
  background: rgba(255, 255, 255, 0.02);
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, 0.03);
  transition: transform 0.2s ease, background 0.2s ease;
}

.stat-item:active {
  transform: scale(0.96);
}

.stat-item--primary {
  background: rgba(41, 98, 255, 0.08);
  border-color: rgba(41, 98, 255, 0.1);
}

.stat-item--watch {
  background: rgba(245, 166, 35, 0.06);
  border-color: rgba(245, 166, 35, 0.1);
}

.stat-item--ready {
  background: rgba(124, 58, 237, 0.08);
  border-color: rgba(124, 58, 237, 0.1);
}

.stat-item--queue .stat-value {
  color: var(--text-secondary);
}

.stat-item--running .stat-value {
  color: #f5a623;
}

.stat-item--success .stat-value {
  color: #0db89a;
}

.stat-item--failed .stat-value {
  color: #f55763;
}

.stat-value {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: 'JetBrains Mono', monospace;
  letter-spacing: -0.02em;
}

.stat-label {
  font-size: 10px;
  color: var(--text-muted);
}

/* 日报摘要 */
.brief-body {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.7;
  margin: 0;
}

.brief-body :deep(h1),
.brief-body :deep(h2),
.brief-body :deep(h3) {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 10px 0 6px;
}

.brief-body :deep(strong) {
  color: var(--text-primary);
  font-weight: 600;
}

.brief-body :deep(ul) {
  padding-left: 16px;
  margin: 6px 0;
}

.brief-body :deep(li) {
  margin: 3px 0;
}

.brief-body :deep(p) {
  margin: 6px 0;
}

.brief-collapsed {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.expand-btn {
  background: none;
  border: none;
  color: #4c7fff;
  font-size: 12px;
  cursor: pointer;
  padding: 4px 0;
  margin-top: 6px;
  transition: color 0.2s ease;
}

.expand-btn:active {
  color: var(--accent-primary-hover);
}

.empty-text {
  color: var(--text-muted);
  font-size: 13px;
  text-align: center;
  padding: 16px 0;
}

.link-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  background: none;
  border: none;
  color: #4c7fff;
  font-size: 12px;
  cursor: pointer;
  padding: 0;
  transition: gap 0.2s ease;
}

.link-btn:active {
  gap: 6px;
}

/* 市场筛选滑块 */
.market-toggle {
  display: inline-flex;
  align-items: center;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 12px;
  padding: 2px;
}

.toggle-btn {
  padding: 3px 8px;
  font-size: 10px;
  border: none;
  border-radius: 10px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.2s ease;
  white-space: nowrap;
}

.toggle-active {
  background: rgba(245, 166, 35, 0.18);
  color: #f5a623;
}

/* 候选列表 */
.candidate-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.candidate-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
  background: rgba(255, 255, 255, 0.02);
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, 0.03);
  cursor: pointer;
  transition: all 0.2s ease;
  opacity: 0;
  animation: item-enter 0.4s cubic-bezier(0.22, 1, 0.36, 1) forwards;
}

.candidate-item:active {
  background: rgba(255, 255, 255, 0.05);
  transform: scale(0.98);
}

@keyframes item-enter {
  from {
    opacity: 0;
    transform: translateX(-10px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}

.candidate-rank {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
  font-family: 'JetBrains Mono', monospace;
}

.rank-1 {
  background: linear-gradient(135deg, #f5a623 0%, #d97706 100%);
  color: #fff;
  box-shadow: 0 2px 8px rgba(245, 166, 35, 0.3);
}

.rank-2 {
  background: linear-gradient(135deg, #9ca3af 0%, #6b7280 100%);
  color: #fff;
  box-shadow: 0 2px 8px rgba(156, 163, 175, 0.2);
}

.rank-3 {
  background: linear-gradient(135deg, #b45309 0%, #92400e 100%);
  color: #fff;
  box-shadow: 0 2px 8px rgba(180, 83, 9, 0.25);
}

.candidate-info {
  flex: 1;
  min-width: 0;
}

.candidate-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.candidate-meta {
  display: flex;
  gap: 4px;
  margin-top: 3px;
  flex-wrap: wrap;
  align-items: center;
}

.meta-symbol {
  font-size: 10px;
  color: var(--text-secondary);
  font-family: 'JetBrains Mono', monospace;
}

.meta-tag {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--text-muted);
}

.candidate-score {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
}

.score-value {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: 'JetBrains Mono', monospace;
}

/* 快捷操作 */
.quick-actions {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.quick-btn {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 14px 8px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.05);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s cubic-bezier(0.22, 1, 0.36, 1);
  color: var(--text-secondary);
}

.quick-btn:active {
  transform: scale(0.95);
}

.quick-btn-icon {
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
  font-size: 18px;
  color: #fff;
  transition: transform 0.2s ease;
}

.quick-btn--daily .quick-btn-icon {
  background: linear-gradient(135deg, #7c3aed 0%, #4c1d95 100%);
  box-shadow: 0 4px 12px rgba(124, 58, 237, 0.25);
}

.quick-btn--inbox .quick-btn-icon {
  background: linear-gradient(135deg, #f5a623 0%, #b45309 100%);
  box-shadow: 0 4px 12px rgba(245, 166, 35, 0.25);
}

.quick-btn--command .quick-btn-icon {
  background: linear-gradient(135deg, #089981 0%, #064e3b 100%);
  box-shadow: 0 4px 12px rgba(8, 153, 129, 0.25);
}

.quick-btn span {
  font-size: 11px;
  font-weight: 500;
}

.pull-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(209, 212, 220, 0.5);
  font-size: 13px;
  overflow: hidden;
  transition: height 0.1s;
}

.refresh-indicator {
  text-align: center;
  padding: 8px;
  color: rgba(209, 212, 220, 0.7);
  font-size: 13px;
}

@media (prefers-reduced-motion: reduce) {
  .candidate-item {
    animation: none;
    opacity: 1;
  }
}
</style>