<template>
  <div class="mobile-task-detail">
    <!-- 返回按钮 -->
    <button class="back-btn" @click="emit('back')">
      <i class="fa-solid fa-arrow-left" />
      <span>返回列表</span>
    </button>

    <!-- 加载状态 -->
    <div v-if="loading" class="detail-loading">
      <a-spin size="large" />
      <span>加载中...</span>
    </div>

    <template v-else-if="task">
      <MobileCard :title="task.task_type">
        <div class="detail-row">
          <span class="detail-label">状态</span>
          <MobileStatusBadge :status="task.status" />
        </div>
        <div class="detail-row">
          <span class="detail-label">创建时间</span>
          <span class="detail-value">{{ formatTime(task.created_at) }}</span>
        </div>
        <div v-if="task.started_at" class="detail-row">
          <span class="detail-label">开始时间</span>
          <span class="detail-value">{{ formatTime(task.started_at) }}</span>
        </div>
        <div v-if="task.finished_at" class="detail-row">
          <span class="detail-label">完成时间</span>
          <span class="detail-value">{{ formatTime(task.finished_at) }}</span>
        </div>
        <div v-if="task.elapsed_ms" class="detail-row">
          <span class="detail-label">耗时</span>
          <span class="detail-value">{{ formatElapsed(task.elapsed_ms) }}</span>
        </div>
        <div v-if="task.source" class="detail-row">
          <span class="detail-label">来源</span>
          <span class="detail-value">{{ task.source }}</span>
        </div>
      </MobileCard>

      <!-- 参数 -->
      <MobileCard v-if="task.params && Object.keys(task.params).length" title="参数" class="detail-section">
        <div class="params-list">
          <div v-for="(val, key) in task.params" :key="key" class="param-row">
            <span class="param-key">{{ key }}</span>
            <span class="param-val">{{ val }}</span>
          </div>
        </div>
      </MobileCard>

      <!-- 进度 -->
      <MobileCard v-if="task.progress && Object.keys(task.progress).length" title="进度" class="detail-section">
        <div v-if="task.status === 'RUNNING'" class="detail-progress">
          <div class="progress-bar-lg">
            <div
              class="progress-fill-lg"
              :style="{ width: `${calcProgress(task.progress)}%` }"
            />
          </div>
          <span class="progress-pct">{{ task.progress.message || `${calcProgress(task.progress)}%` }}</span>
        </div>
        <div class="params-list">
          <div v-for="(val, key) in task.progress" :key="key" class="param-row">
            <span class="param-key">{{ key }}</span>
            <span class="param-val">{{ val }}</span>
          </div>
        </div>
      </MobileCard>

      <!-- 错误信息 -->
      <MobileCard v-if="task.error" title="错误信息" class="detail-section">
        <div class="error-content">
          <pre>{{ JSON.stringify(task.error, null, 2) }}</pre>
        </div>
      </MobileCard>

      <!-- 报告链接 -->
      <MobileCard v-if="task.report_paths?.length" title="报告" class="detail-section">
        <div class="report-links">
          <div v-for="(path, idx) in task.report_paths" :key="idx" class="report-link">
            <i class="fa-regular fa-file-lines" />
            <span>{{ extractFilename(path) }}</span>
          </div>
        </div>
      </MobileCard>
    </template>

    <!-- 未找到 -->
    <div v-else class="detail-empty">
      <p>未找到任务信息</p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileTaskDetail - 移动端任务详情子组件
 * 展示单个任务的完整信息
 */
import { ref, onMounted, watch } from 'vue';
import { getTask } from '../../api/mobile';
import type { TaskInfo } from '../../api/mobile';
import MobileCard from '../../components/mobile/MobileCard.vue';
import MobileStatusBadge from '../../components/mobile/MobileStatusBadge.vue';

const props = defineProps<{
  /** 任务 ID */
  taskId: string;
}>();

const emit = defineEmits<{
  /** 返回列表 */
  (e: 'back'): void;
}>();

const loading = ref(false);
const task = ref<TaskInfo | null>(null);

/** 加载任务详情 */
async function loadTaskDetail() {
  if (!props.taskId) return;
  loading.value = true;
  try {
    const res = await getTask(props.taskId);
    task.value = res?.data || res;
  } catch (err) {
    console.error('[MobileTaskDetail] 加载失败:', err);
  } finally {
    loading.value = false;
  }
}

/** 格式化时间 */
function formatTime(timeStr?: string): string {
  if (!timeStr) return '--';
  try {
    const d = new Date(timeStr);
    return `${d.getFullYear()}-${(d.getMonth() + 1).toString().padStart(2, '0')}-${d.getDate().toString().padStart(2, '0')} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`;
  } catch {
    return timeStr;
  }
}

/** 格式化耗时 */
function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}min`;
}

/** 计算进度百分比 */
function calcProgress(progress: Record<string, any>): number {
  if (typeof progress.percent === 'number') return progress.percent;
  if (progress.current && progress.total) {
    return Math.round((progress.current / progress.total) * 100);
  }
  return 0;
}

/** 从路径提取文件名 */
function extractFilename(path: string): string {
  return path.split('/').pop() || path;
}

onMounted(() => {
  loadTaskDetail();
});

watch(() => props.taskId, () => {
  loadTaskDetail();
});
</script>

<style scoped>
.mobile-task-detail {
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

.detail-section {
  margin-top: 0;
}

.detail-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 60px 0;
  color: var(--text-muted);
  font-size: 13px;
}

.detail-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
}

.detail-row:last-child {
  border-bottom: none;
}

.detail-label {
  font-size: 12px;
  color: var(--text-muted);
}

.detail-value {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: 'JetBrains Mono', monospace;
}

/* 参数列表 */
.params-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.param-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
}

.param-key {
  font-size: 12px;
  color: var(--text-muted);
}

.param-val {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: 'JetBrains Mono', monospace;
  max-width: 60%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 进度条 */
.detail-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.progress-bar-lg {
  flex: 1;
  height: 6px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill-lg {
  height: 100%;
  background: #4c7fff;
  border-radius: 3px;
  transition: width 0.3s ease;
}

.progress-pct {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
  font-family: 'JetBrains Mono', monospace;
}

/* 错误信息 */
.error-content {
  background: rgba(242, 54, 69, 0.05);
  border: 1px solid rgba(242, 54, 69, 0.1);
  border-radius: 6px;
  padding: 10px;
  overflow-x: auto;
}

.error-content pre {
  margin: 0;
  font-size: 11px;
  color: #f55763;
  font-family: 'JetBrains Mono', monospace;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 报告链接 */
.report-links {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.report-link {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: rgba(255, 255, 255, 0.02);
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.report-link i {
  color: #4c7fff;
}

.detail-empty {
  text-align: center;
  color: var(--text-muted);
  padding: 40px 0;
  font-size: 13px;
}
</style>
