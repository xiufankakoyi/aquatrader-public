<template>
  <div class="three-segment-time-slider">
    <div class="mb-4">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm text-slate-300">时间区间分配</span>
        <div class="flex items-center gap-4 text-xs text-slate-400">
          <div class="flex items-center gap-1">
            <div class="w-3 h-3 rounded bg-blue-500/60"></div>
            <span>训练</span>
            <span class="font-mono text-blue-300">{{ ((split1).toFixed(2)) }}%</span>
          </div>
          <div class="flex items-center gap-1">
            <div class="w-3 h-3 rounded bg-yellow-500/60"></div>
            <span>验证</span>
            <span class="font-mono text-yellow-300">{{ ((split2 - split1).toFixed(2)) }}%</span>
          </div>
          <div class="flex items-center gap-1">
            <div class="w-3 h-3 rounded bg-green-500/60"></div>
            <span>测试</span>
            <span class="font-mono text-green-300">{{ ((100 - split2).toFixed(2)) }}%</span>
          </div>
        </div>
      </div>
      <div class="text-xs text-slate-500 mb-3 flex justify-between">
        <span>{{ formatDate(trainStartDate) }} ~ {{ formatDate(trainEndDate) }}</span>
        <span>{{ formatDate(valStartDate) }} ~ {{ formatDate(valEndDate) }}</span>
        <span>{{ formatDate(testStartDate) }} ~ {{ formatDate(testEndDate) }}</span>
      </div>
    </div>
    
    <div class="relative select-none touch-none" ref="sliderContainer">
      <div 
        ref="sliderTrack"
        class="slider-track h-8 rounded-lg overflow-hidden relative"
      >
        <div 
          class="absolute top-0 bottom-0 bg-blue-500/60 border-r border-white/10"
          :style="{ left: '0%', width: `${split1}%` }"
        >
          <div v-if="split1 > 10" class="absolute inset-0 flex items-center justify-center text-xs font-semibold text-white/90">
            训练
          </div>
        </div>
        
        <div 
          class="absolute top-0 bottom-0 bg-yellow-500/60 border-r border-white/10"
          :style="{ left: `${split1}%`, width: `${split2 - split1}%` }"
        >
          <div v-if="(split2 - split1) > 10" class="absolute inset-0 flex items-center justify-center text-xs font-semibold text-white/90">
            验证
          </div>
        </div>
        
        <div 
          class="absolute top-0 bottom-0 bg-green-500/60"
          :style="{ left: `${split2}%`, width: `${100 - split2}%` }"
        >
          <div v-if="(100 - split2) > 10" class="absolute inset-0 flex items-center justify-center text-xs font-semibold text-white/90">
            测试
          </div>
        </div>
      </div>

      <div
        class="absolute top-0 bottom-0 w-4 -ml-2 z-20 cursor-col-resize group"
        :style="{ left: `${split1}%` }"
        @mousedown.stop.prevent="startDrag('trainVal', $event)"
        @touchstart.stop.prevent="startDrag('trainVal', $event)"
      >
        <div class="h-full w-1 mx-auto bg-white shadow-[0_0_10px_rgba(0,0,0,0.5)] group-hover:bg-blue-200 transition-colors"></div>
        <div class="absolute -top-6 left-1/2 transform -translate-x-1/2 bg-slate-800 text-white text-[10px] py-0.5 px-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
           {{ formatDate(trainEndDate) }}
        </div>
      </div>
      
      <div
        class="absolute top-0 bottom-0 w-4 -ml-2 z-20 cursor-col-resize group"
        :style="{ left: `${split2}%` }"
        @mousedown.stop.prevent="startDrag('valTest', $event)"
        @touchstart.stop.prevent="startDrag('valTest', $event)"
      >
        <div class="h-full w-1 mx-auto bg-white shadow-[0_0_10px_rgba(0,0,0,0.5)] group-hover:bg-yellow-200 transition-colors"></div>
        <div class="absolute -top-6 left-1/2 transform -translate-x-1/2 bg-slate-800 text-white text-[10px] py-0.5 px-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
           {{ formatDate(valEndDate) }}
        </div>
      </div>

    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';

interface Props {
  startDate: string;
  endDate: string;
  trainStartDate: string;
  trainEndDate: string;
  valStartDate: string;
  valEndDate: string;
  testStartDate: string;
  testEndDate: string;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  'update:trainStartDate': [value: string];
  'update:trainEndDate': [value: string];
  'update:valStartDate': [value: string];
  'update:valEndDate': [value: string];
  'update:testStartDate': [value: string];
  'update:testEndDate': [value: string];
}>();

const sliderTrack = ref<HTMLElement | null>(null);
const dragging = ref<'trainVal' | 'valTest' | null>(null);

// --- 核心状态：使用两个分割点 (0-100) ---
// split1: Train的结束点 / Val的开始点
// split2: Val的结束点 / Test的开始点
const split1 = ref(33.33); 
const split2 = ref(66.66);

// --- 辅助计算 ---
const totalTimestampRange = computed(() => {
  const start = new Date(props.startDate + 'T00:00:00').getTime();
  const end = new Date(props.endDate + 'T00:00:00').getTime();
  return Math.max(1, end - start);
});

// --- 日期 <=> 百分比 转换工具 ---
function getPercentByDate(dateStr: string): number {
  const start = new Date(props.startDate + 'T00:00:00').getTime();
  const current = new Date(dateStr + 'T00:00:00').getTime();
  const range = totalTimestampRange.value;
  const p = ((current - start) / range) * 100;
  return Math.max(0, Math.min(100, p));
}

function getDateByPercent(percent: number): string {
  const start = new Date(props.startDate + 'T00:00:00').getTime();
  const msOffset = (percent / 100) * totalTimestampRange.value;
  const targetDate = new Date(start + msOffset);
  
  const y = targetDate.getFullYear();
  const m = String(targetDate.getMonth() + 1).padStart(2, '0');
  const d = String(targetDate.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

// --- 同步逻辑：Props 改变 -> 更新 UI ---
// 只有在【不拖拽】的时候，才允许 Props 更新 UI，防止冲突
watch(
  () => [props.startDate, props.endDate, props.trainEndDate, props.valEndDate],
  () => {
    if (dragging.value) return; 
    
    // 如果主日期范围改变，需要重新计算分割点
    if (!props.startDate || !props.endDate) return;
    
    // 重新计算分割点位置
    const s1 = props.trainEndDate ? getPercentByDate(props.trainEndDate) : split1.value;
    const s2 = props.valEndDate ? getPercentByDate(props.valEndDate) : split2.value;
    
    // 简单的防抖，防止微小浮点数差异导致 UI 闪烁
    if (Math.abs(s1 - split1.value) > 0.01) split1.value = s1;
    if (Math.abs(s2 - split2.value) > 0.01) split2.value = s2;
  },
  { immediate: true }
);

// 监听主日期范围变化，确保滑块边界同步更新
watch(
  () => [props.startDate, props.endDate],
  () => {
    if (dragging.value) return;
    
    // 当主日期范围改变时，重新计算分割点位置
    // 这会触发上面的 watch，确保 UI 更新
    if (props.startDate && props.endDate && props.trainEndDate && props.valEndDate) {
      const s1 = getPercentByDate(props.trainEndDate);
      const s2 = getPercentByDate(props.valEndDate);
      
      split1.value = s1;
      split2.value = s2;
    }
  },
  { immediate: true }
);

// --- 拖拽逻辑 ---
const startDrag = (handle: 'trainVal' | 'valTest', event: MouseEvent | TouchEvent) => {
  dragging.value = handle;
  document.body.style.cursor = 'col-resize';
};

const handleMouseMove = (event: MouseEvent | TouchEvent) => {
  if (!dragging.value || !sliderTrack.value) return;
  
  const clientX = 'touches' in event ? event.touches[0].clientX : event.clientX;
  const rect = sliderTrack.value.getBoundingClientRect();
  
  // 计算当前鼠标位置的百分比 (0-100)
  let rawPercent = ((clientX - rect.left) / rect.width) * 100;
  rawPercent = Math.max(0, Math.min(100, rawPercent));
  
  // 最小间隔保护 (例如最小保留 5%)
  const MIN_SEGMENT = 5;

  if (dragging.value === 'trainVal') {
    // 移动的是第一个分割点 (Train | Val)
    // 限制：不能小于 0，不能大于第二个分割点 - 最小间隔
    const max = split2.value - MIN_SEGMENT;
    const newSplit1 = Math.min(Math.max(0, rawPercent), max);
    
    split1.value = newSplit1; // 立即更新 UI，极致丝滑
    
    // 转换并 Emit 日期
    const dateStr = getDateByPercent(newSplit1);
    emit('update:trainEndDate', dateStr);
    emit('update:valStartDate', dateStr);
    
  } else if (dragging.value === 'valTest') {
    // 移动的是第二个分割点 (Val | Test)
    // 限制：不能小于第一个分割点 + 最小间隔，不能大于 100
    const min = split1.value + MIN_SEGMENT;
    const newSplit2 = Math.max(Math.min(100, rawPercent), min);
    
    split2.value = newSplit2; // 立即更新 UI
    
    // 转换并 Emit 日期
    const dateStr = getDateByPercent(newSplit2);
    emit('update:valEndDate', dateStr);
    emit('update:testStartDate', dateStr);
  }
};

const handleMouseUp = () => {
  if (dragging.value) {
    dragging.value = null;
    document.body.style.cursor = '';
    // 拖拽结束后，强制用 Props 再校准一次，确保数据一致性
    // (可选：如果不加这行，UI 会停留在鼠标松开的位置，看起来更顺滑)
  }
};

function formatDate(dateStr: string): string {
  if (!dateStr) return '--';
  const date = new Date(dateStr);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

onMounted(() => {
  window.addEventListener('mousemove', handleMouseMove);
  window.addEventListener('mouseup', handleMouseUp);
  window.addEventListener('touchmove', handleMouseMove);
  window.addEventListener('touchend', handleMouseUp);
});

onUnmounted(() => {
  window.removeEventListener('mousemove', handleMouseMove);
  window.removeEventListener('mouseup', handleMouseUp);
  window.removeEventListener('touchmove', handleMouseMove);
  window.removeEventListener('touchend', handleMouseUp);
});
</script>

<style scoped>
.three-segment-time-slider {
  width: 100%;
}
.slider-track {
  box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.4);
  background: #1e293b; /* 默认深色底 */
}
</style>