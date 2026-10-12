<template>
  <!--
    LightCodeEditor - 轻量代码预览/编辑器（不依赖 monaco-editor / codemirror）

    适用场景:
      - 策略生成回执中的"代码预览"段
      - 需要轻量展示 + 简单编辑 + 错误行联动
      - 不想引入 ~1.5MB Monaco bundle 的位置

    核心特性:
      - 行号 (gutter)
      - Python 关键字 / 字符串 / 注释高亮 (自实现简易词法分析)
      - 错误行 (errorLine / errors) 在 gutter 标红点 + 边框
      - 复制 / 格式化 / 保存 按钮
      - Python 缩进绝不破坏 (textarea 保留所有空白, 格式化只清理行尾/折叠空行)
  -->
  <div class="light-code-editor" :class="{ 'is-readonly': readonly, 'is-dark': true }">
    <!-- 工具栏 -->
    <div class="lce-toolbar">
      <span class="lce-toolbar__lang">{{ languageLabel }}</span>
      <span v-if="lineCount > 0" class="lce-toolbar__meta">
        {{ lineCount }} 行
        <span v-if="effectiveErrorLine" class="lce-toolbar__err">L{{ effectiveErrorLine }} 异常</span>
      </span>
      <div class="lce-toolbar__actions">
        <button
          type="button"
          class="lce-btn"
          :title="copyTip"
          :aria-label="copyTip"
          @click="handleCopy"
        >
          <span aria-hidden="true">{{ copied ? '✓ ' : '' }}</span>{{ copied ? '已复制' : '复制' }}
        </button>
        <button
          type="button"
          class="lce-btn"
          title="去除行尾空格 / 折叠连续空行（不动相对缩进）"
          aria-label="格式化代码"
          @click="handleFormat"
        >
          格式化
        </button>
        <button
          v-if="!readonly"
          type="button"
          class="lce-btn lce-btn--primary"
          title="触发保存事件 (父组件接住并调保存 API)"
          aria-label="保存代码"
          @click="handleSave"
        >
          保存
        </button>
      </div>
    </div>

    <!-- 主体: gutter + 编辑层 -->
    <div ref="bodyRef" class="lce-body" :style="{ maxHeight: maxBodyHeight }">
      <!-- 行号 gutter -->
      <div
        ref="gutterRef"
        class="lce-gutter"
        aria-hidden="true"
        @click="handleGutterClick($event)"
      >
        <div
          v-for="n in lineCount"
          :key="n"
          class="lce-gutter__line"
          :class="{
            'lce-gutter__line--error': isErrorLine(n),
            'lce-gutter__line--active': activeLine === n
          }"
          :data-line="n"
        >
          <span
            v-if="isErrorLine(n)"
            class="lce-gutter__dot"
            :title="errorTooltipForLine(n)"
            aria-label="错误标记"
          >●</span>
          <span class="lce-gutter__num">{{ n }}</span>
        </div>
      </div>
      <!-- 高亮 + 编辑层 -->
      <div class="lce-pane">
        <!-- 高亮背景层 (只读展示) -->
        <pre
          ref="highlightRef"
          class="lce-highlight"
          :class="['hl-' + language]"
          aria-hidden="true"
        ><code v-html="highlightedCode"></code></pre>
        <!-- 编辑层: 透明 textarea, 保留原生 tab/缩进行为 -->
        <textarea
          ref="textareaRef"
          class="lce-textarea"
          :class="{ 'lce-textarea--readonly': readonly }"
          :value="modelValue"
          :readonly="readonly"
          spellcheck="false"
          autocapitalize="off"
          autocorrect="off"
          :aria-label="`${languageLabel} 代码编辑器`"
          @input="handleInput"
          @scroll="handleScroll"
          @click="updateActiveLine"
          @keyup="updateActiveLine"
          @keydown.tab.prevent="handleTab"
        ></textarea>
      </div>
    </div>

    <!-- 错误说明列表 -->
    <div v-if="hasErrorMessages" class="lce-errors">
      <p
        v-for="(e, idx) in visibleErrors"
        :key="idx"
        class="lce-errors__item"
        :class="'lce-errors__item--' + (e.severity || 'error')"
        @click="jumpToLine(e.line)"
      >
        <strong>L{{ e.line }}</strong>
        <span v-if="e.severity" class="lce-errors__sev">[{{ severityLabel(e.severity) }}]</span>
        <span class="lce-errors__msg">{{ e.message }}</span>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * LightCodeEditor - 轻量代码编辑器
 *
 * 设计原则:
 *  - DRY: 自实现简易 Python 高亮, 不引入 highlight.js / prism 等
 *  - KISS: textarea + 透明叠层, 不实现完整编辑器协议
 *  - 防御性: 字段缺失降级 (lineCount 取 max(1, modelValue 行数))
 *  - 缩进保护: textarea 原生保留空白, 格式化只清理行尾空格 / 折叠空行
 *  - 可访问: 行号标记 aria-hidden, 错误列表用 button 语义 (click 跳转)
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

defineOptions({ name: 'LightCodeEditor' })

/** 单个错误结构 (外部可批量传入) */
export interface EditorError {
  line: number
  message: string
  severity?: 'error' | 'warning' | 'info'
}

interface Props {
  /** 双向绑定的代码字符串 */
  modelValue: string
  /** 语言 (目前只实现 python 高亮, 其它语言用纯文本) */
  language?: 'python' | 'plain'
  /** 只读模式 */
  readonly?: boolean
  /** 错误行号 (1-based, 越界静默忽略) */
  errorLine?: number | null
  /** 错误列表 (gutter 与底部列表联动) */
  errors?: EditorError[]
  /** 主体最大高度 (默认 320px) */
  maxBodyHeight?: string
}

const props = withDefaults(defineProps<Props>(), {
  language: 'python',
  readonly: false,
  errorLine: null,
  errors: () => [],
  maxBodyHeight: '320px',
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'change', value: string): void
  (e: 'save', value: string): void
  (e: 'format', value: string): void
  (e: 'line-click', line: number): void
}>()

// ============================================
// 状态
// ============================================
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const gutterRef = ref<HTMLDivElement | null>(null)
const highlightRef = ref<HTMLPreElement | null>(null)
const bodyRef = ref<HTMLDivElement | null>(null)
const copied = ref(false)
const activeLine = ref(1)
let copiedTimer: ReturnType<typeof setTimeout> | null = null

// ============================================
// 计算属性
// ============================================
const languageLabel = computed(() => (props.language === 'python' ? 'Python' : 'Plain'))

const lines = computed<string[]>(() => {
  const text = props.modelValue || ''
  // split 不丢末尾空行; 不调用 trim, 保留原始空白
  return text.split('\n')
})

const lineCount = computed(() => Math.max(1, lines.value.length))

const effectiveErrorLine = computed<number | null>(() => {
  // 优先取 errors 列表第一个有 line 的; 否则取 errorLine
  if (props.errors && props.errors.length > 0) {
    const first = props.errors.find((e) => e && typeof e.line === 'number')
    if (first) return first.line
  }
  return props.errorLine ?? null
})

const visibleErrors = computed<EditorError[]>(() => {
  const list = (props.errors || []).filter(
    (e) => e && typeof e.line === 'number' && e.line >= 1 && e.line <= lineCount.value
  )
  return list
})

const hasErrorMessages = computed(() => visibleErrors.value.length > 0)

const copyTip = computed(() => (copied.value ? '已复制到剪贴板' : '复制全部代码'))

// ============================================
// Python 简易高亮
// ============================================
const PY_KEYWORDS = new Set<string>([
  'def', 'class', 'import', 'from', 'return', 'if', 'elif', 'else',
  'for', 'while', 'try', 'except', 'finally', 'with', 'as', 'in',
  'not', 'and', 'or', 'is', 'None', 'True', 'False', 'pass',
  'break', 'continue', 'lambda', 'yield', 'global', 'nonlocal',
  'raise', 'async', 'await', 'assert', 'del',
])

const PY_BUILTINS = new Set<string>([
  'print', 'len', 'range', 'str', 'int', 'float', 'list', 'dict',
  'set', 'tuple', 'bool', 'isinstance', 'type', 'open', 'self',
  'super', 'enumerate', 'zip', 'map', 'filter', 'sum', 'min', 'max',
  'abs', 'round', 'sorted', 'reversed', 'any', 'all',
])

/**
 * HTML 转义, 防止高亮层注入
 */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * 高亮单行 Python
 * - 保留前导空白 (防止缩进被替换)
 * - 顺序: 注释 > 字符串(三引号/单引号) > 关键字/数字/标识符
 */
function highlightPythonLine(line: string): string {
  // 1. 提取前导空白 (原样返回, 不高亮)
  const leadingMatch = line.match(/^([ \t]*)/)
  const leading = leadingMatch ? leadingMatch[1] : ''
  let rest = line.slice(leading.length)

  // 2. 整行都是注释
  if (/^\s*#/.test(rest) || (leading === '' && rest.startsWith('#'))) {
    return escapeHtml(leading) + `<span class="hl-c">${escapeHtml(rest)}</span>`
  }

  // 3. 三引号字符串 (行内有 """ / ''')
  // 简单处理: 行内包含三引号开头或结尾, 整行当字符串
  if (/"""/.test(rest) || /'''/.test(rest)) {
    return escapeHtml(leading) + `<span class="hl-s">${escapeHtml(rest)}</span>`
  }

  // 4. 逐 token 处理
  // 模式: 注释 | 字符串 | 数字 | 标识符
  const out: string[] = []
  let i = 0
  while (i < rest.length) {
    const ch = rest[i]

    // 行注释
    if (ch === '#') {
      out.push(`<span class="hl-c">${escapeHtml(rest.slice(i))}</span>`)
      break
    }

    // 字符串 (单/双引号)
    if (ch === '"' || ch === "'") {
      const quote = ch
      // 找匹配的结束 (含转义)
      let j = i + 1
      while (j < rest.length) {
        if (rest[j] === '\\' && j + 1 < rest.length) {
          j += 2
          continue
        }
        if (rest[j] === quote) {
          j++
          break
        }
        j++
      }
      out.push(`<span class="hl-s">${escapeHtml(rest.slice(i, j))}</span>`)
      i = j
      continue
    }

    // 数字
    if (/[0-9]/.test(ch)) {
      let j = i + 1
      while (j < rest.length && /[0-9._eE+\-]/.test(rest[j])) j++
      out.push(`<span class="hl-n">${escapeHtml(rest.slice(i, j))}</span>`)
      i = j
      continue
    }

    // 标识符 (关键字/内置/函数名/普通名)
    if (/[A-Za-z_]/.test(ch)) {
      let j = i + 1
      while (j < rest.length && /[A-Za-z0-9_]/.test(rest[j])) j++
      const word = rest.slice(i, j)
      let cls = ''
      if (PY_KEYWORDS.has(word)) cls = 'hl-k'
      else if (PY_BUILTINS.has(word)) cls = 'hl-b'
      // def/class 后面的名字高亮为函数名
      else if (i > 0 && /\b(def|class)\s+$/.test(out.join('').replace(/<[^>]+>/g, ''))) {
        cls = 'hl-f'
      }
      if (cls) {
        out.push(`<span class="${cls}">${escapeHtml(word)}</span>`)
      } else {
        out.push(escapeHtml(word))
      }
      i = j
      continue
    }

    // 其它字符原样
    out.push(escapeHtml(ch))
    i++
  }
  return escapeHtml(leading) + out.join('')
}

const highlightedCode = computed<string>(() => {
  if (props.language === 'python') {
    return lines.value.map(highlightPythonLine).join('\n')
  }
  // 纯文本: 仅转义
  return lines.value.map(escapeHtml).join('\n')
})

// ============================================
// 错误行辅助
// ============================================
function isErrorLine(n: number): boolean {
  if (props.errors && props.errors.length > 0) {
    return props.errors.some((e) => e && e.line === n)
  }
  return props.errorLine === n
}

function errorTooltipForLine(n: number): string {
  const e = (props.errors || []).find((x) => x && x.line === n)
  return e ? `L${n}: ${e.message}` : `L${n} 异常`
}

function severityLabel(s: 'error' | 'warning' | 'info' | undefined): string {
  if (s === 'warning') return '警告'
  if (s === 'info') return '提示'
  return '错误'
}

// ============================================
// 事件处理
// ============================================
function handleInput(e: Event): void {
  const value = (e.target as HTMLTextAreaElement).value
  emit('update:modelValue', value)
  emit('change', value)
}

function handleScroll(): void {
  if (!textareaRef.value || !gutterRef.value || !highlightRef.value) return
  const top = textareaRef.value.scrollTop
  gutterRef.value.scrollTop = top
  highlightRef.value.scrollTop = top
  highlightRef.value.scrollLeft = textareaRef.value.scrollLeft
}

function handleTab(e: KeyboardEvent): void {
  // 在光标处插入 4 空格 (不替换现有 tab, 保持 Python 缩进稳定)
  const ta = e.target as HTMLTextAreaElement
  const start = ta.selectionStart
  const end = ta.selectionEnd
  const value = ta.value
  const before = value.slice(0, start)
  const after = value.slice(end)
  const inserted = '    '
  ta.value = before + inserted + after
  ta.selectionStart = ta.selectionEnd = start + inserted.length
  emit('update:modelValue', ta.value)
  emit('change', ta.value)
}

function updateActiveLine(): void {
  if (!textareaRef.value) return
  const ta = textareaRef.value
  const upto = ta.value.slice(0, ta.selectionStart)
  const line = upto.split('\n').length
  if (line >= 1 && line <= lineCount.value) activeLine.value = line
}

function handleGutterClick(e: MouseEvent): void {
  const target = e.target as HTMLElement
  const lineEl = target.closest('.lce-gutter__line') as HTMLElement | null
  if (!lineEl) return
  const n = Number(lineEl.dataset.line)
  if (Number.isFinite(n) && n >= 1 && n <= lineCount.value) {
    jumpToLine(n)
    emit('line-click', n)
  }
}

function jumpToLine(n: number): void {
  if (!textareaRef.value) return
  const ta = textareaRef.value
  const linesArr = ta.value.split('\n')
  let pos = 0
  for (let i = 0; i < Math.min(n - 1, linesArr.length); i++) {
    pos += linesArr[i].length + 1
  }
  ta.focus()
  ta.setSelectionRange(pos, pos)
  // 简单滚动估算: 行高 ~ 18px (CSS line-height: 1.6 * 11px font)
  const lineHeight = 18
  ta.scrollTop = Math.max(0, (n - 3) * lineHeight)
  handleScroll()
  activeLine.value = n
}

// ============================================
// 复制
// ============================================
function handleCopy(): void {
  const text = props.modelValue || ''
  if (!text) return
  // 优先 Clipboard API
  if (navigator?.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(
      () => markCopied(),
      () => fallbackCopy(text)
    )
  } else {
    fallbackCopy(text)
  }
}

function fallbackCopy(text: string): void {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  try {
    document.execCommand('copy')
    markCopied()
  } catch {
    // 静默: 用户可见状态不变
  } finally {
    document.body.removeChild(ta)
  }
}

function markCopied(): void {
  copied.value = true
  if (copiedTimer) clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => {
    copied.value = false
  }, 1500)
}

// ============================================
// 格式化 (缩进保护版)
// 规则:
//   1. 去除每行行尾空白 (trimEnd) — 不会改变前导缩进
//   2. 折叠 3+ 连续空行为 1 个 — 不影响有内容的行
//   3. 绝不改变前导空白字符 (tab 或空格) 的数量和组合
//   4. 保留原文末尾的 1 个换行符 (Python 文件约定)
// ============================================
function handleFormat(): void {
  const text = props.modelValue || ''
  // 记录原文是否以 \n 结尾 (Python 文件约定, 保留单个尾部换行)
  const trailingNewline = text.endsWith('\n')
  const arr = text.split('\n').map((l) => l.replace(/[ \t]+$/g, ''))
  const folded: string[] = []
  let blankRun = 0
  for (const line of arr) {
    if (line === '') {
      blankRun++
      if (blankRun <= 1) folded.push(line)
    } else {
      blankRun = 0
      folded.push(line)
    }
  }
  // 收尾: 去除首尾连续空行, 但保留单空行
  while (folded.length > 0 && folded[0] === '') folded.shift()
  while (folded.length > 0 && folded[folded.length - 1] === '') folded.pop()
  // 恢复单个尾部换行 (如果原文有)
  if (trailingNewline) folded.push('')
  const formatted = folded.join('\n')
  if (formatted !== text) {
    emit('update:modelValue', formatted)
    emit('change', formatted)
  }
  emit('format', formatted)
}

function handleSave(): void {
  emit('save', props.modelValue || '')
}

// ============================================
// 同步 modelValue -> textarea (外部 prop 变更时)
// 关键: 只在值真正变化时同步, 防止输入时回弹
// ============================================
watch(
  () => props.modelValue,
  async (val) => {
    if (!textareaRef.value) return
    if (textareaRef.value.value !== val) {
      textareaRef.value.value = val
      await nextTick()
      handleScroll()
    }
  }
)

// ============================================
// 滚动到 errorLine (外部传入时)
// ============================================
watch(
  () => props.errorLine,
  (n) => {
    if (typeof n === 'number' && n >= 1 && n <= lineCount.value) {
      // 仅滚动, 不抢焦点
      const ta = textareaRef.value
      if (ta) {
        const lineHeight = 18
        ta.scrollTop = Math.max(0, (n - 3) * lineHeight)
        handleScroll()
        activeLine.value = n
      }
    }
  }
)

// 暴露给父组件
defineExpose({
  focus: () => textareaRef.value?.focus(),
  format: handleFormat,
  copy: handleCopy,
  jumpToLine,
})

onBeforeUnmount(() => {
  if (copiedTimer) clearTimeout(copiedTimer)
})
</script>

<style scoped>
/* ============================================
 * 容器
 * ============================================ */
.light-code-editor {
  display: flex;
  flex-direction: column;
  background: #0a0a0a;
  border: 1px solid #2a2e39;
  border-radius: 6px;
  overflow: hidden;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: #d1d4dc;
  /* tab-size 仅用于视觉渲染, 不会改 textarea.value */
  tab-size: 4;
  -moz-tab-size: 4;
}

/* ============================================
 * 工具栏
 * ============================================ */
.lce-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 10px;
  background: #131722;
  border-bottom: 1px solid #2a2e39;
  font-size: 11px;
  user-select: none;
}
.lce-toolbar__lang {
  padding: 1px 8px;
  border-radius: 999px;
  background: rgba(41, 98, 255, 0.2);
  color: #4d8aff;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.lce-toolbar__meta {
  color: #787b86;
}
.lce-toolbar__err {
  margin-left: 8px;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(220, 53, 69, 0.2);
  color: #ff6b78;
  font-weight: 600;
}
.lce-toolbar__actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.lce-btn {
  padding: 3px 10px;
  font-size: 11px;
  background: transparent;
  color: #d1d4dc;
  border: 1px solid #2a2e39;
  border-radius: 3px;
  cursor: pointer;
  transition: all 0.15s;
}
.lce-btn:hover { background: #1a1a1a; }
.lce-btn--primary {
  background: #2962ff;
  color: #fff;
  border-color: #2962ff;
}
.lce-btn--primary:hover { background: #1e4bd8; }

/* ============================================
 * 主体布局
 * ============================================ */
.lce-body {
  display: flex;
  flex: 1;
  min-height: 0;
  background: #0a0a0a;
  overflow: hidden;
  position: relative;
}

/* ============================================
 * 行号 gutter
 * ============================================ */
.lce-gutter {
  flex: 0 0 44px;
  overflow: hidden;
  background: #0a0a0a;
  border-right: 1px solid #2a2e39;
  padding: 8px 0;
  font-size: 11px;
  line-height: 1.6;
  user-select: none;
}
.lce-gutter__line {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
  padding: 0 6px;
  height: 18px;
  color: #4a4e57;
  cursor: pointer;
}
.lce-gutter__line:hover { background: #1a1a1a; color: #787b86; }
.lce-gutter__line--active { color: #d1d4dc; }
.lce-gutter__line--error {
  color: #ff6b78;
  font-weight: 700;
  background: rgba(220, 53, 69, 0.08);
}
.lce-gutter__dot {
  color: #ff6b78;
  font-size: 9px;
  line-height: 1;
}
.lce-gutter__num {
  font-variant-numeric: tabular-nums;
}

/* ============================================
 * 编辑层
 * ============================================ */
.lce-pane {
  flex: 1;
  position: relative;
  overflow: hidden;
}
.lce-highlight,
.lce-textarea {
  position: absolute;
  inset: 0;
  margin: 0;
  padding: 8px 12px;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  /* 关键: pre + white-space: pre 保留所有空白字符 (tab/空格/换行) */
  white-space: pre;
  word-wrap: normal;
  word-break: normal;
  border: 0;
  outline: 0;
  background: transparent;
  overflow: auto;
  tab-size: 4;
  -moz-tab-size: 4;
}
.lce-highlight {
  pointer-events: none;
  color: #d1d4dc;
  z-index: 1;
}
.lce-textarea {
  color: transparent;
  caret-color: #d1d4dc;
  resize: none;
  z-index: 2;
  /* Safari 透明文字仍可滚动 */
  -webkit-text-fill-color: transparent;
}
.lce-textarea--readonly {
  cursor: default;
}

/* ============================================
 * 高亮配色 (Python)
 * ============================================ */
:deep(.hl-k) { color: #c586c0; font-weight: 600; }       /* 关键字 */
:deep(.hl-b) { color: #4ec9b0; }                          /* 内建 */
:deep(.hl-f) { color: #dcdcaa; font-weight: 600; }        /* def/class 后函数名 */
:deep(.hl-s) { color: #ce9178; }                          /* 字符串 */
:deep(.hl-c) { color: #6a9955; font-style: italic; }      /* 注释 */
:deep(.hl-n) { color: #b5cea8; }                          /* 数字 */

/* ============================================
 * 错误列表
 * ============================================ */
.lce-errors {
  border-top: 1px solid rgba(220, 53, 69, 0.3);
  background: rgba(220, 53, 69, 0.05);
  padding: 6px 10px;
  max-height: 100px;
  overflow: auto;
  font-size: 11px;
}
.lce-errors__item {
  margin: 0;
  padding: 2px 0;
  display: flex;
  gap: 6px;
  align-items: baseline;
  cursor: pointer;
  color: #d1d4dc;
}
.lce-errors__item:hover { color: #fff; }
.lce-errors__item strong { color: #ff6b78; font-family: inherit; }
.lce-errors__sev {
  font-size: 10px;
  color: #787b86;
}
.lce-errors__msg { flex: 1; }
.lce-errors__item--warning { color: #ffc107; }
.lce-errors__item--warning strong { color: #ffc107; }
.lce-errors__item--info { color: #4d8aff; }
.lce-errors__item--info strong { color: #4d8aff; }

/* ============================================
 * 只读状态
 * ============================================ */
.is-readonly .lce-textarea { cursor: default; }
</style>
