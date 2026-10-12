<template>
  <div class="filter-panel" :class="{ 'panel-collapsed': !isExpanded }">
    <!-- 折叠式侧边栏 -->
    <div class="sidebar-container">
      <!-- 图标导航栏 -->
      <div class="sidebar-nav">
        <div class="nav-items">
          <!-- 日期选择 -->
          <div
            class="nav-item"
            :class="{ active: activePanel === 'date' }"
            @click="togglePanel('date')"
          >
            <CalendarOutlined class="nav-icon" />
            <span class="nav-label">日期</span>
            <div class="nav-indicator" v-if="selectedDate"></div>
          </div>

          <!-- 筛选条件 -->
          <div
            class="nav-item"
            :class="{ active: activePanel === 'filter', 'has-content': conditions.length > 0 }"
            @click="togglePanel('filter')"
          >
            <FilterOutlined class="nav-icon" />
            <span class="nav-label">条件</span>
            <span class="nav-badge" v-if="conditions.length > 0">{{ conditions.length }}</span>
          </div>

          <!-- 排序设置 -->
          <div
            class="nav-item"
            :class="{ active: activePanel === 'sort', 'has-content': orderBy.length > 0 }"
            @click="togglePanel('sort')"
          >
            <SortAscendingOutlined class="nav-icon" />
            <span class="nav-label">排序</span>
            <span class="nav-badge" v-if="orderBy.length > 0">{{ orderBy.length }}</span>
          </div>

          <!-- 模板管理 -->
          <div
            class="nav-item"
            :class="{ active: activePanel === 'template' }"
            @click="togglePanel('template')"
          >
            <SaveOutlined class="nav-icon" />
            <span class="nav-label">模板</span>
            <div class="nav-indicator" v-if="templates.length > 0"></div>
          </div>
        </div>

        <!-- 执行按钮 -->
        <div class="nav-footer">
          <a-button
            type="primary"
            :loading="loading"
            class="execute-nav-btn"
            @click="onExecuteFilter"
          >
            <SearchOutlined />
          </a-button>
        </div>
      </div>

      <!-- 展开的内容面板 -->
      <div class="sidebar-content" :class="{ expanded: isExpanded }">
        <div class="content-inner">
          <!-- 日期面板 -->
          <div v-show="activePanel === 'date'" class="panel-section">
            <div class="section-header">
              <h3 class="section-title">
                <CalendarOutlined />
                选择日期
              </h3>
            </div>
            <div class="section-body">
              <a-date-picker
                v-model:value="selectedDateValue"
                style="width: 100%"
                value-format="YYYY-MM-DD"
                :disabled-date="disabledDate"
                @change="onDateChange"
                class="dark-datepicker"
                placeholder="选择交易日期"
              />
              <div class="date-hint">
                <span v-if="latestDate" class="latest-badge">最新: {{ latestDate }}</span>
                <span v-else class="no-data-hint">暂无交易日数据</span>
              </div>
            </div>
          </div>

          <!-- 筛选条件面板 -->
          <div v-show="activePanel === 'filter'" class="panel-section">
            <div class="section-header">
              <h3 class="section-title">
                <FilterOutlined />
                筛选条件
              </h3>
              <a-button
                v-if="conditions.length > 0"
                type="text"
                size="small"
                class="clear-btn"
                @click="onClearAll"
              >
                清空
              </a-button>
            </div>
            <div class="section-body">
              <FilterConditionTags
                v-model:logic="logicValue"
                :conditions="conditions"
                :categories="categories"
                :estimated-results="estimatedResults"
                @add="onAddCondition"
                @remove="onConditionRemove"
              />
            </div>
          </div>

          <!-- 排序面板 -->
          <div v-show="activePanel === 'sort'" class="panel-section">
            <div class="section-header">
              <h3 class="section-title">
                <SortAscendingOutlined />
                排序设置
              </h3>
            </div>
            <div class="section-body">
              <div class="sort-list">
                <a-tag
                  v-for="sort in orderBy"
                  :key="sort.field"
                  closable
                  color="processing"
                  @close="onRemoveSort(sort.field)"
                  class="dark-tag"
                >
                  {{ getIndicatorName(sort.field) }}
                  <ArrowUpOutlined v-if="sort.direction === 'asc'" />
                  <ArrowDownOutlined v-else />
                </a-tag>

                <a-dropdown v-if="availableSortFields.length > 0">
                  <a-button type="dashed" size="small" class="dark-dashed-btn">
                    <PlusOutlined />
                    添加排序
                  </a-button>
                  <template #overlay>
                    <a-menu class="dark-menu">
                      <a-sub-menu
                        v-for="(category, key) in categories"
                        :key="key"
                        :title="category.name"
                      >
                        <a-menu-item
                          v-for="indicator in getSortableIndicators(category.indicators)"
                          :key="indicator.field"
                          @click="onAddSort(indicator.field, 'desc')"
                        >
                          {{ indicator.name }}
                        </a-menu-item>
                      </a-sub-menu>
                    </a-menu>
                  </template>
                </a-dropdown>
              </div>
            </div>
          </div>

          <!-- 模板面板 -->
          <div v-show="activePanel === 'template'" class="panel-section">
            <div class="section-header">
              <h3 class="section-title">
                <SaveOutlined />
                模板管理
              </h3>
            </div>
            <div class="section-body">
              <div class="template-actions">
                <a-dropdown>
                  <a-button class="dark-btn" block>
                    <FolderOpenOutlined />
                    加载模板
                  </a-button>
                  <template #overlay>
                    <a-menu class="dark-menu">
                      <a-empty
                        v-if="templates.length === 0"
                        description="暂无保存的模板"
                        :image="Empty.PRESENTED_IMAGE_SIMPLE"
                        style="padding: 16px"
                        class="dark-empty"
                      />
                      <a-menu-item
                        v-for="template in templates"
                        :key="template.id"
                      >
                        <div class="template-item" @click="onLoadTemplate(template.id)">
                          <span>{{ template.name }}</span>
                          <a-button
                            type="text"
                            danger
                            size="small"
                            @click.stop="onDeleteTemplate(template.id)"
                          >
                            <DeleteOutlined />
                          </a-button>
                        </div>
                      </a-menu-item>
                    </a-menu>
                  </template>
                </a-dropdown>

                <a-button @click="showSaveModal = true" class="dark-btn" block>
                  <SaveOutlined />
                  保存模板
                </a-button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 条件编辑器弹窗 -->
    <a-modal
      v-model:open="showConditionModal"
      title="添加筛选条件"
      @ok="onConfirmAddCondition"
      @cancel="showConditionModal = false"
      class="dark-modal"
      width="600px"
    >
      <FilterConditionItem
        v-if="editingCondition"
        :index="-1"
        :condition="editingCondition"
        :categories="categories"
        :operators="operators"
        :field-stats="fieldStats"
        @change="onEditingConditionChange"
      />
    </a-modal>

    <!-- 保存模板弹窗 -->
    <a-modal
      v-model:open="showSaveModal"
      title="保存筛选模板"
      @ok="onSaveTemplate"
      @cancel="showSaveModal = false"
      class="dark-modal"
    >
      <a-input
        v-model:value="templateName"
        placeholder="请输入模板名称"
        :max-length="50"
        class="dark-input"
      />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import {
  CalendarOutlined,
  FilterOutlined,
  ClearOutlined,
  PlusOutlined,
  SortAscendingOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
  SaveOutlined,
  FolderOpenOutlined,
  DeleteOutlined,
  SearchOutlined
} from '@ant-design/icons-vue'
import { Empty, message } from 'ant-design-vue'
import dayjs from 'dayjs'
import type { Indicator, IndicatorCategory, Operator, FilterCondition, OrderBy } from '@/api/screener'
import FilterConditionItem from './FilterCondition.vue'
import FilterConditionTags from './FilterConditionTags.vue'

interface Props {
  categories: Record<string, IndicatorCategory>
  operators: Record<string, Operator[]>
  conditions: FilterCondition[]
  logic: 'AND' | 'OR'
  orderBy: OrderBy[]
  selectedDate: string
  latestDate: string
  tradeDates: string[]
  fieldStats: Record<string, any>
  templates: any[]
  loading: boolean
}

const props = defineProps<Props>()
const emit = defineEmits<{
  'update:conditions': [conditions: FilterCondition[]]
  'update:logic': [logic: 'AND' | 'OR']
  'update:orderBy': [orderBy: OrderBy[]]
  'update:selectedDate': [date: string]
  'addCondition': []
  'removeCondition': [index: number]
  'clearConditions': []
  'executeFilter': []
  'saveTemplate': [name: string]
  'loadTemplate': [id: string]
  'deleteTemplate': [id: string]
  'loadFieldStats': [field: string]
}>()

// ============ 响应式数据 ============
const selectedDateValue = ref<string | null>(null)
const logicValue = ref<'AND' | 'OR'>('AND')
const showSaveModal = ref(false)
const templateName = ref('')
const showConditionModal = ref(false)
const editingCondition = ref<FilterCondition | null>(null)
const activePanel = ref<string>('date')
const isExpanded = ref(true)

// ============ 计算属性 ============
const estimatedResults = computed(() => {
  if (props.conditions.length === 0) return 0
  const baseCount = 5000
  const reductionPerCondition = 0.6
  return Math.round(baseCount * Math.pow(reductionPerCondition, props.conditions.length))
})

const availableSortFields = computed(() => {
  const usedFields = new Set(props.orderBy.map(o => o.field))
  const allFields: Indicator[] = []
  Object.values(props.categories).forEach(cat => {
    allFields.push(...cat.indicators.filter(ind => ind.type === 'number'))
  })
  return allFields.filter(ind => !usedFields.has(ind.field))
})

// ============ 方法 ============
function togglePanel(panel: string) {
  if (activePanel.value === panel && isExpanded.value) {
    isExpanded.value = false
  } else {
    activePanel.value = panel
    isExpanded.value = true
  }
}

function disabledDate(current: dayjs.Dayjs) {
  if (!props.tradeDates || props.tradeDates.length === 0) {
    return false
  }
  const dateStr = current.format('YYYY-MM-DD')
  return !props.tradeDates.includes(dateStr)
}

function onDateChange(date: string | null) {
  if (!date) return
  emit('update:selectedDate', date)
}

function onLogicChange() {
  emit('update:logic', logicValue.value)
}

function onAddCondition() {
  editingCondition.value = {
    field: '',
    operator: 'gt',
    value: 0
  }
  showConditionModal.value = true
}

function onEditingConditionChange(index: number, condition: FilterCondition) {
  editingCondition.value = condition
}

function onConfirmAddCondition() {
  if (editingCondition.value && editingCondition.value.field) {
    const newConditions = [...props.conditions, editingCondition.value]
    emit('update:conditions', newConditions)
    showConditionModal.value = false
    editingCondition.value = null
  } else {
    message.warning('请完整填写筛选条件')
  }
}

function onConditionChange(index: number, condition: FilterCondition) {
  const newConditions = [...props.conditions]
  newConditions[index] = condition
  emit('update:conditions', newConditions)
  if (condition.field) {
    emit('loadFieldStats', condition.field)
  }
}

function onConditionRemove(index: number) {
  emit('removeCondition', index)
}

function onClearAll() {
  emit('clearConditions')
}

function onAddSort(field: string, direction: 'asc' | 'desc') {
  const newOrderBy = [...props.orderBy, { field, direction }]
  emit('update:orderBy', newOrderBy)
}

function onRemoveSort(field: string) {
  const newOrderBy = props.orderBy.filter(o => o.field !== field)
  emit('update:orderBy', newOrderBy)
}

function getSortableIndicators(indicators: Indicator[]) {
  return indicators.filter(ind => ind.type === 'number')
}

function getIndicatorName(field: string): string {
  for (const cat of Object.values(props.categories)) {
    const found = cat.indicators.find(ind => ind.field === field)
    if (found) return found.name
  }
  return field
}

function onSaveTemplate() {
  if (!templateName.value.trim()) {
    message.warning('请输入模板名称')
    return
  }
  emit('saveTemplate', templateName.value.trim())
  showSaveModal.value = false
  templateName.value = ''
}

function onLoadTemplate(id: string) {
  emit('loadTemplate', id)
}

function onDeleteTemplate(id: string) {
  emit('deleteTemplate', id)
}

function onExecuteFilter() {
  emit('executeFilter')
}

// ============ 监听 ============
watch(() => props.selectedDate, (newDate) => {
  if (newDate) {
    selectedDateValue.value = newDate
  }
}, { immediate: true })

watch(() => props.logic, (newLogic) => {
  logicValue.value = newLogic
}, { immediate: true })
</script>

<style scoped lang="scss">
.filter-panel {
  height: 100%;
  min-height: calc(100vh - 200px);

  .sidebar-container {
    display: flex;
    height: 100%;
    min-height: calc(100vh - 200px);
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    overflow: hidden;
  }

  // 图标导航栏
  .sidebar-nav {
    width: 64px;
    background: #141414;
    border-right: 1px solid #2a2a2a;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 16px 0;
    flex-shrink: 0;

    .nav-items {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 0 8px;
    }

    .nav-item {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 4px;
      padding: 12px 8px;
      border-radius: 10px;
      cursor: pointer;
      transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      position: relative;

      &:hover {
        background: rgba(41, 98, 255, 0.1);

        .nav-icon {
          color: #2962ff;
          transform: scale(1.1);
        }

        .nav-label {
          color: #d1d4dc;
        }
      }

      &.active {
        background: rgba(41, 98, 255, 0.15);

        .nav-icon {
          color: #2962ff;
        }

        .nav-label {
          color: #2962ff;
          font-weight: 500;
        }

        &::before {
          content: '';
          position: absolute;
          left: -8px;
          top: 50%;
          transform: translateY(-50%);
          width: 3px;
          height: 24px;
          background: linear-gradient(180deg, #2962ff, #5c8aff);
          border-radius: 0 3px 3px 0;
          transition: opacity 0.25s ease;
        }
      }

      &.has-content {
        .nav-icon {
          color: #089981;
        }
      }

      .nav-icon {
        font-size: 20px;
        color: #787b86;
        transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      }

      .nav-label {
        font-size: 11px;
        color: #787b86;
        transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      }

      .nav-badge {
        position: absolute;
        top: 6px;
        right: 4px;
        min-width: 16px;
        height: 16px;
        padding: 0 4px;
        background: linear-gradient(135deg, #089981, #067a66);
        border-radius: 8px;
        font-size: 10px;
        color: #fff;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 600;
        box-shadow: 0 2px 6px rgba(8, 153, 129, 0.3);
        animation: badgePop 0.3s cubic-bezier(0.68, -0.55, 0.265, 1.55);
      }

      .nav-indicator {
        position: absolute;
        top: 6px;
        right: 6px;
        width: 6px;
        height: 6px;
        background: #089981;
        border-radius: 50%;
        box-shadow: 0 0 4px rgba(8, 153, 129, 0.5);
      }
    }

    .nav-footer {
      padding: 0 12px;

      .execute-nav-btn {
        width: 40px;
        height: 40px;
        border-radius: 10px;
        background: linear-gradient(135deg, #089981, #067a66);
        border: none;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
        box-shadow: 0 4px 12px rgba(8, 153, 129, 0.3);

        &:hover {
          transform: translateY(-2px);
          box-shadow: 0 6px 16px rgba(8, 153, 129, 0.4);
        }

        &:active {
          transform: translateY(0);
        }

        .anticon {
          font-size: 18px;
        }
      }
    }
  }

  // 内容面板
  .sidebar-content {
    width: 0;
    overflow: hidden;
    transition: width 0.35s cubic-bezier(0.4, 0, 0.2, 1);
    opacity: 0;

    &.expanded {
      width: 280px;
      opacity: 1;

      .content-inner {
        transform: translateX(0);
      }
    }

    .content-inner {
      width: 280px;
      height: 100%;
      padding: 20px;
      transform: translateX(-20px);
      transition: transform 0.35s cubic-bezier(0.4, 0, 0.2, 1);
      overflow-y: auto;

      &::-webkit-scrollbar {
        width: 4px;
      }

      &::-webkit-scrollbar-track {
        background: transparent;
      }

      &::-webkit-scrollbar-thumb {
        background: #3e3e42;
        border-radius: 2px;
      }
    }
  }

  // 面板区块
  .panel-section {
    margin-bottom: 24px;
    animation: fadeSlideIn 0.3s cubic-bezier(0.4, 0, 0.2, 1);

    &:last-child {
      margin-bottom: 0;
    }

    .section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid #2a2a2a;

      .section-title {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 14px;
        font-weight: 600;
        color: #d1d4dc;
        margin: 0;

        .anticon {
          color: #2962ff;
          font-size: 16px;
        }
      }

      .clear-btn {
        color: #787b86;
        font-size: 12px;
        padding: 2px 8px;
        height: auto;
        transition: all 0.2s ease;

        &:hover {
          color: #f23645;
          background: rgba(242, 54, 69, 0.1);
        }
      }
    }

    .section-body {
      .date-hint {
        margin-top: 12px;

        .latest-badge {
          display: inline-block;
          padding: 4px 10px;
          background: rgba(41, 98, 255, 0.1);
          border: 1px solid rgba(41, 98, 255, 0.2);
          border-radius: 6px;
          font-size: 11px;
          color: #2962ff;
          transition: all 0.2s ease;

          &:hover {
            background: rgba(41, 98, 255, 0.15);
            border-color: rgba(41, 98, 255, 0.3);
          }
        }

        .no-data-hint {
          display: inline-block;
          padding: 4px 10px;
          background: rgba(242, 54, 69, 0.08);
          border: 1px solid rgba(242, 54, 69, 0.15);
          border-radius: 6px;
          font-size: 11px;
          color: #f23645;
        }
      }

      .sort-list {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
      }

      .template-actions {
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
    }
  }

  .template-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 200px;

    span {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }
}

// 动画定义
@keyframes badgePop {
  0% {
    transform: scale(0);
    opacity: 0;
  }
  70% {
    transform: scale(1.2);
  }
  100% {
    transform: scale(1);
    opacity: 1;
  }
}

@keyframes fadeSlideIn {
  0% {
    opacity: 0;
    transform: translateX(-10px);
  }
  100% {
    opacity: 1;
    transform: translateX(0);
  }
}

// 深色按钮
.dark-btn {
  background: #252526 !important;
  border: 1px solid #3e3e42 !important;
  color: #d1d4dc !important;
  transition: all 0.2s ease;

  &:hover {
    background: #2d2d30 !important;
    border-color: #505053 !important;
    color: #fff !important;
    transform: translateY(-1px);
  }

  &:active {
    transform: translateY(0);
  }
}

.dark-dashed-btn {
  background: transparent !important;
  border: 1px dashed #3e3e42 !important;
  color: #787b86 !important;
  transition: all 0.2s ease;

  &:hover {
    border-color: #505053 !important;
    color: #d1d4dc !important;
    background: rgba(255, 255, 255, 0.02) !important;
  }
}

// 深色标签
.dark-tag {
  background: rgba(41, 98, 255, 0.1) !important;
  border: 1px solid rgba(41, 98, 255, 0.2) !important;
  color: #5c8aff !important;
  transition: all 0.2s ease;

  &:hover {
    background: rgba(41, 98, 255, 0.15) !important;
    border-color: rgba(41, 98, 255, 0.3) !important;
  }
}

// 深色菜单
.dark-menu {
  background: #1a1a1a !important;
  border: 1px solid #2a2a2a !important;

  &:deep(.ant-menu-item) {
    color: #d1d4dc !important;
    transition: all 0.15s ease;

    &:hover {
      background: #252526;
    }
  }

  &:deep(.ant-menu-submenu-title) {
    color: #d1d4dc !important;
    transition: all 0.15s ease;

    &:hover {
      background: #252526;
    }
  }

  &:deep(.ant-menu-submenu-popup) {
    background: #1a1a1a !important;
    border: 1px solid #2a2a2a !important;

    .ant-menu {
      background: #1a1a1a !important;
    }

    .ant-menu-item {
      color: #d1d4dc !important;
      transition: all 0.15s ease;

      &:hover {
        background: #252526;
      }
    }

    .ant-menu-submenu-title {
      color: #d1d4dc !important;
      transition: all 0.15s ease;

      &:hover {
        background: #252526;
      }
    }
  }
}

// 深色输入框
.dark-input {
  background: #252526 !important;
  border: 1px solid #3e3e42 !important;
  color: #d1d4dc !important;
  transition: all 0.2s ease;

  &:focus {
    border-color: #2962ff !important;
    box-shadow: 0 0 0 2px rgba(41, 98, 255, 0.2) !important;
  }

  &::placeholder {
    color: #787b86 !important;
  }
}

// 深色日期选择器
.dark-datepicker {
  background: #252526 !important;
  border: 1px solid #3e3e42 !important;
  border-radius: 6px;

  &:deep(.ant-picker-input) {
    input {
      color: #d1d4dc !important;
      background: transparent !important;
    }
  }

  &:deep(.ant-picker-suffix) {
    color: #787b86 !important;
  }

  &:deep(.ant-picker-clear) {
    color: #787b86;
    background: #252526;

    &:hover {
      color: #d1d4dc;
    }
  }

  // 日期选择器下拉面板深色主题
  &:deep(.ant-picker-dropdown) {
    .ant-picker-panel {
      background: #1a1a1a !important;
      border: 1px solid #2a2a2a !important;
    }

    .ant-picker-header {
      background: #252526 !important;
      border-bottom: 1px solid #2a2a2a !important;
      color: #d1d4dc !important;

      button {
        color: #787b86 !important;

        &:hover {
          color: #d1d4dc !important;
        }
      }
    }

    .ant-picker-content {
      th {
        color: #787b86 !important;
      }

      td {
        color: #d1d4dc !important;

        &.ant-picker-cell-in-view {
          color: #d1d4dc !important;
        }

        &.ant-picker-cell-disabled {
          color: #505053 !important;
        }

        &:hover .ant-picker-cell-inner {
          background: #2962ff !important;
        }

        &.ant-picker-cell-selected .ant-picker-cell-inner {
          background: #2962ff !important;
        }

        &.ant-picker-cell-today .ant-picker-cell-inner::before {
          border-color: #2962ff !important;
        }
      }
    }

    .ant-picker-footer {
      background: #252526 !important;
      border-top: 1px solid #2a2a2a !important;

      a {
        color: #2962ff !important;
      }
    }

    .ant-picker-time-panel {
      background: #1a1a1a !important;
      border-left: 1px solid #2a2a2a !important;

      .ant-picker-content {
        ul {
          &::-webkit-scrollbar {
            width: 4px;
          }

          &::-webkit-scrollbar-thumb {
            background: #3e3e42;
            border-radius: 2px;
          }
        }

        li {
          color: #d1d4dc !important;

          &:hover {
            background: #2962ff !important;
          }

          &.ant-picker-time-panel-cell-selected {
            background: #2962ff !important;
          }
        }
      }
    }
  }
}

// 深色空状态
.dark-empty {
  &:deep(.ant-empty-description) {
    color: #787b86;
  }
}

// 深色模态框
:global(.dark-modal .ant-modal-content) {
  background: #1a1a1a !important;
  border: 1px solid #2a2a2a !important;
  border-radius: 12px !important;
}

:global(.dark-modal .ant-modal-header) {
  background: #1a1a1a !important;
  border-bottom: 1px solid #2a2a2a !important;
  border-radius: 12px 12px 0 0 !important;
}

:global(.dark-modal .ant-modal-title) {
  color: #d1d4dc !important;
  font-weight: 600 !important;
}

:global(.dark-modal .ant-modal-close) {
  color: #787b86 !important;

  &:hover {
    color: #d1d4dc !important;
    background: rgba(255, 255, 255, 0.05) !important;
  }
}

:global(.dark-modal .ant-modal-body) {
  color: #d1d4dc !important;
}

:global(.dark-modal .ant-modal-footer) {
  background: #1a1a1a !important;
  border-top: 1px solid #2a2a2a !important;
  border-radius: 0 0 12px 12px !important;
}
</style>
