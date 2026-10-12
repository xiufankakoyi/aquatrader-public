<template>
  <div class="chain-canvas">
    <div ref="chartRef" class="chart-container"></div>
    <div v-if="loading" class="loading-overlay">
      <div class="loading-spinner">
        <i class="fas fa-spinner fa-spin"></i>
        <span>加载图谱...</span>
      </div>
    </div>
    <div v-if="!loading && isEmpty" class="empty-state">
      <i class="fas fa-project-diagram"></i>
      <p>暂无产业链结构数据</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue';
import * as echarts from 'echarts';
import type { ChainEdge, ChainGraphData, ChainNode, ChainLayer } from '@/api/industryChain';

interface Props {
  graphData: ChainGraphData | null;
  loading: boolean;
  selectedNodeId: string;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  (e: 'selectNode', node: ChainNode): void;
}>();

const chartRef = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const isEmpty = computed(() => !props.graphData || props.graphData.nodes.length === 0);

// 兼容旧 layer + 新 type 的颜色映射
const layerColors: Record<string, string> = {
  upstream: '#3dd6a3',
  component: '#4cc9f0',
  product: '#f5b84b',
  application: '#a78bfa',
  // T3.4 新增 type 颜色
  theme: '#f97316',
  industry: '#4cc9f0',
  stock: '#22d3ee',
  event: '#a78bfa',
};

// 节点逐个出现的延迟（毫秒）
const NODE_ANIMATION_STEP_MS = 100;

onMounted(() => {
  if (!chartRef.value) return;
  chartInstance = echarts.init(chartRef.value, 'dark');
  chartInstance.on('click', (params: any) => {
    if (params.dataType === 'node' && params.data && !params.data.silent) {
      emit('selectNode', params.data.rawNode as ChainNode);
    }
  });
  window.addEventListener('resize', handleResize);
  renderChart();
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  chartInstance?.dispose();
  chartInstance = null;
});

watch(
  () => [props.graphData, props.selectedNodeId],
  () => {
    nextTick(() => renderChart());
  },
  { deep: true }
);

function handleResize() {
  chartInstance?.resize();
}

function renderChart() {
  if (!chartInstance || !props.graphData) return;

  const { nodes, edges, layers } = props.graphData;
  if (!nodes.length) {
    chartInstance.clear();
    return;
  }

  const sortedLayers = [...(layers || [])].sort((a, b) => a.order - b.order);
  const layerOrderMap = Object.fromEntries(sortedLayers.map((layer, index) => [layer.id, index]));
  const layoutNodes = buildLayoutNodes(nodes, sortedLayers);
  const nodeById = new Map(layoutNodes.map((node) => [node.id, node]));

  const chartNodes = layoutNodes.map((node, index) => {
    const ntype = String(node.type || 'industry');
    const color = layerColors[ntype] || layerColors[node.layer || ''] || '#94a3b8';
    const isSelected = node.id === props.selectedNodeId;
    const isDemo = Boolean(node.demo_highlight || node.hot_score_source === '示例热度');
    return {
      id: node.id,
      name: node.name,
      rawNode: node,
      // 让 echarts 按延迟逐个出现
      animationDelay: index * NODE_ANIMATION_STEP_MS,
      animationDuration: 320,
      x: node.x,
      y: node.y,
      fixed: true,
      symbol: 'roundRect',
      symbolSize: [148, 52],
      value: node.score || node.hot_score || node.importance || 1,
      itemStyle: {
        color: isDemo ? brighten(color) : 'rgba(15, 23, 42, 0.98)',
        borderColor: isSelected ? '#f8fafc' : color,
        borderWidth: isSelected ? 3 : 1.5,
        shadowBlur: isSelected || isDemo ? 18 : 8,
        shadowColor: isDemo ? color : 'rgba(15, 23, 42, 0.5)',
      },
      label: {
        show: true,
        position: 'inside',
        color: '#f8fafc',
        fontSize: 13,
        fontWeight: 650,
        lineHeight: 17,
        formatter: () => formatNodeLabel(node),
      },
    };
  });

  const chartEdges = normalizeEdges(edges, nodeById, layerOrderMap);

  const option: echarts.EChartsOption = {
    backgroundColor: 'transparent',
    animationDurationUpdate: 240,
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(15, 23, 42, 0.96)',
      borderColor: '#334155',
      borderWidth: 1,
      textStyle: {
        color: '#e2e8f0',
        fontSize: 12,
      },
      formatter: (params: any) => {
        if (params.dataType === 'node') {
          const d = params.data.rawNode as ChainNode;
          const type = String(d.type || 'industry');
          const score = d.score != null ? Number(d.score).toFixed(2) : '暂无';
          const stockCount = d.evidence_count ?? d.candidate_count ?? d.stock_count ?? 0;
          const source = d.source || 'lancedb_industry_chain';
          const dataDate = (d.data_date || '').slice(0, 10) || '暂无';
          return `
            <div style="padding:8px;max-width:280px;">
              <div style="font-weight:700;font-size:14px;margin-bottom:5px;color:#f8fafc;">${escapeHtml(d.name)}</div>
              <div style="color:#94a3b8;font-size:12px;margin-bottom:4px;">类型：${escapeHtml(type)} / 来源：${escapeHtml(source)}</div>
              <div style="color:#cbd5e1;font-size:12px;line-height:1.45;">${escapeHtml(d.description || '暂无描述')}</div>
              <div style="margin-top:6px;color:#94a3b8;font-size:11px;">score: ${score}</div>
              <div style="color:#94a3b8;font-size:11px;">证据数: ${stockCount}</div>
              <div style="color:#94a3b8;font-size:11px;">data_date: ${escapeHtml(dataDate)}</div>
            </div>
          `;
        }
        if (params.dataType === 'edge') {
          const d = params.data;
          const weight = typeof d.weight === 'number' ? d.weight.toFixed(2) : '—';
          const evidence = Array.isArray(d.evidence) ? d.evidence : [];
          return `
            <div style="padding:8px;max-width:280px;">
              <div style="font-weight:700;color:#f8fafc;margin-bottom:4px;">${escapeHtml(d.relationTypeLabel || d.relation || '关联')}</div>
              <div style="color:#94a3b8;font-size:12px;margin-bottom:4px;">权重: ${weight}${d.highlighted ? ' (高亮)' : ''}</div>
              ${evidence
                .slice(0, 3)
                .map(
                  (item: string) =>
                    `<div style="color:#cbd5e1;font-size:11px;line-height:1.4;">• ${escapeHtml(item)}</div>`
                )
                .join('')}
            </div>
          `;
        }
        return '';
      },
    },
    graphic: buildLayerTitles(sortedLayers),
    series: [
      {
        type: 'graph',
        layout: 'none',
        roam: true,
        left: 28,
        right: 28,
        top: 54,
        bottom: 28,
        data: chartNodes,
        links: chartEdges,
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: [0, 10],
        lineStyle: {
          color: '#64748b',
          width: 1.6,
          opacity: 0.78,
          curveness: 0.08,
        },
        edgeLabel: {
          show: false,
        },
        emphasis: {
          focus: 'adjacency',
          lineStyle: {
            width: 3,
            opacity: 1,
          },
        },
      } as any,
    ],
  };

  chartInstance.setOption(option, true);
  chartInstance.resize();
}

/**
 * 节点 label 文本生成：兼容示例热度标记与新数据源标识。
 */
function formatNodeLabel(node: ChainNode): string {
  if (node.hot_score_source === '示例热度') {
    return `${node.name}\n示例热度`;
  }
  const sourceTag = node.source ? `\n[${node.source}]` : '';
  return `${node.name}${sourceTag}`;
}

/**
 * 计算每个节点 (x, y)。
 * 兼容: 旧 layer 体系 + 新 type 体系。
 */
function buildLayoutNodes(nodes: ChainNode[], layers: ChainLayer[]): ChainNode[] {
  const horizontalGap = 280;
  const verticalGap = 78;
  const layerStartX = 90;
  // 用 layer 或 type 作为分组键
  const layerOrderMap = Object.fromEntries(layers.map((layer, index) => [layer.id, index]));

  const grouped = new Map<string, ChainNode[]>();
  for (const node of nodes) {
    const key = String(node.layer || node.type || 'industry');
    const group = grouped.get(key) || [];
    group.push(node);
    grouped.set(key, group);
  }
  for (const group of grouped.values()) {
    group.sort((a, b) => (a.order || 0) - (b.order || 0));
  }

  // 当 layers 为空时，根据 type 推断顺序
  const groupOrder =
    layers.length > 0
      ? layers.map((l) => l.id)
      : ['theme', 'industry', 'event', 'stock', 'upstream', 'component', 'product', 'application'];

  return nodes.map((node) => {
    const key = String(node.layer || node.type || 'industry');
    const group = grouped.get(key) || [];
    const index = Math.max(group.findIndex((item) => item.id === node.id), 0);
    const layerIndexRaw = layerOrderMap[key] ?? groupOrder.indexOf(key);
    const layerIndex = layerIndexRaw >= 0 ? layerIndexRaw : 0;
    const yOffset = Math.max(0, (10 - group.length) * verticalGap * 0.18);
    return {
      ...node,
      x: layerStartX + layerIndex * horizontalGap,
      y: 70 + yOffset + index * verticalGap,
      fixed: true,
    };
  });
}

/**
 * 边归一化：高亮 weight > 0.7 的边。
 */
function normalizeEdges(
  edges: ChainEdge[],
  nodeById: Map<string, ChainNode>,
  layerOrderMap: Record<string, number>
) {
  const HIGHLIGHT_THRESHOLD = 0.7;
  return edges
    .filter((edge) => nodeById.has(edge.source) && nodeById.has(edge.target))
    .map((edge) => {
      const sourceNode = nodeById.get(edge.source)!;
      const targetNode = nodeById.get(edge.target)!;
      const sourceLayerOrder = layerOrderMap[sourceNode.layer || ''] ?? 0;
      const targetLayerOrder = layerOrderMap[targetNode.layer || ''] ?? 0;
      const shouldReverse = sourceLayerOrder > targetLayerOrder;
      const labelText =
        typeof edge.label?.formatter === 'string' ? edge.label.formatter : edge.relation;
      const weight = typeof edge.weight === 'number' ? edge.weight : 0.6;
      const highlighted = weight > HIGHLIGHT_THRESHOLD;
      return {
        source: shouldReverse ? edge.target : edge.source,
        target: shouldReverse ? edge.source : edge.target,
        relation: edge.relation,
        relationType: edge.relation_type || 'belongs_to',
        relationTypeLabel: labelText,
        description: edge.description,
        evidence: Array.isArray(edge.evidence) ? edge.evidence : [],
        weight,
        highlighted,
        lineStyle: {
          color: highlighted ? '#fbbf24' : shouldReverse ? '#475569' : '#64748b',
          width: highlighted ? 2.6 : 1.6,
          opacity: highlighted ? 0.95 : 0.78,
          curveness: sourceLayerOrder === targetLayerOrder ? 0.16 : 0.08,
        },
      };
    });
}

function buildLayerTitles(layers: ChainLayer[]) {
  const lefts = ['8%', '33%', '58%', '82%'];
  return layers.map((layer, index) => ({
    type: 'group',
    left: lefts[index] || `${8 + index * 24}%`,
    top: 12,
    children: [
      {
        type: 'rect',
        shape: { width: 118, height: 28, r: 6 },
        style: {
          fill: 'rgba(15, 23, 42, 0.96)',
          stroke: layerColors[layer.id] || '#94a3b8',
          lineWidth: 1,
        },
      },
      {
        type: 'text',
        left: 12,
        top: 7,
        style: {
          text: layer.name,
          fill: '#e2e8f0',
          font: '12px sans-serif',
          fontWeight: 700,
        },
      },
    ],
  }));
}

function brighten(color: string): string {
  const palette: Record<string, string> = {
    '#3dd6a3': '#0f766e',
    '#4cc9f0': '#0369a1',
    '#f5b84b': '#b45309',
    '#a78bfa': '#6d28d9',
    '#f97316': '#c2410c',
    '#22d3ee': '#0e7490',
  };
  return palette[color] || color;
}

function escapeHtml(value: string): string {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
</script>

<style scoped>
.chain-canvas {
  position: relative;
  flex: 1;
  min-height: 590px;
  border: 1px solid rgba(71, 85, 105, 0.45);
  border-radius: 8px;
  background:
    linear-gradient(rgba(148, 163, 184, 0.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(148, 163, 184, 0.045) 1px, transparent 1px),
    #0b1120;
  background-size: 28px 28px;
  overflow: hidden;
}

.chart-container {
  width: 100%;
  height: 100%;
  min-height: 590px;
}

.loading-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(2, 6, 23, 0.82);
}

.loading-spinner {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #94a3b8;
  font-size: 14px;
}

.empty-state {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: #64748b;
  font-size: 14px;
}
</style>
