import { defineStore } from 'pinia';
import { ref, computed, shallowRef } from 'vue';
import { apiService } from '../services/api';
import { useSocketIO } from '../composables/useSocketIO';
import type { KlineData } from '../types/api';

export interface TradeMarker {
  date: string;
  action: 'buy' | 'sell';
  price: number;
  quantity: number;
  symbol: string;
}

export const useKlineStore = defineStore('kline', () => {
  const klineData = ref<KlineData[]>([]);
  const selectedSymbol = ref<string>('');
  const tradeMarkers = ref<TradeMarker[]>([]);
  const isLoading = ref(false);
  const error = ref<string | null>(null);
  const dateRange = ref<{ start: string; end: string } | null>(null);

  const { onEvent } = useSocketIO();

  async function loadKlineData(
    symbolCode: string,
    startDate: string,
    endDate: string
  ) {
    console.log('[klineStore] loadKlineData called, symbolCode:', symbolCode, 'startDate:', startDate, 'endDate:', endDate);
    try {
      isLoading.value = true;
      error.value = null;
      selectedSymbol.value = symbolCode;
      dateRange.value = { start: startDate, end: endDate };

      console.log('[klineStore] 开始调用 apiService.getKlineData...');
      const data = await apiService.getKlineData(symbolCode, startDate, endDate);
      console.log('[klineStore] apiService.getKlineData 返回, 数据:', {
        is_array: Array.isArray(data),
        length: data?.length,
        first_item: data?.[0],
        last_item: data?.[data.length - 1]
      });
      klineData.value = data || [];
      console.log('[klineStore] klineData.value 已更新, 长度:', klineData.value.length);
    } catch (err) {
      console.error('[klineStore] loadKlineData error:', err);
      error.value = err instanceof Error ? err.message : '加载K线数据失败';
      klineData.value = [];
    } finally {
      isLoading.value = false;
    }
  }

  function setSelectedSymbol(symbol: string) {
    selectedSymbol.value = symbol;
  }

  function addTradeMarker(marker: TradeMarker) {
    const existingIndex = tradeMarkers.value.findIndex(
      m => m.date === marker.date && m.action === marker.action
    );
    if (existingIndex >= 0) {
    }
  }

  function clearTradeMarkers() {
    tradeMarkers.value = [];
  }

  function clearKlineData() {
    klineData.value = [];
  }

  const markersForDate = computed(() => {
    return (date: string) => {
      return tradeMarkers.value.filter(m => m.date === date);
    };
  });

  return {
    klineData,
    selectedSymbol,
    tradeMarkers,
    isLoading,
    error,
    dateRange,
    loadKlineData,
    addTradeMarker,
    clearTradeMarkers,
    clearKlineData,
    markersForDate,
    setSelectedSymbol,
  };
});
