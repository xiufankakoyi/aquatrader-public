<template>
  <!-- 加载界面容器 -->
  <div v-if="visible || isRevealing" class="splash-container" :class="{ 'is-exiting': isRevealing }">
    <!-- 动态K线背景 -->
    <canvas ref="klineCanvas" class="kline-canvas"></canvas>
    
    <div class="splash-overlay"></div>
    <div class="splash-grain"></div>
    
    <!-- 内容区（加载完成时向上滑出） -->
    <div class="splash-content" :class="{ 'content-exit': isRevealing }">
      <div class="hero-section">
        <div class="label-line">
          <span class="line"></span>
          <span class="label-text">QUANTITATIVE TRADING</span>
        </div>
        
        <h1 class="main-title">
          <span class="title-line">AQUA</span>
          <span class="title-line accent">TRADER</span>
        </h1>
        
        <p class="subtitle">{{ status.message }}</p>
      </div>

      <div class="status-panel">
        <div class="panel-header">
          <span class="status-label">System Status</span>
          <span class="status-value" :class="{ 'text-emerald-400': status.ready, 'text-white/60': !status.ready }">
            {{ status.progress }}%
          </span>
        </div>
        
        <div class="progress-track">
          <div 
            class="progress-fill" 
            :style="{ width: `${status.progress}%` }"
            :class="{ 'active': status.progress > 0 && status.progress < 100 }"
          ></div>
        </div>
        
        <div class="panel-metrics">
          <div class="metric-item">
            <span class="metric-label">Phase</span>
            <span class="metric-value">{{ status.phase || '--' }}</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">Status</span>
            <span class="metric-value status-dot-wrapper">
              <span class="dot" :class="dotClass"></span>
              {{ statusText }}
            </span>
          </div>
        </div>
      </div>

      <div v-if="hasError" class="error-section">
        <p class="error-code">{{ status.error_code }}</p>
        <p class="error-message">{{ status.error_message }}</p>
        <button @click="retryStartup" class="btn-retry">
          <i class="fas fa-redo"></i> Retry
        </button>
      </div>
      
      <div v-if="isTimeout" class="timeout-section">
        <p>Connection timeout</p>
        <button @click="retryStartup" class="btn-retry">
          <i class="fas fa-redo"></i> Retry
        </button>
      </div>

    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { request } from '../services/api'

interface StartupStatus {
  phase: string
  step: string
  message: string
  progress: number
  ready: boolean
  error_code: string | null
  error_message: string | null
}

const emit = defineEmits<{
  (e: 'ready'): void
  (e: 'error', code: string, message: string): void
}>()

const visible = ref(true)
const isRevealing = ref(false)
const klineCanvas = ref<HTMLCanvasElement | null>(null)
const status = ref<StartupStatus>({
  phase: 'idle',
  step: 'initializing',
  message: 'Initializing AquaTrade...',
  progress: 0,
  ready: false,
  error_code: null,
  error_message: null,
})

let eventSource: EventSource | null = null
let animFrameId: ReturnType<typeof requestAnimationFrame> | null = null
let retryTimer: ReturnType<typeof setTimeout> | null = null
let timeoutTimer: ReturnType<typeof setTimeout> | null = null
let readyTriggered = false
const STARTUP_TIMEOUT_MS = 120000

// ==================== K线图引擎 ====================
interface RealCandle {
  open: number
  high: number
  low: number
  close: number
}

function generateRealisticCandles(count: number): RealCandle[] {
  const candles: RealCandle[] = []
  let currentPrice = 100
  let trend = 0
  
  for (let i = 0; i < count; i++) {
    // 每5-10根K线改变一次趋势
    if (i % (5 + Math.floor(Math.random() * 6)) === 0) {
      trend = (Math.random() - 0.5) * 0.6
    }
    
    // 添加随机噪声
    const noise = (Math.random() - 0.5) * 0.015
    const effectiveTrend = Math.max(-0.2, Math.min(0.2, trend + noise))
    
    // 随机决定K线类型
    const type = Math.random()
    let open: number, close: number, high: number, low: number
    
    if (type < 0.4) {
      // 阳线（40%）
      open = currentPrice * (1 + (Math.random() - 0.5) * 0.02)
      const changePct = 0.005 + Math.random() * 0.025
      close = open * (1 + changePct)
      
      const shadowType = Math.random()
      let upperPct = 0.05, lowerPct = 0.05
      if (shadowType < 0.3) {
        upperPct = lowerPct = 0.01
      } else if (shadowType < 0.6) {
        upperPct = 0.3 + Math.random() * 0.4
      } else {
        lowerPct = 0.3 + Math.random() * 0.4
      }
      
      const bodyPct = 0.5 + Math.random() * 0.4
      const totalRange = Math.abs(close - open) / bodyPct
      high = Math.max(open, close) + totalRange * upperPct
      low = Math.min(open, close) - totalRange * lowerPct
      
    } else if (type < 0.75) {
      // 阴线（35%）
      open = currentPrice * (1 + (Math.random() - 0.5) * 0.02)
      const changePct = 0.005 + Math.random() * 0.03
      close = open * (1 - changePct)
      
      const shadowType = Math.random()
      let upperPct = 0.05, lowerPct = 0.05
      if (shadowType < 0.3) {
        upperPct = lowerPct = 0.01
      } else if (shadowType < 0.6) {
        upperPct = 0.3 + Math.random() * 0.4
      } else {
        lowerPct = 0.3 + Math.random() * 0.4
      }
      
      const bodyPct = 0.5 + Math.random() * 0.4
      const totalRange = Math.abs(close - open) / bodyPct
      high = Math.max(open, close) + totalRange * upperPct
      low = Math.min(open, close) - totalRange * lowerPct
      
    } else {
      // 十字星/纺锤线（25%）
      open = currentPrice * (1 + (Math.random() - 0.5) * 0.01)
      const smallChange = 0.001 + Math.random() * 0.008
      close = open * (Math.random() > 0.5 ? 1 + smallChange : 1 - smallChange)
      
      const upperPct = 0.4 + Math.random() * 0.5
      const lowerPct = 0.4 + Math.random() * 0.5
      const bodyPct = 0.1 + Math.random() * 0.2
      
      const totalRange = Math.abs(close - open) / bodyPct
      high = Math.max(open, close) + totalRange * upperPct
      low = Math.min(open, close) - totalRange * lowerPct
    }
    
    candles.push({ open, high, low, close })
    currentPrice = close
  }
  
  return candles
}

class LightweightCandleScroller {
  private canvas: HTMLCanvasElement
  private ctx: CanvasRenderingContext2D
  private frames: ImageBitmap[] = []
  private currentFrame = 0
  private frameCount = 30  // 减少到30帧，降低速度
  private width = 0
  private height = 0
  private isReady = false
  private candles: RealCandle[] = []
  private frameDelay = 4  // 每4帧渲染一次，大幅降低速度
  private frameTick = 0

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas
    this.ctx = canvas.getContext('2d')!
    this.candles = generateRealisticCandles(300)  // 生成更多K线避免边界问题
    this.resize()
    this.preRenderFrames()
  }

  resize() {
    const dpr = window.devicePixelRatio || 1
    this.width = window.innerWidth
    this.height = window.innerHeight
    this.canvas.width = this.width * dpr
    this.canvas.height = this.height * dpr
    this.canvas.style.width = `${this.width}px`
    this.canvas.style.height = `${this.height}px`
    this.ctx.scale(dpr, dpr)
  }

  private async preRenderFrames() {
    const offscreen = document.createElement('canvas')
    offscreen.width = this.width
    offscreen.height = this.height
    const ctx = offscreen.getContext('2d')!

    // 预渲染所有帧 - 确保覆盖完整滚动周期
    const scrollRange = 20  // 减少滚动范围，让每帧移动更少
    for (let frame = 0; frame < this.frameCount; frame++) {
      const offset = (frame / this.frameCount) * scrollRange
      this.drawFrame(ctx, offset)
      
      // 创建ImageBitmap
      const bitmap = await createImageBitmap(offscreen)
      this.frames.push(bitmap)
    }
    
    this.isReady = true
  }

  private drawFrame(ctx: CanvasRenderingContext2D, offset: number) {
    const { width, height, candles } = this

    // 背景渐变
    const bgGrad = ctx.createLinearGradient(0, 0, 0, height)
    bgGrad.addColorStop(0, '#03080d')
    bgGrad.addColorStop(0.5, '#050a10')
    bgGrad.addColorStop(1, '#081018')
    ctx.fillStyle = bgGrad
    ctx.fillRect(0, 0, width, height)

    // 网格线
    ctx.strokeStyle = 'rgba(6, 182, 212, 0.03)'
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(0, height * 0.33)
    ctx.lineTo(width, height * 0.33)
    ctx.moveTo(0, height * 0.66)
    ctx.lineTo(width, height * 0.66)
    ctx.stroke()

    // 可见K线数量 - 减少数量确保完全在屏幕内
    const visibleCount = 30
    // 确保有足够的K线数据，避免边界露出
    const maxOffset = candles.length - visibleCount - 10
    const safeOffset = Math.min(offset, maxOffset)
    const startIdx = Math.floor(safeOffset) % maxOffset
    const remainder = offset % 1

    // 计算价格范围 - 使用safeOffset
    let minP = Infinity, maxP = -Infinity
    for (let i = startIdx; i < startIdx + visibleCount + 1 && i < candles.length; i++) {
      const c = candles[i]
      minP = Math.min(minP, c.low)
      maxP = Math.max(maxP, c.high)
    }
    const range = maxP - minP || 1

    const padY = height * 0.12
    const drawH = height - padY * 2
    const stepX = width / visibleCount
    const candleW = stepX * 0.55
    const gap = stepX * 0.45
    const baseAlpha = 0.35

    // 子像素偏移
    const shiftX = -remainder * stepX

    // 批量绘制影线（绿色）
    ctx.strokeStyle = `rgba(52, 211, 153, ${baseAlpha})`
    ctx.lineWidth = 1.2
    ctx.beginPath()
    for (let i = 0; i < visibleCount; i++) {
      const c = candles[startIdx + i]
      if (c.close < c.open) continue
      const x = i * stepX + gap / 2 + candleW / 2 + shiftX
      const yHigh = padY + (1 - (c.high - minP) / range) * drawH
      const yLow = padY + (1 - (c.low - minP) / range) * drawH
      ctx.moveTo(x, yHigh)
      ctx.lineTo(x, yLow)
    }
    ctx.stroke()

    // 批量绘制影线（红色）
    ctx.strokeStyle = `rgba(239, 68, 68, ${baseAlpha})`
    ctx.beginPath()
    for (let i = 0; i < visibleCount; i++) {
      const c = candles[startIdx + i]
      if (c.close >= c.open) continue
      const x = i * stepX + gap / 2 + candleW / 2 + shiftX
      const yHigh = padY + (1 - (c.high - minP) / range) * drawH
      const yLow = padY + (1 - (c.low - minP) / range) * drawH
      ctx.moveTo(x, yHigh)
      ctx.lineTo(x, yLow)
    }
    ctx.stroke()

    // 批量绘制实体（绿色）
    ctx.fillStyle = `rgba(16, 185, 129, ${baseAlpha * 0.75})`
    for (let i = 0; i < visibleCount; i++) {
      const c = candles[startIdx + i]
      if (c.close < c.open) continue
      const x = i * stepX + gap / 2 + shiftX
      const yOpen = padY + (1 - (c.open - minP) / range) * drawH
      const yClose = padY + (1 - (c.close - minP) / range) * drawH
      const bodyTop = Math.min(yOpen, yClose)
      const bodyH = Math.max(Math.abs(yClose - yOpen), 1)
      ctx.fillRect(x, bodyTop, candleW, bodyH)
    }

    // 批量绘制实体（红色）
    ctx.fillStyle = `rgba(220, 38, 38, ${baseAlpha * 0.75})`
    for (let i = 0; i < visibleCount; i++) {
      const c = candles[startIdx + i]
      if (c.close >= c.open) continue
      const x = i * stepX + gap / 2 + shiftX
      const yOpen = padY + (1 - (c.open - minP) / range) * drawH
      const yClose = padY + (1 - (c.close - minP) / range) * drawH
      const bodyTop = Math.min(yOpen, yClose)
      const bodyH = Math.max(Math.abs(yClose - yOpen), 1)
      ctx.fillRect(x, bodyTop, candleW, bodyH)
    }

    // 绘制MA均线
    ctx.strokeStyle = `rgba(251, 191, 36, ${baseAlpha * 0.4})`
    ctx.lineWidth = 1.5
    ctx.beginPath()
    let first = true
    const period = 10
    for (let i = period; i < visibleCount; i++) {
      let sum = 0
      for (let j = 0; j < period; j++) {
        sum += candles[startIdx + i - j].close
      }
      const ma = sum / period
      const x = (i - period / 2) * stepX + gap / 2 + candleW / 2 + shiftX
      const y = padY + (1 - (ma - minP) / range) * drawH
      if (first) { ctx.moveTo(x, y); first = false }
      else ctx.lineTo(x, y)
    }
    ctx.stroke()
  }

  render() {
    if (!this.isReady || this.frames.length === 0) return
    
    this.frameTick++
    if (this.frameTick % this.frameDelay !== 0) return  // 控制播放速度
    
    this.ctx.clearRect(0, 0, this.width, this.height)
    this.ctx.drawImage(this.frames[this.currentFrame], 0, 0)
    this.currentFrame = (this.currentFrame + 1) % this.frames.length
  }

  destroy() {
    // 释放ImageBitmap内存
    this.frames.forEach(bitmap => bitmap.close())
    this.frames = []
  }
}

let candleScroller: LightweightCandleScroller | null = null

function animateKLine() {
  if (!candleScroller) return
  candleScroller.render()
  animFrameId = requestAnimationFrame(animateKLine)
}
// ================================================

const hasError = computed(() => status.value.error_code !== null)
const isTimeout = computed(() => status.value.error_code === 'STARTUP_TIMEOUT')

const dotClass = computed(() => {
  if (status.value.ready) return 'ready'
  if (hasError.value) return 'error'
  return 'loading'
})

const statusText = computed(() => {
  if (status.value.ready) return 'Online'
  if (hasError.value) return 'Error'
  return 'Loading...'
})

const cleanupConnection = () => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  if (retryTimer) {
    clearTimeout(retryTimer)
    retryTimer = null
  }
  if (timeoutTimer) {
    clearTimeout(timeoutTimer)
    timeoutTimer = null
  }
}

const setStartupError = (code: string, message: string) => {
  status.value = {
    ...status.value,
    phase: 'error',
    step: 'error',
    message,
    error_code: code,
    error_message: message,
  }
  emit('error', code, message)
}

const beginStartup = async () => {
  cleanupConnection()

  try {
    const res = await request<{ status: StartupStatus }>('/startup-begin', { method: 'POST' })

    if (!res.ok) {
      retryTimer = setTimeout(beginStartup, 2000)
      return
    }

    // 后端可能已就绪，立即同步一次状态
    if (res.data?.status) {
      status.value = res.data.status
      if (res.data.status.ready) {
        onReady()
        return
      }
      if (res.data.status.error_code) {
        setStartupError(res.data.status.error_code, res.data.status.error_message || 'Startup failed')
        return
      }
    }

    eventSource = new EventSource('/api/startup-logs')

    timeoutTimer = setTimeout(() => {
      cleanupConnection()
      setStartupError('STARTUP_TIMEOUT', 'Connection timeout')
    }, STARTUP_TIMEOUT_MS)

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as StartupStatus & { event?: string }
        if (data.event === 'close') {
          cleanupConnection()
          return
        }
        status.value = data
        if (data.ready) {
          cleanupConnection()
          onReady()
        }
        if (data.error_code) {
          cleanupConnection()
          emit('error', data.error_code, data.error_message || 'Startup failed')
        }
      } catch (e) {
        console.error('[SplashScreen] Failed to parse startup log:', e)
      }
    }

    eventSource.onerror = () => {
      if (readyTriggered) return
      cleanupConnection()
      retryTimer = setTimeout(beginStartup, 2000)
    }
  } catch (e) {
    console.error('[SplashScreen] Startup request failed:', e)
    retryTimer = setTimeout(beginStartup, 2000)
  }
}

const onReady = () => {
  if (readyTriggered) return
  readyTriggered = true

  // 触发过渡状态 - 内容向上滑出，背景保留
  isRevealing.value = true

  // 通知父组件显示主界面（主界面开始淡入）
  setTimeout(() => {
    emit('ready')
  }, 400)

  // 完全移除加载界面
  setTimeout(() => {
    visible.value = false
    isRevealing.value = false
  }, 1200)
}

const retryStartup = () => {
  readyTriggered = false
  status.value = {
    phase: 'idle',
    step: 'initializing',
    message: 'Initializing AquaTrade...',
    progress: 0,
    ready: false,
    error_code: null,
    error_message: null,
  }
  beginStartup()
}

onMounted(() => {
  // 初始化K线画布
  if (klineCanvas.value) {
    candleScroller = new LightweightCandleScroller(klineCanvas.value)
    animateKLine()
  }

  beginStartup()
})

onUnmounted(() => {
  cleanupConnection()
  if (animFrameId) cancelAnimationFrame(animFrameId)
  candleScroller?.destroy()
})
</script>

<style scoped>
.splash-container {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding: 3rem;
  overflow: hidden;
  background: #03080d;
}

.kline-canvas {
  position: absolute;
  inset: 0;
  z-index: 0;
}

.splash-overlay {
  position: absolute;
  inset: 0;
  z-index: 1;
  background: 
    radial-gradient(ellipse at 30% 85%, rgba(6, 182, 212, 0.06) 0%, transparent 50%),
    radial-gradient(ellipse at 75% 20%, rgba(6, 182, 212, 0.03) 0%, transparent 35%);
  pointer-events: none;
}

.splash-grain {
  position: absolute;
  inset: 0;
  z-index: 2;
  pointer-events: none;
  opacity: 0.025;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 400 400' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseFilter'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseFilter)'/%3E%3C/svg%3E");
}

.splash-content {
  position: relative;
  z-index: 3;
  width: 100%;
  max-width: 1200px;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 4rem;
  align-items: end;
}

.hero-section {
  max-width: 700px;
}

.label-line {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 2rem;
}

.line {
  width: 2rem;
  height: 1px;
  background: linear-gradient(90deg, rgba(255,255,255,0.6), transparent);
}

.label-text {
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 0.7rem;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: rgba(255, 255, 255, 0.6);
}

.main-title {
  margin: 0;
  line-height: 0.9;
  letter-spacing: -0.04em;
}

.title-line {
  display: block;
  font-size: clamp(4rem, 10vw, 8rem);
  font-weight: 600;
  color: white;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
}

.title-line.accent {
  color: rgba(6, 182, 212, 0.15);
  -webkit-text-stroke: 1px rgba(6, 182, 212, 0.4);
}

.subtitle {
  margin-top: 1.5rem;
  font-size: 1rem;
  color: rgba(255, 255, 255, 0.5);
  font-weight: 300;
  letter-spacing: 0.02em;
}

.status-panel {
  width: 280px;
  background: rgba(8, 12, 16, 0.75);
  backdrop-filter: blur(24px);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 16px;
  padding: 1.5rem;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1rem;
}

.status-label {
  font-size: 0.75rem;
  color: rgba(255, 255, 255, 0.45);
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

.status-value {
  font-size: 0.85rem;
  font-weight: 600;
  font-family: 'SF Mono', 'Fira Code', monospace;
}

.text-emerald-400 {
  color: #34d399;
}

.progress-track {
  width: 100%;
  height: 4px;
  background: rgba(255, 255, 255, 0.07);
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: 1.5rem;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #06b6d4, #34d399);
  border-radius: 2px;
  transition: width 0.4s cubic-bezier(0.22, 1, 0.36, 1);
  box-shadow: 0 0 12px rgba(52, 211, 153, 0.4);
}

.progress-fill.active {
  animation: pulse-glow 2s ease-in-out infinite;
}

@keyframes pulse-glow {
  0%, 100% { box-shadow: 0 0 12px rgba(52, 211, 153, 0.4); }
  50% { box-shadow: 0 0 20px rgba(52, 211, 153, 0.6); }
}

.panel-metrics {
  display: flex;
  gap: 2rem;
}

.metric-item {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.metric-label {
  font-size: 0.65rem;
  color: rgba(255, 255, 255, 0.3);
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

.metric-value {
  font-size: 0.8rem;
  color: rgba(255, 255, 255, 0.75);
  font-family: 'SF Mono', 'Fira Code', monospace;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  animation: pulse 1.5s ease-in-out infinite;
}

.dot.loading {
  background: #fbbf24;
  box-shadow: 0 0 8px rgba(251, 191, 36, 0.5);
}

.dot.ready {
  background: #34d399;
  box-shadow: 0 0 8px rgba(52, 211, 153, 0.5);
  animation: none;
}

.dot.error {
  background: #ef4444;
  box-shadow: 0 0 8px rgba(239, 68, 68, 0.5);
  animation: none;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.8); }
}

.error-section, .timeout-section {
  position: absolute;
  bottom: 2rem;
  left: 50%;
  transform: translateX(-50%);
  text-align: center;
  padding: 1rem 2rem;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 12px;
}

.error-section p, .timeout-section p {
  margin: 0 0 0.75rem;
  font-size: 0.875rem;
}

.error-code {
  font-family: 'SF Mono', 'Fira Code', monospace;
  color: #fca5a5;
  font-weight: 600;
}

.error-message {
  color: #fecaca;
  font-size: 0.8rem;
}

.timeout-section p {
  color: #fbbf24;
}

.btn-retry {
  padding: 0.5rem 1.25rem;
  border: none;
  border-radius: 8px;
  background: #ef4444;
  color: white;
  font-size: 0.8rem;
  cursor: pointer;
  transition: all 0.2s;
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
}

.btn-retry:hover {
  background: #dc2626;
  transform: translateY(-1px);
}

/* ========== 过渡动画 ========== */
.splash-container {
  position: fixed;
  inset: 0;
  z-index: 9999;
  transition: opacity 0.8s ease, transform 0.8s ease;
}

.splash-container.is-exiting {
  opacity: 0;
  transform: scale(1.05);
  pointer-events: none;
}

.splash-content {
  transition: transform 0.6s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.5s ease;
}

.splash-content.content-exit {
  transform: translateY(-60px);
  opacity: 0;
}
</style>
