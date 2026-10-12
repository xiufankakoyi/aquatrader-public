<template>
  <div class="bg-[#151925] rounded-lg p-6 border border-slate-800">
    <p class="text-sm text-slate-400 mb-2">{{ label }}</p>
    <p class="text-3xl font-bold mb-1" :class="valueClass">
      {{ formattedValue }}
    </p>
    <p v-if="subtitle" class="text-xs text-slate-500 mt-1">{{ subtitle }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

interface Props {
  label: string;
  value: number | null;
  subtitle?: string;
  format?: 'percent' | 'number' | 'currency';
  positiveColor?: string;
  negativeColor?: string;
  zeroColor?: string; // 零值颜色
  useOriginalValue?: boolean; // 是否使用原始值判断颜色（用于最大回撤等特殊指标）
  originalValue?: number | null; // 原始值（用于颜色判断）
}

const props = withDefaults(defineProps<Props>(), {
  format: 'number',
  positiveColor: 'text-red-400',
  negativeColor: 'text-green-400',
  zeroColor: 'text-gray-400',
  useOriginalValue: false,
  originalValue: null
});

const hasData = computed(() => {
  return props.value !== null && props.value !== undefined;
});

const formattedValue = computed(() => {
  // 如果没有数据，显示占位符
  if (!hasData.value) {
    return '--';
  }
  
  if (props.format === 'percent') {
    // 对于最大回撤等负值指标，显示正确的符号
    // 注意：props.value 可能已经是绝对值（如在 DashboardOverview 中），但我们应该使用 originalValue 来判断
    // 对于直接传入负值的情况，直接显示符号
    const sign = props.value! >= 0 ? '+' : '';
    
    // 如果是最大回撤等指标，确保显示为负值
    // 检查是否使用了 originalValue 且 originalValue 是负值（表示真实回撤）
    if (props.useOriginalValue && props.originalValue !== null && props.originalValue < 0) {
      // 真实回撤是负值，显示为负值
      return `-${props.value!.toFixed(2)}%`;
    }
    
    return `${sign}${props.value!.toFixed(2)}%`;
  }
  if (props.format === 'currency') {
    return `¥${Math.abs(props.value!).toFixed(2)}`;
  }
  return props.value!.toFixed(2);
});

const valueClass = computed(() => {
  // 如果没有数据，使用灰色占位符样式
  if (!hasData.value) {
    return 'text-gray-500';
  }
  
  // 如果使用原始值判断颜色（用于最大回撤等特殊指标）
  if (props.useOriginalValue && props.originalValue !== null && props.originalValue !== undefined) {
    if (props.originalValue === 0) {
      return props.zeroColor;
    }
    return props.originalValue > 0 ? props.positiveColor : props.negativeColor;
  }
  
  if (props.format === 'percent' || props.format === 'currency') {
    // 如果值为0，使用零值颜色
    if (props.value === 0) {
      return props.zeroColor;
    }
    return props.value! > 0 ? props.positiveColor : props.negativeColor;
  }
  return 'text-white';
});
</script>

