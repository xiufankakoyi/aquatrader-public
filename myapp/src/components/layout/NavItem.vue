<template>
  <router-link
    :to="item.to"
    class="group relative flex items-center rounded-lg transition-all duration-200"
    :class="[
      isCollapsed ? 'justify-center px-2 py-2.5' : 'px-3 py-2',
      item.isActive
        ? 'bg-indigo-500/20 text-indigo-400'
        : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
    ]"
  >
    <!-- Icon -->
    <i
      class="fas text-sm transition-transform duration-200"
      :class="[
        item.icon,
        isCollapsed ? 'text-base' : 'w-5',
        item.isActive && 'text-indigo-400'
      ]"
    ></i>

    <!-- Label (expanded only) -->
    <span
      v-if="!isCollapsed"
      class="ml-3 text-xs font-medium whitespace-nowrap"
    >
      {{ item.label }}
    </span>

    <!-- Tooltip (collapsed + hover only) -->
    <div
      v-if="isCollapsed"
      class="absolute left-full ml-2 px-2.5 py-1.5 bg-slate-800 text-slate-200 text-xs font-medium rounded-md whitespace-nowrap opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all duration-200 z-50 shadow-lg border border-slate-700"
    >
      {{ item.label }}
      <!-- Tooltip arrow -->
      <div class="absolute left-0 top-1/2 -translate-x-1 -translate-y-1/2 w-0 h-0 border-y-4 border-y-transparent border-r-4 border-r-slate-800"></div>
    </div>

    <!-- Active indicator (collapsed) -->
    <div
      v-if="isCollapsed && item.isActive"
      class="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 bg-indigo-400 rounded-r-full"
    ></div>
  </router-link>
</template>

<script setup lang="ts">
const props = defineProps<{
  item: {
    to: string;
    icon: string;
    label: string;
    isActive: boolean;
  };
  isCollapsed: boolean;
  isHovered: boolean;
}>();
</script>
