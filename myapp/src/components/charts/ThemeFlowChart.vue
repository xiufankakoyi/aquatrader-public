<template>
  <div class="theme-flow-chart h-full flex flex-col">
    <!-- 图表容器 - 自适应父容器高度 -->
    <div ref="chartRef" class="flex-1 w-full min-h-0"></div>
    
    <!-- 加载状态 -->
    <div v-if="loading" class="absolute inset-0 flex items-center justify-center bg-[#0a0a0a]/80 rounded-xl">
      <div class="flex items-center gap-2 text-slate-400">
        <i class="fas fa-spinner fa-spin"></i>
        <span>加载中...</span>
      </div>
    </div>
    
    <!-- 空状态 -->
    <div v-if="!loading && isEmpty" class="absolute inset-0 flex items-center justify-center">
      <div class="text-center text-slate-500">
        <i class="fas fa-route text-4xl mb-2"></i>
        <p class="text-sm">暂无数据</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue';
import * as echarts from 'echarts';

/**
 * ThemeFlowChart - 题材流向路径图组件
 * 
 * 可视化展示不同交易日之间题材板块的资金流向和强度变化：
 * - 使用桑基图(Sankey diagram)展示流量关系
 * - 节点大小反映板块强度
 * - 清晰展示龙头股引领、跟风股跟进、扩散补涨的完整发酵路径
 * - 无边框设计风格，使用阴影和背景色区分层次
 */

interface FlowNode {
  name: string;
  date: string;
  count: number;
  is_main: boolean;
}

interface FlowLink {
  source: number;
  target: number;
  value: number;
}

interface FlowData {
  nodes: FlowNode[];
  links: FlowLink[];
}

interface Props {
  data: FlowData | null;
  loading?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  data: null,
  loading: false
});

const chartRef = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const isEmpty = ref(true);

/**
 * 题材颜色映射 - Divergence Meter 风格
 */
const themeColorMap: Record<string, string> = {
  'AI应用': '#00D2FF',
  '算力': '#FF4757',
  '机器人': '#FFA502',
  '半导体': '#FF6B35',
  '新能源': '#00FF88',
  '光伏': '#A55EEA',
  '化工': '#26DE81',
  '公告': '#778CA3',
  '其他': '#787B86',
  '商业航天': '#FD79A8',
  '光通信': '#74B9FF',
  '玻璃纤维布': '#00CEC9',
  '矿产资源': '#6C5CE7',
  '固态电池': '#00D2FF',
  '医药': '#26DE81',
  '智能电网': '#A55EEA',
  '染料': '#FD79A8',
  '影视短剧': '#FF6B35',
  '版权IP': '#FFA502',
  'AI安全': '#00D2FF',
  '液冷': '#74B9FF',
  '燃气轮机': '#FF4757',
  '军工': '#778CA3',
  '无人驾驶': '#00CEC9',
  '石油化工': '#6C5CE7'
};

/**
 * 阶段颜色配置 - 龙头引领、跟风跟进、扩散补涨
 */
const stageColors = {
  leader: '#00d084',    // 龙头引领 - 翠绿
  follower: '#ffa502',  // 跟风跟进 - 琥珀
  spread: '#ff6b81'     // 扩散补涨 - 粉红
};

/**
 * 生成颜色 - 优先使用预定义颜色，并根据阶段调整色调
 */
const generateColor = (name: string, isMain: boolean, depth: number = 0, totalDepth: number = 3): string => {
  // 优先使用预定义颜色
  let baseColor = themeColorMap[name];
  
  if (!baseColor) {
    if (isMain) {
      // 主流题材使用鲜明的颜色
      const colors = [
        '#00D2FF', '#00FF88', '#FF6B35', '#FFA502', '#FF4757', '#A55EEA'
      ];
      const hash = name.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0);
      baseColor = colors[hash % colors.length];
    } else {
      // 普通题材使用灰色
      baseColor = '#787B86';
    }
  }
  
  // 根据阶段调整颜色 - 龙头引领(早期)、跟风跟进(中期)、扩散补涨(后期)
  if (totalDepth <= 1) return baseColor;
  
  const stageRatio = depth / (totalDepth - 1);
  if (stageRatio < 0.33) {
    // 龙头引领阶段 - 偏向翠绿
    return blendColors(baseColor, stageColors.leader, 0.3);
  } else if (stageRatio < 0.66) {
    // 跟风跟进阶段 - 偏向琥珀
    return blendColors(baseColor, stageColors.follower, 0.3);
  } else {
    // 扩散补涨阶段 - 偏向粉红
    return blendColors(baseColor, stageColors.spread, 0.3);
  }
};

/**
 * 颜色混合函数
 */
const blendColors = (color1: string, color2: string, ratio: number): string => {
  const hex2rgb = (hex: string) => {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return [r, g, b];
  };
  
  const rgb2hex = (r: number, g: number, b: number) => {
    return '#' + [r, g, b].map(x => {
      const hex = Math.round(x).toString(16);
      return hex.length === 1 ? '0' + hex : hex;
    }).join('');
  };
  
  const rgb1 = hex2rgb(color1);
  const rgb2 = hex2rgb(color2);
  
  const r = rgb1[0] * (1 - ratio) + rgb2[0] * ratio;
  const g = rgb1[1] * (1 - ratio) + rgb2[1] * ratio;
  const b = rgb1[2] * (1 - ratio) + rgb2[2] * ratio;
  
  return rgb2hex(r, g, b);
};

/**
 * 计算日期范围文本
 */
const dateRangeText = computed(() => {
  if (!props.data || !props.data.nodes || props.data.nodes.length === 0) {
    return '';
  }
  
  const dates = [...new Set(props.data.nodes.map(n => n.date))].sort();
  if (dates.length === 0) return '';
  
  const start = dates[0].slice(5); // MM-DD
  const end = dates[dates.length - 1].slice(5);
  return `${start} → ${end}`;
});

/**
 * 初始化图表
 */
const initChart = () => {
  if (!chartRef.value) return;
  
  chartInstance = echarts.init(chartRef.value);
  
  // 监听窗口大小变化
  const resizeHandler = () => chartInstance?.resize();
  window.addEventListener('resize', resizeHandler);
};

/**
 * 更新图表数据
 */
const updateChart = () => {
  if (!chartInstance || !props.data) {
    isEmpty.value = true;
    return;
  }
  
  const { nodes, links } = props.data;
  
  if (!nodes || nodes.length === 0) {
    isEmpty.value = true;
    chartInstance.clear();
    return;
  }
  
  isEmpty.value = false;
  
  // 获取日期深度信息
  const uniqueDates = [...new Set(nodes.map(n => n.date))].sort();
  const totalDepth = uniqueDates.length;
  
  // 构建节点数据，添加日期前缀以区分不同日期的同名题材
  const processedNodes = nodes.map((node, index) => {
    const depth = getNodeDepth(node.date, nodes);
    return {
      name: `${node.date.slice(5)}_${node.name}`,
      // 原始数据用于 tooltip
      rawName: node.name,
      rawDate: node.date,
      count: node.count,
      isMain: node.is_main,
      depth: depth,
      itemStyle: {
        color: generateColor(node.name, node.is_main, depth, totalDepth),
        borderWidth: 0,
        borderColor: 'transparent'
      }
    };
  });
  
  // 构建连接数据
  const processedLinks = links.map(link => ({
    source: processedNodes[link.source]?.name || '',
    target: processedNodes[link.target]?.name || '',
    value: link.value,
    lineStyle: {
      opacity: 0.3,
      curveness: 0.6
    }
  }));
  
  const option: echarts.EChartsOption = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      triggerOn: 'mousemove',
      backgroundColor: 'rgba(10, 10, 10, 0.95)',
      borderColor: '#2a2a2a',
      borderWidth: 1,
      textStyle: {
        color: '#e0e0e0',
        fontSize: 11,
        fontFamily: 'JetBrains Mono, Roboto Mono, monospace'
      },
      formatter: (params: any) => {
        if (params.dataType === 'node') {
          const node = processedNodes.find(n => n.name === params.name);
          if (node) {
            const color = generateColor(node.rawName, node.isMain, node.depth, totalDepth);
            // 判断阶段
            const stageRatio = totalDepth > 1 ? node.depth / (totalDepth - 1) : 0;
            let stageText = '';
            let stageColor = '';
            if (stageRatio < 0.33) {
              stageText = '龙头引领';
              stageColor = stageColors.leader;
            } else if (stageRatio < 0.66) {
              stageText = '跟风跟进';
              stageColor = stageColors.follower;
            } else {
              stageText = '扩散补涨';
              stageColor = stageColors.spread;
            }
            return `
              <div style="font-weight:600;color:${color}">${node.rawName}</div>
              <div class="text-xs mt-1 space-y-0.5" style="color:#888">
                <div>日期: ${node.rawDate}</div>
                <div>涨停数: <span style="color:${color}">${node.count}只</span></div>
                <div>类型: ${node.isMain ? '<span style="color:#00FF88">主流题材</span>' : '普通题材'}</div>
                <div>阶段: <span style="color:${stageColor}">${stageText}</span></div>
              </div>
            `;
          }
        } else if (params.dataType === 'edge') {
          return `
            <div style="font-size:11px">
              <div style="color:#888">资金流向</div>
              <div style="color:#666">强度: ${params.value}</div>
            </div>
          `;
        }
        return '';
      }
    },
    series: [
      {
        type: 'sankey',
        layout: 'none',
        top: '5%',
        bottom: '5%',
        left: '2%',
        right: '15%',
        emphasis: {
          focus: 'none',
          itemStyle: { opacity: 1 },
          lineStyle: { opacity: 0.9 }
        },
        select: {
          itemStyle: { opacity: 1 },
          lineStyle: { opacity: 0.9 }
        },
        nodeAlign: 'left',
        nodeGap: 12,
        nodeWidth: 12,
        layoutIterations: 32,
        lineStyle: {
          color: 'source',
          curveness: 0.6,
          opacity: 0.3
        },
        itemStyle: {
          borderWidth: 0,
          borderColor: 'transparent'
        },
        label: {
          position: 'right',
          distance: 8,
          color: '#888',
          fontSize: 10,
          fontFamily: 'JetBrains Mono, Roboto Mono, monospace',
          formatter: (params: any) => {
            const node = processedNodes.find(n => n.name === params.name);
            return node ? node.rawName : params.name;
          }
        },
        data: processedNodes,
        links: processedLinks
      }
    ]
  };
  
  chartInstance.setOption(option, true);
  
  // 添加鼠标事件监听，实现整条路径高亮
  chartInstance.off('mouseover');
  chartInstance.off('mouseout');
  
  chartInstance.on('mouseover', (params: any) => {
    if (params.dataType === 'edge') {
      // 获取当前悬停的边
      const hoveredLink = params.data;
      const sourceNode = hoveredLink.source;
      const targetNode = hoveredLink.target;
      
      // 获取原始 option
      const option = chartInstance!.getOption();
      const series = option.series?.[0] as any;
      const links = series.links || [];
      const nodes = series.data || [];
      
      // 找到整条路径上的所有节点和边
      const pathNodes = new Set<string>();
      const pathLinks = new Set<number>();
      
      // 添加当前边的源节点和目标节点
      pathNodes.add(sourceNode);
      pathNodes.add(targetNode);
      
      // 向前追溯源节点
      let currentSource = sourceNode;
      let found = true;
      while (found) {
        found = false;
        for (let i = 0; i < links.length; i++) {
          if (links[i].target === currentSource && !pathLinks.has(i)) {
            pathLinks.add(i);
            pathNodes.add(links[i].source);
            currentSource = links[i].source;
            found = true;
            break;
          }
        }
      }
      
      // 向后追溯目标节点
      let currentTarget = targetNode;
      found = true;
      while (found) {
        found = false;
        for (let i = 0; i < links.length; i++) {
          if (links[i].source === currentTarget && !pathLinks.has(i)) {
            pathLinks.add(i);
            pathNodes.add(links[i].target);
            currentTarget = links[i].target;
            found = true;
            break;
          }
        }
      }
      
      // 添加当前边
      const currentLinkIndex = links.findIndex((l: any) => l.source === sourceNode && l.target === targetNode);
      if (currentLinkIndex >= 0) {
        pathLinks.add(currentLinkIndex);
      }
      
      // 应用高亮效果
      nodes.forEach((node: any) => {
        if (pathNodes.has(node.name)) {
          node.itemStyle.opacity = 1;
          node.label = { 
            color: '#fff', 
            fontSize: 11, 
            fontWeight: 'bold',
            fontFamily: 'JetBrains Mono, Roboto Mono, monospace'
          };
        } else {
          node.itemStyle.opacity = 0.15;
          node.label = { 
            color: '#444', 
            fontSize: 10,
            fontFamily: 'JetBrains Mono, Roboto Mono, monospace'
          };
        }
      });
      
      links.forEach((link: any, index: number) => {
        if (pathLinks.has(index)) {
          link.lineStyle = { opacity: 0.9 };
        } else {
          link.lineStyle = { opacity: 0.05 };
        }
      });
      
      chartInstance!.setOption({ series: [{ data: nodes, links }] });
    }
  });
  
  chartInstance.on('mouseout', () => {
    // 恢复所有节点和边的默认状态
    const option = chartInstance!.getOption();
    const series = option.series?.[0] as any;
    const links = series.links || [];
    const nodes = series.data || [];
    
    nodes.forEach((node: any) => {
      node.itemStyle.opacity = 1;
      node.label = { 
        color: '#888', 
        fontSize: 10, 
        fontFamily: 'JetBrains Mono, Roboto Mono, monospace'
      };
    });
    
    links.forEach((link: any) => {
      link.lineStyle = { opacity: 0.3 };
    });
    
    chartInstance!.setOption({ series: [{ data: nodes, links }] });
  });
};

/**
 * 获取节点深度（用于时间轴排列）
 */
const getNodeDepth = (date: string, allNodes: FlowNode[]): number => {
  const uniqueDates = [...new Set(allNodes.map(n => n.date))].sort();
  return uniqueDates.indexOf(date);
};

/**
 * 高亮指定题材
 */
const highlightTheme = (themeName: string | null) => {
  if (!chartInstance) return;
  
  const option = chartInstance.getOption();
  const series = option.series?.[0] as any;
  if (!series) return;
  
  const data = series.data || [];
  const links = series.links || [];
  
  if (!themeName) {
    // 恢复所有
    data.forEach((node: any) => {
      node.itemStyle.opacity = 1;
      node.label = { 
        color: '#888', 
        fontSize: 10, 
        fontFamily: 'JetBrains Mono, Roboto Mono, monospace' 
      };
    });
    links.forEach((link: any) => {
      link.lineStyle = { opacity: 0.3 };
    });
  } else {
    // 高亮相关节点
    const relatedNodes = new Set<string>();
    
    data.forEach((node: any) => {
      if (node.rawName === themeName || node.rawName.includes(themeName)) {
        relatedNodes.add(node.name);
        node.itemStyle.opacity = 1;
        node.label = { 
          color: '#fff', 
          fontSize: 11, 
          fontWeight: 'bold',
          fontFamily: 'JetBrains Mono, Roboto Mono, monospace'
        };
      } else {
        node.itemStyle.opacity = 0.15;
        node.label = { 
          color: '#444', 
          fontSize: 10,
          fontFamily: 'JetBrains Mono, Roboto Mono, monospace'
        };
      }
    });
    
    // 高亮相关连线
    links.forEach((link: any) => {
      if (relatedNodes.has(link.source) || relatedNodes.has(link.target)) {
        link.lineStyle = { opacity: 0.8 };
      } else {
        link.lineStyle = { opacity: 0.05 };
      }
    });
  }
  
  chartInstance.setOption({ series: [{ data, links }] });
};

// 监听数据变化
watch(
  () => props.data,
  () => {
    nextTick(() => updateChart());
  },
  { deep: true }
);

// 监听加载状态
watch(
  () => props.loading,
  (loading) => {
    if (!loading) {
      nextTick(() => updateChart());
    }
  }
);

onMounted(() => {
  initChart();
  if (props.data) {
    updateChart();
  }
});

onUnmounted(() => {
  chartInstance?.dispose();
  window.removeEventListener('resize', () => chartInstance?.resize());
});

// 暴露方法给父组件
defineExpose({
  highlightTheme
});
</script>

<style scoped>
.theme-flow-chart {
  position: relative;
  background: transparent;
}
</style>
