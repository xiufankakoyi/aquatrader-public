<template>
  <div class="bg-[#1c202b] border-t border-[#2a2e39] px-6 py-3 flex items-center gap-6 select-none shadow-inner">
    <!-- Playback Controls -->
    <div class="flex items-center gap-1">
      <button 
        class="w-10 h-10 flex items-center justify-center rounded-full hover:bg-[#2a2e39] text-[#787b86] hover:text-[#d1d4dc] transition-all"
        title="上一步"
        @click="stepBackward"
      >
        <i class="fas fa-step-backward"></i>
      </button>
      
      <button 
        class="w-10 h-10 flex items-center justify-center rounded-full bg-[#2962ff] hover:bg-[#1e53e5] text-white shadow-lg transition-all"
        :title="isPlaying ? '暂停' : '播发'"
        @click="togglePlay"
      >
        <i :class="['fas', isPlaying ? 'fa-pause' : 'fa-play', 'ml-0.5']"></i>
      </button>
      
      <button 
        class="w-10 h-10 flex items-center justify-center rounded-full hover:bg-[#2a2e39] text-[#787b86] hover:text-[#d1d4dc] transition-all"
        title="下一步"
        @click="stepForward"
      >
        <i class="fas fa-step-forward"></i>
      </button>
    </div>

    <!-- Timeline Slider -->
    <div class="flex-1 flex flex-col gap-1">
      <div class="flex items-center justify-between px-1">
        <div class="flex items-center gap-2">
          <span class="text-xs font-bold text-blue-400 font-mono">{{ backtestStore.playbackCursor || '--' }}</span>
          <span v-if="backtestStore.playbackMode" class="text-[10px] text-white/40 bg-white/5 px-1.5 py-0.5 rounded border border-white/10 uppercase tracking-tighter">Playback Active</span>
        </div>
        <span class="text-[10px] text-[#787b86] font-mono">{{ currentIndex + 1 }} / {{ totalSteps }}</span>
      </div>
      
      <div class="relative h-6 flex items-center group">
        <input 
          type="range" 
          :min="0" 
          :max="totalSteps - 1" 
          :value="currentIndex"
          class="timeline-slider"
          @input="handleInput"
        />
        <!-- Progress track (visual only) -->
        <div 
          class="absolute left-0 top-1/2 -translate-y-1/2 h-1 bg-[#2962ff] rounded-full pointer-events-none"
          :style="{ width: (currentIndex / (totalSteps - 1)) * 100 + '%' }"
        ></div>
      </div>

      <!-- Instruction C: Concentration Heatmap -->
      <div v-if="backtestStore.dailyConcentration.length > 0" class="h-1.5 w-full bg-[#0A0A0A] rounded-full overflow-hidden flex gap-[1px]">
        <div 
          v-for="(day, idx) in backtestStore.dailyConcentration" 
          :key="idx"
          class="flex-1 h-full transition-colors duration-300"
          :style="{ 
            backgroundColor: `rgba(99, 102, 241, ${day.score * 0.9 + 0.1})`,
            opacity: day.score > 0 ? 1 : 0.2
          }"
          :title="`${day.date}: ${day.count} 标的`"
        ></div>
      </div>
      <div class="flex items-center justify-between px-1">
        <span class="text-[8px] text-[#787b86] uppercase tracking-tighter">持仓集中度热力图</span>
      </div>
    </div>

    <!-- Snap Follow & Speed Selector -->
    <div class="flex items-center gap-3">
      <button 
        class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border transition-all"
        :class="backtestStore.playbackSnap ? 'bg-indigo-500/20 border-indigo-500/50 text-indigo-400 shadow-[0_0_10px_rgba(99,102,241,0.2)]' : 'bg-[#2a2e39]/50 border-[#2a2e39] text-[#787b86] hover:bg-[#2a2e39]'"
        @click="backtestStore.togglePlaybackSnap()"
        title="自动滚动到当前交易 (Snap Effect)"
      >
        <i class="fas fa-magnet text-[10px]" :class="{ 'animate-pulse': backtestStore.playbackSnap }"></i>
        <span class="text-[10px] font-bold uppercase tracking-wider">Snap</span>
      </button>

      <div class="flex items-center gap-2 bg-[#2a2e39]/50 p-1 rounded-lg border border-[#2a2e39]">
        <button 
          v-for="speed in [0.5, 1, 2, 5]" 
          :key="speed"
          :class="[
            'px-2.5 py-1 text-[10px] font-bold rounded transition-all',
            backtestStore.playbackSpeed === speed ? 'bg-[#2962ff] text-white' : 'text-[#787b86] hover:bg-[#2a2e39]'
          ]"
          @click="backtestStore.setPlaybackSpeed(speed)"
        >
          {{ speed }}x
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onUnmounted, watch } from 'vue';
import { useBacktestStore } from '../store/backtestStore';

const backtestStore = useBacktestStore();
const isPlaying = ref(false);
let timer: number | null = null;

const totalSteps = computed(() => backtestStore.equitySeries.length);
const currentIndex = computed(() => {
  if (totalSteps.value === 0 || !backtestStore.playbackCursor) return 0;
  return backtestStore.equitySeries.findIndex(p => p.date === backtestStore.playbackCursor);
});

function handleInput(e: Event) {
  const index = parseInt((e.target as HTMLInputElement).value);
  const point = backtestStore.equitySeries[index];
  if (point) {
    backtestStore.setPlaybackCursor(point.date);
    backtestStore.togglePlaybackMode(true);
  }
}

function togglePlay() {
  isPlaying.value = !isPlaying.value;
  if (isPlaying.value) {
    backtestStore.togglePlaybackMode(true);
    startPlayback();
  } else {
    stopPlayback();
  }
}

function startPlayback() {
  if (timer) clearInterval(timer);
  const interval = 1000 / backtestStore.playbackSpeed;
  timer = window.setInterval(() => {
    if (currentIndex.value < totalSteps.value - 1) {
      stepForward();
    } else {
      stopPlayback();
      isPlaying.value = false;
    }
  }, interval);
}

function stopPlayback() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

function stepForward() {
  const nextIndex = Math.min(currentIndex.value + 1, totalSteps.value - 1);
  const point = backtestStore.equitySeries[nextIndex];
  if (point) {
    backtestStore.setPlaybackCursor(point.date);
  }
}

function stepBackward() {
  const prevIndex = Math.max(currentIndex.value - 1, 0);
  const point = backtestStore.equitySeries[prevIndex];
  if (point) {
    backtestStore.setPlaybackCursor(point.date);
  }
}

watch(() => backtestStore.playbackSpeed, () => {
  if (isPlaying.value) startPlayback();
});

onUnmounted(() => {
  stopPlayback();
});
</script>

<style scoped>
.timeline-slider {
  -webkit-appearance: none;
  width: 100%;
  height: 4px;
  background: #2a2e39;
  border-radius: 2px;
  outline: none;
  cursor: pointer;
  z-index: 10;
}

.timeline-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 12px;
  height: 12px;
  background: #d1d4dc;
  border: 2px solid #2962ff;
  border-radius: 50%;
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 0 10px rgba(41, 98, 255, 0.5);
}

.timeline-slider:hover::-webkit-slider-thumb {
  transform: scale(1.2);
  background: #ffffff;
}

.timeline-slider::-moz-range-thumb {
  width: 12px;
  height: 12px;
  background: #d1d4dc;
  border: 2px solid #2962ff;
  border-radius: 50%;
  cursor: pointer;
}
</style>
