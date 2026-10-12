import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { BacktestError } from '../types/error'
import { ErrorSeverity, ErrorStage, STAGE_LABELS } from '../types/error'

export const useErrorStore = defineStore('error', () => {
  const currentError = ref<BacktestError | null>(null)
  const errorHistory = ref<BacktestError[]>([])
  const isShowing = ref(false)
  const maxHistorySize = 20

  const hasError = computed(() => currentError.value !== null)
  
  const errorCount = computed(() => errorHistory.value.length)
  
  const criticalErrors = computed(() => 
    errorHistory.value.filter(e => e.severity === ErrorSeverity.CRITICAL)
  )
  
  const recentErrors = computed(() => errorHistory.value.slice(0, 5))

  function setError(error: BacktestError) {
    currentError.value = error
    isShowing.value = true
    addToHistory(error)
  }

  function clearError() {
    currentError.value = null
    isShowing.value = false
  }

  function showLast() {
    if (errorHistory.value.length > 0) {
      currentError.value = errorHistory.value[0]
      isShowing.value = true
    }
  }

  function addToHistory(error: BacktestError) {
    errorHistory.value.unshift(error)
    if (errorHistory.value.length > maxHistorySize) {
      errorHistory.value.pop()
    }
  }

  function clearHistory() {
    errorHistory.value = []
  }

  function getErrorsByStage(stage: ErrorStage) {
    return errorHistory.value.filter(e => e.stage === stage)
  }

  function getErrorsByStrategy(strategyName: string) {
    return errorHistory.value.filter(e => e.context.strategyName === strategyName)
  }

  function getStagePath(error: BacktestError): { stage: ErrorStage; label: string; status: 'success' | 'error' | 'pending' }[] {
    const stages = [
      { stage: ErrorStage.FRONTEND_SEND, label: STAGE_LABELS[ErrorStage.FRONTEND_SEND], status: 'pending' as const },
      { stage: ErrorStage.BACKEND_RECEIVE, label: STAGE_LABELS[ErrorStage.BACKEND_RECEIVE], status: 'pending' as const },
      { stage: ErrorStage.SYSTEM_INTERACTION, label: STAGE_LABELS[ErrorStage.SYSTEM_INTERACTION], status: 'pending' as const },
      { stage: ErrorStage.BACKTEST_EXECUTE, label: STAGE_LABELS[ErrorStage.BACKTEST_EXECUTE], status: 'pending' as const },
      { stage: ErrorStage.RESULT_RETURN, label: STAGE_LABELS[ErrorStage.RESULT_RETURN], status: 'pending' as const }
    ]
    
    const errorStageIndex = stages.findIndex(s => s.stage === error.stage)
    
    for (let i = 0; i < stages.length; i++) {
      if (i < errorStageIndex) {
        stages[i].status = 'success'
      } else if (i === errorStageIndex) {
        stages[i].status = 'error'
      }
    }
    
    return stages
  }

  return {
    currentError,
    errorHistory,
    isShowing,
    hasError,
    errorCount,
    criticalErrors,
    recentErrors,
    setError,
    clearError,
    showLast,
    clearHistory,
    getErrorsByStage,
    getErrorsByStrategy,
    getStagePath
  }
})
