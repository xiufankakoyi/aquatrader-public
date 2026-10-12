<template>
  <div class="filter-condition">
    <a-row :gutter="8" align="middle">
      <!-- 指标选择 -->
      <a-col :span="7">
        <a-select
          v-model:value="selectedField"
          placeholder="选择指标"
          style="width: 100%"
          @change="onFieldChange"
          show-search
          :filter-option="filterOption"
          class="dark-select"
        >
          <a-select-opt-group
            v-for="(category, key) in categories"
            :key="key"
            :label="category.name"
          >
            <a-select-option
              v-for="indicator in category.indicators"
              :key="indicator.field"
              :value="indicator.field"
            >
              {{ indicator.name }}
              <span v-if="indicator.unit" class="unit">({{ indicator.unit }})</span>
            </a-select-option>
          </a-select-opt-group>
        </a-select>
      </a-col>

      <!-- 运算符选择 -->
      <a-col :span="5">
        <a-select
          v-model:value="selectedOperator"
          placeholder="运算符"
          style="width: 100%"
          @change="onOperatorChange"
          class="dark-select"
        >
          <a-select-option
            v-for="op in availableOperators"
            :key="op.value"
            :value="op.value"
          >
            {{ op.label }}
          </a-select-option>
        </a-select>
      </a-col>

      <!-- 值输入 -->
      <a-col :span="10">
        <!-- 单个值 -->
        <template v-if="inputType === 'single'">
          <a-input-number
            v-if="indicatorType === 'number'"
            v-model:value="value1"
            style="width: 100%"
            :placeholder="`输入数值${indicatorUnit ? ' (' + indicatorUnit + ')' : ''}`"
            :precision="4"
            class="dark-input-number"
          />
          <a-input
            v-else-if="indicatorType === 'text'"
            v-model:value="textValue"
            style="width: 100%"
            placeholder="输入文本"
            class="dark-input"
          />
          <a-switch
            v-else-if="indicatorType === 'boolean'"
            v-model:checked="boolValue"
            checked-children="是"
            un-checked-children="否"
            class="dark-switch"
          />
          <a-date-picker
            v-else-if="indicatorType === 'date'"
            v-model:value="dateValue"
            style="width: 100%"
            value-format="YYYY-MM-DD"
            class="dark-date-picker"
          />
        </template>

        <!-- 范围值 -->
        <template v-else-if="inputType === 'range'">
          <a-input-group compact>
            <a-input-number
              v-model:value="value1"
              style="width: 45%"
              :precision="4"
              placeholder="最小值"
              class="dark-input-number"
            />
            <a-input style="width: 10%; text-align: center; border-left: 0; border-right: 0; pointer-events: none; background: #252526; color: #787b86; border-color: #3e3e42;" placeholder="~" disabled />
            <a-input-number
              v-model:value="value2"
              style="width: 45%"
              :precision="4"
              placeholder="最大值"
              class="dark-input-number"
            />
          </a-input-group>
        </template>

        <!-- 百分比 -->
        <template v-else-if="inputType === 'percent'">
          <a-input-number
            v-model:value="value1"
            style="width: 100%"
            :min="0"
            :max="100"
            :precision="2"
            addon-after="%"
            placeholder="输入百分比"
            class="dark-input-number"
          />
        </template>
      </a-col>

      <!-- 删除按钮 -->
      <a-col :span="2">
        <a-button type="text" danger @click="onRemove" class="dark-delete-btn">
          <DeleteOutlined />
        </a-button>
      </a-col>
    </a-row>

    <!-- 统计信息 -->
    <div v-if="fieldStats && fieldStats[field]" class="stats-hint">
      <a-tag size="small" color="blue" class="dark-tag">
        范围: {{ formatNumber(fieldStats[field].min) }} ~ {{ formatNumber(fieldStats[field].max) }}
      </a-tag>
      <a-tag size="small" color="green" class="dark-tag-green">
        均值: {{ formatNumber(fieldStats[field].avg) }}
      </a-tag>
    </div>

    <!-- 图形化操作区域 -->
    <div v-if="fieldStats && fieldStats[field] && indicatorType === 'number'" class="visual-controls">
      <!-- 滑块控制 -->
      <div class="slider-control">
        <div class="slider-label">
          <span>拖动调整阈值</span>
          <span class="current-value">{{ formatNumber(currentSliderValue) }}</span>
        </div>
        <a-slider
          v-model:value="sliderValue"
          :min="fieldStats[field].min"
          :max="fieldStats[field].max"
          :step="calculateStep(fieldStats[field].min, fieldStats[field].max)"
          @change="onSliderChange"
          class="dark-slider"
        />
        <div class="slider-range">
          <span>{{ formatNumber(fieldStats[field].min) }}</span>
          <span>{{ formatNumber(fieldStats[field].max) }}</span>
        </div>
      </div>

      <!-- 直方图预览 -->
      <div class="histogram-preview">
        <div class="histogram-title">
          <span>数据分布</span>
          <span class="percentile-hint">{{ calculatePercentile() }}</span>
        </div>
        <div class="histogram-bars">
          <div
            v-for="(bar, index) in generateHistogram()"
            :key="index"
            class="histogram-bar"
            :style="{
              height: bar.height + '%',
              background: bar.isSelected ? '#2962ff' : '#3e3e42',
              opacity: bar.isSelected ? 1 : 0.6
            }"
            :title="`${formatNumber(bar.range[0])} ~ ${formatNumber(bar.range[1])}: ${bar.count}条`"
          />
        </div>
        <div class="histogram-axis">
          <span>{{ formatNumber(fieldStats[field].min) }}</span>
          <span>{{ formatNumber((fieldStats[field].min + fieldStats[field].max) / 2) }}</span>
          <span>{{ formatNumber(fieldStats[field].max) }}</span>
        </div>
      </div>

      <!-- 快捷选择按钮 -->
      <div class="quick-select">
        <span class="quick-label">快捷选择:</span>
        <a-space wrap>
          <a-button
            size="small"
            class="quick-btn"
            @click="setQuickValue(fieldStats[field].min, 'gt')"
          >
            最小值以上
          </a-button>
          <a-button
            size="small"
            class="quick-btn"
            @click="setQuickValue(fieldStats[field].avg, 'gt')"
          >
            均值以上
          </a-button>
          <a-button
            size="small"
            class="quick-btn"
            @click="setQuickValue(fieldStats[field].max, 'lt')"
          >
            最大值以下
          </a-button>
          <a-button
            size="small"
            class="quick-btn"
            @click="setRangeValue(fieldStats[field].avg * 0.9, fieldStats[field].avg * 1.1)"
          >
            均值±10%
          </a-button>
        </a-space>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { DeleteOutlined } from '@ant-design/icons-vue'
import dayjs from 'dayjs'
import type { Indicator, IndicatorCategory, Operator, FilterCondition } from '@/api/screener'

interface Props {
  index: number
  condition?: FilterCondition
  categories: Record<string, IndicatorCategory>
  operators: Record<string, Operator[]>
  fieldStats: Record<string, any>
}

const props = defineProps<Props>()
const emit = defineEmits<{
  change: [index: number, condition: FilterCondition]
  remove: [index: number]
}>()

// ============ 响应式数据 ============
const selectedField = ref<string>('')
const selectedOperator = ref<string>('')
const value1 = ref<number | null>(null)
const value2 = ref<number | null>(null)
const textValue = ref<string>('')
const boolValue = ref<boolean>(false)
const dateValue = ref<string>('')
const sliderValue = ref<number>(0)

// ============ 计算属性 ============
const currentIndicator = computed<Indicator | null>(() => {
  if (!selectedField.value) return null
  for (const cat of Object.values(props.categories)) {
    const found = cat.indicators.find(ind => ind.field === selectedField.value)
    if (found) return found
  }
  return null
})

const indicatorType = computed(() => {
  return currentIndicator.value?.type || 'number'
})

const indicatorUnit = computed(() => {
  return currentIndicator.value?.unit || ''
})

const availableOperators = computed(() => {
  const type = indicatorType.value
  return props.operators[type] || props.operators['number'] || []
})

const inputType = computed(() => {
  const op = availableOperators.value.find(o => o.value === selectedOperator.value)
  return op?.input || 'single'
})

const field = computed(() => selectedField.value)

const currentSliderValue = computed(() => {
  return sliderValue.value
})

// ============ 方法 ============
function filterOption(input: string, option: any) {
  const text = option.children?.[0]?.children || option.label || ''
  return text.toLowerCase().includes(input.toLowerCase())
}

function onFieldChange() {
  // 重置运算符和值
  selectedOperator.value = ''
  value1.value = null
  value2.value = null
  textValue.value = ''
  boolValue.value = false
  dateValue.value = ''

  // 加载字段统计
  if (selectedField.value) {
    emitChange()
  }
}

function onOperatorChange() {
  // 重置值
  value1.value = null
  value2.value = null
  textValue.value = ''
  boolValue.value = false
  dateValue.value = ''
}

function onRemove() {
  emit('remove', props.index)
}

function emitChange() {
  const condition: FilterCondition = {
    field: selectedField.value,
    operator: selectedOperator.value,
    value: getValue()
  }

  if (inputType.value === 'range') {
    condition.value2 = value2.value
  }

  emit('change', props.index, condition)
}

function getValue(): any {
  const type = indicatorType.value
  if (type === 'boolean') {
    return boolValue.value
  } else if (type === 'text') {
    return textValue.value
  } else if (type === 'date') {
    return dateValue.value
  } else {
    return value1.value
  }
}

function formatNumber(val: number | null): string {
  if (val === null || val === undefined) return '-'
  if (Math.abs(val) >= 100000000) {
    return (val / 100000000).toFixed(2) + '亿'
  } else if (Math.abs(val) >= 10000) {
    return (val / 10000).toFixed(2) + '万'
  } else if (Math.abs(val) >= 1) {
    return val.toFixed(2)
  } else {
    return val.toFixed(4)
  }
}

// ============ 图形化操作方法 ============
function calculateStep(min: number, max: number): number {
  const range = max - min
  if (range >= 100000000) return 1000000
  if (range >= 10000) return 100
  if (range >= 100) return 1
  if (range >= 1) return 0.01
  return 0.0001
}

function onSliderChange(val: number) {
  // 根据当前运算符更新值
  if (inputType.value === 'single') {
    value1.value = val
    // 如果没有选择运算符，默认选择大于
    if (!selectedOperator.value) {
      selectedOperator.value = 'gt'
    }
  } else if (inputType.value === 'range') {
    // 范围模式下，滑块控制上限
    value2.value = val
    if (value1.value === null) {
      value1.value = props.fieldStats[field.value]?.min || val * 0.9
    }
    if (!selectedOperator.value) {
      selectedOperator.value = 'between'
    }
  }
}

function generateHistogram() {
  const stats = props.fieldStats[field.value]
  if (!stats) return []

  const bars = []
  const binCount = 20
  const min = stats.min
  const max = stats.max
  const binSize = (max - min) / binCount

  // 模拟数据分布（基于正态分布）
  for (let i = 0; i < binCount; i++) {
    const binMin = min + i * binSize
    const binMax = min + (i + 1) * binSize

    // 模拟每个区间的数据量（中间多，两边少）
    const normalizedPos = (i - binCount / 2) / (binCount / 2)
    const gaussian = Math.exp(-0.5 * normalizedPos * normalizedPos)
    const count = Math.round(gaussian * 1000) + Math.random() * 50

    // 判断当前值是否在这个区间
    const currentVal = value1.value !== null ? value1.value : sliderValue.value
    const isSelected = currentVal >= binMin && currentVal < binMax

    bars.push({
      range: [binMin, binMax],
      count: count,
      height: Math.max(10, gaussian * 100),
      isSelected
    })
  }

  return bars
}

function calculatePercentile(): string {
  const stats = props.fieldStats[field.value]
  if (!stats || value1.value === null) return ''

  const val = value1.value
  const min = stats.min
  const max = stats.max

  if (max === min) return '50%'

  const percentile = ((val - min) / (max - min)) * 100
  return `前 ${Math.max(0, Math.min(100, percentile.toFixed(1)))}%`
}

function setQuickValue(val: number, operator: string) {
  value1.value = val
  selectedOperator.value = operator
  sliderValue.value = val
}

function setRangeValue(min: number, max: number) {
  value1.value = min
  value2.value = max
  selectedOperator.value = 'between'
  sliderValue.value = max
}

// ============ 监听 ============
watch([value1, value2, textValue, boolValue, dateValue], () => {
  if (selectedField.value && selectedOperator.value) {
    emitChange()
  }
}, { deep: true })

// ============ 初始化 ============
onMounted(() => {
  if (props.condition) {
    selectedField.value = props.condition.field
    selectedOperator.value = props.condition.operator

    const type = indicatorType.value
    if (type === 'boolean') {
      boolValue.value = props.condition.value
    } else if (type === 'text') {
      textValue.value = props.condition.value
    } else if (type === 'date') {
      dateValue.value = props.condition.value
    } else {
      value1.value = props.condition.value
      if (props.condition.value2 !== undefined) {
        value2.value = props.condition.value2
      }
    }
  }
})
</script>

<style scoped lang="scss">
.filter-condition {
  padding: 12px;
  background: #1a1a1a;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
  margin-bottom: 8px;

  &:hover {
    background: #252526;
  }

  .unit {
    color: #787b86;
    font-size: 12px;
  }

  .stats-hint {
    margin-top: 8px;
    display: flex;
    gap: 8px;
  }
}

// 深色选择器
.dark-select {
  &:deep(.ant-select-selector) {
    background: #252526 !important;
    border-color: #3e3e42 !important;
    color: #d1d4dc !important;
  }

  &:deep(.ant-select-arrow) {
    color: #787b86;
  }

  &:deep(.ant-select-selection-placeholder) {
    color: #787b86;
  }

  &:deep(.ant-select-selection-item) {
    color: #d1d4dc;
  }
}

// 深色输入框
.dark-input-number {
  &:deep(.ant-input-number-input) {
    background: #252526 !important;
    color: #d1d4dc !important;
  }

  &:deep(.ant-input-number-input-wrap) {
    background: #252526 !important;
  }

  &:deep(.ant-input-number-handler-wrap) {
    background: #2d2d30;
    border-left: 1px solid #3e3e42;
  }

  &:deep(.ant-input-number-handler) {
    color: #787b86;

    &:hover {
      color: #d1d4dc;
    }
  }

  &:deep(.ant-input-number) {
    background: #252526 !important;
    border-color: #3e3e42 !important;
  }

  &:deep(.ant-input-number-group-addon) {
    background: #2d2d30;
    border-color: #3e3e42;
    color: #787b86;
  }
}

// 深色文本输入框
.dark-input {
  background: #252526 !important;
  border-color: #3e3e42 !important;
  color: #d1d4dc !important;

  &::placeholder {
    color: #787b86;
  }

  &:focus {
    border-color: #2962ff !important;
  }
}

// 深色开关
.dark-switch {
  &:deep(.ant-switch-inner) {
    background: #3e3e42;
  }

  &:deep(.ant-switch-checked .ant-switch-inner) {
    background: #089981;
  }
}

// 深色日期选择器
.dark-date-picker {
  &:deep(.ant-picker) {
    background: #252526 !important;
    border-color: #3e3e42 !important;
  }

  &:deep(.ant-picker-input > input) {
    color: #d1d4dc !important;

    &::placeholder {
      color: #787b86;
    }
  }

  &:deep(.ant-picker-suffix) {
    color: #787b86;
  }
}

// 深色删除按钮
.dark-delete-btn {
  color: #f23645 !important;

  &:hover {
    color: #ff6b6b !important;
    background: rgba(242, 54, 69, 0.1) !important;
  }
}

// 深色标签
.dark-tag {
  background: rgba(41, 98, 255, 0.15) !important;
  border: 1px solid rgba(41, 98, 255, 0.3) !important;
  color: #2962ff !important;
}

.dark-tag-green {
  background: rgba(8, 153, 129, 0.15) !important;
  border: 1px solid rgba(8, 153, 129, 0.3) !important;
  color: #089981 !important;
}

// 图形化操作区域
.visual-controls {
  margin-top: 16px;
  padding: 16px;
  background: #0d1117;
  border: 1px solid #2a2a2a;
  border-radius: 8px;

  // 滑块控制
  .slider-control {
    margin-bottom: 16px;

    .slider-label {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 12px;
      color: #787b86;

      .current-value {
        color: #2962ff;
        font-weight: 600;
        font-size: 14px;
      }
    }

    .slider-range {
      display: flex;
      justify-content: space-between;
      margin-top: 4px;
      font-size: 11px;
      color: #505053;
    }
  }

  // 深色滑块
  .dark-slider {
    &:deep(.ant-slider-rail) {
      background: #2a2a2a;
    }

    &:deep(.ant-slider-track) {
      background: #2962ff;
    }

    &:deep(.ant-slider-handle) {
      border-color: #2962ff;
      background: #2962ff;

      &:hover,
      &:focus {
        border-color: #2962ff;
        box-shadow: 0 0 0 4px rgba(41, 98, 255, 0.2);
      }
    }
  }

  // 直方图预览
  .histogram-preview {
    margin-bottom: 16px;

    .histogram-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 12px;
      color: #787b86;

      .percentile-hint {
        color: #089981;
        font-weight: 500;
      }
    }

    .histogram-bars {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      height: 60px;
      padding: 0 4px;
      gap: 2px;

      .histogram-bar {
        flex: 1;
        min-width: 4px;
        border-radius: 2px 2px 0 0;
        transition: all 0.3s ease;
        cursor: pointer;

        &:hover {
          opacity: 1 !important;
        }
      }
    }

    .histogram-axis {
      display: flex;
      justify-content: space-between;
      margin-top: 4px;
      font-size: 10px;
      color: #505053;
    }
  }

  // 快捷选择
  .quick-select {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;

    .quick-label {
      font-size: 12px;
      color: #787b86;
      white-space: nowrap;
    }

    .quick-btn {
      background: #252526;
      border: 1px solid #3e3e42;
      color: #d1d4dc;
      font-size: 12px;

      &:hover {
        background: #2d2d30;
        border-color: #505053;
        color: #fff;
      }
    }
  }
}
</style>
