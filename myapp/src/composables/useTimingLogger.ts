import { ref, computed } from 'vue'

export interface TimingEvent {
  name: string
  startTime: number
  endTime?: number
  duration?: number
  metadata?: Record<string, any>
}

export interface TimingStats {
  totalDuration: number
  averageDuration: number
  minDuration: number
  maxDuration: number
  eventCount: number
  // Alias to make it clearer when used as an operations counter in UI
  totalOperations: number
}

class TimingLogger {
  private events = ref<TimingEvent[]>([])
  private activeTimers = new Map<string, number>()

  // 开始计时
  start(name: string, metadata?: Record<string, any>): void {
    const startTime = performance.now()
    this.activeTimers.set(name, startTime)
    
    const event: TimingEvent = {
      name,
      startTime,
      metadata
    }
    
    this.events.value.push(event)
    console.log(`[⏱️ 计时开始] ${name}`, metadata || '')
  }

  // 结束计时
  end(name: string): number | null {
    const endTime = performance.now()
    const startTime = this.activeTimers.get(name)
    
    if (startTime === undefined) {
      console.warn(`[⚠️ 计时警告] 未找到计时器: ${name}`)
      return null
    }

    const duration = endTime - startTime
    this.activeTimers.delete(name)

    // 更新事件记录
    const event = this.events.value.find(e => e.name === name && e.endTime === undefined)
    if (event) {
      event.endTime = endTime
      event.duration = duration
    }

    console.log(`[⏱️ 计时结束] ${name}: ${duration.toFixed(2)}ms`)
    return duration
  }

  // 获取事件统计
  getStats(name?: string): TimingStats {
    const events = name 
      ? this.events.value.filter(e => e.name === name && e.duration !== undefined)
      : this.events.value.filter(e => e.duration !== undefined)

    if (events.length === 0) {
      return {
        totalDuration: 0,
        averageDuration: 0,
        minDuration: 0,
        maxDuration: 0,
        eventCount: 0,
        totalOperations: 0
      }
    }

    const durations = events.map(e => e.duration!)
    const totalDuration = durations.reduce((sum, d) => sum + d, 0)
    const minDuration = Math.min(...durations)
    const maxDuration = Math.max(...durations)

    return {
      totalDuration,
      averageDuration: totalDuration / durations.length,
      minDuration,
      maxDuration,
      eventCount: durations.length,
      totalOperations: durations.length
    }
  }

  // 获取所有事件的统计
  getAllStats(): Record<string, TimingStats> {
    const eventNames = [...new Set(this.events.value.map(e => e.name))]
    const stats: Record<string, TimingStats> = {}

    eventNames.forEach(name => {
      stats[name] = this.getStats(name)
    })

    return stats
  }

  // 清除所有计时记录
  clear(): void {
    this.events.value = []
    this.activeTimers.clear()
    console.log('[🧹 计时清理] 所有计时记录已清除')
  }

  // 获取最近的事件
  getRecentEvents(count: number = 10): TimingEvent[] {
    return this.events.value.slice(-count)
  }

  // 获取活跃计时器
  getActiveTimers(): string[] {
    return Array.from(this.activeTimers.keys())
  }

  // 导出计时数据
  exportData(): object {
    return {
      events: this.events.value,
      stats: this.getAllStats(),
      activeTimers: this.getActiveTimers(),
      timestamp: new Date().toISOString()
    }
  }
}

// 创建全局实例
const globalTimingLogger = new TimingLogger()

// Vue组合式函数
export function useTimingLogger() {
  const events = computed(() => globalTimingLogger.getRecentEvents())
  // Aggregated stats across all timers
  const stats = computed(() => globalTimingLogger.getStats())
  // Stats grouped by timer name for callers that need detail
  const allStats = computed(() => globalTimingLogger.getAllStats())
  const activeTimers = computed(() => globalTimingLogger.getActiveTimers())

  const start = (name: string, metadata?: Record<string, any>) => {
    return globalTimingLogger.start(name, metadata)
  }

  const end = (name: string) => {
    return globalTimingLogger.end(name)
  }

  const clear = () => {
    globalTimingLogger.clear()
  }

  const exportData = () => {
    return globalTimingLogger.exportData()
  }

  const getStats = (name?: string) => {
    return globalTimingLogger.getStats(name)
  }

  return {
    events,
    stats,
    allStats,
    activeTimers,
    start,
    end,
    clear,
    exportData,
    getStats
  }
}

// 辅助函数：格式化时间
export function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms.toFixed(0)}ms`
  } else if (ms < 60000) {
    return `${(ms / 1000).toFixed(2)}s`
  } else {
    const minutes = Math.floor(ms / 60000)
    const seconds = ((ms % 60000) / 1000).toFixed(0)
    return `${minutes}m ${seconds}s`
  }
}

// 辅助函数：创建带计时的异步函数包装器
export function withTiming<T extends (...args: any[]) => Promise<any>>(
  fn: T,
  name: string
): T {
  return (async (...args: Parameters<T>) => {
    globalTimingLogger.start(name, { function: fn.name, args: JSON.stringify(args) })
    try {
      const result = await fn(...args)
      globalTimingLogger.end(name)
      return result
    } catch (error) {
      globalTimingLogger.end(name)
      console.error(`[❌ 计时错误] ${name} 执行失败:`, error)
      throw error
    }
  }) as T
}

// 辅助函数：创建带计时的同步函数包装器
export function withTimingSync<T extends (...args: any[]) => any>(
  fn: T,
  name: string
): T {
  return ((...args: Parameters<T>) => {
    globalTimingLogger.start(name, { function: fn.name, args: JSON.stringify(args) })
    try {
      const result = fn(...args)
      globalTimingLogger.end(name)
      return result
    } catch (error) {
      globalTimingLogger.end(name)
      console.error(`[❌ 计时错误] ${name} 执行失败:`, error)
      throw error
    }
  }) as T
}
