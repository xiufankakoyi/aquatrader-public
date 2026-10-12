import { ref, computed } from 'vue';
import { useSocketIO } from './useSocketIO';
import type { AlgorithmKey } from '@/config/strategyConfig';

const MAX_HISTORY_POINTS = 20;

export interface OptimizationState {
  isOptimizing: boolean;
  progress: number;
  optimizationStatus: string;
  evaluatedCount: number;
  totalIterations: number;
  bestMetric: number | null;
  bestParams: Record<string, any>;
  evaluationResults: Array<{
    iteration: number;
    metric: number;
    params: Record<string, any>;
    timestamp: number;
  }>;
  optimizationHistory: Array<{
    iteration: number;
    metric: number;
    params: Record<string, any>;
  }>;
  candidates: any[];
  finalSelected: any;
}

export function useOptimization() {
  const socket = useSocketIO();
  
  // 状态
  const isOptimizing = ref(false);
  const progress = ref(0);
  const optimizationStatus = ref('idle');
  const evaluatedCount = ref(0);
  const totalIterations = ref(0);
  const bestMetric = ref<number | null>(null);
  const bestParams = ref<Record<string, any>>({});
  const evaluationResults = ref<Array<{
    iteration: number;
    metric: number;
    params: Record<string, any>;
    timestamp: number;
  }>>([]);
  const optimizationHistory = ref<Array<{
    iteration: number;
    metric: number;
    params: Record<string, any>;
  }>>([]);
  const candidates = ref<any[]>([]);
  const finalSelected = ref<any>(null);
  
  const unsubscribers: Array<() => void> = [];
  
  // 重置状态
  const resetState = () => {
    isOptimizing.value = false;
    optimizationStatus.value = 'idle';
    progress.value = 0;
    evaluatedCount.value = 0;
    totalIterations.value = 0;
    bestMetric.value = null;
    bestParams.value = {};
    candidates.value = [];
    finalSelected.value = null;
    evaluationResults.value = [];
    optimizationHistory.value = [];
  };
  
  // 开始优化
  const startOptimization = (payload: any) => {
    resetState();
    isOptimizing.value = true;
    optimizationStatus.value = 'starting';
    socket.emitEvent('run_optimization', payload);
  };
  
  // 停止优化
  const stopOptimization = () => {
    if (!isOptimizing.value) return;
    socket.emitEvent('stop_optimization', {});
    isOptimizing.value = false;
    optimizationStatus.value = 'cancelled';
  };
  
  // 设置 Socket 监听
  const setupSocketListeners = (callbacks: {
    onProgress?: (data: any) => void;
    onComplete?: (data: any) => void;
    onError?: (data: any) => void;
  }) => {
    const unsubProgress = socket.onEvent('optimization_progress', (data: any) => {
      progress.value = Number(data.progress || 0);
      const iter = data.generation || data.iteration || evaluatedCount.value;
      evaluatedCount.value = iter;
      totalIterations.value = data.total_generations || data.total_iterations || totalIterations.value;
      
      const currentBest =
        typeof data.current_best === 'number'
          ? data.current_best
          : typeof data.best_metric === 'number'
            ? data.best_metric
            : null;
      const currentParams = data.best_params || data.params || {};
      
      if (currentBest !== null) {
        bestMetric.value = currentBest;
        bestParams.value = currentParams;
        const point = {
          iteration: iter || 0,
          metric: currentBest,
          params: currentParams,
          timestamp: Date.now(),
        };
        evaluationResults.value.push(point);
        if (evaluationResults.value.length > MAX_HISTORY_POINTS) {
          evaluationResults.value.shift();
        }
        optimizationHistory.value.push({
          iteration: iter || 0,
          metric: currentBest,
          params: currentParams,
        });
        if (optimizationHistory.value.length > MAX_HISTORY_POINTS) {
          optimizationHistory.value.shift();
        }
      }
      
      callbacks.onProgress?.(data);
    });
    
    const unsubComplete = socket.onEvent('optimization_complete', (data: any) => {
      isOptimizing.value = false;
      optimizationStatus.value = 'finished';
      progress.value = 100;
      
      const bestScoreFromEvent =
        typeof data.best_score === 'number'
          ? data.best_score
          : typeof data.best_metric === 'number'
            ? data.best_metric
            : null;
      const bestParamsFromEvent = data.best_params || bestParams.value;
      
      if (bestScoreFromEvent !== null) {
        bestMetric.value = bestScoreFromEvent;
        bestParams.value = bestParamsFromEvent;
        const point = {
          iteration: evaluatedCount.value || (data.total_iterations || data.total_generations || 0),
          metric: bestScoreFromEvent,
          params: bestParamsFromEvent,
          timestamp: Date.now(),
        };
        evaluationResults.value.push(point);
        if (evaluationResults.value.length > MAX_HISTORY_POINTS) {
          evaluationResults.value.shift();
        }
        optimizationHistory.value.push({
          iteration: point.iteration,
          metric: bestScoreFromEvent,
          params: bestParamsFromEvent,
        });
        if (optimizationHistory.value.length > MAX_HISTORY_POINTS) {
          optimizationHistory.value.shift();
        }
      }
      
      candidates.value = Array.isArray(data.candidates) ? data.candidates : [];
      finalSelected.value = data.final_selected || null;
      
      callbacks.onComplete?.(data);
    });
    
    const unsubError = socket.onEvent('optimization_error', (data: any) => {
      isOptimizing.value = false;
      optimizationStatus.value = 'error';
      callbacks.onError?.(data);
    });
    
    unsubscribers.push(unsubProgress, unsubComplete, unsubError);
  };
  
  // 清理监听
  const cleanup = () => {
    unsubscribers.forEach((fn) => fn());
    unsubscribers.length = 0;
  };
  
  return {
    // 状态
    isOptimizing,
    progress,
    optimizationStatus,
    evaluatedCount,
    totalIterations,
    bestMetric,
    bestParams,
    evaluationResults,
    optimizationHistory,
    candidates,
    finalSelected,
    // 方法
    startOptimization,
    stopOptimization,
    setupSocketListeners,
    cleanup,
    resetState,
  };
}





















