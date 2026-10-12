<template>
  <header class="dashboard-header">
    <div class="header-content">
      <h1 class="header-title">量化回测系统</h1>
      <div class="header-actions">
        <slot name="actions"></slot>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
// 占位组件，需要根据实际需求完善
</script>

<style scoped>
.dashboard-header {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: 56px;
  background: white;
  border-bottom: 1px solid #e5e7eb;
  z-index: 100;
}

.header-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 100%;
  padding: 0 1rem;
}

.header-title {
  font-size: 1.25rem;
  font-weight: 600;
  margin: 0;
}
</style>

