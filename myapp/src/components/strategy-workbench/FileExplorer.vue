<template>
  <div class="file-explorer">
    <div class="explorer-header">
      <span class="explorer-title">资源管理器</span>
      <div class="explorer-actions">
        <button
          class="icon-btn"
          title="新建策略"
          @click="$emit('new-file')"
        >
          <i class="fas fa-file-circle-plus"></i>
        </button>
        <button
          class="icon-btn"
          title="刷新"
          @click="refresh"
        >
          <i class="fas fa-rotate-right"></i>
        </button>
      </div>
    </div>

    <div class="explorer-section">
      <div
        class="section-header"
        @click="toggleSection('user')"
      >
        <i
          class="fas"
          :class="collapsed.user ? 'fa-chevron-right' : 'fa-chevron-down'"
        ></i>
        <span class="section-name">用户策略</span>
        <span class="section-count">{{ userFiles.length }}</span>
      </div>
      <ul v-show="!collapsed.user" class="file-list">
        <li
          v-for="file in userFiles"
          :key="file.path"
          class="file-item"
          :class="{ active: file.path === activePath }"
          @click="$emit('open-file', file)"
        >
          <i class="fab fa-python file-icon"></i>
          <span class="file-name">{{ file.name }}</span>
          <button
            v-if="file.is_user"
            class="file-delete"
            title="删除"
            @click.stop="$emit('delete-file', file)"
          >
            <i class="fas fa-trash"></i>
          </button>
        </li>
      </ul>
    </div>

    <div class="explorer-section">
      <div
        class="section-header"
        @click="toggleSection('system')"
      >
        <i
          class="fas"
          :class="collapsed.system ? 'fa-chevron-right' : 'fa-chevron-down'"
        ></i>
        <span class="section-name">系统策略</span>
        <span class="section-count">{{ systemFiles.length }}</span>
      </div>
      <ul v-show="!collapsed.system" class="file-list">
        <li
          v-for="file in systemFiles"
          :key="file.path"
          class="file-item"
          :class="{ active: file.path === activePath, readonly: !file.is_user }"
          @click="$emit('open-file', file)"
        >
          <i class="fab fa-python file-icon"></i>
          <span class="file-name">{{ file.name }}</span>
          <i
            v-if="!file.is_user"
            class="fas fa-lock lock-icon"
            title="系统策略只读"
          ></i>
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { apiService } from '../../services/api';

interface StrategyFile {
  name: string;
  path: string;
  is_user: boolean;
}

interface Props {
  activePath?: string;
}

interface Emits {
  (e: 'open-file', file: StrategyFile): void;
  (e: 'new-file'): void;
  (e: 'delete-file', file: StrategyFile): void;
}

defineProps<Props>();
const emit = defineEmits<Emits>();

const userFiles = ref<StrategyFile[]>([]);
const systemFiles = ref<StrategyFile[]>([]);
const collapsed = ref({ user: false, system: true });

/** 加载策略文件列表 */
async function refresh() {
  try {
    const res = await apiService.getDiscoveredStrategies();
    const strategies = res.strategies || [];
    userFiles.value = strategies
      .filter((s: any) => s.id?.startsWith('user_'))
      .map((s: any) => ({
        name: s.id.replace('user_', '') + '.py',
        path: s.file_path || s.module_path || s.id,
        is_user: true,
      }));
    systemFiles.value = strategies
      .filter((s: any) => !s.id?.startsWith('user_'))
      .map((s: any) => ({
        name: (s.id || 'unknown') + '.py',
        path: s.file_path || s.module_path || s.id,
        is_user: false,
      }));
  } catch (e) {
    console.error('[FileExplorer] 加载策略列表失败', e);
  }
}

function toggleSection(key: 'user' | 'system') {
  collapsed.value[key] = !collapsed.value[key];
}

onMounted(() => {
  refresh();
});

defineExpose({ refresh });
</script>

<style scoped>
.file-explorer {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #252526;
  color: #cccccc;
  font-size: 13px;
  overflow: hidden;
}

.explorer-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: #2d2d2d;
  border-bottom: 1px solid #1e1e1e;
}

.explorer-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #bbbbbb;
}

.explorer-actions {
  display: flex;
  gap: 4px;
}

.icon-btn {
  background: transparent;
  border: none;
  color: #cccccc;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 3px;
  font-size: 12px;
}

.icon-btn:hover {
  background: #3c3c3c;
  color: #ffffff;
}

.explorer-section {
  border-bottom: 1px solid #1e1e1e;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  cursor: pointer;
  user-select: none;
  font-weight: 500;
}

.section-header:hover {
  background: #2a2d2e;
}

.section-name {
  flex: 1;
  font-size: 12px;
}

.section-count {
  font-size: 11px;
  color: #888888;
  background: #3c3c3c;
  padding: 1px 6px;
  border-radius: 8px;
}

.file-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px 4px 24px;
  cursor: pointer;
  user-select: none;
  position: relative;
}

.file-item:hover {
  background: #2a2d2e;
}

.file-item.active {
  background: #37373d;
  color: #ffffff;
}

.file-item.readonly {
  opacity: 0.7;
}

.file-icon {
  color: #4ec9b0;
  font-size: 14px;
}

.file-name {
  flex: 1;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.lock-icon {
  color: #888888;
  font-size: 10px;
}

.file-delete {
  background: transparent;
  border: none;
  color: #888888;
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 3px;
  font-size: 11px;
  opacity: 0;
  transition: opacity 0.15s;
}

.file-item:hover .file-delete {
  opacity: 1;
}

.file-delete:hover {
  background: #5a1d1d;
  color: #f48771;
}
</style>
