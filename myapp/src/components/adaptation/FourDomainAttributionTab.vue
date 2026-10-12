<template>
  <div class="adaptation-tab">
    <a-alert type="info" show-icon message="四域反馈严格隔离：任何域只能生成复核任务，不能直接修改其他域参数。" />
    <div class="toolbar"><a-input v-model:value="asOf" placeholder="as_of（可选）" style="width:220px"/><a-button type="primary" :loading="loading" @click="load">刷新归因</a-button></div>
    <a-row :gutter="12" class="domain-row">
      <a-col v-for="domain in domains" :key="domain.key" :span="6"><a-card size="small"><a-statistic :title="domain.label" :value="feedbackCounts[domain.key] || 0" suffix="条反馈"/><div class="domain-note">{{ domain.note }}</div></a-card></a-col>
    </a-row>
    <a-empty v-if="!reports.length" description="暂无四域归因报告" />
    <a-card v-for="report in reports" :key="report.report_id" class="report-card" :title="`归因报告 ${report.as_of || '时间未知'}`">
      <a-descriptions :column="3" size="small" bordered>
        <a-descriptions-item label="PIT 拦截">{{ report.pit_blocked_count }}</a-descriptions-item>
        <a-descriptions-item label="IC">{{ fmt(report.ic) }}</a-descriptions-item>
        <a-descriptions-item label="Rank IC">{{ fmt(report.rank_ic) }}</a-descriptions-item>
        <a-descriptions-item label="集中度 HHI">{{ fmt(report.diagnostics?.concentration_hhi) }}</a-descriptions-item>
        <a-descriptions-item label="流动性占用">{{ pct(report.diagnostics?.liquidity_utilization) }}</a-descriptions-item>
        <a-descriptions-item label="容量占用">{{ pct(report.diagnostics?.capacity_utilization) }}</a-descriptions-item>
      </a-descriptions>
      <div class="section"><b>行业暴露：</b>{{ pairs(report.diagnostics?.industry_exposure, true) }}</div>
      <div class="section"><b>风格暴露：</b>{{ pairs(report.diagnostics?.style_exposure, false) }}</div>
      <div class="section"><b>执行成本：</b>{{ moneyPairs(report.cost_attribution) }}</div>
      <div class="section"><b>约束告警：</b>{{ report.diagnostics?.violations?.join('；') || '无' }}</div>
    </a-card>
    <a-tabs>
      <a-tab-pane key="reviews" tab="跨域复核任务"><a-table :data-source="reviews" :columns="reviewColumns" row-key="task_id" size="small" /></a-tab-pane>
      <a-tab-pane key="calibrations" tab="校准版本"><a-table :data-source="calibrations" :columns="calibrationColumns" row-key="calibration_id" size="small" /></a-tab-pane>
    </a-tabs>
  </div>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'; import { message } from 'ant-design-vue';
import { listAdaptationCalibrations, listAdaptationReports, listAttributionReviews, listDomainFeedback, type FourDomainReport } from '@/api/adaptation';
const asOf=ref(''); const loading=ref(false); const reports=ref<FourDomainReport[]>([]); const feedback=ref<Record<string,unknown>[]>([]); const reviews=ref<Record<string,unknown>[]>([]); const calibrations=ref<Record<string,unknown>[]>([]);
const domains=[{key:'knowledge',label:'知识域',note:'Claim 是否真实'},{key:'research',label:'研究域',note:'盈利预测与区间覆盖'},{key:'alpha',label:'Alpha 域',note:'样本外 IC 与衰减'},{key:'execution',label:'执行域',note:'滑点、成本与成交质量'}];
const feedbackCounts=computed(()=>feedback.value.reduce((a:any,r:any)=>(a[r.domain]=(a[r.domain]||0)+1,a),{}));
const reviewColumns=[{title:'来源域',dataIndex:'source_domain'},{title:'归因类型',dataIndex:'attribution_type'},{title:'对象',dataIndex:'entity_id'},{title:'状态',dataIndex:'status'}];
const calibrationColumns=[{title:'域',dataIndex:'domain'},{title:'状态',dataIndex:'status'},{title:'样本数',dataIndex:'sample_size'},{title:'审批人',dataIndex:'approved_by'}];
async function load(){loading.value=true;try{[reports.value,feedback.value,reviews.value,calibrations.value]=await Promise.all([listAdaptationReports(asOf.value),listDomainFeedback(),listAttributionReviews(),listAdaptationCalibrations()]);}catch(e){message.error((e as Error).message);}finally{loading.value=false;}}
const fmt=(v:number|null|undefined)=>v==null?'-':Number(v).toFixed(4); const pct=(v:number|undefined)=>v==null?'-':`${(v*100).toFixed(2)}%`;
const pairs=(v:Record<string,number>|undefined,isPct:boolean)=>!v||!Object.keys(v).length?'暂无':Object.entries(v).map(([k,x])=>`${k} ${isPct?pct(x):fmt(x)}`).join('；');
const money=(v:number)=>Math.abs(v)>=1e8?`${(v/1e8).toFixed(2)}亿`:Math.abs(v)>=1e4?`${(v/1e4).toFixed(2)}万`:v.toFixed(2); const moneyPairs=(v:Record<string,number>)=>!v||!Object.keys(v).length?'暂无':Object.entries(v).map(([k,x])=>`${k} ${money(x)}`).join('；');
onMounted(load);
</script>
<style scoped>.adaptation-tab{padding:12px 0}.toolbar{display:flex;gap:8px;margin:16px 0}.domain-row{margin-bottom:16px}.domain-note{color:#787b86;margin-top:4px}.report-card{margin-bottom:12px;background:#131722;border-color:#2a2e39}.section{margin-top:10px;color:#d1d4dc}</style>
