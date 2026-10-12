<template>
  <div class="w-full h-full relative">
    <TVKlineChart
      v-if="!loadError"
      ref="chartRef"
      :data="klineData"
      :show-legend="true"
      :show-volume="true"
      :show-m-a="true"
      :is-loading="isLoading"
      symbol="000300"
      @hover="handleHover"
      @click="handleClick"
    />
    
    <!-- 空状态引导层 - 当没有回测数据时显示 -->
    <div 
      v-if="!loadError && !isLoading && klineData.length > 0" 
      class="absolute inset-0 flex items-center justify-center bg-[#0A0A0A]/80 backdrop-blur-sm pointer-events-none"
    >
      <div class="text-center pointer-events-auto">
        <div class="w-16 h-16 mx-auto mb-4 rounded-full bg-[#2a2e39] flex items-center justify-center">
          <i class="fas fa-chart-line text-2xl text-[#2962ff]"></i>
        </div>
        <h3 class="text-lg font-bold text-white mb-2">开始策略回测</h3>
        <p class="text-sm text-[#787b86] mb-4 max-w-xs mx-auto">
          在右侧配置面板中设置回测参数，或点击按钮快速开始
        </p>
        <button 
          @click="$emit('start-backtest')"
          class="px-6 py-2.5 bg-[#2962ff] hover:bg-[#1e4bd8] text-white text-sm font-bold rounded-lg transition-all hover:scale-105 active:scale-95 shadow-lg shadow-[#2962ff]/25"
        >
          <i class="fas fa-play mr-2"></i>运行回测
        </button>
      </div>
    </div>
    
    <!-- 错误状态 -->
    <div v-if="loadError" class="absolute inset-0 flex items-center justify-center bg-[#0A0A0A]">
      <div class="text-center text-[#787b86]">
        <i class="fas fa-exclamation-circle text-2xl mb-2 text-[#f23645]"></i>
        <p class="text-xs mb-2">数据加载失败</p>
        <button 
          @click="retryFetch"
          class="px-3 py-1 bg-[#2a2e39] hover:bg-[#363a45] text-white text-xs rounded transition-colors"
        >
          <i class="fas fa-redo mr-1"></i>重试
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import TVKlineChart from './charts/TVKlineChart.vue';
import { request } from '../services/api';

/**
 * 默认市场K线图组件
 * 使用 TradingView Lightweight Charts 展示沪深300指数
 */

const chartRef = ref<InstanceType<typeof TVKlineChart> | null>(null);

defineEmits<{
  (e: 'start-backtest'): void;
}>();
const klineData = ref<Array<{
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}>>([]);
const isLoading = ref(false);
const loadError = ref(false);
const retryCount = ref(0);
const MAX_RETRIES = 5;
const RETRY_DELAY = 2000;

// 计算显示的日期范围
const dateRangeText = computed(() => {
  if (klineData.value.length === 0) return '';
  const firstDate = klineData.value[0].date;
  const lastDate = klineData.value[klineData.value.length - 1].date;
  return `${firstDate} ~ ${lastDate}`;
});

/**
 * 获取沪深300 K线数据
 */
async function fetchHS300Kline(): Promise<any[] | null> {
  isLoading.value = true;
  loadError.value = false;
  
  try {
    const endDate = new Date('2025-12-31');
    const startDate = new Date('2000-01-01');
    
    const start = startDate.toISOString().split('T')[0];
    const end = endDate.toISOString().split('T')[0];
    
    const res = await request<{ data: any[] }>(`/kline?symbol=000300&start=${start}&end=${end}`);
    
    if (res.ok && res.data && res.data.length > 0) {
      retryCount.value = 0;
      const allData = res.data;
      const oneYearAgo = new Date();
      oneYearAgo.setFullYear(oneYearAgo.getFullYear() - 1);
      const filteredData = allData.filter((item: any) => new Date(item.date) >= oneYearAgo);
      return filteredData.length > 0 ? filteredData : allData.slice(-252);
    }
    
    return null;
  } catch (error) {
    console.warn('获取沪深300数据失败:', error);
    return null;
  } finally {
    isLoading.value = false;
  }
}

/**
 * 重试获取数据
 */
async function retryFetch() {
  if (retryCount.value >= MAX_RETRIES) {
    loadError.value = true;
    return;
  }
  
  retryCount.value++;
  console.log(`[DefaultMarketChart] 第 ${retryCount.value} 次重试...`);
  
  const data = await fetchHS300Kline();
  
  if (data && data.length > 0) {
    klineData.value = data.map((item: any) => ({
      date: item.date,
      open: item.open,
      high: item.high,
      low: item.low,
      close: item.close,
      volume: item.volume || 0
    }));
  } else {
    setTimeout(retryFetch, RETRY_DELAY);
  }
}

/**
 * 处理悬停事件
 */
function handleHover(data: { 
  date: string; 
  open: number; 
  high: number; 
  low: number; 
  close: number; 
  volume?: number 
}) {
  // 可以在这里处理悬停逻辑，如更新外部状态
  console.log('[DefaultMarketChart] Hover:', data);
}

/**
 * 处理点击事件
 */
function handleClick(data: { date: string; price: number }) {
  console.log('[DefaultMarketChart] Click:', data);
}

/**
 * 初始化加载数据
 */
async function initData() {
  const data = await fetchHS300Kline();
  
  if (data && data.length > 0) {
    klineData.value = data.map((item: any) => ({
      date: item.date,
      open: item.open,
      high: item.high,
      low: item.low,
      close: item.close,
      volume: item.volume || 0
    }));
  } else {
    setTimeout(retryFetch, RETRY_DELAY);
  }
}

onMounted(() => {
  initData();
});
</script>
