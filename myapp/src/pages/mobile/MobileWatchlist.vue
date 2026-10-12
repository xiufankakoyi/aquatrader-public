<template>
  <div class="mobile-watchlist">
    <!-- 分类筛选 -->
    <div class="watchlist-filters">
      <button
        v-for="f in classificationFilters"
        :key="f.value"
        class="filter-btn"
        :class="{ 'filter-active': currentClassification === f.value }"
        @click="changeClassification(f.value)"
      >
        {{ f.label }}
      </button>
    </div>

    <!-- 刷新按钮 -->
    <div class="watchlist-toolbar">
      <span class="watchlist-count">共 {{ total }} 条</span>
      <button class="refresh-btn" :disabled="loading" @click="loadWatchlist">
        <i class="fa-solid fa-rotate" :class="{ 'spin-icon': loading }" />
      </button>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading && !items.length" class="watchlist-loading">
      <a-spin size="large" />
      <span>加载中...</span>
    </div>

    <!-- 卡片列表 -->
    <div v-else-if="items.length" class="watchlist-list">
      <MobileCard
        v-for="item in items"
        :key="item.item_id"
        clickable
      >
        <div class="watchlist-item">
          <div class="watchlist-item-header">
            <span class="watchlist-company">{{ item.company_name || item.symbol }}</span>
            <span class="watchlist-score">
              {{ item.total_score ?? '-' }}
            </span>
          </div>
          <div class="watchlist-item-meta">
            <span v-if="item.chain_node" class="meta-tag chain-tag">{{ item.chain_node }}</span>
            <span v-if="item.concept" class="meta-tag concept-tag">{{ item.concept }}</span>
          </div>
          <div class="watchlist-item-footer">
            <span v-if="item.classification" class="classification-badge">
              {{ item.classification }}
            </span>
            <span v-if="item.evidence_level" class="evidence-level">
              {{ item.evidence_level }}
            </span>
            <MobileStatusBadge
              v-if="item.research_priority_level"
              :status="item.research_priority_level"
            />
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
    <div v-else class="watchlist-empty">
      <i class="fa-regular fa-eye" />
      <p>暂无观察池数据</p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileWatchlist - 移动端观察池页
 * 分类筛选、卡片列表、分页加载
 */
import { ref, onMounted } from 'vue';
import { getWatchlist } from '../../api/mobile';
import type { WatchlistItem } from '../../api/mobile';
import MobileCard from '../../components/mobile/MobileCard.vue';
import MobileStatusBadge from '../../components/mobile/MobileStatusBadge.vue';

/** 分类筛选选项 */
const classificationFilters = [
  { label: '全部', value: '' },
  { label: '紫苏叶', value: '紫苏叶' },
  { label: '主链锚点', value: '主链锚点' },
  { label: '概念宣称', value: '概念宣称' },
  { label: '早期信号观察', value: '早期信号观察' },
  { label: '非核心市场', value: '非核心市场' }
];

const currentClassification = ref('');
const items = ref<WatchlistItem[]>([]);
const total = ref(0);
const loading = ref(false);
const loadingMore = ref(false);
const offset = ref(0);
const pageSize = 20;
const hasMore = ref(false);

/** 切换分类筛选 */
function changeClassification(cls: string) {
  currentClassification.value = cls;
  offset.value = 0;
  items.value = [];
  loadWatchlist();
}

/** 加载观察池数据 */
async function loadWatchlist() {
  loading.value = true;
  try {
    const res = await getWatchlist({
      classification: currentClassification.value || undefined,
      limit: pageSize,
      offset: 0
    });
    const data = res?.data || res;
    items.value = data?.items || [];
    total.value = data?.total || items.value.length;
    offset.value = items.value.length;
    hasMore.value = items.value.length < total.value;
  } catch (err) {
    console.error('[MobileWatchlist] 加载失败:', err);
  } finally {
    loading.value = false;
  }
}

/** 加载更多 */
async function loadMore() {
  loadingMore.value = true;
  try {
    const res = await getWatchlist({
      classification: currentClassification.value || undefined,
      limit: pageSize,
      offset: offset.value
    });
    const data = res?.data || res;
    const newItems = data?.items || [];
    items.value.push(...newItems);
    offset.value = items.value.length;
    hasMore.value = items.value.length < total.value;
  } catch (err) {
    console.error('[MobileWatchlist] 加载更多失败:', err);
  } finally {
    loadingMore.value = false;
  }
}

onMounted(() => {
  loadWatchlist();
});
</script>

<style scoped>
.mobile-watchlist {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 筛选标签 */
.watchlist-filters {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  padding-bottom: 2px;
}

.watchlist-filters::-webkit-scrollbar {
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
  background: rgba(245, 166, 35, 0.15);
  border-color: rgba(245, 166, 35, 0.3);
  color: #f5a623;
}

/* 工具栏 */
.watchlist-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.watchlist-count {
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
.watchlist-loading {
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
.watchlist-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 条目内容 */
.watchlist-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.watchlist-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.watchlist-company {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.watchlist-score {
  font-size: 16px;
  font-weight: 700;
  color: #f5a623;
  font-family: 'JetBrains Mono', monospace;
  flex-shrink: 0;
}

.watchlist-item-meta {
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

.watchlist-item-footer {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  flex-wrap: wrap;
}

.classification-badge {
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--text-muted);
}

.evidence-level {
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(8, 153, 129, 0.1);
  color: #0db89a;
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
.watchlist-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 0;
  color: var(--text-muted);
}

.watchlist-empty i {
  font-size: 32px;
  opacity: 0.3;
}

.watchlist-empty p {
  font-size: 13px;
}
</style>
