<template>
  <div class="w-full bg-gray-200 dark:bg-slate-700 rounded h-2 overflow-hidden">
    <div
      class="bg-blue-500 h-full transition-all duration-300"
      :style="{ width: percentage !== null ? `${percentage}%` : '100%' }"
    ></div>
  </div>
</template>

<script setup lang="ts">
import { defineProps } from 'vue';

const props = defineProps({
  // percentage from 0 to 100, null for indeterminate (full width spinner)
  percentage: {
    type: Number,
    required: false,
    default: null,
  },
});
</script>

<style scoped>
/* No extra styles needed; uses Tailwind utility classes */
</style>
