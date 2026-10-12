<template>
  <div class="watchlist-module">
    <div class="module-header">
      <div class="flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-[#f5a623]/10 flex items-center justify-center">
          <i class="fas fa-eye text-[#f5a623]"></i>
        </div>
        <div>
          <h3 class="text-lg font-semibold text-white">自选监控</h3>
          <p class="text-xs text-[#787b86]">设置监控条件，触发时飞书通知</p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <button
          @click="showFeishuConfig = true"
          class="icon-btn"
          :class="feishuConfig.is_configured ? 'text-[#089981]' : 'text-[#787b86]'"
          :title="feishuConfig.is_configured ? '飞书已配置' : '配置飞书'"
        >
          <i class="fas fa-bell"></i>
        </button>
        <button
          @click="handleCheckSignals"
          :disabled="watchlistStore.isLoading || watchlist.length === 0"
          class="btn-sm btn-primary"
        >
          <i class="fas fa-sync-alt" :class="{ 'animate-spin': watchlistStore.isLoading }"></i>
          <span>检测信号</span>
        </button>
        <button @click="openAddModal" class="btn-sm btn-success">
          <i class="fas fa-plus"></i>
          <span>添加自选</span>
        </button>
      </div>
    </div>

    <div v-if="lastCheckResult && lastCheckResult.signals.length > 0" class="signal-alert mb-4">
      <div class="alert-header">
        <i class="fas fa-exclamation-triangle text-[#f5a623]"></i>
        <span class="font-semibold">检测到 {{ lastCheckResult.signals.length }} 个信号</span>
        <span v-if="lastCheckResult.notified" class="badge badge-success">已推送飞书</span>
      </div>
      <div class="alert-content">
        <div 
          v-for="sig in lastCheckResult.signals" 
          :key="`${sig.stock_code}-${sig.condition_key}`"
          class="signal-item-row"
        >
          <span :class="getSeverityClass(sig.severity)">
            {{ getSeverityIcon(sig.severity) }}
          </span>
          <span class="signal-stock">{{ sig.stock_name }}({{ sig.stock_code }})</span>
          <span class="signal-condition">{{ sig.condition_name }}</span>
          <span class="signal-message">{{ sig.message }}</span>
        </div>
      </div>
    </div>

    <div v-if="watchlist.length === 0" class="empty-state">
      <i class="fas fa-binoculars empty-icon"></i>
      <p class="text-[#787b86]">暂无自选股票</p>
      <p class="text-xs text-[#5a5d63] mt-1">从股票筛选器添加或手动添加</p>
    </div>

    <div v-else class="watchlist-pools">
      <!-- 核心池A -->
      <div v-if="poolAItems.length > 0" class="pool-section pool-a">
        <div class="pool-header">
          <div class="pool-title">
            <span class="pool-badge badge-a">A</span>
            <span class="pool-name">核心池</span>
            <span class="pool-desc">主升浪/龙头右侧标的（重点实操）</span>
          </div>
          <span class="pool-count">{{ poolAItems.length }} 只</span>
        </div>
        <table class="watchlist-table">
          <thead>
            <tr>
              <th class="w-10">
                <input 
                  type="checkbox" 
                  :checked="isPoolAllSelected('A')"
                  @change="togglePoolSelectAll('A')"
                  class="form-checkbox"
                />
              </th>
              <th>股票</th>
              <th>买入条件</th>
              <th>监控条件</th>
              <th>通知</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr 
              v-for="item in poolAItems" 
              :key="item.id"
              :class="{ 'row-inactive': !item.is_active }"
            >
              <td>
                <input 
                  type="checkbox" 
                  :checked="selectedItems.includes(item.id!)"
                  @change="toggleSelectItem(item.id!)"
                  class="form-checkbox"
                />
              </td>
              <td>
                <div class="stock-info-cell">
                  <span class="stock-name">{{ item.stock_name }}</span>
                  <span class="stock-code">{{ item.stock_code }}</span>
                </div>
              </td>
              <td>
                <span class="price-cell buy">
                  {{ getBuyConditionLabel(item) }}
                </span>
              </td>
              <td>
                <div class="conditions-cell">
                  <span 
                    v-for="cond in (item.conditions || []).filter(c => c.enabled).slice(0, 2)" 
                    :key="cond.key"
                    class="condition-tag-mini"
                  >
                    {{ getConditionName(cond.key) }}
                  </span>
                  <span v-if="(item.conditions || []).filter(c => c.enabled).length > 2" class="more-tag">
                    +{{ (item.conditions || []).filter(c => c.enabled).length - 2 }}
                  </span>
                  <span v-if="!(item.conditions || []).filter(c => c.enabled).length" class="text-[#5a5d63] text-xs">-</span>
                </div>
              </td>
              <td>
                <i 
                  class="fas fa-bell" 
                  :class="item.feishu_notify ? 'text-[#089981]' : 'text-[#5a5d63]'"
                  :title="item.feishu_notify ? '通知开启' : '通知关闭'"
                ></i>
              </td>
              <td>
                <div class="actions-cell">
                  <button @click="openEditModal(item)" class="action-btn" title="编辑">
                    <i class="fas fa-edit"></i>
                  </button>
                  <button @click="handleDelete(item)" class="action-btn text-[#f23645]" title="删除">
                    <i class="fas fa-trash"></i>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 观察池B -->
      <div v-if="poolBItems.length > 0" class="pool-section pool-b">
        <div class="pool-header">
          <div class="pool-title">
            <span class="pool-badge badge-b">B</span>
            <span class="pool-name">观察池</span>
            <span class="pool-desc">高波剧震/乖离率过大（等待结构修复）</span>
          </div>
          <span class="pool-count">{{ poolBItems.length }} 只</span>
        </div>
        <table class="watchlist-table">
          <thead>
            <tr>
              <th class="w-10">
                <input 
                  type="checkbox" 
                  :checked="isPoolAllSelected('B')"
                  @change="togglePoolSelectAll('B')"
                  class="form-checkbox"
                />
              </th>
              <th>股票</th>
              <th>买入条件</th>
              <th>监控条件</th>
              <th>通知</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr 
              v-for="item in poolBItems" 
              :key="item.id"
              :class="{ 'row-inactive': !item.is_active }"
            >
              <td>
                <input 
                  type="checkbox" 
                  :checked="selectedItems.includes(item.id!)"
                  @change="toggleSelectItem(item.id!)"
                  class="form-checkbox"
                />
              </td>
              <td>
                <div class="stock-info-cell">
                  <span class="stock-name">{{ item.stock_name }}</span>
                  <span class="stock-code">{{ item.stock_code }}</span>
                </div>
              </td>
              <td>
                <span class="price-cell buy">
                  {{ getBuyConditionLabel(item) }}
                </span>
              </td>
              <td>
                <div class="conditions-cell">
                  <span 
                    v-for="cond in (item.conditions || []).filter(c => c.enabled).slice(0, 2)" 
                    :key="cond.key"
                    class="condition-tag-mini"
                  >
                    {{ getConditionName(cond.key) }}
                  </span>
                  <span v-if="(item.conditions || []).filter(c => c.enabled).length > 2" class="more-tag">
                    +{{ (item.conditions || []).filter(c => c.enabled).length - 2 }}
                  </span>
                  <span v-if="!(item.conditions || []).filter(c => c.enabled).length" class="text-[#5a5d63] text-xs">-</span>
                </div>
              </td>
              <td>
                <i 
                  class="fas fa-bell" 
                  :class="item.feishu_notify ? 'text-[#089981]' : 'text-[#5a5d63]'"
                  :title="item.feishu_notify ? '通知开启' : '通知关闭'"
                ></i>
              </td>
              <td>
                <div class="actions-cell">
                  <button @click="openEditModal(item)" class="action-btn" title="编辑">
                    <i class="fas fa-edit"></i>
                  </button>
                  <button @click="handleDelete(item)" class="action-btn text-[#f23645]" title="删除">
                    <i class="fas fa-trash"></i>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 观察池C -->
      <div v-if="poolCItems.length > 0" class="pool-section pool-c">
        <div class="pool-header">
          <div class="pool-title">
            <span class="pool-badge badge-c">C</span>
            <span class="pool-name">观察池</span>
            <span class="pool-desc">趋势破位/左侧运行（观察修复情况）</span>
          </div>
          <span class="pool-count">{{ poolCItems.length }} 只</span>
        </div>
        <table class="watchlist-table">
          <thead>
            <tr>
              <th class="w-10">
                <input 
                  type="checkbox" 
                  :checked="isPoolAllSelected('C')"
                  @change="togglePoolSelectAll('C')"
                  class="form-checkbox"
                />
              </th>
              <th>股票</th>
              <th>状态</th>
              <th>监控条件</th>
              <th>通知</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr 
              v-for="item in poolCItems" 
              :key="item.id"
              :class="{ 'row-inactive': !item.is_active }"
            >
              <td>
                <input 
                  type="checkbox" 
                  :checked="selectedItems.includes(item.id!)"
                  @change="toggleSelectItem(item.id!)"
                  class="form-checkbox"
                />
              </td>
              <td>
                <div class="stock-info-cell">
                  <span class="stock-name">{{ item.stock_name }}</span>
                  <span class="stock-code">{{ item.stock_code }}</span>
                </div>
              </td>
              <td>
                <span class="status-tag">{{ getPoolCStatus(item) }}</span>
              </td>
              <td>
                <div class="conditions-cell">
                  <span 
                    v-for="cond in (item.conditions || []).filter(c => c.enabled).slice(0, 2)" 
                    :key="cond.key"
                    class="condition-tag-mini"
                  >
                    {{ getConditionName(cond.key) }}
                  </span>
                  <span v-if="(item.conditions || []).filter(c => c.enabled).length > 2" class="more-tag">
                    +{{ (item.conditions || []).filter(c => c.enabled).length - 2 }}
                  </span>
                  <span v-if="!(item.conditions || []).filter(c => c.enabled).length" class="text-[#5a5d63] text-xs">-</span>
                </div>
              </td>
              <td>
                <i 
                  class="fas fa-bell" 
                  :class="item.feishu_notify ? 'text-[#089981]' : 'text-[#5a5d63]'"
                  :title="item.feishu_notify ? '通知开启' : '通知关闭'"
                ></i>
              </td>
              <td>
                <div class="actions-cell">
                  <button @click="openEditModal(item)" class="action-btn" title="编辑">
                    <i class="fas fa-edit"></i>
                  </button>
                  <button @click="handleDelete(item)" class="action-btn text-[#f23645]" title="删除">
                    <i class="fas fa-trash"></i>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      
      <div v-if="selectedItems.length > 0" class="batch-actions-bar">
        <span class="selected-count">已选择 {{ selectedItems.length }} 项</span>
        <div class="batch-buttons">
          <button @click="openBatchConditionModal" class="btn-batch">
            <i class="fas fa-filter"></i>
            批量设置条件
          </button>
          <button @click="batchEnableNotify" class="btn-batch">
            <i class="fas fa-bell"></i>
            开启通知
          </button>
          <button @click="batchDisableNotify" class="btn-batch">
            <i class="fas fa-bell-slash"></i>
            关闭通知
          </button>
          <button @click="batchDelete" class="btn-batch btn-danger">
            <i class="fas fa-trash"></i>
            批量删除
          </button>
        </div>
      </div>
    </div>

    <a-modal
      v-model:open="showModal"
      :title="editingItem ? '编辑自选' : '添加自选'"
      :footer="null"
      width="600px"
      class="watchlist-modal"
    >
      <form @submit.prevent="handleSubmit" class="modal-form">
        <div class="form-row">
          <div class="form-group flex-1">
            <label class="form-label">股票代码 *</label>
            <StockSearchInput
              v-model="form.stock_code"
              placeholder="输入代码或名称搜索"
              :disabled="!!editingItem"
              @select="handleStockSelect"
            />
          </div>
          <div class="form-group flex-1">
            <label class="form-label">股票名称 *</label>
            <input
              v-model="form.stock_name"
              type="text"
              required
              readonly
              class="form-input"
              placeholder="自动填充"
            />
          </div>
        </div>

        <div class="form-section">
          <h4 class="section-title">
            <i class="fas fa-dollar-sign text-[#2962ff]"></i>
            目标价格
          </h4>
          <div class="form-row">
            <div class="form-group flex-1">
              <label class="form-label">买入目标价</label>
              <input
                v-model.number="form.buy_target_price"
                type="number"
                step="0.01"
                class="form-input"
                placeholder="价格跌到此价位提醒"
              />
            </div>
            <div class="form-group flex-1">
              <label class="form-label">卖出目标价</label>
              <input
                v-model.number="form.sell_target_price"
                type="number"
                step="0.01"
                class="form-input"
                placeholder="价格涨到此价位提醒"
              />
            </div>
          </div>
        </div>

        <div class="form-section">
          <h4 class="section-title">
            <i class="fas fa-chart-line text-[#f5a623]"></i>
            技术指标条件
            <button type="button" @click="openConditionSelector" class="add-condition-btn">
              <i class="fas fa-plus"></i>
              添加条件
            </button>
          </h4>
          
          <div v-if="form.conditions && form.conditions.length > 0" class="conditions-editor">
            <div 
              v-for="(cond, index) in form.conditions" 
              :key="index"
              class="condition-row"
            >
              <div class="condition-info">
                <span class="condition-category">{{ getConditionCategory(cond.key) }}</span>
                <span class="condition-name">{{ getConditionName(cond.key) }}</span>
              </div>
              <div class="condition-params">
                <template v-if="getConditionParams(cond.key).length > 0">
                  <div 
                    v-for="param in getConditionParams(cond.key)" 
                    :key="param"
                    class="param-input"
                  >
                    <label>{{ param }}</label>
                    <input
                      v-model.number="cond.params[param]"
                      type="number"
                      step="0.1"
                      class="form-input-sm"
                    />
                  </div>
                </template>
              </div>
              <div class="condition-actions">
                <label class="switch">
                  <input v-model="cond.enabled" type="checkbox" />
                  <span class="slider"></span>
                </label>
                <button type="button" @click="removeCondition(index)" class="remove-btn">
                  <i class="fas fa-times"></i>
                </button>
              </div>
            </div>
          </div>
          <div v-else class="no-conditions">
            <p>暂无技术指标条件，点击上方"添加条件"按钮</p>
          </div>
        </div>

        <div class="form-section">
          <h4 class="section-title">
            <i class="fas fa-sticky-note text-[#787b86]"></i>
            备注与标签
          </h4>
          <div class="form-group">
            <label class="form-label">备注</label>
            <input
              v-model="form.notes"
              type="text"
              class="form-input"
              placeholder="投资逻辑、关注理由等"
            />
          </div>
          <div class="form-group">
            <label class="form-label">标签</label>
            <div class="tags-input">
              <span v-for="(tag, index) in form.tags" :key="index" class="tag-item">
                {{ tag }}
                <button type="button" @click="removeTag(index)" class="tag-remove">
                  <i class="fas fa-times"></i>
                </button>
              </span>
              <input
                v-model="newTag"
                type="text"
                class="tag-input"
                placeholder="输入标签后回车"
                @keydown.enter.prevent="addTag"
              />
            </div>
          </div>
        </div>

        <div class="form-row">
          <label class="checkbox-label">
            <input v-model="form.is_active" type="checkbox" class="form-checkbox" />
            <span>启用监控</span>
          </label>
          <label class="checkbox-label">
            <input v-model="form.feishu_notify" type="checkbox" class="form-checkbox" />
            <span>飞书通知</span>
          </label>
        </div>

        <div class="form-actions">
          <button type="button" @click="showModal = false" class="btn btn-secondary">
            取消
          </button>
          <button type="submit" :disabled="watchlistStore.isLoading" class="btn btn-primary">
            {{ editingItem ? '保存' : '添加' }}
          </button>
        </div>
      </form>
    </a-modal>

    <a-modal
      v-model:open="showConditionSelector"
      title="选择监控条件"
      :footer="null"
      width="500px"
    >
      <div class="condition-selector">
        <div 
          v-for="(category, catKey) in supportedConditions" 
          :key="catKey"
          class="condition-category-group"
        >
          <h4 class="category-title">{{ category.name }}</h4>
          <div class="condition-options">
            <button
              v-for="cond in category.conditions"
              :key="cond.key"
              type="button"
              class="condition-option"
              :class="{ selected: isConditionSelected(cond.key) }"
              :disabled="isConditionSelected(cond.key)"
              @click="addCondition(cond.key, catKey)"
            >
              <span class="cond-name">{{ cond.name }}</span>
              <span class="cond-desc">{{ cond.desc }}</span>
            </button>
          </div>
        </div>
      </div>
    </a-modal>

    <a-modal
      v-model:open="showFeishuConfig"
      title="飞书通知配置"
      :footer="null"
      width="450px"
    >
      <div class="feishu-config">
        <div class="config-hint">
          <i class="fas fa-info-circle text-[#2962ff]"></i>
          <p>在飞书群组中添加自定义机器人，获取 Webhook 地址</p>
        </div>

        <div class="form-group">
          <label class="form-label">Webhook URL</label>
          <input
            v-model="feishuWebhookInput"
            type="text"
            class="form-input"
            placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..."
          />
          <p v-if="feishuConfig.is_configured && !feishuWebhookInput" class="config-status">
            <i class="fas fa-check-circle text-[#089981]"></i>
            已配置: {{ feishuConfig.masked_url }}
          </p>
        </div>

        <div class="form-actions">
          <button @click="handleTestFeishu" :disabled="!feishuWebhookInput && !feishuConfig.is_configured" class="btn btn-secondary">
            <i class="fas fa-paper-plane"></i>
            测试推送
          </button>
          <button @click="handleSaveFeishu" :disabled="!feishuWebhookInput" class="btn btn-primary">
            保存配置
          </button>
        </div>
      </div>
    </a-modal>

    <!-- Batch Condition Modal -->
    <a-modal
      v-model:open="showBatchConditionModal"
      title="批量设置监控条件"
      :footer="null"
      width="600px"
    >
      <div class="batch-condition-modal">
        <p class="modal-description">
          为选中的 <strong>{{ selectedItems.length }}</strong> 只股票批量添加监控条件
        </p>
        
        <div class="condition-selector">
          <div 
            v-for="(category, catKey) in supportedConditions" 
            :key="catKey"
            class="condition-category-group"
          >
            <h4 class="category-title">{{ category.name }}</h4>
            <div class="condition-options">
              <button
                v-for="cond in category.conditions"
                :key="cond.key"
                type="button"
                class="condition-option"
                :class="{ selected: isBatchConditionSelected(cond.key) }"
                :disabled="isBatchConditionSelected(cond.key)"
                @click="addBatchCondition(cond.key)"
              >
                <span class="cond-name">{{ cond.name }}</span>
                <span class="cond-desc">{{ cond.desc }}</span>
              </button>
            </div>
          </div>
        </div>

        <div v-if="batchConditions.length > 0" class="selected-conditions">
          <h4 class="section-title">已选择的条件</h4>
          <div class="conditions-editor">
            <div 
              v-for="(cond, index) in batchConditions" 
              :key="index"
              class="condition-row"
            >
              <div class="condition-info">
                <span class="condition-category">{{ getConditionCategory(cond.key) }}</span>
                <span class="condition-name">{{ getConditionName(cond.key) }}</span>
              </div>
              <div class="condition-params">
                <template v-if="getConditionParams(cond.key).length > 0">
                  <div 
                    v-for="param in getConditionParams(cond.key)" 
                    :key="param"
                    class="param-input"
                  >
                    <label>{{ param }}</label>
                    <input
                      v-model.number="cond.params[param]"
                      type="number"
                      step="0.1"
                      class="form-input-sm"
                    />
                  </div>
                </template>
              </div>
              <div class="condition-actions">
                <label class="switch">
                  <input v-model="cond.enabled" type="checkbox" />
                  <span class="slider"></span>
                </label>
                <button type="button" @click="removeBatchCondition(index)" class="remove-btn">
                  <i class="fas fa-times"></i>
                </button>
              </div>
            </div>
          </div>
        </div>

        <div class="form-actions">
          <button @click="showBatchConditionModal = false" class="btn btn-secondary">
            取消
          </button>
          <button @click="applyBatchConditions" class="btn btn-primary">
            应用到选中股票
          </button>
        </div>
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { message, Modal } from 'ant-design-vue';
import { useWatchlistStore, type WatchItem } from '../store/portfolioStore';
import StockSearchInput from './StockSearchInput.vue';

const watchlistStore = useWatchlistStore();

const watchlist = computed(() => watchlistStore.watchlist);
const feishuConfig = computed(() => watchlistStore.feishuConfig);
const lastCheckResult = computed(() => watchlistStore.lastCheckResult);

const poolAItems = computed(() => {
  return watchlist.value.filter(item => 
    item.tags?.some(t => t.includes('核心池A') || t === '核心池')
  );
});

const poolBItems = computed(() => {
  return watchlist.value.filter(item => 
    item.tags?.some(t => t.includes('观察池B'))
  );
});

const poolCItems = computed(() => {
  return watchlist.value.filter(item => 
    item.tags?.some(t => t.includes('观察池C'))
  );
});

const showModal = ref(false);
const showFeishuConfig = ref(false);
const showConditionSelector = ref(false);
const showBatchConditionModal = ref(false);
const editingItem = ref<WatchItem | null>(null);
const feishuWebhookInput = ref('');
const newTag = ref('');
const supportedConditions = ref<Record<string, any>>({});
const selectedItems = ref<number[]>([]);
const batchConditions = ref<Array<{key: string, enabled: boolean, params: Record<string, any>}>>([]);

const isAllSelected = computed(() => {
  if (watchlist.value.length === 0) return false;
  return selectedItems.value.length === watchlist.value.length;
});

function isPoolAllSelected(pool: string): boolean {
  const items = pool === 'A' ? poolAItems.value : pool === 'B' ? poolBItems.value : poolCItems.value;
  if (items.length === 0) return false;
  return items.every(item => selectedItems.value.includes(item.id!));
}

function togglePoolSelectAll(pool: string) {
  const items = pool === 'A' ? poolAItems.value : pool === 'B' ? poolBItems.value : poolCItems.value;
  const ids = items.map(item => item.id!).filter(Boolean);
  
  if (isPoolAllSelected(pool)) {
    selectedItems.value = selectedItems.value.filter(id => !ids.includes(id));
  } else {
    const newIds = ids.filter(id => !selectedItems.value.includes(id));
    selectedItems.value = [...selectedItems.value, ...newIds];
  }
}

function getStopLossLabel(item: WatchItem): string {
  const hasMa5Down = item.conditions?.some(c => c.key === 'ma5_break_down' && c.enabled);
  const hasMa10Down = item.conditions?.some(c => c.key === 'ma10_break_down' && c.enabled);
  
  if (hasMa5Down) return '跌破5日线';
  if (hasMa10Down) return '跌破10日线';
  return '-';
}

function getBuyConditionLabel(item: WatchItem): string {
  const hasMa5Up = item.conditions?.some(c => c.key === 'ma5_break_up' && c.enabled);
  const hasMa10Up = item.conditions?.some(c => c.key === 'ma10_break_up' && c.enabled);
  const hasMaBull = item.conditions?.some(c => c.key === 'ma_bull_alignment' && c.enabled);
  const hasVolSurge = item.conditions?.some(c => c.key === 'volume_surge' && c.enabled);
  
  if (hasMa5Up) return '回踩5日线买入';
  if (hasMa10Up) return '回踩10日线买入';
  if (hasMaBull) return '均线多头确认';
  if (hasVolSurge) return '放量突破';
  
  const tag = item.tags?.find(t => t.includes('主升浪') || t.includes('慢牛') || t.includes('突破'));
  if (tag) return tag;
  
  return '等待信号';
}

function getPoolCStatus(item: WatchItem): string {
  const tag = item.tags?.find(t => 
    t.includes('断头铡刀') || t.includes('A杀') || t.includes('均线空头') || 
    t.includes('趋势破位') || t.includes('下降通道') || t.includes('震荡')
  );
  return tag || '等待修复';
}

function toggleSelectAll() {
  if (isAllSelected.value) {
    selectedItems.value = [];
  } else {
    selectedItems.value = watchlist.value.map(item => item.id!).filter(Boolean);
  }
}

function toggleSelectItem(id: number) {
  const index = selectedItems.value.indexOf(id);
  if (index > -1) {
    selectedItems.value.splice(index, 1);
  } else {
    selectedItems.value.push(id);
  }
}

function openBatchConditionModal() {
  batchConditions.value = [];
  showBatchConditionModal.value = true;
}

async function batchEnableNotify() {
  Modal.confirm({
    title: '确认开启通知',
    content: `确定要为选中的 ${selectedItems.value.length} 只股票开启飞书通知吗？`,
    async onOk() {
      for (const id of selectedItems.value) {
        const item = watchlist.value.find(w => w.id === id);
        if (item) {
          await watchlistStore.updateWatchlistItem({ ...item, feishu_notify: true });
        }
      }
      message.success('批量开启通知成功');
      selectedItems.value = [];
    }
  });
}

async function batchDisableNotify() {
  Modal.confirm({
    title: '确认关闭通知',
    content: `确定要为选中的 ${selectedItems.value.length} 只股票关闭飞书通知吗？`,
    async onOk() {
      for (const id of selectedItems.value) {
        const item = watchlist.value.find(w => w.id === id);
        if (item) {
          await watchlistStore.updateWatchlistItem({ ...item, feishu_notify: false });
        }
      }
      message.success('批量关闭通知成功');
      selectedItems.value = [];
    }
  });
}

async function batchDelete() {
  Modal.confirm({
    title: '确认删除',
    content: `确定要删除选中的 ${selectedItems.value.length} 只股票监控吗？此操作不可恢复。`,
    okType: 'danger',
    async onOk() {
      for (const id of selectedItems.value) {
        await watchlistStore.deleteWatchlistItem(id);
      }
      message.success('批量删除成功');
      selectedItems.value = [];
    }
  });
}

async function applyBatchConditions() {
  const enabledConditions = batchConditions.value.filter(c => c.enabled);
  if (enabledConditions.length === 0) {
    message.warning('请至少选择一个条件');
    return;
  }
  
  for (const id of selectedItems.value) {
    const item = watchlist.value.find(w => w.id === id);
    if (item) {
      const existingConditions = item.conditions || [];
      const newConditions = enabledConditions.map(c => ({
        key: c.key,
        enabled: true,
        params: { ...c.params }
      }));
      await watchlistStore.updateWatchlistItem({
        ...item,
        conditions: [...existingConditions, ...newConditions]
      });
    }
  }
  message.success('批量设置条件成功');
  showBatchConditionModal.value = false;
  selectedItems.value = [];
}

function addBatchCondition(key: string) {
  if (batchConditions.value.some(c => c.key === key)) return;
  
  const condDef = findConditionDefinition(key);
  const params: Record<string, any> = {};
  if (condDef && condDef.params) {
    condDef.params.forEach((p: string) => {
      params[p] = getDefaultParamValue(p);
    });
  }
  
  batchConditions.value.push({
    key,
    enabled: true,
    params
  });
}

function removeBatchCondition(index: number) {
  batchConditions.value.splice(index, 1);
}

const form = ref<WatchItem>({
  stock_code: '',
  stock_name: '',
  buy_target_price: undefined,
  sell_target_price: undefined,
  conditions: [],
  notes: '',
  tags: [],
  is_active: true,
  feishu_notify: true
});

onMounted(async () => {
  await watchlistStore.fetchWatchlist();
  await watchlistStore.fetchFeishuConfig();
  await fetchSupportedConditions();
});

async function fetchSupportedConditions() {
  try {
    const res = await request<Record<string, any>>('/portfolio/watchlist/conditions');
    if (res.ok) {
      supportedConditions.value = res.data;
    }
  } catch (error) {
    console.error('获取监控条件失败:', error);
  }
}

function openAddModal() {
  editingItem.value = null;
  form.value = {
    stock_code: '',
    stock_name: '',
    buy_target_price: undefined,
    sell_target_price: undefined,
    conditions: [],
    notes: '',
    tags: [],
    is_active: true,
    feishu_notify: true
  };
  showModal.value = true;
}

function openEditModal(item: WatchItem) {
  editingItem.value = item;
  form.value = { 
    ...item,
    conditions: item.conditions ? [...item.conditions] : [],
    tags: item.tags ? [...item.tags] : []
  };
  showModal.value = true;
}

function handleStockSelect(stock: { code: string; name: string }) {
  form.value.stock_code = stock.code;
  form.value.stock_name = stock.name;
}

async function handleSubmit() {
  if (!form.value.stock_code || !form.value.stock_name) {
    message.error('请选择股票');
    return;
  }

  if (editingItem.value) {
    const success = await watchlistStore.updateWatchlistItem({
      ...form.value,
      id: editingItem.value.id
    });
    if (success) {
      message.success('更新成功');
      showModal.value = false;
    }
  } else {
    const id = await watchlistStore.addWatchlistItem(form.value);
    if (id) {
      message.success('添加成功');
      showModal.value = false;
    }
  }
}

async function handleDelete(item: WatchItem) {
  if (!item.id) return;
  
  Modal.confirm({
    title: '确认删除',
    content: `确定要删除 ${item.stock_name} 的监控吗？`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      const success = await watchlistStore.deleteWatchlistItem(item.id);
      if (success) {
        message.success('删除成功');
      }
    }
  });
}

async function handleCheckSignals() {
  const result = await watchlistStore.checkSignals();
  if (result) {
    if (result.notification_count > 0) {
      message.success(`检测到 ${result.notification_count} 个信号${result.notified ? '，已推送飞书' : ''}`);
    } else {
      message.info('暂无触发信号');
    }
  }
}

async function handleSaveFeishu() {
  if (!feishuWebhookInput.value) return;
  
  const success = await watchlistStore.updateFeishuConfig(feishuWebhookInput.value);
  if (success) {
    message.success('飞书配置已保存');
    feishuWebhookInput.value = '';
    showFeishuConfig.value = false;
  }
}

async function handleTestFeishu() {
  const success = await watchlistStore.testFeishuPush(feishuWebhookInput.value || undefined);
  if (success) {
    message.success('测试消息已发送');
  } else {
    message.error('发送失败，请检查 Webhook 地址');
  }
}

function openConditionSelector() {
  showConditionSelector.value = true;
}

function isConditionSelected(key: string): boolean {
  return form.value.conditions?.some(c => c.key === key) || false;
}

function isBatchConditionSelected(key: string): boolean {
  return batchConditions.value.some(c => c.key === key);
}

function addCondition(key: string, category: string) {
  if (!form.value.conditions) {
    form.value.conditions = [];
  }
  
  const condDef = findConditionDefinition(key);
  const params: Record<string, any> = {};
  if (condDef && condDef.params) {
    condDef.params.forEach((p: string) => {
      params[p] = getDefaultParamValue(p);
    });
  }
  
  form.value.conditions.push({
    key,
    enabled: true,
    params
  });
  
  showConditionSelector.value = false;
}

function removeCondition(index: number) {
  form.value.conditions?.splice(index, 1);
}

function findConditionDefinition(key: string): any {
  for (const cat of Object.values(supportedConditions.value)) {
    const cond = (cat as any).conditions?.find((c: any) => c.key === key);
    if (cond) return cond;
  }
  return null;
}

function getConditionName(key: string): string {
  const cond = findConditionDefinition(key);
  return cond?.name || key;
}

function getConditionCategory(key: string): string {
  for (const [catKey, cat] of Object.entries(supportedConditions.value)) {
    if ((cat as any).conditions?.some((c: any) => c.key === key)) {
      return (cat as any).name || catKey;
    }
  }
  return '';
}

function getConditionParams(key: string): string[] {
  const cond = findConditionDefinition(key);
  return cond?.params || [];
}

function getDefaultParamValue(param: string): number {
  const defaults: Record<string, number> = {
    threshold: 30,
    target_price: 0,
    multiplier: 2.0,
    ratio: 0.5
  };
  return defaults[param] || 0;
}

function addTag() {
  if (newTag.value.trim() && !form.value.tags?.includes(newTag.value.trim())) {
    if (!form.value.tags) form.value.tags = [];
    form.value.tags.push(newTag.value.trim());
    newTag.value = '';
  }
}

function removeTag(index: number) {
  form.value.tags?.splice(index, 1);
}

function formatNotifyTime(time: string): string {
  if (!time) return '';
  const date = new Date(time);
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  
  if (diff < 60000) return '刚刚';
  if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`;
  return time.split(' ')[0];
}

function getSeverityClass(severity: string): string {
  const classes: Record<string, string> = {
    buy: 'severity-buy',
    sell: 'severity-sell',
    warning: 'severity-warning',
    info: 'severity-info'
  };
  return classes[severity] || '';
}

function getSeverityIcon(severity: string): string {
  const icons: Record<string, string> = {
    buy: '🟢',
    sell: '🔴',
    warning: '🟡',
    info: '🔵'
  };
  return icons[severity] || '⚪';
}
</script>

<style scoped lang="scss">
.watchlist-module {
  background: #1a1f2e;
  border-radius: 12px;
  padding: 16px;
}

.module-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.btn-sm {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  transition: all 0.2s;
  
  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.btn-primary {
  background: #2962ff;
  color: white;
  border: none;
  
  &:hover:not(:disabled) {
    background: #1e4bd8;
  }
}

.btn-success {
  background: #089981;
  color: white;
  border: none;
  
  &:hover:not(:disabled) {
    background: #067a66;
  }
}

.btn-secondary {
  background: #2d2d30;
  color: #d1d4dc;
  border: 1px solid #3e3e42;
  
  &:hover:not(:disabled) {
    background: #3e3e42;
  }
}

.icon-btn {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: transparent;
  border: none;
  cursor: pointer;
  transition: all 0.2s;
  
  &:hover {
    background: rgba(255, 255, 255, 0.1);
  }
}

.icon-btn-sm {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  background: transparent;
  border: none;
  cursor: pointer;
  color: #787b86;
  font-size: 12px;
  
  &:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #d1d4dc;
  }
}

.signal-alert {
  background: rgba(245, 166, 35, 0.1);
  border: 1px solid rgba(245, 166, 35, 0.3);
  border-radius: 8px;
  padding: 12px;
}

.alert-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  color: #d1d4dc;
}

.badge {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 500;
}

.badge-success {
  background: rgba(8, 153, 129, 0.2);
  color: #089981;
}

.signal-item-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  
  &:last-child {
    border-bottom: none;
  }
}

.signal-stock {
  font-weight: 500;
  color: #d1d4dc;
  min-width: 120px;
}

.signal-condition {
  color: #2962ff;
  font-size: 12px;
  min-width: 80px;
}

.signal-message {
  color: #787b86;
  font-size: 12px;
  flex: 1;
}

.severity-buy { color: #089981; }
.severity-sell { color: #f23645; }
.severity-warning { color: #f5a623; }
.severity-info { color: #2962ff; }

.empty-state {
  text-align: center;
  padding: 40px 20px;
}

.empty-icon {
  font-size: 48px;
  color: #3e3e42;
  margin-bottom: 16px;
}

.watchlist-pools {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.pool-section {
  border-radius: 8px;
  overflow: hidden;
  
  &.pool-a {
    border: 1px solid rgba(8, 153, 129, 0.3);
    background: rgba(8, 153, 129, 0.05);
  }
  
  &.pool-b {
    border: 1px solid rgba(245, 166, 35, 0.3);
    background: rgba(245, 166, 35, 0.05);
  }
  
  &.pool-c {
    border: 1px solid rgba(120, 123, 134, 0.3);
    background: rgba(120, 123, 134, 0.05);
  }
}

.pool-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  background: rgba(0, 0, 0, 0.2);
}

.pool-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pool-badge {
  width: 24px;
  height: 24px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 12px;
  
  &.badge-a {
    background: #089981;
    color: white;
  }
  
  &.badge-b {
    background: #f5a623;
    color: white;
  }
  
  &.badge-c {
    background: #787b86;
    color: white;
  }
}

.pool-name {
  font-weight: 600;
  color: #d1d4dc;
  font-size: 14px;
}

.pool-desc {
  color: #787b86;
  font-size: 11px;
}

.pool-count {
  color: #787b86;
  font-size: 12px;
}

.status-tag {
  background: rgba(120, 123, 134, 0.2);
  color: #787b86;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
}

.stop-loss-cell {
  background: rgba(242, 54, 69, 0.15);
  color: #f23645;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
}

.watchlist-table-container {
  overflow-x: auto;
}

.watchlist-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  
  th, td {
    padding: 10px 12px;
    text-align: left;
    border-bottom: 1px solid #3e3e42;
  }
  
  th {
    color: #787b86;
    font-weight: 500;
    font-size: 12px;
    background: rgba(0, 0, 0, 0.2);
  }
  
  tbody tr {
    transition: background 0.2s;
    
    &:hover {
      background: rgba(255, 255, 255, 0.02);
    }
    
    &.row-inactive {
      opacity: 0.5;
    }
  }
}

.form-checkbox {
  width: 16px;
  height: 16px;
  accent-color: #2962ff;
  cursor: pointer;
}

.stock-info-cell {
  display: flex;
  flex-direction: column;
  
  .stock-name {
    font-weight: 500;
    color: #d1d4dc;
  }
  
  .stock-code {
    font-size: 11px;
    color: #787b86;
    font-family: monospace;
  }
}

.price-cell {
  font-family: monospace;
  font-size: 12px;
  
  &.buy {
    color: #089981;
  }
  
  &.sell {
    color: #f23645;
  }
}

.conditions-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.condition-tag-mini {
  background: rgba(41, 98, 255, 0.15);
  color: #2962ff;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10px;
}

.more-tag {
  color: #787b86;
  font-size: 10px;
}

.actions-cell {
  display: flex;
  gap: 4px;
}

.action-btn {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  background: transparent;
  border: none;
  cursor: pointer;
  color: #787b86;
  font-size: 12px;
  
  &:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #d1d4dc;
  }
}

.batch-actions-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: rgba(41, 98, 255, 0.1);
  border: 1px solid rgba(41, 98, 255, 0.2);
  border-radius: 8px;
  margin-top: 12px;
}

.selected-count {
  color: #d1d4dc;
  font-size: 13px;
}

.batch-buttons {
  display: flex;
  gap: 8px;
}

.btn-batch {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  background: #2d2d30;
  color: #d1d4dc;
  border: 1px solid #3e3e42;
  cursor: pointer;
  transition: all 0.2s;
  
  &:hover {
    background: #3e3e42;
  }
  
  &.btn-danger {
    color: #f23645;
    border-color: rgba(242, 54, 69, 0.3);
    
    &:hover {
      background: rgba(242, 54, 69, 0.1);
    }
  }
}

.batch-condition-modal {
  .modal-description {
    color: #b2b5be;
    margin-bottom: 16px;
    
    strong {
      color: #2962ff;
    }
  }
  
  .selected-conditions {
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid #3e3e42;
  }
}

.watchlist-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}

.watchlist-card {
  background: #252526;
  border-radius: 8px;
  border: 1px solid #3e3e42;
  overflow: hidden;
  transition: all 0.2s;
  
  &:hover {
    border-color: #505053;
  }
  
  &.card-inactive {
    opacity: 0.6;
  }
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px;
  border-bottom: 1px solid #3e3e42;
}

.stock-info {
  display: flex;
  flex-direction: column;
}

.stock-name {
  font-weight: 600;
  color: #d1d4dc;
}

.stock-code {
  font-size: 12px;
  color: #787b86;
  font-family: monospace;
}

.card-actions {
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
  
  .watchlist-card:hover & {
    opacity: 1;
  }
}

.card-body {
  padding: 12px;
}

.price-section {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}

.price-item {
  flex: 1;
  padding: 8px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
  
  &.buy {
    border-left: 2px solid #089981;
  }
  
  &.sell {
    border-left: 2px solid #f23645;
  }
}

.price-label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: #787b86;
  margin-bottom: 4px;
}

.price-value {
  font-size: 14px;
  font-weight: 600;
  color: #d1d4dc;
}

.conditions-section {
  margin-bottom: 12px;
}

.conditions-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: #787b86;
  margin-bottom: 6px;
}

.conditions-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.condition-tag {
  background: rgba(41, 98, 255, 0.2);
  color: #2962ff;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
}

.notes-section {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  color: #787b86;
}

.notes-text {
  flex: 1;
}

.card-footer {
  display: flex;
  justify-content: space-between;
  padding: 8px 12px;
  background: rgba(0, 0, 0, 0.2);
}

.footer-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.modal-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-row {
  display: flex;
  gap: 12px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-label {
  font-size: 12px;
  color: #787b86;
  font-weight: 500;
}

.form-input {
  background: #1a1f2e;
  border: 1px solid #3e3e42;
  border-radius: 6px;
  padding: 8px 12px;
  color: #d1d4dc;
  font-size: 14px;
  
  &:focus {
    outline: none;
    border-color: #2962ff;
  }
  
  &::placeholder {
    color: #5a5d63;
  }
}

.form-input-sm {
  width: 60px;
  background: #1a1f2e;
  border: 1px solid #3e3e42;
  border-radius: 4px;
  padding: 4px 8px;
  color: #d1d4dc;
  font-size: 12px;
  
  &:focus {
    outline: none;
    border-color: #2962ff;
  }
}

.form-section {
  background: rgba(255, 255, 255, 0.02);
  border-radius: 8px;
  padding: 12px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: #d1d4dc;
  margin-bottom: 12px;
}

.add-condition-btn {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: rgba(41, 98, 255, 0.2);
  color: #2962ff;
  border: none;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  
  &:hover {
    background: rgba(41, 98, 255, 0.3);
  }
}

.conditions-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.condition-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px;
  background: rgba(0, 0, 0, 0.2);
  border-radius: 6px;
}

.condition-info {
  display: flex;
  flex-direction: column;
  min-width: 120px;
}

.condition-category {
  font-size: 10px;
  color: #787b86;
}

.condition-name {
  font-size: 12px;
  color: #d1d4dc;
}

.condition-params {
  display: flex;
  gap: 8px;
  flex: 1;
}

.param-input {
  display: flex;
  flex-direction: column;
  gap: 2px;
  
  label {
    font-size: 10px;
    color: #787b86;
  }
}

.condition-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.switch {
  position: relative;
  width: 36px;
  height: 20px;
  
  input {
    opacity: 0;
    width: 0;
    height: 0;
  }
  
  .slider {
    position: absolute;
    cursor: pointer;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: #3e3e42;
    transition: 0.3s;
    border-radius: 10px;
    
    &:before {
      position: absolute;
      content: "";
      height: 14px;
      width: 14px;
      left: 3px;
      bottom: 3px;
      background-color: white;
      transition: 0.3s;
      border-radius: 50%;
    }
  }
  
  input:checked + .slider {
    background-color: #089981;
  }
  
  input:checked + .slider:before {
    transform: translateX(16px);
  }
}

.remove-btn {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  color: #787b86;
  cursor: pointer;
  
  &:hover {
    color: #f23645;
  }
}

.no-conditions {
  text-align: center;
  padding: 16px;
  color: #5a5d63;
  font-size: 12px;
}

.tags-input {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  background: #1a1f2e;
  border: 1px solid #3e3e42;
  border-radius: 6px;
  padding: 6px 8px;
}

.tag-item {
  display: flex;
  align-items: center;
  gap: 4px;
  background: rgba(41, 98, 255, 0.2);
  color: #2962ff;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
}

.tag-remove {
  background: transparent;
  border: none;
  color: inherit;
  cursor: pointer;
  padding: 0;
  font-size: 10px;
  
  &:hover {
    color: #f23645;
  }
}

.tag-input {
  flex: 1;
  min-width: 80px;
  background: transparent;
  border: none;
  color: #d1d4dc;
  font-size: 12px;
  outline: none;
  
  &::placeholder {
    color: #5a5d63;
  }
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #b2b5be;
  cursor: pointer;
}

.form-checkbox {
  width: 16px;
  height: 16px;
  accent-color: #2962ff;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

.btn {
  padding: 8px 16px;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}

.condition-selector {
  max-height: 400px;
  overflow-y: auto;
}

.condition-category-group {
  margin-bottom: 16px;
}

.category-title {
  font-size: 13px;
  font-weight: 600;
  color: #d1d4dc;
  margin-bottom: 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid #3e3e42;
}

.condition-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.condition-option {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 8px 12px;
  background: #252526;
  border: 1px solid #3e3e42;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
  min-width: 120px;
  
  &:hover:not(:disabled) {
    border-color: #2962ff;
    background: rgba(41, 98, 255, 0.1);
  }
  
  &.selected {
    border-color: #089981;
    background: rgba(8, 153, 129, 0.1);
  }
  
  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  
  .cond-name {
    font-size: 12px;
    color: #d1d4dc;
    font-weight: 500;
  }
  
  .cond-desc {
    font-size: 10px;
    color: #787b86;
    margin-top: 2px;
  }
}

.feishu-config {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.config-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
  background: rgba(41, 98, 255, 0.1);
  border-radius: 6px;
  
  p {
    margin: 0;
    font-size: 12px;
    color: #b2b5be;
  }
}

.config-status {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 12px;
  color: #089981;
}
</style>
