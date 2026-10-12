<template>
  <div class="result-table">
    <!-- 视图模板选择器 -->
    <a-card class="view-template-card dark-card" :bordered="false">
      <div class="view-template-wrapper">
        <div class="view-template-label">
          <LayoutOutlined class="icon" />
          <span>当前视图</span>
        </div>
        <a-dropdown :overlayClassName="'view-template-dropdown'">
          <a-button class="view-template-btn">
            {{ currentViewTemplate.name }}
            <DownOutlined />
          </a-button>
          <template #overlay>
            <a-menu class="dark-menu view-template-menu">
              <a-menu-item
                v-for="template in viewTemplates"
                :key="template.id"
                :class="{ active: currentView === template.id }"
                @click="switchViewTemplate(template.id)"
              >
                <div class="template-menu-item">
                  <component :is="template.icon" class="template-icon" />
                  <div class="template-info">
                    <div class="template-name">{{ template.name }}</div>
                    <div class="template-desc">{{ template.description }}</div>
                  </div>
                  <CheckOutlined v-if="currentView === template.id" class="check-icon" />
                </div>
              </a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
        <div class="view-template-stats">
          <span>显示 {{ displayColumns.length }} 列</span>
        </div>
      </div>
    </a-card>

    <!-- 统计信息 -->
    <a-card class="stats-card dark-card" :bordered="false">
      <a-statistic
        title="筛选结果"
        :value="total"
        suffix="只"
        style="margin-right: 32px"
        class="dark-statistic"
      >
        <template #prefix>
          <StockOutlined class="text-[#2962ff]" />
        </template>
      </a-statistic>

      <a-statistic
        title="当前页"
        :value="currentPage"
        class="dark-statistic"
        style="margin-right: 32px"
      >
        <template #suffix>
          <span style="font-size: 14px; color: #787b86;">/ {{ totalPages }}</span>
        </template>
      </a-statistic>

      <a-statistic
        title="数据日期"
        :value="date"
        class="dark-statistic"
      />

      <template #extra>
        <a-space>
          <a-button 
            type="primary" 
            :disabled="selectedRowKeys.length === 0"
            @click="onAddToWatchlist"
            class="bg-[#089981] border-[#089981] hover:bg-[#067a66]"
          >
            <PlusOutlined />
            加入自选 ({{ selectedRowKeys.length }})
          </a-button>
          <a-button @click="showColumnDrawer = true" class="dark-btn">
            <SettingOutlined />
            自定义列
          </a-button>
          <a-button @click="onRefresh" class="dark-btn">
            <ReloadOutlined />
            刷新
          </a-button>
          <a-button @click="onExport" class="dark-btn">
            <DownloadOutlined />
            导出
          </a-button>
        </a-space>
      </template>
    </a-card>

    <!-- 列设置抽屉 -->
    <a-drawer
      v-model:open="showColumnDrawer"
      title="自定义显示列"
      placement="right"
      width="400px"
      class="dark-drawer"
    >
      <div class="column-settings">
        <!-- 快捷操作 -->
        <div class="quick-actions">
          <a-button size="small" @click="selectAllColumns">全选</a-button>
          <a-button size="small" @click="clearAllColumns">清空</a-button>
          <a-button size="small" @click="resetToDefault">恢复默认</a-button>
        </div>

        <!-- 按分组显示列 -->
        <div class="column-groups">
          <div
            v-for="(group, groupIndex) in columnGroups"
            :key="groupIndex"
            class="column-group"
          >
            <div class="group-header" :style="{ borderLeftColor: group.color }" @click="toggleGroup(groupIndex)">
              <div class="group-title">
                <component :is="group.expanded ? DownOutlined : RightOutlined" class="expand-icon" />
                <span class="group-name">{{ group.name }}</span>
                <span class="group-count">({{ getSelectedCountInGroup(group) }}/{{ group.columns.length }})</span>
              </div>
              <a-checkbox
                :checked="isGroupFullySelected(group)"
                :indeterminate="isGroupPartiallySelected(group)"
                @click.stop
                @change="(e) => toggleGroupSelection(group, e.target.checked)"
              />
            </div>
            
            <div v-show="group.expanded" class="group-columns">
              <a-checkbox
                v-for="col in group.columns"
                :key="col.key"
                :checked="selectedColumnKeys.includes(col.key)"
                class="column-checkbox"
                @change="(e: any) => toggleColumnSelection(col.key, e.target.checked)"
              >
                {{ col.title }}
              </a-checkbox>
            </div>
          </div>
        </div>
      </div>
    </a-drawer>

    <!-- 数据表格 -->
    <a-card class="table-card dark-card" :bordered="false">
      <a-table
        :data-source="records"
        :columns="columnsWithSorter"
        :loading="loading"
        :pagination="false"
        :scroll="{ x: scrollX }"
        :row-selection="rowSelection"
        size="small"
        row-key="stock_code"
        class="dark-table"
        show-sorter-tooltip
        @change="onTableChange"
      >
        <!-- 股票代码列 - 合并显示股票名称和代码 -->
        <template #bodyCell="{ column, record, text }">
          <template v-if="column.key === 'stock_code'">
            <div class="stock-code-cell" @click="onStockClick(record)">
              <div class="stock-name">{{ record.stock_name || '-' }}</div>
              <div class="stock-code">{{ text }}</div>
            </div>
          </template>

          <template v-else-if="column.key === 'change_pct'">
            <span :class="getChangeClass(text)">
              {{ formatPercent(text) }}
            </span>
          </template>

          <template v-else-if="column.key === 'close' || column.key === 'open' || column.key === 'high' || column.key === 'low'">
            <span class="text-[#d1d4dc]">{{ formatPrice(text) }}</span>
          </template>

          <template v-else-if="column.key === 'volume' || column.key === 'amount'">
            <span class="text-[#d1d4dc]">{{ formatVolume(text) }}</span>
          </template>

          <template v-else-if="column.key === 'total_mv' || column.key === 'float_mv'">
            <span class="text-[#d1d4dc]">{{ formatMarketCap(text) }}</span>
          </template>

          <template v-else-if="column.key === 'pe' || column.key === 'pb' || column.key === 'ps'">
            <span class="text-[#d1d4dc]">{{ formatNumber(text, 2) }}</span>
          </template>

          <template v-else-if="column.key === 'turnover_rate'">
            <span class="text-[#d1d4dc]">{{ formatPercent(text) }}</span>
          </template>

          <template v-else-if="column.key === 'ma5' || column.key === 'ma10' || column.key === 'ma20'">
            <span class="text-[#d1d4dc]">{{ formatPrice(text) }}</span>
          </template>

          <template v-else-if="column.key === 'rsi_6' || column.key === 'rsi_12' || column.key === 'rsi_24'">
            <a-tag :color="getRSIColor(text)" class="dark-tag">{{ formatNumber(text, 2) }}</a-tag>
          </template>

          <template v-else-if="column.key === 'macd_bar'">
            <span :class="text > 0 ? 'up' : text < 0 ? 'down' : 'text-[#d1d4dc]'">
              {{ formatNumber(text, 4) }}
            </span>
          </template>

          <template v-else-if="column.key === 'ma_bull_alignment' || column.key === 'golden_cross' || column.key === 'death_cross' || column.key === 'macd_golden_cross' || column.key === 'macd_death_cross'">
            <a-tag :color="text ? 'success' : 'default'" class="dark-tag">
              {{ text ? '是' : '否' }}
            </a-tag>
          </template>

          <template v-else-if="column.key === 'corr_60d' || column.key === 'corr_120d' || column.key === 'corr_250d'">
            <span :class="getCorrelationColor(text)" class="font-medium">
              {{ formatCorrelation(text) }}
            </span>
          </template>

          <template v-else-if="column.key === 'beta_60d' || column.key === 'beta_120d' || column.key === 'beta_250d'">
            <span class="text-[#d1d4dc]">{{ formatNumber(text, 2) }}</span>
          </template>

          <template v-else-if="column.key === 'alpha_60d' || column.key === 'alpha_120d' || column.key === 'alpha_250d'">
            <span :class="text > 0 ? 'up' : text < 0 ? 'down' : 'text-[#d1d4dc]'">
              {{ formatNumber(text, 2) }}
            </span>
          </template>

          <template v-else-if="column.key === 'return_5d' || column.key === 'return_20d' || column.key === 'return_60d'">
            <span :class="getChangeClass(text)">
              {{ formatPercent(text) }}
            </span>
          </template>

          <template v-else-if="column.key === 'volatility_20d' || column.key === 'max_drawdown_20d'">
            <span class="text-[#d1d4dc]">{{ formatPercent(text) }}</span>
          </template>

          <template v-else>
            <span class="text-[#d1d4dc]">{{ formatNumber(text, 4) }}</span>
          </template>
        </template>
      </a-table>

      <!-- 分页 -->
      <div class="pagination-wrapper">
        <a-pagination
          :current="current"
          :page-size="pageSize"
          :total="total"
          :page-size-options="['20', '50', '100', '200']"
          show-size-changer
          show-quick-jumper
          :show-total="(total: number) => `共 ${total} 条`"
          @change="onPageChange"
          @show-size-change="onPageSizeChange"
          class="dark-pagination"
        />
      </div>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, markRaw } from 'vue'
import { request } from '../../services/api'
import {
  StockOutlined,
  ReloadOutlined,
  DownloadOutlined,
  PlusOutlined,
  LayoutOutlined,
  DownOutlined,
  CheckOutlined,
  DashboardOutlined,
  BarChartOutlined,
  LineChartOutlined,
  FundOutlined,
  EyeOutlined,
  SettingOutlined,
  RightOutlined
} from '@ant-design/icons-vue'
import type { TableColumnType } from 'ant-design-vue'
import { message } from 'ant-design-vue'

// 列分组定义
interface ColumnGroup {
  name: string
  color: string
  expanded: boolean
  columns: { key: string; title: string }[]
}

interface Props {
  records: any[]
  total: number
  currentPage: number
  pageSize: number
  totalPages: number
  date: string
  loading: boolean
  columns?: string[]
}

const props = defineProps<Props>()
const emit = defineEmits<{
  'update:currentPage': [page: number]
  'update:pageSize': [size: number]
  'refresh': []
  'export': []
  'stockClick': [record: any]
  'sortChange': [sort: { field: string; direction: 'asc' | 'desc' } | null]
}>()

const selectedRowKeys = ref<string[]>([])

const rowSelection = computed(() => ({
  selectedRowKeys: selectedRowKeys.value,
  onChange: (keys: string[]) => {
    selectedRowKeys.value = keys
  }
}))

// ============ 排序状态 ============
interface SortState {
  columnKey: string | null
  order: 'descend' | 'ascend' | null
}

const sortState = ref<SortState>({
  columnKey: null,
  order: null
})

// 将 ant-design 的排序方向转换为后端需要的格式
const directionMap: Record<string, 'asc' | 'desc'> = {
  'ascend': 'asc',
  'descend': 'desc'
}

// ============ 响应式数据 ============
const current = ref(props.currentPage)
const pageSize = ref(props.pageSize)
const currentView = ref('default')
const showColumnDrawer = ref(false)
const selectedColumnKeys = ref<string[]>([])

// 默认显示的列
const defaultVisibleColumns = [
  'stock_code', 'close', 'change_pct',
  'amount', 'turnover_rate', 'total_mv'
]

// 列分组配置
const columnGroups = ref<ColumnGroup[]>([
  {
    name: '基本信息',
    color: '#2962ff',
    expanded: true,
    columns: [
      { key: 'stock_code', title: '股票' }
    ]
  },
  {
    name: '价格数据',
    color: '#089981',
    expanded: true,
    columns: [
      { key: 'open', title: '开盘价' },
      { key: 'high', title: '最高价' },
      { key: 'low', title: '最低价' },
      { key: 'close', title: '收盘价' },
      { key: 'change_pct', title: '涨跌幅' }
    ]
  },
  {
    name: '成交数据',
    color: '#f59e0b',
    expanded: true,
    columns: [
      { key: 'volume', title: '成交量' },
      { key: 'amount', title: '成交额' },
      { key: 'turnover_rate', title: '换手率' }
    ]
  },
  {
    name: '市值估值',
    color: '#8b5cf6',
    expanded: false,
    columns: [
      { key: 'total_mv', title: '总市值' },
      { key: 'float_mv', title: '流通市值' },
      { key: 'pe', title: '市盈率' },
      { key: 'pb', title: '市净率' }
    ]
  },
  {
    name: '均线指标',
    color: '#2962ff',
    expanded: false,
    columns: [
      { key: 'ma5', title: 'MA5' },
      { key: 'ma10', title: 'MA10' },
      { key: 'ma20', title: 'MA20' },
      { key: 'ma60', title: 'MA60' }
    ]
  },
  {
    name: '动量指标',
    color: '#ec4899',
    expanded: false,
    columns: [
      { key: 'rsi_6', title: 'RSI(6)' },
      { key: 'rsi_12', title: 'RSI(12)' },
      { key: 'rsi_24', title: 'RSI(24)' },
      { key: 'macd_dif', title: 'MACD' },
      { key: 'macd_dea', title: 'MACD信号' },
      { key: 'macd_bar', title: 'MACD柱' },
      { key: 'kdj_k', title: 'KDJ K' },
      { key: 'kdj_d', title: 'KDJ D' },
      { key: 'kdj_j', title: 'KDJ J' }
    ]
  },
  {
    name: '布林带',
    color: '#8b5cf6',
    expanded: false,
    columns: [
      { key: 'boll_upper', title: '布林上轨' },
      { key: 'boll_mid', title: '布林中轨' },
      { key: 'boll_lower', title: '布林下轨' }
    ]
  },
  {
    name: '波动指标',
    color: '#f23645',
    expanded: false,
    columns: [
      { key: 'atr_14', title: 'ATR(14)' },
      { key: 'bias_6', title: 'BIAS(6)' },
      { key: 'bias_12', title: 'BIAS(12)' },
      { key: 'bias_24', title: 'BIAS(24)' }
    ]
  },
  {
    name: '收益表现',
    color: '#f59e0b',
    expanded: false,
    columns: [
      { key: 'return_5d', title: '5日收益' },
      { key: 'return_20d', title: '20日收益' },
      { key: 'return_60d', title: '60日收益' }
    ]
  },
  {
    name: '风险指标',
    color: '#ec4899',
    expanded: false,
    columns: [
      { key: 'volatility_20d', title: '20日波动率' },
      { key: 'max_drawdown_20d', title: '最大回撤(20)' }
    ]
  },
  {
    name: '统计指标',
    color: '#2962ff',
    expanded: false,
    columns: [
      { key: 'beta_60d', title: 'Beta(60)' },
      { key: 'beta_120d', title: 'Beta(120)' },
      { key: 'beta_250d', title: 'Beta(250)' },
      { key: 'alpha_60d', title: 'Alpha(60)' },
      { key: 'alpha_120d', title: 'Alpha(120)' },
      { key: 'alpha_250d', title: 'Alpha(250)' },
      { key: 'corr_60d', title: '相关系数(60)' },
      { key: 'corr_120d', title: '相关系数(120)' },
      { key: 'corr_250d', title: '相关系数(250)' }
    ]
  }
])

// ============ 视图模板定义 ============
interface ViewTemplate {
  id: string
  name: string
  description: string
  icon: any
  columns: string[]
}

const viewTemplates: ViewTemplate[] = [
  {
    id: 'default',
    name: '默认基础视图',
    description: '常用基础指标，适合快速浏览',
    icon: markRaw(DashboardOutlined),
    columns: ['stock_code', 'close', 'change_pct',
              'amount', 'turnover_rate', 'total_mv']
  },
  {
    id: 'fundamental',
    name: '基本面视图',
    description: '估值指标，适合价值投资',
    icon: markRaw(FundOutlined),
    columns: ['stock_code', 'close', 'change_pct', 'total_mv', 'float_mv',
              'pe', 'pb', 'ps', 'turnover_rate', 'volume']
  },
  {
    id: 'volume_price',
    name: '量价视图',
    description: '成交量价指标，适合短线交易',
    icon: markRaw(BarChartOutlined),
    columns: ['stock_code', 'close', 'change_pct', 'volume', 'amount',
              'turnover_rate', 'return_5d', 'return_20d', 'volatility_20d']
  },
  {
    id: 'technical',
    name: 'RSI/MACD技术视图',
    description: '技术指标，适合技术分析',
    icon: markRaw(LineChartOutlined),
    columns: ['stock_code', 'close', 'change_pct', 'ma5', 'ma10', 'ma20',
              'rsi_6', 'rsi_12', 'rsi_24', 'macd_dif', 'macd_dea', 'macd_bar', 'kdj_j']
  },
  {
    id: 'trend',
    name: '趋势跟踪视图',
    description: '趋势指标，适合趋势交易',
    icon: markRaw(LineChartOutlined),
    columns: ['stock_code', 'close', 'change_pct', 'ma5', 'ma10', 'ma20', 'ma60',
              'return_5d', 'return_20d', 'return_60d', 'beta_60d', 'alpha_60d']
  },
  {
    id: 'risk',
    name: '风险评估视图',
    description: '风险指标，适合风控分析',
    icon: markRaw(DashboardOutlined),
    columns: ['stock_code', 'close', 'change_pct', 'volatility_20d',
              'max_drawdown_20d', 'beta_60d', 'beta_120d', 'corr_60d']
  },
  {
    id: 'full',
    name: '全景视图',
    description: '显示所有指标',
    icon: markRaw(EyeOutlined),
    columns: ['*']
  }
]

const currentViewTemplate = computed(() => {
  return viewTemplates.find(t => t.id === currentView.value) || viewTemplates[0]
})

// ============ 列定义 ============
const defaultColumns: TableColumnType[] = [
  { title: '股票', dataIndex: 'stock_code', key: 'stock_code', width: 140, align: 'left', fixed: 'left' },
  { title: '开盘价', dataIndex: 'open', key: 'open', width: 90, align: 'right' },
  { title: '最高价', dataIndex: 'high', key: 'high', width: 90, align: 'right' },
  { title: '最低价', dataIndex: 'low', key: 'low', width: 90, align: 'right' },
  { title: '收盘价', dataIndex: 'close', key: 'close', width: 90, align: 'right' },
  { title: '涨跌幅', dataIndex: 'change_pct', key: 'change_pct', width: 90, align: 'right' },
  { title: '成交量', dataIndex: 'volume', key: 'volume', width: 100, align: 'right' },
  { title: '成交额', dataIndex: 'amount', key: 'amount', width: 110, align: 'right' },
  { title: '换手率', dataIndex: 'turnover_rate', key: 'turnover_rate', width: 90, align: 'right' },
  { title: '总市值', dataIndex: 'total_mv', key: 'total_mv', width: 110, align: 'right' },
]

const extendedColumns: TableColumnType[] = [
  { title: '流通市值', dataIndex: 'float_mv', key: 'float_mv', width: 110, align: 'right' },
  { title: '市盈率', dataIndex: 'pe', key: 'pe', width: 80, align: 'right' },
  { title: '市净率', dataIndex: 'pb', key: 'pb', width: 80, align: 'right' },
  { title: 'MA5', dataIndex: 'ma5', key: 'ma5', width: 90, align: 'right' },
  { title: 'MA10', dataIndex: 'ma10', key: 'ma10', width: 90, align: 'right' },
  { title: 'MA20', dataIndex: 'ma20', key: 'ma20', width: 90, align: 'right' },
  { title: 'MA60', dataIndex: 'ma60', key: 'ma60', width: 90, align: 'right' },
  { title: 'RSI(6)', dataIndex: 'rsi_6', key: 'rsi_6', width: 80, align: 'right' },
  { title: 'RSI(12)', dataIndex: 'rsi_12', key: 'rsi_12', width: 80, align: 'right' },
  { title: 'RSI(24)', dataIndex: 'rsi_24', key: 'rsi_24', width: 80, align: 'right' },
  { title: 'MACD', dataIndex: 'macd_dif', key: 'macd_dif', width: 90, align: 'right' },
  { title: 'MACD信号', dataIndex: 'macd_dea', key: 'macd_dea', width: 90, align: 'right' },
  { title: 'MACD柱', dataIndex: 'macd_bar', key: 'macd_bar', width: 90, align: 'right' },
  { title: 'KDJ K', dataIndex: 'kdj_k', key: 'kdj_k', width: 80, align: 'right' },
  { title: 'KDJ D', dataIndex: 'kdj_d', key: 'kdj_d', width: 80, align: 'right' },
  { title: 'KDJ J', dataIndex: 'kdj_j', key: 'kdj_j', width: 80, align: 'right' },
  { title: '布林上轨', dataIndex: 'boll_upper', key: 'boll_upper', width: 90, align: 'right' },
  { title: '布林中轨', dataIndex: 'boll_mid', key: 'boll_mid', width: 90, align: 'right' },
  { title: '布林下轨', dataIndex: 'boll_lower', key: 'boll_lower', width: 90, align: 'right' },
  { title: 'ATR(14)', dataIndex: 'atr_14', key: 'atr_14', width: 90, align: 'right' },
  { title: 'BIAS(6)', dataIndex: 'bias_6', key: 'bias_6', width: 80, align: 'right' },
  { title: 'BIAS(12)', dataIndex: 'bias_12', key: 'bias_12', width: 80, align: 'right' },
  { title: 'BIAS(24)', dataIndex: 'bias_24', key: 'bias_24', width: 80, align: 'right' },
  { title: '5日收益', dataIndex: 'return_5d', key: 'return_5d', width: 90, align: 'right' },
  { title: '20日收益', dataIndex: 'return_20d', key: 'return_20d', width: 90, align: 'right' },
  { title: '60日收益', dataIndex: 'return_60d', key: 'return_60d', width: 90, align: 'right' },
  { title: '20日波动率', dataIndex: 'volatility_20d', key: 'volatility_20d', width: 100, align: 'right' },
  { title: '最大回撤(20)', dataIndex: 'max_drawdown_20d', key: 'max_drawdown_20d', width: 110, align: 'right' },
  { title: 'Beta(60)', dataIndex: 'beta_60d', key: 'beta_60d', width: 90, align: 'right' },
  { title: 'Beta(120)', dataIndex: 'beta_120d', key: 'beta_120d', width: 90, align: 'right' },
  { title: 'Beta(250)', dataIndex: 'beta_250d', key: 'beta_250d', width: 90, align: 'right' },
  { title: 'Alpha(60)', dataIndex: 'alpha_60d', key: 'alpha_60d', width: 90, align: 'right' },
  { title: 'Alpha(120)', dataIndex: 'alpha_120d', key: 'alpha_120d', width: 90, align: 'right' },
  { title: 'Alpha(250)', dataIndex: 'alpha_250d', key: 'alpha_250d', width: 90, align: 'right' },
  { title: '相关系数(60)', dataIndex: 'corr_60d', key: 'corr_60d', width: 100, align: 'right' },
  { title: '相关系数(120)', dataIndex: 'corr_120d', key: 'corr_120d', width: 100, align: 'right' },
  { title: '相关系数(250)', dataIndex: 'corr_250d', key: 'corr_250d', width: 100, align: 'right' },
]

const allColumns = computed(() => [...defaultColumns, ...extendedColumns])

const displayColumns = computed(() => {
  const template = currentViewTemplate.value

  // 全景视图显示所有列
  if (template.columns.includes('*')) {
    return allColumns.value
  }

  // 如果用户自定义了列，使用用户选择的列
  if (selectedColumnKeys.value.length > 0) {
    return allColumns.value.filter(col => selectedColumnKeys.value.includes(col.key as string))
  }

  // 根据视图模板的列配置过滤
  return allColumns.value.filter(col => template.columns.includes(col.key as string))
})

// 带排序功能的列定义
const columnsWithSorter = computed(() => {
  return displayColumns.value.map(col => ({
    ...col,
    sorter: col.key !== 'stock_code', // 股票代码列不排序，其他列都支持排序
    sortOrder: sortState.value.columnKey === col.key ? sortState.value.order : undefined
  }))
})

// 计算表格横向滚动宽度（所有列宽之和）
const scrollX = computed(() => {
  const columns = displayColumns.value
  return columns.reduce((total, col) => total + (col.width || 100), 0)
})

// ============ 方法 ============
function formatNumber(val: number | null, precision: number = 2): string {
  if (val === null || val === undefined || isNaN(val)) return '-'
  return val.toFixed(precision)
}

function formatPrice(val: number | null): string {
  if (val === null || val === undefined) return '-'
  return val.toFixed(2)
}

function formatPercent(val: number | null): string {
  if (val === null || val === undefined) return '-'
  const sign = val > 0 ? '+' : ''
  return `${sign}${val.toFixed(2)}%`
}

function formatVolume(val: number | null): string {
  if (val === null || val === undefined) return '-'
  if (val >= 100000000) {
    return (val / 100000000).toFixed(2) + '亿'
  } else if (val >= 10000) {
    return (val / 10000).toFixed(2) + '万'
  }
  return val.toFixed(0)
}

function formatMoney(val: number | null): string {
  if (val === null || val === undefined) return '-'
  if (val >= 100000000) {
    return (val / 100000000).toFixed(2) + '亿'
  } else if (val >= 10000) {
    return (val / 10000).toFixed(2) + '万'
  }
  return val.toFixed(2)
}

function formatMarketCap(val: number | null): string {
  if (val === null || val === undefined) return '-'
  const yuan = val * 10000
  if (yuan >= 100000000) {
    return (yuan / 100000000).toFixed(2) + '亿'
  } else if (yuan >= 10000) {
    return (yuan / 10000).toFixed(2) + '万'
  }
  return yuan.toFixed(2)
}

function getChangeClass(val: number | null): string {
  if (val === null || val === undefined) return 'text-[#d1d4dc]'
  if (val > 0) return 'up'
  if (val < 0) return 'down'
  return 'text-[#d1d4dc]'
}

function getRSIColor(val: number | null): string {
  if (val === null || val === undefined) return 'default'
  if (val >= 70) return 'red'
  if (val <= 30) return 'green'
  return 'default'
}

function getCorrelationColor(val: number | null): string {
  if (val === null || val === undefined) return 'text-[#d1d4dc]'
  // 相关系数：强正相关(>0.7)绿色，强负相关(<-0.7)红色
  if (val >= 0.7) return 'text-green-500'
  if (val <= -0.7) return 'text-red-500'
  return 'text-[#d1d4dc]'
}

function formatCorrelation(val: number | null): string {
  if (val === null || val === undefined) return '-'
  return val.toFixed(4)
}

function onPageChange(page: number) {
  current.value = page
  emit('update:currentPage', page)
}

/**
 * 处理表格排序变化
 * 实现三态排序：第一次点击降序 -> 第二次点击升序 -> 第三次点击恢复默认
 * 触发后端重新排序并获取数据
 */
function onTableChange(_pagination: any, _filters: any, sorter: any) {
  if (!sorter || !sorter.columnKey) {
    sortState.value = { columnKey: null, order: null }
    emit('sortChange', null)
    return
  }

  const { columnKey, order } = sorter

  // 如果点击的是当前排序列
  if (sortState.value.columnKey === columnKey) {
    // 当前是降序 -> 切换到升序
    if (sortState.value.order === 'descend') {
      sortState.value.order = 'ascend'
    }
    // 当前是升序 -> 恢复默认（无排序）
    else if (sortState.value.order === 'ascend') {
      sortState.value.columnKey = null
      sortState.value.order = null
      emit('sortChange', null)
      return
    }
  } else {
    // 点击新列，第一次点击为降序（从大到小）
    sortState.value.columnKey = columnKey
    sortState.value.order = 'descend'
  }

  // 通知父组件排序变化，触发后端重新查询
  emit('sortChange', {
    field: columnKey,
    direction: directionMap[sortState.value.order!]
  })
}

function onPageSizeChange(_current: number, size: number) {
  pageSize.value = size
  current.value = 1
  emit('update:pageSize', size)
}

function switchViewTemplate(templateId: string) {
  currentView.value = templateId
  // 切换视图时重置用户自定义列选择
  selectedColumnKeys.value = []
}

// ============ 列设置方法 ============
function toggleGroup(groupIndex: number) {
  columnGroups.value[groupIndex].expanded = !columnGroups.value[groupIndex].expanded
}

function getSelectedCountInGroup(group: ColumnGroup): number {
  return group.columns.filter(col => selectedColumnKeys.value.includes(col.key)).length
}

function isGroupFullySelected(group: ColumnGroup): boolean {
  return group.columns.every(col => selectedColumnKeys.value.includes(col.key))
}

function isGroupPartiallySelected(group: ColumnGroup): boolean {
  const selectedCount = getSelectedCountInGroup(group)
  return selectedCount > 0 && selectedCount < group.columns.length
}

function toggleGroupSelection(group: ColumnGroup, checked: boolean) {
  const groupKeys = group.columns.map(col => col.key)
  if (checked) {
    selectedColumnKeys.value = [...new Set([...selectedColumnKeys.value, ...groupKeys])]
  } else {
    selectedColumnKeys.value = selectedColumnKeys.value.filter(key => !groupKeys.includes(key))
  }
}

function toggleColumnSelection(colKey: string, checked: boolean) {
  if (checked) {
    if (!selectedColumnKeys.value.includes(colKey)) {
      selectedColumnKeys.value = [...selectedColumnKeys.value, colKey]
    }
  } else {
    selectedColumnKeys.value = selectedColumnKeys.value.filter(key => key !== colKey)
  }
}

function selectAllColumns() {
  const allKeys = columnGroups.value.flatMap(g => g.columns.map(c => c.key))
  selectedColumnKeys.value = allKeys
}

function clearAllColumns() {
  selectedColumnKeys.value = []
}

function resetToDefault() {
  selectedColumnKeys.value = [...defaultVisibleColumns]
}

// 初始化默认选中列
watch(() => showColumnDrawer.value, (visible) => {
  if (visible && selectedColumnKeys.value.length === 0) {
    // 打开抽屉时，如果没有自定义选择，使用当前显示的列
    const currentKeys = displayColumns.value.map(col => col.key as string)
    selectedColumnKeys.value = currentKeys
  }
})

function onRefresh() {
  emit('refresh')
}

function onExport() {
  emit('export')
}

function onStockClick(record: any) {
  emit('stockClick', record)
}

async function onAddToWatchlist() {
  if (selectedRowKeys.value.length === 0) {
    message.warning('请先选择要加入自选的股票')
    return
  }
  
  const selectedRecords = props.records.filter(r => selectedRowKeys.value.includes(r.stock_code))
  
  const items = selectedRecords.map(r => ({
    stock_code: r.stock_code,
    stock_name: r.stock_name || r.name || r.stock_code,
    conditions: [],
    notes: '',
    tags: [],
    is_active: true,
    feishu_notify: true
  }))
  
  try {
    const res = await request<{ added_count: number }>('/portfolio/watchlist/batch', {
      method: 'POST',
      body: JSON.stringify({ items })
    })
    
    if (res.ok) {
      message.success(`成功添加 ${res.data.added_count} 只股票到自选列表`)
      selectedRowKeys.value = []
    } else {
      message.error(res.message || '添加失败')
    }
  } catch (error: any) {
    message.error('添加失败: ' + error.message)
  }
}

// ============ 监听 ============
watch(() => props.currentPage, (newPage) => {
  current.value = newPage
})

watch(() => props.pageSize, (newSize) => {
  pageSize.value = newSize
})
</script>

<style scoped lang="scss">
.result-table {
  // 视图模板卡片
  .view-template-card {
    margin-bottom: 12px;

    &:deep(.ant-card-body) {
      padding: 10px 14px;
    }

    .view-template-wrapper {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .view-template-label {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      color: #787b86;

      .icon {
        font-size: 14px;
        color: #2962ff;
      }
    }

    .view-template-btn {
      background: #252526;
      border: 1px solid #3e3e42;
      color: #d1d4dc;
      border-radius: 6px;
      font-size: 13px;
      padding: 4px 12px;
      display: flex;
      align-items: center;
      gap: 8px;

      &:hover {
        border-color: #2962ff;
        color: #fff;
      }
    }

    .view-template-stats {
      margin-left: auto;
      font-size: 12px;
      color: #505053;
    }
  }

  .stats-card {
    margin-bottom: 12px;

    &:deep(.ant-card-body) {
      display: flex;
      align-items: flex-end;
      padding: 12px 16px;
    }
  }

  .table-card {
    position: relative;
    overflow-x: auto;
    overflow-y: hidden;
    
    &:deep(.ant-table-wrapper) {
      overflow-x: visible;
      overflow-y: hidden;
    }
    
    &:deep(.ant-table) {
      width: auto;
    }
    
    &:deep(.ant-table-container) {
      overflow-x: visible !important;
      overflow-y: hidden !important;
    }
    
    &:deep(.ant-table-cell) {
      padding: 8px 12px;
    }
  }

  .pagination-wrapper {
    margin-top: 16px;
    display: flex;
    justify-content: flex-end;
  }

  .stock-code-cell {
    cursor: pointer;
    padding: 4px 0;

    &:hover .stock-name {
      color: #2962ff;
    }

    .stock-name {
      font-weight: 600;
      font-size: 14px;
      color: #ffffff;
      line-height: 1.4;
    }

    .stock-code {
      font-size: 12px;
      color: #787b86;
      text-align: left;
      line-height: 1.3;
    }
  }

  .up {
    color: #f23645;
  }

  .down {
    color: #089981;
  }
}

// 深色卡片
.dark-card {
  background: #1a1a1a !important;
  border: 1px solid #2a2a2a !important;
  border-radius: 8px;

  &:deep(.ant-card-head) {
    background: transparent !important;
    border-bottom: 1px solid #2a2a2a !important;
  }

  &:deep(.ant-card-body) {
    background: transparent !important;
  }
}

// 深色按钮
.dark-btn {
  background: #252526 !important;
  border: 1px solid #3e3e42 !important;
  color: #d1d4dc !important;

  &:hover {
    background: #2d2d30 !important;
    border-color: #505053 !important;
    color: #fff !important;
  }
}

// 深色统计
.dark-statistic {
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  height: 100%;

  &:deep(.ant-statistic-title) {
    color: #787b86 !important;
    font-size: 12px;
    line-height: 20px;
    margin-bottom: 0;
  }

  &:deep(.ant-statistic-content) {
    color: #d1d4dc !important;
    font-size: 20px;
    line-height: 28px;
    display: flex;
    align-items: center;
  }

  &:deep(.ant-statistic-content-prefix) {
    display: inline-flex;
    align-items: center;
    margin-right: 4px;
  }

  &:deep(.ant-statistic-content-value) {
    display: inline-flex;
    align-items: center;
  }

  &:deep(.ant-statistic-content-suffix) {
    display: inline-flex;
    align-items: center;
    margin-left: 4px;
  }
}

// 深色表格
.dark-table {
  &:deep(.ant-table) {
    background: transparent;
  }

  &:deep(.ant-table-thead > tr > th) {
    background: #252526 !important;
    border-bottom: 1px solid #2a2a2a !important;
    color: #d1d4dc !important;
    font-weight: 500;
    font-size: 12px;
    padding: 10px 8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: pointer;

    // 排序图标样式
    .ant-table-column-sorter {
      color: #505053;

      &-up,
      &-down {
        &.active {
          color: #2962ff;
        }
      }
    }

    &:hover .ant-table-column-sorter {
      color: #787b86;
    }
  }

  &:deep(.ant-table-tbody > tr > td) {
    border-bottom: 1px solid #2a2a2a !important;
    padding: 8px;
    font-size: 13px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  &:deep(.ant-table-tbody > tr:hover > td) {
    background: #252526 !important;
  }

  &:deep(.ant-table-cell) {
    color: #d1d4dc;
  }

  &:deep(.ant-table-row-selected > td) {
    background: rgba(41, 98, 255, 0.1) !important;
  }

  &:deep(.ant-checkbox-inner) {
    background: #252526;
    border-color: #3e3e42;
  }

  &:deep(.ant-checkbox-checked .ant-checkbox-inner) {
    background: #2962ff;
    border-color: #2962ff;
  }

  // 固定列样式
  &:deep(.ant-table-cell-fix-left) {
    background: #1a1a1a !important;
  }

  &:deep(.ant-table-cell-fix-left::after) {
    box-shadow: inset 10px 0 8px -8px rgba(0, 0, 0, 0.5);
  }
}

// 深色标签
.dark-tag {
  background: rgba(41, 98, 255, 0.15) !important;
  border: 1px solid rgba(41, 98, 255, 0.3) !important;
  color: #2962ff !important;
}

// 列设置抽屉样式
.dark-drawer {
  :deep(.ant-drawer-header) {
    background: #1a1a1a;
    border-bottom: 1px solid #2a2a2a;
    
    .ant-drawer-title {
      color: #d1d4dc;
      font-weight: 500;
    }
    
    .ant-drawer-close {
      color: #787b86;
      
      &:hover {
        color: #d1d4dc;
      }
    }
  }
  
  :deep(.ant-drawer-body) {
    background: #0d1117;
    padding: 16px;
  }
}

.column-settings {
  .quick-actions {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    padding-bottom: 16px;
    border-bottom: 1px solid #2a2a2a;
    
    .ant-btn {
      background: #252526;
      border-color: #3e3e42;
      color: #d1d4dc;
      
      &:hover {
        border-color: #2962ff;
        color: #2962ff;
      }
    }
  }
  
  .column-groups {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  
  .column-group {
    background: #1a1a1a;
    border-radius: 8px;
    overflow: hidden;
  }
  
  .group-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 12px;
    background: #1a1a1a;
    border-left: 3px solid;
    cursor: pointer;
    transition: background 0.2s;
    
    &:hover {
      background: #252526;
    }
    
    .group-title {
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 1;
      
      .expand-icon {
        font-size: 12px;
        color: #787b86;
        transition: transform 0.2s;
      }
      
      .group-name {
        font-size: 13px;
        font-weight: 500;
        color: #d1d4dc;
      }
      
      .group-count {
        font-size: 11px;
        color: #787b86;
      }
    }
    
    :deep(.ant-checkbox) {
      .ant-checkbox-inner {
        background: #252526;
        border-color: #3e3e42;
      }
      
      &.ant-checkbox-checked .ant-checkbox-inner {
        background: #2962ff;
        border-color: #2962ff;
      }
    }
  }
  
  .group-columns {
    padding: 8px 12px 12px 36px;
    background: #0d1117;
  }
  
  .column-checkbox-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  
  .column-checkbox {
    :deep(.ant-checkbox) {
      .ant-checkbox-inner {
        background: #252526;
        border-color: #3e3e42;
      }
      
      &.ant-checkbox-checked .ant-checkbox-inner {
        background: #2962ff;
        border-color: #2962ff;
      }
    }
    
    :deep(.ant-checkbox + span) {
      color: #d1d4dc;
      font-size: 12px;
    }
    
    &:hover :deep(.ant-checkbox + span) {
      color: #fff;
    }
  }
}

// 视图模板下拉菜单容器
:global(.view-template-dropdown) {
  .ant-dropdown-menu {
    min-width: 320px !important;
    background: #1a1a1a !important;
    border: 1px solid #2a2a2a !important;
    border-radius: 8px !important;
    padding: 4px !important;
  }
}

// 视图模板菜单
.view-template-menu {
  min-width: 320px;
  background: #1a1a1a !important;
  border: 1px solid #2a2a2a !important;
  border-radius: 8px !important;
  padding: 4px !important;

  .ant-dropdown-menu-item {
    padding: 8px 12px !important;
    border-radius: 6px !important;
    margin-bottom: 2px;

    &:hover {
      background: #252526 !important;
    }

    &.active {
      background: rgba(41, 98, 255, 0.1) !important;
    }
  }

  .template-menu-item {
    display: flex;
    align-items: center;
    gap: 10px;

    .template-icon {
      font-size: 18px;
      color: #2962ff;
      flex-shrink: 0;
    }

    .template-info {
      flex: 1;
      min-width: 0;

      .template-name {
        font-size: 13px;
        color: #d1d4dc;
        font-weight: 500;
      }

      .template-desc {
        font-size: 11px;
        color: #787b86;
        margin-top: 2px;
      }
    }

    .check-icon {
      font-size: 14px;
      color: #2962ff;
      flex-shrink: 0;
    }
  }
}

// 深色分页
.dark-pagination {
  &:deep(.ant-pagination-item) {
    background: #252526;
    border-color: #3e3e42;

    a {
      color: #787b86;
    }

    &:hover {
      border-color: #505053;

      a {
        color: #d1d4dc;
      }
    }

    &.ant-pagination-item-active {
      background: #2962ff;
      border-color: #2962ff;

      a {
        color: #fff;
      }
    }
  }

  &:deep(.ant-pagination-prev),
  &:deep(.ant-pagination-next) {
    button {
      background: #252526;
      border-color: #3e3e42;
      color: #787b86;

      &:hover {
        border-color: #505053;
        color: #d1d4dc;
      }
    }
  }

  &:deep(.ant-pagination-options) {
    .ant-select-selector {
      background: #252526 !important;
      border-color: #3e3e42 !important;
      color: #d1d4dc !important;
    }

    .ant-select-arrow {
      color: #787b86;
    }

    .ant-pagination-options-quick-jumper {
      color: #787b86;

      input {
        background: #252526;
        border-color: #3e3e42;
        color: #d1d4dc;

        &:focus {
          border-color: #2962ff;
        }
      }
    }
  }
}
</style>
