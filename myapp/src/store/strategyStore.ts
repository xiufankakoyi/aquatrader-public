/**
 * 策略状态管理 Store
 * 使用 Pinia 管理当前版本、可选版本列表、回测数据、搜索结果
 */
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import type { Strategy, Metrics, MonthlyReturn, Trade, StrategySession } from '../types/backtest';

// 策略版本接口
export interface StrategyVersion {
  id: string;
  name: string;
  version: string;
  createdAt: string;
  description?: string;
}

// 回测结果接口
export interface BacktestResult {
  versionId: string;
  metrics: Metrics;
  equityCurve: Array<{ date: string; equity: number; benchmarkEquity?: number }>;
  monthlyReturns: MonthlyReturn[];
  trades: Trade[];
  rejectedOrders?: Record<string, number>;
  slippageCost?: number;
  filterStats?: Record<string, number | boolean | string | null>;
  startDate?: string;
  endDate?: string;
  radarScores?: {
    excessReturn: number;
    riskConsistency: number;
    maxDrawdown: number;
    tradingQuality: number;
    antiOverfitting: number;
  };
  // 回测结果管理字段
  createdAt?: string;
  updatedAt?: string;
  tags?: string[];
  annotations?: {
    id: string;
    content: string;
    author: string;
    createdAt: string;
    updatedAt: string;
  }[];
  notes?: string;
  versionDescription?: string;
}

// 参数搜索结果接口
export interface ParameterSearchResult {
  id: string;
  params: Record<string, number | boolean | string>;
  metrics: Metrics;
  rank: number;
}

export const useStrategyStore = defineStore('strategy', () => {
  // 当前选中的版本 ID
  const currentVersionId = ref<string | null>(null);
  
  // 可用的策略版本列表
  const availableVersions = ref<StrategyVersion[]>([]);
  
  // 当前版本的回测结果
  const currentBacktestResult = ref<BacktestResult | null>(null);
  
  // 参数搜索结果列表
  const parameterSearchResults = ref<ParameterSearchResult[]>([]);
  
  // Dashboard 数据（可能包含多个版本）
  const dashboardData = ref<{
    versions: Array<{
      versionId: string;
      versionName: string;
      equityCurve: Array<{ date: string; equity: number }>;
      metrics: Metrics;
    }>;
    benchmark: Array<{ date: string; equity: number }>;
  } | null>(null);
  
  // 加载状态
  const isLoading = ref(false);
  const error = ref<string | null>(null);
  
  // 策略会话历史（用于历史记录页面）
  const strategySessions = ref<Record<string, StrategySession[]>>({});
  
  // 回测结果管理：保存所有版本的回测结果，包括注释和标签
  const savedBacktestResults = ref<Map<string, BacktestResult>>(new Map());

  // 计算属性：当前版本信息
  const currentVersion = computed(() => {
    if (!currentVersionId.value) return null;
    return availableVersions.value.find(v => v.id === currentVersionId.value) || null;
  });

  // 计算属性：当前版本的雷达图分数
  const currentRadarScores = computed(() => {
    return currentBacktestResult.value?.radarScores || null;
  });
  
  // 计算属性：所有保存的回测结果列表
  const savedResultsList = computed(() => {
    return Array.from(savedBacktestResults.value.values()).sort((a, b) => {
      const dateA = new Date(a.createdAt || 0).getTime();
      const dateB = new Date(b.createdAt || 0).getTime();
      return dateB - dateA;
    });
  });

  // Actions: 设置当前版本
  function setCurrentVersion(versionId: string) {
    currentVersionId.value = versionId;
  }

  // Actions: 设置可用版本列表
  function setAvailableVersions(versions: StrategyVersion[]) {
    availableVersions.value = versions;
  }

  // Actions: 设置当前回测结果
  function setCurrentBacktestResult(result: BacktestResult) {
    currentBacktestResult.value = result;
  }

  // Actions: 设置参数搜索结果
  function setParameterSearchResults(results: ParameterSearchResult[]) {
    parameterSearchResults.value = results;
  }

  // Actions: 设置 Dashboard 数据
  function setDashboardData(data: typeof dashboardData.value) {
    dashboardData.value = data;
  }

  // Actions: 设置加载状态
  function setLoading(loading: boolean) {
    isLoading.value = loading;
  }

  // Actions: 设置错误信息
  function setError(errorMessage: string | null) {
    error.value = errorMessage;
  }

  // Actions: 重置状态
  function reset() {
    currentVersionId.value = null;
    currentBacktestResult.value = null;
    parameterSearchResults.value = [];
    dashboardData.value = null;
    error.value = null;
  }

  // 回测结果管理 Actions
  
  // 保存回测结果，包括注释和标签
  function saveBacktestResult(result: BacktestResult) {
    const now = new Date().toISOString();
    const updatedResult = {
      ...result,
      createdAt: result.createdAt || now,
      updatedAt: now
    };
    savedBacktestResults.value.set(updatedResult.versionId, updatedResult);
    // 保存到本地存储
    saveResultsToLocalStorage();
  }
  
  // 更新回测结果的注释和标签
  function updateBacktestResultMetadata(versionId: string, metadata: Partial<Omit<BacktestResult, 'versionId' | 'metrics' | 'equityCurve' | 'monthlyReturns' | 'trades' | 'radarScores'>>) {
    const result = savedBacktestResults.value.get(versionId);
    if (result) {
      const updatedResult = {
        ...result,
        ...metadata,
        updatedAt: new Date().toISOString()
      };
      savedBacktestResults.value.set(versionId, updatedResult);
      // 如果是当前版本，更新当前回测结果
      if (currentVersionId.value === versionId) {
        currentBacktestResult.value = updatedResult;
      }
      // 保存到本地存储
      saveResultsToLocalStorage();
    }
  }
  
  // 添加标签到回测结果
  function addTagToResult(versionId: string, tag: string) {
    const result = savedBacktestResults.value.get(versionId);
    if (result) {
      const tags = new Set(result.tags || []);
      tags.add(tag);
      updateBacktestResultMetadata(versionId, { tags: Array.from(tags) });
    }
  }
  
  // 从回测结果移除标签
  function removeTagFromResult(versionId: string, tag: string) {
    const result = savedBacktestResults.value.get(versionId);
    if (result) {
      const tags = new Set(result.tags || []);
      tags.delete(tag);
      updateBacktestResultMetadata(versionId, { tags: Array.from(tags) });
    }
  }
  
  // 添加注释到回测结果
  function addAnnotationToResult(versionId: string, annotation: Omit<BacktestResult['annotations'][0], 'id' | 'createdAt' | 'updatedAt'>) {
    const result = savedBacktestResults.value.get(versionId);
    if (result) {
      const now = new Date().toISOString();
      const newAnnotation = {
        ...annotation,
        id: crypto.randomUUID(),
        createdAt: now,
        updatedAt: now
      };
      const annotations = [...(result.annotations || []), newAnnotation];
      updateBacktestResultMetadata(versionId, { annotations });
    }
  }
  
  // 更新注释
  function updateAnnotation(versionId: string, annotationId: string, content: string) {
    const result = savedBacktestResults.value.get(versionId);
    if (result) {
      const annotations = result.annotations?.map(ann => 
        ann.id === annotationId 
          ? { ...ann, content, updatedAt: new Date().toISOString() } 
          : ann
      ) || [];
      updateBacktestResultMetadata(versionId, { annotations });
    }
  }
  
  // 删除注释
  function deleteAnnotation(versionId: string, annotationId: string) {
    const result = savedBacktestResults.value.get(versionId);
    if (result) {
      const annotations = result.annotations?.filter(ann => ann.id !== annotationId) || [];
      updateBacktestResultMetadata(versionId, { annotations });
    }
  }
  
  // 获取回测结果
  function getBacktestResult(versionId: string) {
    return savedBacktestResults.value.get(versionId) || null;
  }
  
  // 获取所有保存的回测结果
  function getAllBacktestResults() {
    return Array.from(savedBacktestResults.value.values());
  }
  
  // 根据标签筛选回测结果
  function getResultsByTag(tag: string) {
    return Array.from(savedBacktestResults.value.values())
      .filter(result => result.tags?.includes(tag) || false);
  }
  
  // 保存结果到本地存储
  function saveResultsToLocalStorage() {
    try {
      const results = Array.from(savedBacktestResults.value.entries());
      localStorage.setItem('backtestResults', JSON.stringify(results));
    } catch (e) {
      console.error('保存回测结果到本地存储失败:', e);
    }
  }
  
  // 从本地存储加载结果
  function loadResultsFromLocalStorage() {
    try {
      const results = localStorage.getItem('backtestResults');
      if (results) {
        const parsed = JSON.parse(results) as Array<[string, BacktestResult]>;
        savedBacktestResults.value = new Map(parsed);
      }
    } catch (e) {
      console.error('从本地存储加载回测结果失败:', e);
    }
  }

  return {
    // State
    currentVersionId,
    availableVersions,
    currentBacktestResult,
    parameterSearchResults,
    dashboardData,
    strategySessions,
    isLoading,
    error,
    // Computed
    currentVersion,
    currentRadarScores,
    // Actions
    setCurrentVersion,
    setAvailableVersions,
    setCurrentBacktestResult,
    setParameterSearchResults,
    setDashboardData,
    setLoading,
    setError,
    reset
  };
});

