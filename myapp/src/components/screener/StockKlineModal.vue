<template>
  <a-modal
    :open="visible"
    :title="modalTitle"
    :width="900"
    :footer="null"
    @cancel="onClose"
    class="stock-kline-modal dark-modal"
  >
    <div class="kline-modal-content">
      <!-- 股票信息头部 -->
      <div class="stock-header">
        <div class="stock-info">
          <span class="stock-code">{{ stockCode }}</span>
          <span class="stock-name">{{ stockName }}</span>
        </div>
        <div class="stock-price" :class="priceChangeClass">
          <span class="current-price">{{ formatPrice(currentPrice) }}</span>
          <span class="price-change">
            {{ changePercent >= 0 ? '+' : '' }}{{ formatPrice(changePercent) }}%
          </span>
        </div>
      </div>

      <!-- K线图 -->
      <div class="chart-container">
        <KLineChart
          v-if="stockCode"
          :symbol="stockCode"
          :start-date="startDate"
          :end-date="endDate"
          :show-legend="true"
          :auto-load="true"
          @data-loaded="onDataLoaded"
          @error="onChartError"
        />
      </div>
    </div>
  </a-modal>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import KLineChart from '@/components/charts/KLineChart.vue'
import dayjs from 'dayjs'

interface Props {
  visible: boolean
  stockCode: string
  stockName?: string
  currentPrice?: number
  changePercent?: number
}

const props = withDefaults(defineProps<Props>(), {
  stockName: '',
  currentPrice: 0,
  changePercent: 0
})

const emit = defineEmits<{
  'update:visible': [visible: boolean]
  close: []
}>()

const startDate = computed(() => {
  return dayjs().subtract(3, 'month').format('YYYY-MM-DD')
})

const endDate = computed(() => {
  return dayjs().format('YYYY-MM-DD')
})

const modalTitle = computed(() => {
  return `${props.stockName || props.stockCode} - K线图`
})

const priceChangeClass = computed(() => {
  if (props.changePercent > 0) return 'up'
  if (props.changePercent < 0) return 'down'
  return 'neutral'
})

function formatPrice(price: number): string {
  if (price === null || price === undefined) return '--'
  return price.toFixed(2)
}

function onClose() {
  emit('update:visible', false)
  emit('close')
}

function onDataLoaded(data: { count: number; fromCache: boolean }) {
  console.log(`[StockKlineModal] K线数据加载完成: ${data.count} 条`)
}

function onChartError(err: string) {
  console.error('[StockKlineModal] K线加载失败:', err)
}
</script>

<style scoped lang="scss">
.kline-modal-content {
  min-height: 400px;
  display: flex;
  flex-direction: column;
}

.stock-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: #1a1a1a;
  border-bottom: 1px solid #2a2a2a;
  border-radius: 8px 8px 0 0;
}

.stock-info {
  display: flex;
  align-items: center;
  gap: 12px;

  .stock-code {
    font-size: 16px;
    font-weight: 600;
    color: #d1d4dc;
  }

  .stock-name {
    font-size: 14px;
    color: #787b86;
  }
}

.stock-price {
  display: flex;
  align-items: baseline;
  gap: 8px;

  &.up {
    .current-price,
    .price-change {
      color: #ef4444;
    }
  }

  &.down {
    .current-price,
    .price-change {
      color: #22c55e;
    }
  }

  &.neutral {
    .current-price,
    .price-change {
      color: #d1d4dc;
    }
  }

  .current-price {
    font-size: 20px;
    font-weight: 700;
  }

  .price-change {
    font-size: 14px;
    font-weight: 500;
  }
}

.chart-container {
  width: 100%;
  height: 450px;
  background: #0a0a0a;
  border-radius: 0 0 8px 8px;
}

:deep(.dark-modal) {
  .ant-modal-content {
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
  }

  .ant-modal-header {
    background: #1a1a1a;
    border-bottom: 1px solid #2a2a2a;
  }

  .ant-modal-title {
    color: #d1d4dc;
  }

  .ant-modal-close {
    color: #787b86;

    &:hover {
      color: #d1d4dc;
    }
  }

  .ant-modal-body {
    padding: 0;
  }
}
</style>
