<template>
  <div class="date-range-picker flex items-center gap-3">
    <!-- 快捷选择按钮 -->
    <div class="flex gap-1">
      <button
        v-for="preset in presets"
        :key="preset.value"
        @click="selectPreset(preset.value)"
        :class="[
          'px-3 py-1.5 text-xs rounded-lg transition-all border',
          selectedPreset === preset.value
            ? 'bg-indigo-500/20 text-indigo-400 border-indigo-500/30'
            : 'bg-slate-800/50 text-slate-400 border-slate-700 hover:bg-slate-700/50 hover:text-slate-300'
        ]"
      >
        {{ preset.label }}
      </button>
    </div>

    <!-- 自定义日期范围 -->
    <div class="flex items-center gap-2 bg-slate-800/50 rounded-lg px-3 py-1.5 border border-slate-700">
      <input
        type="date"
        v-model="localStartDate"
        :max="today"
        class="bg-transparent text-sm text-slate-300 outline-none border-none cursor-pointer w-32"
      />
      <span class="text-slate-500">~</span>
      <input
        type="date"
        v-model="localEndDate"
        :max="today"
        class="bg-transparent text-sm text-slate-300 outline-none border-none cursor-pointer w-32"
      />
    </div>

    <!-- 应用按钮 -->
    <button
      @click="applyCustomRange"
      class="px-3 py-1.5 text-xs bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg transition-all"
    >
      应用
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, computed } from 'vue';

/**
 * DateRangePicker - 日期范围选择器组件
 * 
 * 支持快捷日期选择（近7天、近30天、自定义范围）
 * 日期选择后触发事件通知父组件更新数据
 */

interface Props {
  startDate?: string;
  endDate?: string;
}

const props = withDefaults(defineProps<Props>(), {
  startDate: '',
  endDate: ''
});

const emit = defineEmits<{
  'update:startDate': [value: string];
  'update:endDate': [value: string];
  'change': [range: { startDate: string; endDate: string }];
}>();

// 预设选项
const presets = [
  { label: '近7天', value: '7d' },
  { label: '近15天', value: '15d' },
  { label: '近30天', value: '30d' },
  { label: '自定义', value: 'custom' }
];

const today = new Date().toISOString().split('T')[0];
const selectedPreset = ref<string>('15d');
const localStartDate = ref<string>(props.startDate);
const localEndDate = ref<string>(props.endDate);

/**
 * 计算指定天数前的日期
 */
const getDateBefore = (days: number): string => {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return date.toISOString().split('T')[0];
};

/**
 * 选择预设日期范围
 */
const selectPreset = (value: string) => {
  selectedPreset.value = value;
  
  if (value !== 'custom') {
    const days = parseInt(value.replace('d', ''));
    localEndDate.value = today;
    localStartDate.value = getDateBefore(days);
    
    emitChange();
  }
};

/**
 * 应用自定义日期范围
 */
const applyCustomRange = () => {
  if (localStartDate.value && localEndDate.value) {
    selectedPreset.value = 'custom';
    emitChange();
  }
};

/**
 * 触发变更事件
 */
const emitChange = () => {
  emit('update:startDate', localStartDate.value);
  emit('update:endDate', localEndDate.value);
  emit('change', {
    startDate: localStartDate.value,
    endDate: localEndDate.value
  });
};

// 监听 props 变化
watch(
  () => [props.startDate, props.endDate],
  ([start, end]) => {
    if (start) localStartDate.value = start;
    if (end) localEndDate.value = end;
  },
  { immediate: true }
);

// 初始化默认值
if (!props.startDate || !props.endDate) {
  selectPreset('15d');
}
</script>

<style scoped>
/* 隐藏原生日期选择器的默认样式 */
input[type="date"]::-webkit-calendar-picker-indicator {
  filter: invert(0.7);
  cursor: pointer;
}

input[type="date"]::-webkit-calendar-picker-indicator:hover {
  filter: invert(0.9);
}
</style>
