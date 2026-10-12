import type {
  BacktestError,
  BacktestErrorContext,
  ErrorSolution,
  ErrorCode
} from '../types/error'
import {
  ERROR_CODES,
  ERROR_DEFINITIONS,
  ErrorStage,
  ErrorCategory,
  ErrorSeverity
} from '../types/error'

function generateErrorId(): string {
  return `err_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
}

function createDefaultContext(): BacktestErrorContext {
  return {
    timestamp: Date.now()
  }
}

export class BacktestErrorService {
  private static instance: BacktestErrorService
  private errorHistory: BacktestError[] = []
  private maxHistorySize = 50

  static getInstance(): BacktestErrorService {
    if (!BacktestErrorService.instance) {
      BacktestErrorService.instance = new BacktestErrorService()
    }
    return BacktestErrorService.instance
  }

  createError(
    code: ErrorCode,
    context?: Partial<BacktestErrorContext>,
    rawError?: any,
    detail?: string
  ): BacktestError {
    const definition = ERROR_DEFINITIONS[code] || ERROR_DEFINITIONS[ERROR_CODES.UNKNOWN_ERROR]
    
    const error: BacktestError = {
      id: generateErrorId(),
      code,
      stage: definition.stage || ErrorStage.UNKNOWN,
      category: definition.category || ErrorCategory.UNKNOWN,
      severity: definition.severity || ErrorSeverity.HIGH,
      title: definition.title || '未知错误',
      message: definition.message || '发生了未知错误',
      detail,
      possibleCauses: definition.possibleCauses || [],
      solutions: definition.solutions || [],
      context: {
        ...createDefaultContext(),
        ...context
      },
      rawError,
      isRecoverable: this.isRecoverable(code),
      retryCount: 0,
      maxRetries: this.getMaxRetries(code)
    }
    
    this.addToHistory(error)
    return error
  }

  parseBackendError(
    errorMessage: string,
    context?: Partial<BacktestErrorContext>,
    rawError?: any
  ): BacktestError {
    const lowerMessage = errorMessage.toLowerCase()
    
    if (lowerMessage.includes('database') || lowerMessage.includes('duckdb') || lowerMessage.includes('questdb')) {
      return this.createError(ERROR_CODES.SYSTEM_DB_CONNECTION_FAILED, context, rawError, errorMessage)
    }
    
    if (lowerMessage.includes('redis')) {
      return this.createError(ERROR_CODES.SYSTEM_REDIS_CONNECTION_FAILED, context, rawError, errorMessage)
    }
    
    if (lowerMessage.includes('strategy') && (lowerMessage.includes('not found') || lowerMessage.includes('不存在'))) {
      return this.createError(ERROR_CODES.SYSTEM_STRATEGY_NOT_FOUND, context, rawError, errorMessage)
    }
    
    if (lowerMessage.includes('no data') || lowerMessage.includes('没有数据') || lowerMessage.includes('empty')) {
      return this.createError(ERROR_CODES.BACKTEST_NO_DATA, context, rawError, errorMessage)
    }
    
    if (lowerMessage.includes('initialization') || lowerMessage.includes('初始化')) {
      return this.createError(ERROR_CODES.SYSTEM_INITIALIZATION_FAILED, context, rawError, errorMessage)
    }
    
    if (lowerMessage.includes('calculation') || lowerMessage.includes('计算')) {
      return this.createError(ERROR_CODES.BACKTEST_CALCULATION_ERROR, context, rawError, errorMessage)
    }
    
    if (lowerMessage.includes('memory') || lowerMessage.includes('内存')) {
      return this.createError(ERROR_CODES.BACKTEST_MEMORY_OVERFLOW, context, rawError, errorMessage)
    }
    
    if (lowerMessage.includes('timeout') || lowerMessage.includes('超时')) {
      return this.createError(ERROR_CODES.FRONTEND_REQUEST_TIMEOUT, context, rawError, errorMessage)
    }
    
    if (lowerMessage.includes('validation') || lowerMessage.includes('参数') || lowerMessage.includes('invalid')) {
      return this.createError(ERROR_CODES.BACKEND_VALIDATION_FAILED, context, rawError, errorMessage)
    }
    
    return this.createError(ERROR_CODES.UNKNOWN_ERROR, context, rawError, errorMessage)
  }

  createNetworkError(context?: Partial<BacktestErrorContext>): BacktestError {
    return this.createError(ERROR_CODES.FRONTEND_NETWORK_OFFLINE, context)
  }

  createSocketError(context?: Partial<BacktestErrorContext>): BacktestError {
    return this.createError(ERROR_CODES.FRONTEND_SOCKET_DISCONNECTED, context)
  }

  createTimeoutError(context?: Partial<BacktestErrorContext>): BacktestError {
    return this.createError(ERROR_CODES.FRONTEND_REQUEST_TIMEOUT, context)
  }

  createValidationError(
    field: string,
    value: any,
    reason: string,
    context?: Partial<BacktestErrorContext>
  ): BacktestError {
    return this.createError(
      ERROR_CODES.FRONTEND_INVALID_PARAMS,
      context,
      { field, value, reason },
      `字段 "${field}" 验证失败: ${reason}`
    )
  }

  createStreamInterruptedError(context?: Partial<BacktestErrorContext>): BacktestError {
    return this.createError(ERROR_CODES.RESULT_STREAM_INTERRUPTED, context)
  }

  private isRecoverable(code: ErrorCode): boolean {
    const recoverableCodes = [
      ERROR_CODES.FRONTEND_REQUEST_TIMEOUT,
      ERROR_CODES.FRONTEND_SOCKET_DISCONNECTED,
      ERROR_CODES.BACKEND_RATE_LIMITED,
      ERROR_CODES.RESULT_STREAM_INTERRUPTED,
      ERROR_CODES.RESULT_SOCKET_ERROR
    ]
    return recoverableCodes.includes(code)
  }

  private getMaxRetries(code: ErrorCode): number {
    const retryLimits: Partial<Record<ErrorCode, number>> = {
      [ERROR_CODES.FRONTEND_REQUEST_TIMEOUT]: 3,
      [ERROR_CODES.FRONTEND_SOCKET_DISCONNECTED]: 5,
      [ERROR_CODES.BACKEND_RATE_LIMITED]: 1,
      [ERROR_CODES.RESULT_STREAM_INTERRUPTED]: 2
    }
    return retryLimits[code] || 0
  }

  private addToHistory(error: BacktestError): void {
    this.errorHistory.unshift(error)
    if (this.errorHistory.length > this.maxHistorySize) {
      this.errorHistory.pop()
    }
  }

  getHistory(): BacktestError[] {
    return [...this.errorHistory]
  }

  getLastError(): BacktestError | null {
    return this.errorHistory[0] || null
  }

  clearHistory(): void {
    this.errorHistory = []
  }

  formatErrorForDisplay(error: BacktestError): string {
    const lines = [
      `错误码: ${error.code}`,
      `阶段: ${error.stage}`,
      `类型: ${error.category}`,
      `严重程度: ${error.severity}`,
      '',
      `标题: ${error.title}`,
      `描述: ${error.message}`,
      ''
    ]
    
    if (error.detail) {
      lines.push(`详细信息: ${error.detail}`)
      lines.push('')
    }
    
    if (error.possibleCauses.length > 0) {
      lines.push('可能原因:')
      error.possibleCauses.forEach((cause, i) => {
        lines.push(`  ${i + 1}. ${cause}`)
      })
      lines.push('')
    }
    
    if (error.solutions.length > 0) {
      lines.push('解决方案:')
      error.solutions.forEach((solution, i) => {
        lines.push(`  ${i + 1}. ${solution.title}`)
        solution.steps.forEach((step, j) => {
          lines.push(`     ${j + 1}) ${step}`)
        })
      })
      lines.push('')
    }
    
    lines.push('上下文信息:')
    lines.push(`  时间: ${new Date(error.context.timestamp).toLocaleString()}`)
    if (error.context.strategyName) {
      lines.push(`  策略: ${error.context.strategyName}`)
    }
    if (error.context.startDate && error.context.endDate) {
      lines.push(`  日期范围: ${error.context.startDate} ~ ${error.context.endDate}`)
    }
    
    return lines.join('\n')
  }

  formatErrorForCopy(error: BacktestError): string {
    return JSON.stringify({
      code: error.code,
      stage: error.stage,
      category: error.category,
      severity: error.severity,
      title: error.title,
      message: error.message,
      detail: error.detail,
      possibleCauses: error.possibleCauses,
      solutions: error.solutions.map(s => ({
        title: s.title,
        steps: s.steps
      })),
      context: {
        ...error.context,
        timestamp: new Date(error.context.timestamp).toISOString()
      }
    }, null, 2)
  }

  validateBacktestParams(params: {
    strategy_name: string;
    start_date: string;
    end_date: string;
  }): BacktestError | null {
    if (!params.strategy_name || params.strategy_name.trim() === '') {
      return this.createValidationError('strategy_name', params.strategy_name, '策略名称不能为空')
    }
    
    if (!params.start_date) {
      return this.createValidationError('start_date', params.start_date, '开始日期不能为空')
    }
    
    if (!params.end_date) {
      return this.createValidationError('end_date', params.end_date, '结束日期不能为空')
    }
    
    const startDate = new Date(params.start_date)
    const endDate = new Date(params.end_date)
    
    if (isNaN(startDate.getTime())) {
      return this.createValidationError('start_date', params.start_date, '开始日期格式无效')
    }
    
    if (isNaN(endDate.getTime())) {
      return this.createValidationError('end_date', params.end_date, '结束日期格式无效')
    }
    
    if (startDate >= endDate) {
      return this.createValidationError(
        'date_range',
        `${params.start_date} ~ ${params.end_date}`,
        '开始日期必须早于结束日期'
      )
    }
    
    return null
  }
}

export const errorService = BacktestErrorService.getInstance()

export function useErrorService() {
  return {
    createError: errorService.createError.bind(errorService),
    parseBackendError: errorService.parseBackendError.bind(errorService),
    createNetworkError: errorService.createNetworkError.bind(errorService),
    createSocketError: errorService.createSocketError.bind(errorService),
    createTimeoutError: errorService.createTimeoutError.bind(errorService),
    createValidationError: errorService.createValidationError.bind(errorService),
    createStreamInterruptedError: errorService.createStreamInterruptedError.bind(errorService),
    validateBacktestParams: errorService.validateBacktestParams.bind(errorService),
    formatErrorForDisplay: errorService.formatErrorForDisplay.bind(errorService),
    formatErrorForCopy: errorService.formatErrorForCopy.bind(errorService),
    getHistory: errorService.getHistory.bind(errorService),
    getLastError: errorService.getLastError.bind(errorService),
    clearHistory: errorService.clearHistory.bind(errorService)
  }
}
