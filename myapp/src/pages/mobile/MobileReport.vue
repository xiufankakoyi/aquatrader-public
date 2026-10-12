<template>
  <div class="mobile-report">
    <!-- 返回按钮 -->
    <button class="back-btn" @click="router.back()">
      <i class="fa-solid fa-arrow-left" />
      <span>返回</span>
    </button>

    <!-- 加载状态 -->
    <div v-if="loading" class="report-loading">
      <a-spin size="large" />
      <span>加载中...</span>
    </div>

    <template v-else-if="report">
      <!-- 报告标题 -->
      <div class="report-header">
        <span class="report-filename">{{ report.filename }}</span>
        <span class="report-meta">{{ formatSize(report.size) }} | {{ formatTime(report.modified_at) }}</span>
      </div>

      <!-- 报告内容 -->
      <div class="mobile-report-content report-content">
        <div
          v-if="report.html"
          class="markdown-body"
          v-html="report.html"
        />
        <div
          v-else-if="report.content"
          class="markdown-body"
          v-html="renderMarkdown(report.content)"
        />
        <div v-else class="report-empty-content">
          <p>报告内容为空</p>
        </div>
      </div>
    </template>

    <!-- 未找到 -->
    <div v-else class="report-not-found">
      <i class="fa-regular fa-file-lines" />
      <p>未找到报告</p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileReport - 移动端报告查看页
 * 接收 reportId 路由参数，渲染 Markdown 报告
 */
import { ref, nextTick, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getReport } from '../../api/mobile';
import type { ReportContent } from '../../api/mobile';

const route = useRoute();
const router = useRouter();

const loading = ref(true);
const report = ref<ReportContent | null>(null);

/** 加载报告内容 */
async function loadReport() {
  const reportId = route.params.reportId as string;
  if (!reportId) return;

  loading.value = true;
  try {
    const res = await getReport(reportId);
    report.value = res?.data || res;
  } catch (err) {
    console.error('[MobileReport] 加载报告失败:', err);
  } finally {
    loading.value = false;
    processTableForMobile();
  }
}

/** 处理表格为移动端卡片布局：为 td 添加 data-label 属性 */
function processTableForMobile() {
  nextTick(() => {
    const reportEl = document.querySelector('.mobile-report-content');
    if (!reportEl) return;

    const tables = reportEl.querySelectorAll('table');
    tables.forEach(table => {
      const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent?.trim() || '');
      const rows = table.querySelectorAll('tbody tr');
      rows.forEach(row => {
        const cells = row.querySelectorAll('td');
        cells.forEach((cell, index) => {
          if (headers[index]) {
            cell.setAttribute('data-label', headers[index]);
          }
        });
      });
    });
  });
}

/** 格式化文件大小 */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
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

/** 简易 Markdown 转 HTML（降级方案） */
function renderMarkdown(md?: string): string {
  if (!md) return '';
  return md
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
    .replace(/\n{2,}/g, '<br/><br/>')
    .replace(/\n/g, '<br/>');
}

onMounted(() => {
  loadReport();
});
</script>

<style scoped>
.mobile-report {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.back-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 0;
  background: none;
  border: none;
  color: #4c7fff;
  font-size: 13px;
  cursor: pointer;
}

.report-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 60px 0;
  color: var(--text-muted);
  font-size: 13px;
}

/* 报告标题 */
.report-header {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 14px;
  background: linear-gradient(
    145deg,
    rgba(30, 30, 46, 0.9) 0%,
    rgba(26, 26, 46, 0.95) 100%
  );
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 10px;
}

.report-filename {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.report-meta {
  font-size: 11px;
  color: var(--text-muted);
}

/* 报告内容 */
.report-content {
  background: linear-gradient(
    145deg,
    rgba(30, 30, 46, 0.9) 0%,
    rgba(26, 26, 46, 0.95) 100%
  );
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 10px;
  padding: 16px;
  overflow: hidden;
}

/* Markdown 渲染样式 */
.markdown-body {
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-secondary);
  word-break: break-word;
}

.markdown-body :deep(h1) {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 16px 0 8px;
}

.markdown-body :deep(h2) {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 14px 0 6px;
}

.markdown-body :deep(h3) {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 12px 0 4px;
}

.markdown-body :deep(strong) {
  color: var(--text-primary);
  font-weight: 600;
}

.markdown-body :deep(ul) {
  padding-left: 16px;
  margin: 6px 0;
}

.markdown-body :deep(li) {
  margin: 3px 0;
}

.markdown-body :deep(p) {
  margin: 6px 0;
}

.report-empty-content {
  text-align: center;
  color: var(--text-muted);
  padding: 20px 0;
  font-size: 13px;
}

.report-not-found {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 0;
  color: var(--text-muted);
}

.report-not-found i {
  font-size: 32px;
  opacity: 0.3;
}

.report-not-found p {
  font-size: 13px;
}

/* 移动端报告 Markdown 渲染优化 */
.mobile-report-content :deep(table) {
  display: block;
  width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 13px;
}

.mobile-report-content :deep(thead) {
  display: none;
}

.mobile-report-content :deep(tbody tr) {
  display: block;
  margin-bottom: 8px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  padding: 8px 12px;
  background: rgba(30, 30, 46, 0.6);
}

.mobile-report-content :deep(tbody td) {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 4px 0;
  border: none !important;
}

.mobile-report-content :deep(tbody td::before) {
  content: attr(data-label);
  font-weight: 600;
  color: rgba(209, 212, 220, 0.7);
  margin-right: 8px;
  flex-shrink: 0;
  font-size: 12px;
}

.mobile-report-content :deep(pre) {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  max-width: 100%;
  padding: 12px;
  border-radius: 8px;
  font-size: 12px;
}

.mobile-report-content :deep(h2),
.mobile-report-content :deep(h3) {
  cursor: pointer;
  position: relative;
  padding-right: 24px;
}

.mobile-report-content :deep(h2)::after,
.mobile-report-content :deep(h3)::after {
  content: '\25BC';
  position: absolute;
  right: 0;
  font-size: 10px;
  transition: transform 0.2s;
}

.mobile-report-content :deep(details[open] > summary h2::after),
.mobile-report-content :deep(details[open] > summary h3::after) {
  transform: rotate(180deg);
}

/* 宽屏时恢复表格布局 */
@media (min-width: 768px) {
  .mobile-report-content :deep(table) {
    display: table;
  }
  .mobile-report-content :deep(thead) {
    display: table-header-group;
  }
  .mobile-report-content :deep(tbody tr) {
    display: table-row;
    margin-bottom: 0;
    border: none;
    padding: 0;
    background: none;
  }
  .mobile-report-content :deep(tbody td) {
    display: table-cell;
    padding: 4px 8px;
  }
  .mobile-report-content :deep(tbody td::before) {
    display: none;
  }
}
</style>
