<template>
  <div>
    <div class="bg-[#151925] rounded-lg p-6 border border-slate-800">
      <h2 class="text-xl font-semibold text-white mb-4">历史记录</h2>

      <!-- T2.7: 加载态 (默认 3s 内响应; >5s 显示慢查询) -->
      <LoadingState
        v-if="tradesLoading"
        title="正在加载交易记录"
        description="正在从后端拉取最近 30 天的交易数据"
        :elapsed-ms="elapsedMs"
        :timeout-ms="tradesTimeoutMs"
      />

      <!-- T2.7: 超时态 (>15s 自动转 TimeoutState) -->
      <TimeoutState
        v-else-if="timedOut"
        :endpoint="endpointLabel"
        :elapsed-ms="elapsedMs"
        :query-params="tradesQueryParams"
        @retry="refresh"
      />

      <!-- T2.7: 错误态 (PORTFOLIO_INVALID_PARAMS / INTERNAL_ERROR 等) -->
      <ErrorState
        v-else-if="tradesError"
        :title="errorTitle"
        :message="tradesError"
        :status="errorStatus"
        :code="errorCode"
        :endpoint="endpointLabel"
        :detail="errorDetail"
        :retryable="true"
        :last-request-time="lastRequestTime"
        @retry="refresh"
      />

      <!-- T2.7: 空数据态 (demo_mode=true 时显示"可查看模拟盘记录"提示) -->
      <EmptyState
        v-else-if="trades.length === 0"
        title="暂无交易记录"
        :description="emptyDescription"
        hint="可查看模拟盘记录 / QMT 未接入，真实交易不可用"
      />

      <!-- 正常态: 表格 + 分页 -->
      <div v-else class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="bg-slate-800/50">
            <tr>
              <th class="px-4 py-3 text-left text-slate-400 font-semibold">日期</th>
              <th class="px-4 py-3 text-left text-slate-400 font-semibold">股票</th>
              <th class="px-4 py-3 text-center text-slate-400 font-semibold">动作</th>
              <th class="px-4 py-3 text-right text-slate-400 font-semibold">价格</th>
              <th class="px-4 py-3 text-right text-slate-400 font-semibold">股数</th>
              <th class="px-4 py-3 text-right text-slate-400 font-semibold">金额</th>
              <th class="px-4 py-3 text-right text-slate-400 font-semibold">盈亏</th>
              <th class="px-4 py-3 text-left text-slate-400 font-semibold">备注</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="trade in trades"
              :key="trade.id"
              class="border-b border-slate-800 hover:bg-slate-800/30 transition-colors"
            >
              <td class="px-4 py-3 text-slate-300">{{ formatDate(trade.date) }}</td>
              <td class="px-4 py-3 text-white font-medium">
                {{ trade.stock_name || trade.stock_code }}
                <span class="text-slate-500 text-xs ml-1">{{ trade.stock_code }}</span>
              </td>
              <td class="px-4 py-3 text-center">
                <span :class="getActionClass(trade.action)">
                  {{ getActionLabel(trade.action) }}
                </span>
              </td>
              <td class="px-4 py-3 text-right text-slate-300">{{ trade.price.toFixed(2) }}</td>
              <td class="px-4 py-3 text-right text-slate-300">{{ trade.shares.toFixed(0) }}</td>
              <td class="px-4 py-3 text-right" :class="trade.amount >= 0 ? 'text-red-400' : 'text-green-400'">
                {{ formatAmount(trade.amount) }}
              </td>
              <td class="px-4 py-3 text-right text-slate-300">
                {{ trade.profit_loss === null ? '-' : trade.profit_loss.toFixed(2) }}
              </td>
              <td class="px-4 py-3 text-slate-500 text-xs max-w-xs truncate" :title="trade.notes">
                {{ trade.notes || '-' }}
              </td>
            </tr>
          </tbody>
        </table>

        <!-- 分页信息 + 翻页 -->
        <div class="flex items-center justify-between mt-4 text-xs text-slate-400">
          <div>
            共 {{ tradesMeta?.total ?? 0 }} 条
            <span v-if="tradesMeta?.source" class="ml-2 text-slate-500">
              (来源: {{ sourceLabel }})
            </span>
            <span v-if="tradesMeta?.elapsed_ms !== undefined" class="ml-2 text-slate-500">
              · 耗时 {{ tradesMeta.elapsed_ms }}ms
            </span>
          </div>
          <div class="flex items-center gap-2">
            <button
              class="px-3 py-1 bg-slate-800 hover:bg-slate-700 rounded disabled:opacity-40"
              :disabled="!canPrev"
              @click="changePage((tradesMeta?.page ?? 1) - 1)"
            >
              上一页
            </button>
            <span>第 {{ tradesMeta?.page ?? 1 }} / {{ totalPages }} 页</span>
            <button
              class="px-3 py-1 bg-slate-800 hover:bg-slate-700 rounded disabled:opacity-40"
              :disabled="!canNext"
              @click="changePage((tradesMeta?.page ?? 1) + 1)"
            >
              下一页
            </button>
          </div>
        </div>
      </div>

      <!-- 兼容旧 store 触发的"暂无历史记录"提示 (回测会话列表) -->
      <EmptyState
        v-if="!tradesLoading && !tradesError && historySessions.length === 0 && trades.length > 0"
        title="回测会话列表为空"
        description="尚未完成可保存的回测"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * HistoryRecordsPage - 历史记录页
 *
 * T2.7: 改造说明
 *  - 新增 /api/portfolio/trades 调用, 默认 30 天 / page_size=50
 *  - 接入 LoadingState / ErrorState / TimeoutState / EmptyState
 *  - 保留 useHistoryStore 既有用法, 不破坏回测会话列表渲染
 *  - 后端默认窗口: 30 天; 客户端 timeout 60s, UI >15s 转 TimeoutState
 */
import { computed, onMounted, onBeforeUnmount, ref } from 'vue';
import { useRouter } from 'vue-router';
import { useHistoryStore } from '../store/historyStore';
import { useStrategyStore } from '../store/strategyStore';
import { usePortfolioStore } from '../store/portfolioStore';
import type { StrategySession } from '../types/backtest';
import type { ApiError } from '../services/api';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingState from '../components/common/LoadingState.vue';
import TimeoutState from '../components/common/TimeoutState.vue';

const router = useRouter();
const historyStore = useHistoryStore();
const strategyStore = useStrategyStore();
const portfolioStore = usePortfolioStore();

// T2.7: 交易记录 store 解构
const trades = computed(() => portfolioStore.trades);
const tradesMeta = computed(() => portfolioStore.tradesMeta);
const tradesLoading = computed(() => portfolioStore.tradesLoading);
const tradesError = computed(() => portfolioStore.tradesError);
const tradesQueryParams = computed(() => portfolioStore.tradesQueryParams);
const tradesTimeoutMs = computed(() => portfolioStore.tradesTimeoutMs);

// 兼容旧 useHistoryStore 用法 (回测会话列表)
const historySessions = computed(() => {
  if (historyStore.records.length > 0) {
    return historyStore.paginatedRecords.map(record => ({
      id: record.id,
      strategyId: record.id,
      name: record.strategyName,
      createdAt: record.createdAt,
      updatedAt: record.createdAt,
      version: 'v1.2.4',
      summary: {
        id: record.id,
        strategyName: record.strategyName,
        date: record.dateRange,
        createdAt: record.createdAt,
        status: record.status,
        metrics: record.metrics,
        totalReturn: record.metrics?.totalReturn,
        annualizedReturn: record.metrics?.annualizedReturn,
        maxDrawdown: record.metrics?.maxDrawdown,
      }
    } as StrategySession));
  }

  const sessions: StrategySession[] = [];
  const sessionsMap = strategyStore.strategySessions || {};
  Object.values(sessionsMap).forEach((strategySessions: StrategySession[]) => {
    sessions.push(...strategySessions);
  });
  return sessions.sort((a, b) => {
    const dateA = new Date(a.createdAt).getTime();
    const dateB = new Date(b.createdAt).getTime();
    return dateB - dateA;
  });
});

// T2.7: 慢查询 / 超时态控制
// elapsedMs 用 setInterval 每 200ms 推进, 用于 LoadingState 倒计时
const elapsedMs = ref(0);
const timedOut = ref(false);
let timer: ReturnType<typeof setInterval> | null = null;

const endpointLabel = '/api/portfolio/trades';

const errorTitle = computed(() => {
  if (errorCode.value === 'PORTFOLIO_INVALID_PARAMS') return '请求参数错误';
  if (errorCode.value === 'PORTFOLIO_DATA_SOURCE_MISSING') return '数据源不可用';
  if (errorCode.value === 'PORTFOLIO_QUERY_TIMEOUT') return '查询超时';
  return '加载失败';
});

// 错误码 / 状态 / 详情 / 请求时间
const errorCode = ref<string>('');
const errorStatus = ref<number | string>('');
const errorDetail = ref<string>('');
const lastRequestTime = ref<string>('');

const startTimer = () => {
  stopTimer();
  const startedAt = performance.now();
  elapsedMs.value = 0;
  timedOut.value = false;
  timer = setInterval(() => {
    elapsedMs.value = Math.round(performance.now() - startedAt);
    // 客户端 15s 显示超时态, 与 LoadingState.timeoutMs 对齐
    if (elapsedMs.value >= tradesTimeoutMs.value) {
      timedOut.value = true;
      stopTimer();
    }
  }, 200);
};

const stopTimer = () => {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
};

async function refresh() {
  startTimer();
  errorCode.value = '';
  errorStatus.value = '';
  errorDetail.value = '';
  lastRequestTime.value = new Date().toISOString();
  try {
    await portfolioStore.fetchTrades({
      page: tradesMeta.value?.page ?? 1,
      page_size: tradesMeta.value?.page_size ?? 50,
    });
    // 成功后清理超时态
    timedOut.value = false;
  } catch (e) {
    // 把 ApiError 字段映射到 ErrorState
    const err = e as ApiError;
    errorCode.value = (err?.code as string) || 'PORTFOLIO_INTERNAL_ERROR';
    errorStatus.value = err?.status ?? '';
    errorDetail.value = err?.detail ? safeStringify(err.detail) : '';
  } finally {
    stopTimer();
  }
}

async function changePage(nextPage: number) {
  startTimer();
  errorCode.value = '';
  errorStatus.value = '';
  lastRequestTime.value = new Date().toISOString();
  try {
    await portfolioStore.fetchTrades({
      page: nextPage,
      page_size: tradesMeta.value?.page_size ?? 50,
    });
    timedOut.value = false;
  } catch (e) {
    const err = e as ApiError;
    errorCode.value = (err?.code as string) || 'PORTFOLIO_INTERNAL_ERROR';
    errorStatus.value = err?.status ?? '';
    errorDetail.value = err?.detail ? safeStringify(err.detail) : '';
  } finally {
    stopTimer();
  }
}

// T2.7: 默认 30 天窗口 / page_size=50
async function fetchFirstPage() {
  startTimer();
  errorCode.value = '';
  errorStatus.value = '';
  lastRequestTime.value = new Date().toISOString();
  try {
    await portfolioStore.fetchTrades({
      page: 1,
      page_size: 50,
    });
    timedOut.value = false;
  } catch (e) {
    const err = e as ApiError;
    errorCode.value = (err?.code as string) || 'PORTFOLIO_INTERNAL_ERROR';
    errorStatus.value = err?.status ?? '';
    errorDetail.value = err?.detail ? safeStringify(err.detail) : '';
  } finally {
    stopTimer();
  }
}

onMounted(() => {
  fetchFirstPage();
});

onBeforeUnmount(() => {
  stopTimer();
});

// ---- 工具函数 ----

function formatDate(dateStr: string): string {
  if (!dateStr) return '-';
  // YYYY-MM-DD 短格式
  if (/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return dateStr;
  const date = new Date(dateStr);
  if (isNaN(date.getTime())) return dateStr;
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
}

function formatAmount(amount: number): string {
  // 金额 (万/亿) 格式 - 项目规则要求
  const abs = Math.abs(amount);
  if (abs >= 1e8) return `${(amount / 1e8).toFixed(2)} 亿`;
  if (abs >= 1e4) return `${(amount / 1e4).toFixed(2)} 万`;
  return amount.toFixed(2);
}

function getActionLabel(action: string): string {
  const map: Record<string, string> = {
    buy: '买入',
    sell: '卖出',
    add: '加仓',
    reduce: '减仓',
  };
  return map[action] || action;
}

function getActionClass(action: string): string {
  if (action === 'buy' || action === 'add') {
    return 'px-2 py-0.5 rounded text-xs font-medium bg-red-500/20 text-red-400';
  }
  if (action === 'sell' || action === 'reduce') {
    return 'px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400';
  }
  return 'px-2 py-0.5 rounded text-xs font-medium bg-slate-700 text-slate-300';
}

function safeStringify(obj: any): string {
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}

// 兼容旧 store 触发的 viewSession
function viewSession(session: StrategySession) {
  historyStore.selectRecord({
    id: session.id,
    strategyName: session.name,
    dateRange: session.summary.date || '',
    createdAt: session.createdAt,
    status: session.summary.status || 'completed',
    metrics: session.summary.metrics,
  });
  router.push(`/strategy/${session.strategyId}`);
}

// T2.7: 分页元数据
const totalPages = computed(() => {
  const total = tradesMeta.value?.total ?? 0;
  const ps = tradesMeta.value?.page_size ?? 50;
  if (total === 0) return 1;
  return Math.max(1, Math.ceil(total / ps));
});

const canPrev = computed(() => (tradesMeta.value?.page ?? 1) > 1);
const canNext = computed(() => (tradesMeta.value?.page ?? 1) < totalPages.value);

const sourceLabel = computed(() => {
  const s = tradesMeta.value?.source;
  if (s === 'trade_records') return 'SQLite trade_records';
  if (s === 'position_history') return 'position_history.parquet (demo)';
  if (s === 'empty') return '暂无数据 (demo)';
  return s || '-';
});

const emptyDescription = computed(() => {
  if (tradesMeta.value?.demo_mode) {
    return '当前窗口内没有交易记录, 系统已自动降级到模拟盘数据';
  }
  return '请检查日期范围 / QMT 接入状态, 或调整 page 参数重试';
});
</script>
