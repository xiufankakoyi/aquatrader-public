<template>
  <div class="stock-search-input relative">
    <label class="block text-slate-400 text-sm mb-1">{{ label }}</label>
    <div class="relative">
      <input
        ref="inputRef"
        v-model="searchKeyword"
        type="text"
        :placeholder="placeholder"
        :disabled="disabled"
        class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none disabled:opacity-50"
        @input="handleInput"
        @keydown.down.prevent="highlightNext"
        @keydown.up.prevent="highlightPrev"
        @keydown.enter.prevent="selectHighlighted"
        @keydown.esc="closeDropdown"
        @focus="handleFocus"
        @blur="handleBlur"
      />
      <div
        v-if="isLoading"
        class="absolute right-3 top-1/2 transform -translate-y-1/2"
      >
        <i class="fas fa-spinner fa-spin text-slate-500 text-sm"></i>
      </div>
      <div
        v-else-if="searchKeyword && !disabled"
        class="absolute right-3 top-1/2 transform -translate-y-1/2 cursor-pointer"
        @click="clearSearch"
      >
        <i class="fas fa-times text-slate-500 text-sm hover:text-slate-300"></i>
      </div>
    </div>

    <!-- 搜索结果下拉框 -->
    <div
      v-if="showDropdown && filteredResults.length > 0"
      class="absolute z-50 w-full mt-1 bg-[#1e2330] border border-slate-700 rounded-lg shadow-xl max-h-60 overflow-y-auto"
    >
      <div
        v-for="(stock, index) in filteredResults"
        :key="stock.code"
        class="px-4 py-2 cursor-pointer transition-colors"
        :class="{
          'bg-blue-600/30': highlightedIndex === index,
          'hover:bg-slate-700/50': highlightedIndex !== index
        }"
        @mousedown.prevent="selectStock(stock)"
        @mouseenter="highlightedIndex = index"
      >
        <div class="flex justify-between items-center">
          <span class="text-white font-medium">{{ stock.name }}</span>
          <span class="text-slate-400 text-sm font-mono">{{ stock.code }}</span>
        </div>
        <div class="text-slate-500 text-xs mt-0.5">
          {{ stock.market === 'SH' ? '上海' : '深圳' }}
        </div>
      </div>
    </div>

    <!-- 无结果提示 -->
    <div
      v-if="showDropdown && searchKeyword.length >= 2 && filteredResults.length === 0 && !isLoading"
      class="absolute z-50 w-full mt-1 bg-[#1e2330] border border-slate-700 rounded-lg shadow-xl px-4 py-3"
    >
      <span class="text-slate-500 text-sm">未找到匹配的股票</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import { request } from '../services/api';

/**
 * 股票信息接口
 */
interface StockInfo {
  code: string;
  name: string;
  symbol: string;
  market: 'SH' | 'SZ';
}

/**
 * 组件 Props
 */
const props = defineProps<{
  modelValue: string;
  label?: string;
  placeholder?: string;
  disabled?: boolean;
}>();

/**
 * 组件事件
 */
const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void;
  (e: 'select', stock: StockInfo): void;
}>();

// 内部状态
const searchKeyword = ref(props.modelValue);
const searchResults = ref<StockInfo[]>([]);
const isLoading = ref(false);
const showDropdown = ref(false);
const highlightedIndex = ref(-1);
const inputRef = ref<HTMLInputElement | null>(null);

// 搜索防抖定时器
let debounceTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * 过滤后的结果（确保不重复）
 */
const filteredResults = computed(() => {
  const seen = new Set<string>();
  return searchResults.value.filter(stock => {
    if (seen.has(stock.code)) {
      return false;
    }
    seen.add(stock.code);
    return true;
  });
});

/**
 * 监听外部值变化
 */
watch(() => props.modelValue, (newVal) => {
  if (newVal !== searchKeyword.value) {
    searchKeyword.value = newVal;
  }
});

/**
 * 处理输入事件 - 防抖搜索
 */
const handleInput = () => {
  emit('update:modelValue', searchKeyword.value);

  // 清除之前的定时器
  if (debounceTimer) {
    clearTimeout(debounceTimer);
  }

  // 重置高亮索引
  highlightedIndex.value = -1;

  // 如果输入为空，清空结果
  if (!searchKeyword.value.trim()) {
    searchResults.value = [];
    showDropdown.value = false;
    return;
  }

  // 至少输入2个字符才搜索
  if (searchKeyword.value.trim().length < 2) {
    searchResults.value = [];
    showDropdown.value = false;
    return;
  }

  // 防抖搜索 (300ms)
  debounceTimer = setTimeout(() => {
    performSearch(searchKeyword.value.trim());
  }, 300);
};

/**
 * 执行股票搜索
 */
const performSearch = async (keyword: string) => {
  if (!keyword) return;

  isLoading.value = true;
  try {
    const res = await request<any>(`/stocks/search?keyword=${encodeURIComponent(keyword)}`);

    if (res.ok) {
      searchResults.value = res.data || [];
      showDropdown.value = searchResults.value.length > 0;
      highlightedIndex.value = searchResults.value.length > 0 ? 0 : -1;
    }
  } catch (error) {
    console.error('搜索股票失败:', error);
    searchResults.value = [];
  } finally {
    isLoading.value = false;
  }
};

/**
 * 处理焦点事件
 */
const handleFocus = () => {
  if (searchResults.value.length > 0) {
    showDropdown.value = true;
  }
};

/**
 * 处理失焦事件
 */
const handleBlur = () => {
  // 延迟关闭下拉框，以便点击事件能够触发
  setTimeout(() => {
    showDropdown.value = false;
  }, 200);
};

/**
 * 关闭下拉框
 */
const closeDropdown = () => {
  showDropdown.value = false;
  highlightedIndex.value = -1;
};

/**
 * 清除搜索
 */
const clearSearch = () => {
  searchKeyword.value = '';
  searchResults.value = [];
  showDropdown.value = false;
  highlightedIndex.value = -1;
  emit('update:modelValue', '');
  inputRef.value?.focus();
};

/**
 * 高亮下一项
 */
const highlightNext = () => {
  if (!showDropdown.value || filteredResults.value.length === 0) return;
  highlightedIndex.value = (highlightedIndex.value + 1) % filteredResults.value.length;
};

/**
 * 高亮上一项
 */
const highlightPrev = () => {
  if (!showDropdown.value || filteredResults.value.length === 0) return;
  highlightedIndex.value = highlightedIndex.value <= 0
    ? filteredResults.value.length - 1
    : highlightedIndex.value - 1;
};

/**
 * 选择高亮的股票
 */
const selectHighlighted = () => {
  if (highlightedIndex.value >= 0 && highlightedIndex.value < filteredResults.value.length) {
    selectStock(filteredResults.value[highlightedIndex.value]);
  }
};

/**
 * 选择股票
 */
const selectStock = (stock: StockInfo) => {
  searchKeyword.value = stock.code;
  emit('update:modelValue', stock.code);
  emit('select', stock);
  showDropdown.value = false;
  highlightedIndex.value = -1;
};
</script>

<style scoped>
.stock-search-input {
  position: relative;
}

/* 自定义滚动条 */
.overflow-y-auto::-webkit-scrollbar {
  width: 6px;
}

.overflow-y-auto::-webkit-scrollbar-track {
  background: #1e2330;
}

.overflow-y-auto::-webkit-scrollbar-thumb {
  background: #475569;
  border-radius: 3px;
}

.overflow-y-auto::-webkit-scrollbar-thumb:hover {
  background: #64748b;
}
</style>
