<template>
  <div class="skeleton-dashboard">
    <div class="skeleton-header">
      <div class="skeleton-title"></div>
      <div class="skeleton-controls">
        <div class="skeleton-select"></div>
        <div class="skeleton-button"></div>
      </div>
    </div>
    
    <div class="skeleton-content">
      <div class="skeleton-card skeleton-chart-main">
        <div class="skeleton-card-header"></div>
        <div class="skeleton-chart-area"></div>
      </div>
      
      <div class="skeleton-grid">
        <div class="skeleton-card">
          <div class="skeleton-card-header"></div>
          <div class="skeleton-chart-small"></div>
        </div>
        
        <div class="skeleton-card">
          <div class="skeleton-card-header"></div>
          <div class="skeleton-chart-small"></div>
        </div>
      </div>
      
      <div class="skeleton-grid">
        <div class="skeleton-card">
          <div class="skeleton-card-header"></div>
          <div class="skeleton-metrics">
            <div class="skeleton-metric" v-for="i in 4" :key="i">
              <div class="skeleton-metric-value"></div>
              <div class="skeleton-metric-label"></div>
            </div>
          </div>
        </div>
        
        <div class="skeleton-card">
          <div class="skeleton-card-header"></div>
          <div class="skeleton-list">
            <div class="skeleton-list-item" v-for="i in 3" :key="i"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
</script>

<style scoped>
.skeleton-dashboard {
  min-height: 100vh;
  background: #0A0A0A;
  padding: 1.5rem;
}

.skeleton-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1.5rem;
  flex-wrap: wrap;
  gap: 1rem;
}

.skeleton-title {
  width: 200px;
  height: 32px;
  background: linear-gradient(90deg, #1E293B 0%, #334155 50%, #1E293B 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 6px;
}

.skeleton-controls {
  display: flex;
  gap: 1rem;
}

.skeleton-select {
  width: 180px;
  height: 40px;
  background: linear-gradient(90deg, #1E293B 0%, #334155 50%, #1E293B 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 8px;
}

.skeleton-button {
  width: 140px;
  height: 40px;
  background: linear-gradient(90deg, #1E293B 0%, #334155 50%, #1E293B 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 8px;
}

.skeleton-content {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.skeleton-card {
  background: #1E293B;
  border-radius: 12px;
  padding: 1.5rem;
  border: 1px solid rgba(255, 255, 255, 0.05);
}

.skeleton-card-header {
  width: 150px;
  height: 24px;
  background: linear-gradient(90deg, #334155 0%, #475569 50%, #334155 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 4px;
  margin-bottom: 1rem;
}

.skeleton-chart-main {
  min-height: 350px;
}

.skeleton-chart-area {
  width: 100%;
  height: 280px;
  background: linear-gradient(90deg, #1E293B 0%, #334155 50%, #1E293B 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 8px;
}

.skeleton-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1.5rem;
}

@media (max-width: 768px) {
  .skeleton-grid {
    grid-template-columns: 1fr;
  }
}

.skeleton-chart-small {
  width: 100%;
  height: 220px;
  background: linear-gradient(90deg, #1E293B 0%, #334155 50%, #1E293B 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 8px;
}

.skeleton-metrics {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 1rem;
}

.skeleton-metric {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.skeleton-metric-value {
  width: 80px;
  height: 28px;
  background: linear-gradient(90deg, #334155 0%, #475569 50%, #334155 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 4px;
}

.skeleton-metric-label {
  width: 60px;
  height: 14px;
  background: linear-gradient(90deg, #334155 0%, #475569 50%, #334155 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 3px;
}

.skeleton-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.skeleton-list-item {
  width: 100%;
  height: 48px;
  background: linear-gradient(90deg, #1E293B 0%, #334155 50%, #1E293B 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 6px;
}

@keyframes shimmer {
  0% {
    background-position: -200% 0;
  }
  100% {
    background-position: 200% 0;
  }
}
</style>
