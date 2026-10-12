<template>
  <div class="bg-[#151925] rounded-lg p-4 border border-slate-800">
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center space-x-2">
        <h2 class="text-lg font-semibold text-white">{{ selectedSymbol && !showOverall ? (stockName ? `${stockName} - 多空博弈走势图` : '多空博弈走势图') : '热度 vs 情感分析' }}</h2>
        <div class="group relative">
          <i class="fas fa-info-circle text-slate-400 cursor-help"></i>
          <div class="absolute bottom-6 left-1/2 transform -translate-x-1/2 bg-slate-800 text-xs text-slate-200 px-3 py-2 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none whitespace-nowrap z-10">
            {{ selectedSymbol && !showOverall ? '多空博弈走势图：横轴为时间，纵轴为帖子数量。红线表示看多，绿线表示看空，灰线表示中立。' : '多维数据关系分析：横轴为讨论热度，纵轴为情感倾向' }}
          </div>
        </div>
      </div>
      <div class="flex items-center space-x-3">
        <div class="text-sm text-slate-400">
          {{ selectedSymbol && !showOverall ? '按时间统计的多空情绪变化' : '基于 K-Means 聚类分析' }}
        </div>
        <button
          v-if="selectedSymbol && !showOverall"
          @click="toggleView"
          class="px-3 py-1.5 text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 rounded-lg border border-slate-600 transition-colors flex items-center space-x-1"
          title="切换到整体股票散点图"
        >
          <i class="fas fa-chart-scatter"></i>
          <span>查看全部股票</span>
        </button>
        <button
          v-if="selectedSymbol && showOverall"
          @click="toggleView"
          class="px-3 py-1.5 text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 rounded-lg border border-slate-600 transition-colors flex items-center space-x-1"
          title="切换回个股多空博弈走势图"
        >
          <i class="fas fa-chart-line"></i>
          <span>查看个股走势</span>
        </button>
      </div>
    </div>
    <div ref="chartContainer" class="h-80"></div>
    <div v-if="loading" class="flex items-center justify-center h-80 text-slate-400">
      <i class="fas fa-spinner fa-spin mr-2"></i>
      {{ selectedSymbol && !showOverall ? '正在加载多空博弈数据...' : '正在加载散点图数据...' }}
    </div>
    <div v-if="error" class="text-red-400 text-sm mt-2">
      {{ error }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';
import { fetchScatterData, type ScatterDataPoint, fetchStockSentimentTimeline, type StockSentimentTimelinePoint } from '../api/backtestApi';

interface Props {
  selectedSymbol?: string;
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const loading = ref(false);
const error = ref<string | null>(null);
const data = ref<ScatterDataPoint[]>([]);
const timelineData = ref<StockSentimentTimelinePoint[]>([]);
const stockName = ref<string>('');
const selectedSymbol = computed(() => props.selectedSymbol);
const showOverall = ref(false); // CHANGED: 控制是否显示整体股票散点图

async function loadData() {
  // 第一步：清空当前数据
  data.value = [];
  timelineData.value = [];
  stockName.value = '';
  error.value = null;
  
  // 第二步：显示加载状态
  loading.value = true;
  
  try {
    // CHANGED: 如果有 selectedSymbol 且 showOverall 为 false，显示个股折线图
    // 否则显示整体股票散点图
    if (props.selectedSymbol && !showOverall.value) {
      // 个股模式，获取多空博弈时间序列数据
      const result = await fetchStockSentimentTimeline(props.selectedSymbol);
      console.log('多空博弈时间序列数据获取成功:', {
        selectedSymbol: props.selectedSymbol,
        dataLength: result.data.length,
        stockName: result.stockName
      });
      timelineData.value = result.data;
      stockName.value = result.stockName || '';
    } else {
      // 聚合模式，获取散点图数据
      const result = await fetchScatterData();
      console.log('散点图数据获取成功:', {
        dataLength: result.length,
        firstItem: result[0]
      });
      data.value = result;
    }
    await nextTick();
    updateChart();
  } catch (e) {
    console.error('获取数据失败:', e);
    error.value = '获取数据失败，请检查后端 API';
  } finally {
    loading.value = false;
  }
}

function toggleView() {
  // CHANGED: 切换视图模式
  showOverall.value = !showOverall.value;
  loadData();
}

function initChart() {
  if (!chartContainer.value) return;

  if (chartContainer.value.clientWidth === 0) {
    setTimeout(initChart, 100);
    return;
  }

  if (chartInstance) {
    chartInstance.dispose();
  }

  chartInstance = echarts.init(chartContainer.value);
  updateChart();
}

function updateChart() {
  if (!chartInstance) return;

  // CHANGED: 如果有 selectedSymbol 且 showOverall 为 false，显示折线图（多空博弈走势图）
  // 否则显示散点图
  if (props.selectedSymbol && !showOverall.value) {
    updateTimelineChart();
    return;
  }

  // 确保 data.value 是数组类型
  const points = Array.isArray(data.value) ? data.value : [];
  
  if (points.length === 0) {
    chartInstance.clear();
    return;
  }
  
  // 判断是否为评论数据模式
  const isCommentMode = points.length > 0 && points[0]?.is_comment === true;
  
  // CHANGED: 将 isCommentMode 提升到函数作用域，以便在 series 配置中使用
  console.log('散点图模式:', { isCommentMode, pointsCount: points.length });

  // CHANGED: 检查数据格式，判断是归一化数据还是原始数据
  // 归一化数据通常在 [0, 1] 范围内，原始数据通常 > 1
  const sampleX = points.find(p => p.x !== undefined && p.x !== null)?.x;
  const isNormalizedData = sampleX !== undefined && sampleX >= 0 && sampleX <= 1;
  
  // 收集所有有效的 X 值
  const xValues = points
    .map(p => {
      // 如果有原始 comment_count 且 > 1，使用原始值（适合对数轴）
      if (p.comment_count !== undefined && p.comment_count > 1) {
        return p.comment_count;
      }
      // 否则使用归一化值（需要判断是否适合对数轴）
      if (p.x !== undefined && p.x !== null && p.x > 0) {
        // 如果是归一化数据（0-1范围），不适合直接用于对数轴
        // 我们需要使用原始值，如果没有原始值，就不使用对数轴
        return isNormalizedData ? null : p.x;
      }
      return null;
    })
    .filter(x => x !== null && x !== undefined && !isNaN(x) && x > 0);
  
  console.log('散点图数据检查:', {
    pointsCount: points.length,
    xValuesCount: xValues.length,
    isNormalizedData,
    sampleX,
    hasCommentCount: points.some(p => p.comment_count !== undefined && p.comment_count > 0),
    xValues: xValues.slice(0, 10), // 显示前10个
    firstPoint: points[0]
  });
  
  if (xValues.length === 0) {
    console.warn('没有有效的 X 值，清空图表');
    chartInstance.clear();
    return;
  }
  
  // 判断是否使用对数轴：如果有足够的原始值（> 1），使用对数轴
  const hasRawValues = xValues.some(x => x > 1);
  const useLogAxis = hasRawValues && xValues.length > 0;
  
  // 将 useLogAxis 提升到外层作用域，供后续使用
  const sortedX = [...xValues].sort((a, b) => a - b);
  const xMin = sortedX[0];
  const xMax = sortedX[sortedX.length - 1];
  
  // 类型保护
  if (xMax === undefined || xMin === undefined) {
    console.warn('X 轴范围无效，清空图表');
    chartInstance.clear();
    return;
  }
  
  let xAxisMin: number;
  let xAxisMax: number;
  
  if (useLogAxis) {
    // 对数轴：最小值不能为 0，向上取整到最近的 10 的幂次
    const logMin = Math.log10(Math.max(xMin, 0.0001));
    const logMax = Math.log10(xMax);
    xAxisMin = Math.pow(10, Math.floor(logMin));
    xAxisMax = Math.pow(10, Math.ceil(logMax));
    console.log('使用对数轴:', { xMin, xMax, xAxisMin, xAxisMax });
  } else {
    // 线性轴：归一化数据使用 [0, 1] 范围
    const xRange = xMax - xMin;
    xAxisMin = Math.max(0, xMin - xRange * 0.05);
    xAxisMax = Math.min(1.1, xMax + xRange * 0.05);
    console.log('使用线性轴（归一化数据）:', { xMin, xMax, xAxisMin, xAxisMax });
  }
  
  // 计算Y轴动态范围（根据数据自动缩放）
  const yValues = points.map(p => p.y).filter(y => y !== null && y !== undefined);
  if (yValues.length === 0) {
    chartInstance.clear();
    return;
  }
  
  const sortedY = [...yValues].sort((a, b) => a - b);
  const yMin = sortedY[0];
  const yMax = sortedY[sortedY.length - 1];
  
  // 添加10%的边距
  const yRange = yMax - yMin;
  const yAxisMin = yMin - yRange * 0.1;
  const yAxisMax = yMax + yRange * 0.1;
  
  // 按颜色分组数据
  const groupedData: Record<string, ScatterDataPoint[]> = {};
  points.forEach(point => {
    const color = point.color || 'default';
    if (!groupedData[color]) {
      groupedData[color] = [];
    }
    groupedData[color].push(point);
  });

  // 颜色到标签的映射
  const colorToLabel: Record<string, string> = {
    '#10b981': '看空',
    '#ef4444': '看多',
    '#94a3b8': '中性'
  };

  const series = Object.entries(groupedData).map(([color, points]) => ({
    name: colorToLabel[color] || color, // 使用友好的标签名称
    type: 'scatter',
    data: points.map(p => {
      // CHANGED: 确保 size 有有效值，如果为 0 或无效，使用默认值
      const pointSize = (p.size && p.size > 0 && !isNaN(p.size)) ? p.size : (p.is_comment ? 20 : 10);
      // CHANGED: 根据数据格式选择 X 轴值
      // 如果有原始 comment_count 且 > 1，使用原始值（用于对数轴）
      // 否则使用归一化值 p.x（用于线性轴）
      let xValue: number;
      if (p.comment_count !== undefined && p.comment_count > 1) {
        // 使用原始评论数（适合对数轴）
        xValue = p.comment_count;
      } else if (p.x !== undefined && p.x !== null && p.x > 0) {
        // 使用归一化值（适合线性轴）
        xValue = p.x;
      } else {
        // 默认值：对于对数轴需要 > 0，对于线性轴可以是 0
        xValue = useLogAxis ? 0.0001 : 0.0001;
      }
      
      return {
        value: [xValue, p.y, pointSize],
        symbol: p.symbol,
        name: p.name,
        comment_count: p.comment_count || xValue, // 传递原始评论数用于 tooltip 显示
        is_comment: p.is_comment, // 传递是否为评论数据
        post_title: p.post_title, // 传递评论标题
        market_cap: p.market_cap, // 传递市值
        normalized_x: p.x // 保留归一化值用于参考
      };
    }),
    symbolSize: 8, // CHANGED: 固定小尺寸，避免点太大像马赛克
    itemStyle: {
      color: color,
      opacity: 0.6 // CHANGED: 设置较低透明度，让重叠的点显现出层次（浅色=少量点，深色=大量点重叠）
    },
    label: {
      show: false, // CHANGED: 默认不显示标签，只在鼠标悬停时显示
    },
    emphasis: {
      itemStyle: {
        borderColor: '#fff',
        borderWidth: 2,
        opacity: 1
      },
      label: {
        show: true, // 鼠标悬停时显示标签
        position: 'right',
        formatter: (params: any) => {
          // 显示股票名称或代码
          const dataPoint = params.data;
          const name = dataPoint.name || '';
          const symbol = dataPoint.symbol || '';
          
          // 对于个股评论模式，显示股票名称；如果没有名称，显示代码
          if (isCommentMode) {
            return name || symbol;
          } else {
            // 对于股票聚合模式，显示股票名称（如果有）
            return name || '';
          }
        },
        fontSize: 12,
        fontWeight: 'bold',
        color: '#cbd5e1',
        backgroundColor: 'rgba(15, 23, 42, 0.8)',
        padding: [2, 4],
        borderRadius: 2
      }
    },
    large: true, // 启用大数据量优化
    largeThreshold: 200 // 超过200个点时启用优化
  }));

  const option = {
    tooltip: {
      trigger: 'item',
      formatter: (params: any) => {
        // ECharts 的 tooltip formatter 参数结构
        const dataPoint = params.data;
        const value = dataPoint.value || [];
        const symbol = dataPoint.symbol || 'N/A';
        const name = dataPoint.name || '';
        const isComment = dataPoint.is_comment === true;
        
        // 获取原始评论数（如果存在），否则使用归一化后的值
        const rawCommentCount = dataPoint.comment_count !== undefined 
          ? dataPoint.comment_count 
          : value[0];
        
        if (isComment) {
          // 评论数据模式：显示评论信息
          const postTitle = dataPoint.post_title || '无标题';
          return `<div style="font-weight: bold; margin-bottom: 4px;">${name || symbol}</div>
                  <div>评论标题: ${postTitle}</div>
                  <div>评论数: ${rawCommentCount}</div>
                  <div>情感倾向: ${value[1] !== undefined ? value[1].toFixed(3) : '0.000'}</div>`;
        } else {
          // 股票数据模式：显示股票信息
          const displayName = name ? `${symbol} (${name})` : symbol;
          return `<div style="font-weight: bold; margin-bottom: 4px;">${displayName}</div>
                  <div>股票代码: ${symbol}</div>
                  <div>讨论热度: ${rawCommentCount}</div>
                  <div>情感倾向: ${value[1] !== undefined ? value[1].toFixed(3) : '0.000'}</div>
                  <div>数据量: ${value[2] || 0}</div>`;
        }
      }
    },
    legend: {
      data: series.map(s => s.name), // 使用 series 的名称，确保匹配
      textStyle: {
        color: '#94a3b8'
      }
    },
    grid: {
      left: '1%',      // CHANGED: 左边距几乎贴边，最大化图表区域
      right: '2%',     // CHANGED: 右边距几乎贴边，最大化图表区域
      bottom: '15%',   // CHANGED: 底部留出空间给 dataZoom 滚动条
      top: '10%',      // 顶部留白
      containLabel: true // 【关键】自动计算坐标轴文字宽度，确保文字不被切掉的同时最大化图表
    },
    // 添加缩放和平移交互
    dataZoom: [
      {
        type: 'slider',
        show: true,
        xAxisIndex: [0],
        start: 0,
        end: 100,
        height: 20,
        bottom: '5%', // CHANGED: 使用百分比，确保与 grid 底部对齐
        handleStyle: {
          color: '#475569'
        },
        textStyle: {
          color: '#94a3b8'
        },
        borderColor: '#475569'
      },
      {
        type: 'inside',
        xAxisIndex: [0],
        start: 0,
        end: 100
      }
    ],
    xAxis: {
      type: useLogAxis ? 'log' : 'value', // CHANGED: 根据数据格式选择对数轴或线性轴
      ...(useLogAxis ? { logBase: 10 } : {}), // 对数轴需要设置底数
      name: isCommentMode 
        ? (useLogAxis ? '评论数 (对数)' : '评论数') 
        : (useLogAxis ? '讨论热度 (对数)' : '讨论热度'),
      nameLocation: 'middle',
      nameGap: 30,
      min: useLogAxis ? 'dataMin' : xAxisMin, // CHANGED: 对数轴使用 dataMin 自动贴合数据最小值
      max: useLogAxis ? 'dataMax' : xAxisMax,
      axisLine: {
        lineStyle: {
          color: '#475569'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        formatter: (value: number) => {
          if (useLogAxis) {
            // 对数轴格式化：显示原始值
            if (value >= 1000000) {
              return (value / 1000000).toFixed(1) + 'M';
            } else if (value >= 1000) {
              return (value / 1000).toFixed(1) + 'K';
            } else if (value < 1) {
              return value.toFixed(3);
            }
            return value.toString();
          } else {
            // 线性轴格式化：归一化值显示为小数
            if (value <= 1 && value >= 0) {
              return value.toFixed(2);
            }
            if (value >= 1000) {
              return (value / 1000).toFixed(1) + 'K';
            }
            return value.toString();
          }
        }
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: '#1e293b',
          type: 'dashed'
        }
      }
    },
    yAxis: {
      type: 'value',
      name: '情感倾向',
      nameLocation: 'middle',
      nameGap: 50,
      min: yAxisMin,  // 根据数据自动缩放
      max: yAxisMax,
      axisLine: {
        lineStyle: {
          color: '#475569'
        },
        onZero: true // 轴线在 0 刻度上
      },
      axisLabel: {
        color: '#94a3b8',
        formatter: '{value}'
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: '#1e293b',
          type: 'dashed'
        }
      }
    },
    series: series,
    // 【关键】加上十字辅助线
    markLine: {
      silent: true,
      symbol: 'none',
      label: { show: false },
      lineStyle: { 
        type: 'solid', 
        color: '#64748b',
        width: 1.5
      },
      data: [
        { yAxis: 0 }, // 情感分界线
      ]
    }
  };

  chartInstance.setOption(option);
}

function updateTimelineChart() {
  if (!chartInstance) return;
  
  const timeline = timelineData.value;
  
  if (timeline.length === 0) {
    chartInstance.clear();
    return;
  }
  
  // 提取时间、看多、看空、中立数据
  const times = timeline.map(item => item.time);
  
  // CHANGED: 保存原始数据用于 tooltip 显示
  const originalData = {
    bullish: timeline.map(item => item.bullishCount),
    bearish: timeline.map(item => item.bearishCount),
    neutral: timeline.map(item => item.neutralCount)
  };
  
  // CHANGED: 计算所有数据的最大值，用于设置Y轴范围
  const allValues = [
    ...originalData.bullish,
    ...originalData.bearish,
    ...originalData.neutral
  ].filter(v => v > 0); // 只考虑大于0的值
  
  const maxValue = allValues.length > 0 ? Math.max(...allValues) : 1;
  
  // CHANGED: 对数轴不能处理0值，将0转换为一个很小的值（0.1），但要在tooltip中显示原始值0
  // 使用 Math.max(0.1, value) 确保所有值都大于0，但保留原始值用于显示
  const bullishData = timeline.map(item => {
    const val = item.bullishCount;
    return val === 0 ? 0.1 : Math.max(0.1, val);
  });
  const bearishData = timeline.map(item => {
    const val = item.bearishCount;
    return val === 0 ? 0.1 : Math.max(0.1, val);
  });
  const neutralData = timeline.map(item => {
    const val = item.neutralCount;
    return val === 0 ? 0.1 : Math.max(0.1, val);
  });
  
  // CHANGED: 根据最大值计算对数轴的上限（向上取整到最近的10的幂次）
  // 如果最大值小于1，设置为10；否则向上取整到最近的10的幂次
  let yAxisMax: number;
  if (maxValue < 1) {
    yAxisMax = 10; // 如果所有值都很小，设置上限为10
  } else {
    const logMax = Math.ceil(Math.log10(maxValue)); // 向上取整
    yAxisMax = Math.pow(10, logMax);
    // 如果最大值接近上限，再增加一个数量级，避免数据点贴顶
    if (maxValue > yAxisMax * 0.8) {
      yAxisMax = Math.pow(10, logMax + 1);
    }
  }
  
  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross'
      },
      formatter: (params: any) => {
        if (!Array.isArray(params)) return '';
        let result = `${params[0].axisValue}<br/>`;
        params.forEach((param: any, index: number) => {
          // CHANGED: 显示原始值而不是对数轴处理后的值
          const dataIndex = param.dataIndex;
          let originalValue = param.value;
          if (param.seriesName === '看多' && originalData.bullish[dataIndex] !== undefined) {
            originalValue = originalData.bullish[dataIndex];
          } else if (param.seriesName === '看空' && originalData.bearish[dataIndex] !== undefined) {
            originalValue = originalData.bearish[dataIndex];
          } else if (param.seriesName === '中立' && originalData.neutral[dataIndex] !== undefined) {
            originalValue = originalData.neutral[dataIndex];
          }
          // CHANGED: 如果原始值为0，显示0而不是0.1
          result += `${param.seriesName}: ${originalValue}<br/>`;
        });
        return result;
      }
    },
    legend: {
      data: ['看多', '看空', '中立'],
      textStyle: {
        color: '#94a3b8'
      },
      top: 10
    },
    grid: {
      left: '3%',
      right: '7%',
      bottom: '10%',
      top: '15%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: times,
      axisLine: {
        lineStyle: {
          color: '#475569'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        rotate: 45, // 旋转标签避免重叠
        formatter: (value: string) => {
          // CHANGED: 根据时间跨度智能显示
          // 如果时间跨度超过1天，显示完整日期时间；否则只显示时间
          if (!value) return value;
          
          // 解析时间字符串（格式：'YYYY-MM-DD HH:MM'）
          const parts = value.split(' ');
          if (parts.length < 2) return value;
          
          const datePart = parts[0]; // 'YYYY-MM-DD'
          const timePart = parts[1]; // 'HH:MM'
          
          // 检查时间跨度：如果第一个和最后一个时间不在同一天，显示完整日期
          if (times.length > 0) {
            const firstTime = times[0];
            const lastTime = times[times.length - 1];
            
            if (firstTime && lastTime) {
              const firstDate = firstTime.split(' ')[0];
              const lastDate = lastTime.split(' ')[0];
              
              // 如果跨天，显示完整日期时间（格式：'MM-DD HH:MM'）
              if (firstDate !== lastDate) {
                const dateObj = new Date(datePart);
                const month = String(dateObj.getMonth() + 1).padStart(2, '0');
                const day = String(dateObj.getDate()).padStart(2, '0');
                return `${month}-${day} ${timePart}`;
              } else {
                // 同一天，只显示时间
                return timePart;
              }
            }
          }
          
          // 默认只显示时间
          return timePart;
        }
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: '#1e293b',
          type: 'dashed'
        }
      }
    },
    yAxis: {
      type: 'log', // CHANGED: 使用对数轴，避免小值被压缩到底部
      name: '帖子数量（对数）',
      nameLocation: 'middle',
      nameGap: 50,
      logBase: 10, // 对数底数
      axisLine: {
        lineStyle: {
          color: '#475569'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        formatter: (value: number) => {
          // 对数轴格式化：显示原始值
          return value.toString();
        }
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: '#1e293b',
          type: 'dashed'
        }
      },
      // CHANGED: 对数轴的最小值不能为0，设置为0.1（因为我们将0值转换为0.1）
      min: 0.1,
      // CHANGED: 根据数据最大值动态设置Y轴上限
      max: yAxisMax
    },
    series: [
      {
        name: '看多',
        type: 'line',
        data: bullishData,
        smooth: true,
        lineStyle: {
          color: '#ef4444', // 红色
          width: 2
        },
        itemStyle: {
          color: '#ef4444'
        },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(239, 68, 68, 0.3)' },
              { offset: 1, color: 'rgba(239, 68, 68, 0.05)' }
            ]
          }
        }
      },
      {
        name: '看空',
        type: 'line',
        data: bearishData,
        smooth: true,
        lineStyle: {
          color: '#10b981', // 绿色
          width: 2
        },
        itemStyle: {
          color: '#10b981'
        },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(16, 185, 129, 0.3)' },
              { offset: 1, color: 'rgba(16, 185, 129, 0.05)' }
            ]
          }
        }
      },
      {
        name: '中立',
        type: 'line',
        data: neutralData,
        smooth: true,
        lineStyle: {
          color: '#94a3b8', // 灰色
          width: 2
        },
        itemStyle: {
          color: '#94a3b8'
        },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(148, 163, 184, 0.3)' },
              { offset: 1, color: 'rgba(148, 163, 184, 0.05)' }
            ]
          }
        }
      }
    ]
  };
  
  chartInstance.setOption(option);
}

function handleResize() {
  chartInstance?.resize();
}

onMounted(() => {
  loadData();
  nextTick(() => {
    initChart();
    window.addEventListener('resize', handleResize);
  });
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  chartInstance?.dispose();
  chartInstance = null;
});

watch(
  () => data.value,
  () => {
    if (!props.selectedSymbol) {
      nextTick(() => updateChart());
    }
  },
  { deep: true }
);

watch(
  () => timelineData.value,
  () => {
    if (props.selectedSymbol && !showOverall.value) {
      nextTick(() => updateChart());
    }
  },
  { deep: true }
);

watch(
  () => showOverall.value,
  () => {
    // 当切换视图时，重新加载数据
    loadData();
  }
);

// 当选择的股票符号改变时，重新加载数据
watch(
  () => props.selectedSymbol,
  (newSymbol, oldSymbol) => {
    // 如果选择了新的股票（从无到有，或从一只股票切换到另一只），重置视图状态为个股折线图
    if (newSymbol && newSymbol !== oldSymbol) {
      showOverall.value = false;
    }
    // 无论是否有selectedSymbol都重新加载（无symbol时显示全部股票）
    loadData();
  }
);
</script>