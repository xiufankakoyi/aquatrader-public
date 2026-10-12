<template>
  <aside class="session-sidebar">
    <div class="sidebar-content">
      <slot></slot>
    </div>
  </aside>
</template>

<script setup lang="ts">
// 占位组件，需要根据实际需求完善
</script>

<style scoped>
.session-sidebar {
  position: fixed;
  left: 0;
  top: 56px;
  width: 288px;
  height: calc(100vh - 56px);
  background: white;
  border-right: 1px solid #e5e7eb;
  overflow-y: auto;
}

.sidebar-content {
  padding: 1rem;
}
</style>

