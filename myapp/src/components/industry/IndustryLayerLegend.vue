<script setup lang="ts">
/**
 * 产业链层级图例
 *
 * 展示产业链上中下游各层级的颜色与名称。
 */
interface ChainLayer {
  id: string;
  name: string;
  order?: number;
}

const props = defineProps<{
  layers: ChainLayer[];
}>();

const sortedLayers = () => {
  const list = [...props.layers];
  list.sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
  return list;
};

const layerColor = (id: string) => {
  const palette: Record<string, string> = {
    upstream: '#f59e0b',
    component: '#3b82f6',
    product: '#10b981',
    application: '#8b5cf6',
    theme: '#ef4444',
  };
  return palette[id] ?? '#94a3b8';
};
</script>

<template>
  <div class="industry-layer-legend">
    <div
      v-for="layer in sortedLayers()"
      :key="layer.id"
      class="legend-item"
    >
      <span
        class="legend-dot"
        :style="{ backgroundColor: layerColor(layer.id) }"
      />
      <span class="legend-label">{{ layer.name }}</span>
    </div>
  </div>
</template>

<style scoped>
.industry-layer-legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 1rem;
  padding: 0.75rem 1rem;
  background: rgba(15, 23, 42, 0.6);
  border: 1px solid rgba(148, 163, 184, 0.15);
  border-radius: 0.5rem;
  margin-top: 1rem;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  font-size: 0.8125rem;
  color: #e2e8f0;
}

.legend-dot {
  width: 0.625rem;
  height: 0.625rem;
  border-radius: 50%;
}
</style>
