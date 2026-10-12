import { ref, computed } from 'vue';
import type { IChartApi, ISeriesApi, LineData, Time } from 'lightweight-charts';

/**
 * 图表绘图工具 Composable
 * 提供 TradingView 风格的绘图功能
 */

export type DrawingType = 
  | 'crosshair' 
  | 'trendLine' | 'ray' | 'horizontalLine' | 'verticalLine' | 'channel'
  | 'fibonacci' | 'gannFan'
  | 'rectangle' | 'circle' | 'brush' | 'path'
  | 'text'
  | 'measure';

export interface Drawing {
  id: string;
  type: DrawingType;
  points: Array<{ time: number; price: number }>;
  options?: Record<string, any>;
}

export function useChartDrawing() {
  const drawings = ref<Drawing[]>([]);
  const activeTool = ref<DrawingType>('crosshair');
  const isDrawing = ref(false);
  const currentDrawing = ref<Drawing | null>(null);
  const magnetMode = ref(false);
  const lockDrawings = ref(false);
  const hideDrawings = ref(false);

  // 图表实例引用
  let chartInstance: IChartApi | null = null;
  let candlestickSeries: ISeriesApi<'Candlestick'> | null = null;

  // 存储绘图的 LineSeries
  const drawingSeries = new Map<string, ISeriesApi<'Line'>[]>();

  function init(chart: IChartApi, series: ISeriesApi<'Candlestick'>) {
    chartInstance = chart;
    candlestickSeries = series;
  }

  function setTool(tool: DrawingType) {
    console.log('[useChartDrawing] setTool:', tool, 'current activeTool:', activeTool.value);
    activeTool.value = tool;
    isDrawing.value = false;
    currentDrawing.value = null;
  }

  function toggleMagnetMode() {
    magnetMode.value = !magnetMode.value;
    return magnetMode.value;
  }

  function toggleLockDrawings() {
    lockDrawings.value = !lockDrawings.value;
    return lockDrawings.value;
  }

  function toggleHideDrawings() {
    hideDrawings.value = !hideDrawings.value;
    updateDrawingsVisibility();
    return hideDrawings.value;
  }

  function updateDrawingsVisibility() {
    drawingSeries.forEach((seriesArray) => {
      seriesArray.forEach(series => {
        series.applyOptions({
          visible: !hideDrawings.value
        });
      });
    });
  }

  /**
   * 吸附到最近的 OHLC
   */
  function snapToOHLC(time: number, price: number, data: any[]) {
    if (!magnetMode.value) return { time, price };

    const candle = data.find(d => new Date(d.date).getTime() / 1000 === time);
    if (!candle) return { time, price };

    const prices = [candle.open, candle.high, candle.low, candle.close];
    const closest = prices.reduce((prev, curr) => 
      Math.abs(curr - price) < Math.abs(prev - price) ? curr : prev
    );

    return { time, price: closest };
  }

  /**
   * 开始绘制
   */
  function startDrawing(time: number, price: number, data?: any[]) {
    console.log('[useChartDrawing] startDrawing:', { time, price, activeTool: activeTool.value, lockDrawings: lockDrawings.value });
    if (lockDrawings.value) {
      console.log('[useChartDrawing] startDrawing: locked');
      return;
    }
    if (activeTool.value === 'crosshair') {
      console.log('[useChartDrawing] startDrawing: crosshair mode');
      return;
    }

    const snapped = data ? snapToOHLC(time, price, data) : { time, price };

    isDrawing.value = true;
    currentDrawing.value = {
      id: `drawing-${Date.now()}`,
      type: activeTool.value,
      points: [snapped]
    };
    console.log('[useChartDrawing] startDrawing: started', currentDrawing.value);
  }

  /**
   * 更新绘制中
   */
  function updateDrawing(time: number, price: number, data?: any[]) {
    if (!isDrawing.value || !currentDrawing.value) return;

    const snapped = data ? snapToOHLC(time, price, data) : { time, price };
    currentDrawing.value.points[1] = snapped;

    // 实时更新预览
    renderPreview();
  }

  /**
   * 完成绘制
   */
  function finishDrawing(time: number, price: number, data?: any[]) {
    console.log('[useChartDrawing] finishDrawing:', { time, price, isDrawing: isDrawing.value, hasCurrentDrawing: !!currentDrawing.value });
    if (!isDrawing.value || !currentDrawing.value) {
      console.log('[useChartDrawing] finishDrawing: early return');
      return;
    }

    const snapped = data ? snapToOHLC(time, price, data) : { time, price };
    currentDrawing.value.points[1] = snapped;

    // 保存绘图
    drawings.value.push({ ...currentDrawing.value });
    console.log('[useChartDrawing] finishDrawing: drawing saved, total drawings:', drawings.value.length);

    // 渲染最终效果
    console.log('[useChartDrawing] finishDrawing: calling renderDrawing');
    renderDrawing(currentDrawing.value);

    isDrawing.value = false;
    currentDrawing.value = null;
  }

  /**
   * 渲染预览（绘制中）
   */
  function renderPreview() {
    if (!currentDrawing.value || !chartInstance) return;

    // 清除之前的预览
    clearPreview();

    // 渲染当前预览
    renderDrawing(currentDrawing.value, true);
  }

  /**
   * 清除预览
   */
  function clearPreview() {
    // 找到并移除预览系列的线
    const previewKeys = Array.from(drawingSeries.keys()).filter(k => k.includes('preview'));
    previewKeys.forEach(key => {
      const seriesArray = drawingSeries.get(key);
      seriesArray?.forEach(series => {
        chartInstance?.removeSeries(series);
      });
      drawingSeries.delete(key);
    });
  }

  /**
   * 渲染单个绘图
   */
  function renderDrawing(drawing: Drawing, isPreview = false) {
    if (!chartInstance || hideDrawings.value) return;

    const key = isPreview ? `preview-${drawing.type}` : drawing.id;

    switch (drawing.type) {
      case 'trendLine':
        renderTrendLine(drawing, key);
        break;
      case 'horizontalLine':
        renderHorizontalLine(drawing, key);
        break;
      case 'verticalLine':
        renderVerticalLine(drawing, key);
        break;
      case 'ray':
        renderRay(drawing, key);
        break;
      case 'channel':
        renderChannel(drawing, key);
        break;
      case 'fibonacci':
        renderFibonacci(drawing, key);
        break;
      case 'rectangle':
        renderRectangle(drawing, key);
        break;
      case 'measure':
        renderMeasure(drawing, key);
        break;
    }
  }

  /**
   * 将时间戳转换为 Lightweight Charts 的 Time 格式
   * Lightweight Charts 使用秒级时间戳
   */
  function toChartTime(timestamp: number): Time {
    const time = Math.floor(timestamp);
    console.log('[toChartTime] input timestamp:', timestamp, 'output time:', time);
    return time as Time;
  }

  /**
   * 渲染趋势线
   */
  function renderTrendLine(drawing: Drawing, key: string) {
    if (drawing.points.length < 2 || !chartInstance) {
      console.log('[renderTrendLine] 无法绘制: points.length=', drawing.points.length, 'chartInstance=', !!chartInstance);
      return;
    }

    console.log('[renderTrendLine] 绘制趋势线, points:', drawing.points);

    const { LineSeries } = require('lightweight-charts');

    const series = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    const time1 = toChartTime(drawing.points[0].time);
    const time2 = toChartTime(drawing.points[1].time);
    const data: LineData[] = [
      { time: time1, value: drawing.points[0].price },
      { time: time2, value: drawing.points[1].price }
    ];

    console.log('[renderTrendLine] LineData:', data);
    series.setData(data);

    const existing = drawingSeries.get(key) || [];
    existing.push(series);
    drawingSeries.set(key, existing);
    
    console.log('[renderTrendLine] 趋势线绘制完成');
  }

  /**
   * 渲染水平线
   */
  function renderHorizontalLine(drawing: Drawing, key: string) {
    if (drawing.points.length < 1 || !chartInstance) return;

    const { LineSeries } = require('lightweight-charts');

    const price = drawing.points[0].price;
    const visibleRange = chartInstance.timeScale().getVisibleLogicalRange();
    
    if (!visibleRange) return;

    const series = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: true,
      priceLineVisible: false,
      title: `H: ${price.toFixed(2)}`
    });

    // 获取可见范围的时间
    const fromTime = drawing.points[0].time;
    const toTime = drawing.points[1]?.time || fromTime + 86400 * 30;

    const data: LineData[] = [
      { time: toChartTime(fromTime), value: price },
      { time: toChartTime(toTime), value: price }
    ];

    series.setData(data);

    const existing = drawingSeries.get(key) || [];
    existing.push(series);
    drawingSeries.set(key, existing);
  }

  /**
   * 渲染垂直线
   */
  function renderVerticalLine(drawing: Drawing, key: string) {
    if (drawing.points.length < 1 || !chartInstance || !candlestickSeries) return;

    const { LineSeries } = require('lightweight-charts');

    const time = drawing.points[0].time;
    const priceRange = candlestickSeries.priceScale().getVisiblePriceRange();
    
    if (!priceRange) return;

    const series = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    const data: LineData[] = [
      { time: toChartTime(time), value: priceRange.minValue },
      { time: toChartTime(time), value: priceRange.maxValue }
    ];

    series.setData(data);

    const existing = drawingSeries.get(key) || [];
    existing.push(series);
    drawingSeries.set(key, existing);
  }

  /**
   * 渲染射线
   */
  function renderRay(drawing: Drawing, key: string) {
    if (drawing.points.length < 2 || !chartInstance) return;

    const { LineSeries } = require('lightweight-charts');

    const p1 = drawing.points[0];
    const p2 = drawing.points[1];

    // 计算方向并延伸
    const timeDiff = p2.time - p1.time;
    const priceDiff = p2.price - p1.price;
    const slope = priceDiff / timeDiff;

    // 延伸到可见范围边缘
    const visibleRange = chartInstance.timeScale().getVisibleLogicalRange();
    if (!visibleRange) return;

    const endTime = Math.floor(visibleRange.to * 86400) + p1.time;
    const endPrice = p1.price + slope * (endTime - p1.time);

    const series = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    const data: LineData[] = [
      { time: toChartTime(p1.time), value: p1.price },
      { time: toChartTime(endTime), value: endPrice }
    ];

    series.setData(data);

    const existing = drawingSeries.get(key) || [];
    existing.push(series);
    drawingSeries.set(key, existing);
  }

  /**
   * 渲染通道线
   */
  function renderChannel(drawing: Drawing, key: string) {
    if (drawing.points.length < 2 || !chartInstance) return;

    const { LineSeries } = require('lightweight-charts');

    const p1 = drawing.points[0];
    const p2 = drawing.points[1];

    // 计算通道宽度
    const channelWidth = Math.abs(p2.price - p1.price) * 0.1;

    // 主线
    const mainSeries = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    mainSeries.setData([
      { time: toChartTime(p1.time), value: p1.price },
      { time: toChartTime(p2.time), value: p2.price }
    ]);

    // 上轨
    const upperSeries = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 1,
      lineStyle: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    upperSeries.setData([
      { time: toChartTime(p1.time), value: p1.price + channelWidth },
      { time: toChartTime(p2.time), value: p2.price + channelWidth }
    ]);

    // 下轨
    const lowerSeries = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 1,
      lineStyle: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    lowerSeries.setData([
      { time: toChartTime(p1.time), value: p1.price - channelWidth },
      { time: toChartTime(p2.time), value: p2.price - channelWidth }
    ]);

    drawingSeries.set(key, [mainSeries, upperSeries, lowerSeries]);
  }

  /**
   * 渲染斐波那契回调
   */
  function renderFibonacci(drawing: Drawing, key: string) {
    if (drawing.points.length < 2 || !chartInstance) return;

    const { LineSeries } = require('lightweight-charts');

    const p1 = drawing.points[0];
    const p2 = drawing.points[1];

    const high = Math.max(p1.price, p2.price);
    const low = Math.min(p1.price, p2.price);
    const diff = high - low;

    const levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    const seriesArray: ISeriesApi<'Line'>[] = [];

    levels.forEach(level => {
      const price = high - diff * level;
      const series = chartInstance.addSeries(LineSeries, {
        color: level === 0 || level === 1 ? '#ef4444' : '#787b86',
        lineWidth: level === 0.5 || level === 0.618 ? 2 : 1,
        lineStyle: level === 0 || level === 1 ? 0 : 2,
        lastValueVisible: true,
        priceLineVisible: false,
        title: `${(level * 100).toFixed(1)}%`
      });

      series.setData([
        { time: toChartTime(p1.time), value: price },
        { time: toChartTime(p2.time), value: price }
      ]);

      seriesArray.push(series);
    });

    drawingSeries.set(key, seriesArray);
  }

  /**
   * 渲染矩形
   */
  function renderRectangle(drawing: Drawing, key: string) {
    // 矩形需要4条线组成
    if (drawing.points.length < 2 || !chartInstance) return;

    const { LineSeries } = require('lightweight-charts');

    const p1 = drawing.points[0];
    const p2 = drawing.points[1];

    const left = Math.min(p1.time, p2.time);
    const right = Math.max(p1.time, p2.time);
    const top = Math.max(p1.price, p2.price);
    const bottom = Math.min(p1.price, p2.price);

    const seriesArray: ISeriesApi<'Line'>[] = [];

    // 上边
    const topSeries = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    topSeries.setData([
      { time: toChartTime(left), value: top },
      { time: toChartTime(right), value: top }
    ]);
    seriesArray.push(topSeries);

    // 下边
    const bottomSeries = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    bottomSeries.setData([
      { time: toChartTime(left), value: bottom },
      { time: toChartTime(right), value: bottom }
    ]);
    seriesArray.push(bottomSeries);

    // 左边
    const leftSeries = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    leftSeries.setData([
      { time: toChartTime(left), value: bottom },
      { time: toChartTime(left), value: top }
    ]);
    seriesArray.push(leftSeries);

    // 右边
    const rightSeries = chartInstance.addSeries(LineSeries, {
      color: '#2962ff',
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    rightSeries.setData([
      { time: toChartTime(right), value: bottom },
      { time: toChartTime(right), value: top }
    ]);
    seriesArray.push(rightSeries);

    drawingSeries.set(key, seriesArray);
  }

  /**
   * 渲染测量尺
   */
  function renderMeasure(drawing: Drawing, key: string) {
    if (drawing.points.length < 2 || !chartInstance) return;

    const { LineSeries } = require('lightweight-charts');

    const p1 = drawing.points[0];
    const p2 = drawing.points[1];

    // 主线
    const series = chartInstance.addSeries(LineSeries, {
      color: '#f59e0b',
      lineWidth: 2,
      lineStyle: 2,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    series.setData([
      { time: toChartTime(p1.time), value: p1.price },
      { time: toChartTime(p2.time), value: p2.price }
    ]);

    // 计算测量信息
    const priceDiff = p2.price - p1.price;
    const priceChange = (priceDiff / p1.price) * 100;
    const daysDiff = Math.abs(p2.time - p1.time) / 86400;

    console.log(`[测量尺] 价格差: ${priceDiff.toFixed(2)}, 涨跌幅: ${priceChange.toFixed(2)}%, 天数: ${daysDiff.toFixed(0)}`);

    const existing = drawingSeries.get(key) || [];
    existing.push(series);
    drawingSeries.set(key, existing);
  }

  /**
   * 删除选中的绘图
   */
  function removeSelectedDrawing(id?: string) {
    if (lockDrawings.value) return;

    if (id) {
      // 删除指定ID的绘图
      const index = drawings.value.findIndex(d => d.id === id);
      if (index >= 0) {
        removeDrawingSeries(drawings.value[index].id);
        drawings.value.splice(index, 1);
      }
    } else {
      // 删除最后一个绘图
      const lastDrawing = drawings.value.pop();
      if (lastDrawing) {
        removeDrawingSeries(lastDrawing.id);
      }
    }
  }

  /**
   * 移除绘图的 series
   */
  function removeDrawingSeries(id: string) {
    const seriesArray = drawingSeries.get(id);
    if (seriesArray) {
      seriesArray.forEach(series => {
        chartInstance?.removeSeries(series);
      });
      drawingSeries.delete(id);
    }
  }

  /**
   * 清空所有绘图
   */
  function clearAllDrawings() {
    if (lockDrawings.value) return;

    drawings.value.forEach(drawing => {
      removeDrawingSeries(drawing.id);
    });
    drawings.value = [];

    // 清除预览
    clearPreview();
  }

  /**
   * 重新渲染所有绘图
   */
  function redrawAll() {
    if (hideDrawings.value) return;

    drawings.value.forEach(drawing => {
      renderDrawing(drawing);
    });
  }

  return {
    drawings,
    activeTool,
    isDrawing,
    magnetMode,
    lockDrawings,
    hideDrawings,
    init,
    setTool,
    toggleMagnetMode,
    toggleLockDrawings,
    toggleHideDrawings,
    startDrawing,
    updateDrawing,
    finishDrawing,
    removeSelectedDrawing,
    clearAllDrawings,
    redrawAll,
  };
}
