<template>
  <div v-if="isOpen" class="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm" @click.self="close">
    <div class="bg-[#151925] border border-[#2a2e39] rounded-xl w-full max-w-6xl h-[80vh] flex flex-col shadow-2xl overflow-hidden">
      <!-- Header -->
      <div class="px-6 py-4 border-b border-[#2a2e39] flex items-center justify-between bg-[#1c202b]">
        <div class="flex items-center gap-4">
          <div class="flex flex-col">
            <h3 class="text-xl font-bold text-[#d1d4dc] flex items-center gap-2">
              {{ symbolName }} ({{ symbolCode }})
              <span class="text-sm font-normal text-[#787b86]">穿透式 K 线分析</span>
            </h3>
          </div>
          <div class="h-8 w-px bg-[#2a2e39]"></div>
          <div class="flex items-center gap-6">
            <div class="flex flex-col">
              <span class="text-[10px] text-[#787b86] uppercase tracking-wider">关联交易数</span>
              <span class="text-sm font-mono text-indigo-400 font-bold">{{ symbolTrades.length }}</span>
            </div>
            <div class="flex flex-col">
              <span class="text-[10px] text-[#787b86] uppercase tracking-wider">基准重叠</span>
              <span class="text-sm text-blue-400 font-medium">{{ benchmarkCode || '默认基准' }}</span>
            </div>
          </div>
        </div>
        <button @click="close" class="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-white/10 text-[#787b86] transition-colors">
          <i class="fas fa-times"></i>
        </button>
      </div>

      <!-- Main Content Area -->
      <div class="flex-1 flex overflow-hidden">
        <!-- Chart Content (Left) -->
        <div class="flex-1 relative bg-[#0A0A0A] border-r border-[#2a2e39]">
          <div v-if="isLoading" class="absolute inset-0 flex flex-col items-center justify-center bg-[#0A0A0A]/80 z-10">
            <div class="w-12 h-12 border-4 border-indigo-500/20 border-t-indigo-500 rounded-full animate-spin mb-4"></div>
            <p class="text-sm text-slate-400">正在加载历史 K 线及基准数据...</p>
          </div>
          
          <div ref="chartRef" class="w-full h-full"></div>

          <!-- Legend Overlay -->
          <div v-if="!isLoading" class="absolute top-6 left-6 flex flex-col gap-1 pointer-events-none">
            <div class="flex items-center gap-2">
              <span class="w-3 h-0.5 bg-blue-400"></span>
              <span class="text-[10px] text-blue-400/80 font-bold uppercase">基准走势 (归一化)</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="w-3 h-3 rounded-full bg-red-400/30 border border-red-500/60"></span>
              <span class="text-[10px] text-red-300/80 font-bold uppercase">买入点</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="w-1.5 h-6 bg-indigo-500/40"></span>
              <span class="text-[10px] text-indigo-300/80 font-bold uppercase">策略信号强度</span>
            </div>
          </div>
        </div>

        <!-- Sandbox Sidebar (Right) -->
        <div class="w-80 bg-[#1c202b] flex flex-col shrink-0">
          <div class="p-4 border-b border-[#2a2e39] bg-[#151925]">
            <h4 class="text-xs font-bold text-[#d1d4dc] uppercase tracking-wider flex items-center gap-2">
              <i class="fas fa-flask text-amber-400"></i>
              逻辑对照沙盒
            </h4>
            <p class="text-[10px] text-[#787b86] mt-1">剔除特定交易查看对净值的实时影响</p>
          </div>
          
          <div class="flex-1 overflow-y-auto p-2 space-y-2">
            <!-- AI Analysis Toggle -->
            <button 
              @click="analyzeSymbol" 
              :disabled="isAnalyzingSymbol || symbolTrades.length === 0"
              class="w-full py-3 mb-2 rounded-lg bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 font-bold text-xs flex items-center justify-center gap-2 hover:bg-indigo-500/20 transition-all disabled:opacity-50"
            >
              <i class="fas" :class="isAnalyzingSymbol ? 'fa-spinner fa-spin' : 'fa-brain'"></i>
              {{ isAnalyzingSymbol ? 'AI 穿透分析中...' : '标的 AI 深度分析' }}
            </button>

            <!-- AI Report Display -->
            <div v-if="aiReport" class="p-3 mb-4 rounded-lg bg-slate-900 border border-slate-700 animate-in fade-in slide-in-from-top-2">
              <div class="flex items-center justify-between mb-2">
                <span class="text-[10px] text-indigo-400 font-bold uppercase">AI 复盘结论</span>
                <button @click="aiReport = ''" class="text-slate-500 hover:text-white">
                  <i class="fas fa-times text-[10px]"></i>
                </button>
              </div>
              <div class="prose prose-invert prose-xs max-w-none text-[11px] text-slate-300 leading-relaxed" v-html="renderMarkdown(aiReport)"></div>
            </div>

            <div 
              v-for="trade in symbolTrades" 
              :key="trade.id"
              class="p-3 rounded-lg border transition-all"
              :class="backtestStore.excludedTradeIds.has(trade.id) 
                ? 'bg-red-500/5 border-red-500/30 opacity-60' 
                : 'bg-[#2a2e39]/30 border-[#2a2e39] hover:bg-[#2a2e39]/50'"
            >
              <div class="flex items-center justify-between mb-2">
                <span :class="['text-[10px] font-bold px-1.5 py-0.5 rounded uppercase', 
                  trade.action === 'buy' ? 'bg-red-500/20 text-red-400' : 'bg-green-500/20 text-green-400']">
                  {{ trade.action === 'buy' ? '买入' : '卖出' }}
                </span>
                <span class="text-[11px] font-mono text-[#787b86]">{{ trade.date }}</span>
              </div>
              <div class="flex flex-col gap-1 text-[10px] mb-3 p-1.5 rounded bg-black/20 border border-white/5">
                <div class="flex justify-between">
                  <span class="text-[#787b86]">量比:</span>
                  <span class="text-indigo-400 font-mono">{{ trade.indicators?.volume_ratio?.toFixed(2) || '--' }}</span>
                </div>
                <div v-if="trade.indicators?.gain_3d !== undefined" class="flex justify-between">
                  <span class="text-[#787b86]">3日涨幅:</span>
                  <span :class="['font-mono', (trade.indicators.gain_3d || 0) >= 0 ? 'text-red-400' : 'text-green-400']">
                    {{ trade.indicators.gain_3d.toFixed(2) }}%
                  </span>
                </div>
                <div v-if="trade.indicators?.turnover_rate !== undefined" class="flex justify-between">
                  <span class="text-[#787b86]">换手率:</span>
                  <span class="text-amber-400 font-mono">{{ trade.indicators.turnover_rate.toFixed(2) }}%</span>
                </div>
              </div>
              <div class="flex items-center justify-between text-xs mb-3">
                <span class="text-[#d1d4dc]">¥{{ trade.price.toFixed(2) }}</span>
                <span :class="['font-bold', (trade.profitLoss || 0) >= 0 ? 'text-green-400' : 'text-red-400']">
                  {{ (trade.profitLoss || 0) >= 0 ? '+' : '' }}{{ trade.profitLoss?.toFixed(2) || '0.00' }}
                </span>
              </div>
              <button 
                @click="backtestStore.toggleTradeExclusion(trade.id)"
                class="w-full py-1.5 rounded text-[10px] font-bold uppercase tracking-wider border transition-all"
                :class="backtestStore.excludedTradeIds.has(trade.id)
                  ? 'bg-amber-500/20 border-amber-500/50 text-amber-400'
                  : 'bg-white/5 border-white/10 text-white/60 hover:bg-white/10'"
              >
                {{ backtestStore.excludedTradeIds.has(trade.id) ? '已剔除 (恢复)' : '从模拟中剔除' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- Footer Info -->
      <div class="px-6 py-3 border-t border-[#2a2e39] bg-[#1c202b] flex items-center justify-between text-[11px] text-[#787b86]">
        <p>提示：点击买卖标记可查看该笔交易详情。基准曲线已按 K 线首日价格归一化。</p>
        <div class="flex items-center gap-4">
          <span>数据源: LanceDB + Tushare</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, markRaw } from 'vue';
import * as echarts from 'echarts';
import { getKlineData } from '../../api/backtestApi';
import { useBacktestStore } from '../../store/backtestStore';
import type { Trade } from '../../types/backtest';

const backtestStore = useBacktestStore();

const props = defineProps<{
  isOpen: boolean;
  symbolCode: string;
  symbolName: string;
  startDate: string;
  endDate: string;
  trades: Trade[];
  benchmarkCode?: string;
  playbackCursor?: string;
}>();

const emit = defineEmits(['close']);

// AI 分析相关
const isAnalyzingSymbol = ref(false);
const aiReport = ref('');
let analysisController: AbortController | null = null;

async function analyzeSymbol() {
  if (isAnalyzingSymbol.value || symbolTrades.value.length === 0) return;
  
  isAnalyzingSymbol.value = true;
  aiReport.value = '';
  
  try {
    // 构造当前标的特定回测快照
    const targetResult = {
      metrics: backtestStore.metrics,
      trades: symbolTrades.value,
      strategyInfo: {
        name: `标的穿透分析: ${props.symbolName} (${props.symbolCode})`,
        period: `${props.startDate} ~ ${props.endDate}`
      }
    };

    analysisController = new AbortController();
    const response = await fetch('/api/analyze_report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        strategy_id: '',
        backtest_result: targetResult
      }),
      signal: analysisController.signal
    });

    if (!response.body) throw new Error('ReadableStream not supported');
    
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          if (line.startsWith('stream:')) {
            const data = JSON.parse(line.slice(7));
            if (data.content) aiReport.value += data.content;
          }
        } catch (e) {
          console.error('Parse chunk failed', e);
        }
      }
    }
  } catch (e: any) {
    if (e.name !== 'AbortError') {
      aiReport.value = `### 分析失败\n\n${e.message}`;
    }
  } finally {
    isAnalyzingSymbol.value = false;
    analysisController = null;
  }
}

// 极其简易的 Markdown 渲染 (因为 TradeDeepDive 没有全局依赖大渲染库)
function renderMarkdown(md: string): string {
  if (!md) return '';
  return md
    .replace(/^### (.*$)/gim, '<h4 class="text-white font-bold mb-2 mt-4">$1</h4>')
    .replace(/^## (.*$)/gim, '<h3 class="text-white font-bold mb-3 mt-6">$1</h3>')
    .replace(/^# (.*$)/gim, '<h2 class="text-white font-bold mb-4 mt-8">$1</h2>')
    .replace(/\*\*(.*?)\*\*/gim, '<b class="text-white">$1</b>')
    .replace(/^- (.*$)/gim, '<div class="flex gap-2 mb-1"><span class="text-indigo-400">•</span><span>$1</span></div>')
    .replace(/\n/g, '<br>');
}

const chartRef = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;
const isLoading = ref(false);
const symbolTrades = ref<Trade[]>([]);

function normalizeSymbolCode(value?: string | null): string {
  if (!value) return '';
  const trimmed = value.trim().toUpperCase();
  const match = trimmed.match(/(\d+)/);
  if (match) {
    // CHANGED: 补齐 6 位，解决 00 开头股票无法查询名称的问题
    const digits = match[1];
    return digits.length < 6 ? digits.padStart(6, '0') : digits;
  }
  return trimmed;
}

function close() {
  emit('close');
}

async function loadData() {
  if (!props.symbolCode || !props.isOpen) return;
  
  isLoading.value = true;
  // 【修复】使用规范化后的代码进行查询
  const targetCode = normalizeSymbolCode(props.symbolCode);
  symbolTrades.value = props.trades.filter(t => normalizeSymbolCode(t.symbolCode || t.symbol) === targetCode);
  
  try {
    // 【修复】使用规范化后的代码获取K线数据
    const klineData = await getKlineData(targetCode, props.startDate, props.endDate);
    const bCode = normalizeSymbolCode(props.benchmarkCode || '000300');
    const benchmarkData = await getKlineData(bCode, props.startDate, props.endDate);
    
    renderChart(klineData, benchmarkData);
  } catch (e) {
    console.error('DeepDive load failed', e);
  } finally {
    isLoading.value = false;
  }
}

function renderChart(kline: any[], benchmark: any[]) {
  if (!chartRef.value) return;
  if (chartInstance) chartInstance.dispose();
  
  chartInstance = markRaw(echarts.init(chartRef.value));
  
  const dates = kline.map(d => d.date);
  const candleData = kline.map(d => [d.open, d.close, d.low, d.high]);

  // Handle current playback position marker
  const cursorDate = props.playbackCursor ? props.playbackCursor.split('T')[0] : null;
  const cursorLine = cursorDate && dates.includes(cursorDate) ? [{
    xAxis: cursorDate,
    label: {
      show: true,
      position: 'end',
      formatter: '当前回放点',
      backgroundColor: '#2962ff',
      color: '#fff',
      padding: [2, 4],
      borderRadius: 2
    },
    lineStyle: {
      color: '#2962ff',
      type: 'dashed',
      width: 1,
      opacity: 0.8
    }
  }] : [];

  // Force-align benchmark to start at the exact price level of the stock on the first day
  const stockStart = kline[0];
  const bStart = benchmark.find(b => b.date === stockStart?.date) || benchmark[0];
  
  const firstStockClose = stockStart?.close || 1;
  const firstBenchmarkClose = bStart?.close || 1;
  
  // Normalized calculation: Benchmark(t) * (Stock(0) / Benchmark(0))
  const normalizedBenchmark = benchmark.map(b => {
    return {
      date: b.date,
      value: (b.close / firstBenchmarkClose) * firstStockClose
    };
  }).filter(b => dates.includes(b.date)); // Keep synced with stock dates

  const benchmarkData = normalizedBenchmark.map(b => b.value);

  // Generate markPoints
  const buyPoints = symbolTrades.value
    .filter(t => t.action === 'buy')
    .map(t => ({
      name: 'Buy',
      coord: [t.date, t.price],
      value: t.price,
      itemStyle: { color: '#ef4444' },
      symbol: 'path://M12 2L1 21h22L12 2z', // Triangle up
      symbolSize: 15,
      label: {
        show: true,
        formatter: '买',
        position: 'bottom',
        color: '#ef4444',
        fontSize: 10,
        fontWeight: 'bold'
      }
    }));

  const sellPoints = symbolTrades.value
    .filter(t => t.action === 'sell')
    .map(t => ({
      name: 'Sell',
      coord: [t.date, t.price],
      value: t.price,
      itemStyle: { color: '#10b981' },
      symbol: 'path://M12 21l11-19H1l11 19z', // Triangle down
      symbolSize: 15,
      label: {
        show: true,
        formatter: '卖',
        position: 'top',
        color: '#10b981',
        fontSize: 10,
        fontWeight: 'bold'
      }
    }));
  
  console.log('[TradeDeepDive] 买卖点标记:', { 
    buyCount: buyPoints.length, 
    sellCount: sellPoints.length,
    symbolTrades: symbolTrades.value.length,
    dates: dates.length
  });

  const option: echarts.EChartsOption = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#1c202b',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc' }
    },
    xAxis: [
      {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#2a2e39' } },
        axisLabel: { show: false }, // Hide for the main chart
        gridIndex: 0
      },
      {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#2a2e39' } },
        axisLabel: { color: '#787b86', fontSize: 10 },
        gridIndex: 1
      }
    ],
    yAxis: [
      {
        scale: true,
        axisLine: { lineStyle: { color: '#2a2e39' } },
        axisLabel: { color: '#787b86', fontSize: 10 },
        splitLine: { lineStyle: { color: '#1c202b' } },
        gridIndex: 0
      },
      {
        type: 'value',
        axisLine: { lineStyle: { color: '#2a2e39' } },
        axisLabel: { show: false },
        splitLine: { show: false },
        gridIndex: 1,
        max: 100
      }
    ],
    grid: [
      { left: '3%', right: '3%', top: '5%', height: '65%', containLabel: true },
      { left: '3%', right: '3%', top: '75%', height: '20%', containLabel: true }
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: candleData,
        itemStyle: {
          color: '#f23645',
          color0: '#089981',
          borderColor: '#f23645',
          borderColor0: '#089981'
        },
        markPoint: {
          data: [...buyPoints, ...sellPoints]
        },
        markLine: {
          symbol: ['none', 'none'],
          data: cursorLine
        },
        xAxisIndex: 0,
        yAxisIndex: 0
      },
      {
        name: '大盘基准',
        type: 'line',
        data: benchmarkData,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1.5, color: '#3b82f6', opacity: 0.4 },
        z: 1,
        xAxisIndex: 0,
        yAxisIndex: 0
      },
      {
        name: '策略信号强度',
        type: 'bar',
        data: dates.map(d => {
          // Find if there's a trade on this date
          const trade = symbolTrades.value.find(t => t.date === d);
          if (trade && trade.indicators) {
            // Use real volume ratio as signal strength if available
            // Map 0-10 volume ratio to 20-100 height
            const vr = trade.indicators.volume_ratio || 0;
            return Math.min(100, 20 + vr * 10);
          }
          // Default baseline for signal presence
          return trade ? 60 : 5 + (Math.sin(dates.indexOf(d) / 5) * 5 + 5); 
        }),
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(99, 102, 241, 0.6)' },
            { offset: 1, color: 'rgba(99, 102, 241, 0.1)' }
          ])
        },
        xAxisIndex: 1,
        yAxisIndex: 1
      }
    ],
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', bottom: 10, height: 20, borderColor: 'transparent', fillerColor: 'rgba(41, 98, 255, 0.1)', handleStyle: { color: '#2962ff' } }
    ]
  };

  chartInstance.setOption(option);
}

watch(() => props.isOpen, (val) => {
  if (val) {
    setTimeout(loadData, 100);
  }
}, { immediate: true });
</script>

<style scoped>
.font-mono {
  font-family: 'JetBrains Mono', 'Roboto Mono', monospace;
}
</style>
