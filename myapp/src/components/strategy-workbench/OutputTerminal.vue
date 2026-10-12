<template>
  <div class="output-terminal">
    <div class="terminal-header">
      <div class="tab-list">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          class="tab-btn"
          :class="{ active: activeTab === tab.id }"
          @click="activeTab = tab.id"
        >
          <i :class="tab.icon"></i>
          <span>{{ tab.label }}</span>
          <span
            v-if="tab.id === 'problems' && problems.length"
            class="badge"
          >{{ problems.length }}</span>
          <span
            v-if="tab.id === 'logs' && logs.length"
            class="badge"
          >{{ logs.length }}</span>
        </button>
      </div>
      <div class="terminal-actions">
        <button
          class="icon-btn"
          title="清空"
          @click="clearActive"
        >
          <i class="fas fa-trash-can"></i>
        </button>
        <button
          class="icon-btn"
          title="折叠/展开"
          @click="$emit('toggle')"
        >
          <i :class="collapsed ? 'fa-chevron-up' : 'fa-chevron-down'"></i>
        </button>
      </div>
    </div>

    <div v-show="!collapsed" class="terminal-body">
      <!-- 终端输出 -->
      <div v-show="activeTab === 'terminal'" class="output-content">
        <div
          v-for="(line, i) in terminalLines"
          :key="i"
          class="output-line"
          :class="`level-${line.level}`"
        >
          <span class="line-time">{{ line.time }}</span>
          <span class="line-message">{{ line.message }}</span>
        </div>
        <div v-if="!terminalLines.length" class="empty-hint">
          终端就绪。按 Ctrl+Enter 运行回测，Ctrl+S 保存策略。
        </div>
      </div>

      <!-- 日志 -->
      <div v-show="activeTab === 'logs'" class="output-content">
        <div
          v-for="(log, i) in logs"
          :key="i"
          class="output-line"
          :class="`level-${log.level || 'info'}`"
        >
          <span class="line-time">{{ log.time || '' }}</span>
          <span class="line-message">{{ log.message }}</span>
        </div>
        <div v-if="!logs.length" class="empty-hint">暂无日志</div>
      </div>

      <!-- 问题/错误 -->
      <div v-show="activeTab === 'problems'" class="output-content">
        <div
          v-for="(err, i) in problems"
          :key="i"
          class="problem-item"
          :class="`severity-${err.severity}`"
        >
          <i
            class="fas"
            :class="err.severity === 'error' ? 'fa-circle-xmark' : 'fa-triangle-exclamation'"
          ></i>
          <span class="problem-message">{{ err.message }}</span>
          <span v-if="err.code" class="problem-code">{{ err.code }}</span>
        </div>
        <div v-if="!problems.length" class="empty-hint">
          未检测到问题
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';

interface LogEntry {
  time?: string;
  message: string;
  level?: string;
}

interface Problem {
  message: string;
  severity: 'error' | 'warning';
  code?: string;
}

interface Props {
  logs: LogEntry[];
  problems: Problem[];
  collapsed?: boolean;
}

interface Emits {
  (e: 'toggle'): void;
}

const props = defineProps<Props>();
defineEmits<Emits>();

const activeTab = ref<'terminal' | 'logs' | 'problems'>('terminal');

const tabs = [
  { id: 'terminal' as const, label: '终端', icon: 'fas fa-terminal' },
  { id: 'logs' as const, label: '日志', icon: 'fas fa-list' },
  { id: 'problems' as const, label: '问题', icon: 'fas fa-circle-exclamation' },
];

// 终端输出 = 日志的简化版（带时间戳，按回测执行流程展示）
const terminalLines = computed<LogEntry[]>(() => {
  return props.logs.slice(-200);  // 最多展示 200 行
});

function clearActive() {
  // 仅切换视图，不清空原始数据（避免误删回测结果）
  // 如需清空，请在父组件中清空 logs 数组
}
</script>

<style scoped>
.output-terminal {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #1e1e1e;
  color: #cccccc;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 12px;
}

.terminal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #2d2d2d;
  border-top: 1px solid #1e1e1e;
  border-bottom: 1px solid #1e1e1e;
  padding: 0 4px;
  height: 32px;
  flex-shrink: 0;
}

.tab-list {
  display: flex;
  height: 100%;
}

.tab-btn {
  background: transparent;
  border: none;
  color: #969696;
  cursor: pointer;
  padding: 0 12px;
  height: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 2px solid transparent;
  transition: color 0.15s;
}

.tab-btn:hover {
  color: #cccccc;
}

.tab-btn.active {
  color: #ffffff;
  border-bottom-color: #007acc;
}

.badge {
  background: #4e4e4e;
  color: #ffffff;
  padding: 1px 6px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
  min-width: 18px;
  text-align: center;
}

.terminal-actions {
  display: flex;
  gap: 2px;
}

.icon-btn {
  background: transparent;
  border: none;
  color: #cccccc;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 3px;
  font-size: 12px;
}

.icon-btn:hover {
  background: #3c3c3c;
}

.terminal-body {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.output-content {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
}

.output-line {
  display: flex;
  gap: 8px;
  padding: 1px 0;
  white-space: pre-wrap;
  word-break: break-all;
}

.line-time {
  color: #858585;
  flex-shrink: 0;
  font-size: 11px;
}

.line-message {
  flex: 1;
  color: #cccccc;
}

.level-info .line-message { color: #cccccc; }
.level-warning .line-message { color: #cca700; }
.level-error .line-message { color: #f48771; }
.level-success .line-message { color: #89d185; }
.level-debug .line-message { color: #569cd6; }

.problem-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

.severity-error .fa-circle-xmark {
  color: #f48771;
}

.severity-warning .fa-triangle-exclamation {
  color: #cca700;
}

.problem-message {
  flex: 1;
}

.problem-code {
  color: #858585;
  font-size: 11px;
  background: #2d2d2d;
  padding: 1px 6px;
  border-radius: 3px;
}

.empty-hint {
  color: #6e6e6e;
  font-style: italic;
  padding: 16px 0;
  text-align: center;
}
</style>
