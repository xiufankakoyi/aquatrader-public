<template>
  <div class="range-slider-container">
    <div v-if="label || true" class="flex items-center justify-between mb-3">
      <div class="flex items-center gap-2">
        <span v-if="label" class="text-sm font-medium text-slate-200">{{ label }}</span>
        <span v-if="locked" class="text-xs text-slate-500">🔒</span>
      </div>
      <div class="flex items-center gap-2 text-xs font-mono">
        <input
          type="text"
          :value="editingMin ? minInputValue : formatValue(displayMinValue)"
          @input="handleMinInput"
          @blur="handleMinBlur"
          @keydown.enter="handleMinBlur"
          @focus="() => { editingMin = true; minInputValue = formatValue(displayMinValue); }"
          class="px-3 py-1.5 rounded-lg bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 font-semibold shadow-sm w-20 text-center focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
          :disabled="locked"
        />
        <span class="text-slate-500 font-bold">——</span>
        <input
          type="text"
          :value="editingMax ? maxInputValue : formatValue(displayMaxValue)"
          @input="handleMaxInput"
          @blur="handleMaxBlur"
          @keydown.enter="handleMaxBlur"
          @focus="() => { editingMax = true; maxInputValue = formatValue(displayMaxValue); }"
          class="px-3 py-1.5 rounded-lg bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 font-semibold shadow-sm w-20 text-center focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
          :disabled="locked"
        />
      </div>
    </div>
    
    <div class="relative" ref="sliderContainer">
      <!-- 背景轨道 -->
      <div 
        ref="sliderTrack"
        class="slider-track"
        :class="{ 'slider-track-locked': locked }"
        :style="{ background: trackBackground }"
        @mousedown="handleTrackClick"
        @touchstart="handleTrackClick"
      >
        <!-- 最小值滑块 -->
        <div
          class="slider-handle"
          :class="{ 'slider-handle-active': dragging === 'min' }"
          :style="{ left: `${minPercent}%` }"
          @mousedown.stop="startDrag('min', $event)"
          @touchstart.stop="startDrag('min', $event)"
        >
          <div class="slider-handle-tooltip">
            {{ formatValue(displayMinValue) }}{{ unit }}
          </div>
        </div>
        
        <!-- 最大值滑块 -->
        <div
          class="slider-handle"
          :class="{ 'slider-handle-active': dragging === 'max' }"
          :style="{ left: `${maxPercent}%` }"
          @mousedown.stop="startDrag('max', $event)"
          @touchstart.stop="startDrag('max', $event)"
        >
          <div class="slider-handle-tooltip">
            {{ formatValue(displayMaxValue) }}{{ unit }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';

interface Props {
  label: string;
  unit?: string;
  absoluteMin: number;
  absoluteMax: number;
  minValue: number;
  maxValue: number;
  step?: number;
  locked?: boolean;
  trackColor?: string;
  activeColor?: string;
  useLogScale?: boolean; // 是否使用对数轴
}

const props = withDefaults(defineProps<Props>(), {
  unit: '',
  step: 0.01,
  locked: false,
  trackColor: 'rgba(148, 163, 184, 0.2)',
  activeColor: 'rgba(99, 102, 241, 0.5)',
  useLogScale: false,
});

const emit = defineEmits<{
  'update:minValue': [value: number];
  'update:maxValue': [value: number];
}>();

const dragging = ref<'min' | 'max' | null>(null);
const sliderTrack = ref<HTMLElement | null>(null);

// 输入框编辑状态
const editingMin = ref(false);
const editingMax = ref(false);
const minInputValue = ref<string>('');
const maxInputValue = ref<string>('');

// === 核心：本地视觉状态（UI层，0-100%线性） ===
// 拖动时，滑块位置完全由这个本地状态控制，不受 props 影响
const localMinPercent = ref<number>(0);
const localMaxPercent = ref<number>(100);

// 格式化值，根据步长判断是否为整数
const formatValue = (value: number): string => {
  // 如果步长 >= 1，认为是整数参数
  if (props.step && props.step >= 1) {
    return Math.round(value).toString();
  }
  // 如果步长 < 1，显示小数，但最多2位
  if (props.step && props.step < 1) {
    // 如果步长很小（如0.01），显示2位小数
    if (props.step <= 0.01) {
      return value.toFixed(2);
    }
    // 如果步长较大（如0.1），显示1位小数
    return value.toFixed(1);
  }
  // 默认显示2位小数
  return value.toFixed(2);
};

// === 对数轴转换函数（只在边界使用） ===
// Logic -> UI: 将数值转换为百分比
const valueToPercent = (value: number, min: number, max: number): number => {
  if (props.useLogScale) {
    if (min <= 0 || max <= 0 || value <= 0) return 0;
    const logMin = Math.log10(min);
    const logMax = Math.log10(max);
    const logValue = Math.log10(value);
    return ((logValue - logMin) / (logMax - logMin)) * 100;
  }
  const range = max - min;
  if (range <= 0) return 0;
  return ((value - min) / range) * 100;
};

// UI -> Logic: 将百分比转换为数值
const percentToValue = (percent: number, min: number, max: number): number => {
  const clampedPercent = Math.max(0, Math.min(100, percent)) / 100;
  if (props.useLogScale) {
    if (min <= 0 || max <= 0) return min;
    const logMin = Math.log10(min);
    const logMax = Math.log10(max);
    const logValue = logMin + clampedPercent * (logMax - logMin);
    return Math.pow(10, logValue);
  }
  const range = max - min;
  if (range <= 0) return min;
  return min + clampedPercent * range;
};

// === 滑块位置计算：拖动时用本地状态，非拖动时用 props ===
const minPercent = computed(() => {
  // 拖动时：完全使用本地状态，切断 prop 影响
  if (dragging.value === 'min') {
    return localMinPercent.value;
  }
  // 非拖动时：从 props 同步到本地状态（用于初始化或外部更新）
  const percent = valueToPercent(props.minValue, props.absoluteMin, props.absoluteMax);
  localMinPercent.value = percent;
  return percent;
});

const maxPercent = computed(() => {
  // 拖动时：完全使用本地状态，切断 prop 影响
  if (dragging.value === 'max') {
    return localMaxPercent.value;
  }
  // 非拖动时：从 props 同步到本地状态（用于初始化或外部更新）
  const percent = valueToPercent(props.maxValue, props.absoluteMin, props.absoluteMax);
  localMaxPercent.value = percent;
  return percent;
});

// === 显示值：拖动时从本地状态计算，非拖动时用 props ===
const displayMinValue = computed(() => {
  if (dragging.value === 'min') {
    // 拖动时：从本地百分比计算显示值
    return percentToValue(localMinPercent.value, props.absoluteMin, props.absoluteMax);
  }
  return props.minValue;
});

const displayMaxValue = computed(() => {
  if (dragging.value === 'max') {
    // 拖动时：从本地百分比计算显示值
    return percentToValue(localMaxPercent.value, props.absoluteMin, props.absoluteMax);
  }
  return props.maxValue;
});

const trackBackground = computed(() => {
  const range = props.absoluteMax - props.absoluteMin;
  if (range <= 0) return props.trackColor;
  return `linear-gradient(to right, 
    ${props.trackColor} 0%, 
    ${props.trackColor} ${minPercent.value}%, 
    ${props.activeColor} ${minPercent.value}%, 
    ${props.activeColor} ${maxPercent.value}%, 
    ${props.trackColor} ${maxPercent.value}%, 
    ${props.trackColor} 100%)`;
});

// === 从鼠标位置获取百分比（UI层，纯线性） ===
const getPercentFromPosition = (clientX: number): number => {
  if (!sliderTrack.value) return 0;
  const rect = sliderTrack.value.getBoundingClientRect();
  const rawPercent = ((clientX - rect.left) / rect.width) * 100;
  return Math.max(0, Math.min(100, rawPercent));
};

const startDrag = (handle: 'min' | 'max', event: MouseEvent | TouchEvent) => {
  if (props.locked) {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  
  // === 开启拖动模式：切断 prop 监听 ===
  dragging.value = handle;
  event.preventDefault();
  
  // 立即更新本地视觉状态（0延迟）
  const clientX = ('touches' in event && event.touches && event.touches.length > 0 && event.touches[0])
    ? event.touches[0].clientX 
    : (event as MouseEvent).clientX;
  const percent = getPercentFromPosition(clientX);
  
  if (handle === 'min') {
    // 限制：不能超过 max 的位置
    localMinPercent.value = Math.min(percent, localMaxPercent.value - 0.1);
  } else {
    // 限制：不能小于 min 的位置
    localMaxPercent.value = Math.max(percent, localMinPercent.value + 0.1);
  }
  
  // 异步上报父组件（不阻塞 UI）
  requestAnimationFrame(() => {
    syncToParent(handle);
  });
};

const handleTrackClick = (event: MouseEvent | TouchEvent) => {
  if (props.locked || dragging.value) {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  
  let clientX: number;
  if ('touches' in event && event.touches && event.touches.length > 0 && event.touches[0]) {
    clientX = event.touches[0].clientX;
  } else {
    clientX = (event as MouseEvent).clientX;
  }
  const percent = getPercentFromPosition(clientX);
  
  // 判断点击位置更接近哪个滑块
  const distToMin = Math.abs(percent - localMinPercent.value);
  const distToMax = Math.abs(percent - localMaxPercent.value);
  
  if (distToMin < distToMax) {
    localMinPercent.value = Math.min(percent, localMaxPercent.value - 0.1);
    syncToParent('min');
  } else {
    localMaxPercent.value = Math.max(percent, localMinPercent.value + 0.1);
    syncToParent('max');
  }
};

const handleMouseMove = (event: MouseEvent | TouchEvent) => {
  if (!dragging.value) return;
  
  // === 拖动中：直接更新本地视觉状态（0延迟，60fps丝滑） ===
  let clientX: number;
  if ('touches' in event && event.touches && event.touches.length > 0 && event.touches[0]) {
    clientX = event.touches[0].clientX;
  } else {
    clientX = (event as MouseEvent).clientX;
  }
  const percent = getPercentFromPosition(clientX);
  
  if (dragging.value === 'min') {
    // 限制：不能超过 max 的位置
    localMinPercent.value = Math.min(percent, localMaxPercent.value - 0.1);
  } else {
    // 限制：不能小于 min 的位置
    localMaxPercent.value = Math.max(percent, localMinPercent.value + 0.1);
  }
  
  // 异步上报父组件（使用 requestAnimationFrame 批量更新，不阻塞 UI）
  requestAnimationFrame(() => {
    syncToParent(dragging.value!);
  });
};

// === 同步本地状态到父组件（只在边界做对数转换） ===
const syncToParent = (handle: 'min' | 'max') => {
  if (handle === 'min') {
    // UI -> Logic: 百分比转数值，对齐步长
    let value = percentToValue(localMinPercent.value, props.absoluteMin, props.absoluteMax);
    value = Math.round(value / props.step) * props.step;
    value = Math.max(props.absoluteMin, Math.min(value, props.maxValue - props.step));
    emit('update:minValue', value);
  } else {
    // UI -> Logic: 百分比转数值，对齐步长
    let value = percentToValue(localMaxPercent.value, props.absoluteMin, props.absoluteMax);
    value = Math.round(value / props.step) * props.step;
    value = Math.min(props.absoluteMax, Math.max(value, props.minValue + props.step));
    emit('update:maxValue', value);
  }
};

const handleMouseUp = () => {
  if (dragging.value) {
    // === 松开时：最终同步一次，然后恢复 prop 监听 ===
    syncToParent(dragging.value);
    dragging.value = null;
    // 此时 computed 会自动从 props 同步最新值到本地状态
  }
};

// === 输入框处理函数 ===
const handleMinInput = (event: Event) => {
  const target = event.target as HTMLInputElement | null;
  if (!target) return;
  minInputValue.value = target.value;
  editingMin.value = true;
};

const handleMinBlur = () => {
  editingMin.value = false;
  const inputValue = minInputValue.value.trim();
  
  // 如果输入为空，直接清空，让 computed 显示当前值
  if (!inputValue) {
    minInputValue.value = '';
    return;
  }
  
  // 移除单位（如果有）
  const numericStr = inputValue.replace(/[^\d.-]/g, '');
  const numericValue = parseFloat(numericStr);
  
  // 如果解析失败，清空输入，让 computed 显示当前值
  if (isNaN(numericValue)) {
    minInputValue.value = '';
    return;
  }
  
  // 验证范围
  const clampedValue = Math.max(props.absoluteMin, Math.min(numericValue, props.maxValue - props.step));
  // 对齐步长
  const alignedValue = Math.round(clampedValue / props.step) * props.step;
  
  // 更新本地百分比状态
  const percent = valueToPercent(alignedValue, props.absoluteMin, props.absoluteMax);
  localMinPercent.value = Math.min(percent, localMaxPercent.value - 0.1);
  
  // 同步到父组件
  syncToParent('min');
  minInputValue.value = '';
};

const handleMaxInput = (event: Event) => {
  const target = event.target as HTMLInputElement | null;
  if (!target) return;
  maxInputValue.value = target.value;
  editingMax.value = true;
};

const handleMaxBlur = () => {
  editingMax.value = false;
  const inputValue = maxInputValue.value.trim();
  
  // 如果输入为空，直接清空，让 computed 显示当前值
  if (!inputValue) {
    maxInputValue.value = '';
    return;
  }
  
  // 移除单位（如果有）
  const numericStr = inputValue.replace(/[^\d.-]/g, '');
  const numericValue = parseFloat(numericStr);
  
  // 如果解析失败，清空输入，让 computed 显示当前值
  if (isNaN(numericValue)) {
    maxInputValue.value = '';
    return;
  }
  
  // 验证范围
  const clampedValue = Math.min(props.absoluteMax, Math.max(numericValue, props.minValue + props.step));
  // 对齐步长
  const alignedValue = Math.round(clampedValue / props.step) * props.step;
  
  // 更新本地百分比状态
  const percent = valueToPercent(alignedValue, props.absoluteMin, props.absoluteMax);
  localMaxPercent.value = Math.max(percent, localMinPercent.value + 0.1);
  
  // 同步到父组件
  syncToParent('max');
  maxInputValue.value = '';
};

onMounted(() => {
  document.addEventListener('mousemove', handleMouseMove);
  document.addEventListener('mouseup', handleMouseUp);
  document.addEventListener('touchmove', handleMouseMove);
  document.addEventListener('touchend', handleMouseUp);
});

onUnmounted(() => {
  document.removeEventListener('mousemove', handleMouseMove);
  document.removeEventListener('mouseup', handleMouseUp);
  document.removeEventListener('touchmove', handleMouseMove);
  document.removeEventListener('touchend', handleMouseUp);
});
</script>

<style scoped>
.range-slider-container {
  width: 100%;
}

.slider-track {
  position: relative;
  width: 100%;
  height: 10px;
  border-radius: 5px;
  cursor: pointer;
  transition: all 0.2s ease;
  box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.3);
}

.slider-track:hover:not(.slider-track-locked) {
  height: 12px;
}

.slider-track-locked {
  cursor: not-allowed;
  opacity: 0.6;
}

.slider-handle {
  position: absolute;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 22px;
  height: 22px;
  background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
  border: 3px solid rgba(255, 255, 255, 0.95);
  border-radius: 50%;
  cursor: grab;
  transition: all 0.2s ease;
  z-index: 2;
  box-shadow: 0 2px 8px rgba(99, 102, 241, 0.4), 0 0 0 2px rgba(99, 102, 241, 0.1);
}

.slider-track-locked .slider-handle {
  cursor: not-allowed;
  opacity: 0.5;
  background: linear-gradient(135deg, #475569 0%, #64748b 100%);
}

.slider-handle:hover {
  transform: translate(-50%, -50%) scale(1.2);
  box-shadow: 0 4px 12px rgba(99, 102, 241, 0.6);
}

.slider-handle-active {
  transform: translate(-50%, -50%) scale(1.3);
  cursor: grabbing;
  box-shadow: 0 6px 16px rgba(99, 102, 241, 0.8);
}

.slider-handle-tooltip {
  position: absolute;
  bottom: 100%;
  left: 50%;
  transform: translateX(-50%);
  margin-bottom: 10px;
  padding: 6px 10px;
  background: linear-gradient(135deg, rgba(15, 23, 42, 0.98) 0%, rgba(30, 41, 59, 0.98) 100%);
  border: 1px solid rgba(99, 102, 241, 0.6);
  border-radius: 8px;
  font-size: 11px;
  font-weight: 600;
  color: #e2e8f0;
  white-space: nowrap;
  opacity: 0;
  pointer-events: none;
  transition: all 0.2s ease;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
}

.slider-handle:hover .slider-handle-tooltip,
.slider-handle-active .slider-handle-tooltip {
  opacity: 1;
}

.slider-handle-tooltip::after {
  content: '';
  position: absolute;
  top: 100%;
  left: 50%;
  transform: translateX(-50%);
  border: 5px solid transparent;
  border-top-color: rgba(99, 102, 241, 0.6);
  filter: drop-shadow(0 2px 2px rgba(0, 0, 0, 0.2));
}
</style>