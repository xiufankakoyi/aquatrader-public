import { ref, computed, watch } from 'vue';
import { ALGO_CONFIGS, type AlgorithmKey } from '@/config/strategyConfig';

const ALGO_PARAMS_STORAGE_KEY = 'algorithmParams';

export function useAlgorithmParams(selectedAlgorithm: { value: AlgorithmKey }) {
  const buildDefaultAlgoParams = (): Record<AlgorithmKey, Record<string, any>> => {
    const defaults: Partial<Record<AlgorithmKey, Record<string, any>>> = {};
    (Object.keys(ALGO_CONFIGS) as AlgorithmKey[]).forEach((key) => {
      const cfg = ALGO_CONFIGS[key];
      defaults[key] = {};
      cfg.fields.forEach((field) => {
        defaults[key]![field.key] = field.default;
      });
      if (key === 'grid') {
        defaults[key]!.grid_density = 10;
      }
    });
    return defaults as Record<AlgorithmKey, Record<string, any>>;
  };

  const algorithmParams = ref<Record<AlgorithmKey, Record<string, any>>>(buildDefaultAlgoParams());

  const currentAlgoConfig = computed(() => {
    const key = selectedAlgorithm.value;
    const config = ALGO_CONFIGS[key];
    if (!config) {
      console.warn(`算法配置未找到: ${key}`, '可用配置:', Object.keys(ALGO_CONFIGS));
    }
    return config;
  });

  const currentAlgoParams = computed<Record<string, any>>(() => {
    const key = selectedAlgorithm.value;
    return algorithmParams.value[key] || {};
  });

  let saveTimer: NodeJS.Timeout | null = null;

  const saveAlgorithmParams = () => {
    if (saveTimer) {
      clearTimeout(saveTimer);
    }
    
    saveTimer = setTimeout(() => {
      try {
        const dataStr = JSON.stringify(algorithmParams.value);
        const dataSize = new Blob([dataStr]).size;
        
        if (dataSize > 4 * 1024 * 1024) {
          console.warn('算法参数数据较大，可能无法保存到 localStorage:', dataSize, 'bytes');
        }
        
        localStorage.setItem(ALGO_PARAMS_STORAGE_KEY, dataStr);
      } catch (error: any) {
        if (error.name === 'QuotaExceededError') {
          console.warn('localStorage 配额已满，无法保存算法参数。尝试清理旧数据...');
          try {
            const keysToKeep = [ALGO_PARAMS_STORAGE_KEY];
            const allKeys = Object.keys(localStorage);
            allKeys.forEach(key => {
              if (!keysToKeep.includes(key)) {
                localStorage.removeItem(key);
              }
            });
            localStorage.setItem(ALGO_PARAMS_STORAGE_KEY, JSON.stringify(algorithmParams.value));
            console.log('清理后成功保存算法参数');
          } catch (retryError) {
            console.error('清理后仍无法保存算法参数:', retryError);
            try {
              const currentKey = selectedAlgorithm.value;
              const minimalData = {
                [currentKey]: algorithmParams.value[currentKey] || {}
              };
              localStorage.setItem(ALGO_PARAMS_STORAGE_KEY, JSON.stringify(minimalData));
              console.log('已保存最小化的算法参数');
            } catch (minimalError) {
              console.error('无法保存算法参数，将跳过持久化:', minimalError);
            }
          }
        } else {
          console.error('保存算法参数时出错:', error);
        }
      }
    }, 500);
  };

  const loadAlgorithmParams = () => {
    const saved = localStorage.getItem(ALGO_PARAMS_STORAGE_KEY);
    if (!saved) return;
    try {
      const parsed = JSON.parse(saved);
      const merged = buildDefaultAlgoParams();
      (Object.keys(ALGO_CONFIGS) as AlgorithmKey[]).forEach((key) => {
        merged[key] = { ...merged[key], ...(parsed?.[key] || {}) };
      });
      algorithmParams.value = merged;
    } catch (e) {
      console.warn('Failed to load saved algorithm params:', e);
    }
  };

  watch(
    algorithmParams,
    () => {
      saveAlgorithmParams();
    },
    { deep: true }
  );

  return {
    algorithmParams,
    currentAlgoConfig,
    currentAlgoParams,
    loadAlgorithmParams,
  };
}






























































