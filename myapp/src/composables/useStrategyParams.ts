import { ref } from 'vue';
import { CUSTOM_PARAM_CONFIG } from '@/config/strategyConfig';
import { getDefaultRange } from '@/utils/paramUtils';
import { request } from '../services/api';

export function useStrategyParams(selectedStrategy: { value: string }) {
  const availableParams = ref<any[]>([]);
  const selectedParamKeys = ref<string[]>([]);
  const paramLocked = ref<Record<string, boolean>>({});
  const paramRangeValues = ref<Record<string, { min: number; max: number }>>({});
  const isLoadingParams = ref(false);

  const fetchStrategyParams = async () => {
    if (!selectedStrategy.value) return;
    isLoadingParams.value = true;
    try {
      const res = await request<any[]>(`/strategies/${encodeURIComponent(selectedStrategy.value)}/params`);
      if (res.ok) {
        const params = res.data;
        if (Array.isArray(params)) {
          availableParams.value = params;
          
          // 遍历所有参数
          params.forEach((param: any) => {
            // 1. 尝试从配置表中获取自定义配置（前端覆盖）
            const config = CUSTOM_PARAM_CONFIG[param.key];
            
            // 2. 处理 min/max 的优先级：
            //    a) 如果前端有 CUSTOM_PARAM_CONFIG 覆盖，使用覆盖值
            //    b) 否则，如果后端从 metadata 返回了值，使用后端值
            //    c) 否则，使用默认范围生成器
            
            let finalMin: number;
            let finalMax: number;
            
            if (config) {
              // === 前端覆盖优先 ===
              finalMin = config.overrideMin ?? param.min ?? getDefaultRange(param).min;
              finalMax = config.overrideMax ?? param.max ?? getDefaultRange(param).max;
              
              // 设置滑块初始选区（把手位置）
              const startVal = config.defaultStart ?? finalMin;
              const endVal = config.defaultEnd ?? finalMax;
              
              paramRangeValues.value[param.key] = {
                min: startVal,
                max: endVal
              };
            } else {
              // === 使用后端值或默认范围 ===
              const defaultRange = getDefaultRange(param);
              finalMin = param.min ?? defaultRange.min;
              finalMax = param.max ?? defaultRange.max;
              
              paramRangeValues.value[param.key] = {
                min: finalMin,
                max: finalMax
              };
            }
            
            // 更新 param 对象，确保后续使用正确的范围值
            param.min = finalMin;
            param.max = finalMax;
          });
        }
      }
    } catch (e) {
      console.error('获取策略参数失败:', e);
    } finally {
      isLoadingParams.value = false;
    }
  };

  return {
    availableParams,
    selectedParamKeys,
    paramLocked,
    paramRangeValues,
    isLoadingParams,
    fetchStrategyParams,
  };
}






























































