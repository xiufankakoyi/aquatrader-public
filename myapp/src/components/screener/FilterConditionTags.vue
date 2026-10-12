<template>
  <div class="filter-condition-tags">
    <!-- 逻辑关系链 -->
    <div class="logic-chain">
      <div class="logic-node">
        <NodeIndexOutlined class="icon" />
        <span>条件组合</span>
      </div>
      <div class="logic-line"></div>
      <button class="logic-operator" @click="toggleLogic">
        {{ logic }}
      </button>
    </div>

    <!-- 筛选统计 -->
    <div class="filter-stats">
      <span>已选条件: <em>{{ conditions.length }}</em></span>
      <a-divider type="vertical" class="dark-divider" />
      <span>预估结果: <em>{{ estimatedResults }}</em> 只</span>
    </div>

    <!-- 标签云 -->
    <div class="tags-cloud">
      <TransitionGroup name="tag">
        <div
          v-for="(condition, index) in conditions"
          :key="condition.id || index"
          class="filter-tag"
          :class="getTagClass(condition)"
        >
          <span class="tag-field">{{ getFieldName(condition.field) }}</span>
          <span class="tag-operator">{{ getOperatorSymbol(condition.operator) }}</span>
          <span class="tag-value">
            <TrendIcon :condition="condition" />
            {{ formatValue(condition) }}
          </span>
          <button class="tag-remove" @click="removeCondition(index)" title="删除条件">
            <CloseOutlined />
          </button>
        </div>
      </TransitionGroup>

      <!-- 添加按钮 -->
      <a-button
        type="dashed"
        size="small"
        class="add-tag-btn"
        @click="$emit('add')"
      >
        <PlusOutlined />
        添加条件
      </a-button>
    </div>

    <!-- 空状态 -->
    <a-empty
      v-if="conditions.length === 0"
      description="点击添加筛选条件"
      :image="Empty.PRESENTED_IMAGE_SIMPLE"
      class="dark-empty"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  PlusOutlined,
  CloseOutlined,
  NodeIndexOutlined
} from '@ant-design/icons-vue'
import { Empty } from 'ant-design-vue'
import type { FilterCondition, IndicatorCategory } from '@/api/screener'

interface Props {
  conditions: FilterCondition[]
  logic: 'AND' | 'OR'
  categories: Record<string, IndicatorCategory>
  estimatedResults?: number
}

const props = withDefaults(defineProps<Props>(), {
  estimatedResults: 0
})

const emit = defineEmits<{
  'update:logic': [logic: 'AND' | 'OR']
  'remove': [index: number]
  'add': []
}>()

// 获取字段名称
function getFieldName(field: string): string {
  for (const cat of Object.values(props.categories)) {
    const found = cat.indicators.find(ind => ind.field === field)
    if (found) return found.name
  }
  return field
}

// 获取运算符符号
function getOperatorSymbol(operator: string): string {
  const symbols: Record<string, string> = {
    'gt': '>',
    'gte': '≥',
    'lt': '<',
    'lte': '≤',
    'eq': '=',
    'ne': '≠',
    'between': '∈',
    'in': '∈',
    'like': '~'
  }
  return symbols[operator] || operator
}

// 格式化值
function formatValue(condition: FilterCondition): string {
  const { value, value2, operator } = condition

  if (operator === 'between' && value2 !== undefined) {
    return `${value} - ${value2}`
  }

  // 百分比字段
  const percentFields = ['change_pct', 'turnover_rate', 'ret_5d', 'ret_20d', 'ret_60d']
  if (percentFields.includes(condition.field)) {
    return `${value}%`
  }

  // 金额字段
  const moneyFields = ['volume', 'amount']
  if (moneyFields.includes(condition.field)) {
    if (value >= 100000000) {
      return `${(value / 100000000).toFixed(1)}亿`
    } else if (value >= 10000) {
      return `${(value / 10000).toFixed(0)}万`
    }
    return value.toString()
  }

  // 市值字段（单位：万元 → 元）
  const capFields = ['total_mv', 'float_mv']
  if (capFields.includes(condition.field)) {
    const yuan = value * 10000
    if (yuan >= 100000000) {
      return `${(yuan / 100000000).toFixed(1)}亿`
    } else if (yuan >= 10000) {
      return `${(yuan / 10000).toFixed(0)}万`
    }
    return yuan.toString()
  }

  return value.toString()
}

// 获取标签样式类
function getTagClass(condition: FilterCondition): string {
  const bullishFields = ['change_pct', 'ret_5d', 'ret_20d', 'ret_60d', 'alpha_60d', 'alpha_120d', 'alpha_250d']
  const bearishFields = ['volatility_20d', 'max_drawdown_20d']

  if (bullishFields.includes(condition.field)) {
    const value = Number(condition.value)
    if (value > 0) return 'bullish'
    if (value < 0) return 'bearish'
  }

  if (bearishFields.includes(condition.field)) {
    return 'warning'
  }

  return ''
}

// 切换逻辑
function toggleLogic() {
  emit('update:logic', props.logic === 'AND' ? 'OR' : 'AND')
}

// 删除条件
function removeCondition(index: number) {
  emit('remove', index)
}
</script>

<script lang="ts">
// 趋势图标组件
import { h } from 'vue'
import { ArrowUpOutlined, ArrowDownOutlined, MinusOutlined } from '@ant-design/icons-vue'

const TrendIcon = {
  props: ['condition'],
  setup(props: { condition: FilterCondition }) {
    const { field, value } = props.condition
    const bullishFields = ['change_pct', 'ret_5d', 'ret_20d', 'ret_60d']

    if (!bullishFields.includes(field)) return null

    const numValue = Number(value)
    if (numValue > 0) {
      return () => h(ArrowUpOutlined, { class: 'trend-icon up' })
    } else if (numValue < 0) {
      return () => h(ArrowDownOutlined, { class: 'trend-icon down' })
    }
    return () => h(MinusOutlined, { class: 'trend-icon neutral' })
  }
}
</script>

<style scoped lang="scss">
.filter-condition-tags {
  padding: 8px 0;
}

// 逻辑链
.logic-chain {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid #2a2a2a;

  .logic-node {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: #787b86;

    .icon {
      font-size: 12px;
      color: #2962ff;
    }
  }

  .logic-line {
    flex: 1;
    height: 2px;
    background: linear-gradient(90deg, #2a2a2a, #2962ff, #2a2a2a);
    border-radius: 1px;
    position: relative;

    &::before,
    &::after {
      content: '';
      position: absolute;
      width: 4px;
      height: 4px;
      background: #2962ff;
      border-radius: 50%;
      top: 50%;
      transform: translateY(-50%);
    }

    &::before { left: 0; }
    &::after { right: 0; }
  }

  .logic-operator {
    padding: 2px 8px;
    background: #252526;
    border: 1px solid #3e3e42;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    color: #2962ff;
    cursor: pointer;
    transition: all 0.2s;

    &:hover {
      border-color: #2962ff;
      background: rgba(41, 98, 255, 0.1);
    }
  }
}

// 筛选统计
.filter-stats {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  font-size: 11px;
  color: #787b86;

  em {
    color: #2962ff;
    font-weight: 600;
    font-style: normal;
  }

  .dark-divider {
    background: #3e3e42;
  }
}

// 标签云
.tags-cloud {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 32px;
}

// 筛选标签
.filter-tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  background: #252526;
  border: 1px solid #3e3e42;
  border-radius: 14px;
  padding: 3px 8px;
  font-size: 11px;
  transition: all 0.2s;
  animation: tagIn 0.2s ease;

  &:hover {
    border-color: #505053;
    background: #2d2d30;
  }

  // 字段名
  .tag-field {
    color: #787b86;
    font-weight: 500;
  }

  // 运算符
  .tag-operator {
    color: #505053;
    font-size: 10px;
    padding: 0 2px;
  }

  // 值
  .tag-value {
    display: flex;
    align-items: center;
    gap: 2px;
    color: #d1d4dc;
    font-weight: 600;

    .trend-icon {
      font-size: 9px;

      &.up { color: #089981; }
      &.down { color: #f23645; }
      &.neutral { color: #787b86; }
    }
  }

  // 删除按钮
  .tag-remove {
    width: 14px;
    height: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    color: #505053;
    cursor: pointer;
    border-radius: 50%;
    margin-left: 2px;
    transition: all 0.2s;

    &:hover {
      background: rgba(242, 54, 69, 0.15);
      color: #f23645;
    }
  }

  // 上涨样式
  &.bullish {
    border-color: rgba(8, 153, 129, 0.4);
    background: rgba(8, 153, 129, 0.08);

    .tag-value {
      color: #089981;
    }
  }

  // 下跌样式
  &.bearish {
    border-color: rgba(242, 54, 69, 0.4);
    background: rgba(242, 54, 69, 0.08);

    .tag-value {
      color: #f23645;
    }
  }

  // 警告样式
  &.warning {
    border-color: rgba(245, 158, 11, 0.4);
    background: rgba(245, 158, 11, 0.08);

    .tag-value {
      color: #f59e0b;
    }
  }
}

// 添加按钮
.add-tag-btn {
  background: transparent !important;
  border: 1px dashed #3e3e42 !important;
  color: #787b86 !important;
  border-radius: 14px !important;
  height: 24px !important;
  font-size: 11px !important;
  padding: 0 10px !important;

  &:hover {
    border-color: #2962ff !important;
    color: #2962ff !important;
  }
}

// 动画
@keyframes tagIn {
  from {
    opacity: 0;
    transform: scale(0.9);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
}

.tag-enter-active,
.tag-leave-active {
  transition: all 0.2s ease;
}

.tag-enter-from,
.tag-leave-to {
  opacity: 0;
  transform: scale(0.9);
}

// 空状态
.dark-empty {
  margin: 24px 0;

  :deep(.ant-empty-description) {
    color: #505053;
  }
}
</style>
