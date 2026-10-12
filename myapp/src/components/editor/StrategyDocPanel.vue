<template>
  <div class="doc-panel">
    <div class="doc-header">
      <span class="doc-title">📚 开发文档</span>
    </div>
    
    <div class="doc-content">
      <a-collapse v-model:activeKey="activeKeys" :bordered="false">
        <!-- 快速开始 -->
        <a-collapse-panel key="quickstart" header="快速开始">
          <div class="doc-section">
            <p class="doc-desc">3步创建你的第一个策略：</p>
            <ol class="doc-list">
              <li>继承 <code>StrategyBase</code> 基类</li>
              <li>实现 <code>generate_signals</code> 方法</li>
              <li>返回买卖信号字典</li>
            </ol>
            <a-button type="link" size="small" @click="insertTemplate('basic')">
              插入基础模板
            </a-button>
          </div>
        </a-collapse-panel>

        <!-- 数据字段 -->
        <a-collapse-panel key="fields" header="数据字段速查">
          <div class="doc-section">
            <p class="doc-desc">stock_pool_today 可用字段：</p>
            <div class="field-group">
              <div class="field-category">基础行情</div>
              <div class="field-tags">
                <a-tag v-for="field in basicFields" :key="field.name" 
                       class="field-tag" @click="insertField(field.name)">
                  {{ field.name }}
                  <a-tooltip :title="field.desc">
                    <InfoCircleOutlined class="info-icon" />
                  </a-tooltip>
                </a-tag>
              </div>
            </div>
            <div class="field-group">
              <div class="field-category">预计算均线</div>
              <div class="field-tags">
                <a-tag v-for="field in maFields" :key="field.name" 
                       class="field-tag" @click="insertField(field.name)">
                  {{ field.name }}
                </a-tag>
              </div>
            </div>
            <div class="field-group">
              <div class="field-category">状态标记</div>
              <div class="field-tags">
                <a-tag v-for="field in statusFields" :key="field.name" 
                       class="field-tag" @click="insertField(field.name)">
                  {{ field.name }}
                </a-tag>
              </div>
            </div>
          </div>
        </a-collapse-panel>

        <!-- 代码片段 -->
        <a-collapse-panel key="snippets" header="代码片段">
          <div class="doc-section">
            <div class="snippet-list">
              <div v-for="snippet in codeSnippets" :key="snippet.name" 
                   class="snippet-item" @click="insertSnippet(snippet.code)">
                <div class="snippet-name">{{ snippet.name }}</div>
                <div class="snippet-desc">{{ snippet.desc }}</div>
              </div>
            </div>
          </div>
        </a-collapse-panel>

        <!-- API参考 -->
        <a-collapse-panel key="api" header="API参考">
          <div class="doc-section">
            <div class="api-list">
              <div class="api-item">
                <div class="api-signature">generate_signals(current_date, stock_pool_today, data_query)</div>
                <div class="api-desc">策略核心方法，每天调用一次</div>
              </div>
              <div class="api-item">
                <div class="api-signature">data_query.query(sql)</div>
                <div class="api-desc">执行SQL查询，获取额外因子数据</div>
              </div>
              <div class="api-item">
                <div class="api-signature">data_query.get_stock_history(code, start, end)</div>
                <div class="api-desc">获取单只股票历史数据</div>
              </div>
            </div>
          </div>
        </a-collapse-panel>

        <!-- 最佳实践 -->
        <a-collapse-panel key="bestpractice" header="最佳实践">
          <div class="doc-section">
            <ul class="practice-list">
              <li>✅ 优先使用预计算指标，不要自己计算</li>
              <li>✅ 使用向量化操作替代循环遍历</li>
              <li>✅ 使用 <code>.get()</code> 避免 KeyError</li>
              <li>✅ 检查数据有效性后再使用</li>
              <li>❌ 不要在策略里计算 MA、RSI 等指标</li>
              <li>❌ 不要重复查询相同数据</li>
            </ul>
          </div>
        </a-collapse-panel>
      </a-collapse>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { InfoCircleOutlined } from '@ant-design/icons-vue';

interface Emits {
  (e: 'insert', code: string): void;
}

const emit = defineEmits<Emits>();

const activeKeys = ref(['quickstart', 'fields']);

// 基础字段
const basicFields = [
  { name: 'stock_code', desc: '股票代码，如 000001.SZ' },
  { name: 'trade_date', desc: '交易日期' },
  { name: 'open', desc: '开盘价' },
  { name: 'high', desc: '最高价' },
  { name: 'low', desc: '最低价' },
  { name: 'close', desc: '收盘价' },
  { name: 'volume', desc: '成交量' },
  { name: 'amount', desc: '成交额' },
  { name: 'total_mv', desc: '总市值（万元）' },
  { name: 'volume_ratio', desc: '量比' },
  { name: 'turnover_rate', desc: '换手率' },
];

// 均线字段
const maFields = [
  { name: 'ma5', desc: '5日均线' },
  { name: 'ma10', desc: '10日均线' },
  { name: 'ma20', desc: '20日均线' },
];

// 状态字段
const statusFields = [
  { name: 'is_st', desc: '是否ST股' },
  { name: 'is_limit_up', desc: '是否涨停' },
  { name: 'is_limit_down', desc: '是否跌停' },
  { name: 'is_suspended', desc: '是否停牌' },
];

// 代码片段
const codeSnippets = [
  {
    name: '买入条件',
    desc: '突破均线 + 放量 + 非ST',
    code: `if (row['close'] > row['ma20'] and 
    row['volume_ratio'] > 2.0 and
    not row['is_st'] and
    not row['is_limit_up']):
    signals[code] = 'buy'`,
  },
  {
    name: '卖出条件',
    desc: '跌破短期均线',
    code: `elif row['close'] < row['ma5']:
    signals[code] = 'sell'`,
  },
  {
    name: '富信号',
    desc: '带权重和评分的信号',
    code: `signals[code] = {
    'action': 'buy',
    'weight': 0.2,
    'score': 1.5,
    'params': {'reason': '突破均线'}
}`,
  },
  {
    name: '查询RSI',
    desc: '从动量因子表查询RSI',
    code: `rsi_df = data_query.query(f"""
    SELECT stock_code, rsi_14
    FROM factors_momentum
    WHERE trade_date = '{current_date}'
"")`,
  },
  {
    name: '向量化筛选',
    desc: '使用布尔索引批量筛选',
    code: `buy_mask = (
    (stock_pool['close'] > stock_pool['ma20']) &
    (stock_pool['volume_ratio'] > 2.0) &
    (~stock_pool['is_st'])
)`,
  },
];

// 插入字段
const insertField = (field: string) => {
  emit('insert', `row['${field}']`);
};

// 插入代码片段
const insertSnippet = (code: string) => {
  emit('insert', code);
};

// 插入模板
const insertTemplate = (type: string) => {
  if (type === 'basic') {
    const template = `from core.quant.strategies.strategy_framework import StrategyBase

class MyStrategy(StrategyBase):
    """
    我的策略描述
    """
    strategy_name = "我的策略"
    
    def __init__(self, name=None):
        super().__init__(name)
    
    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        策略逻辑实现
        """
        signals = {}
        
        for _, row in stock_pool_today.iterrows():
            code = row['stock_code']
            
            # 买入条件：突破均线 + 放量 + 非ST
            if (row['close'] > row['ma20'] and 
                row['volume_ratio'] > 2.0 and
                not row['is_st'] and
                not row['is_limit_up']):
                signals[code] = 'buy'
            
            # 卖出条件：跌破短期均线
            elif row['close'] < row['ma5']:
                signals[code] = 'sell'
            
            else:
                signals[code] = 'hold'
        
        return signals`;
    emit('insert', template);
  }
};
</script>

<style scoped>
.doc-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #1f1f1f;
  color: #d4d4d4;
}

.doc-header {
  padding: 12px 16px;
  border-bottom: 1px solid #333;
  background: #252526;
}

.doc-title {
  font-size: 14px;
  font-weight: 500;
  color: #e8e8e8;
}

.doc-content {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.doc-section {
  padding: 8px 0;
}

.doc-desc {
  font-size: 12px;
  color: #9cdcfe;
  margin-bottom: 8px;
}

.doc-list {
  font-size: 12px;
  padding-left: 16px;
  margin: 0;
}

.doc-list li {
  margin-bottom: 4px;
  color: #d4d4d4;
}

:deep(.ant-collapse) {
  background: transparent;
}

:deep(.ant-collapse-header) {
  color: #e8e8e8 !important;
  font-size: 13px;
  font-weight: 500;
  padding: 8px 12px !important;
}

:deep(.ant-collapse-content) {
  background: #252526;
  border-top: 1px solid #333;
}

:deep(.ant-collapse-content-box) {
  padding: 8px 12px !important;
}

.field-group {
  margin-bottom: 12px;
}

.field-category {
  font-size: 11px;
  color: #6a9955;
  margin-bottom: 6px;
  text-transform: uppercase;
}

.field-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.field-tag {
  cursor: pointer;
  font-size: 11px;
  padding: 2px 6px;
  background: #3c3c3c;
  border-color: #3c3c3c;
  color: #9cdcfe;
  transition: all 0.2s;
}

.field-tag:hover {
  background: #0e639c;
  border-color: #0e639c;
  color: #fff;
}

.info-icon {
  font-size: 10px;
  margin-left: 4px;
  opacity: 0.6;
}

.snippet-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.snippet-item {
  padding: 8px;
  background: #2d2d2d;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
  border: 1px solid transparent;
}

.snippet-item:hover {
  background: #3c3c3c;
  border-color: #0e639c;
}

.snippet-name {
  font-size: 12px;
  font-weight: 500;
  color: #dcdcaa;
  margin-bottom: 2px;
}

.snippet-desc {
  font-size: 11px;
  color: #6a9955;
}

.api-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.api-item {
  padding: 8px;
  background: #2d2d2d;
  border-radius: 4px;
}

.api-signature {
  font-family: 'Consolas', monospace;
  font-size: 11px;
  color: #dcdcaa;
  margin-bottom: 4px;
  word-break: break-all;
}

.api-desc {
  font-size: 11px;
  color: #6a9955;
}

.practice-list {
  font-size: 12px;
  padding-left: 16px;
  margin: 0;
}

.practice-list li {
  margin-bottom: 6px;
  color: #d4d4d4;
}

code {
  font-family: 'Consolas', monospace;
  background: #3c3c3c;
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11px;
  color: #ce9178;
}

:deep(.ant-btn-link) {
  color: #4ec9b0;
  padding: 0;
  font-size: 12px;
}

:deep(.ant-btn-link:hover) {
  color: #6ad9c0;
}
</style>
