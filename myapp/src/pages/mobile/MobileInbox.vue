<template>
  <div class="mobile-inbox">
    <!-- 下拉刷新指示器 -->
    <div v-if="isPulling" class="pull-indicator" :style="{ height: pullDistance + 'px' }">
      <span v-if="pullDistance < 60">下拉刷新</span>
      <span v-else>释放刷新</span>
    </div>
    <div v-if="isRefreshing" class="refresh-indicator">刷新中...</div>

    <!-- 状态筛选标签 -->
    <div class="inbox-filters">
      <button
        v-for="f in statusFilters"
        :key="f.value"
        class="filter-btn"
        :class="{ 'filter-active': currentStatus === f.value }"
        @click="changeStatus(f.value)"
      >
        {{ f.label }}
      </button>
    </div>

    <!-- 刷新按钮 -->
    <div class="inbox-toolbar">
      <span class="inbox-count">共 {{ total }} 条</span>
      <button class="refresh-btn" :disabled="loading" @click="loadInbox">
        <i class="fa-solid fa-rotate" :class="{ 'spin-icon': loading }" />
      </button>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading && !items.length" class="inbox-loading">
      <a-spin size="large" />
      <span>加载中...</span>
    </div>

    <!-- 卡片列表 -->
    <div v-else-if="items.length" class="inbox-list">
      <MobileCard
        v-for="item in items"
        :key="item.item_id"
        clickable
        @click="handleItemClick(item)"
      >
        <div class="inbox-item">
          <div class="inbox-item-header">
            <span class="inbox-company">{{ item.company_name || item.symbol }}</span>
            <MobileStatusBadge :status="item.inbox_status" />
          </div>
          <div class="inbox-item-meta">
            <span v-if="item.chain_node" class="meta-tag chain-tag">{{ item.chain_node }}</span>
            <span v-if="item.concept" class="meta-tag concept-tag">{{ item.concept }}</span>
          </div>
          <div class="inbox-item-footer">
            <span v-if="item.classification" class="classification">{{ item.classification }}</span>
            <span v-if="item.evidence_level" class="evidence-level">{{ item.evidence_level }}</span>
            <span class="priority-score">
              优先级: {{ item.research_priority_score ?? item.total_score ?? '-' }}
            </span>
          </div>
        </div>
      </MobileCard>

      <!-- 加载更多 -->
      <div v-if="hasMore" class="load-more">
        <button class="load-more-btn" :disabled="loadingMore" @click="loadMore">
          {{ loadingMore ? '加载中...' : '加载更多' }}
        </button>
      </div>
    </div>

    <!-- 空状态 -->
    <div v-else class="inbox-empty">
      <i class="fa-regular fa-inbox" />
      <p>暂无收件箱数据</p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileInbox - 移动端收件箱页
 * 状态筛选、卡片列表、分页加载
 */
import { ref, onMounted } from 'vue';
import { getInbox } from '../../api/mobile';
import type { InboxItem } from '../../api/mobile';
import MobileCard from '../../components/mobile/MobileCard.vue';
import MobileStatusBadge from '../../components/mobile/MobileStatusBadge.vue';
import { useMobileGestures } from '../../composables/useMobileGestures';

/** 状态筛选选项 */
const statusFilters = [
  { label: '全部', value: '' },
  { label: '新发现', value: 'NEW' },
  { label: '观察中', value: 'WATCHING' },
  { label: '可深研', value: 'DEEP_RESEARCH_READY' },
  { label: '已忽略', value: 'DISMISSED' },
  { label: '已过期', value: 'STALE' }
];

const currentStatus = ref('');
const items = ref<InboxItem[]>([]);
const total = ref(0);
const loading = ref(false);
const loadingMore = ref(false);
const offset = ref(0);
const pageSize = 20;

const { isRefreshing, pullDistance, isPulling } = useMobileGestures({
  onPullRefresh: async () => {
    await loadInbox();
  },
});

/** 是否还有更多数据 */
const hasMore = ref(false);

/** 切换状态筛选 */
function changeStatus(status: string) {
  currentStatus.value = status;
  offset.value = 0;
  items.value = [];
  loadInbox();
}

/** 加载收件箱数据 */
async function loadInbox() {
  loading.value = true;
  try {
    const res = await getInbox({
      status: currentStatus.value || undefined,
      limit: pageSize,
      offset: 0
    });
    const data = res?.data || res;
    items.value = data?.items || [];
    total.value = data?.total || items.value.length;
    offset.value = items.value.length;
    hasMore.value = items.value.length < total.value;
  } catch (err) {
    console.error('[MobileInbox] 加载失败:', err);
  } finally {
    loading.value = false;
  }
}

/** 加载更多 */
async function loadMore() {
  loadingMore.value = true;
  try {
    const res = await getInbox({
      status: currentStatus.value || undefined,
      limit: pageSize,
      offset: offset.value
    });
    const data = res?.data || res;
    const newItems = data?.items || [];
    items.value.push(...newItems);
    offset.value = items.value.length;
    hasMore.value = items.value.length < total.value;
  } catch (err) {
    console.error('[MobileInbox] 加载更多失败:', err);
  } finally {
    loadingMore.value = false;
  }
}

/** 点击条目 */
function handleItemClick(_item: InboxItem) {
  // 预留：跳转详情或展开操作
}

onMounted(() => {
  loadInbox();
});
</script>

<style scoped>
.mobile-inbox {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 筛选标签 */
.inbox-filters {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  padding-bottom: 2px;
}

.inbox-filters::-webkit-scrollbar {
  display: none;
}

.filter-btn {
  flex-shrink: 0;
  padding: 6px 12px;
  font-size: 12px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--text-muted);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.filter-btn:active {
  transform: scale(0.95);
}

.filter-active {
  background: rgba(41, 98, 255, 0.15);
  border-color: rgba(41, 98, 255, 0.3);
  color: #4c7fff;
}

/* 工具栏 */
.inbox-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.inbox-count {
  font-size: 12px;
  color: var(--text-muted);
}

.refresh-btn {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.refresh-btn:active {
  background: rgba(255, 255, 255, 0.06);
}

.refresh-btn:disabled {
  opacity: 0.5;
}

.spin-icon {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* 加载状态 */
.inbox-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 60px 0;
  color: var(--text-muted);
  font-size: 13px;
}

/* 卡片列表 */
.inbox-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 条目内容 */
.inbox-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.inbox-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.inbox-company {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inbox-item-meta {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.meta-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
}

.chain-tag {
  background: rgba(41, 98, 255, 0.1);
  color: #4c7fff;
}

.concept-tag {
  background: rgba(245, 166, 35, 0.1);
  color: #f5a623;
}

.inbox-item-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-muted);
}

.classification {
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.04);
}

.evidence-level {
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(8, 153, 129, 0.1);
  color: #0db89a;
}

.priority-score {
  margin-left: auto;
  font-family: 'JetBrains Mono', monospace;
  color: var(--text-secondary);
}

/* 加载更多 */
.load-more {
  text-align: center;
  padding: 12px 0;
}

.load-more-btn {
  padding: 8px 24px;
  font-size: 13px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--text-secondary);
  cursor: pointer;
}

.load-more-btn:disabled {
  opacity: 0.5;
}

/* 空状态 */
.inbox-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 0;
  color: var(--text-muted);
}

.inbox-empty i {
  font-size: 32px;
  opacity: 0.3;
}

.inbox-empty p {
  font-size: 13px;
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
</style>
