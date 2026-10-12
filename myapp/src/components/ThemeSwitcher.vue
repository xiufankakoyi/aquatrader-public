<template>
  <button
    @click="toggleTheme"
    class="w-7 h-7 flex items-center justify-center rounded bg-[#141414] border border-[#2a2a2a] text-gray-500 hover:text-gray-300 hover:border-[#404040] transition-colors"
    aria-label="切换主题"
  >
    <i
      v-if="isDarkTheme"
      class="fas fa-sun text-[11px] text-amber-500"
    ></i>
    <i
      v-else
      class="fas fa-moon text-[11px] text-blue-400"
    ></i>
  </button>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';

const isDarkTheme = ref(true);

function toggleTheme() {
  isDarkTheme.value = !isDarkTheme.value;
  document.documentElement.setAttribute('data-theme', isDarkTheme.value ? 'dark' : 'light');
  localStorage.setItem('theme', isDarkTheme.value ? 'dark' : 'light');
}

onMounted(() => {
  // 从 localStorage 恢复主题设置
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme) {
    isDarkTheme.value = savedTheme === 'dark';
  } else {
    // 自动检测系统主题
    isDarkTheme.value = window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  document.documentElement.setAttribute('data-theme', isDarkTheme.value ? 'dark' : 'light');
});
</script>
