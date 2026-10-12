<template>
  <div class="industry-toolbar">
    <div class="toolbar-row">
      <div class="chain-selector">
        <label>产业链</label>
        <select :value="currentChainId" @change="$emit('selectChain', ($event.target as HTMLSelectElement).value)">
          <option v-for="chain in chains" :key="chain.chain_id" :value="chain.chain_id">
            {{ chain.name }}
          </option>
        </select>
      </div>

      <div class="search-box">
        <input
          v-model="localKeyword"
          type="text"
          placeholder="搜索节点，例如：光模块 / 磷化铟 / InP / CPO"
          @keydown.enter="$emit('search')"
        />
        <button type="button" title="搜索节点" @click="$emit('search')">
          <i class="fas fa-search"></i>
        </button>
      </div>
    </div>

    <div v-if="message" class="search-message">{{ message }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { ChainInfo } from '@/api/industryChain';

interface Props {
  chains: ChainInfo[];
  currentChainId: string;
  keyword: string;
  message?: string;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  (e: 'selectChain', chainId: string): void;
  (e: 'search'): void;
  (e: 'update:keyword', value: string): void;
}>();

const localKeyword = computed({
  get: () => props.keyword,
  set: (val) => emit('update:keyword', val),
});
</script>

<style scoped>
.industry-toolbar {
  margin-top: 14px;
  padding: 12px 14px;
  border: 1px solid rgba(71, 85, 105, 0.45);
  border-radius: 8px;
  background: #0f172a;
}

.toolbar-row {
  display: flex;
  align-items: center;
  gap: 16px;
}

.chain-selector {
  display: flex;
  align-items: center;
  gap: 8px;
}

.chain-selector label {
  font-size: 13px;
  color: #94a3b8;
}

.chain-selector select {
  padding: 6px 12px;
  border: 1px solid #334155;
  border-radius: 6px;
  background: #1e293b;
  color: #e2e8f0;
  font-size: 13px;
  cursor: pointer;
}

.search-box {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.search-box input {
  padding: 8px 12px;
  width: min(34vw, 360px);
  border: 1px solid #334155;
  border-radius: 6px;
  background: #1e293b;
  color: #e2e8f0;
  font-size: 13px;
}

.search-box input::placeholder {
  color: #475569;
}

.search-box button {
  width: 36px;
  height: 34px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: #334155;
  color: #e2e8f0;
  cursor: pointer;
  transition: background 0.2s;
}

.search-box button:hover {
  background: #475569;
}

.search-message {
  margin-top: 8px;
  color: #fbbf24;
  font-size: 12px;
}

@media (max-width: 760px) {
  .toolbar-row {
    flex-direction: column;
    align-items: stretch;
  }

  .chain-selector,
  .search-box {
    width: 100%;
  }

  .chain-selector select,
  .search-box input {
    width: 100%;
  }
}
</style>
