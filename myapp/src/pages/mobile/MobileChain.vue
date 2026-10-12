<template>
  <div class="mobile-chain">
    <MobileCard
      title="产业链拓扑"
      accent="purple"
      icon="fa-solid fa-diagram-project"
      :delay="0"
    >
      <div class="chain-toolbar">
        <select v-model="selectedChainId" class="chain-select" @change="loadChain">
          <option v-for="chain in availableChains" :key="chain.chain_id" :value="chain.chain_id">
            {{ chain.name }}
          </option>
        </select>
        <button class="reload-btn" :disabled="loading" @click="loadChain">
          <i class="fa-solid fa-rotate" :class="{ 'fa-spin': loading }" />
        </button>
      </div>
      <div class="chain-meta" v-if="graphMeta">
        <span class="meta-source">来源: {{ graphMeta.source || '-' }}</span>
        <span class="meta-date">日期: {{ graphMeta.data_date || '-' }}</span>
        <span class="meta-count">节点: {{ nodeCount }}</span>
      </div>
      <div class="chain-legend">
        <span class="legend-item"><i class="fa-solid fa-circle legend-dot legend-upstream" />上游</span>
        <span class="legend-item"><i class="fa-solid fa-circle legend-dot legend-midstream" />中游</span>
        <span class="legend-item"><i class="fa-solid fa-circle legend-dot legend-downstream" />下游</span>
        <span class="legend-item"><i class="fa-solid fa-circle legend-dot legend-application" />应用/终端</span>
      </div>
    </MobileCard>

    <MobileCard class="chain-canvas-card" accent="blue" icon="fa-solid fa-sitemap" :delay="80">
      <div v-if="loading" class="chain-loading">
        <a-spin size="small" />
        <span>加载产业链数据...</span>
      </div>
      <div v-else-if="error" class="chain-error">
        <i class="fa-solid fa-circle-exclamation" />
        <span>{{ error }}</span>
      </div>
      <div v-else-if="!nodes.length" class="chain-empty">
        <span>暂无产业链数据</span>
      </div>
      <div v-else ref="canvasRef" class="chain-canvas" @scroll="onCanvasScroll">
        <div class="chain-columns" :style="canvasStyle">
          <div
            v-for="(column, colIdx) in columns"
            :key="column.layer"
            class="chain-column"
            :style="{ animationDelay: `${colIdx * 80}ms` }"
          >
            <div class="chain-level-label">{{ column.name }}</div>
            <div class="chain-nodes">
              <div
                v-for="(node, rowIdx) in column.nodes"
                :key="node.id"
                :id="`node-${node.id}`"
                class="chain-node"
                :class="`chain-node--${node.levelClass}`"
                :style="{ animationDelay: `${colIdx * 80 + rowIdx * 40}ms` }"
                @click="selectNode(node)"
              >
                <div class="chain-node-header">
                  <span class="chain-node-name">{{ node.name }}</span>
                </div>
                <div v-if="node.note" class="chain-node-note">{{ node.note }}</div>
              </div>
            </div>
          </div>
        </div>
        <svg class="chain-links" :width="canvasWidth" :height="canvasHeight">
          <path
            v-for="(link, idx) in visibleLinks"
            :key="idx"
            class="chain-link"
            :d="link.path"
            :style="{ animationDelay: `${link.delay}ms` }"
          />
        </svg>
      </div>
    </MobileCard>

    <MobileCard v-if="selectedNode" title="节点详情" class="chain-detail" accent="gold" icon="fa-solid fa-circle-info" :delay="160">
      <div class="detail-content">
        <div class="detail-name">{{ selectedNode.name }}</div>
        <div class="detail-layer">{{ selectedNode.layerName }}</div>
        <div v-if="selectedNode.description" class="detail-note">{{ selectedNode.description }}</div>
        <div v-if="selectedNode.keywords?.length" class="detail-keywords">
          <span v-for="kw in selectedNode.keywords" :key="kw" class="detail-chip">{{ kw }}</span>
        </div>
        <div class="detail-stats">
          <div class="detail-stat" v-if="selectedNode.score !== undefined">
            <span class="stat-label">热度</span>
            <span class="stat-value">{{ selectedNode.score }}</span>
          </div>
          <div class="detail-stat" v-if="selectedNode.evidence_count !== undefined">
            <span class="stat-label">证据</span>
            <span class="stat-value">{{ selectedNode.evidence_count }}</span>
          </div>
          <div class="detail-stat" v-if="selectedNode.stock_count !== undefined">
            <span class="stat-label">股票</span>
            <span class="stat-value">{{ selectedNode.stock_count }}</span>
          </div>
        </div>
      </div>
    </MobileCard>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileChain - 移动端产业链拓扑图
 * 只从 /api/mobile/chain 加载 inbox_items.json 真实数据，按 concept → chain_node → company 横向展开
 * concept 列表由 /api/mobile/chain/concepts 提供，不使用示例/兜底数据
 */
import { ref, computed, nextTick, onMounted } from 'vue';
import MobileCard from '../../components/mobile/MobileCard.vue';
import { getMobileChain, getMobileChainConcepts } from '../../api/mobile';
import type { ChainNode as ApiChainNode, ChainEdge, ChainLayer, GraphMeta } from '../../api/industryChain';

interface DisplayNode extends ApiChainNode {
  levelClass: string;
  row: number;
  col: number;
  layerName: string;
  note?: string;
}

interface Column {
  layer: string;
  name: string;
  order: number;
  nodes: DisplayNode[];
}

interface Link {
  path: string;
  delay: number;
}

interface ChainOption {
  chain_id: string;
  name: string;
}

const COLUMN_WIDTH = 132;

const LEVEL_CLASS_MAP: Record<string, string> = {
  upstream: 'upstream',
  material: 'upstream',
  raw_material: 'upstream',
  component: 'midstream',
  device: 'midstream',
  product: 'downstream',
  equipment: 'downstream',
  application: 'application',
  terminal: 'application',
  theme: 'application',
  chain_node: 'raw_material',
  company: 'stock',
  stock: 'stock'
};

const LAYER_NAME_MAP: Record<string, string> = {
  upstream: '上游材料',
  material: '上游材料',
  raw_material: '原材料',
  component: '中游器件',
  device: '中游器件',
  product: '下游产品',
  equipment: '设备',
  application: '应用场景',
  terminal: '终端',
  theme: '主题',
  chain_node: '产业链环节',
  company: '公司/标的',
  stock: '股票'
};

const loading = ref(false);
const error = ref('');
const availableChains = ref<ChainOption[]>([]);
const selectedChainId = ref('');
const rawNodes = ref<ApiChainNode[]>([]);
const rawLinks = ref<ChainEdge[]>([]);
const rawLayers = ref<ChainLayer[]>([]);
const graphMeta = ref<GraphMeta | null>(null);
const canvasRef = ref<HTMLElement | null>(null);
const canvasWidth = ref(0);
const canvasHeight = ref(0);
const selectedNode = ref<DisplayNode | null>(null);

const nodes = computed<DisplayNode[]>(() => {
  const result: DisplayNode[] = [];
  columns.value.forEach((col, colIdx) => {
    col.nodes.forEach((node, rowIdx) => {
      node.row = rowIdx;
      node.col = colIdx;
      result.push(node);
    });
  });
  return result;
});

const columns = computed<Column[]>(() => {
  if (!rawNodes.value.length) return [];

  // 优先使用后端返回的 layers 顺序
  const layerOrder: Record<string, number> = {};
  const layerName: Record<string, string> = {};
  rawLayers.value.forEach((layer) => {
    layerOrder[layer.id] = layer.order;
    layerName[layer.id] = layer.name;
  });

  rawNodes.value.forEach((node) => {
    if (layerOrder[node.layer || ''] === undefined) {
      layerOrder[node.layer || ''] = node.order ?? 0;
    }
    if (!layerName[node.layer || '']) {
      layerName[node.layer || ''] = LAYER_NAME_MAP[node.layer || ''] || node.layer || '其他';
    }
  });

  const groups: Record<string, Column> = {};
  rawNodes.value.forEach((node) => {
    const layer = node.layer || 'unknown';
    if (!groups[layer]) {
      groups[layer] = {
        layer,
        name: layerName[layer] || layer,
        order: layerOrder[layer] ?? 0,
        nodes: []
      };
    }
    const levelClass = LEVEL_CLASS_MAP[layer] || 'midstream';
    groups[layer].nodes.push({
      ...node,
      levelClass,
      row: 0,
      col: 0,
      layerName: layerName[layer] || layer,
      note: node.description?.slice(0, 40)
    });
  });

  return Object.values(groups)
    .map((col) => ({
      ...col,
      nodes: col.nodes.sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    }))
    .sort((a, b) => a.order - b.order);
});

const visibleLinks = computed<Link[]>(() => {
  const links: Link[] = [];
  const nodeMap = new Map(nodes.value.map((n) => [n.id, n]));
  const colIndex = new Map(columns.value.map((c, i) => [c.layer, i]));
  const nodeHeight = 60;
  const gapY = 10;

  rawLinks.value.forEach((edge, idx) => {
    const source = nodeMap.get(edge.source);
    const target = nodeMap.get(edge.target);
    if (!source || !target) return;

    const sourceCol = colIndex.get(source.layer || '') ?? 0;
    const targetCol = colIndex.get(target.layer || '') ?? 0;

    const x1 = sourceCol * COLUMN_WIDTH + 112;
    const y1 = source.row * (nodeHeight + gapY) + nodeHeight / 2 + 24;
    const x2 = targetCol * COLUMN_WIDTH + 8;
    const y2 = target.row * (nodeHeight + gapY) + nodeHeight / 2 + 24;
    const cpx = x1 + (x2 - x1) * 0.5;

    links.push({
      path: `M ${x1} ${y1} C ${cpx} ${y1}, ${cpx} ${y2}, ${x2} ${y2}`,
      delay: idx * 15
    });
  });

  return links;
});

const canvasStyle = computed(() => {
  const colCount = columns.value.length;
  return {
    minWidth: `${Math.max(colCount * COLUMN_WIDTH + 24, 300)}px`
  };
});

const nodeCount = computed(() => rawNodes.value.length);

async function loadChainList() {
  try {
    const res = await getMobileChainConcepts();
    if (res?.success && res.data?.length) {
      availableChains.value = res.data.map((c: any) => ({
        chain_id: c.chain_id,
        name: c.name || c.chain_id
      }));
      if (!selectedChainId.value || !availableChains.value.find((c) => c.chain_id === selectedChainId.value)) {
        selectedChainId.value = availableChains.value[0].chain_id;
      }
    } else {
      availableChains.value = [];
      error.value = '收件箱暂无产业链概念数据';
    }
  } catch (err: any) {
    availableChains.value = [];
    error.value = err?.message || '概念列表加载失败';
  }
}

async function loadChain() {
  loading.value = true;
  error.value = '';
  selectedNode.value = null;

  try {
    if (!selectedChainId.value) {
      throw new Error('请选择产业链概念');
    }
    const concept = selectedChainId.value.replace(/^inbox::/, '');
    const res = await getMobileChain(concept);
    const data = res?.data;
    if (res?.success && data?.has_data && data.nodes?.length) {
      rawNodes.value = data.nodes;
      rawLinks.value = data.links || [];
      rawLayers.value = data.layers || [];
      graphMeta.value = {
        endpoint: '/api/mobile/chain',
        data_date: data.data_date || null,
        has_data: true,
        source: data.source || 'inbox_items.json',
        missing_sources: [],
        partial_success: false,
        demo_mode: false,
        stale: false,
        query_plan: ['mobile_chain'],
        elapsed_ms: 0
      };
    } else {
      throw new Error('产业链数据为空');
    }
  } catch (err: any) {
    error.value = err?.message || '产业链数据加载失败';
    rawNodes.value = [];
    rawLinks.value = [];
    rawLayers.value = [];
  } finally {
    loading.value = false;
    nextTick(updateCanvasSize);
  }
}

function selectNode(node: DisplayNode) {
  selectedNode.value = node;
}

function updateCanvasSize() {
  const canvas = canvasRef.value;
  if (!canvas) return;
  const cols = columns.value.length;
  const rows = Math.max(...columns.value.map((c) => c.nodes.length), 1);
  canvasWidth.value = Math.max(cols * COLUMN_WIDTH + 24, canvas.clientWidth);
  canvasHeight.value = Math.max(rows * 70 + 36, canvas.clientHeight);
}

function onCanvasScroll() {
  // 预留滚动优化
}

onMounted(async () => {
  await loadChainList();
  await loadChain();
});
</script>

<style scoped>
.mobile-chain {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 80px;
}

.chain-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}

.chain-select {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  font-size: 13px;
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(0, 0, 0, 0.2);
  color: var(--text-primary);
  outline: none;
}

.chain-select option {
  background: #1a1a2e;
  color: var(--text-primary);
}

.reload-btn {
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: var(--text-secondary);
  cursor: pointer;
}

.reload-btn:disabled {
  opacity: 0.5;
}

.chain-meta {
  display: flex;
  gap: 12px;
  font-size: 10px;
  color: var(--text-muted);
  margin-bottom: 10px;
}

.chain-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  font-size: 10px;
  color: var(--text-muted);
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.legend-dot {
  font-size: 6px;
}

.legend-upstream { color: #f5a623; }
.legend-midstream { color: #4c7fff; }
.legend-downstream { color: #0db89a; }
.legend-application { color: #a855f7; }

.chain-canvas-card :deep(.mobile-card-body) {
  padding: 0;
}

.chain-canvas {
  position: relative;
  overflow-x: auto;
  overflow-y: hidden;
  min-height: 320px;
  padding: 12px;
  background: rgba(0, 0, 0, 0.15);
}

.chain-loading,
.chain-error,
.chain-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 200px;
  color: var(--text-muted);
  font-size: 13px;
}

.chain-error {
  color: #f55763;
}

.chain-columns {
  display: flex;
  gap: 12px;
  position: relative;
  z-index: 2;
}

.chain-column {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 120px;
  flex-shrink: 0;
  animation: column-enter 0.5s cubic-bezier(0.22, 1, 0.36, 1) both;
}

@keyframes column-enter {
  from {
    opacity: 0;
    transform: translateX(-16px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}

.chain-level-label {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  text-align: center;
  padding-bottom: 4px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}

.chain-nodes {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.chain-node {
  padding: 10px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.08);
  cursor: pointer;
  transition: all 0.2s ease;
  position: relative;
  animation: node-enter 0.4s cubic-bezier(0.22, 1, 0.36, 1) both;
}

.chain-node::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 0 3px 3px 0;
}

.chain-node--upstream::before { background: #f5a623; box-shadow: 0 0 8px rgba(245, 166, 35, 0.25); }
.chain-node--midstream::before { background: #4c7fff; box-shadow: 0 0 8px rgba(76, 127, 255, 0.25); }
.chain-node--downstream::before { background: #0db89a; box-shadow: 0 0 8px rgba(13, 184, 154, 0.25); }
.chain-node--application::before { background: #a855f7; box-shadow: 0 0 8px rgba(168, 85, 247, 0.25); }
.chain-node--stock::before { background: #22d3ee; box-shadow: 0 0 8px rgba(34, 211, 238, 0.25); }

.chain-node:active {
  transform: scale(0.97);
  background: rgba(255, 255, 255, 0.08);
}

.chain-node-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.chain-node-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  word-break: break-word;
}

.chain-node-note {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 4px;
  line-height: 1.3;
}

.chain-links {
  position: absolute;
  top: 0;
  left: 0;
  pointer-events: none;
  z-index: 1;
}

.chain-link {
  fill: none;
  stroke: rgba(255, 255, 255, 0.12);
  stroke-width: 1.5;
  animation: link-enter 0.4s cubic-bezier(0.22, 1, 0.36, 1) both;
}

@keyframes link-enter {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

.chain-detail {
  margin-top: 0;
}

.detail-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.detail-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.detail-layer {
  font-size: 11px;
  color: var(--accent-primary-light);
  font-weight: 500;
}

.detail-note {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.detail-keywords {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.detail-chip {
  font-size: 10px;
  padding: 3px 8px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--text-secondary);
}

.detail-stats {
  display: flex;
  gap: 16px;
  padding-top: 8px;
  border-top: 1px solid rgba(255, 255, 255, 0.04);
}

.detail-stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.detail-stat .stat-label {
  font-size: 10px;
  color: var(--text-muted);
}

.detail-stat .stat-value {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

@media (prefers-reduced-motion: reduce) {
  .chain-column,
  .chain-node,
  .chain-link {
    animation: none;
    opacity: 1;
  }
}
</style>