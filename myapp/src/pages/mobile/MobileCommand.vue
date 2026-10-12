<template>
  <div class="mobile-command">
    <MobileCard title="只读命令" accent="blue" icon="fa-solid fa-eye" :delay="0">
      <div class="command-grid">
        <MobileCommandButton
          label="查看今日日报"
          icon="fa-solid fa-newspaper"
          type="read"
          :loading="cmdLoading === 'daily'"
          @execute="executeRead('daily')"
        />
        <MobileCommandButton
          label="查看 Top 10"
          icon="fa-solid fa-ranking-star"
          type="read"
          :loading="cmdLoading === 'top10'"
          @execute="executeRead('top10')"
        />
        <MobileCommandButton
          label="查看任务状态"
          icon="fa-solid fa-list-check"
          type="read"
          :loading="cmdLoading === 'tasks'"
          @execute="executeRead('tasks')"
        />
      </div>
    </MobileCard>

    <MobileCard title="写命令（需确认）" class="cmd-section" accent="gold" icon="fa-solid fa-pen-to-square" :delay="80">
      <div class="command-grid">
        <MobileCommandButton
          label="运行新主题扫描"
          icon="fa-solid fa-magnifying-glass-chart"
          type="write"
          :loading="cmdLoading === 'scan'"
          confirm-title="确认运行新主题扫描"
          :confirm-description="`将启动紫苏叶主题扫描任务（主题：${scanTopic}），扫描可能需要数分钟完成。确认执行？`"
          @confirm="executeWrite('scan')"
        />
        <div class="scan-topic-row">
          <span class="scan-topic-label">扫描主题</span>
          <input
            v-model="scanTopic"
            class="scan-topic-input"
            placeholder="输入主题，如：新能源"
            maxlength="20"
          />
        </div>
        <MobileCommandButton
          label="刷新 Watchlist"
          icon="fa-solid fa-arrows-rotate"
          type="write"
          :loading="cmdLoading === 'refresh'"
          confirm-title="确认刷新观察池"
          confirm-description="将重新计算并刷新观察池数据，可能需要数分钟完成。确认执行？"
          @confirm="executeWrite('refresh')"
        />
        <MobileCommandButton
          label="生成 Daily Brief"
          icon="fa-solid fa-wand-magic-sparkles"
          type="write"
          :loading="cmdLoading === 'daily'"
          confirm-title="确认生成日报"
          confirm-description="将重新生成今日日报，可能需要数分钟完成。确认执行？"
          @confirm="executeWrite('daily')"
        />
      </div>
    </MobileCard>

    <!-- 执行结果 -->
    <MobileCard v-if="lastResult" title="执行结果" class="cmd-section" accent="green" icon="fa-solid fa-circle-check" :delay="160">
      <div class="result-content">
        <div v-if="lastResult.accepted" class="result-success">
          <i class="fa-solid fa-circle-check" />
          <span>命令已接受</span>
        </div>
        <div v-else class="result-failed">
          <i class="fa-solid fa-circle-xmark" />
          <span>{{ lastResult.message || '命令执行失败' }}</span>
        </div>
        <div v-if="lastResult.task_id" class="result-task">
          <span class="result-label">任务 ID:</span>
          <button class="task-link" @click="router.push(`/mobile/tasks/${lastResult.task_id}`)">
            {{ lastResult.task_id }}
            <i class="fa-solid fa-arrow-right" />
          </button>
        </div>
        <div v-if="lastResult.message" class="result-message">
          {{ lastResult.message }}
        </div>
        <pre v-if="lastResult.preview" class="result-preview">{{ lastResult.preview }}</pre>
      </div>
    </MobileCard>

    <!-- 命令历史 -->
    <MobileCard v-if="commandHistory.length" title="执行历史" class="cmd-section" accent="red" icon="fa-solid fa-clock-rotate-left" :delay="240">
      <div class="history-list">
        <div
          v-for="(h, idx) in commandHistory"
          :key="idx"
          class="history-item"
          :class="{ 'history-success': h.accepted, 'history-failed': !h.accepted }"
        >
          <span class="history-cmd">{{ h.command }}</span>
          <span class="history-time">{{ h.time }}</span>
        </div>
      </div>
    </MobileCard>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileCommand - 移动端命令控制台
 * 只读命令直接调用数据 API，写命令通过 /api/mobile/command 提交后台任务
 */
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { submitCommand, confirmCommand, getDaily, getInbox, getTasks } from '../../api/mobile';
import type { CommandResult } from '../../api/mobile';
import MobileCard from '../../components/mobile/MobileCard.vue';
import MobileCommandButton from '../../components/mobile/MobileCommandButton.vue';

const router = useRouter();

const cmdLoading = ref('');
const scanTopic = ref('新能源');
const lastResult = ref<(CommandResult & { command: string; preview?: string }) | null>(null);

/** 命令历史记录 */
const commandHistory = ref<Array<{ command: string; accepted: boolean; time: string }>>([]);

/** 只读命令映射到对应的数据 API */
const READ_COMMANDS: Record<string, () => Promise<any>> = {
  daily: () => getDaily(),
  top10: () => getInbox({ limit: 10 }),
  tasks: () => getTasks({ limit: 20 })
};

/** 写命令映射到后端 /api/mobile/command 支持的命令 */
const WRITE_COMMANDS: Record<string, string> = {
  scan: 'scan',
  refresh: 'refresh',
  daily: 'daily'
};

/** 执行只读命令 */
async function executeRead(key: string) {
  const apiCall = READ_COMMANDS[key];
  if (!apiCall) return;

  cmdLoading.value = key;
  try {
    const res = await apiCall();
    const data = res?.data || res;
    lastResult.value = {
      accepted: true,
      task_id: null,
      requires_confirmation: false,
      confirmation_token: null,
      message: '数据已获取',
      error: null,
      command: key,
      preview: formatPreview(data)
    };
    addHistory(key, true);
  } catch (err: any) {
    lastResult.value = {
      accepted: false,
      task_id: null,
      requires_confirmation: false,
      confirmation_token: null,
      message: err?.message || '数据获取失败',
      error: null,
      command: key
    };
    addHistory(key, false);
  } finally {
    cmdLoading.value = '';
  }
}

/** 格式化只读命令返回数据为预览文本 */
function formatPreview(data: any): string {
  if (!data) return '无数据';
  if (typeof data === 'string') return data.slice(0, 300);
  if (data.daily_brief?.markdown) return data.daily_brief.markdown.slice(0, 300);
  if (Array.isArray(data)) return `共 ${data.length} 条记录`;
  return JSON.stringify(data, null, 2).slice(0, 300);
}

/** 执行写命令（已确认） */
async function executeWrite(key: string) {
  const command = WRITE_COMMANDS[key];
  if (!command) return;

  cmdLoading.value = key;
  try {
    const params: Record<string, any> = {};
    if (key === 'scan') {
      params.topic = scanTopic.value || '新能源';
    }

    const res = await submitCommand(buildBackendCommand(key, params), params, false);
    const result = res?.data || res;

    if (result?.requires_confirmation && result?.confirmation_token) {
      const confirmRes = await confirmCommand(result.confirmation_token);
      const confirmData = confirmRes?.data || confirmRes;
      lastResult.value = { ...confirmData, command };
      addHistory(command, confirmData?.accepted ?? true);
    } else {
      lastResult.value = { ...result, command };
      addHistory(command, result?.accepted ?? true);
    }
  } catch (err: any) {
    lastResult.value = {
      accepted: false,
      task_id: null,
      requires_confirmation: false,
      confirmation_token: null,
      message: err?.message || '命令执行失败',
      error: null,
      command
    };
    addHistory(command, false);
  } finally {
    cmdLoading.value = '';
  }
}

/** 构建后端可识别的命令字符串 */
function buildBackendCommand(key: string, params: Record<string, any>): string {
  if (key === 'scan') {
    return `scan ${params.topic || '新能源'}`;
  }
  return key;
}

/** 添加命令历史 */
function addHistory(command: string, accepted: boolean) {
  const now = new Date();
  const time = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}`;
  commandHistory.value.unshift({ command, accepted, time });
  if (commandHistory.value.length > 20) {
    commandHistory.value = commandHistory.value.slice(0, 20);
  }
}
</script>

<style scoped>
.mobile-command {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.cmd-section {
  margin-top: 0;
}

.command-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.scan-topic-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.02);
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.04);
}

.scan-topic-label {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
}

.scan-topic-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  color: var(--text-primary);
  font-size: 13px;
  font-family: 'JetBrains Mono', monospace;
}

.scan-topic-input::placeholder {
  color: var(--text-muted);
}

/* 执行结果 */
.result-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.result-success {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #0db89a;
  font-size: 14px;
  font-weight: 500;
}

.result-failed {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #f55763;
  font-size: 14px;
  font-weight: 500;
}

.result-task {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: rgba(41, 98, 255, 0.06);
  border-radius: 6px;
}

.result-label {
  font-size: 12px;
  color: var(--text-muted);
}

.task-link {
  display: flex;
  align-items: center;
  gap: 4px;
  background: none;
  border: none;
  color: #4c7fff;
  font-size: 12px;
  font-family: 'JetBrains Mono', monospace;
  cursor: pointer;
}

.result-message {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.result-preview {
  max-height: 160px;
  overflow: auto;
  margin: 0;
  padding: 10px;
  background: rgba(0, 0, 0, 0.2);
  border-radius: 6px;
  font-size: 11px;
  color: var(--text-secondary);
  font-family: 'JetBrains Mono', monospace;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 命令历史 */
.history-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 200px;
  overflow-y: auto;
}

.history-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 12px;
}

.history-success {
  background: rgba(8, 153, 129, 0.05);
}

.history-failed {
  background: rgba(242, 54, 69, 0.05);
}

.history-cmd {
  color: var(--text-secondary);
  font-family: 'JetBrains Mono', monospace;
}

.history-time {
  color: var(--text-muted);
  font-family: 'JetBrains Mono', monospace;
}
</style>