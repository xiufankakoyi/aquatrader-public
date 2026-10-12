<script setup lang="ts">
import { computed } from 'vue';
import GAVisualizer from './optimizers/GAVisualizer.vue';
import PSOVisualizer from './optimizers/PSOVisualizer.vue';
import CMAESVisualizer from './optimizers/CMAESVisualizer.vue';
import SAVisualizer from './optimizers/SAVisualizer.vue';
import GridVisualizer from './optimizers/GridVisualizer.vue';
import BOVisualizer from './optimizers/BOVisualizer.vue';

const props = defineProps<{
  optimizer: string;
  progress: number;
  iteration: number;
  totalIterations: number;
  bestScore: number | null;
  metricLabel: string;
  history?: Array<{ iteration: number; metric: number; params?: Record<string, any> }>;
}>();

const algoLabel = computed(() => {
  const map: Record<string, string> = {
    ga: '遗传算法 (GA)',
    pso: '粒子群优化 (PSO)',
    cmaes: 'CMA-ES',
    simulatedAnnealing: '模拟退火 (SA)',
    grid: '网格搜索 (Grid)',
    bayesian: '贝叶斯优化 (BO)',
  };
  return map[props.optimizer] || props.optimizer.toUpperCase();
});
</script>

<template>
  <div class="mt-6 rounded-2xl border border-slate-800/60 bg-slate-950/40 p-4 space-y-3">
    <!-- 顶部状态行 -->
    <div class="flex items-center justify-between text-xs text-slate-400">
      <span>{{ algoLabel }} | 第 {{ iteration }}{{ totalIterations ? `/${totalIterations}` : '' }} 次评估</span>
      <span class="flex items-center gap-3">
        <span v-if="history && history.length" class="text-[10px] text-slate-500">
          历史记录 {{ history.length }}{{ totalIterations ? `/${totalIterations}` : '' }}
        </span>
        <span>
          最佳 {{ metricLabel }}
          <span class="font-mono text-emerald-300">
            {{ bestScore !== null ? bestScore.toFixed(4) : '—' }}
          </span>
        </span>
      </span>
    </div>

    <!-- 不同算法专属动画 -->
    <div class="h-32 md:h-36 overflow-hidden rounded-xl bg-slate-950/60 border border-slate-800/70">
      <GAVisualizer
        v-if="optimizer === 'ga'"
        :progress="progress"
        :iteration="iteration"
        :history="history"
      />
      <PSOVisualizer
        v-else-if="optimizer === 'pso'"
        :progress="progress"
        :iteration="iteration"
        :history="history"
      />
      <CMAESVisualizer
        v-else-if="optimizer === 'cmaes'"
        :progress="progress"
        :iteration="iteration"
        :history="history"
      />
      <SAVisualizer
        v-else-if="optimizer === 'simulatedAnnealing'"
        :progress="progress"
        :iteration="iteration"
        :history="history"
      />
      <GridVisualizer 
        v-else-if="optimizer === 'grid'" 
        :progress="progress"
        :iteration="iteration"
        :history="history"
      />
      <BOVisualizer
        v-else
        :progress="progress"
        :iteration="iteration"
        :total-iterations="totalIterations"
        :best-score="bestScore"
        :history="history"
      />
    </div>
  </div>
</template>


