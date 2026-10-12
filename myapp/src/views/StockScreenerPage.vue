<template>
  <div class="stock-screener-page">
    <DebugButton />
    <!-- Header -->
    <div class="page-header">
      <div class="flex items-center gap-3">
        <div class="w-1 h-8 bg-gradient-to-b from-blue-500 to-blue-600 rounded-full"></div>
        <div>
          <h1 class="text-2xl font-bold text-white tracking-tight">股票量化筛选器</h1>
          <p class="text-sm text-[#787b86] mt-1">基于多维度技术指标的智能选股工具</p>
        </div>
      </div>
      <button
        @click="showHelp = true"
        class="btn-enhanced px-4 py-2 bg-[#252526] hover:bg-[#2d2d30] text-[#d1d4dc] rounded-lg transition-all duration-200 border border-[#3e3e42] hover:border-[#505053] flex items-center gap-2"
      >
        <QuestionCircleOutlined />
        <span>使用帮助</span>
      </button>
    </div>

    <div class="page-content">
      <ErrorState v-if="store.error" :message="store.error" @retry="onExecuteFilter" />
      <LoadingState v-else-if="store.loading && !store.filterResult" title="正在加载筛选数据" />
      <EmptyState
        v-else-if="store.filterResult && store.allRecords.length === 0"
        title="筛选结果为空"
        description="当前日期范围或筛选条件暂无数据"
      />
      <div class="screener-layout">
        <!-- 左侧筛选面板 - 自适应宽度 -->
        <div class="filter-panel-wrapper">
          <FilterPanel
            v-model:conditions="store.conditions"
            v-model:logic="store.logic"
            v-model:order-by="store.orderBy"
            v-model:selected-date="store.selectedDate"
            :categories="store.categories"
            :operators="store.operators"
            :trade-dates="store.tradeDates"
            :latest-date="store.latestDate"
            :field-stats="store.fieldStats"
            :templates="store.templates"
            :loading="store.loading"
            @add-condition="onAddCondition"
            @remove-condition="onRemoveCondition"
            @clear-conditions="onClearConditions"
            @execute-filter="onExecuteFilter"
            @save-template="onSaveTemplate"
            @load-template="onLoadTemplate"
            @delete-template="onDeleteTemplate"
            @load-field-stats="onLoadFieldStats"
          />
        </div>

        <!-- 右侧结果表格 - 自适应填充 -->
        <div class="result-table-wrapper">
          <ResultTable
            :current-page="store.currentPage"
            :page-size="store.pageSize"
            :records="store.paginatedRecords"
            :total="store.filterResult?.total || 0"
            :total-pages="Math.ceil((store.filterResult?.total || 0) / store.pageSize)"
            :date="store.filterResult?.date || store.selectedDate"
            :loading="store.loading"
            @update:current-page="store.setPage"
            @update:page-size="store.setPageSize"
            @refresh="onExecuteFilter"
            @export="onExport"
            @stock-click="onStockClick"
            @sort-change="onSortChange"
          />
        </div>
      </div>
    </div>

    <!-- 帮助弹窗 -->
    <a-modal
      v-model:open="showHelp"
      title="使用帮助"
      width="700px"
      :footer="null"
      :class="'dark-modal'"
    >
      <a-typography>
        <a-typography-title :level="4" class="text-white">快速开始</a-typography-title>
        <a-typography-paragraph class="text-[#d1d4dc]">
          <ol>
            <li>在左侧选择要筛选的<strong>交易日期</strong></li>
            <li>点击"添加条件"按钮添加筛选条件</li>
            <li>选择<strong>指标</strong>、<strong>运算符</strong>和<strong>数值</strong></li>
            <li>可添加多个条件，选择"全部满足"或"任一满足"</li>
            <li>点击"执行筛选"查看结果</li>
          </ol>
        </a-typography-paragraph>

        <a-typography-title :level="4" class="text-white">指标说明</a-typography-title>
        <a-typography-paragraph class="text-[#d1d4dc]">
          <a-collapse class="dark-collapse">
            <a-collapse-panel key="1" header="动量类指标">
              <ul>
                <li><strong>RSI</strong> - 相对强弱指标，70以上超买，30以下超卖</li>
                <li><strong>MACD</strong> - 指数平滑异同平均线，金叉买入信号，死叉卖出信号</li>
                <li><strong>KDJ</strong> - 随机指标，K、D、J三线组合判断超买超卖</li>
              </ul>
            </a-collapse-panel>
            <a-collapse-panel key="2" header="趋势类指标">
              <ul>
                <li><strong>MA</strong> - 移动平均线，判断趋势方向</li>
                <li><strong>EMA</strong> - 指数移动平均线，对近期价格更敏感</li>
                <li><strong>布林带</strong> - 判断价格波动区间和突破</li>
              </ul>
            </a-collapse-panel>
            <a-collapse-panel key="3" header="风险指标">
              <ul>
                <li><strong>Beta</strong> - 相对于市场的波动程度</li>
                <li><strong>Alpha</strong> - 超额收益能力</li>
                <li><strong>夏普比率</strong> - 风险调整后的收益</li>
                <li><strong>最大回撤</strong> - 一段时间内最大亏损幅度</li>
              </ul>
            </a-collapse-panel>
          </a-collapse>
        </a-typography-paragraph>

        <a-typography-title :level="4" class="text-white">模板功能</a-typography-title>
        <a-typography-paragraph class="text-[#d1d4dc]">
          可以将常用的筛选条件保存为模板，方便下次快速加载使用。
          点击"保存模板"按钮，输入模板名称即可保存当前筛选条件。
        </a-typography-paragraph>
      </a-typography>
    </a-modal>

    <!-- 股票K线弹窗 -->
    <StockKlineModal
      v-model:visible="showKlineModal"
      :stock-code="selectedStock?.stock_code || ''"
      :stock-name="selectedStock?.stock_name || ''"
      :current-price="selectedStock?.close || 0"
      :change-percent="selectedStock?.change_pct || 0"
      @close="onKlineModalClose"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { QuestionCircleOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { useScreenerStore } from '@/store/screenerStore'
import { FilterPanel, ResultTable } from '@/components/screener'
import StockKlineModal from '@/components/screener/StockKlineModal.vue'
import LoadingState from '@/components/common/LoadingState.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorState from '@/components/common/ErrorState.vue';
import DebugButton from '@/components/common/DebugButton.vue';
import type { FilterCondition } from '@/api/screener'

const router = useRouter()
const store = useScreenerStore()
const showHelp = ref(false)
const showKlineModal = ref(false)
const selectedStock = ref<any>(null)

// ============ 生命周期 ============
onMounted(async () => {
  // 初始化加载数据
  await store.loadIndicators()
  await store.loadTradeDates()
  store.loadTemplatesFromStorage()
  
  // 检查是否有交易日数据
  if (!store.tradeDates || store.tradeDates.length === 0) {
    message.warning('未获取到交易日数据，请检查数据服务是否正常')
  }
  
  // 自动执行筛选，显示最新交易日的所有数据
  await store.executeFilter()
})

// ============ 事件处理 ============
function onAddCondition() {
  const newCondition: FilterCondition = {
    field: '',
    operator: '',
    value: null
  }
  store.addCondition(newCondition)
}

function onRemoveCondition(index: number) {
  store.removeCondition(index)
}

function onClearConditions() {
  store.clearConditions()
}

async function onExecuteFilter() {
  await store.executeFilter()
}

function onSaveTemplate(name: string) {
  store.saveTemplate(name)
}

function onLoadTemplate(id: string) {
  store.loadTemplate(id)
}

function onDeleteTemplate(id: string) {
  store.deleteTemplate(id)
}

function onLoadFieldStats(field: string) {
  store.loadFieldStats(field)
}

function onExport() {
  store.exportResults()
}

function onStockClick(record: any) {
  selectedStock.value = record
  showKlineModal.value = true
}

function onKlineModalClose() {
  showKlineModal.value = false
  selectedStock.value = null
}

/**
 * 处理排序变化
 * 更新排序条件并重新执行筛选（后端排序）
 */
async function onSortChange(sort: { field: string; direction: 'asc' | 'desc' } | null) {
  if (sort) {
    // 设置新的排序条件
    store.setOrderBy([{ field: sort.field, direction: sort.direction }])
  } else {
    // 清除排序
    store.setOrderBy([])
  }
  // 重新执行筛选，后端会返回排序后的数据
  await store.executeFilter()
}
</script>

<style scoped lang="scss">
.stock-screener-page {
  min-height: 100vh;
  background: #0a0a0a;
  padding: 24px;

  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid #1a1a1a;
  }

  .page-content {
    padding: 0;
    overflow-x: visible;
  }

  // 新的弹性布局
  .screener-layout {
    display: flex;
    gap: 20px;
    min-height: calc(100vh - 140px);

    .filter-panel-wrapper {
      flex-shrink: 0;
      transition: all 0.35s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .result-table-wrapper {
      flex: 1;
      min-width: 0;
      overflow-x: auto;
      overflow-y: hidden;

      /* 自定义滚动条样式 - Webkit */
      &::-webkit-scrollbar {
        height: 10px;
        display: block;
      }

      &::-webkit-scrollbar-track {
        background: #2a2a2a;
        border-radius: 5px;
      }

      &::-webkit-scrollbar-thumb {
        background: #505053;
        border-radius: 5px;
        min-width: 50px;

        &:hover {
          background: #787b86;
        }
      }

      /* Firefox 滚动条 */
      scrollbar-width: auto;
      scrollbar-color: #505053 #2a2a2a;

      /* 确保内容可以超出容器 */
      & > * {
        min-width: fit-content;
      }
    }
  }
}

// 深色模态框样式
:global(.dark-modal .ant-modal-content) {
  background: #1a1a1a !important;
  border: 1px solid #2a2a2a;
}

:global(.dark-modal .ant-modal-header) {
  background: #1a1a1a !important;
  border-bottom: 1px solid #2a2a2a;
}

:global(.dark-modal .ant-modal-title) {
  color: #fff !important;
}

:global(.dark-modal .ant-modal-close) {
  color: #787b86 !important;
}

:global(.dark-modal .ant-modal-close:hover) {
  color: #fff !important;
}

// 深色折叠面板
:global(.dark-collapse) {
  background: transparent;
}

:global(.dark-collapse .ant-collapse-item) {
  background: #252526;
  border: 1px solid #3e3e42;
  margin-bottom: 8px;
  border-radius: 6px;
}

:global(.dark-collapse .ant-collapse-header) {
  color: #d1d4dc !important;
}

:global(.dark-collapse .ant-collapse-content) {
  background: #1a1a1a;
  border-top: 1px solid #3e3e42;
}

:global(.dark-collapse .ant-collapse-content-box) {
  color: #d1d4dc;
}
</style>
