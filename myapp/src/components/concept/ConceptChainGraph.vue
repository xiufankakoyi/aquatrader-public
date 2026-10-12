<template>
  <section class="chain-panel">
    <div v-if="!concept" class="empty">请选择概念</div>
    <template v-else>
      <h3>{{ concept.concept_name }}</h3>
      <p>{{ concept.description }}</p>
      <div class="group">
        <span class="label">别名</span>
        <span v-for="alias in concept.aliases" :key="alias" class="chip">{{ alias }}</span>
      </div>
      <div class="group">
        <span class="label">上位概念</span>
        <span v-for="parent in concept.parent_concepts" :key="parent" class="chip parent">{{ parent }}</span>
      </div>
      <div class="chain">
        <span v-for="node in concept.industry_chain" :key="node" class="chain-node">{{ node }}</span>
      </div>
      <div class="group">
        <span class="label">关键词</span>
        <span v-for="keyword in concept.keywords" :key="keyword" class="chip keyword">{{ keyword }}</span>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import type { ConceptInfo } from '@/api/concept';

defineProps<{
  concept: ConceptInfo | null;
}>();
</script>

<style scoped>
.chain-panel {
  min-height: 280px;
  padding: 16px;
  border: 1px solid #243244;
  border-radius: 8px;
  background: #0b1120;
}

h3 {
  margin: 0 0 8px;
  color: #f8fafc;
  font-size: 20px;
}

p {
  margin: 0 0 14px;
  color: #94a3b8;
  line-height: 1.6;
}

.group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 10px 0;
}

.label {
  color: #64748b;
  min-width: 64px;
  font-size: 12px;
}

.chip,
.chain-node {
  padding: 4px 8px;
  border-radius: 4px;
  background: rgba(56, 189, 248, 0.12);
  color: #bae6fd;
  font-size: 12px;
}

.chip.parent {
  background: rgba(34, 197, 94, 0.12);
  color: #86efac;
}

.chip.keyword {
  background: rgba(251, 191, 36, 0.12);
  color: #fde68a;
}

.chain {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 14px 0;
}

.chain-node::after {
  content: '→';
  margin-left: 8px;
  color: #64748b;
}

.chain-node:last-child::after {
  content: '';
  margin: 0;
}

.empty {
  display: grid;
  min-height: 240px;
  place-items: center;
  color: #64748b;
}
</style>
