import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { apiService } from '../services/api';
import { useSocketIO } from '../composables/useSocketIO';
import type { StrategyInfo } from '../types/api';

export const useDashboardStore = defineStore('dashboard', () => {
  const strategies = ref<StrategyInfo[]>([]);
  const selectedStrategyId = ref<string | null>(null);
  const isLoading = ref(false);
  const error = ref<string | null>(null);
  const lastRefreshTime = ref<Date | null>(null);

  const { status } = useSocketIO();
  const apiConnected = computed(() => status.value === 'OPEN');

  const selectedStrategy = computed(() => {
    if (!selectedStrategyId.value) return null;
    return strategies.value.find(s => s.id === selectedStrategyId.value) || null;
  });

  async function loadStrategies() {
    try {
      isLoading.value = true;
      error.value = null;
      const data = await apiService.getStrategies();
      strategies.value = data;
      if (data.length > 0 && !selectedStrategyId.value) {
        selectedStrategyId.value = data[0].id;
      }
      lastRefreshTime.value = new Date();
    } catch (err) {
      error.value = err instanceof Error ? err.message : '加载策略列表失败';
      strategies.value = [];
    } finally {
      isLoading.value = false;
    }
  }

  function setSelectedStrategy(strategyId: string) {
    selectedStrategyId.value = strategyId;
  }

  function clearError() {
    error.value = null;
  }

  return {
    strategies,
    selectedStrategyId,
    selectedStrategy,
    isLoading,
    error,
    apiConnected,
    lastRefreshTime,
    loadStrategies,
    setSelectedStrategy,
    clearError,
  };
});

