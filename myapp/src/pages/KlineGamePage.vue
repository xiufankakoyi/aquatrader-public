<template>
  <div class="kline-game-page">
    <div class="game-header">
      <h1 class="page-title">
        <i class="fas fa-gamepad"></i>
        K线盘感训练
      </h1>
      <div class="header-actions">
        <button
          v-if="!gameStarted"
          @click="startGame"
          :disabled="isLoading"
          class="btn-primary"
        >
          <i class="fas fa-play"></i>
          开始训练
        </button>
        <button
          v-else
          @click="resetGame"
          :disabled="isLoading"
          class="btn-secondary"
        >
          <i class="fas fa-redo"></i>
          重新开始
        </button>
      </div>
    </div>

    <div v-if="!gameStarted && !isLoading" class="welcome-panel">
      <div class="welcome-content">
        <div class="welcome-icon">
          <i class="fas fa-graduation-cap"></i>
        </div>
        <h2>提升你的K线盘感</h2>
        <p>通过模拟真实交易环境，训练对K线走势的判断能力和仓位管理技巧。</p>
        <div class="rules-summary">
          <div class="rule-item">
            <i class="fas fa-coins"></i>
            <span>初始资金: ¥10,000</span>
          </div>
          <div class="rule-item">
            <i class="fas fa-history"></i>
            <span>60根历史K线参考</span>
          </div>
          <div class="rule-item">
            <i class="fas fa-chart-line"></i>
            <span>30根K线交易训练</span>
          </div>
          <div class="rule-item">
            <i class="fas fa-balance-scale"></i>
            <span>支持 1/4仓 / 半仓 / 全仓</span>
          </div>
        </div>
        <div class="volatility-selector">
          <label>波动模式:</label>
          <div class="volatility-options">
            <button 
              @click="volatilityFilter = 'random'" 
              :class="['volatility-btn', { active: volatilityFilter === 'random' }]"
            >
              <i class="fas fa-random"></i>
              随机
            </button>
            <button 
              @click="volatilityFilter = 'high'" 
              :class="['volatility-btn', { active: volatilityFilter === 'high' }]"
            >
              <i class="fas fa-fire"></i>
              高波动
            </button>
            <button 
              @click="volatilityFilter = 'extreme'" 
              :class="['volatility-btn', { active: volatilityFilter === 'extreme' }]"
            >
              <i class="fas fa-bolt"></i>
              极端波动
            </button>
          </div>
        </div>
        <button @click="startGame" class="btn-start">
          <i class="fas fa-rocket"></i>
          立即开始
        </button>
      </div>
    </div>

    <div v-if="isLoading" class="loading-panel">
      <i class="fas fa-spinner fa-spin"></i>
      <span>正在加载游戏数据...</span>
    </div>

    <div v-if="gameStarted && !isLoading" class="game-container">
      <div class="game-main">
        <div class="chart-section">
          <div class="chart-header">
            <div class="stock-info">
              <span class="stock-code">{{ stockCode }}</span>
              <span class="stock-name">{{ stockName }}</span>
            </div>
            <div class="chart-controls">
              <div class="indicator-selector">
                <label>技术指标:</label>
                <select v-model="selectedIndicator" class="indicator-select">
                  <option value="none">无</option>
                  <option value="ma">均线 (MA5/MA10/MA20)</option>
                  <option value="macd">MACD</option>
                </select>
              </div>
              <div class="phase-indicator" :class="{ trading: isTradingPhase }">
                <i :class="isTradingPhase ? 'fas fa-chart-line' : 'fas fa-eye'"></i>
                {{ isTradingPhase ? '交易阶段' : '历史参考' }}
              </div>
            </div>
          </div>

          <div class="chart-wrapper">
            <TVKlineChart
              :data="displayKlines"
              :markers="tradeMarkers"
              :showLegend="true"
              :showVolume="true"
              :showMA="selectedIndicator === 'ma'"
              :symbol="stockCode"
            />
            <div v-if="historyKlinesCount > 0" class="history-divider">
              <span>↑ 历史参考区域 ({{ historyKlinesCount }}根)</span>
            </div>
          </div>
          
          <div v-if="selectedIndicator === 'macd'" class="macd-wrapper">
            <div class="macd-header">
              <span class="macd-label">MACD(12,26,9)</span>
              <span class="macd-legend">
                <span class="legend-item dif">DIF</span>
                <span class="legend-item dea">DEA</span>
                <span class="legend-item histogram">MACD</span>
              </span>
            </div>
            <MACDChart
              ref="macdChartRef"
              :data="displayKlines"
              :height="120"
            />
          </div>
        </div>

        <div class="control-section">
          <div class="progress-panel">
            <div class="progress-header">
              <span class="progress-label">交易进度</span>
              <span class="progress-text">
                第 {{ currentTradeIndex + 1 }} / {{ totalTradeKlines }} 根K线
              </span>
            </div>
            <div class="progress-bar">
              <div
                class="progress-fill"
                :style="{ width: tradeProgressPercent + '%' }"
              ></div>
            </div>
          </div>

          <div class="stats-panel">
            <div class="stats-row">
              <div class="stat-item">
                <span class="stat-label">可用资金</span>
                <span class="stat-value">{{ formatMoney(statistics.cash) }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">持仓数量</span>
                <span class="stat-value">{{ (statistics.position ?? 0).toFixed(2) }}</span>
              </div>
            </div>
            <div class="stats-row">
              <div class="stat-item">
                <span class="stat-label">持仓成本</span>
                <span class="stat-value">{{ (statistics.avg_cost ?? 0).toFixed(2) }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">当前价格</span>
                <span class="stat-value">{{ (statistics.current_price ?? 0).toFixed(2) }}</span>
              </div>
            </div>
            <div class="stats-row">
              <div class="stat-item">
                <span class="stat-label">浮动盈亏</span>
                <span
                  class="stat-value"
                  :class="(statistics.unrealized_pnl ?? 0) >= 0 ? 'positive' : 'negative'"
                >
                  {{ (statistics.unrealized_pnl ?? 0) >= 0 ? '+' : '' }}{{ (statistics.unrealized_pnl ?? 0).toFixed(2) }}
                </span>
              </div>
              <div class="stat-item">
                <span class="stat-label">总资产</span>
                <span class="stat-value highlight">{{ formatMoney(statistics.total_assets) }}</span>
              </div>
            </div>
          </div>

          <div class="position-selector">
            <span class="selector-label">仓位选择:</span>
            <div class="position-options">
              <button
                @click="positionRatio = 0.25"
                :class="['position-btn', { active: positionRatio === 0.25 }]"
              >
                1/4 仓
              </button>
              <button
                @click="positionRatio = 0.5"
                :class="['position-btn', { active: positionRatio === 0.5 }]"
              >
                半仓
              </button>
              <button
                @click="positionRatio = 1.0"
                :class="['position-btn', { active: positionRatio === 1.0 }]"
              >
                全仓
              </button>
            </div>
          </div>

          <div class="action-buttons">
            <button
              @click="handleBuy"
              :disabled="isFinished || actionLoading || !isTradingPhase"
              class="btn-buy"
            >
              <i class="fas fa-arrow-up"></i>
              买入
            </button>
            <button
              @click="handleSell"
              :disabled="isFinished || actionLoading || statistics.position <= 0 || !isTradingPhase"
              class="btn-sell"
            >
              <i class="fas fa-arrow-down"></i>
              卖出
            </button>
            <button
              @click="handleNext"
              :disabled="isFinished || actionLoading"
              class="btn-next"
            >
              <i class="fas fa-forward"></i>
              下一根
            </button>
            <button
              @click="handleFastForward(5)"
              :disabled="isFinished || actionLoading"
              class="btn-fast-forward"
              title="跳过5根K线"
            >
              <i class="fas fa-fast-forward"></i>
              快进5
            </button>
          </div>

          <div class="secondary-buttons">
            <button
              @click="handleChangeStock"
              :disabled="actionLoading || statistics.position > 0"
              class="btn-change-stock"
              :title="statistics.position > 0 ? '请先清仓再换股' : '切换到另一只股票'"
            >
              <i class="fas fa-exchange-alt"></i>
              换股
            </button>
            <button
              @click="handleEndGame"
              :disabled="isFinished || actionLoading || currentTradeIndex < 60"
              class="btn-end-game"
              :title="currentTradeIndex < 60 ? '交易满60根后可提前结束' : '结束本局游戏'"
            >
              <i class="fas fa-stop"></i>
              结束游戏
            </button>
          </div>

          <div v-if="message" class="action-message" :class="messageType">
            <i :class="messageType === 'success' ? 'fas fa-check-circle' : 'fas fa-exclamation-circle'"></i>
            {{ message }}
          </div>
        </div>
      </div>

      <div class="game-sidebar">
        <div class="metrics-panel">
          <h3 class="panel-title">
            <i class="fas fa-chart-pie"></i>
            统计指标
          </h3>
          <div class="metrics-grid">
            <div class="metric-item">
              <span class="metric-label">最大回撤</span>
              <span class="metric-value negative">{{ (statistics.max_drawdown ?? 0).toFixed(2) }}%</span>
            </div>
            <div class="metric-item">
              <span class="metric-label">盈亏比</span>
              <span class="metric-value">{{ statistics.profit_loss_ratio }}</span>
            </div>
            <div class="metric-item">
              <span class="metric-label">本局收益</span>
              <span
                class="metric-value"
                :class="(statistics.total_return ?? 0) >= 0 ? 'positive' : 'negative'"
              >
                {{ (statistics.total_return ?? 0) >= 0 ? '+' : '' }}{{ (statistics.total_return ?? 0).toFixed(2) }}%
              </span>
            </div>
            <div class="metric-item">
              <span class="metric-label">已实现盈亏</span>
              <span
                class="metric-value"
                :class="(statistics.realized_pnl ?? 0) >= 0 ? 'positive' : 'negative'"
              >
                {{ (statistics.realized_pnl ?? 0) >= 0 ? '+' : '' }}{{ (statistics.realized_pnl ?? 0).toFixed(2) }}
              </span>
            </div>
            <div class="metric-item">
              <span class="metric-label">交易次数</span>
              <span class="metric-value">{{ statistics.trade_count }} 次</span>
            </div>
            <div class="metric-item">
              <span class="metric-label">盈/亏次数</span>
              <span class="metric-value">
                <span class="positive">{{ statistics.profit_trades }}</span>
                /
                <span class="negative">{{ statistics.loss_trades }}</span>
              </span>
            </div>
          </div>
        </div>

        <div class="trades-panel">
          <h3 class="panel-title">
            <i class="fas fa-list"></i>
            交易明细
          </h3>
          <div class="trades-list">
            <div v-if="trades.length === 0" class="no-trades">
              暂无交易记录
            </div>
            <div
              v-for="(trade, index) in trades.slice().reverse()"
              :key="index"
              class="trade-item"
              :class="trade.action"
            >
              <div class="trade-main">
                <span class="trade-action">{{ getActionLabel(trade.action) }}</span>
                <span class="trade-price">¥{{ (trade.price ?? 0).toFixed(2) }}</span>
              </div>
              <div class="trade-details">
                <span v-if="trade.quantity > 0">{{ (trade.quantity ?? 0).toFixed(2) }}股</span>
                <span v-if="trade.position_ratio > 0" class="trade-ratio">
                  {{ getPositionLabel(trade.position_ratio) }}
                </span>
                <span v-if="trade.realized_pnl !== 0" class="trade-pnl" :class="(trade.realized_pnl ?? 0) >= 0 ? 'positive' : 'negative'">
                  {{ (trade.realized_pnl ?? 0) >= 0 ? '+' : '' }}{{ (trade.realized_pnl ?? 0).toFixed(2) }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-if="showResult" class="result-modal">
      <div class="result-content">
        <div class="result-header">
          <h2>游戏结束</h2>
          <div class="result-stock">{{ stockName }} ({{ stockCode }})</div>
        </div>

        <div class="result-body">
          <div class="result-main">
            <div class="result-item large">
              <span class="result-label">最终资产</span>
              <span class="result-value">{{ formatMoney(finalAssets) }}</span>
            </div>
            <div class="result-item large">
              <span class="result-label">总收益率</span>
              <span
                class="result-value"
                :class="(statistics.total_return ?? 0) >= 0 ? 'positive' : 'negative'"
              >
                {{ (statistics.total_return ?? 0) >= 0 ? '+' : '' }}{{ (statistics.total_return ?? 0).toFixed(2) }}%
              </span>
            </div>
          </div>

          <div class="result-grid">
            <div class="result-item">
              <span class="result-label">最大回撤</span>
              <span class="result-value negative">{{ (statistics.max_drawdown ?? 0).toFixed(2) }}%</span>
            </div>
            <div class="result-item">
              <span class="result-label">盈亏比</span>
              <span class="result-value">{{ statistics.profit_loss_ratio }}</span>
            </div>
            <div class="result-item">
              <span class="result-label">交易次数</span>
              <span class="result-value">{{ statistics.trade_count }} 次</span>
            </div>
            <div class="result-item">
              <span class="result-label">胜率</span>
              <span class="result-value">
                {{ statistics.trade_count > 0 ? (((statistics.profit_trades ?? 0) / ((statistics.profit_trades ?? 0) + (statistics.loss_trades ?? 0))) * 100).toFixed(1) : 0 }}%
              </span>
            </div>
          </div>
        </div>

        <div class="result-actions">
          <button @click="resetGame" class="btn-primary">
            <i class="fas fa-redo"></i>
            再来一局
          </button>
          <button @click="showResult = false" class="btn-secondary">
            <i class="fas fa-eye"></i>
            查看详情
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import { request, ApiError } from '../services/api';
import TVKlineChart from '../components/charts/TVKlineChart.vue';
import MACDChart from '../components/charts/MACDChart.vue';

interface KlineData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

interface Trade {
  index: number;
  action: string;
  price: number;
  quantity: number;
  position_ratio: number;
  amount: number;
  realized_pnl: number;
  date: string;
}

interface Statistics {
  cash: number;
  position: number;
  avg_cost: number;
  current_price: number;
  total_assets: number;
  unrealized_pnl: number;
  realized_pnl: number;
  max_drawdown: number;
  profit_loss_ratio: number | string;
  total_return: number;
  trade_count: number;
  buy_count: number;
  sell_count: number;
  profit_trades: number;
  loss_trades: number;
  current_trade_index: number;
  total_trade_klines: number;
  history_klines_count: number;
  is_trading_phase: boolean;
  is_finished: boolean;
}

const isLoading = ref(false);
const actionLoading = ref(false);
const gameStarted = ref(false);
const showResult = ref(false);
const message = ref('');
const messageType = ref<'success' | 'error'>('success');

const sessionId = ref('');
const stockCode = ref('');
const stockName = ref('');
const finalAssets = ref(0);

const historyKlines = ref<KlineData[]>([]);
const tradeKlines = ref<KlineData[]>([]);
const currentKline = ref<KlineData | null>(null);
const historyKlinesCount = ref(0);
const totalTradeKlines = ref(0);
const currentTradeIndex = ref(0);

const trades = ref<Trade[]>([]);
const statistics = ref<Statistics>({
  cash: 10000,
  position: 0,
  avg_cost: 0,
  current_price: 0,
  total_assets: 10000,
  unrealized_pnl: 0,
  realized_pnl: 0,
  max_drawdown: 0,
  profit_loss_ratio: 0,
  total_return: 0,
  trade_count: 0,
  buy_count: 0,
  sell_count: 0,
  profit_trades: 0,
  loss_trades: 0,
  current_trade_index: 0,
  total_trade_klines: 30,
  history_klines_count: 60,
  is_trading_phase: true,
  is_finished: false
});

const positionRatio = ref(0.25);
const selectedIndicator = ref('ma');
const volatilityFilter = ref('random');
const macdChartRef = ref<InstanceType<typeof MACDChart> | null>(null);

const isTradingPhase = computed(() => statistics.value.is_trading_phase);
const isFinished = computed(() => statistics.value.is_finished);
const tradeProgressPercent = computed(() => {
  if (totalTradeKlines.value === 0) return 0;
  return ((currentTradeIndex.value + 1) / totalTradeKlines.value) * 100;
});

const displayKlines = computed(() => {
  const all = [...historyKlines.value, ...tradeKlines.value];
  if (currentKline.value && !tradeKlines.value.find(k => k.date === currentKline.value?.date)) {
    all.push(currentKline.value);
  }
  return all;
});

const tradeMarkers = computed(() => {
  return trades.value
    .filter(t => t.action === 'buy' || t.action === 'sell')
    .map(t => ({
      date: t.date,
      action: t.action as 'buy' | 'sell',
      price: t.price,
      quantity: t.quantity
    }));
});

function formatMoney(value: number): string {
  return '¥' + value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function getActionLabel(action: string): string {
  const labels: Record<string, string> = {
    buy: '买入',
    sell: '卖出'
  };
  return labels[action] || action;
}

function getPositionLabel(ratio: number): string {
  if (ratio === 0.25) return '1/4仓';
  if (ratio === 0.5) return '半仓';
  if (ratio === 1.0) return '全仓';
  return '';
}

function showMessage(msg: string, type: 'success' | 'error' = 'success') {
  message.value = msg;
  messageType.value = type;
  setTimeout(() => {
    message.value = '';
  }, 3000);
}

async function startGame() {
  isLoading.value = true;
  console.log('[KlineGame] 开始加载游戏...');
  try {
    const res = await request<any>('/game/start', {
      method: 'POST',
      body: JSON.stringify({
        initial_capital: 10000,
        volatility_filter: volatilityFilter.value
      })
    });
    console.log('[KlineGame] API响应完成');

    if (res.ok) {
      const data = res.data;
      console.log('[KlineGame] 数据解析开始, history_klines长度:', data.history_klines?.length);
      
      sessionId.value = data.session_id;
      stockCode.value = data.stock_code;
      stockName.value = data.stock_name;
      historyKlines.value = data.history_klines || [];
      currentKline.value = data.current_kline;
      historyKlinesCount.value = data.statistics.history_klines_count;
      totalTradeKlines.value = data.statistics.total_trade_klines;
      currentTradeIndex.value = data.statistics.current_trade_index;
      statistics.value = data.statistics;
      
      console.log('[KlineGame] 数据赋值完成, displayKlines长度:', historyKlines.value.length + 1);
      
      gameStarted.value = true;
      showMessage('游戏开始！先观察历史K线，再进行交易决策', 'success');
      console.log('[KlineGame] 游戏启动完成');
    } else {
      showMessage(res.message || '启动游戏失败', 'error');
    }
  } catch (error: any) {
    console.error('[KlineGame] 加载失败:', error);
    showMessage(error.message || '网络错误', 'error');
  } finally {
    isLoading.value = false;
    console.log('[KlineGame] isLoading设为false');
  }
}

async function handleNext() {
  if (actionLoading.value) return;
  actionLoading.value = true;

  try {
    const res = await request<any>('/game/next', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId.value
      })
    });

    if (res.ok) {
      const data = res.data;
      
      if (data.current_kline) {
        if (currentKline.value) {
          tradeKlines.value.push(currentKline.value);
        }
        currentKline.value = data.current_kline;
      }
      
      statistics.value = data.statistics;
      currentTradeIndex.value = data.statistics.current_trade_index;

      if (data.is_finished || data.statistics.is_finished) {
        await fetchResult();
      }
    } else {
      if ((res as any).detail?.data?.is_finished) {
        await fetchResult();
      } else {
        showMessage(res.message || '操作失败', 'error');
      }
    }
  } catch (error: any) {
    showMessage(error.message || '网络错误', 'error');
  } finally {
    actionLoading.value = false;
  }
}

async function handleFastForward(steps: number) {
  if (actionLoading.value) return;
  actionLoading.value = true;

  try {
    const res = await request<any>('/game/fast_forward', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId.value,
        steps: steps
      })
    });

    if (res.ok) {
      const data = res.data;
      
      // 将被跳过的K线添加到交易K线列表
      if (data.skipped_klines && data.skipped_klines.length > 0) {
        if (currentKline.value) {
          tradeKlines.value.push(currentKline.value);
        }
        tradeKlines.value.push(...data.skipped_klines);
      }
      
      if (data.current_kline) {
        currentKline.value = data.current_kline;
      }
      
      statistics.value = data.statistics;
      currentTradeIndex.value = data.statistics.current_trade_index;
      
      showMessage(`快进 ${data.skipped_klines?.length || 0} 根K线`, 'success');

      if (data.is_finished || data.statistics.is_finished) {
        await fetchResult();
      }
    } else {
      if ((res as any).detail?.data?.is_finished) {
        await fetchResult();
      } else {
        showMessage(res.message || '快进失败', 'error');
      }
    }
  } catch (error: any) {
    showMessage(error.message || '网络错误', 'error');
  } finally {
    actionLoading.value = false;
  }
}

async function handleBuy() {
  if (actionLoading.value) return;
  actionLoading.value = true;

  try {
    const res = await request<any>('/game/buy', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId.value,
        position_ratio: positionRatio.value
      })
    });

    if (res.ok) {
      const data = res.data;
      statistics.value = data.statistics;
      if (data.trade) {
        trades.value.push({
          ...data.trade,
          date: currentKline.value?.date || ''
        });
      }
      showMessage(`买入成功: ${(data.trade.quantity ?? 0).toFixed(2)}股 (${getPositionLabel(positionRatio.value)})`, 'success');
    } else {
      showMessage(res.message || '买入失败', 'error');
    }
  } catch (error: any) {
    showMessage(error.message || '网络错误', 'error');
  } finally {
    actionLoading.value = false;
  }
}

async function handleSell() {
  if (actionLoading.value) return;
  actionLoading.value = true;

  try {
    const res = await request<any>('/game/sell', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId.value,
        position_ratio: positionRatio.value
      })
    });

    if (res.ok) {
      const data = res.data;
      statistics.value = data.statistics;
      if (data.trade) {
        trades.value.push({
          ...data.trade,
          date: currentKline.value?.date || ''
        });
      }
      const realizedPnl = data.trade.realized_pnl ?? 0;
      const pnlText = realizedPnl >= 0 ? `盈利 ${realizedPnl.toFixed(2)}` : `亏损 ${Math.abs(realizedPnl).toFixed(2)}`;
      showMessage(`卖出成功: ${pnlText}`, realizedPnl >= 0 ? 'success' : 'error');
    } else {
      showMessage(res.message || '卖出失败', 'error');
    }
  } catch (error: any) {
    showMessage(error.message || '网络错误', 'error');
  } finally {
    actionLoading.value = false;
  }
}

async function fetchResult() {
  try {
    const res = await request<any>(`/game/result?session_id=${encodeURIComponent(sessionId.value)}`);

    if (res.ok) {
      const data = res.data;
      finalAssets.value = data.final_assets;
      statistics.value = data.statistics;
      showResult.value = true;
    }
  } catch (error) {
    console.error('获取结果失败:', error);
  }
}

async function handleChangeStock() {
  if (actionLoading.value) return;
  if (statistics.value.position > 0) {
    showMessage('请先清仓再换股', 'error');
    return;
  }

  actionLoading.value = true;
  try {
    const res = await request<any>('/game/change_stock', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId.value,
        volatility_filter: volatilityFilter.value
      })
    });

    if (res.ok) {
      const data = res.data;
      stockCode.value = data.stock_code;
      stockName.value = data.stock_name;
      historyKlines.value = data.history_klines || [];
      currentKline.value = data.current_kline;
      historyKlinesCount.value = data.statistics.history_klines_count;
      totalTradeKlines.value = data.statistics.total_trade_klines;
      currentTradeIndex.value = data.statistics.current_trade_index;
      statistics.value = data.statistics;
      tradeKlines.value = [];
      showMessage(`已切换到 ${data.stock_name}，继续交易！`, 'success');
    } else {
      showMessage(res.message || '换股失败', 'error');
    }
  } catch (error: any) {
    showMessage(error.message || '网络错误', 'error');
  } finally {
    actionLoading.value = false;
  }
}

async function handleEndGame() {
  if (actionLoading.value || currentTradeIndex.value < 60) return;
  
  actionLoading.value = true;
  try {
    const res = await request<any>('/game/end', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId.value
      })
    });

    if (res.ok) {
      await fetchResult();
    } else {
      showMessage(res.message || '结束游戏失败', 'error');
    }
  } catch (error: any) {
    showMessage(error.message || '网络错误', 'error');
  } finally {
    actionLoading.value = false;
  }
}

async function resetGame() {
  showResult.value = false;
  gameStarted.value = false;
  sessionId.value = '';
  stockCode.value = '';
  stockName.value = '';
  historyKlines.value = [];
  tradeKlines.value = [];
  currentKline.value = null;
  trades.value = [];
  currentTradeIndex.value = 0;
  historyKlinesCount.value = 0;
  totalTradeKlines.value = 0;
}
</script>

<style scoped>
.kline-game-page {
  padding: 20px;
  min-height: calc(100vh - 60px);
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  color: #fff;
}

.game-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}

.page-title i {
  color: #4ecdc4;
}

.header-actions {
  display: flex;
  gap: 10px;
}

.btn-primary, .btn-secondary {
  padding: 10px 20px;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: all 0.3s ease;
}

.btn-primary {
  background: linear-gradient(135deg, #4ecdc4 0%, #44a08d 100%);
  color: #fff;
}

.btn-primary:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 4px 15px rgba(78, 205, 196, 0.4);
}

.btn-secondary {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.2);
}

.btn-secondary:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.2);
}

.btn-primary:disabled, .btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.welcome-panel {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 60vh;
}

.welcome-content {
  text-align: center;
  max-width: 500px;
  padding: 40px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 20px;
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.welcome-icon {
  width: 80px;
  height: 80px;
  background: linear-gradient(135deg, #4ecdc4 0%, #44a08d 100%);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 20px;
  font-size: 36px;
}

.welcome-content h2 {
  font-size: 28px;
  margin-bottom: 15px;
}

.welcome-content p {
  color: rgba(255, 255, 255, 0.7);
  margin-bottom: 30px;
  line-height: 1.6;
}

.rules-summary {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 15px;
  margin-bottom: 30px;
}

.volatility-selector {
  margin-bottom: 25px;
  text-align: center;
}

.volatility-selector label {
  display: block;
  margin-bottom: 10px;
  color: #9ca3af;
  font-size: 14px;
}

.volatility-options {
  display: flex;
  justify-content: center;
  gap: 12px;
}

.volatility-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 20px;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.05);
  color: #9ca3af;
  cursor: pointer;
  transition: all 0.2s;
  font-size: 14px;
}

.volatility-btn:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}

.volatility-btn.active {
  background: rgba(59, 130, 246, 0.3);
  border-color: rgba(59, 130, 246, 0.5);
  color: #fff;
}

.volatility-btn i {
  font-size: 12px;
}

.rule-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 10px;
  font-size: 14px;
}

.rule-item i {
  color: #4ecdc4;
}

.btn-start {
  padding: 15px 40px;
  background: linear-gradient(135deg, #4ecdc4 0%, #44a08d 100%);
  color: #fff;
  border: none;
  border-radius: 30px;
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  transition: all 0.3s ease;
}

.btn-start:hover {
  transform: translateY(-3px);
  box-shadow: 0 6px 20px rgba(78, 205, 196, 0.5);
}

.loading-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 50vh;
  gap: 15px;
  font-size: 18px;
  color: rgba(255, 255, 255, 0.7);
}

.loading-panel i {
  font-size: 40px;
  color: #4ecdc4;
}

.game-container {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 20px;
}

.game-main {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.chart-section {
  background: rgba(255, 255, 255, 0.05);
  border-radius: 16px;
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 15px 20px;
  background: rgba(0, 0, 0, 0.2);
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.stock-info {
  display: flex;
  align-items: center;
  gap: 10px;
}

.stock-code {
  font-size: 18px;
  font-weight: 600;
  color: #4ecdc4;
}

.stock-name {
  font-size: 14px;
  color: rgba(255, 255, 255, 0.7);
}

.chart-controls {
  display: flex;
  align-items: center;
  gap: 15px;
}

.indicator-selector {
  display: flex;
  align-items: center;
  gap: 8px;
}

.indicator-selector label {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.7);
}

.indicator-select {
  padding: 6px 12px;
  background: rgba(255, 255, 255, 0.1);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 6px;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
}

.indicator-select option {
  background: #1a1a2e;
}

.phase-indicator {
  padding: 6px 12px;
  background: rgba(78, 205, 196, 0.2);
  border-radius: 20px;
  font-size: 13px;
  color: #4ecdc4;
  display: flex;
  align-items: center;
  gap: 6px;
}

.phase-indicator.trading {
  background: rgba(255, 107, 107, 0.2);
  color: #ff6b6b;
}

.chart-wrapper {
  position: relative;
  height: 400px;
}

.macd-wrapper {
  background: rgba(0, 0, 0, 0.2);
  border-radius: 12px;
  padding: 10px;
  margin-top: 10px;
}

.macd-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 5px 10px;
  margin-bottom: 5px;
}

.macd-label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.6);
  font-weight: 500;
}

.macd-legend {
  display: flex;
  gap: 12px;
}

.macd-legend .legend-item {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
}

.macd-legend .legend-item.dif {
  color: #3b82f6;
  background: rgba(59, 130, 246, 0.2);
}

.macd-legend .legend-item.dea {
  color: #f59e0b;
  background: rgba(245, 158, 11, 0.2);
}

.macd-legend .legend-item.histogram {
  color: #22c55e;
  background: rgba(34, 197, 94, 0.2);
}

.history-divider {
  position: absolute;
  bottom: 80px;
  left: 0;
  right: 0;
  padding: 8px;
  background: linear-gradient(180deg, transparent, rgba(78, 205, 196, 0.1));
  text-align: center;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
  border-top: 1px dashed rgba(78, 205, 196, 0.3);
}

.control-section {
  background: rgba(255, 255, 255, 0.05);
  border-radius: 16px;
  padding: 20px;
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.progress-panel {
  margin-bottom: 20px;
}

.progress-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.progress-label {
  font-size: 14px;
  font-weight: 500;
}

.progress-text {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.7);
}

.progress-bar {
  height: 6px;
  background: rgba(255, 255, 255, 0.1);
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #4ecdc4, #44a08d);
  border-radius: 3px;
  transition: width 0.3s ease;
}

.stats-panel {
  margin-bottom: 20px;
}

.stats-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 15px;
  margin-bottom: 10px;
}

.stat-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.stat-label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}

.stat-value {
  font-size: 16px;
  font-weight: 600;
}

.stat-value.highlight {
  color: #4ecdc4;
}

.stat-value.positive {
  color: #4ecdc4;
}

.stat-value.negative {
  color: #ff6b6b;
}

.position-selector {
  display: flex;
  align-items: center;
  gap: 15px;
  margin-bottom: 20px;
}

.selector-label {
  font-size: 14px;
  color: rgba(255, 255, 255, 0.7);
}

.position-options {
  display: flex;
  gap: 8px;
}

.position-btn {
  padding: 8px 16px;
  background: rgba(255, 255, 255, 0.1);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 8px;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.3s ease;
}

.position-btn:hover {
  background: rgba(255, 255, 255, 0.15);
}

.position-btn.active {
  background: linear-gradient(135deg, #4ecdc4 0%, #44a08d 100%);
  border-color: transparent;
}

.action-buttons {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.action-buttons button {
  padding: 12px;
  border: none;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: all 0.3s ease;
}

.btn-buy {
  background: linear-gradient(135deg, #4ecdc4 0%, #44a08d 100%);
  color: #fff;
}

.btn-buy:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 4px 15px rgba(78, 205, 196, 0.4);
}

.btn-sell {
  background: linear-gradient(135deg, #ff6b6b 0%, #ee5a5a 100%);
  color: #fff;
}

.btn-sell:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 4px 15px rgba(255, 107, 107, 0.4);
}

.btn-next {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.2);
}

.btn-next:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.2);
}

.btn-fast-forward {
  background: rgba(147, 51, 234, 0.3);
  color: #fff;
  border: 1px solid rgba(147, 51, 234, 0.5);
}

.btn-fast-forward:hover:not(:disabled) {
  background: rgba(147, 51, 234, 0.5);
}

.action-buttons button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  transform: none !important;
}

.secondary-buttons {
  display: flex;
  gap: 10px;
  margin-top: 10px;
}

.secondary-buttons button {
  flex: 1;
  padding: 10px 16px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.3s ease;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.btn-change-stock {
  background: rgba(147, 51, 234, 0.2);
  color: #a78bfa;
  border: 1px solid rgba(147, 51, 234, 0.3);
}

.btn-change-stock:hover:not(:disabled) {
  background: rgba(147, 51, 234, 0.3);
  transform: translateY(-1px);
}

.btn-end-game {
  background: rgba(239, 68, 68, 0.2);
  color: #f87171;
  border: 1px solid rgba(239, 68, 68, 0.3);
}

.btn-end-game:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.3);
  transform: translateY(-1px);
}

.secondary-buttons button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  transform: none !important;
}

.action-message {
  margin-top: 15px;
  padding: 12px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
}

.action-message.success {
  background: rgba(78, 205, 196, 0.2);
  color: #4ecdc4;
}

.action-message.error {
  background: rgba(255, 107, 107, 0.2);
  color: #ff6b6b;
}

.game-sidebar {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.metrics-panel, .trades-panel {
  background: rgba(255, 255, 255, 0.05);
  border-radius: 16px;
  padding: 20px;
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.panel-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 15px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-title i {
  color: #4ecdc4;
}

.metrics-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 15px;
}

.metric-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.metric-label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}

.metric-value {
  font-size: 14px;
  font-weight: 600;
}

.metric-value.positive {
  color: #4ecdc4;
}

.metric-value.negative {
  color: #ff6b6b;
}

.trades-list {
  max-height: 300px;
  overflow-y: auto;
}

.no-trades {
  text-align: center;
  color: rgba(255, 255, 255, 0.5);
  padding: 20px;
  font-size: 14px;
}

.trade-item {
  padding: 12px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 8px;
  margin-bottom: 8px;
  border-left: 3px solid;
}

.trade-item.buy {
  border-left-color: #4ecdc4;
}

.trade-item.sell {
  border-left-color: #ff6b6b;
}

.trade-main {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.trade-action {
  font-size: 13px;
  font-weight: 500;
}

.trade-item.buy .trade-action {
  color: #4ecdc4;
}

.trade-item.sell .trade-action {
  color: #ff6b6b;
}

.trade-price {
  font-size: 14px;
  font-weight: 600;
}

.trade-details {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.6);
}

.trade-ratio {
  background: rgba(255, 255, 255, 0.1);
  padding: 2px 6px;
  border-radius: 4px;
}

.trade-pnl.positive {
  color: #4ecdc4;
}

.trade-pnl.negative {
  color: #ff6b6b;
}

.result-modal {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.result-content {
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  border-radius: 20px;
  padding: 30px;
  max-width: 500px;
  width: 90%;
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.result-header {
  text-align: center;
  margin-bottom: 25px;
}

.result-header h2 {
  font-size: 28px;
  margin-bottom: 10px;
}

.result-stock {
  font-size: 14px;
  color: rgba(255, 255, 255, 0.7);
}

.result-body {
  margin-bottom: 25px;
}

.result-main {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 15px;
  margin-bottom: 20px;
}

.result-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 15px;
}

.result-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 15px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 10px;
}

.result-item.large {
  text-align: center;
}

.result-label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}

.result-value {
  font-size: 18px;
  font-weight: 600;
}

.result-item.large .result-value {
  font-size: 24px;
}

.result-value.positive {
  color: #4ecdc4;
}

.result-value.negative {
  color: #ff6b6b;
}

.result-actions {
  display: flex;
  gap: 15px;
  justify-content: center;
}

.result-actions button {
  padding: 12px 30px;
  border: none;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: all 0.3s ease;
}

@media (max-width: 1200px) {
  .game-container {
    grid-template-columns: 1fr;
  }
  
  .game-sidebar {
    flex-direction: row;
  }
  
  .metrics-panel, .trades-panel {
    flex: 1;
  }
}

@media (max-width: 768px) {
  .game-sidebar {
    flex-direction: column;
  }
  
  .rules-summary {
    grid-template-columns: 1fr;
  }
  
  .stats-row {
    grid-template-columns: 1fr;
  }
  
  .action-buttons {
    grid-template-columns: 1fr;
  }
}
</style>
