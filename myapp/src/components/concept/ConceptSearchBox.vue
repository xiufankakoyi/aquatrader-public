<template>
  <form class="search-box" @submit.prevent="$emit('search')">
    <i class="fas fa-search"></i>
    <input
      :value="modelValue"
      placeholder="输入概念关键词，例如：磷化铟、InP、CPO"
      @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />
    <button type="submit" :disabled="loading">{{ loading ? '搜索中...' : '搜索' }}</button>
  </form>
</template>

<script setup lang="ts">
defineProps<{
  modelValue: string;
  loading?: boolean;
}>();

defineEmits<{
  'update:modelValue': [value: string];
  search: [];
}>();
</script>

<style scoped>
.search-box {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid #243244;
  border-radius: 8px;
  background: #0b1120;
}

i {
  color: #38bdf8;
}

input {
  flex: 1;
  height: 36px;
  border: 0;
  outline: none;
  background: transparent;
  color: #e2e8f0;
}

button {
  height: 34px;
  padding: 0 14px;
  border: 0;
  border-radius: 6px;
  background: #16a34a;
  color: white;
  font-weight: 700;
  cursor: pointer;
}

button:disabled {
  opacity: 0.55;
}
</style>
