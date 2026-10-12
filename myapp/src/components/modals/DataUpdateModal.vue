<template>
  <div v-if="isVisible" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
    <div class="bg-[#151925] border border-slate-800 rounded-2xl w-full max-w-md shadow-2xl shadow-indigo-500/10 overflow-hidden">
      <!-- Header -->
      <div class="p-5 border-b border-slate-800 flex justify-between items-center bg-gradient-to-r from-indigo-500/10 to-transparent">
        <div class="flex items-center gap-3">
          <div class="w-8 h-8 rounded-lg bg-indigo-500/20 flex items-center justify-center">
            <i class="fas fa-database text-indigo-400"></i>
          </div>
          <h3 class="text-lg font-semibold text-white">数据库增量更新</h3>
        </div>
        <button @click="hideToBackground" class="text-slate-400 hover:text-white p-2 transition-colors">
          <i class="fas fa-compress-alt"></i>
        </button>
      </div>

      <!-- Content -->
      <div class="p-6 space-y-6">
        <div v-if="status === 'IDLE'" class="text-center py-4">
          <p class="text-slate-300 text-sm mb-6">准备从 Tushare 同步最新市场数据至 LanceDB</p>
          <button 
            @click="startUpdate"
            class="px-8 py-3 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl font-semibold transition-all shadow-lg shadow-indigo-500/20 active:scale-95"
          >
            立即开始更新
          </button>
        </div>

        <div v-else class="space-y-4">
          <!-- Progress Bar Area -->
          <div class="space-y-2">
            <div class="flex justify-between text-xs">
              <span class="text-slate-400">{{ statusText }}</span>
              <span class="font-mono text-indigo-400">{{ Math.floor(progress) }}%</span>
            </div>
            <div class="h-2 bg-slate-800 rounded-full overflow-hidden">
              <div 
                class="h-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all duration-500 ease-out shadow-[0_0_10px_rgba(99,102,241,0.5)]"
                :style="{ width: `${progress}%` }"
              ></div>
            </div>
          </div>

          <!-- Status Message -->
          <div class="bg-black/20 rounded-lg p-3 border border-slate-800/50 min-h-[60px] flex items-center">
            <p class="text-xs text-slate-300 leading-relaxed italic">
              {{ message }}
            </p>
          </div>

          <!-- Controls -->
          <div class="flex justify-center pt-2">
            <button 
              v-if="status === 'COMPLETED' || status === 'FAILED'"
              @click="close"
              class="px-6 py-2 bg-slate-800 hover:bg-slate-700 text-white rounded-lg text-sm transition-colors"
            >
              关闭
            </button>
            <p v-else class="text-[10px] text-slate-500 animate-pulse">
              更新过程中请勿刷新页面，可点击右上角最小化至后台...
            </p>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Background Notification Bubble -->
  <Transition name="slide-up">
    <div 
      v-if="isMinimized && !isVisible" 
      @click="show"
      class="fixed bottom-6 right-6 z-50 bg-indigo-600 text-white px-4 py-3 rounded-full shadow-xl shadow-indigo-500/30 cursor-pointer hover:scale-105 transition-transform flex items-center gap-3 border border-indigo-400/30"
    >
      <div class="relative">
        <i class="fas fa-spinner fa-spin"></i>
        <span class="absolute -top-1 -right-1 w-2 h-2 bg-green-400 rounded-full animate-ping"></span>
      </div>
      <span class="text-xs font-semibold">数据库更新中 ({{ Math.floor(progress) }}%)</span>
    </div>
  </Transition>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue';
import { request } from '../../services/api';
import { useSocketIO } from '../../composables/useSocketIO';

const props = defineProps<{
  initialVisible?: boolean;
}>();

const emit = defineEmits(['close']);

const isVisible = ref(props.initialVisible ?? false);
const isMinimized = ref(false);
const status = ref('IDLE'); // IDLE, FETCHING, UPDATING, COMPLETED, FAILED
const progress = ref(0);
const message = ref('准备就绪');

const { connect, onEvent } = useSocketIO();

// 保存取消监听的函数
let unsubscribeProgress: (() => void) | null = null;

const statusText = computed(() => {
  switch (status.value) {
    case 'FETCHING': return '正在获取交易日历...';
    case 'UPDATING': return '正在同步行情数据...';
    case 'COMPLETED': return '已完成';
    case 'FAILED': return '更新失败';
    default: return '就绪';
  }
});

const startUpdate = async () => {
    status.value = 'FETCHING';
    progress.value = 5;
    message.value = '正在联系服务器启动同步任务...';
    
    try {
        const res = await request('/db/update', { method: 'POST' });
        if (!res.ok) {
            status.value = 'FAILED';
            message.value = `启动任务失败: ${res.message}`;
        }
    } catch (err: any) {
        status.value = 'FAILED';
        message.value = `启动任务失败: ${err.message}`;
    }
};

const hideToBackground = () => {
  isVisible.value = false;
  isMinimized.value = true;
};

const show = () => {
  isVisible.value = true;
};

const close = () => {
  if (status.value === 'COMPLETED' || status.value === 'FAILED') {
    status.value = 'IDLE';
    progress.value = 0;
    message.value = '准备就绪';
    isMinimized.value = false;
  }
  isVisible.value = false;
  emit('close');
};

const handleProgressUpdate = (data: any) => {
  if (data.status) status.value = data.status;
  if (data.progress !== undefined) progress.value = data.progress;
  if (data.message) message.value = data.message;
  
  if (data.status === 'COMPLETED' || data.status === 'FAILED') {
      isMinimized.value = false; // 任务结束，不再显示气泡
  } else {
      isMinimized.value = true; // 只要任务在运行且弹窗隐藏，就显示气泡
  }
};

onMounted(() => {
  // 使用相对路径 ''，让 Socket.IO 自动使用当前域名
  // 这样 Vite 代理可以正确代理 /socket.io 请求到后端
  connect('');
  // 使用 onEvent 方法监听事件，它返回一个取消监听的函数
  unsubscribeProgress = onEvent('db_update_progress', handleProgressUpdate);
});

onUnmounted(() => {
  // 取消监听
  if (unsubscribeProgress) {
    unsubscribeProgress();
    unsubscribeProgress = null;
  }
});

defineExpose({ show });
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

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
