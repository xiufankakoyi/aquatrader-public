<template>
  <div class="template-grid">
    <button
      v-for="template in templates"
      :key="template.pattern_id"
      class="template-card"
      :class="{ active: template.pattern_id === modelValue }"
      type="button"
      @click="$emit('update:modelValue', template.pattern_id)"
    >
      <span class="template-name">{{ template.pattern_name }}</span>
      <span class="template-desc">{{ template.description }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import type { PatternTemplate } from '@/api/pattern';

defineProps<{
  templates: PatternTemplate[];
  modelValue: string;
}>();

defineEmits<{
  'update:modelValue': [value: string];
}>();
</script>

<style scoped>
.template-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.template-card {
  min-height: 92px;
  padding: 14px;
  border: 1px solid rgba(56, 189, 248, 0.16);
  border-radius: 8px;
  background: #111827;
  color: #cbd5e1;
  text-align: left;
  cursor: pointer;
}

.template-card.active {
  border-color: #22c55e;
  background: rgba(34, 197, 94, 0.1);
}

.template-name {
  display: block;
  color: #f8fafc;
  font-weight: 700;
  margin-bottom: 8px;
}

.template-desc {
  display: block;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1.45;
}

@media (max-width: 960px) {
  .template-grid {
    grid-template-columns: 1fr;
  }
}
</style>
