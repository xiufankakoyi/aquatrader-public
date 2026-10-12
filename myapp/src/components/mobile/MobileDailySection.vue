<template>
  <div class="daily-section-list">
    <template v-if="sections.length">
      <article
        v-for="(section, idx) in sections"
        :key="section.id"
        class="daily-section-card"
        :class="[`daily-section-card--${section.accent}`]"
        :style="{ animationDelay: `${idx * 60}ms` }"
      >
        <header class="daily-section-header">
          <span class="daily-section-icon" aria-hidden="true">
            <i :class="section.icon" />
          </span>
          <h2 class="daily-section-title">{{ section.title }}</h2>
          <span v-if="section.items.length" class="daily-section-count">
            {{ section.items.length }}
          </span>
        </header>
        <div class="daily-section-body">
          <ul v-if="section.items.length" class="daily-list">
            <li
              v-for="(item, itemIdx) in section.items"
              :key="itemIdx"
              class="daily-list-item"
            >
              <span class="daily-list-bullet" aria-hidden="true" />
              <span class="daily-list-text" v-html="renderInline(item)" />
            </li>
          </ul>
          <div
            v-else-if="section.content"
            class="daily-paragraph"
            v-html="renderMarkdown(section.content)"
          />
        </div>
      </article>
    </template>

    <div v-else-if="fallbackHtml" class="daily-content">
      <div class="markdown-body" v-html="fallbackHtml" />
    </div>

    <div v-else class="daily-empty">
      <i :class="emptyIcon" aria-hidden="true" />
      <p>{{ emptyText }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileDailySection - 日报/报告卡片列表
 *
 * 将解析后的 Markdown 区块渲染为带图标、强调色和渐进动画的卡片。
 */
import { renderMarkdown } from '../../utils/markdown';

export interface DailySection {
  id: string;
  title: string;
  icon: string;
  accent: string;
  content: string;
  items: string[];
}

interface Props {
  /** 解析后的卡片区块数组 */
  sections: DailySection[];
  /** 兜底 Markdown HTML（无区块时直接渲染） */
  fallbackHtml?: string;
  /** 空状态图标类名 */
  emptyIcon?: string;
  /** 空状态提示文本 */
  emptyText?: string;
}

withDefaults(defineProps<Props>(), {
  fallbackHtml: '',
  emptyIcon: 'fa-regular fa-newspaper',
  emptyText: '暂无数据',
});

/**
 * 将单行 Markdown 文本中的粗体、斜体等内联语法渲染为 HTML，
 * 并去除外层 <p> 标签与 <br>，适合嵌入列表项。
 */
function renderInline(text: string): string {
  return renderMarkdown(text)
    .replace(/^<p>|<\/p>$/g, '')
    .replace(/<br\/?>/g, '');
}
</script>

<style scoped>
.daily-section-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.daily-section-card {
  background: linear-gradient(145deg, rgba(30, 30, 46, 0.9), rgba(26, 26, 46, 0.95));
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 14px;
  padding: 14px 16px;
  overflow: hidden;
  opacity: 0;
  transform: translateY(12px);
  animation: section-enter 380ms ease forwards;
  transition: transform 180ms ease, box-shadow 180ms ease;
  touch-action: manipulation;
}

.daily-section-card:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
}

@media (prefers-reduced-motion: reduce) {
  .daily-section-card {
    animation: none;
    opacity: 1;
    transform: none;
  }

  .daily-section-card:hover {
    transform: none;
  }
}

@keyframes section-enter {
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.daily-section-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  min-width: 0;
}

.daily-section-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  font-size: 13px;
  flex-shrink: 0;
}

.daily-section-title {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
  line-height: 1.3;
  text-wrap: balance;
}

.daily-section-count {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
  padding: 2px 7px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.05);
}

/* 区块强调色 */
.daily-section-card--amber .daily-section-icon { background: rgba(245, 166, 35, 0.15); color: #f5a623; }
.daily-section-card--gold .daily-section-icon { background: rgba(250, 204, 21, 0.15); color: #facc15; }
.daily-section-card--blue .daily-section-icon { background: rgba(76, 127, 255, 0.15); color: #4c7fff; }
.daily-section-card--purple .daily-section-icon { background: rgba(168, 85, 247, 0.15); color: #a855f7; }
.daily-section-card--teal .daily-section-icon { background: rgba(13, 184, 154, 0.15); color: #0db89a; }
.daily-section-card--cyan .daily-section-icon { background: rgba(34, 211, 238, 0.15); color: #22d3ee; }
.daily-section-card--slate .daily-section-icon { background: rgba(148, 163, 184, 0.15); color: #94a3b8; }
.daily-section-card--indigo .daily-section-icon { background: rgba(99, 102, 241, 0.15); color: #6366f1; }

.daily-section-card--amber { border-left: 3px solid rgba(245, 166, 35, 0.5); }
.daily-section-card--gold { border-left: 3px solid rgba(250, 204, 21, 0.5); }
.daily-section-card--blue { border-left: 3px solid rgba(76, 127, 255, 0.5); }
.daily-section-card--purple { border-left: 3px solid rgba(168, 85, 247, 0.5); }
.daily-section-card--teal { border-left: 3px solid rgba(13, 184, 154, 0.5); }
.daily-section-card--cyan { border-left: 3px solid rgba(34, 211, 238, 0.5); }
.daily-section-card--slate { border-left: 3px solid rgba(148, 163, 184, 0.5); }
.daily-section-card--indigo { border-left: 3px solid rgba(99, 102, 241, 0.5); }

.daily-section-card--amber .daily-section-count { color: #f5a623; }
.daily-section-card--gold .daily-section-count { color: #facc15; }
.daily-section-card--blue .daily-section-count { color: #4c7fff; }
.daily-section-card--purple .daily-section-count { color: #a855f7; }
.daily-section-card--teal .daily-section-count { color: #0db89a; }
.daily-section-card--cyan .daily-section-count { color: #22d3ee; }
.daily-section-card--slate .daily-section-count { color: #94a3b8; }
.daily-section-card--indigo .daily-section-count { color: #6366f1; }

/* 列表样式 */
.daily-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.daily-list-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.daily-list-bullet {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--text-muted);
  margin-top: 8px;
  flex-shrink: 0;
}

.daily-list-text {
  min-width: 0;
  word-break: break-word;
}

.daily-list-text :deep(strong) {
  color: var(--text-primary);
  font-weight: 600;
}

.daily-list-text :deep(em) {
  color: var(--text-muted);
}

/* 段落与兜底 Markdown */
.daily-paragraph {
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
  word-break: break-word;
}

.daily-content {
  background: linear-gradient(145deg, rgba(30, 30, 46, 0.9), rgba(26, 26, 46, 0.95));
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 14px;
  padding: 16px;
  overflow: hidden;
}

.markdown-body {
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-secondary);
  word-break: break-word;
}

.markdown-body :deep(h1) {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0 0 10px;
}

.markdown-body :deep(h2) {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 16px 0 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.markdown-body :deep(h3) {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 12px 0 6px;
}

.markdown-body :deep(strong) {
  color: var(--text-primary);
  font-weight: 600;
}

.markdown-body :deep(ul) {
  padding-left: 16px;
  margin: 8px 0;
}

.markdown-body :deep(li) {
  margin: 4px 0;
}

.markdown-body :deep(p) {
  margin: 8px 0;
}

.markdown-body :deep(pre) {
  background: rgba(0, 0, 0, 0.25);
  border-radius: 8px;
  padding: 10px;
  overflow-x: auto;
  font-size: 11px;
}

/* 空状态 */
.daily-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 0;
  color: var(--text-muted);
}

.daily-empty i {
  font-size: 32px;
  opacity: 0.3;
}

.daily-empty p {
  font-size: 13px;
}
</style>
