<template>
  <div class="empty-state">
    <p class="empty-text">{{ message || '暂无数据' }}</p>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  message?: string;
}>();
</script>

<style scoped>
.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 200px;
}

.empty-text {
  color: #999;
  font-size: 14px;
}
</style>

