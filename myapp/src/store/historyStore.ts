import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

export interface HistoryRecord {
  id: string;
  strategyName: string;
  dateRange: string;
  createdAt: string;
  status: 'running' | 'completed' | 'error' | 'cancelled';
  strategy?: string;
  algorithm?: string;
  metric?: string;
  bestMetric?: number;
  bestParams?: Record<string, any>;
  totalIterations?: number;
  timestamp?: string;
  results?: any[];
  summary?: any;
  type?: string;
  metrics?: {
    totalReturn: number;
    annualizedReturn: number;
    maxDrawdown: number;
    sharpeRatio: number;
  };
  // CHANGED: 添加完整回测数据字段，用于版本切换
  equitySeries?: Array<{ date: string; equity: number }>;
  benchmarkEquitySeries?: Array<{ date: string; equity: number }>;
  trades?: any[];
  monthlyReturns?: any[];
  holdingPeriods?: any[];
}

export const useHistoryStore = defineStore('history', () => {
  const records = ref<HistoryRecord[]>([]);
  const selectedRecord = ref<HistoryRecord | null>(null);
  const isLoading = ref(false);
  const error = ref<string | null>(null);
  
  // CHANGED: 从 localStorage 加载历史记录
  function loadFromStorage() {
    try {
      const historyKey = 'backtest_history';
      const stored = localStorage.getItem(historyKey);
      if (stored) {
        records.value = JSON.parse(stored);
      }
    } catch (e) {
      console.error('加载历史记录失败:', e);
    }
  }
  
  // 初始化时加载
  loadFromStorage();
  
  const page = ref(1);
  const pageSize = ref(20);
  const total = ref(0);
  const filters = ref<{
    strategyName?: string;
    status?: string;
    startDate?: string;
    endDate?: string;
  }>({});

  const filteredRecords = computed(() => {
    let result = [...records.value];
    
    if (filters.value.strategyName) {
      result = result.filter(r => 
        r.strategyName.toLowerCase().includes(filters.value.strategyName!.toLowerCase())
      );
    }
    
    if (filters.value.status) {
      result = result.filter(r => r.status === filters.value.status);
    }
    
    if (filters.value.startDate) {
      result = result.filter(r => r.createdAt >= filters.value.startDate!);
    }
    
    if (filters.value.endDate) {
      result = result.filter(r => r.createdAt <= filters.value.endDate!);
    }
    
    total.value = result.length;
    return result;
  });

  const paginatedRecords = computed(() => {
    const start = (page.value - 1) * pageSize.value;
    const end = start + pageSize.value;
    return filteredRecords.value.slice(start, end);
  });

  function setFilters(newFilters: typeof filters.value) {
    filters.value = { ...filters.value, ...newFilters };
    page.value = 1;
  }

  function setPage(newPage: number) {
    page.value = newPage;
  }

  function setPageSize(newSize: number) {
    pageSize.value = newSize;
    page.value = 1;
  }

  function selectRecord(record: HistoryRecord | null) {
    selectedRecord.value = record;
  }

  function addRecord(record: HistoryRecord) {
    const existingIndex = records.value.findIndex(r => r.id === record.id);
    if (existingIndex >= 0) {
      records.value[existingIndex] = record;
    } else {
      records.value.unshift(record);
    }
  }

  function updateRecordStatus(id: string, status: HistoryRecord['status']) {
    const record = records.value.find(r => r.id === id);
    if (record) {
      record.status = status;
    }
  }

  function clearRecords() {
    records.value = [];
    selectedRecord.value = null;
  }

  return {
    records,
    selectedRecord,
    isLoading,
    error,
    page,
    pageSize,
    total,
    filters,
    filteredRecords,
    paginatedRecords,
    setFilters,
    setPage,
    setPageSize,
    selectRecord,
    addRecord,
    updateRecordStatus,
    clearRecords,
    loadFromStorage,  // CHANGED: 导出加载函数
  };
});

