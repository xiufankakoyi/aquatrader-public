<template>
  <div class="space-y-6">
    <DebugButton :demo-mode="demoMode" />
    <!-- Header Section -->
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-semibold text-white mb-1">股票舆情分析 Dashboard</h1>
        <p class="text-sm text-slate-400">
          基于东方财富股吧爬虫数据，采用多维度分析方法和机器学习算法进行情感分析和主题建模
        </p>
      </div>
      <div class="flex items-center space-x-4">
        <!-- T4.1: Demo 角标 (始终可见, 紫色/灰色低调显示) -->
        <span
          v-if="demoMode"
          class="inline-flex items-center space-x-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-purple-900/30 text-purple-300 border border-purple-500/40"
          title="当前为 Demo 模式, 数据为静态结构化样本, 不作为投资建议"
        >
          <span aria-hidden="true">🧪</span>
          <span>Demo / Experimental</span>
        </span>
        <div class="text-right">
          <div class="text-sm text-slate-400">当前分析数据量</div>
          <div class="text-xl font-semibold text-blue-400">{{ totalDataCount }} 条</div>
        </div>
        <div class="flex items-center space-x-3">
          <label class="flex items-center space-x-2 text-sm text-slate-300">
            <span>展示条数</span>
            <input
              type="number"
              min="10"
              max="200"
              step="10"
              v-model.number="limit"
              @change="handleLimitChange"
              class="w-20 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </label>
          <button
            class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-sm text-slate-100 border border-slate-700 flex items-center space-x-2"
            @click="reload"
          >
            <i class="fas fa-sync-alt" :class="{ 'fa-spin': loading }"></i>
            <span>刷新</span>
          </button>
        </div>
      </div>
    </div>

    <!-- T4.1: 通用错误状态 (整页级, 仅在 Demo 模式时不显示) -->
    <ErrorState
      v-if="error && !loading && !demoMode"
      title="舆情接口暂不可用"
      :message="error"
      endpoint="/api/research/stock_sentiment"
      :retryable="true"
      @retry="reload"
    />

    <!-- T4.1: 通用空数据状态 (接口成功但无数据, 且非 Demo 模式) -->
    <EmptyState
      v-else-if="!loading && !items.length && !demoMode"
      title="暂无本地股吧证据"
      description="当前没有可用的股吧舆论数据"
      hint="可以执行股吧爬虫更新数据, 或切换到 Demo 模式查看页面布局"
    />

    <!-- Dashboard Layout -->
    <div v-else class="grid grid-cols-12 gap-6">
      <!-- T4.1: 加载中状态 (覆盖主区域) -->
      <div v-if="loading && !items.length" class="col-span-12">
        <LoadingState
          title="正在加载股吧舆情数据"
          description="从本地证据 / 后端 API 拉取聚合面板数据"
          :elapsed-ms="loadingElapsedMs"
          :timeout-ms="60000"
        />
      </div>
      <!-- Top Row: Sentiment Trend and LDA Topics -->
      <div class="col-span-12 lg:col-span-6">
        <SentimentTrendChart :selected-symbol="selectedItem?.symbol" />
      </div>
      <div class="col-span-12 lg:col-span-6">
        <LdaTopicChart :selected-symbol="selectedItem?.symbol" />
      </div>
      
      <!-- Second Row: Scatter Plot and Stock List -->
      <div class="col-span-12 lg:col-span-8">
        <ScatterPlotChart :selected-symbol="selectedItem?.symbol" />
      </div>
      <div class="col-span-12 lg:col-span-4">
        <div class="bg-[#151925] rounded-lg p-4 border border-slate-800 flex flex-col">
          <h2 class="text-lg font-semibold text-white mb-3">股票舆情列表</h2>
          <!-- 搜索框 -->
          <div class="mb-3">
            <input
              type="text"
              v-model="searchText"
              placeholder="搜索股票代码或名称..."
              class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
          <div class="flex-1 overflow-auto max-h-80">
            <table class="w-full text-sm">
              <thead class="sticky top-0 bg-[#151925] z-10">
                <tr class="text-slate-400 border-b border-slate-800">
                  <th class="px-2 py-2 text-left">代码</th>
                  <th class="px-2 py-2 text-left">名称</th>
                  <th class="px-2 py-2 text-right">帖子数</th>
                  <th class="px-2 py-2 text-right">情绪</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in filteredItems"
                  :key="item.symbol"
                  class="border-b border-slate-800/60 hover:bg-slate-800/40 cursor-pointer"
                  :class="{ 'bg-slate-800/60': selectedItem && selectedItem.symbol === item.symbol }"
                  @click="handleRowClick(item)"
                >
                  <td class="px-2 py-2 text-slate-100 whitespace-nowrap">{{ item.stockCode }}</td>
                  <td class="px-2 py-2 text-slate-100 truncate max-w-[6rem]">{{ item.stockName || '-' }}</td>
                  <td class="px-2 py-2 text-slate-100 text-right tabular-nums">{{ item.totalPosts }}</td>
                  <td class="px-2 py-2 text-right tabular-nums font-medium" :class="sentimentClass(item.sentimentScore)">
                    {{ formatSentimentScore(item.sentimentScore) }}
                  </td>
                </tr>
                <tr v-if="!loading && !filteredItems.length">
                  <td colspan="4" class="px-2 py-4 text-center text-slate-500">
                    {{ searchText ? '未找到匹配的股票' : '暂无数据' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="loading" class="mt-3 text-xs text-slate-400 flex items-center space-x-2">
            <i class="fas fa-spinner fa-spin"></i>
            <span>正在加载数据...</span>
          </div>
        </div>
      </div>
      
      <!-- Third Row: Word Cloud -->
      <div class="col-span-12">
        <div class="bg-[#151925] rounded-lg p-4 border border-slate-800">
          <div class="flex items-center justify-between mb-4">
            <div class="flex items-center space-x-2">
              <h2 class="text-lg font-semibold text-white">关键词词云分析</h2>
              <div class="group relative">
                <i class="fas fa-info-circle text-slate-400 cursor-help"></i>
                <div class="absolute bottom-6 left-1/2 transform -translate-x-1/2 bg-slate-800 text-xs text-slate-200 px-3 py-2 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none whitespace-nowrap z-10">
                  基于 TF-IDF 算法提取的关键词，绿色表示正面词汇，红色表示负面词汇
                </div>
              </div>
            </div>
            <div class="text-sm text-slate-400">
              <span v-if="selectedItem">
                {{ selectedItem.stockCode }} {{ selectedItem.stockName || '-' }} | 帖子
                {{ selectedItem.totalPosts }} | 基于 TF-IDF 算法
              </span>
              <span v-else>
                点击左侧列表中的股票，查看对应的关键词词云
              </span>
            </div>
          </div>
          <div ref="wordCloudContainer" class="h-80"></div>
          <div v-if="wordCloudLoading" class="flex items-center justify-center h-80 text-slate-400">
            <i class="fas fa-spinner fa-spin mr-2"></i>
            正在加载词云数据...
          </div>
          <div v-if="wordCloudError" class="text-red-400 text-sm mt-2">
            {{ wordCloudError }}
          </div>
          <div
            v-if="!wordCloudLoading && !wordCloudError && selectedItem && (!wordCloudData || !wordCloudData.words.length)"
            class="text-slate-400 text-sm mt-2"
          >
            该股票暂无可用的帖子标题或关键词。
          </div>
        </div>
      </div>
      
      <!-- Fourth Row: Posts List (Click-through from Word Cloud) -->
      <div v-if="selectedKeyword" class="col-span-12">
        <div class="bg-[#151925] rounded-lg p-4 border border-slate-800">
          <div class="flex items-center justify-between mb-4">
            <div class="flex items-center space-x-2">
              <h2 class="text-lg font-semibold text-white">
                包含关键词 "<span class="text-blue-400">{{ selectedKeyword }}</span>" 的帖子
              </h2>
              <button
                @click="selectedKeyword = null"
                class="ml-2 px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 rounded"
              >
                <i class="fas fa-times mr-1"></i>关闭
              </button>
            </div>
            <div class="text-sm text-slate-400">
              共 {{ filteredPosts.length }} 条
            </div>
          </div>
          <div class="max-h-96 overflow-y-auto">
            <table class="w-full text-sm">
              <thead class="sticky top-0 bg-slate-800">
                <tr class="text-slate-400 border-b border-slate-700">
                  <th class="px-3 py-2 text-left">标题</th>
                  <th class="px-3 py-2 text-right">点击</th>
                  <th class="px-3 py-2 text-right">评论</th>
                  <th class="px-3 py-2 text-right">转发</th>
                  <th class="px-3 py-2 text-left">发布时间</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(post, idx) in filteredPosts"
                  :key="idx"
                  class="border-b border-slate-800/60 hover:bg-slate-800/40"
                >
                  <td class="px-3 py-2 text-slate-100">
                    <span v-html="highlightKeyword(post.title, selectedKeyword)"></span>
                  </td>
                  <td class="px-3 py-2 text-slate-300 text-right tabular-nums">{{ post.clicks }}</td>
                  <td class="px-3 py-2 text-slate-300 text-right tabular-nums">{{ post.comments }}</td>
                  <td class="px-3 py-2 text-slate-300 text-right tabular-nums">{{ post.forwards }}</td>
                  <td class="px-3 py-2 text-slate-400 text-left">{{ post.publishTime || '-' }}</td>
                </tr>
                <tr v-if="filteredPostsLoading">
                  <td colspan="5" class="px-3 py-4 text-center text-slate-400">
                    <i class="fas fa-spinner fa-spin mr-2"></i>
                    正在加载...
                  </td>
                </tr>
                <tr v-if="!filteredPostsLoading && filteredPosts.length === 0">
                  <td colspan="5" class="px-3 py-4 text-center text-slate-500">
                    暂无相关帖子
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- T4.1: Demo 模式结论提示 (放在算法说明前, 让结论区先看到) -->
    <div
      v-if="demoMode"
      class="p-3 rounded-lg bg-purple-900/20 border border-purple-500/30 text-sm text-purple-200 flex items-start space-x-2"
      role="note"
    >
      <span aria-hidden="true" class="text-base">⚠️</span>
      <div>
        <div class="font-semibold mb-1">Demo 数据，仅供参考，不作为投资建议</div>
        <div class="text-xs text-purple-300/80">
          当前为静态 Demo 模式 (source: {{ dataSourceLabel }})，所展示的股吧舆论汇总仅为结构化样本，
          不含真实帖子内容，不参与 quantFlow / 综合打分的强证据决策。
        </div>
      </div>
    </div>

    <!-- Algorithm Explanations -->
    <div class="bg-[#151925] rounded-lg p-4 border border-slate-800">
      <h3 class="text-lg font-semibold text-white mb-3">算法说明</h3>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
        <div class="flex items-start space-x-3">
          <i class="fas fa-chart-line text-blue-400 mt-1"></i>
          <div>
            <div class="text-white font-medium">情感趋势分析</div>
            <div class="text-slate-400">基于 SnowNLP 情感分析算法，对股票相关帖文进行情感倾向分析</div>
          </div>
        </div>
        <div class="flex items-start space-x-3">
          <i class="fas fa-layer-group text-green-400 mt-1"></i>
          <div>
            <div class="text-white font-medium">LDA 主题建模</div>
            <div class="text-slate-400">使用 Latent Dirichlet Allocation 算法进行主题聚类分析</div>
          </div>
        </div>
        <div class="flex items-start space-x-3">
          <i class="fas fa-project-diagram text-purple-400 mt-1"></i>
          <div>
            <div class="text-white font-medium">K-Means 聚类</div>
            <div class="text-slate-400">采用 K-Means 聚类算法对股票进行多维度特征分析</div>
          </div>
        </div>
      </div>
    </div>

    <div v-if="error" class="p-3 rounded bg-red-900/40 border border-red-500/40 text-sm text-red-200">
      {{ error }}
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * StockSentimentPage - 股票舆情分析 Dashboard
 *
 * T4.1 改造要点:
 *  - 顶部右上角加 "🧪 Demo / Experimental" chip (始终可见, 紫色低调)
 *  - 后端无数据 / 接口失败时, 降级为本地静态 demo fixture, 标记 demo_mode=true / source=static_demo
 *  - 接入通用状态组件: ErrorState / EmptyState / LoadingState
 *  - DebugButton 接收 :demo-mode, 自动联动 ApiDebugPanel
 *  - 结论区强制显示 "⚠️ Demo 数据, 仅供参考, 不作为投资建议"
 *  - 不构造虚假股吧帖子内容, 不参与 quantFlow / 综合打分强证据
 */
defineOptions({
  name: 'StockSentimentPage'
});

import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';
import 'echarts-wordcloud';
import {
  fetchStockSentimentEnvelope,
  fetchStockWordCloud,
  fetchPostsByKeyword,
  type StockSentimentItem,
  type StockWordCloudResponse
} from '../api/backtestApi';
import {
  DEMO_STOCK_SENTIMENT_ITEMS,
  buildDemoStockWordCloud,
  SENTIMENT_DEMO_FLAG
} from '../api/sentimentDemo';

// 导入通用状态组件
import ErrorState from '../components/common/ErrorState.vue';
import EmptyState from '../components/common/EmptyState.vue';
import LoadingState from '../components/common/LoadingState.vue';

// 导入新的图表组件
import SentimentTrendChart from '../components/SentimentTrendChart.vue';
import LdaTopicChart from '../components/LdaTopicChart.vue';
import ScatterPlotChart from '../components/ScatterPlotChart.vue';
import DebugButton from '../components/common/DebugButton.vue';

const items = ref<StockSentimentItem[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const limit = ref(50);
const searchText = ref('');

// T4.1: Demo 模式状态
const demoMode = ref<boolean>(false);
const dataSource = ref<string>('—');
const dataSourceLabel = computed(() => dataSource.value || 'static_demo');

// T4.1: 加载计时, 供 LoadingState 显示
const loadingStartTs = ref<number>(0);
const loadingElapsedMs = ref<number>(0);
let loadingTimer: ReturnType<typeof setInterval> | null = null;

/** 启动 loading 计时 */
function startLoadingTimer() {
  loadingStartTs.value = Date.now();
  loadingElapsedMs.value = 0;
  if (loadingTimer) clearInterval(loadingTimer);
  loadingTimer = setInterval(() => {
    loadingElapsedMs.value = Date.now() - loadingStartTs.value;
  }, 250);
}

/** 停止 loading 计时 */
function stopLoadingTimer() {
  if (loadingTimer) {
    clearInterval(loadingTimer);
    loadingTimer = null;
  }
}

// 过滤后的股票列表
const filteredItems = computed(() => {
  if (!searchText.value.trim()) {
    return items.value;
  }
  const search = searchText.value.trim().toLowerCase();
  return items.value.filter(item => {
    const code = (item.stockCode || '').toLowerCase();
    const name = (item.stockName || '').toLowerCase();
    return code.includes(search) || name.includes(search);
  });
});

const wordCloudContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const selectedItem = ref<StockSentimentItem | null>(null);
const wordCloudData = ref<StockWordCloudResponse | null>(null);
const wordCloudLoading = ref(false);
const wordCloudError = ref<string | null>(null);

// 点击穿透相关状态
const selectedKeyword = ref<string | null>(null);
const filteredPosts = ref<Array<{
  title: string;
  clicks: number;
  comments: number;
  forwards: number;
  publishTime: string;
}>>([]);
const filteredPostsLoading = ref(false);

// 计算总数据量
const totalDataCount = ref(0);

/**
 * 兜底: 切换为本地静态 Demo 模式
 *  - 不构造虚假真实股吧帖子内容
 *  - 仅使用结构化聚合样本, 提示用户当前为演示状态
 */
function fallbackToDemo(reason: string) {
  console.warn('[StockSentimentPage] 降级为 Demo 模式:', reason);
  items.value = DEMO_STOCK_SENTIMENT_ITEMS;
  totalDataCount.value = DEMO_STOCK_SENTIMENT_ITEMS.reduce(
    (sum, item) => sum + (item.totalPosts || 0),
    0
  );
  demoMode.value = true;
  dataSource.value = SENTIMENT_DEMO_FLAG.source;
  selectedItem.value = null;
  wordCloudData.value = null;
  wordCloudError.value = null;
}

async function loadData() {
  // 第一步：清空当前数据
  items.value = [];
  totalDataCount.value = 0;
  // 不立刻清除 demoMode, 等待接口明确返回真实数据时再关闭

  // 第二步：显示加载状态
  loading.value = true;
  error.value = null;
  startLoadingTimer();

  try {
    // 第三步: 通过 envelope 接口获取数据 + demo 标记
    const envelope = await fetchStockSentimentEnvelope(limit.value || 50);
    items.value = Array.isArray(envelope.data) ? envelope.data : [];

    // 计算总数据量
    totalDataCount.value = items.value.reduce((sum, item) => {
      return sum + (item.totalPosts || 0);
    }, 0);

    // T4.1: 接口成功, 同步后端 demo_mode 标记
    demoMode.value = envelope.demoMode === true;
    dataSource.value = envelope.source || (demoMode.value ? SENTIMENT_DEMO_FLAG.source : 'local_parquet');

    // 重置选择状态
    selectedItem.value = null;
    wordCloudData.value = null;
    wordCloudError.value = null;
  } catch (e) {
    // T4.1: 接口彻底失败 → 降级为 Demo, 绝不允许白屏
    console.error(e);
    error.value = '获取股票风评数据失败，请检查后端 /api/research/stock_sentiment 是否可用。已降级为本地 Demo 数据。';
    fallbackToDemo('fetchStockSentimentEnvelope fetch 失败');
  } finally {
    loading.value = false;
    stopLoadingTimer();
  }
}

function handleLimitChange() {
  if (!limit.value || limit.value < 10) {
    limit.value = 10;
  }
  if (limit.value > 200) {
    limit.value = 200;
  }
  reload();
}

function reload() {
  loadData().then(() => {
    nextTick(() => {
      updateWordCloudChart();
    });
  });
}

async function handleRowClick(item: StockSentimentItem) {
  selectedItem.value = item;
  await loadWordCloud();
}

// 格式化情感分数，修复-0.00显示问题
function formatSentimentScore(score: number): string {
  // 处理浮点数精度问题，避免显示-0.00
  const absScore = Math.abs(score);
  if (absScore < 0.005) {
    return '0.00';
  }
  return score.toFixed(2);
}

// 改进情感颜色区分，更明显的正负区分
function sentimentClass(score: number): string {
  const absScore = Math.abs(score);
  // 处理接近0的情况
  if (absScore < 0.005) {
    return 'text-slate-300'; // 中性，稍微亮一点
  }
  // 正情感：绿色系
  if (score > 0.3) return 'text-green-400 font-semibold'; // 强正情感，更亮的绿色
  if (score > 0.15) return 'text-green-300'; // 中等正情感
  if (score > 0.05) return 'text-green-200'; // 弱正情感
  // 负情感：红色系
  if (score < -0.3) return 'text-red-400 font-semibold'; // 强负情感，更亮的红色
  if (score < -0.15) return 'text-red-300'; // 中等负情感
  if (score < -0.05) return 'text-red-200'; // 弱负情感
  return 'text-slate-300'; // 中性
}

function initWordCloudChart() {
  if (!wordCloudContainer.value) return;

  if (wordCloudContainer.value.clientWidth === 0) {
    setTimeout(initWordCloudChart, 100);
    return;
  }

  if (chartInstance) {
    chartInstance.dispose();
  }

  chartInstance = echarts.init(wordCloudContainer.value);
  updateWordCloudChart();
}

function updateWordCloudChart() {
  if (!chartInstance) return;

  const data = wordCloudData.value;
  if (!data || !data.words || !data.words.length) {
    chartInstance.clear();
    return;
  }

  const seriesData = data.words.map((w) => {
    const pos = w.positiveWeight ?? 0;
    const neg = w.negativeWeight ?? 0;
    const sentiment = w.sentiment ?? 0;

    // 颜色映射：1（正面）→ 浅红，0（中性）→ 灰色，-1（负面）→ 浅绿
    let color = '#9ca3af'; // 默认中性灰色

    if (sentiment > 0.15) {
      // 正面情绪（接近1）：浅红色
      color = '#fca5a5'; // 浅红色（tailwind red-300）
    } else if (sentiment < -0.15) {
      // 负面情绪（接近-1）：浅绿色
      color = '#86efac'; // 浅绿色（tailwind green-300）
    } else {
      // 中性情绪（接近0）：灰色
      color = '#9ca3af'; // 灰色（tailwind gray-400）
    }

    return {
      name: w.word,
      value: w.weight,
      textStyle: {
        color
      },
      positiveWeight: pos,
      negativeWeight: neg,
      count: w.count,
      sentiment: sentiment
    };
  });

  const option: any = {
    tooltip: {
      show: true,
      formatter: (params: any) => {
        const d = params.data as any;
        const lines = [
          `词语：${d.name}`,
          `出现次数：${d.count ?? '-'}`,
          `权重：${(d.value ?? 0).toFixed(2)}`,
          `正向权重：${(d.positiveWeight ?? 0).toFixed(2)}`,
          `负向权重：${(d.negativeWeight ?? 0).toFixed(2)}`,
          `<span style="color: #60a5fa;">点击查看包含该词的帖子</span>`
        ];
        return lines.join('<br/>');
      }
    },
    series: [
      {
        type: 'wordCloud',
        gridSize: 8,
        sizeRange: [12, 48],
        rotationRange: [-45, 45],
        shape: 'circle',
        width: '100%',
        height: '100%',
        textStyle: {
          fontFamily:
            'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
        },
        data: seriesData
      }
    ],
    backgroundColor: 'transparent'
  };

  chartInstance.setOption(option);

  // 添加点击事件监听
  chartInstance.off('click');
  chartInstance.on('click', (params: any) => {
    if (params.data && params.data.name) {
      handleWordClick(params.data.name);
    }
  });
}

async function handleWordClick(keyword: string) {
  if (!selectedItem.value || !keyword) return;

  selectedKeyword.value = keyword;
  filteredPostsLoading.value = true;
  filteredPosts.value = [];

  try {
    const data = await fetchPostsByKeyword(selectedItem.value.symbol, keyword, 100);
    if (data && data.posts) {
      filteredPosts.value = data.posts;
    }
  } catch (e) {
    // T4.1: 关键词帖子接口失败时, 保留空列表 + 静默, 不影响 Demo 模式
    console.error('获取关键词帖子列表失败:', e);
  } finally {
    filteredPostsLoading.value = false;
  }
}

function highlightKeyword(text: string, keyword: string | null): string {
  if (!keyword || !text) return text;
  const regex = new RegExp(`(${keyword})`, 'gi');
  return text.replace(regex, '<span class="bg-yellow-500/30 text-yellow-300 font-semibold">$1</span>');
}

async function loadWordCloud() {
  if (!selectedItem.value) return;

  // 第一步：清空当前数据
  wordCloudData.value = null;
  wordCloudError.value = null;

  // 第二步：显示加载状态
  wordCloudLoading.value = true;

  try {
    // 第三步：获取新数据
    const data = await fetchStockWordCloud(selectedItem.value.symbol);
    if (data) {
      wordCloudData.value = data;
    } else {
      // T4.1: 接口返回 null 时, 用 Demo 占位 (空词云)
      wordCloudData.value = buildDemoStockWordCloud(selectedItem.value.symbol);
    }
    await nextTick();
    updateWordCloudChart();
  } catch (e) {
    // T4.1: 接口失败时, 静默降级为 Demo 占位, 不弹错误打断 Demo 流程
    console.error(e);
    wordCloudData.value = buildDemoStockWordCloud(selectedItem.value.symbol);
    wordCloudError.value = '获取词云数据失败, 已使用 Demo 占位。';
  } finally {
    wordCloudLoading.value = false;
  }
}

function handleResize() {
  chartInstance?.resize();
}

onMounted(async () => {
  await loadData();
  nextTick(() => {
    initWordCloudChart();
    window.addEventListener('resize', handleResize);
  });
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  stopLoadingTimer();
  chartInstance?.dispose();
  chartInstance = null;
});

watch(
  () => wordCloudData.value,
  () => {
    nextTick(() => updateWordCloudChart());
  },
  { deep: true }
);
</script>
