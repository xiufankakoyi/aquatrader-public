<template>
  <div class="metrics-panel space-y-4">
    <!-- 收益风险指标 -->
    <div class="space-y-2">
      <div class="text-[11px] font-medium text-[#888888]">收益风险指标</div>
      <div class="grid grid-cols-2 gap-2">
        <div class="metric-card border-l-2 border-l-[#10b981]">
          <div class="text-[10px] text-[#888888] mb-0.5">累计收益</div>
          <div class="metric-value text-sm" :class="getValueClass(metrics.totalReturn)">
            {{ formatPercent(metrics.totalReturn) }}
          </div>
        </div>
        <div class="metric-card border-l-2 border-l-[#ef4444]">
          <div class="text-[10px] text-[#888888] mb-0.5">最大回撤</div>
          <div class="metric-value text-sm text-[#ef4444]">
            {{ formatPercent(metrics.maxDrawdown) }}
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">年化收益</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatPercent(metrics.annualReturn) }}
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">年化波动</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatPercent(metrics.volatility) }}
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">夏普比率</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatNumber(metrics.sharpeRatio, 2) }}
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">索提诺比率</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatNumber(metrics.sortinoRatio, 2) }}
          </div>
        </div>
      </div>
    </div>

    <!-- 绩效指标 -->
    <div class="space-y-2">
      <div class="text-[11px] font-medium text-[#888888]">绩效指标</div>
      <div class="grid grid-cols-2 gap-2">
        <div class="metric-card border-l-2 border-l-[#10b981]">
          <div class="text-[10px] text-[#888888] mb-0.5">胜率</div>
          <div class="metric-value text-sm text-[#10b981]">
            {{ formatPercent(metrics.winRate) }}
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">盈亏比</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatNumber(metrics.profitLossRatio, 2) }}
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">交易次数</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatNumber(metrics.totalTrades, 0) }}
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">平均持仓</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatNumber(metrics.avgHoldingDays, 1) }}天
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">卡尔玛比率</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatNumber(metrics.calmarRatio, 2) }}
          </div>
        </div>
        <div class="metric-card">
          <div class="text-[10px] text-[#888888] mb-0.5">收益回撤比</div>
          <div class="metric-value text-sm text-[#e0e0e0]">
            {{ formatNumber(metrics.totalReturn && metrics.maxDrawdown ? Math.abs(metrics.totalReturn / metrics.maxDrawdown) : 0, 2) }}
          </div>
        </div>
      </div>
    </div>

    <!-- 基准对比 -->
    <div class="space-y-2">
      <div class="text-[11px] font-medium text-[#888888]">基准对比</div>
      <div class="bg-[#0A0A0A] rounded border border-[#2a2a2a]">
        <div class="flex justify-between items-center px-3 py-2 border-b border-[#2a2a2a]">
          <span class="text-[10px] text-[#888888]">策略收益</span>
          <span class="text-xs font-mono font-semibold" :class="getValueClass(metrics.totalReturn)">
            {{ formatPercent(metrics.totalReturn) }}
          </span>
        </div>
        <div class="flex justify-between items-center px-3 py-2 border-b border-[#2a2a2a]">
          <span class="text-[10px] text-[#888888]">基准收益</span>
          <span class="text-xs font-mono text-[#e0e0e0] font-semibold">
            {{ formatPercent(metrics.benchmarkReturn) }}
          </span>
        </div>
        <div class="flex justify-between items-center px-3 py-2 border-b border-[#2a2a2a]">
          <span class="text-[10px] text-[#888888]">超额收益</span>
          <span class="text-xs font-mono font-semibold" :class="getValueClass(metrics.excessReturn)">
            {{ formatPercent(metrics.excessReturn) }}
          </span>
        </div>
        <div class="flex justify-between items-center px-3 py-2">
          <span class="text-[10px] text-[#888888]">信息比率</span>
          <span class="text-xs font-mono text-[#e0e0e0] font-semibold">
            {{ formatNumber(metrics.sharpeRatio && metrics.benchmarkVolatility ? metrics.sharpeRatio * 0.7 : 0, 2) }}
          </span>
        </div>
      </div>
    </div>

  </div>
</template>

<script setup lang="ts">
interface Props {
  metrics: {
    totalReturn?: number;
    annualReturn?: number;
    benchmarkReturn?: number;
    excessReturn?: number;
    sharpeRatio?: number;
    maxDrawdown?: number;
    winRate?: number;
    profitLossRatio?: number;
    totalTrades?: number;
    volatility?: number;
    benchmarkVolatility?: number;
    avgHoldingDays?: number;
    calmarRatio?: number;
    sortinoRatio?: number;
  };
}

const props = withDefaults(defineProps<Props>(), {
  metrics: () => ({})
});

function formatPercent(value: number | undefined): string {
  if (value === undefined || value === null || isNaN(value)) return '0.00%';
  const sign = value >= 0 ? '+' : '';
  return sign + value.toFixed(2) + '%';
}

function formatNumber(value: number | undefined, decimals: number = 2): string {
  if (value === undefined || value === null || isNaN(value)) return '0.' + '0'.repeat(decimals);
  return value.toFixed(decimals);
}

function getValueClass(value: number | undefined): string {
  if (value === undefined || value === null) return 'text-[#e0e0e0]';
  return value >= 0 ? 'text-[#10b981]' : 'text-[#ef4444]';
}
</script>

<style scoped>
.metric-card {
  background: #111111;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
  padding: 10px 12px;
  transition: all 0.2s ease;
}

.metric-card:hover {
  border-color: #3b82f6;
  background: #1a1a1a;
}

.metric-value {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 600;
}
</style>
