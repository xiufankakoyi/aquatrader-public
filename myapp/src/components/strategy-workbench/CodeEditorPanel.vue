<template>
  <div class="code-editor-panel">
    <!-- 参数配置面板 (可折叠) -->
    <div class="config-section" :class="{ collapsed: isConfigCollapsed }">
      <div class="section-header" @click="isConfigCollapsed = !isConfigCollapsed">
        <i class="fas fa-cog section-icon"></i>
        <span class="section-title">回测参数</span>
        <i class="fas" :class="isConfigCollapsed ? 'fa-chevron-down' : 'fa-chevron-up'"></i>
      </div>
      
      <div v-show="!isConfigCollapsed" class="section-content">
        <div class="config-grid">
          <div class="config-item">
            <label class="config-label">初始资金</label>
            <div class="config-input-wrapper">
              <span class="input-prefix">¥</span>
              <input
                v-model.number="localConfig.initialCapital"
                type="number"
                class="config-input"
                step="10000"
                min="10000"
                @change="updateConfig"
              />
            </div>
          </div>
          
          <div class="config-item">
            <label class="config-label">开始日期</label>
            <input
              v-model="localConfig.startDate"
              type="date"
              class="config-input"
              @change="updateConfig"
            />
          </div>
          
          <div class="config-item">
            <label class="config-label">结束日期</label>
            <input
              v-model="localConfig.endDate"
              type="date"
              class="config-input"
              @change="updateConfig"
            />
          </div>
          
          <div class="config-item">
            <label class="config-label">股票池</label>
            <select v-model="localConfig.stockPool" class="config-select" @change="updateConfig">
              <option value="all">全市场</option>
              <option value="hs300">沪深300</option>
              <option value="zz500">中证500</option>
              <option value="zz1000">中证1000</option>
            </select>
          </div>
          
          <div class="config-item">
            <label class="config-label">基准指数</label>
            <select v-model="localConfig.benchmark" class="config-select" @change="updateConfig">
              <option value="000300.SH">沪深300</option>
              <option value="000905.SH">中证500</option>
              <option value="000001.SH">上证指数</option>
              <option value="399001.SZ">深证成指</option>
            </select>
          </div>
          
          <div class="config-item">
            <label class="config-label">手续费率</label>
            <div class="config-input-wrapper">
              <input
                v-model.number="localConfig.commission"
                type="number"
                class="config-input"
                step="0.0001"
                min="0"
                max="0.01"
                @change="updateConfig"
              />
              <span class="input-suffix">%</span>
            </div>
          </div>
          
          <div class="config-item">
            <label class="config-label">滑点</label>
            <div class="config-input-wrapper">
              <input
                v-model.number="localConfig.slippage"
                type="number"
                class="config-input"
                step="0.0001"
                min="0"
                max="0.01"
                @change="updateConfig"
              />
              <span class="input-suffix">%</span>
            </div>
          </div>
          
          <div class="config-item full-width">
            <button class="reset-btn" @click="resetConfig">
              <i class="fas fa-undo"></i>
              重置默认
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- API 文档抽屉按钮 -->
    <div class="doc-drawer-trigger">
      <button class="doc-btn" @click="isDocDrawerOpen = !isDocDrawerOpen">
        <i class="fas fa-book"></i>
        <span>API 文档</span>
        <i class="fas" :class="isDocDrawerOpen ? 'fa-chevron-right' : 'fa-chevron-left'"></i>
      </button>
    </div>

    <!-- 代码编辑器区域 -->
    <div class="editor-section">
      <div class="editor-header">
        <div class="editor-tabs">
          <div class="editor-tab active">
            <i class="fas fa-file-code"></i>
            <span>strategy.py</span>
          </div>
        </div>
        <div class="editor-actions">
          <button class="action-btn" @click="$emit('format')" title="格式化代码">
            <i class="fas fa-magic"></i>
          </button>
          <button class="action-btn" @click="copyCode" title="复制代码">
            <i class="fas fa-copy"></i>
          </button>
        </div>
      </div>
      
      <div class="editor-body">
        <MonacoEditor
          ref="editorRef"
          v-model="localCode"
          language="python"
          theme="vs-dark"
          :options="editorOptions"
          @change="onCodeChange"
          @save="() => emit('save')"
          @run="() => emit('run')"
        />
      </div>
    </div>

    <!-- API 文档抽屉 -->
    <div class="doc-drawer" :class="{ open: isDocDrawerOpen }">
      <div class="doc-drawer-header">
        <span class="doc-title">📚 API 文档</span>
        <button class="close-btn" @click="isDocDrawerOpen = false">
          <i class="fas fa-times"></i>
        </button>
      </div>
      <div class="doc-drawer-content">
        <div class="doc-section">
          <h4 class="doc-section-title">数据接口</h4>
          <div
            v-for="snippet in dataApiSnippets"
            :key="snippet.name"
            class="doc-snippet"
            @click="insertSnippet(snippet.code)"
          >
            <div class="snippet-name">{{ snippet.name }}</div>
            <div class="snippet-desc">{{ snippet.description }}</div>
          </div>
        </div>
        
        <div class="doc-section">
          <h4 class="doc-section-title">技术指标</h4>
          <div
            v-for="snippet in indicatorSnippets"
            :key="snippet.name"
            class="doc-snippet"
            @click="insertSnippet(snippet.code)"
          >
            <div class="snippet-name">{{ snippet.name }}</div>
            <div class="snippet-desc">{{ snippet.description }}</div>
          </div>
        </div>
        
        <div class="doc-section">
          <h4 class="doc-section-title">交易函数</h4>
          <div
            v-for="snippet in tradeSnippets"
            :key="snippet.name"
            class="doc-snippet"
            @click="insertSnippet(snippet.code)"
          >
            <div class="snippet-name">{{ snippet.name }}</div>
            <div class="snippet-desc">{{ snippet.description }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import MonacoEditor from '../editor/MonacoEditor.vue';
import type { BacktestConfig } from '../../types/backtest';

// ============================================
// Props & Emits
// ============================================
interface Props {
  modelValue: string;
  strategyName: string;
  isRunning: boolean;
  backtestConfig: BacktestConfig;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  'update:modelValue': [code: string];
  'update:config': [config: BacktestConfig];
  format: [];
  insertCode: [code: string];
  save: [];      // Ctrl+S 触发
  run: [];       // Ctrl+Enter 触发
}>();

// ============================================
// 状态
// ============================================
const editorRef = ref<InstanceType<typeof MonacoEditor>>();
const localCode = ref(props.modelValue);
const localConfig = ref<BacktestConfig>({ ...props.backtestConfig });
const isConfigCollapsed = ref(false);
const isDocDrawerOpen = ref(false);

// 编辑器配置
const editorOptions = {
  fontSize: 13,
  minimap: { enabled: true, scale: 1 },
  scrollBeyondLastLine: false,
  wordWrap: 'on',
  automaticLayout: true,
  lineNumbers: 'on',
  folding: true,
  renderLineHighlight: 'all',
  matchBrackets: 'always',
  tabSize: 4,
  insertSpaces: true,
};

// 代码片段
const dataApiSnippets = [
  {
    name: '获取日线数据',
    description: '获取指定股票的日线行情',
    code: `# 获取日线数据
daily_data = data_query.get_daily_data(
    stock_code='000001.SZ',
    start_date='2024-01-01',
    end_date='2024-12-31'
)`,
  },
  {
    name: '获取财务数据',
    description: '获取基本面财务指标',
    code: `# 获取财务数据
financial_data = data_query.get_financial_data(
    stock_code='000001.SZ',
    fields=['roe', 'pe', 'pb']
)`,
  },
];

const indicatorSnippets = [
  {
    name: '计算均线',
    description: '简单移动平均线 SMA',
    code: `# 计算均线
ma5 = data['close'].rolling(window=5).mean()
ma10 = data['close'].rolling(window=10).mean()
ma20 = data['close'].rolling(window=20).mean()`,
  },
  {
    name: '计算MACD',
    description: 'MACD 指标计算',
    code: `# 计算MACD
exp1 = data['close'].ewm(span=12, adjust=False).mean()
exp2 = data['close'].ewm(span=26, adjust=False).mean()
macd = exp1 - exp2
signal = macd.ewm(span=9, adjust=False).mean()`,
  },
  {
    name: '计算RSI',
    description: '相对强弱指标 RSI',
    code: `# 计算RSI
delta = data['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
rs = gain / loss
rsi = 100 - (100 / (1 + rs))`,
  },
];

const tradeSnippets = [
  {
    name: '买入信号',
    description: '生成买入交易信号',
    code: `# 买入条件示例
if (data['close'] > data['ma20'] and 
    data['volume'] > data['volume'].rolling(20).mean() * 2):
    signals[stock_code] = 'buy'`,
  },
  {
    name: '卖出信号',
    description: '生成卖出交易信号',
    code: `# 卖出条件示例
if (data['close'] < data['ma5'] or 
    data['close'] < entry_price * 0.95):  # 止损
    signals[stock_code] = 'sell'`,
  },
  {
    name: '仓位管理',
    description: '动态仓位计算',
    code: `# 仓位管理示例
position_size = min(
    int(available_cash / current_price / 100),
    int(max_position_ratio * total_value / current_price / 100)
) * 100`,
  },
];

// ============================================
// 计算属性
// ============================================
const hasUnsavedChanges = computed(() => {
  return localCode.value !== props.modelValue;
});

// ============================================
// 监听
// ============================================
watch(() => props.modelValue, (newVal) => {
  if (newVal !== localCode.value) {
    localCode.value = newVal;
  }
});

watch(() => props.backtestConfig, (newVal) => {
  localConfig.value = { ...newVal };
}, { deep: true });

// ============================================
// 方法
// ============================================
function onCodeChange() {
  emit('update:modelValue', localCode.value);
}

function updateConfig() {
  emit('update:config', { ...localConfig.value });
}

function resetConfig() {
  localConfig.value = {
    initialCapital: 1000000,
    startDate: '',
    endDate: '',
    stockPool: 'all',
    benchmark: '000300.SH',
    commission: 0.0003,
    slippage: 0.001,
  };
  updateConfig();
}

function copyCode() {
  navigator.clipboard.writeText(localCode.value);
}

function insertSnippet(code: string) {
  emit('insertCode', code);
  isDocDrawerOpen.value = false;
}
</script>

<style scoped>
.code-editor-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  position: relative;
}

/* 配置面板 - 卡片样式 */
.config-section {
  background-color: transparent;
  flex-shrink: 0;
}

.config-section.collapsed {
  background-color: transparent;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
  transition: all 0.2s;
  background-color: rgba(41, 98, 255, 0.1);
  border-bottom: 1px solid var(--border-color);
}

.section-header:hover {
  background-color: rgba(41, 98, 255, 0.2);
}

.section-icon {
  font-size: 12px;
  color: var(--accent-primary);
}

.section-title {
  flex: 1;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.section-header .fa-chevron-up,
.section-header .fa-chevron-down {
  font-size: 10px;
  color: var(--text-muted);
  transition: transform 0.2s;
}

.section-content {
  padding: 16px;
  background-color: var(--bg-secondary);
}

.config-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}

.config-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.config-item.full-width {
  grid-column: span 2;
}

.config-label {
  font-size: 10px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 500;
}

.config-input-wrapper {
  display: flex;
  align-items: center;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  overflow: hidden;
  transition: border-color 0.2s;
}

.config-input-wrapper:focus-within {
  border-color: var(--accent-primary);
}

.config-input {
  flex: 1;
  height: 28px;
  padding: 0 10px;
  background: transparent;
  border: none;
  color: var(--text-primary);
  font-size: 12px;
  font-family: 'JetBrains Mono', monospace;
  outline: none;
}

.config-select {
  height: 28px;
  padding: 0 10px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
  cursor: pointer;
  transition: border-color 0.2s;
}

.config-select:focus {
  border-color: var(--accent-primary);
}

.input-prefix,
.input-suffix {
  padding: 0 6px;
  font-size: 10px;
  color: var(--text-muted);
  font-family: 'JetBrains Mono', monospace;
}

.reset-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 28px;
  background-color: transparent;
  border: 1px dashed var(--border-color);
  border-radius: 4px;
  color: var(--text-muted);
  font-size: 10px;
  cursor: pointer;
  transition: all 0.2s;
}

.reset-btn:hover {
  border-color: var(--text-muted);
  color: var(--text-secondary);
}

/* 文档抽屉按钮 */
.doc-drawer-trigger {
  padding: 6px 12px;
  border-bottom: 1px solid var(--border-color);
  background-color: var(--bg-secondary);
  flex-shrink: 0;
}

.doc-btn {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 6px 10px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.2s;
}

.doc-btn:hover {
  background-color: var(--bg-hover);
  border-color: var(--border-hover);
  color: var(--text-primary);
}

.doc-btn i {
  font-size: 10px;
}

/* 编辑器区域 */
.editor-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}

.editor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 32px;
  padding: 0 12px;
  background-color: var(--bg-tertiary);
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
}

.editor-tabs {
  display: flex;
  gap: 4px;
}

.editor-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  height: 24px;
  background-color: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 4px 4px 0 0;
  font-size: 11px;
  color: var(--text-primary);
}

.editor-tab i {
  font-size: 10px;
  color: var(--accent-primary);
}

.editor-actions {
  display: flex;
  gap: 4px;
}

.action-btn {
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: 3px;
  color: var(--text-muted);
  font-size: 10px;
  cursor: pointer;
  transition: all 0.2s;
}

.action-btn:hover {
  background-color: var(--bg-hover);
  color: var(--text-primary);
}

.editor-body {
  flex: 1;
  overflow: hidden;
}

/* API 文档抽屉 */
.doc-drawer {
  position: absolute;
  top: 0;
  right: 0;
  width: 280px;
  height: 100%;
  background-color: var(--bg-secondary);
  border-left: 1px solid var(--border-color);
  transform: translateX(100%);
  transition: transform 0.3s ease;
  z-index: 100;
  display: flex;
  flex-direction: column;
}

.doc-drawer.open {
  transform: translateX(0);
}

.doc-drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px;
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
}

.doc-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.close-btn {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: 4px;
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}

.close-btn:hover {
  background-color: var(--bg-hover);
  color: var(--text-primary);
}

.doc-drawer-content {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}

.doc-section {
  margin-bottom: 16px;
}

.doc-section-title {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--border-color);
}

.doc-snippet {
  padding: 8px 10px;
  margin-bottom: 6px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}

.doc-snippet:hover {
  background-color: var(--bg-hover);
  border-color: var(--accent-primary);
}

.snippet-name {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-primary);
  margin-bottom: 2px;
}

.snippet-desc {
  font-size: 10px;
  color: var(--text-muted);
}

/* 响应式 */
@media (max-width: 767px) {
  .config-grid {
    grid-template-columns: 1fr;
  }
  
  .config-item.full-width {
    grid-column: span 1;
  }
  
  .doc-drawer {
    width: 100%;
  }
}
</style>
