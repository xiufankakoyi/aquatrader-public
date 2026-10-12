<template>
  <!-- Data Status Alert Modal -->
  <div v-if="isVisible" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
    <div class="bg-[#151925] border border-slate-800 rounded-2xl w-full max-w-md shadow-2xl overflow-hidden">
      <!-- Header -->
      <div :class="headerClass" class="p-5 border-b border-slate-800 flex justify-between items-center">
        <div class="flex items-center gap-3">
          <div :class="iconBgClass" class="w-10 h-10 rounded-xl flex items-center justify-center">
            <i :class="iconClass"></i>
          </div>
          <div>
            <h3 class="text-lg font-semibold text-white">{{ title }}</h3>
            <p class="text-xs text-slate-400">{{ subtitle }}</p>
          </div>
        </div>
        <button @click="dismiss" class="text-slate-400 hover:text-white p-2 transition-colors">
          <i class="fas fa-times"></i>
        </button>
      </div>

      <!-- Content -->
      <div class="p-6 space-y-5">
        <!-- Stock Data Status -->
        <div class="bg-black/30 rounded-xl p-4 space-y-3">
          <h4 class="text-sm font-semibold text-slate-300 flex items-center gap-2">
            <i class="fas fa-chart-line text-indigo-400"></i>
            股票数据 (Tushare)
          </h4>
          <div class="flex justify-between items-center">
            <span class="text-xs text-slate-400">数据库最新</span>
            <span class="text-xs font-mono text-white">{{ status.stock.db_latest_date || '未知' }}</span>
          </div>
          <div class="flex justify-between items-center">
            <span class="text-xs text-slate-400">最新交易日</span>
            <span class="text-xs font-mono text-white">{{ status.stock.api_latest_date || '未知' }}</span>
          </div>
          <div class="flex justify-between items-center">
            <span class="text-xs text-slate-400">状态</span>
            <span :class="getStatusBadgeClass(status.stock.status)" class="text-xs px-2 py-0.5 rounded-full font-semibold">
              {{ getStatusText(status.stock.status) }}
            </span>
          </div>
        </div>

        <!-- DragonEye Status -->
        <div class="bg-black/30 rounded-xl p-4 space-y-3">
          <h4 class="text-sm font-semibold text-slate-300 flex items-center gap-2">
            <i class="fas fa-eye text-orange-400"></i>
            爬虫数据 (DragonEye)
          </h4>
          <div class="flex justify-between items-center">
            <span class="text-xs text-slate-400">最新数据日期</span>
            <span class="text-xs font-mono text-white">{{ status.dragon_eye.latest_date || '未知' }}</span>
          </div>
          <div class="flex justify-between items-center">
            <span class="text-xs text-slate-400">状态</span>
            <span :class="getStatusBadgeClass(status.dragon_eye.status)" class="text-xs px-2 py-0.5 rounded-full font-semibold">
              {{ getStatusText(status.dragon_eye.status) }}
            </span>
          </div>
        </div>

        <!-- Warning Message -->
        <div :class="messageClass" class="rounded-lg p-4 border flex items-start gap-3">
          <i :class="messageIcon" class="mt-0.5"></i>
          <div>
            <p class="text-sm font-medium">{{ messageTitle }}</p>
            <p class="text-xs mt-1 opacity-80">{{ messageDesc }}</p>
          </div>
        </div>

        <!-- Action Buttons -->
        <div class="flex gap-3">
          <button
            v-if="status.overall_status !== 'OK'"
            @click="startFullUpdate"
            :disabled="isUpdating"
            class="flex-1 px-4 py-3 bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-600 disabled:cursor-not-allowed text-white rounded-xl font-semibold transition-all flex items-center justify-center gap-2"
          >
            <i v-if="isUpdating" class="fas fa-spinner fa-spin"></i>
            <i v-else class="fas fa-download"></i>
            {{ isUpdating ? '更新中...' : '更新全部数据' }}
          </button>
          <button
            @click="dismiss"
            class="px-4 py-3 bg-slate-800 hover:bg-slate-700 text-white rounded-xl font-semibold transition-all"
          >
            {{ status.overall_status === 'OK' ? '知道了' : '稍后提醒' }}
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- Background Notification Bubble (when minimized) -->
  <Transition name="slide-up">
    <div
      v-if="isMinimized && !isVisible && status.overall_status !== 'OK'"
      :class="bubbleClass"
      class="fixed bottom-6 right-6 z-50 px-5 py-3 rounded-full shadow-xl cursor-pointer hover:scale-105 transition-transform flex items-center gap-3 border"
      @click="show"
    >
      <i :class="bubbleIcon"></i>
      <span class="text-sm font-semibold">{{ bubbleText }}</span>
      <button @click.stop="dismiss" class="ml-2 opacity-60 hover:opacity-100">
        <i class="fas fa-times text-xs"></i>
      </button>
    </div>
  </Transition>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { request } from '../../services/api';

interface DataStatus {
  stock: {
    db_latest_date: string | null;
    api_latest_date: string | null;
    days_behind: number;
    row_count: number;
    status: 'OK' | 'WARNING' | 'CRITICAL' | 'CHECKING';
    message: string;
  };
  dragon_eye: {
    latest_date: string | null;
    days_behind: number;
    status: 'OK' | 'WARNING' | 'CRITICAL' | 'CHECKING';
    message: string;
  };
  overall_status: 'OK' | 'WARNING' | 'CRITICAL' | 'CHECKING';
  overall_message: string;
}

const emit = defineEmits(['close', 'update-started']);

const isVisible = ref(false);
const isMinimized = ref(false);
const isUpdating = ref(false);
const status = ref<DataStatus>({
  stock: {
    db_latest_date: null,
    api_latest_date: null,
    days_behind: 0,
    row_count: 0,
    status: 'CHECKING',
    message: '检查中...'
  },
  dragon_eye: {
    latest_date: null,
    days_behind: 0,
    status: 'CHECKING',
    message: '检查中...'
  },
  overall_status: 'CHECKING',
  overall_message: '检查中...'
});

const getStatusText = (s: string) => {
  switch (s) {
    case 'OK': return '正常';
    case 'WARNING': return '落后';
    case 'CRITICAL': return '严重';
    default: return '检查中';
  }
};

const getStatusBadgeClass = (s: string) => {
  switch (s) {
    case 'OK': return 'bg-green-500/20 text-green-400';
    case 'WARNING': return 'bg-yellow-500/20 text-yellow-400';
    case 'CRITICAL': return 'bg-red-500/20 text-red-400';
    default: return 'bg-slate-500/20 text-slate-400';
  }
};

const title = computed(() => {
  switch (status.value.overall_status) {
    case 'OK': return '数据状态正常';
    case 'WARNING': return '数据更新提醒';
    case 'CRITICAL': return '数据严重落后';
    default: return '数据状态';
  }
});

const subtitle = computed(() => {
  if (status.value.stock.row_count === 0) return '数据库为空';
  if (status.value.overall_status === 'OK') return '所有数据已是最新';
  if (status.value.overall_status === 'WARNING') return '部分数据落后';
  return '存在严重落后的数据';
});

const headerClass = computed(() => {
  switch (status.value.overall_status) {
    case 'OK': return 'bg-gradient-to-r from-green-500/20 to-transparent';
    case 'WARNING': return 'bg-gradient-to-r from-yellow-500/20 to-transparent';
    case 'CRITICAL': return 'bg-gradient-to-r from-red-500/20 to-transparent';
    default: return 'bg-gradient-to-r from-indigo-500/10 to-transparent';
  }
});

const iconBgClass = computed(() => {
  switch (status.value.overall_status) {
    case 'OK': return 'bg-green-500/20';
    case 'WARNING': return 'bg-yellow-500/20';
    case 'CRITICAL': return 'bg-red-500/20';
    default: return 'bg-indigo-500/20';
  }
});

const iconClass = computed(() => {
  switch (status.value.overall_status) {
    case 'OK': return 'fas fa-check-circle text-green-400 text-lg';
    case 'WARNING': return 'fas fa-exclamation-triangle text-yellow-400 text-lg';
    case 'CRITICAL': return 'fas fa-exclamation-circle text-red-400 text-lg';
    default: return 'fas fa-sync-alt text-indigo-400 text-lg';
  }
});

const messageClass = computed(() => {
  switch (status.value.overall_status) {
    case 'OK': return 'bg-green-500/10 border-green-500/30 text-green-400';
    case 'WARNING': return 'bg-yellow-500/10 border-yellow-500/30 text-yellow-400';
    case 'CRITICAL': return 'bg-red-500/10 border-red-500/30 text-red-400';
    default: return 'bg-indigo-500/10 border-indigo-500/30 text-indigo-400';
  }
});

const messageIcon = computed(() => {
  switch (status.value.overall_status) {
    case 'OK': return 'fas fa-check-circle';
    case 'WARNING': return 'fas fa-exclamation-triangle';
    case 'CRITICAL': return 'fas fa-exclamation-circle';
    default: return 'fas fa-info-circle';
  }
});

const messageTitle = computed(() => {
  if (status.value.stock.row_count === 0) return '数据库为空';
  return status.value.overall_message;
});

const messageDesc = computed(() => {
  if (status.value.stock.row_count === 0) return '请点击"更新全部数据"获取最新市场数据';
  if (status.value.overall_status === 'OK') return '您的数据库包含最新的交易和爬虫数据';
  return '建议更新数据以确保策略使用最新信息';
});

const bubbleClass = computed(() => {
  switch (status.value.overall_status) {
    case 'OK': return 'bg-green-600 border-green-500/30 text-white';
    case 'WARNING': return 'bg-yellow-600 border-yellow-500/30 text-white';
    case 'CRITICAL': return 'bg-red-600 border-red-500/30 text-white';
    default: return 'bg-indigo-600 border-indigo-500/30 text-white';
  }
});

const bubbleIcon = computed(() => {
  switch (status.value.overall_status) {
    case 'OK': return 'fas fa-check-circle';
    case 'WARNING': return 'fas fa-exclamation-triangle';
    case 'CRITICAL': return 'fas fa-exclamation-circle';
    default: return 'fas fa-sync-alt';
  }
});

const bubbleText = computed(() => {
  if (status.value.stock.row_count === 0) return '数据库为空';
  if (status.value.overall_status === 'OK') return '数据已是最新';
  if (status.value.overall_status === 'WARNING') return '部分数据落后';
  return '数据严重落后';
});

const show = () => {
  isVisible.value = true;
  isMinimized.value = false;
};

const dismiss = () => {
  isVisible.value = false;
  if (status.value.overall_status !== 'OK') {
    isMinimized.value = true;
  }
  emit('close');
};

const startFullUpdate = async () => {
  isUpdating.value = true;
  try {
    const res = await request('/db/update/all', { method: 'POST' });
    if (res.ok) {
      emit('update-started');
      dismiss();
    }
  } catch (err) {
    console.error('[DataStatusAlert] Update failed:', err);
  } finally {
    isUpdating.value = false;
  }
};

const checkStatus = async () => {
  try {
    const res = await request<DataStatus>('/db/status');
    if (res.ok) {
      status.value = res.data;
      if (status.value.overall_status !== 'OK') {
        isVisible.value = true;
      }
    }
  } catch (err) {
    console.error('[DataStatusAlert] Status check failed:', err);
  }
};

onMounted(() => {
  checkStatus();
});

defineExpose({ show, checkStatus });
</script>

<style scoped>
.slide-up-enter-active,
.slide-up-leave-active {
  transition: all 0.3s ease-out;
}

.slide-up-enter-from,
.slide-up-leave-to {
  transform: translateY(30px) scale(0.9);
  opacity: 0;
}
</style>
