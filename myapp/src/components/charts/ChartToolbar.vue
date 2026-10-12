<template>
  <div class="chart-toolbar">
    <!-- 第一组：基础操作与绘图 -->
    <div class="toolbar-group">
      <!-- 光标样式 -->
      <div class="toolbar-item" :class="{ active: activeTool === 'crosshair' }" @click="setTool('crosshair')">
        <i class="fas fa-crosshairs"></i>
        <span class="tooltip">十字准星</span>
      </div>

      <!-- 趋势线工具 -->
      <div class="toolbar-item has-submenu" :class="{ active: isTrendToolActive, open: openSubmenu === 'trend' }" @click="toggleSubmenu('trend')">
        <i class="fas fa-chart-line"></i>
        <span class="tooltip">趋势线</span>
        <div class="submenu" v-show="openSubmenu === 'trend'">
          <div class="submenu-item" :class="{ active: activeTool === 'trendLine' }" @click.stop="selectTool('trendLine')">
            <i class="fas fa-slash"></i>
            <span>趋势线</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'ray' }" @click.stop="selectTool('ray')">
            <i class="fas fa-long-arrow-alt-right"></i>
            <span>射线</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'horizontalLine' }" @click.stop="selectTool('horizontalLine')">
            <i class="fas fa-minus"></i>
            <span>水平线</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'verticalLine' }" @click.stop="selectTool('verticalLine')">
            <i class="fas fa-grip-lines-vertical"></i>
            <span>垂直线</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'channel' }" @click.stop="selectTool('channel')">
            <i class="fas fa-stream"></i>
            <span>通道线</span>
          </div>
        </div>
      </div>

      <!-- 斐波那契与江恩工具 -->
      <div class="toolbar-item has-submenu" :class="{ active: isFibToolActive, open: openSubmenu === 'fib' }" @click="toggleSubmenu('fib')">
        <i class="fas fa-grip-lines"></i>
        <span class="tooltip">斐波那契</span>
        <div class="submenu" v-show="openSubmenu === 'fib'">
          <div class="submenu-item" :class="{ active: activeTool === 'fibonacci' }" @click.stop="selectTool('fibonacci')">
            <i class="fas fa-percentage"></i>
            <span>斐波那契回调</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'gannFan' }" @click.stop="selectTool('gannFan')">
            <i class="fas fa-fan"></i>
            <span>江恩扇</span>
          </div>
        </div>
      </div>

      <!-- 几何图形 -->
      <div class="toolbar-item has-submenu" :class="{ active: isShapeToolActive, open: openSubmenu === 'shape' }" @click="toggleSubmenu('shape')">
        <i class="fas fa-shapes"></i>
        <span class="tooltip">几何图形</span>
        <div class="submenu" v-show="openSubmenu === 'shape'">
          <div class="submenu-item" :class="{ active: activeTool === 'rectangle' }" @click.stop="selectTool('rectangle')">
            <i class="far fa-square"></i>
            <span>矩形</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'circle' }" @click.stop="selectTool('circle')">
            <i class="far fa-circle"></i>
            <span>圆形</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'brush' }" @click.stop="selectTool('brush')">
            <i class="fas fa-paint-brush"></i>
            <span>笔刷</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'path' }" @click.stop="selectTool('path')">
            <i class="fas fa-bezier-curve"></i>
            <span>路径</span>
          </div>
        </div>
      </div>

      <!-- 文字注释 -->
      <div class="toolbar-item" :class="{ active: activeTool === 'text' }" @click="setTool('text')">
        <i class="fas fa-font"></i>
        <span class="tooltip">文字注释</span>
      </div>

      <!-- 技术形态 -->
      <div class="toolbar-item has-submenu" :class="{ active: isPatternToolActive, open: openSubmenu === 'pattern' }" @click="toggleSubmenu('pattern')">
        <i class="fas fa-wave-square"></i>
        <span class="tooltip">技术形态</span>
        <div class="submenu" v-show="openSubmenu === 'pattern'">
          <div class="submenu-item" :class="{ active: activeTool === 'headShoulders' }" @click.stop="selectTool('headShoulders')">
            <i class="fas fa-mountain"></i>
            <span>头肩顶/底</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'elliottWave' }" @click.stop="selectTool('elliottWave')">
            <i class="fas fa-water"></i>
            <span>艾略特波浪</span>
          </div>
          <div class="submenu-item" :class="{ active: activeTool === 'triangle' }" @click.stop="selectTool('triangle')">
            <i class="fas fa-play"></i>
            <span>三角形态</span>
          </div>
        </div>
      </div>

      <!-- 预测与测量工具 -->
      <div class="toolbar-item" :class="{ active: activeTool === 'position' }" @click="setTool('position')">
        <i class="fas fa-balance-scale"></i>
        <span class="tooltip">仓位工具</span>
      </div>
    </div>

    <div class="divider"></div>

    <!-- 第二组：辅助功能 -->
    <div class="toolbar-group">
      <!-- 测量尺 -->
      <div class="toolbar-item" :class="{ active: activeTool === 'measure' }" @click="setTool('measure')">
        <i class="fas fa-ruler"></i>
        <span class="tooltip">测量尺</span>
      </div>

      <!-- 放大 -->
      <div class="toolbar-item" @click="zoomIn">
        <i class="fas fa-search-plus"></i>
        <span class="tooltip">放大</span>
      </div>

      <!-- 磁铁模式 -->
      <div class="toolbar-item" :class="{ active: magnetMode }" @click="toggleMagnetMode">
        <i class="fas fa-magnet"></i>
        <span class="tooltip">磁铁模式</span>
      </div>

      <!-- 连续绘图模式 -->
      <div class="toolbar-item" :class="{ active: stayInDrawingMode }" @click="toggleStayInDrawingMode">
        <i class="fas fa-pencil-alt"></i>
        <span class="tooltip">连续绘图</span>
      </div>

      <!-- 锁定所有绘图 -->
      <div class="toolbar-item" :class="{ active: lockDrawings }" @click="toggleLockDrawings">
        <i class="fas fa-lock"></i>
        <span class="tooltip">锁定绘图</span>
      </div>

      <!-- 隐藏所有绘图 -->
      <div class="toolbar-item" :class="{ active: hideDrawings }" @click="toggleHideDrawings">
        <i class="fas fa-eye-slash"></i>
        <span class="tooltip">隐藏绘图</span>
      </div>

      <!-- 删除 -->
      <div class="toolbar-item has-submenu danger" :class="{ open: openSubmenu === 'delete' }" @click="toggleSubmenu('delete')">
        <i class="fas fa-trash-alt"></i>
        <span class="tooltip">删除</span>
        <div class="submenu" v-show="openSubmenu === 'delete'">
          <div class="submenu-item" @click.stop="removeSelected">
            <i class="fas fa-eraser"></i>
            <span>删除选中</span>
          </div>
          <div class="submenu-item danger" @click.stop="clearAll">
            <i class="fas fa-trash"></i>
            <span>清空所有</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';

/**
 * TradingView 风格图表工具栏
 * 提供绘图工具、测量工具和辅助功能
 */

type ToolType = 
  | 'crosshair' | 'trendLine' | 'ray' | 'horizontalLine' | 'verticalLine' | 'channel'
  | 'fibonacci' | 'gannFan'
  | 'rectangle' | 'circle' | 'brush' | 'path'
  | 'text'
  | 'headShoulders' | 'elliottWave' | 'triangle'
  | 'position'
  | 'measure';

const activeTool = ref<ToolType>('crosshair');
const magnetMode = ref(false);
const stayInDrawingMode = ref(false);
const lockDrawings = ref(false);
const hideDrawings = ref(false);
const openSubmenu = ref<string | null>(null);

const isTrendToolActive = computed(() => 
  ['trendLine', 'ray', 'horizontalLine', 'verticalLine', 'channel'].includes(activeTool.value)
);

const isFibToolActive = computed(() => 
  ['fibonacci', 'gannFan'].includes(activeTool.value)
);

const isShapeToolActive = computed(() => 
  ['rectangle', 'circle', 'brush', 'path'].includes(activeTool.value)
);

const isPatternToolActive = computed(() => 
  ['headShoulders', 'elliottWave', 'triangle'].includes(activeTool.value)
);

const emit = defineEmits<{
  'tool-change': [tool: ToolType];
  'magnet-toggle': [enabled: boolean];
  'stay-drawing-toggle': [enabled: boolean];
  'lock-toggle': [enabled: boolean];
  'hide-toggle': [enabled: boolean];
  'zoom-in': [];
  'remove-selected': [];
  'clear-all': [];
}>();

function setTool(tool: ToolType) {
  console.log('[ChartToolbar] setTool:', tool);
  activeTool.value = tool;
  openSubmenu.value = null;
  emit('tool-change', tool);
}

function selectTool(tool: ToolType) {
  console.log('[ChartToolbar] selectTool:', tool);
  activeTool.value = tool;
  openSubmenu.value = null;
  emit('tool-change', tool);
}

function toggleSubmenu(name: string) {
  console.log('[ChartToolbar] toggleSubmenu:', name);
  openSubmenu.value = openSubmenu.value === name ? null : name;
}

function toggleMagnetMode() {
  magnetMode.value = !magnetMode.value;
  emit('magnet-toggle', magnetMode.value);
}

function toggleStayInDrawingMode() {
  stayInDrawingMode.value = !stayInDrawingMode.value;
  emit('stay-drawing-toggle', stayInDrawingMode.value);
}

function toggleLockDrawings() {
  lockDrawings.value = !lockDrawings.value;
  emit('lock-toggle', lockDrawings.value);
}

function toggleHideDrawings() {
  hideDrawings.value = !hideDrawings.value;
  emit('hide-toggle', hideDrawings.value);
}

function zoomIn() {
  emit('zoom-in');
}

function removeSelected() {
  emit('remove-selected');
}

function clearAll() {
  emit('clear-all');
}

// 测试点击
function testClick(e: Event) {
  console.log('[ChartToolbar] testClick:', e.target);
}
</script>

<style scoped>
.chart-toolbar {
  display: flex;
  flex-direction: column;
  background: #1c202b;
  border-left: 1px solid #2a2e39;
  padding: 8px 4px;
  gap: 4px;
  height: 100%;
  overflow-y: auto;
}

.toolbar-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.toolbar-item {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s ease;
  color: #787b86;
}

.toolbar-item:hover {
  background: #2a2e39;
  color: #d1d4dc;
}

.toolbar-item.active {
  background: #2962ff;
  color: #fff;
}

.toolbar-item.danger:hover {
  background: rgba(239, 68, 68, 0.2);
  color: #ef4444;
}

.toolbar-item i {
  font-size: 14px;
}

/* Tooltip */
.tooltip {
  position: absolute;
  left: 100%;
  top: 50%;
  transform: translateY(-50%);
  margin-left: 8px;
  padding: 4px 8px;
  background: #2a2e39;
  border: 1px solid #363a45;
  border-radius: 4px;
  font-size: 11px;
  color: #d1d4dc;
  white-space: nowrap;
  opacity: 0;
  visibility: hidden;
  transition: all 0.15s ease;
  z-index: 100;
}

.toolbar-item:hover .tooltip {
  opacity: 1;
  visibility: visible;
}

/* Submenu */
.has-submenu {
  position: relative;
}

.has-submenu::after {
  content: '';
  position: absolute;
  right: 2px;
  bottom: 2px;
  width: 0;
  height: 0;
  border-left: 3px solid transparent;
  border-right: 3px solid transparent;
  border-top: 3px solid #787b86;
}

.submenu {
  position: absolute;
  left: 100%;
  top: 0;
  margin-left: 4px;
  background: #1c202b;
  border: 1px solid #2a2e39;
  border-radius: 4px;
  padding: 4px;
  min-width: 140px;
  z-index: 100;
}

/* 打开状态的样式 */
.has-submenu.open {
  background: #2a2e39;
}

.submenu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s ease;
  color: #787b86;
  font-size: 12px;
}

.submenu-item:hover {
  background: #2a2e39;
  color: #d1d4dc;
}

.submenu-item.active {
  background: rgba(41, 98, 255, 0.2);
  color: #2962ff;
}

.submenu-item.danger:hover {
  background: rgba(239, 68, 68, 0.2);
  color: #ef4444;
}

.submenu-item i {
  font-size: 12px;
  width: 16px;
  text-align: center;
}

.divider {
  height: 1px;
  background: #2a2e39;
  margin: 4px 0;
}

/* Scrollbar */
.chart-toolbar::-webkit-scrollbar {
  width: 3px;
}

.chart-toolbar::-webkit-scrollbar-track {
  background: transparent;
}

.chart-toolbar::-webkit-scrollbar-thumb {
  background: #2a2e39;
  border-radius: 2px;
}

.chart-toolbar::-webkit-scrollbar-thumb:hover {
  background: #363a45;
}
</style>
