<template>
  <div class="empty-state">
    <strong class="empty-state__title">{{ title }}</strong>
    <p v-if="description" class="empty-state__description">{{ description }}</p>
    <p v-if="hint" class="empty-state__hint">{{ hint }}</p>
  </div>
</template>

<script setup lang="ts">
/**
 * EmptyState - 通用空数据展示
 *
 *  - title: 主标题
 *  - description: 副标题
 *  - hint: 额外提示, 用于补充"为什么空 / 接下来可以做什么"
 */
withDefaults(
  defineProps<{
    title?: string
    description?: string
    hint?: string
  }>(),
  {
    title: '暂无数据',
    description: '暂无本地证据',
    hint: '',
  }
)
</script>

<style scoped>
.empty-state {
  min-height: 140px;
  display: grid;
  place-content: center;
  gap: 8px;
  padding: 18px 20px;
  text-align: center;
  color: #475569;
  background: #f8fafc;
  border: 1px dashed #cbd5e1;
  border-radius: 12px;
}
.empty-state__title { font-size: 15px; color: #334155; }
.empty-state__description { margin: 0; color: #64748b; font-size: 13px; }
.empty-state__hint { margin: 0; color: #94a3b8; font-size: 12px; }
</style>
