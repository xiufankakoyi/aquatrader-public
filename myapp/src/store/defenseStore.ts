import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

export interface DefenseRule {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  threshold?: number;
  type: 'drawdown' | 'volatility' | 'liquidity' | 'market' | 'custom';
}

export interface DefensePosition {
  symbol: string;
  symbolCode: string;
  symbolName: string;
  allocation: number;
  category: 'bank' | 'insurance' | 'bond' | 'cash' | 'other';
}

export const useDefenseStore = defineStore('defense', () => {
  const rules = ref<DefenseRule[]>([
    {
      id: 'max_drawdown',
      name: '最大回撤阈值',
      description: '当策略回撤超过设定阈值时触发防守仓',
      enabled: true,
      threshold: 15,
      type: 'drawdown',
    },
    {
      id: 'volatility_limit',
      name: '波动率上限',
      description: '当市场波动率超过设定值时启用防守仓',
      enabled: true,
      threshold: 30,
      type: 'volatility',
    },
    {
      id: 'liquidity_alert',
      name: '流动性警报',
      description: '当标的流动性不足时自动切换到防守仓',
      enabled: false,
      threshold: 50,
      type: 'liquidity',
    },
  ]);

  const positions = ref<DefensePosition[]>([
    {
      symbol: '000001',
      symbolCode: '000001',
      symbolName: '平安银行',
      allocation: 30,
      category: 'bank',
    },
    {
      symbol: '601318',
      symbolCode: '601318',
      symbolName: '中国平安',
      allocation: 25,
      category: 'insurance',
    },
    {
      symbol: '000012',
      symbolCode: '000012',
      symbolName: '国债',
      allocation: 25,
      category: 'bond',
    },
    {
      symbol: 'CASH',
      symbolCode: 'CASH',
      symbolName: '现金',
      allocation: 20,
      category: 'cash',
    },
  ]);

  const isLoading = ref(false);
  const error = ref<string | null>(null);

  const totalAllocation = computed(() => {
    return positions.value.reduce((sum, pos) => sum + pos.allocation, 0);
  });

  const positionsByCategory = computed(() => {
    const grouped: Record<string, DefensePosition[]> = {};
    positions.value.forEach(pos => {
      if (!grouped[pos.category]) {
        grouped[pos.category] = [];
      }
      grouped[pos.category].push(pos);
    });
    return grouped;
  });

  function toggleRule(ruleId: string) {
    const rule = rules.value.find(r => r.id === ruleId);
    if (rule) {
      rule.enabled = !rule.enabled;
    }
  }

  function updateRuleThreshold(ruleId: string, threshold: number) {
    const rule = rules.value.find(r => r.id === ruleId);
    if (rule) {
      rule.threshold = threshold;
    }
  }

  function updatePositionAllocation(symbolCode: string, allocation: number) {
    const position = positions.value.find(p => p.symbolCode === symbolCode);
    if (position) {
      position.allocation = allocation;
    }
  }

  return {
    rules,
    positions,
    isLoading,
    error,
    totalAllocation,
    positionsByCategory,
    toggleRule,
    updateRuleThreshold,
    updatePositionAllocation,
  };
});

