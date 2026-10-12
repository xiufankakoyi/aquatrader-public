<template>
  <Teleport to="body">
    <Transition name="fade">
      <div v-if="errorStore.isShowing" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" @click.self="closeModal">
        <div class="bg-white dark:bg-gray-800 rounded-xl shadow-2xl max-w-2xl w-full max-h-[90vh] overflow-hidden flex flex-col" @click.stop>
          
          <div class="p-4 border-b dark:border-gray-700 flex items-center justify-between" :class="headerBgClass">
            <div class="flex items-center gap-3">
              <span class="text-2xl">{{ severityInfo.icon }}</span>
              <div>
                <h3 class="font-bold text-lg text-white">{{ error?.title || '发生错误' }}</h3>
                <p class="text-sm text-white/80">{{ error?.code }} · {{ stageLabel }}</p>
              </div>
            </div>
            <button @click="closeModal" class="text-white/80 hover:text-white transition-colors">
              <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <div class="p-4 border-b dark:border-gray-700">
            <div class="flex items-center justify-between mb-2">
              <span class="text-sm font-medium text-gray-500 dark:text-gray-400">错误定位路径</span>
            </div>
            <div class="flex items-center gap-1">
              <template v-for="(stage, index) in stagePath" :key="stage.stage">
                <div 
                  class="flex items-center gap-1 px-2 py-1 rounded text-xs font-medium transition-colors"
                  :class="getStageClass(stage.status)"
                >
                  <svg v-if="stage.status === 'success'" class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd" />
                  </svg>
                  <svg v-else-if="stage.status === 'error'" class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
                  </svg>
                  <span>{{ stage.label }}</span>
                </div>
                <svg v-if="index < stagePath.length - 1" class="w-4 h-4 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                </svg>
              </template>
            </div>
          </div>

          <div class="flex-1 overflow-y-auto p-4 space-y-4">
            
            <div class="bg-red-50 dark:bg-red-900/20 rounded-lg p-4">
              <p class="text-sm text-red-800 dark:text-red-200">{{ error?.message }}</p>
              <p v-if="error?.detail" class="mt-2 text-xs text-red-600 dark:text-red-300 font-mono bg-red-100 dark:bg-red-900/40 p-2 rounded">{{ error.detail }}</p>
            </div>

            <div v-if="error?.possibleCauses && error.possibleCauses.length > 0">
              <button @click="toggleSection('causes')" class="flex items-center justify-between w-full text-left font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white">
                <span>可能原因</span>
                <svg class="w-5 h-5 transition-transform" :class="{ 'rotate-180': expandedSections.causes }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              <Transition name="slide">
                <ul v-show="expandedSections.causes" class="mt-2 space-y-1">
                  <li v-for="(cause, i) in error.possibleCauses" :key="i" class="flex items-start gap-2 text-sm text-gray-600 dark:text-gray-400">
                    <span class="text-gray-400 mt-0.5">•</span>
                    {{ cause }}
                  </li>
                </ul>
              </Transition>
            </div>

            <div v-if="error?.solutions && error.solutions.length > 0">
              <button @click="toggleSection('solutions')" class="flex items-center justify-between w-full text-left font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white">
                <span>解决方案</span>
                <svg class="w-5 h-5 transition-transform" :class="{ 'rotate-180': expandedSections.solutions }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              <Transition name="slide">
                <div v-show="expandedSections.solutions" class="mt-2 space-y-3">
                  <div v-for="(solution, i) in error.solutions" :key="i" class="bg-green-50 dark:bg-green-900/20 rounded-lg p-3">
                    <h4 class="font-medium text-green-800 dark:text-green-200">{{ solution.title }}</h4>
                    <p class="text-sm text-green-600 dark:text-green-300 mt-1">{{ solution.description }}</p>
                    <ol class="mt-2 space-y-1">
                      <li v-for="(step, j) in solution.steps" :key="j" class="flex items-start gap-2 text-sm text-green-700 dark:text-green-300">
                        <span class="font-medium">{{ j + 1 }}.</span>
                        {{ step }}
                      </li>
                    </ol>
                  </div>
                </div>
              </Transition>
            </div>

            <div>
              <button @click="toggleSection('context')" class="flex items-center justify-between w-full text-left font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white">
                <span>上下文信息</span>
                <svg class="w-5 h-5 transition-transform" :class="{ 'rotate-180': expandedSections.context }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              <Transition name="slide">
                <div v-show="expandedSections.context" class="mt-2 bg-gray-50 dark:bg-gray-900/50 rounded-lg p-3 text-sm">
                  <div class="grid grid-cols-2 gap-2">
                    <div class="text-gray-500 dark:text-gray-400">发生时间</div>
                    <div class="text-gray-700 dark:text-gray-300">{{ formatTimestamp(error?.context.timestamp) }}</div>
                    
                    <div v-if="error?.context.strategyName" class="text-gray-500 dark:text-gray-400">策略名称</div>
                    <div v-if="error?.context.strategyName" class="text-gray-700 dark:text-gray-300">{{ error.context.strategyName }}</div>
                    
                    <div v-if="error?.context.startDate" class="text-gray-500 dark:text-gray-400">开始日期</div>
                    <div v-if="error?.context.startDate" class="text-gray-700 dark:text-gray-300">{{ error.context.startDate }}</div>
                    
                    <div v-if="error?.context.endDate" class="text-gray-500 dark:text-gray-400">结束日期</div>
                    <div v-if="error?.context.endDate" class="text-gray-700 dark:text-gray-300">{{ error.context.endDate }}</div>
                    
                    <div v-if="error?.context.benchmarkCode" class="text-gray-500 dark:text-gray-400">基准代码</div>
                    <div v-if="error?.context.benchmarkCode" class="text-gray-700 dark:text-gray-300">{{ error.context.benchmarkCode }}</div>
                  </div>
                  
                  <div v-if="error?.context.params && Object.keys(error.context.params).length > 0" class="mt-3 pt-3 border-t dark:border-gray-700">
                    <div class="text-gray-500 dark:text-gray-400 mb-1">参数</div>
                    <pre class="text-xs bg-gray-100 dark:bg-gray-800 p-2 rounded overflow-x-auto">{{ JSON.stringify(error.context.params, null, 2) }}</pre>
                  </div>
                </div>
              </Transition>
            </div>

            <div v-if="error?.rawError">
              <button @click="toggleSection('raw')" class="flex items-center justify-between w-full text-left font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white">
                <span>原始错误信息</span>
                <svg class="w-5 h-5 transition-transform" :class="{ 'rotate-180': expandedSections.raw }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              <Transition name="slide">
                <pre v-show="expandedSections.raw" class="mt-2 text-xs bg-gray-100 dark:bg-gray-900 p-3 rounded overflow-x-auto text-red-600 dark:text-red-400">{{ formatRawError(error?.rawError) }}</pre>
              </Transition>
            </div>
          </div>

          <div class="p-4 border-t dark:border-gray-700 flex items-center justify-between gap-3 bg-gray-50 dark:bg-gray-900/50">
            <div class="flex items-center gap-2">
              <button 
                @click="copyError" 
                class="flex items-center gap-1 px-3 py-1.5 text-sm bg-white dark:bg-gray-700 border dark:border-gray-600 rounded hover:bg-gray-100 dark:hover:bg-gray-600 transition-colors"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                </svg>
                {{ copyButtonText }}
              </button>
            </div>
            
            <div class="flex items-center gap-2">
              <button 
                v-if="error?.isRecoverable" 
                @click="retryAction" 
                class="px-4 py-1.5 text-sm bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors"
              >
                重试
              </button>
              <button 
                @click="closeModal" 
                class="px-4 py-1.5 text-sm bg-gray-200 dark:bg-gray-700 rounded hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, reactive } from 'vue'
import { useErrorStore } from '../store/errorStore'
import { useErrorService } from '../services/errorService'
import { ErrorSeverity, SEVERITY_LABELS, STAGE_LABELS } from '../types/error'
import type { BacktestError } from '../types/error'

const errorStore = useErrorStore()
const { formatErrorForCopy } = useErrorService()

const expandedSections = reactive({
  causes: true,
  solutions: true,
  context: false,
  raw: false
})

const copyButtonText = ref('复制错误信息')

const error = computed(() => errorStore.currentError)

const severityInfo = computed(() => {
  if (!error.value) return SEVERITY_LABELS[ErrorSeverity.MEDIUM]
  return SEVERITY_LABELS[error.value.severity] || SEVERITY_LABELS[ErrorSeverity.MEDIUM]
})

const headerBgClass = computed(() => {
  switch (error.value?.severity) {
    case ErrorSeverity.CRITICAL:
      return 'bg-red-600'
    case ErrorSeverity.HIGH:
      return 'bg-orange-500'
    case ErrorSeverity.MEDIUM:
      return 'bg-yellow-500'
    default:
      return 'bg-blue-500'
  }
})

const stageLabel = computed(() => {
  if (!error.value) return ''
  return STAGE_LABELS[error.value.stage] || error.value.stage
})

const stagePath = computed(() => {
  if (!error.value) return []
  return errorStore.getStagePath(error.value)
})

function toggleSection(section: keyof typeof expandedSections) {
  expandedSections[section] = !expandedSections[section]
}

function getStageClass(status: 'success' | 'error' | 'pending') {
  switch (status) {
    case 'success':
      return 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300'
    case 'error':
      return 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300'
    default:
      return 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400'
  }
}

function formatTimestamp(timestamp?: number): string {
  if (!timestamp) return '-'
  return new Date(timestamp).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}

function formatRawError(rawError: any): string {
  if (typeof rawError === 'string') return rawError
  if (rawError instanceof Error) {
    return `${rawError.name}: ${rawError.message}\n${rawError.stack || ''}`
  }
  try {
    return JSON.stringify(rawError, null, 2)
  } catch {
    return String(rawError)
  }
}

async function copyError() {
  if (!error.value) return
  
  try {
    const errorText = formatErrorForCopy(error.value)
    await navigator.clipboard.writeText(errorText)
    copyButtonText.value = '已复制!'
    setTimeout(() => {
      copyButtonText.value = '复制错误信息'
    }, 2000)
  } catch (err) {
    console.error('复制失败:', err)
    copyButtonText.value = '复制失败'
    setTimeout(() => {
      copyButtonText.value = '复制错误信息'
    }, 2000)
  }
}

function closeModal() {
  errorStore.clearError()
}

function retryAction() {
  console.log('Retry action triggered')
  closeModal()
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.slide-enter-active,
.slide-leave-active {
  transition: all 0.2s ease;
  overflow: hidden;
}

.slide-enter-from,
.slide-leave-to {
  opacity: 0;
  max-height: 0;
  margin-top: 0;
}

.slide-enter-to,
.slide-leave-from {
  opacity: 1;
  max-height: 500px;
}
</style>
