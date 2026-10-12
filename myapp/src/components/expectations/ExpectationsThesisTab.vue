<template>
  <div class="expectations-tab">
    <a-alert type="info" show-icon message="研究观点仅用于证据验证，不提供买卖、仓位或止损建议。" />
    <div class="toolbar">
      <a-input v-model:value="company" placeholder="公司代码" style="width: 180px" />
      <a-button type="primary" :loading="loading" @click="loadAll">查询</a-button>
      <a-button @click="showCreate = true">新建可证伪观点</a-button>
    </div>
    <a-row :gutter="12" class="summary">
      <a-col :span="6"><a-statistic title="公司指引版本" :value="guidance.length" /></a-col>
      <a-col :span="6"><a-statistic title="一致预期版本" :value="consensus.length" /></a-col>
      <a-col :span="6"><a-statistic title="隐含预期版本" :value="implied.length" /></a-col>
      <a-col :span="6"><a-statistic title="区间预期差" :value="gaps.length" /></a-col>
    </a-row>
    <a-tabs>
      <a-tab-pane key="guidance" tab="公司指引"><JsonTable :rows="guidance" empty="暂无公司指引" /></a-tab-pane>
      <a-tab-pane key="consensus" tab="一致预期"><JsonTable :rows="consensus" empty="暂无一致预期" /></a-tab-pane>
      <a-tab-pane key="implied" tab="价格隐含预期"><JsonTable :rows="implied" empty="暂无隐含预期" /></a-tab-pane>
      <a-tab-pane key="gaps" tab="预期差"><JsonTable :rows="gaps" empty="暂无可计算预期差" /></a-tab-pane>
      <a-tab-pane key="theses" tab="研究观点">
        <a-empty v-if="!theses.length" description="暂无研究观点" />
        <a-card v-for="item in theses" :key="item.thesis_id" class="thesis-card" :title="item.title || '未命名研究观点'">
          <template #extra><a-tag :color="item.status === 'active' ? 'green' : 'orange'">{{ statusText(item.status) }}</a-tag></template>
          <a-descriptions :column="2" size="small">
            <a-descriptions-item label="最脆弱假设">{{ item.weakest_assumption || '待补充' }}</a-descriptions-item>
            <a-descriptions-item label="下一项证据">{{ item.next_evidence_required || '待补充' }}</a-descriptions-item>
            <a-descriptions-item label="证伪条件"><span v-if="item.invalidation_conditions?.length">{{ item.invalidation_conditions.join('；') }}</span><span v-else>待补充</span></a-descriptions-item>
            <a-descriptions-item label="有效期/复核日">{{ item.valid_until || '未知' }} / {{ item.review_at || '未知' }}</a-descriptions-item>
          </a-descriptions>
        </a-card>
      </a-tab-pane>
    </a-tabs>
    <a-modal v-model:open="showCreate" title="新建可证伪研究观点" @ok="submitThesis">
      <a-form layout="vertical">
        <a-form-item label="标题"><a-input v-model:value="form.title" /></a-form-item>
        <a-form-item label="必要 Claim ID（逗号分隔）"><a-input v-model:value="form.claims" /></a-form-item>
        <a-form-item label="核心假设"><a-textarea v-model:value="form.assumption" /></a-form-item>
        <a-form-item label="证伪条件"><a-textarea v-model:value="form.invalidation" /></a-form-item>
        <a-form-item label="最脆弱假设"><a-input v-model:value="form.weakest" /></a-form-item>
        <a-form-item label="下一项必须验证的证据"><a-input v-model:value="form.nextEvidence" /></a-form-item>
        <a-form-item label="有效期"><a-input v-model:value="form.validUntil" placeholder="YYYY-MM-DD" /></a-form-item>
        <a-form-item label="复核日期"><a-input v-model:value="form.reviewAt" placeholder="YYYY-MM-DD" /></a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { defineComponent, h, onMounted, reactive, ref } from 'vue';
import { Empty, Table, message } from 'ant-design-vue';
import { createThesis, listConsensus, listGaps, listGuidance, listImplied, listTheses, type Thesis } from '@/api/expectations';

const JsonTable = defineComponent({
  props: { rows: { type: Array, required: true }, empty: { type: String, default: '暂无数据' } },
  setup(props) {
    return () => props.rows.length
      ? h(Table, { dataSource: props.rows, columns: [
          { title: 'ID/版本', customRender: ({ record }: any) => record.guidance_id || record.consensus_id || record.implied_id || record.gap_id || '-' },
          { title: '期间/指标', customRender: ({ record }: any) => record.period || record.values?.[0]?.period || '-' },
          { title: '数据版本', customRender: ({ record }: any) => record.market_data_version || record.values?.[0]?.data_version || '-' },
          { title: '状态', customRender: ({ record }: any) => record.status || '有效' },
        ], pagination: false, size: 'small', rowKey: (r: any) => r.guidance_id || r.consensus_id || r.implied_id || r.gap_id }, () => [])
      : h(Empty, { description: props.empty });
  },
});
const company = ref(''); const loading = ref(false); const showCreate = ref(false);
const guidance = ref<Record<string, unknown>[]>([]); const consensus = ref<Record<string, unknown>[]>([]);
const implied = ref<Record<string, unknown>[]>([]); const gaps = ref<Record<string, unknown>[]>([]); const theses = ref<Thesis[]>([]);
const form = reactive({ title: '', claims: '', assumption: '', invalidation: '', weakest: '', nextEvidence: '', validUntil: '', reviewAt: '' });
const split = (v: string) => v.split(/[，,；;\n]/).map(x => x.trim()).filter(Boolean);
async function loadAll() { loading.value = true; try { [guidance.value, consensus.value, implied.value, gaps.value, theses.value] = await Promise.all([listGuidance(company.value), listConsensus(company.value), listImplied(company.value), listGaps(company.value), listTheses(company.value)]); } catch (e) { message.error((e as Error).message); } finally { loading.value = false; } }
async function submitThesis() { if (!form.invalidation.trim() || !form.nextEvidence.trim()) { message.warning('必须填写证伪条件和下一项证据'); return; } await createThesis({ company_symbol: company.value, title: form.title, required_claim_ids: split(form.claims), core_assumptions: split(form.assumption), invalidation_conditions: split(form.invalidation), weakest_assumption: form.weakest, next_evidence_required: form.nextEvidence, valid_until: form.validUntil, review_at: form.reviewAt, status: 'draft' }); showCreate.value = false; await loadAll(); }
function statusText(v: string) { return ({ active: '有效', draft: '草稿', needs_review: '待复核', invalidated: '已证伪', expired: '已过期' } as Record<string,string>)[v] || v || '未知'; }
onMounted(loadAll);
</script>

<style scoped>
.expectations-tab { padding: 12px 0; } .toolbar { display:flex; gap:8px; margin:16px 0; } .summary { margin-bottom:12px; }
.thesis-card { margin-bottom:12px; background:#131722; border-color:#2a2e39; }
</style>
