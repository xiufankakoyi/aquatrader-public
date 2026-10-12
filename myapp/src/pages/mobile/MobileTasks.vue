<template>
  <div class="mobile-tasks">
    <!-- 任务详情模式 -->
    <template v-if="taskId">
      <MobileTaskDetail :task-id="taskId" @back="clearTaskId" />
    </template>

    <!-- 任务列表模式 -->
    <template v-else>
      <!-- 状态筛选 -->
      <div class="tasks-filters">
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
      <div class="tasks-toolbar">
        <span class="tasks-count">共 {{ total }} 条</span>
        <button class="refresh-btn" :disabled="loading" @click="loadTasks">
          <i class="fa-solid fa-rotate" :class="{ 'spin-icon': loading }" />
        </button>
      </div>

      <!-- 加载状态 -->
      <div v-if="loading && !items.length" class="tasks-loading">
        <a-spin size="large" />
        <span>加载中...</span>
      </div>

      <!-- 卡片列表 -->
      <div v-else-if="items.length" class="tasks-list">
        <MobileCard
          v-for="item in items"
          :key="item.task_id"
          clickable
          @click="viewTask(item.task_id)"
        >
          <div class="task-item">
            <div class="task-item-header">
              <span class="task-type">{{ item.task_type }}</span>
              <MobileStatusBadge :status="item.status" />
            </div>
            <div class="task-item-info">
              <span class="task-time">
                <i class="fa-regular fa-clock" />
                {{ formatTime(item.created_at) }}
              </span>
              <span v-if="item.elapsed_ms" class="task-elapsed">
                耗时 {{ formatElapsed(item.elapsed_ms) }}
              </span>
            </div>
            <!-- 进度条 -->
            <div v-if="item.status === 'RUNNING' && item.progress" class="task-progress">
              <div class="progress-bar">
                <div
                  class="progress-fill"
                  :style="{ width: `${calcProgress(item.progress)}%` }"
                />
              </div>
              <span class="progress-text">{{ item.progress.message || `${calcProgress(item.progress)}%` }}</span>
            </div>
            <!-- 取消按钮 -->
            <div v-if="item.status === 'QUEUED'" class="task-actions">
              <button class="cancel-btn" @click.stop="cancelTask(item.task_id)">
                取消任务
              </button>
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
      <div v-else class="tasks-empty">
        <i class="fa-regular fa-clipboard" />
        <p>暂无任务数据</p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileTasks - 移动端任务管理页
 * 状态筛选、任务列表、任务详情、取消任务
 */
import { ref, onMounted, computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getTasks, submitCommand } from '../../api/mobile';
import type { TaskInfo } from '../../api/mobile';
import MobileCard from '../../components/mobile/MobileCard.vue';
import MobileStatusBadge from '../../components/mobile/MobileStatusBadge.vue';
import MobileTaskDetail from './MobileTaskDetail.vue';

const route = useRoute();
const router = useRouter();

/** 状态筛选选项 */
const statusFilters = [
  { label: '全部', value: '' },
  { label: '排队中', value: 'QUEUED' },
  { label: '运行中', value: 'RUNNING' },
  { label: '成功', value: 'SUCCESS' },
  { label: '失败', value: 'FAILED' }
];

const currentStatus = ref('');
const items = ref<TaskInfo[]>([]);
const total = ref(0);
const loading = ref(false);
const loadingMore = ref(false);
const offset = ref(0);
const pageSize = 20;
const hasMore = ref(false);

/** 当前查看的任务 ID（从路由参数获取） */
const taskId = computed(() => (route.params.taskId as string) || '');

/** 清除任务详情，返回列表 */
function clearTaskId() {
  router.replace('/mobile/tasks');
}

/** 切换状态筛选 */
function changeStatus(status: string) {
  currentStatus.value = status;
  offset.value = 0;
  items.value = [];
  loadTasks();
}

/** 加载任务列表 */
async function loadTasks() {
  loading.value = true;
  try {
    const res = await getTasks({
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
    console.error('[MobileTasks] 加载失败:', err);
  } finally {
    loading.value = false;
  }
}

/** 加载更多 */
async function loadMore() {
  loadingMore.value = true;
  try {
    const res = await getTasks({
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
    console.error('[MobileTasks] 加载更多失败:', err);
  } finally {
    loadingMore.value = false;
  }
}

/** 查看任务详情 */
function viewTask(id: string) {
  router.push(`/mobile/tasks/${id}`);
}

/** 取消任务 */
async function cancelTask(id: string) {
  try {
    await submitCommand('cancel_task', { task_id: id });
    // 刷新列表
    loadTasks();
  } catch (err) {
    console.error('[MobileTasks] 取消任务失败:', err);
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

onMounted(() => {
  if (!taskId.value) {
    loadTasks();
  }
});
</script>

<style scoped>
.mobile-tasks {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 筛选标签 */
.tasks-filters {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  padding-bottom: 2px;
}

.tasks-filters::-webkit-scrollbar {
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
  background: rgba(8, 153, 129, 0.15);
  border-color: rgba(8, 153, 129, 0.3);
  color: #0db89a;
}

/* 工具栏 */
.tasks-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.tasks-count {
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
.tasks-loading {
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
.tasks-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 任务条目 */
.task-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.task-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.task-type {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.task-item-info {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 11px;
  color: var(--text-muted);
}

.task-time {
  display: flex;
  align-items: center;
  gap: 4px;
}

.task-elapsed {
  font-family: 'JetBrains Mono', monospace;
}

/* 进度条 */
.task-progress {
  display: flex;
  align-items: center;
  gap: 8px;
}

.progress-bar {
  flex: 1;
  height: 4px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 2px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: #4c7fff;
  border-radius: 2px;
  transition: width 0.3s ease;
}

.progress-text {
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
}

/* 取消按钮 */
.task-actions {
  display: flex;
  justify-content: flex-end;
}

.cancel-btn {
  padding: 4px 12px;
  font-size: 11px;
  border: 1px solid rgba(242, 54, 69, 0.2);
  border-radius: 6px;
  background: rgba(242, 54, 69, 0.08);
  color: #f55763;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.cancel-btn:active {
  background: rgba(242, 54, 69, 0.15);
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
.tasks-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 0;
  color: var(--text-muted);
}

.tasks-empty i {
  font-size: 32px;
  opacity: 0.3;
}

.tasks-empty p {
  font-size: 13px;
}
</style>
