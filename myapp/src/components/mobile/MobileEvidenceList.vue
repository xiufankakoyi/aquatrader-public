<template>
  <div class="mobile-evidence-list">
    <div
      v-for="(item, idx) in items"
      :key="idx"
      class="evidence-item"
      :class="{ 'evidence-expanded': expandedSet.has(idx) }"
    >
      <div class="evidence-header" @click="toggleExpand(idx)">
        <span class="evidence-index">{{ idx + 1 }}</span>
        <span class="evidence-title">{{ item.title || `证据 #${idx + 1}` }}</span>
        <span class="evidence-level" :class="`level-${(item.level || 'unknown').toLowerCase()}`">
          {{ item.level || '未知' }}
        </span>
        <i
          class="fa-solid fa-chevron-down evidence-arrow"
          :class="{ 'arrow-rotated': expandedSet.has(idx) }"
        />
      </div>
      <Transition name="evidence-fade">
        <div v-if="expandedSet.has(idx)" class="evidence-detail">
          <p v-if="item.description" class="evidence-desc">{{ item.description }}</p>
          <p v-if="item.source" class="evidence-source">
            <i class="fa-solid fa-link" />
            {{ item.source }}
          </p>
        </div>
      </Transition>
    </div>
    <div v-if="!items.length" class="evidence-empty">暂无本地证据</div>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileEvidenceList - 移动端证据列表组件
 * 可展开/折叠的证据条目，带等级标签
 */
import { ref } from 'vue';

export interface EvidenceItem {
  title?: string;
  level?: string;
  description?: string;
  source?: string;
}

defineProps<{
  /** 证据列表 */
  items: EvidenceItem[];
}>();

const expandedSet = ref<Set<number>>(new Set());

/** 切换展开/折叠 */
function toggleExpand(idx: number) {
  if (expandedSet.value.has(idx)) {
    expandedSet.value.delete(idx);
  } else {
    expandedSet.value.add(idx);
  }
}
</script>

<style scoped>
.evidence-item {
  border: 1px solid rgba(255, 255, 255, 0.04);
  border-radius: 8px;
  margin-bottom: 6px;
  overflow: hidden;
  transition: all var(--transition-fast);
}

.evidence-expanded {
  border-color: rgba(255, 255, 255, 0.08);
}

.evidence-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  cursor: pointer;
}

.evidence-index {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
  font-size: 11px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.evidence-title {
  flex: 1;
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.evidence-level {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 500;
  flex-shrink: 0;
}

.level-high {
  background: rgba(8, 153, 129, 0.15);
  color: #0db89a;
}

.level-medium {
  background: rgba(245, 166, 35, 0.15);
  color: #f5a623;
}

.level-low {
  background: rgba(120, 123, 134, 0.15);
  color: #787b86;
}

.level-unknown {
  background: rgba(120, 123, 134, 0.1);
  color: #5d606b;
}

.evidence-arrow {
  font-size: 10px;
  color: var(--text-muted);
  transition: transform 0.2s ease;
  flex-shrink: 0;
}

.arrow-rotated {
  transform: rotate(180deg);
}

.evidence-detail {
  padding: 0 12px 10px;
}

.evidence-desc {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin-bottom: 4px;
}

.evidence-source {
  font-size: 11px;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: 4px;
}

.evidence-empty {
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  padding: 20px;
}

/* 展开动画 */
.evidence-fade-enter-active,
.evidence-fade-leave-active {
  transition: all 0.2s ease;
}

.evidence-fade-enter-from,
.evidence-fade-leave-to {
  opacity: 0;
  max-height: 0;
}
</style>
