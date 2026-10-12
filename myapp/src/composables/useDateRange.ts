import { ref, watch } from 'vue';
import { DEFAULT_DATES } from '@/config/strategyConfig';

const DATE_STORAGE_KEY = 'grid_opt_dates_v1';

// 计算日期之间的天数差
function daysBetween(start: string, end: string): number {
  const startDate = new Date(start + 'T00:00:00');
  const endDate = new Date(end + 'T00:00:00');
  return Math.max(0, Math.ceil((endDate.getTime() - startDate.getTime()) / (1000 * 60 * 60 * 24)));
}

// 根据起始日期和天数偏移量计算日期
function addDays(dateStr: string, days: number): string {
  const date = new Date(dateStr + 'T00:00:00');
  date.setDate(date.getDate() + days);
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

export function useDateRange() {
  const startDate = ref('');
  const endDate = ref('');
  const trainStartDate = ref('');
  const trainEndDate = ref('');
  const valStartDate = ref('');
  const valEndDate = ref('');
  const testStartDate = ref('');
  const testEndDate = ref('');

  // 标记是否正在自动重新计算，避免循环更新
  const isRecalculating = ref(false);

  // 计算当前三段区间的比例
  const calculateCurrentRatios = () => {
    if (!startDate.value || !endDate.value) return null;
    
    const totalDays = daysBetween(startDate.value, endDate.value);
    if (totalDays === 0) return null;

    const trainDays = trainStartDate.value && trainEndDate.value 
      ? daysBetween(trainStartDate.value, trainEndDate.value) 
      : 0;
    const valDays = valStartDate.value && valEndDate.value 
      ? daysBetween(valStartDate.value, valEndDate.value) 
      : 0;
    const testDays = testStartDate.value && testEndDate.value 
      ? daysBetween(testStartDate.value, testEndDate.value) 
      : 0;

    return {
      trainRatio: trainDays / totalDays,
      valRatio: valDays / totalDays,
      testRatio: testDays / totalDays,
    };
  };

  // 根据当前比例重新计算三段区间日期
  const recalculateSegments = () => {
    if (!startDate.value || !endDate.value || isRecalculating.value) return;
    
    const totalDays = daysBetween(startDate.value, endDate.value);
    if (totalDays === 0) return;

    // 获取当前比例，如果没有则使用默认比例（60%/20%/20%）
    const ratios = calculateCurrentRatios();
    const trainRatio = ratios?.trainRatio || 0.6;
    const valRatio = ratios?.valRatio || 0.2;
    const testRatio = ratios?.testRatio || 0.2;

    // 确保比例总和为1
    const totalRatio = trainRatio + valRatio + testRatio;
    const normalizedTrainRatio = trainRatio / totalRatio;
    const normalizedValRatio = valRatio / totalRatio;
    const normalizedTestRatio = testRatio / totalRatio;

    isRecalculating.value = true;

    try {
      // 计算各段的结束日期（相对于起始日期的天数）
      const trainEndDays = Math.floor(totalDays * normalizedTrainRatio);
      const valEndDays = Math.floor(totalDays * (normalizedTrainRatio + normalizedValRatio));

      // 设置训练区间
      trainStartDate.value = startDate.value;
      trainEndDate.value = addDays(startDate.value, trainEndDays);

      // 设置验证区间
      valStartDate.value = trainEndDate.value;
      valEndDate.value = addDays(startDate.value, valEndDays);

      // 设置测试区间
      testStartDate.value = valEndDate.value;
      testEndDate.value = endDate.value;
    } finally {
      isRecalculating.value = false;
    }
  };

  const loadSavedDates = () => {
    try {
      const raw = localStorage.getItem(DATE_STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as Partial<typeof DEFAULT_DATES>;
      return parsed || null;
    } catch {
      return null;
    }
  };

  const applyInitialDates = () => {
    const saved = loadSavedDates();
    const src = saved && saved.startDate && saved.endDate ? { ...DEFAULT_DATES, ...saved } : DEFAULT_DATES;
    if (!startDate.value) startDate.value = src.startDate;
    if (!endDate.value) endDate.value = src.endDate;
    if (!trainStartDate.value) trainStartDate.value = src.trainStartDate;
    if (!trainEndDate.value) trainEndDate.value = src.trainEndDate;
    if (!valStartDate.value) valStartDate.value = src.valStartDate;
    if (!valEndDate.value) valEndDate.value = src.valEndDate;
    if (!testStartDate.value) testStartDate.value = src.testStartDate;
    if (!testEndDate.value) testEndDate.value = src.testEndDate;
  };

  const persistDates = () => {
    const payload = {
      startDate: startDate.value,
      endDate: endDate.value,
      trainStartDate: trainStartDate.value,
      trainEndDate: trainEndDate.value,
      valStartDate: valStartDate.value,
      valEndDate: valEndDate.value,
      testStartDate: testStartDate.value,
      testEndDate: testEndDate.value,
    };
    try {
      localStorage.setItem(DATE_STORAGE_KEY, JSON.stringify(payload));
    } catch {
      // Ignore storage errors
    }
  };

  // 监听主回测日期变化，自动重新计算三段区间
  watch(
    [startDate, endDate],
    (newValues, oldValues) => {
      if (!startDate.value || !endDate.value) return;
      
      // 检查主日期范围是否真的改变了
      const oldStart = oldValues?.[0];
      const oldEnd = oldValues?.[1];
      const newStart = newValues[0];
      const newEnd = newValues[1];
      
      // 如果日期范围改变了，重新计算三段区间
      if (oldStart !== newStart || oldEnd !== newEnd) {
        recalculateSegments();
      }
    },
    { immediate: false }
  );

  // 监听所有日期变化，持久化存储
  watch(
    [startDate, endDate, trainStartDate, trainEndDate, valStartDate, valEndDate, testStartDate, testEndDate],
    () => {
      if (!isRecalculating.value) {
        persistDates();
      }
    },
  );

  return {
    startDate,
    endDate,
    trainStartDate,
    trainEndDate,
    valStartDate,
    valEndDate,
    testStartDate,
    testEndDate,
    applyInitialDates,
  };
}




