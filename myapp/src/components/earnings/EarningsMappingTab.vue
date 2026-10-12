<template>
  <div class="earnings-tab">
    <!-- 顶部操作栏 -->
    <div class="earnings-toolbar">
      <a-space wrap>
        <span class="toolbar-label" v-if="healthData">
          存储：驱动 {{ healthData.drivers }} / 输入 {{ healthData.inputs }} / 结果 {{ healthData.results }} / 缺口 {{ healthData.gaps }}
        </span>
        <a-button size="small" @click="loadResults" :loading="loadingResults">刷新结果</a-button>
        <a-button size="small" @click="loadHealth" :loading="loadingHealth">健康检查</a-button>
      </a-space>
    </div>

    <!-- 公式说明 -->
    <div class="formula-block">
      <div class="block-title">计算公式</div>
      <pre class="formula-text">{{ formulaText }}</pre>
      <div class="formula-note">
        所有定量输入保留区间 [low, high]，运算时按区间算术传播，避免伪精确。
        缺输入返回 insufficient_data + gaps，严禁默认比例或伪精确。
      </div>
    </div>

    <!-- 测算表单 -->
    <div class="compute-block">
      <div class="block-title">
        发起盈利测算
        <span class="interval-hint">数值支持区间输入：如 100~120（悲观取下界，乐观取上界）；单值 100 则三情景相同</span>
      </div>
      <a-form layout="vertical" :model="computeForm">
        <a-row :gutter="16">
          <a-col :span="6">
            <a-form-item label="公司代码">
              <a-input v-model:value="computeForm.company_symbol" placeholder="如 600000.SH" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="公司名称">
              <a-input v-model:value="computeForm.company_name" placeholder="如 测试公司" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="产品/业务">
              <a-input v-model:value="computeForm.product_name" placeholder="如 测试产品" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="期间">
              <a-input v-model:value="computeForm.period" placeholder="如 2026Q1" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="6">
            <a-form-item label="市场规模（件）">
              <a-input v-model:value="computeForm.market_size" placeholder="如 10000000 或 9000000~11000000" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="单机用量">
              <a-input v-model:value="computeForm.per_unit_usage" placeholder="如 2.0 或 1.8~2.2" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="市场增长变化">
              <a-input v-model:value="computeForm.market_growth_delta" placeholder="如 0.25 或 0.20~0.30" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="变化类型">
              <a-select v-model:value="computeForm.market_growth_delta_kind">
                <a-select-option value="rate">增长率</a-select-option>
                <a-select-option value="absolute">绝对增量</a-select-option>
              </a-select>
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="公司份额">
              <a-input v-model:value="computeForm.market_share" placeholder="如 0.18 或 0.15~0.20" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="6"><a-form-item label="收入基数"><a-input v-model:value="computeForm.revenue_base" placeholder="如 1000000000 或 900000000~1100000000" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="名义产能"><a-input v-model:value="computeForm.nameplate_capacity" placeholder="如 5000000 或 4500000~5500000" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="良率"><a-input v-model:value="computeForm.yield_rate" placeholder="如 0.95 或 0.93~0.98" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="单位成本"><a-input v-model:value="computeForm.unit_cost" placeholder="如 65 或 60~70" /></a-form-item></a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="6"><a-form-item label="收入暴露比例"><a-input v-model:value="computeForm.revenue_exposure_ratio" placeholder="如 0.20 或 0.15~0.25" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="置信度下限"><a-input v-model:value="computeForm.confidence_low" placeholder="如 0.70" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="置信度上限"><a-input v-model:value="computeForm.confidence_high" placeholder="如 0.90" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="Claim ID"><a-input v-model:value="computeForm.claim_id" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="Evidence ID"><a-input v-model:value="computeForm.evidence_id" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="数据版本"><a-input v-model:value="computeForm.data_version" /></a-form-item></a-col>
          <a-col :span="6"><a-form-item label="来源发布时间"><a-input v-model:value="computeForm.published_at" placeholder="ISO 时间" /></a-form-item></a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="6"><a-form-item label="采集时间"><a-input v-model:value="computeForm.retrieved_at" placeholder="ISO 时间" /></a-form-item></a-col>
          <a-col :span="6">
            <a-form-item label="币种">
              <a-select v-model:value="computeForm.currency">
                <a-select-option value="CNY">CNY 人民币</a-select-option>
                <a-select-option value="USD">USD 美元</a-select-option>
                <a-select-option value="HKD">HKD 港币</a-select-option>
              </a-select>
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="敞口来源类型">
              <a-select v-model:value="computeForm.exposure_source_kind">
                <a-select-option value="financial_segment">财报分部</a-select-option>
                <a-select-option value="company_disclosure">公司披露</a-select-option>
                <a-select-option value="manual_audit">人工审核</a-select-option>
              </a-select>
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="6">
            <a-form-item label="有效产能（件）">
              <a-input v-model:value="computeForm.effective_capacity" placeholder="如 4000000 或 3500000~4500000" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="利用率">
              <a-input v-model:value="computeForm.utilization_rate" placeholder="如 0.88 或 0.85~0.90" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="ASP（元）">
              <a-input v-model:value="computeForm.asp" placeholder="如 110 或 100~120" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="总股本（股）">
              <a-input v-model:value="computeForm.shares_outstanding" placeholder="如 1000000000 或 950000000~1050000000" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="6">
            <a-form-item label="毛利率">
              <a-input v-model:value="computeForm.gross_margin" placeholder="如 0.38 或 0.35~0.40" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="费用率">
              <a-input v-model:value="computeForm.expense_ratio" placeholder="如 0.11 或 0.10~0.12" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="税率">
              <a-input v-model:value="computeForm.tax_rate" placeholder="如 0.18 或 0.15~0.20" />
            </a-form-item>
          </a-col>
          <a-col :span="6">
            <a-form-item label="PIT 时点">
              <a-input v-model:value="computeForm.as_of" placeholder="如 2026-01-01T00:00:00+00:00" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-space>
          <a-button type="primary" @click="runCompute" :loading="computing">执行测算</a-button>
          <a-checkbox v-model:checked="computeForm.historical">历史模式（严格 PIT）</a-checkbox>
          <a-checkbox v-model:checked="computeForm.save">保存到 DB</a-checkbox>
        </a-space>
      </a-form>
    </div>

    <!-- 当前测算结果 -->
    <div class="result-block" v-if="currentResult">
      <div class="block-title">
        测算结果
        <a-tag :color="statusColor(currentResult.status)" class="status-tag">{{ statusLabel(currentResult.status) }}</a-tag>
        <span class="result-meta" v-if="currentResult.company_symbol">
          {{ currentResult.company_name || '-' }}（{{ currentResult.company_symbol }}）
        </span>
        <span class="result-meta" v-if="currentResult.product_name">· {{ currentResult.product_name }}</span>
        <span class="result-meta" v-if="currentResult.period">· {{ currentResult.period }}</span>
        <span class="result-meta" v-if="currentResult.currency">· 币种 {{ currentResult.currency }}</span>
        <span class="result-meta" v-if="currentResult.as_of">· as_of {{ currentResult.as_of }}</span>
      </div>

      <!-- 被阻断的探索假设 -->
      <a-alert
        v-if="currentResult.exploration_hypothesis_blocked && currentResult.exploration_hypothesis_blocked.length"
        class="blocked-alert"
        type="warning"
        show-icon
        message="已硬阻断探索假设"
        :description="blockedDescription"
      />

      <!-- 数据缺口 -->
      <div class="gaps-section" v-if="currentResult.gaps && currentResult.gaps.length">
        <div class="section-title">数据缺口</div>
        <a-table
          :columns="gapColumns"
          :data-source="currentResult.gaps"
          row-key="gap_id"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'severity'">
              <a-tag :color="record.severity === 'blocker' ? 'red' : 'orange'">
                {{ record.severity === 'blocker' ? '阻断' : '警告' }}
              </a-tag>
            </template>
          </template>
        </a-table>
      </div>

      <!-- 三情景结果 -->
      <div class="scenarios-section" v-if="currentResult.scenarios && currentResult.scenarios.length">
        <div class="section-title">三情景结果（区间传播）</div>
        <a-table
          :columns="scenarioColumns"
          :data-source="currentResult.scenarios"
          row-key="kind"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'kind'">
              <a-tag :color="scenarioColor(record.kind)">{{ scenarioLabel(record.kind) }}</a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'new_demand'">
              {{ formatAmountInterval(record.new_demand) }}
            </template>
            <template v-else-if="column.dataIndex === 'deliverable_volume'">
              {{ formatAmountInterval(record.deliverable_volume) }}
            </template>
            <template v-else-if="column.dataIndex === 'new_revenue'">
              {{ formatAmountInterval(record.new_revenue) }}
            </template>
            <template v-else-if="column.dataIndex === 'new_gross_profit'">
              {{ formatAmountInterval(record.new_gross_profit) }}
            </template>
            <template v-else-if="column.dataIndex === 'new_net_profit'">
              {{ formatAmountInterval(record.new_net_profit) }}
            </template>
            <template v-else-if="column.dataIndex === 'eps_contribution'">
              {{ formatNumberInterval(record.eps_contribution, 4) }}
            </template>
          </template>
        </a-table>
      </div>

      <!-- 敏感性矩阵 -->
      <div class="sensitivity-section" v-if="currentResult.sensitivity_matrix && currentResult.sensitivity_matrix.length">
        <div class="section-title">敏感性矩阵（±10% 扰动）</div>
        <a-table
          :columns="sensitivityColumns"
          :data-source="currentResult.sensitivity_matrix"
          row-key="input_name"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'base_value'">
              {{ formatNumber(record.base_value, 4) }}
            </template>
            <template v-else-if="column.dataIndex === 'low_value'">
              {{ formatNumber(record.low_value, 4) }}
            </template>
            <template v-else-if="column.dataIndex === 'high_value'">
              {{ formatNumber(record.high_value, 4) }}
            </template>
            <template v-else-if="column.dataIndex === 'eps_impact_low'">
              {{ formatNumber(record.eps_impact_low, 4) }}
            </template>
            <template v-else-if="column.dataIndex === 'eps_impact_high'">
              {{ formatNumber(record.eps_impact_high, 4) }}
            </template>
            <template v-else-if="column.dataIndex === 'elasticity'">
              {{ formatNumber(record.elasticity, 4) }}
            </template>
          </template>
        </a-table>
      </div>

      <!-- 证据来源快照 -->
      <div class="sources-section" v-if="sourceItems.length">
        <div class="section-title">证据来源与输入快照</div>
        <a-table
          :columns="sourceColumns"
          :data-source="sourceItems"
          row-key="name"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'value'">
              {{ formatNumberInterval(record.value, 4) }} {{ record.unit }}
            </template>
            <template v-else-if="column.dataIndex === 'confidence'">
              {{ formatRatioInterval(record.confidence) }}
            </template>
            <template v-else-if="column.dataIndex === 'source'">
              <span class="source-link">
                {{ record.source_claim_id || record.source_evidence_id || '暂无来源' }}
              </span>
            </template>
          </template>
        </a-table>
      </div>
    </div>

    <!-- 历史结果列表 -->
    <div class="history-block">
      <div class="block-title">历史测算结果</div>
      <a-empty v-if="!resultsList.length && !loadingResults" description="暂无本地证据" />
      <a-table
        v-else
        :columns="historyColumns"
        :data-source="resultsList"
        row-key="result_id"
        size="small"
        :loading="loadingResults"
        :pagination="{ pageSize: 10 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'status'">
            <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'created_at'">
            {{ record.created_at || '-' }}
          </template>
          <template v-else-if="column.dataIndex === 'action'">
            <a-button size="small" type="link" @click="viewResult(record)">查看</a-button>
          </template>
        </template>
      </a-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue';
import { message } from 'ant-design-vue';
import {
  getEarningsHealth,
  listEarningsResults,
  computeEarnings,
  type EarningsImpactResult,
  type EarningsHealth,
} from '@/api/earnings';

const formulaText = `新增需求 = 市场规模变化 × 单机用量或渗透率
公司可兑现销量 = min(新增需求 × 公司份额, 有效产能 × 利用率 × 良率)
新增收入 = 公司可兑现销量 × ASP
新增毛利 = 新增收入 × 增量毛利率
新增净利润 = 新增毛利 - 增量费用 - 税费
EPS贡献 = 新增净利润 / point-in-time 总股本`;

const healthData = ref<EarningsHealth | null>(null);
const loadingHealth = ref(false);
const resultsList = ref<EarningsImpactResult[]>([]);
const loadingResults = ref(false);
const currentResult = ref<EarningsImpactResult | null>(null);
const computing = ref(false);

const computeForm = reactive({
  company_symbol: '', company_name: '', product_name: '', period: '',
  market_size: '', per_unit_usage: '', market_growth_delta: '', market_growth_delta_kind: 'rate',
  market_share: '', effective_capacity: '', nameplate_capacity: '', utilization_rate: '', yield_rate: '',
  asp: '', unit_cost: '', revenue_base: '', shares_outstanding: '',
  gross_margin: '', expense_ratio: '', tax_rate: '', as_of: '',
  claim_id: '', evidence_id: '', data_version: '', published_at: '', retrieved_at: '',
  revenue_exposure_ratio: '', confidence_low: '', confidence_high: '',
  currency: '',
  exposure_source_kind: '',
  historical: true,
  save: true,
});

const gapColumns = [
  { title: '字段', dataIndex: 'field_name', key: 'field_name' },
  { title: '原因', dataIndex: 'reason', key: 'reason' },
  { title: '期望来源', dataIndex: 'required_source_kind', key: 'required_source_kind' },
  { title: '严重度', dataIndex: 'severity', key: 'severity', width: 80 },
  { title: '建议任务', dataIndex: 'suggested_task', key: 'suggested_task' },
];

const scenarioColumns = [
  { title: '情景', dataIndex: 'kind', key: 'kind', width: 100 },
  { title: '新增需求', dataIndex: 'new_demand', key: 'new_demand' },
  { title: '可兑现销量', dataIndex: 'deliverable_volume', key: 'deliverable_volume' },
  { title: '新增收入', dataIndex: 'new_revenue', key: 'new_revenue' },
  { title: '新增毛利', dataIndex: 'new_gross_profit', key: 'new_gross_profit' },
  { title: '新增净利润', dataIndex: 'new_net_profit', key: 'new_net_profit' },
  { title: 'EPS贡献', dataIndex: 'eps_contribution', key: 'eps_contribution' },
];

const sensitivityColumns = [
  { title: '输入', dataIndex: 'input_name', key: 'input_name' },
  { title: '基准值', dataIndex: 'base_value', key: 'base_value' },
  { title: '悲观值', dataIndex: 'low_value', key: 'low_value' },
  { title: '乐观值', dataIndex: 'high_value', key: 'high_value' },
  { title: 'EPS下界', dataIndex: 'eps_impact_low', key: 'eps_impact_low' },
  { title: 'EPS上界', dataIndex: 'eps_impact_high', key: 'eps_impact_high' },
  { title: '弹性', dataIndex: 'elasticity', key: 'elasticity' },
];

const sourceColumns = [
  { title: '输入名称', dataIndex: 'name', key: 'name' },
  { title: '值', dataIndex: 'value', key: 'value' },
  { title: '单位', dataIndex: 'unit', key: 'unit', width: 80 },
  { title: '币种', dataIndex: 'currency', key: 'currency', width: 80 },
  { title: '期间', dataIndex: 'period', key: 'period' },
  { title: '置信区间', dataIndex: 'confidence', key: 'confidence' },
  { title: '来源', dataIndex: 'source', key: 'source' },
  { title: '抽取方式', dataIndex: 'extraction_method', key: 'extraction_method' },
];

const historyColumns = [
  { title: '结果ID', dataIndex: 'result_id', key: 'result_id', ellipsis: true },
  { title: '公司', dataIndex: 'company_symbol', key: 'company_symbol', width: 120 },
  { title: '产品', dataIndex: 'product_name', key: 'product_name' },
  { title: '期间', dataIndex: 'period', key: 'period', width: 100 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 120 },
  { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 180 },
  { title: '操作', dataIndex: 'action', key: 'action', width: 80 },
];

const blockedDescription = computed(() => {
  if (!currentResult.value?.exploration_hypothesis_blocked?.length) return '';
  return currentResult.value.exploration_hypothesis_blocked
    .map((h: Record<string, unknown>) => `来源类型 ${h.source_kind || '-'}：${h.reason || '探索假设'}`)
    .join('；');
});

interface SourceItem {
  name: string;
  value: { low: number; high: number; mid: number };
  unit: string;
  currency: string;
  period: string;
  confidence: { low: number; high: number };
  source_claim_id: string;
  source_evidence_id: string;
  extraction_method: string;
}

const sourceItems = computed<SourceItem[]>(() => {
  if (!currentResult.value?.base_inputs_snapshot) return [];
  const snapshot = currentResult.value.base_inputs_snapshot as Record<string, unknown>;
  const items: SourceItem[] = [];
  const extractInputs = (obj: Record<string, unknown>, prefix = '') => {
    if (!obj || typeof obj !== 'object') return;
    for (const [key, val] of Object.entries(obj)) {
      if (val && typeof val === 'object' && 'input_id' in (val as object)) {
        const input = val as Record<string, unknown>;
        items.push({
          name: String(input.name || prefix + key),
          value: input.value as { low: number; high: number; mid: number },
          unit: String(input.unit || '-'),
          currency: String(input.currency || '-'),
          period: String(input.period || '-'),
          confidence: input.confidence as { low: number; high: number },
          source_claim_id: String(input.source_claim_id || ''),
          source_evidence_id: String(input.source_evidence_id || ''),
          extraction_method: String(input.extraction_method || '-'),
        });
      } else if (val && typeof val === 'object' && Array.isArray(val)) {
        // capital_versions 等数组
        (val as unknown[]).forEach((v, i) => {
          if (v && typeof v === 'object') {
            extractInputs(v as Record<string, unknown>, `${prefix}${key}[${i}].`);
          }
        });
      } else if (val && typeof val === 'object') {
        extractInputs(val as Record<string, unknown>, prefix + key + '.');
      }
    }
  };
  extractInputs(snapshot);
  return items;
});

function statusColor(status: string): string {
  if (status === 'ok') return 'green';
  if (status === 'degraded') return 'orange';
  return 'red';
}

function statusLabel(status: string): string {
  if (status === 'ok') return '正常';
  if (status === 'degraded') return '降级';
  if (status === 'insufficient_data') return '数据不足';
  return status;
}

function scenarioColor(kind: string): string {
  if (kind === 'pessimistic') return 'red';
  if (kind === 'baseline') return 'blue';
  if (kind === 'optimistic') return 'green';
  return 'default';
}

function scenarioLabel(kind: string): string {
  if (kind === 'pessimistic') return '悲观';
  if (kind === 'baseline') return '基准';
  if (kind === 'optimistic') return '乐观';
  return kind;
}

/** 格式化数值，指定小数位。 */
function formatNumber(value: number | undefined, digits = 2): string {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return Number(value).toFixed(digits);
}

/** 格式化区间，指定小数位。 */
function formatNumberInterval(interval: { low: number; high: number } | undefined, digits = 2): string {
  if (!interval) return '-';
  return `[${formatNumber(interval.low, digits)}, ${formatNumber(interval.high, digits)}]`;
}

/** 格式化比例区间（4 位小数）。 */
function formatRatioInterval(interval: { low: number; high: number } | undefined): string {
  return formatNumberInterval(interval, 4);
}

/** 格式化金额区间，自动万/亿。 */
function formatAmountInterval(interval: { low: number; high: number } | undefined): string {
  if (!interval) return '-';
  return `[${formatAmount(interval.low)}, ${formatAmount(interval.high)}]`;
}

/** 格式化金额，自动万/亿。 */
function formatAmount(value: number | undefined): string {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  const abs = Math.abs(value);
  if (abs >= 1e8) return (value / 1e8).toFixed(2) + '亿';
  if (abs >= 1e4) return (value / 1e4).toFixed(2) + '万';
  return value.toFixed(2);
}

async function loadHealth(): Promise<void> {
  loadingHealth.value = true;
  try {
    healthData.value = await getEarningsHealth();
  } catch (err) {
    message.warning('健康检查失败：' + (err as Error).message);
  } finally {
    loadingHealth.value = false;
  }
}

async function loadResults(): Promise<void> {
  loadingResults.value = true;
  try {
    resultsList.value = await listEarningsResults({ limit: 50 });
  } catch (err) {
    message.warning('加载结果失败：' + (err as Error).message);
    resultsList.value = [];
  } finally {
    loadingResults.value = false;
  }
}

function viewResult(record: EarningsImpactResult): void {
  currentResult.value = record;
}

async function runCompute(): Promise<void> {
  // 安全降级：不硬阻断提交，缺字段时由后端返回 insufficient_data + gaps。
  // 仅当完全无公司标识时提示（但仍允许提交，让后端安全降级）。
  if (!computeForm.company_symbol && !computeForm.period) {
    message.warning('未填写公司代码和期间，后端将返回 insufficient_data');
  }
  if (computeForm.historical && !computeForm.as_of) {
    message.warning('历史模式下缺 as_of，后端将返回 insufficient_data + 审计 gap');
  }
  computing.value = true;
  try {
    const body = buildComputeBody();
    const result = await computeEarnings(body);
    currentResult.value = result;
    if (result.status === 'insufficient_data') {
      message.warning('数据不足，已返回缺口列表，未产生伪精确结果');
    } else if (result.status === 'degraded') {
      message.info('测算完成（部分输入缺失，结果已降级）');
    } else {
      message.success('测算完成');
    }
    await loadResults();
  } catch (err) {
    message.error('测算失败：' + (err as Error).message);
  } finally {
    computing.value = false;
  }
}

/**
 * 解析区间字符串：支持 "low~high" 或 "value" 格式。
 * - "100~120" → { low: 100, high: 120 }
 * - "100" → { low: 100, high: 100 }
 * - "" → null（缺失，由后端返回 gap）
 */
function parseIntervalValue(raw: string): { low: number; high: number } | null {
  const trimmed = String(raw ?? '').trim();
  if (!trimmed) return null;
  const tildeIdx = trimmed.indexOf('~');
  if (tildeIdx > 0) {
    const lowStr = trimmed.substring(0, tildeIdx).trim();
    const highStr = trimmed.substring(tildeIdx + 1).trim();
    const low = parseFloat(lowStr);
    const high = parseFloat(highStr);
    if (!Number.isNaN(low) && !Number.isNaN(high)) {
      return { low, high: high >= low ? high : low };
    }
  }
  const val = parseFloat(trimmed);
  if (!Number.isNaN(val)) return { low: val, high: val };
  return null;
}

/** 构造测算请求体，所有输入标注来源 Claim/Evidence、单位、期间、as_of。
 *  缺失字段不伪造，由后端返回 insufficient_data + gaps 安全降级。
 */
function buildComputeBody(): Record<string, unknown> {
  const asOf = computeForm.as_of.trim();
  const claimId = computeForm.claim_id.trim();
  const evidenceId = computeForm.evidence_id.trim();
  const dataVersion = computeForm.data_version.trim();
  const currency = computeForm.currency.trim();
  const exposureSourceKind = computeForm.exposure_source_kind.trim();
  const confidenceRaw = {
    low: parseFloat(computeForm.confidence_low),
    high: parseFloat(computeForm.confidence_high),
  };
  const confidence = Number.isNaN(confidenceRaw.low) || Number.isNaN(confidenceRaw.high)
    ? { low: 0, high: 0 }
    : confidenceRaw;

  /** 构造定量输入；value 支持区间 "low~high" 或单值。缺失返回 null 由后端降级。 */
  const makeInput = (
    name: string,
    rawValue: string,
    unit: string,
    sourceKind = '',
  ): Record<string, unknown> | null => {
    const interval = parseIntervalValue(rawValue);
    if (interval === null) return null;
    return {
      name,
      value: interval,
      unit,
      currency,
      period: computeForm.period,
      as_of: asOf,
      data_version: dataVersion,
      source_claim_id: claimId,
      source_evidence_id: evidenceId,
      extraction_method: 'manual',
      confidence,
      published_at: computeForm.published_at,
      retrieved_at: computeForm.retrieved_at,
      source_kind: sourceKind,
    };
  };

  /** 构造输入，如果缺失则返回 undefined（不包含在请求体中，由后端返回 gap）。 */
  const makeOptionalInput = (
    name: string,
    rawValue: string,
    unit: string,
    sourceKind = '',
  ): Record<string, unknown> | undefined => {
    const input = makeInput(name, rawValue, unit, sourceKind);
    return input ?? undefined;
  };

  const body: Record<string, unknown> = {
    driver: {
      company_symbol: computeForm.company_symbol,
      company_name: computeForm.company_name,
      product_name: computeForm.product_name,
      period: computeForm.period,
      driver_type: 'demand_expansion',
      claim_ids: claimId ? [claimId] : [],
    },
    as_of: asOf,
    historical: computeForm.historical,
    save: computeForm.save,
  };

  // 收入敞口（source_kind 由表单显式选择，不硬编码）
  const exposureRatio = parseIntervalValue(computeForm.revenue_exposure_ratio);
  const revenueBaseInput = makeInput('revenue_base', computeForm.revenue_base, 'yuan', exposureSourceKind);
  if (exposureRatio !== null || revenueBaseInput !== null) {
    const exposure: Record<string, unknown> = {
      company_symbol: computeForm.company_symbol,
      product_name: computeForm.product_name,
      source_kind: exposureSourceKind,
      source_claim_id: claimId,
      source_evidence_id: evidenceId,
      as_of: asOf,
      period: computeForm.period,
      data_version: dataVersion,
      confidence,
      published_at: computeForm.published_at,
      retrieved_at: computeForm.retrieved_at,
    };
    if (exposureRatio !== null) exposure.exposure_ratio = exposureRatio;
    if (revenueBaseInput !== null) exposure.revenue_base = revenueBaseInput;
    body.exposure = exposure;
  }

  // 需求模型
  const marketSizeInput = makeOptionalInput('market_size', computeForm.market_size, 'pieces');
  const perUnitUsageInput = makeOptionalInput('per_unit_usage', computeForm.per_unit_usage, 'pieces');
  const growthDeltaInput = makeOptionalInput(
    'market_growth_delta',
    computeForm.market_growth_delta,
    computeForm.market_growth_delta_kind === 'rate' ? 'ratio' : 'pieces',
  );
  if (marketSizeInput || perUnitUsageInput || growthDeltaInput) {
    body.demand = {
      market_size: marketSizeInput || undefined,
      per_unit_usage: perUnitUsageInput || undefined,
      market_growth_delta_input: growthDeltaInput || undefined,
      market_growth_delta_kind: computeForm.market_growth_delta_kind,
    };
  }

  // 产能模型
  const effCapInput = makeOptionalInput('effective_capacity', computeForm.effective_capacity, 'pieces');
  const nameplateCapInput = makeOptionalInput('nameplate_capacity', computeForm.nameplate_capacity, 'pieces');
  const utilInput = makeOptionalInput('utilization_rate', computeForm.utilization_rate, 'ratio');
  const yieldInput = makeOptionalInput('yield_rate', computeForm.yield_rate, 'ratio');
  if (effCapInput || nameplateCapInput || utilInput || yieldInput) {
    body.capacity = {
      effective_capacity: effCapInput || undefined,
      nameplate_capacity: nameplateCapInput || undefined,
      utilization_rate: utilInput || undefined,
      yield_rate: yieldInput || undefined,
    };
  }

  // 定价模型
  const aspInput = makeOptionalInput('asp', computeForm.asp, 'yuan');
  const unitCostInput = makeOptionalInput('unit_cost', computeForm.unit_cost, 'yuan');
  if (aspInput || unitCostInput) {
    body.pricing = {
      asp: aspInput || undefined,
      unit_cost: unitCostInput || undefined,
      period: computeForm.period,
    };
  }

  // 利润率模型
  const grossMarginInput = makeOptionalInput('gross_margin', computeForm.gross_margin, 'ratio');
  const expenseRatioInput = makeOptionalInput('expense_ratio', computeForm.expense_ratio, 'ratio');
  const taxRateInput = makeOptionalInput('tax_rate', computeForm.tax_rate, 'ratio');
  if (grossMarginInput || expenseRatioInput || taxRateInput) {
    body.margin = {
      gross_margin: grossMarginInput || undefined,
      expense_ratio: expenseRatioInput || undefined,
      tax_rate: taxRateInput || undefined,
    };
  }

  // 股本版本
  const sharesInput = makeInput('shares_outstanding', computeForm.shares_outstanding, 'pieces');
  if (sharesInput) {
    body.capital_versions = [{
      company_symbol: computeForm.company_symbol,
      period: computeForm.period,
      shares_outstanding: sharesInput,
      as_of: asOf,
      source_claim_id: claimId,
      source_evidence_id: evidenceId,
      published_at: computeForm.published_at,
      retrieved_at: computeForm.retrieved_at,
    }];
  }

  // 公司份额（与收入敞口严格分离，不互推）
  const marketShareInput = makeOptionalInput('market_share', computeForm.market_share, 'ratio');
  if (marketShareInput) {
    body.market_share = marketShareInput;
  }

  return body;
}

onMounted(() => {
  loadHealth();
  loadResults();
});
</script>

<style scoped>
.earnings-tab {
  padding: 16px 0;
}
.earnings-toolbar {
  margin-bottom: 16px;
}
.toolbar-label {
  color: #787b86;
  font-size: 13px;
}
.formula-block,
.compute-block,
.result-block,
.history-block {
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 16px;
  margin-bottom: 16px;
}
.block-title {
  font-size: 15px;
  font-weight: 600;
  color: #fff;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.status-tag {
  margin-left: 4px;
}
.result-meta {
  font-size: 13px;
  color: #787b86;
  font-weight: 400;
}
.formula-text {
  background: #0d1117;
  border: 1px solid #2a2e39;
  border-radius: 4px;
  padding: 12px;
  color: #91cc75;
  font-size: 13px;
  line-height: 1.8;
  margin: 0 0 8px;
  white-space: pre-wrap;
}
.formula-note {
  color: #787b86;
  font-size: 12px;
  line-height: 1.6;
}
.interval-hint {
  color: #787b86;
  font-size: 12px;
  font-weight: 400;
  margin-left: 8px;
}
.blocked-alert {
  margin-bottom: 16px;
}
.section-title {
  font-size: 14px;
  font-weight: 600;
  color: #d1d4dc;
  margin: 16px 0 8px;
}
.gaps-section,
.scenarios-section,
.sensitivity-section,
.sources-section {
  margin-bottom: 16px;
}
.source-link {
  color: #5470c6;
  font-size: 12px;
}
</style>
