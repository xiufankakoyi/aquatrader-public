<template>
  <div class="company-research-page">
    <!-- 顶部：搜索栏 + 创建运行 + 错误提示 -->
    <div class="page-header">
      <div class="header-title">
        <div class="w-1 h-8 bg-gradient-to-b from-cyan-500 to-blue-600 rounded-full"></div>
        <div>
          <h1 class="text-2xl font-bold text-white tracking-tight">公司研究档案</h1>
          <p class="text-sm text-[#787b86] mt-1">
            P0 可信研究底稿 · 区分事实 / 观点 / 假设 / 市场隐含 · 不输出交易建议
          </p>
        </div>
      </div>
      <div class="header-actions">
        <a-input
          v-model:value="symbolInput"
          placeholder="600519.SH"
          style="width: 160px"
          @press-enter="handleSearch"
        />
        <a-date-picker v-model:value="asOfInput" style="width: 140px" />
        <a-input-number
          v-model:value="lookbackInput"
          :min="30"
          :max="730"
          :step="10"
          style="width: 100px"
          placeholder="回看天数"
        />
        <a-button @click="handleSearch" :loading="loadingLatest">
          <template #icon><i class="fas fa-search"></i></template>
          查询
        </a-button>
        <a-button
          type="primary"
          @click="handleCreateRun"
          :loading="creating"
          :disabled="!symbolInput || !asOfInput"
        >
          <template #icon><i class="fas fa-plus"></i></template>
          创建研究运行
        </a-button>
        <a-button @click="refreshCurrent" :loading="loadingLatest">
          <template #icon><i class="fas fa-sync"></i></template>
          刷新
        </a-button>
      </div>
    </div>

    <!-- 错误提示 -->
    <div v-if="pageError" class="error-banner">
      <i class="fas fa-circle-exclamation"></i>
      <span class="error-message">{{ pageError.message }}</span>
      <span class="error-code" v-if="pageError.code">[{{ pageError.code }}]</span>
      <span class="error-trace" v-if="pageError.trace_id">trace_id: {{ pageError.trace_id }}</span>
    </div>

    <!-- 降级提示 -->
    <div v-if="degradedReason" class="degraded-banner">
      <i class="fas fa-circle-info"></i>
      <span>研究档案处于降级状态：{{ degradedReason }}</span>
      <span v-if="symbolInput" class="ml-2 text-[#787b86]">symbol={{ symbolInput }}</span>
    </div>

    <!-- 加载态 -->
    <div v-if="loadingLatest && !latestBundle" class="loading-state">
      <i class="fas fa-spinner fa-spin"></i>
      <span>正在加载研究档案...</span>
    </div>

    <!-- 空态 -->
    <div v-else-if="!latestBundle || !latestBundle.dossier" class="empty-state">
      <i class="fas fa-folder-open"></i>
      <div class="empty-title">暂无本地证据</div>
      <div class="empty-hint">输入 A 股代码并点击「创建研究运行」生成档案</div>
    </div>

    <!-- 主体九分区 -->
    <div v-else class="research-grid">
      <!-- 分区 1: 公司与数据时点 -->
      <section class="card card-company">
        <div class="card-title">
          <i class="fas fa-building"></i> 公司与数据时点
        </div>
        <div class="company-row">
          <div class="company-name">{{ dossier.company_name || '未知' }}</div>
          <div class="company-symbol">{{ dossier.symbol }}</div>
        </div>
        <div class="company-meta">
          <div class="meta-item">
            <span class="meta-label">行业</span>
            <span class="meta-value">{{ safeString(dossier.industry, '未披露') }}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">PIT 时点</span>
            <span class="meta-value">{{ safeString(dossier.as_of, '-') }}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">Schema</span>
            <span class="meta-value">{{ safeString(dossier.schema_version, '-') }}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">运行 ID</span>
            <span class="meta-value mono">{{ runIdShort }}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">运行状态</span>
            <span class="meta-value">
              <span :class="`status-badge status-${runStatus}`">{{ runStatusLabel }}</span>
            </span>
          </div>
          <div class="meta-item">
            <span class="meta-label">创建时间</span>
            <span class="meta-value">{{ formatTime(latestBundle.run?.created_at || '') }}</span>
          </div>
        </div>
      </section>

      <!-- 分区 2: 八维 readiness 面板 -->
      <section class="card card-readiness">
        <ResearchReadinessPanel :readiness="dossier.research_readiness" />
      </section>

      <!-- 分区 3: 已验证事实 / 观点 / 假设 三分栏 -->
      <section class="card card-facts">
        <div class="card-title">
          <i class="fas fa-list-check"></i> 事实 / 观点 / 假设
          <span class="card-subtitle">三者必须可区分；不得混用</span>
        </div>
        <div class="facts-columns">
          <div class="facts-col">
            <div class="col-header">
              <span class="col-label verified">已验证事实</span>
              <span class="col-count">{{ verifiedFacts.length }}</span>
            </div>
            <div class="col-body">
              <div v-if="!verifiedFacts.length" class="col-empty">暂无本地证据</div>
              <div v-for="(fact, idx) in verifiedFacts.slice(0, 10)" :key="`vf-${idx}`" class="fact-item">
                <div class="fact-title">{{ safeString(fact.metric_or_relation || fact.relation, '未命名') }}</div>
                <div class="fact-meta">
                  <span v-if="fact.value !== null && fact.value !== undefined" class="fact-value">
                    {{ formatFactValue(fact) }}
                  </span>
                  <span v-if="fact.period" class="fact-period">{{ fact.period }}</span>
                </div>
                <div class="fact-evidence" v-if="fact.evidence_ids && fact.evidence_ids.length">
                  证据 {{ fact.evidence_ids.length }} 条
                </div>
              </div>
            </div>
          </div>

          <div class="facts-col">
            <div class="col-header">
              <span class="col-label opinion">市场观点</span>
              <span class="col-count">{{ marketOpinions.length }}</span>
            </div>
            <div class="col-body">
              <div v-if="!marketOpinions.length" class="col-empty">暂无本地证据</div>
              <div v-for="(op, idx) in marketOpinions.slice(0, 10)" :key="`op-${idx}`" class="fact-item">
                <div class="fact-title">{{ safeString(op.metric || op.metric_or_relation || op.variable_name, '未命名') }}</div>
                <div class="fact-meta">
                  <span v-if="op.value !== null && op.value !== undefined" class="fact-value">
                    {{ formatFactValue(op) }}
                  </span>
                  <span v-if="op.period" class="fact-period">{{ op.period }}</span>
                </div>
                <div class="fact-evidence" v-if="op.source_type">
                  来源: {{ sourceTypeLabel(op.source_type) }}
                </div>
              </div>
            </div>
          </div>

          <div class="facts-col">
            <div class="col-header">
              <span class="col-label assumption">研究假设</span>
              <span class="col-count">{{ assumptions.length }}</span>
            </div>
            <div class="col-body">
              <div v-if="!assumptions.length" class="col-empty">暂无本地证据</div>
              <div v-for="(as, idx) in assumptions.slice(0, 10)" :key="`as-${idx}`" class="fact-item">
                <div class="fact-title">{{ safeString(as.metric || as.variable_name, '未命名假设') }}</div>
                <div class="fact-meta">
                  <span v-if="as.value !== null && as.value !== undefined" class="fact-value">
                    {{ formatFactValue(as) }}
                  </span>
                  <span v-if="as.interval" class="fact-value">
                    {{ formatInterval(as.interval) }}
                  </span>
                  <span v-if="as.period" class="fact-period">{{ as.period }}</span>
                </div>
                <div class="fact-evidence" v-if="as.source_type">
                  来源: {{ sourceTypeLabel(as.source_type) }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 分区 4: 财务历史摘要 -->
      <section class="card card-financial">
        <div class="card-title">
          <i class="fas fa-chart-line"></i> 财务历史摘要
          <span class="card-subtitle">仅展示历史记录，不作预测</span>
        </div>
        <div v-if="!financialHistory.length" class="card-empty">暂无本地证据</div>
        <div v-else class="financial-table">
          <table>
            <thead>
              <tr>
                <th>报告期</th>
                <th class="text-right">营业收入</th>
                <th class="text-right">归母净利润</th>
                <th class="text-right">毛利率</th>
                <th class="text-right">净利率</th>
                <th class="text-right">ROE</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, idx) in financialHistory.slice(0, 8)" :key="`fin-${idx}`">
                <td>{{ safeString(row.period || row.end_date || row.report_date, '-') }}</td>
                <td class="text-right">{{ formatAmount(row.revenue || row.total_revenue) }}</td>
                <td class="text-right">{{ formatAmount(row.net_profit || row.parent_net_profit) }}</td>
                <td class="text-right">{{ formatPercent(row.gross_margin || row.gross_profit_margin) }}</td>
                <td class="text-right">{{ formatPercent(row.net_margin || row.net_profit_margin) }}</td>
                <td class="text-right">{{ formatPercent(row.roe) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- 分区 5: 产业因果关系 -->
      <section class="card card-causal">
        <div class="card-title">
          <i class="fas fa-diagram-project"></i> 产业因果关系
          <span class="card-subtitle">verified_edges / observed_facts / forward_plans</span>
        </div>
        <div v-if="!hasCausalContext" class="card-empty">暂无本地证据</div>
        <div v-else class="causal-grid">
          <div class="causal-block">
            <div class="block-title">
              已验证因果边
              <span class="count">{{ verifiedEdges.length }}</span>
            </div>
            <div class="block-body">
              <div v-if="!verifiedEdges.length" class="block-empty">暂无</div>
              <div v-for="(edge, idx) in verifiedEdges.slice(0, 8)" :key="`edge-${idx}`" class="causal-item">
                <span class="causal-head">{{ safeString(edge.subject_name || edge.subject_id, '?') }}</span>
                <span class="causal-rel">→ {{ safeString(edge.relation, 'relates_to') }} →</span>
                <span class="causal-tail">{{ safeString(edge.object_name || edge.object_id, '?') }}</span>
              </div>
            </div>
          </div>

          <div class="causal-block">
            <div class="block-title">
              已观察事实
              <span class="count">{{ observedFacts.length }}</span>
            </div>
            <div class="block-body">
              <div v-if="!observedFacts.length" class="block-empty">暂无</div>
              <div v-for="(fact, idx) in observedFacts.slice(0, 8)" :key="`of-${idx}`" class="causal-item">
                <span class="causal-head">{{ safeString(fact.subject_name || fact.relation, '未命名') }}</span>
                <span class="causal-rel">→ {{ safeString(fact.relation, 'observed') }} →</span>
                <span class="causal-tail">{{ safeString(fact.object_name, '-') }}</span>
              </div>
            </div>
          </div>

          <div class="causal-block">
            <div class="block-title">
              前瞻指引
              <span class="count">{{ forwardPlans.length }}</span>
            </div>
            <div class="block-body">
              <div v-if="!forwardPlans.length" class="block-empty">暂无</div>
              <div v-for="(plan, idx) in forwardPlans.slice(0, 8)" :key="`fp-${idx}`" class="causal-item">
                <span class="causal-head">{{ safeString(plan.subject_name || plan.relation, '未命名') }}</span>
                <span class="causal-rel">{{ safeString(plan.direction || plan.relation, '') }}</span>
                <span class="causal-tail">{{ safeString(plan.object_name, '-') }}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 分区 6: 市场预期与估值 -->
      <section class="card card-valuation">
        <div class="card-title">
          <i class="fas fa-balance-scale"></i> 市场预期与估值
          <span class="card-subtitle">市场隐含场景只能提升 market_expectation_computability</span>
        </div>
        <div class="valuation-grid">
          <div class="valuation-block">
            <div class="block-title">估值指标</div>
            <div class="valuation-body">
              <div class="kv-item">
                <span class="kv-label">PE(TTM)</span>
                <span class="kv-value">{{ formatMetric(dossier.valuation?.metrics?.pe) }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">PB</span>
                <span class="kv-value">{{ formatMetric(dossier.valuation?.metrics?.pb) }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">总市值</span>
                <span class="kv-value">{{ formatAmount(dossier.valuation?.metrics?.market_cap) }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">估值状态</span>
                <span :class="`status-badge status-${dossier.valuation?.status || 'not_computable'}`">
                  {{ statusLabel(dossier.valuation?.status || 'not_computable') }}
                </span>
              </div>
            </div>
          </div>

          <div class="valuation-block">
            <div class="block-title">市场预期</div>
            <div class="valuation-body">
              <div class="kv-item">
                <span class="kv-label">已验证预期数</span>
                <span class="kv-value">{{ verifiedExpectations.length }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">已拒绝预期数</span>
                <span class="kv-value">{{ rejectedExpectations.length }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">一致预期状态</span>
                <span :class="`status-badge status-${consensusStatus}`">{{ consensusStatusLabel }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">市场隐含场景</span>
                <span class="kv-value">{{ marketImpliedScenarios.length }} 个</span>
              </div>
            </div>
          </div>

          <div class="valuation-block">
            <div class="block-title">盈利前瞻驱动</div>
            <div class="valuation-body">
              <div class="kv-item">
                <span class="kv-label">前瞻假设数</span>
                <span class="kv-value">{{ forwardDriverAssumptions.length }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">公司披露指引</span>
                <span class="kv-value">{{ forwardDisclosedIndicators.length }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">前瞻状态</span>
                <span :class="`status-badge status-${profitBridgeStatus}`">{{ profitBridgeStatusLabel }}</span>
              </div>
              <div class="kv-item">
                <span class="kv-label">市场隐含驱动</span>
                <span class="kv-value">{{ marketImpliedScenarios.length }} 个</span>
                <span class="kv-hint">(不计入前瞻完整度)</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 分区 7: 技术状态（不改变基本面结论） -->
      <section class="card card-technical">
        <div class="card-title">
          <i class="fas fa-chart-bar"></i> 技术状态
          <span class="warning-tag">⚠ 不改变基本面结论</span>
        </div>
        <div v-if="!hasTechnicalContext" class="card-empty">暂无本地证据</div>
        <div v-else class="technical-grid">
          <div class="kv-item">
            <span class="kv-label">技术状态</span>
            <span class="kv-value">{{ safeString(dossier.technical_context?.technical_state, '-') }}</span>
          </div>
          <div class="kv-item">
            <span class="kv-label">计算状态</span>
            <span :class="`status-badge status-${dossier.technical_context?.status || 'not_computable'}`">
              {{ statusLabel(dossier.technical_context?.status || 'not_computable') }}
            </span>
          </div>
          <div class="kv-item" v-if="dossier.technical_context?.ma5 !== undefined">
            <span class="kv-label">MA5</span>
            <span class="kv-value">{{ formatNumber(dossier.technical_context?.ma5) }}</span>
          </div>
          <div class="kv-item" v-if="dossier.technical_context?.ma20 !== undefined">
            <span class="kv-label">MA20</span>
            <span class="kv-value">{{ formatNumber(dossier.technical_context?.ma20) }}</span>
          </div>
          <div class="kv-item" v-if="dossier.technical_context?.rsi_14 !== undefined">
            <span class="kv-label">RSI(14)</span>
            <span class="kv-value">{{ formatNumber(dossier.technical_context?.rsi_14) }}</span>
          </div>
          <div class="kv-item" v-if="dossier.technical_context?.volume !== undefined">
            <span class="kv-label">成交量</span>
            <span class="kv-value">{{ formatNumber(dossier.technical_context?.volume, 0) }}</span>
          </div>
        </div>
        <div class="technical-disclaimer">
          技术状态仅用于辅助观察，不参与事实/盈利/估值可信度计算，不影响研究 readiness 结论。
        </div>
      </section>

      <!-- 分区 8: 证据 / 反证 / 缺口 / 下一步研究任务 -->
      <section class="card card-gaps">
        <div class="card-title">
          <i class="fas fa-magnifying-glass"></i> 证据 / 反证 / 缺口 / 下一步
        </div>
        <div class="gaps-grid">
          <div class="gaps-block">
            <div class="block-title">
              降级原因 / 缺口
              <span class="count">{{ degradationReasons.length }}</span>
            </div>
            <div class="block-body">
              <div v-if="!degradationReasons.length" class="block-empty">无缺口</div>
              <div v-for="(reason, idx) in degradationReasons" :key="`gap-${idx}`" class="gap-tag">
                {{ reason }}
              </div>
            </div>
          </div>

          <div class="gaps-block">
            <div class="block-title">
              来源快照
              <span class="count">{{ sourceSnapshotCount }}</span>
            </div>
            <div class="block-body">
              <div v-if="!sourceSnapshotCount" class="block-empty">暂无本地证据</div>
              <div v-for="(snap, key) in sourceSnapshots" :key="`snap-${key}`" class="source-item">
                <span class="source-name">{{ key }}</span>
                <span :class="`status-badge status-${snap?.status || 'not_computable'}`">
                  {{ statusLabel(snap?.status || 'not_computable') }}
                </span>
                <span class="source-count" v-if="snap?.records">
                  {{ snap.records.length }} 条
                </span>
              </div>
            </div>
          </div>

          <div class="gaps-block">
            <div class="block-title">
              运行验收
              <span class="count">{{ acceptanceFields.length }}</span>
            </div>
            <div class="block-body">
              <div v-for="field in acceptanceFields" :key="`acc-${field.key}`" class="kv-item">
                <span class="kv-label">{{ field.label }}</span>
                <span class="kv-value">{{ field.value }}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 分区 9: 历史运行与版本差异 -->
      <section class="card card-history">
        <div class="card-title">
          <i class="fas fa-clock-rotate-left"></i> 历史运行与版本差异
          <span class="card-subtitle">同输入重复运行生成新版本，不覆盖旧版本</span>
        </div>
        <div class="history-toolbar">
          <a-button size="small" @click="loadHistoryRuns" :loading="loadingHistory">
            <i class="fas fa-list"></i> 加载历史
          </a-button>
          <a-select
            v-model:value="selectedBaseRunId"
            placeholder="选择基线 run（用于 diff）"
            style="width: 280px"
            size="small"
            :options="historyRunOptions"
            allow-clear
          />
          <a-button
            size="small"
            type="primary"
            :disabled="!selectedBaseRunId || !currentRunId || selectedBaseRunId === currentRunId"
            @click="handleDiff"
            :loading="loadingDiff"
          >
            对比当前
          </a-button>
          <a-select
            v-model:value="selectedRunId"
            placeholder="切换到其他 run"
            style="width: 280px"
            size="small"
            :options="historyRunOptions"
            allow-clear
            @change="handleSwitchRun"
          />
        </div>
        <div v-if="!historyRuns.length && !loadingHistory" class="card-empty">
          点击「加载历史」查看公司所有运行
        </div>
        <div v-else class="history-table">
          <table>
            <thead>
              <tr>
                <th>运行 ID</th>
                <th>PIT 时点</th>
                <th>状态</th>
                <th>创建时间</th>
                <th>Schema</th>
                <th>来源指纹</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="run in historyRuns" :key="run.run_id">
                <td class="mono">{{ run.run_id.slice(-12) }}</td>
                <td>{{ run.as_of }}</td>
                <td>
                  <span :class="`status-badge status-${run.status}`">{{ run.status }}</span>
                </td>
                <td>{{ formatTime(run.created_at) }}</td>
                <td class="mono">{{ run.schema_version }}</td>
                <td class="mono">{{ sourceFingerprintShort(run.source_fingerprint) }}</td>
                <td>
                  <a-button size="small" @click="switchToRun(run.run_id)">切换</a-button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="diffResult" class="diff-result">
          <div class="diff-title">
            Diff: {{ diffResult.base_run?.run_id?.slice(-12) || '?' }}
            vs {{ diffResult.compare_run?.run_id?.slice(-12) || '?' }}
          </div>
          <div class="diff-grid">
            <div class="diff-block">
              <div class="block-title">
                事实变化
                <span class="count">+{{ factsAdded }} / -{{ factsRemoved }}</span>
              </div>
              <div class="diff-body">
                <div class="diff-item add">新增 verified_edges: {{ diffResult.facts?.verified_edges?.added?.length || 0 }}</div>
                <div class="diff-item rem">移除 verified_edges: {{ diffResult.facts?.verified_edges?.removed?.length || 0 }}</div>
                <div class="diff-item add">新增 observed_facts: {{ diffResult.facts?.observed_facts?.added?.length || 0 }}</div>
                <div class="diff-item rem">移除 observed_facts: {{ diffResult.facts?.observed_facts?.removed?.length || 0 }}</div>
                <div class="diff-item add">新增 forward_plans: {{ diffResult.facts?.forward_plans?.added?.length || 0 }}</div>
                <div class="diff-item rem">移除 forward_plans: {{ diffResult.facts?.forward_plans?.removed?.length || 0 }}</div>
              </div>
            </div>
            <div class="diff-block">
              <div class="block-title">
                缺口变化
                <span class="count">+{{ diffResult.gaps?.added?.length || 0 }} / -{{ diffResult.gaps?.removed?.length || 0 }}</span>
              </div>
              <div class="diff-body">
                <div v-for="(g, idx) in (diffResult.gaps?.added || []).slice(0, 5)" :key="`gap-add-${idx}`" class="diff-item add">
                  + {{ g }}
                </div>
                <div v-for="(g, idx) in (diffResult.gaps?.removed || []).slice(0, 5)" :key="`gap-rem-${idx}`" class="diff-item rem">
                  - {{ g }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- ============================================================ -->
    <!-- P1.9 CFA 风格研究报告（固定 10 章节） -->
    <!-- ============================================================ -->
    <section class="cfa-report-section">
      <div class="cfa-report-header">
        <div class="header-title">
          <div class="w-1 h-8 bg-gradient-to-b from-amber-500 to-orange-600 rounded-full"></div>
          <div>
            <h2 class="text-xl font-bold text-white tracking-tight">CFA 风格研究报告</h2>
            <p class="text-xs text-[#787b86] mt-1">
              P1.9 · 固定 10 章节 · {{ cfaNotice }}
            </p>
          </div>
        </div>
        <div class="header-actions">
          <a-button
            size="small"
            @click="handleBuildCfaReport"
            :loading="buildingCfa"
            :disabled="!symbolInput || !asOfInput"
          >
            <template #icon><i class="fas fa-file-circle-plus"></i></template>
            构建报告
          </a-button>
          <a-button
            size="small"
            @click="loadLatestCfaReport"
            :loading="loadingCfa"
          >
            <template #icon><i class="fas fa-sync"></i></template>
            刷新
          </a-button>
        </div>
      </div>

      <!-- CFA 报告降级提示 -->
      <div v-if="cfaDegradedReason" class="degraded-banner">
        <i class="fas fa-circle-info"></i>
        <span>CFA 报告处于降级状态：{{ cfaDegradedReason }}</span>
      </div>

      <!-- CFA 报告加载态 -->
      <div v-if="loadingCfa && !cfaReport" class="loading-state">
        <i class="fas fa-spinner fa-spin"></i>
        <span>正在加载 CFA 研究报告...</span>
      </div>

      <!-- CFA 报告空态 -->
      <div v-else-if="!cfaReport" class="empty-state">
        <i class="fas fa-folder-open"></i>
        <div class="empty-title">暂无 CFA 研究报告</div>
        <div class="empty-hint">点击「构建报告」生成 10 章节 CFA 风格研究报告</div>
      </div>

      <!-- CFA 报告主体：固定 10 章节 -->
      <div v-else class="cfa-report-body">
        <!-- 报告元数据 -->
        <div class="cfa-meta-bar">
          <div class="meta-item">
            <span class="meta-label">报告 ID</span>
            <span class="meta-value mono">{{ cfaReport.report_id.slice(-16) }}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">PIT 时点</span>
            <span class="meta-value">{{ safeString(cfaReport.as_of, '-') }}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">整体状态</span>
            <span class="meta-value">
              <span :class="`status-badge status-${cfaReport.overall_status}`">
                {{ cfaOverallStatusLabel(cfaReport.overall_status) }}
              </span>
            </span>
          </div>
          <div class="meta-item">
            <span class="meta-label">回看天数</span>
            <span class="meta-value">{{ cfaReport.lookback_days }} 天</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">模型版本</span>
            <span class="meta-value mono">{{ cfaReport.model_version }}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">创建时间</span>
            <span class="meta-value">{{ formatTime(cfaReport.created_at) }}</span>
          </div>
        </div>

        <!-- 固定 notice -->
        <div class="cfa-notice-banner">
          <i class="fas fa-circle-info"></i>
          <span>{{ cfaReport.notice }}</span>
        </div>

        <!-- 10 章节列表 -->
        <div class="cfa-sections">
          <div
            v-for="section in cfaReport.sections"
            :key="section.section"
            class="cfa-section-card"
            :class="`status-${section.section_status}`"
          >
            <div class="section-header">
              <div class="section-title">
                <i :class="cfaSectionIcon(section.section)"></i>
                <span>{{ section.title }}</span>
              </div>
              <span :class="`status-badge status-${section.section_status}`">
                {{ cfaSectionStatusLabel(section.section_status) }}
              </span>
            </div>

            <!-- 缺数据章节 -->
            <div v-if="section.section_status === 'missing'" class="section-empty">
              暂无本地证据
              <span v-if="section.limitations.length" class="empty-reason">
                （{{ section.limitations.join('; ') }}）
              </span>
            </div>

            <!-- 章节内容（按 section 类型分支渲染） -->
            <div v-else class="section-content">
              <!-- 章节 1: 研究范围与 as_of -->
              <template v-if="section.section === 'scope_and_as_of'">
                <div class="kv-grid">
                  <div class="kv-item">
                    <span class="kv-label">公司代码</span>
                    <span class="kv-value mono">{{ sectionContent(section, 'company_symbol', '-') }}</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">公司名称</span>
                    <span class="kv-value">{{ sectionContent(section, 'company_name', '未披露') }}</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">行业</span>
                    <span class="kv-value">{{ sectionContent(section, 'industry', '未披露') }}</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">数据范围</span>
                    <span class="kv-value">{{ sectionContent(section, 'data_scope', '-') }}</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">PIT 原则</span>
                    <span class="kv-value">{{ sectionContent(section, 'pit_principle', '-') }}</span>
                  </div>
                </div>
              </template>

              <!-- 章节 2: 公司战略和商业模式 -->
              <template v-else-if="section.section === 'strategy_business_model'">
                <div class="kv-grid">
                  <div class="kv-item">
                    <span class="kv-label">商业模式</span>
                    <span class="kv-value">{{ sectionContent(section, 'business_model', '未识别') }}</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">币种</span>
                    <span class="kv-value">{{ sectionContent(section, 'currency', '-') }}</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">单位</span>
                    <span class="kv-value">{{ sectionContent(section, 'unit', '-') }}</span>
                  </div>
                </div>
                <div class="section-note">{{ sectionContent(section, 'strategy_note', '') }}</div>
                <div v-if="(sectionContent(section, 'segments', []) as any[]).length" class="sub-block">
                  <div class="sub-title">业务分部 ({{ (sectionContent(section, 'segments', []) as any[]).length }})</div>
                  <div class="sub-body">
                    <div v-for="(seg, idx) in (sectionContent(section, 'segments', []) as any[])" :key="`seg-${idx}`" class="sub-item">
                      {{ safeString(seg?.segment_name || seg?.name, '未命名分部') }}
                    </div>
                  </div>
                </div>
              </template>

              <!-- 章节 3: 行业、竞争和产业链 -->
              <template v-else-if="section.section === 'industry_competition'">
                <div class="section-note">{{ sectionContent(section, 'member_of_theme_filter', '') }}</div>
                <div class="section-note">{{ sectionContent(section, 'company_mapping_note', '') }}</div>
                <div v-if="sectionContent(section, 'industry_profile', null)" class="kv-grid">
                  <div class="kv-item">
                    <span class="kv-label">行业名称</span>
                    <span class="kv-value">{{ sectionNestedContent(section, ['industry_profile', 'industry_name'], '未披露') }}</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">行业代码</span>
                    <span class="kv-value mono">{{ sectionNestedContent(section, ['industry_profile', 'industry_code'], '-') }}</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">代码体系</span>
                    <span class="kv-value">{{ sectionNestedContent(section, ['industry_profile', 'code_system'], '-') }}</span>
                  </div>
                </div>
              </template>

              <!-- 章节 4: 财务质量与会计调整 -->
              <template v-else-if="section.section === 'financial_quality'">
                <div class="section-note">{{ sectionContent(section, 'audit_boundary', '') }}</div>
                <div class="section-note">{{ sectionContent(section, 'adjustment_note', '') }}</div>
                <div v-if="sectionContent(section, 'quality_report', null)" class="kv-grid">
                  <div class="kv-item">
                    <span class="kv-label">质量报告状态</span>
                    <span class="kv-value">
                      {{ sectionNestedContent(section, ['quality_report', 'status'], '-') }}
                    </span>
                  </div>
                </div>
              </template>

              <!-- 章节 5: 产品驱动和盈利预测 + 图表 -->
              <template v-else-if="section.section === 'drivers_and_forecast'">
                <div class="section-note">{{ sectionContent(section, 'observation_vs_assumption_note', '') }}</div>
                <RevenueProfitCashflowChart
                  :data="revenueProfitCashflowData(section)"
                  :height="280"
                />
                <ProfitBridgeWaterfall
                  :items="profitBridgeItems(section)"
                  :start-value="profitBridgeStart(section)"
                  :end-value="profitBridgeEnd(section)"
                  :height="280"
                />
                <div class="sub-block">
                  <div class="sub-title">历史驱动 ({{ (sectionContent(section, 'historical_drivers', []) as any[]).length }})</div>
                  <div class="sub-body">
                    <div v-for="(drv, idx) in (sectionContent(section, 'historical_drivers', []) as any[]).slice(0, 8)" :key="`hd-${idx}`" class="sub-item">
                      <span class="sub-item-name">{{ safeString(drv?.name, '未命名') }}</span>
                      <span v-if="drv?.value != null" class="sub-item-value">{{ formatNumber(Number(drv.value)) }}</span>
                    </div>
                  </div>
                </div>
                <div class="sub-block">
                  <div class="sub-title">前瞻假设 ({{ (sectionContent(section, 'forward_assumptions', []) as any[]).length }})</div>
                  <div class="sub-body">
                    <div v-for="(asm, idx) in (sectionContent(section, 'forward_assumptions', []) as any[]).slice(0, 8)" :key="`fa-${idx}`" class="sub-item">
                      <span class="sub-item-name">{{ safeString(asm?.name, '未命名') }}</span>
                      <span v-if="asm?.assumption_text" class="sub-item-value">{{ asm.assumption_text }}</span>
                    </div>
                  </div>
                </div>
              </template>

              <!-- 章节 6: 公司指引、一致预期和研究差异 -->
              <template v-else-if="section.section === 'expectation_differences'">
                <div class="section-note">{{ sectionContent(section, 'separation_note', '') }}</div>
                <div class="kv-grid">
                  <div class="kv-item">
                    <span class="kv-label">公司指引</span>
                    <span class="kv-value">{{ (sectionContent(section, 'company_guidance', []) as any[]).length }} 条</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">第三方一致预期</span>
                    <span class="kv-value">{{ (sectionContent(section, 'consensus_estimates', []) as any[]).length }} 条</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">市场隐含预期</span>
                    <span class="kv-value">{{ (sectionContent(section, 'implied_expectations', []) as any[]).length }} 条</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">AquaTrade 预测</span>
                    <span class="kv-value">{{ (sectionContent(section, 'aquatrade_forecasts', []) as any[]).length }} 条</span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">预期差 gap</span>
                    <span class="kv-value">{{ (sectionContent(section, 'expectation_gaps', []) as any[]).length }} 条</span>
                  </div>
                </div>
              </template>

              <!-- 章节 7: 估值方法与敏感性 + 图表 -->
              <template v-else-if="section.section === 'valuation_sensitivity'">
                <div class="section-note">{{ sectionContent(section, 'range_note', '') }}</div>
                <div class="section-note">{{ sectionContent(section, 'no_action_note', '') }}</div>
                <ValuationBandChart
                  :band-data="valuationBandData(section)"
                  :forward-points="valuationForwardPoints(section)"
                  :height="280"
                />
                <div v-if="sectionContent(section, 'valuation_report', null)" class="kv-grid">
                  <div class="kv-item">
                    <span class="kv-label">估值方法数</span>
                    <span class="kv-value">
                      {{ (sectionNestedContent(section, ['valuation_report', 'cases'], []) as any[]).length }} 个
                    </span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">综合区间方法</span>
                    <span class="kv-value">
                      {{ sectionNestedContent(section, ['valuation_report', 'overall_range_method'], '-') }}
                    </span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">综合区间下界</span>
                    <span class="kv-value">
                      {{ formatMetric(sectionNestedContent(section, ['valuation_report', 'overall_range', 'low'], null)) }}
                    </span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">综合区间上界</span>
                    <span class="kv-value">
                      {{ formatMetric(sectionNestedContent(section, ['valuation_report', 'overall_range', 'high'], null)) }}
                    </span>
                  </div>
                </div>
                <div class="sub-block">
                  <div class="sub-title">估值案例 ({{ (sectionContent(section, 'valuation_cases', []) as any[]).length }})</div>
                  <div class="sub-body">
                    <div v-for="(c, idx) in (sectionContent(section, 'valuation_cases', []) as any[]).slice(0, 8)" :key="`vc-${idx}`" class="sub-item">
                      <span class="sub-item-name">{{ safeString(c?.method, '未命名方法') }}</span>
                      <span v-if="c?.output_interval" class="sub-item-value">
                        [{{ formatMetric(c.output_interval?.low) }}, {{ formatMetric(c.output_interval?.high) }}]
                      </span>
                    </div>
                  </div>
                </div>
              </template>

              <!-- 章节 8: 管理层、治理和资本配置 -->
              <template v-else-if="section.section === 'governance_capital'">
                <div class="section-note">{{ sectionContent(section, 'emotional_news_note', '') }}</div>
                <div class="section-note">{{ sectionContent(section, 'commitment_note', '') }}</div>
                <div class="section-note">{{ sectionContent(section, 'risk_separation_note', '') }}</div>
                <div v-if="sectionContent(section, 'governance_ledger', null)" class="kv-grid">
                  <div class="kv-item">
                    <span class="kv-label">管理层承诺</span>
                    <span class="kv-value">
                      {{ (sectionNestedContent(section, ['governance_ledger', 'management_commitments'], []) as any[]).length }} 条
                    </span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">管理层人员</span>
                    <span class="kv-value">
                      {{ (sectionNestedContent(section, ['governance_ledger', 'management_persons'], []) as any[]).length }} 人
                    </span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">治理风险</span>
                    <span class="kv-value">
                      {{ (sectionNestedContent(section, ['governance_ledger', 'governance_risks'], []) as any[]).length }} 项
                    </span>
                  </div>
                  <div class="kv-item">
                    <span class="kv-label">资本动作</span>
                    <span class="kv-value">
                      {{ (sectionNestedContent(section, ['governance_ledger', 'capital_actions'], []) as any[]).length }} 项
                    </span>
                  </div>
                </div>
              </template>

              <!-- 章节 9: 重要风险、反证和模型限制 -->
              <template v-else-if="section.section === 'risks_counterevidence'">
                <div class="section-note">{{ sectionContent(section, 'no_trade_advice_note', '') }}</div>
                <div class="sub-block">
                  <div class="sub-title">
                    反证证据 ({{ (sectionContent(section, 'counter_evidence', []) as any[]).length }})
                  </div>
                  <div class="sub-body">
                    <div v-if="!(sectionContent(section, 'counter_evidence', []) as any[]).length" class="block-empty">暂无本地证据</div>
                    <div v-for="(ce, idx) in (sectionContent(section, 'counter_evidence', []) as any[]).slice(0, 6)" :key="`ce-${idx}`" class="sub-item">
                      <span class="sub-item-name">{{ safeString(ce, '未命名') }}</span>
                    </div>
                  </div>
                </div>
                <div class="sub-block">
                  <div class="sub-title">
                    模型失效条件 ({{ (sectionContent(section, 'model_failure_conditions', []) as any[]).length }})
                  </div>
                  <div class="sub-body">
                    <div v-for="(mf, idx) in (sectionContent(section, 'model_failure_conditions', []) as any[])" :key="`mf-${idx}`" class="sub-item warning">
                      <span class="sub-item-name">{{ safeString(mf, '-') }}</span>
                    </div>
                  </div>
                </div>
              </template>

              <!-- 章节 10: 来源、模型版本和记录留存信息 -->
              <template v-else-if="section.section === 'sources_and_versions'">
                <div class="section-note">{{ sectionContent(section, 'audit_chain_note', '') }}</div>
                <div class="sub-block">
                  <div class="sub-title">
                    来源引用 ({{ (sectionContent(section, 'source_refs', []) as any[]).length }})
                  </div>
                  <div class="sub-body">
                    <div v-if="!(sectionContent(section, 'source_refs', []) as any[]).length" class="block-empty">暂无本地证据</div>
                    <div v-for="(ref, idx) in (sectionContent(section, 'source_refs', []) as any[]).slice(0, 20)" :key="`ref-${idx}`" class="source-tag">
                      {{ ref }}
                    </div>
                  </div>
                </div>
                <div class="sub-block">
                  <div class="sub-title">模型版本</div>
                  <div class="sub-body">
                    <div v-for="(ver, key) in (sectionContent(section, 'model_versions', {}) as Record<string, string>)" :key="`ver-${key}`" class="sub-item">
                      <span class="sub-item-name">{{ key }}</span>
                      <span class="sub-item-value mono">{{ ver }}</span>
                    </div>
                  </div>
                </div>
              </template>

              <!-- 未知章节（兼容性降级） -->
              <template v-else>
                <div class="kv-grid">
                  <div v-for="(val, key) in section.content" :key="`uk-${key}`" class="kv-item">
                    <span class="kv-label">{{ key }}</span>
                    <span class="kv-value">{{ safeString(val, '-') }}</span>
                  </div>
                </div>
              </template>

              <!-- 章节限制说明 -->
              <div v-if="section.limitations.length" class="section-limitations">
                <span class="lim-label">限制:</span>
                <span v-for="(lim, idx) in section.limitations" :key="`lim-${idx}`" class="lim-tag">
                  {{ lim }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import dayjs from 'dayjs';
import { message } from 'ant-design-vue';
import ResearchReadinessPanel from '@/components/research/ResearchReadinessPanel.vue';
import RevenueProfitCashflowChart, {
  type PeriodPoint,
} from '@/components/research/RevenueProfitCashflowChart.vue';
import ProfitBridgeWaterfall, {
  type BridgeItem,
} from '@/components/research/ProfitBridgeWaterfall.vue';
import ValuationBandChart, {
  type BandPoint,
  type ForwardPoint,
} from '@/components/research/ValuationBandChart.vue';
import {
  createRun,
  getLatest,
  listRuns,
  getRun,
  diffRuns,
  CompanyResearchApiError,
  type RunBundle,
  type ResearchRunRecord,
  type RunDiffResponse,
} from '@/api/companyResearch';
import {
  buildCfaReport,
  getLatestCfaReport,
  CfaReportApiError,
  type CfaReport,
  type CfaReportSection,
} from '@/api/cfaReport';
import {
  formatPercent,
  formatScore,
  formatAmount,
  formatNumber,
  formatTime,
  formatSafe,
  noEvidence,
} from '@/utils/format';

/** CFA 报告固定 notice 文本。 */
const CFA_NOTICE = '研究数据产物，不包含交易动作建议。';

/** 默认 symbol：贵州茅台（流动性最好，便于 smoke）。 */
const DEFAULT_SYMBOL = '600519.SH';

/** 页面状态。 */
const symbolInput = ref(DEFAULT_SYMBOL);
const asOfInput = ref(dayjs());
const lookbackInput = ref(120);

/** 最新 bundle。 */
const latestBundle = ref<RunBundle | null>(null);
const loadingLatest = ref(false);
const creating = ref(false);

/** 错误信息。 */
const pageError = ref<{ message: string; code?: string; trace_id?: string } | null>(null);

/** 历史运行。 */
const historyRuns = ref<ResearchRunRecord[]>([]);
const loadingHistory = ref(false);
const selectedBaseRunId = ref<string | undefined>(undefined);
const selectedRunId = ref<string | undefined>(undefined);

/** diff 结果。 */
const diffResult = ref<RunDiffResponse | null>(null);
const loadingDiff = ref(false);

/** CFA 报告状态。 */
const cfaReport = ref<CfaReport | null>(null);
const loadingCfa = ref(false);
const buildingCfa = ref(false);
const cfaDegradedReason = ref<string>('');

/** CFA 固定 notice。 */
const cfaNotice = computed(() => CFA_NOTICE);

// ---------------------------------------------------------------------------
// 派生
// ---------------------------------------------------------------------------

const dossier = computed(() => latestBundle.value?.dossier || null);
const currentRunId = computed(() => latestBundle.value?.run?.run_id || '');
const runStatus = computed(() => {
  const status = String(latestBundle.value?.run?.status || latestBundle.value?.acceptance?.status || 'partial');
  return status;
});
const runStatusLabel = computed(() => statusLabel(runStatus.value));
const runIdShort = computed(() => currentRunId.value ? currentRunId.value.slice(-12) : '-');

const degradedReason = computed(() => {
  if (!latestBundle.value) return '';
  if (latestBundle.value.degraded) {
    return latestBundle.value.degradation_reason || 'degraded';
  }
  return '';
});

/** 已验证事实：来自 industry_context.verified_edges + observed_facts。 */
const verifiedEdges = computed(() => arrayFrom(dossier.value?.industry_context?.verified_edges));
const observedFacts = computed(() => arrayFrom(dossier.value?.industry_context?.observed_facts));
const forwardPlans = computed(() => arrayFrom(dossier.value?.industry_context?.forward_plans));
const hasCausalContext = computed(() => verifiedEdges.value.length || observedFacts.value.length || forwardPlans.value.length);

/** 已验证事实（合并 verified_edges 中的证据 + observed_facts）。 */
const verifiedFacts = computed(() => {
  const formalFacts = arrayFrom(dossier.value?.research_facts);
  if (formalFacts.length) return formalFacts;
  const items: Array<Record<string, any>> = [];
  for (const edge of verifiedEdges.value) {
    items.push({
      metric_or_relation: edge.relation || 'verified_edge',
      value: edge.evidence_confidence ?? null,
      period: edge.period || '',
      evidence_ids: Array.isArray(edge.evidence)
        ? edge.evidence.map((e: any) => e.evidence_id || e.evidence_evidence_id).filter(Boolean)
        : [],
    });
  }
  for (const fact of observedFacts.value) {
    items.push({
      metric_or_relation: fact.relation || fact.subject_name || 'observed_fact',
      value: fact.evidence_confidence ?? fact.value ?? null,
      period: fact.period || '',
      evidence_ids: Array.isArray(fact.evidence)
        ? fact.evidence.map((e: any) => e.evidence_id).filter(Boolean)
        : Array.isArray(fact.evidence_ids) ? fact.evidence_ids : [],
    });
  }
  return items;
});

/** 市场观点：来自 expectation_context.verified_expectations + structured_consensus。 */
const verifiedExpectations = computed(() => arrayFrom(dossier.value?.expectation_context?.verified_expectations));
const rejectedExpectations = computed(() => arrayFrom(dossier.value?.expectation_context?.rejected_expectations));
const marketOpinions = computed(() => {
  const list: Array<Record<string, any>> = [];
  for (const exp of verifiedExpectations.value) {
    list.push({
      metric: exp.metric || exp.variable_name || exp.relation || 'expectation',
      value: exp.value ?? exp.evidence_confidence ?? null,
      period: exp.period || '',
      source_type: exp.source_type || 'third_party_consensus',
    });
  }
  return list;
});

/** 一致预期状态。 */
const consensusStatus = computed(() => {
  const consensus = dossier.value?.expectation_context?.structured_consensus as any;
  return String(consensus?.status || dossier.value?.expectation_context?.status || 'not_computable');
});
const consensusStatusLabel = computed(() => statusLabel(consensusStatus.value));

/** 假设：来自 profit_bridge.forward_driver_assumptions。 */
const assumptions = computed(() => {
  const formalAssumptions = arrayFrom(dossier.value?.research_assumptions);
  return formalAssumptions.length
    ? formalAssumptions
    : arrayFrom(dossier.value?.profit_bridge?.forward_driver_assumptions);
});
const forwardDriverAssumptions = computed(() => arrayFrom(dossier.value?.profit_bridge?.forward_driver_assumptions));
const forwardDisclosedIndicators = computed(() => arrayFrom(dossier.value?.profit_bridge?.forward_disclosed_indicators));
const marketImpliedScenarios = computed(() => arrayFrom(dossier.value?.profit_bridge?.market_implied_scenarios));
const profitBridgeStatus = computed(() => String(dossier.value?.profit_bridge?.status || 'not_computable'));
const profitBridgeStatusLabel = computed(() => statusLabel(profitBridgeStatus.value));

/** 财务历史。 */
const financialHistory = computed(() => arrayFrom(dossier.value?.financial_history));

/** 技术状态。 */
const hasTechnicalContext = computed(() => {
  const tech = dossier.value?.technical_context;
  return tech && Object.keys(tech).length > 0 && tech.status !== 'not_computable';
});

/** 缺口。 */
const degradationReasons = computed(() => arrayFrom(dossier.value?.degradation_reasons));

/** 来源快照。 */
const sourceSnapshots = computed(() => {
  const snaps = dossier.value?.source_snapshots;
  if (!snaps || typeof snaps !== 'object') return {} as Record<string, any>;
  return snaps as Record<string, any>;
});
const sourceSnapshotCount = computed(() => Object.keys(sourceSnapshots.value).length);

/** 运行验收字段。 */
const acceptanceFields = computed(() => {
  const acc = latestBundle.value?.acceptance || {};
  return [
    { key: 'status', label: '状态', value: formatSafe(acc.status, '-') },
    { key: 'financial_quality_status', label: '财务质量', value: formatSafe(acc.financial_quality_status, '-') },
    { key: 'valuation_status', label: '估值状态', value: formatSafe(acc.valuation_status, '-') },
    { key: 'technical_status', label: '技术状态', value: formatSafe(acc.technical_status, '-') },
    { key: 'finished_at', label: '完成时间', value: formatTime(String(acc.finished_at || '')) },
    { key: 'gap_tasks', label: '缺口任务', value: formatGapTasks(acc.gap_tasks) },
  ];
});

/** 历史运行下拉选项。 */
const historyRunOptions = computed(() => {
  return historyRuns.value.map((r) => ({
    label: `${r.run_id.slice(-12)} · ${r.as_of} · ${r.status}`,
    value: r.run_id,
  }));
});

/** diff 派生。 */
const factsAdded = computed(() => {
  const f = diffResult.value?.facts;
  if (!f) return 0;
  return (f.verified_edges?.added?.length || 0)
    + (f.observed_facts?.added?.length || 0)
    + (f.forward_plans?.added?.length || 0);
});
const factsRemoved = computed(() => {
  const f = diffResult.value?.facts;
  if (!f) return 0;
  return (f.verified_edges?.removed?.length || 0)
    + (f.observed_facts?.removed?.length || 0)
    + (f.forward_plans?.removed?.length || 0);
});

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

/** 安全转换数组。 */
function arrayFrom<T = any>(v: unknown): T[] {
  if (Array.isArray(v)) return v as T[];
  return [];
}

/** 安全字符串。 */
function safeString(v: unknown, fallback = '-'): string {
  if (v === null || v === undefined || v === '') return fallback;
  return String(v);
}

/** 状态中文标签。 */
function statusLabel(status: string): string {
  const map: Record<string, string> = {
    ok: '完整',
    partial: '部分',
    blocked: '阻断',
    failed: '失败',
    not_computable: '不可计算',
    degraded: '降级',
    verified: '已验证',
    rejected: '已拒绝',
    market_implied: '市场隐含',
    missing: '缺失',
    empty: '空',
  };
  return map[status] || status;
}

/** 来源类型中文标签。 */
function sourceTypeLabel(t: string): string {
  const map: Record<string, string> = {
    company_disclosed: '公司披露',
    third_party_consensus: '第三方一致预期',
    market_implied: '市场隐含',
    analyst_assumption: '分析师假设',
  };
  return map[t] || t;
}

/** 数值指标格式化（None 显示 "-"）。 */
function formatMetric(v: unknown): string {
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  if (!Number.isFinite(n)) return '-';
  return n.toFixed(2);
}

/** 事实值格式化。 */
function formatFactValue(fact: Record<string, any>): string {
  const v = fact.value;
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  // 0~1 视为比例
  if (Math.abs(n) <= 1) return formatPercent(n);
  return formatNumber(n);
}

/** 区间格式化。 */
function formatInterval(interval: any): string {
  if (!interval || typeof interval !== 'object') return '-';
  const low = interval.low;
  const high = interval.high;
  if (low === null || low === undefined || high === null || high === undefined) return '-';
  return `[${Number(low).toFixed(4)}, ${Number(high).toFixed(4)}]`;
}

/** 缺口任务格式化。 */
function formatGapTasks(gapTasks: any): string {
  if (!gapTasks || typeof gapTasks !== 'object') return '-';
  const created = Array.isArray(gapTasks.created_task_ids) ? gapTasks.created_task_ids.length : 0;
  const existing = Array.isArray(gapTasks.existing_task_ids) ? gapTasks.existing_task_ids.length : 0;
  return `新增 ${created} / 复用 ${existing}`;
}

/** 来源指纹缩短显示。 */
function sourceFingerprintShort(fp: string | undefined): string {
  if (!fp) return '-';
  return fp.length > 16 ? `${fp.slice(0, 8)}...${fp.slice(-4)}` : fp;
}

// ---------------------------------------------------------------------------
// 业务逻辑
// ---------------------------------------------------------------------------

/** 查询最新 run。 */
async function handleSearch() {
  pageError.value = null;
  if (!symbolInput.value) {
    pageError.value = { message: '请输入 A 股代码（如 600519.SH）', code: 'INVALID_SYMBOL' };
    return;
  }
  loadingLatest.value = true;
  try {
    const bundle = await getLatest(symbolInput.value.trim().toUpperCase());
    latestBundle.value = bundle;
    if (bundle.degraded && !bundle.dossier) {
      // 暂无运行，不视为错误
      message.info(`暂无 ${symbolInput.value} 的研究运行，可点击「创建研究运行」生成`);
    }
  } catch (e: any) {
    handleError(e);
  } finally {
    loadingLatest.value = false;
  }
  // 同时加载 CFA 报告
  await loadLatestCfaReport();
}

/** 创建新 run。 */
async function handleCreateRun() {
  pageError.value = null;
  if (!symbolInput.value || !asOfInput.value) {
    pageError.value = { message: '请填写 symbol 与 as_of', code: 'INVALID_INPUT' };
    return;
  }
  creating.value = true;
  try {
    const asOfStr = dayjs(asOfInput.value).format('YYYY-MM-DD');
    const result = await createRun(symbolInput.value.trim().toUpperCase(), asOfStr, {
      lookback_days: lookbackInput.value,
    });
    message.success(`研究运行已创建：${result.run_id.slice(-12)}`);
    // 刷新最新 run 与历史
    await handleSearch();
    await loadHistoryRuns();
  } catch (e: any) {
    handleError(e);
  } finally {
    creating.value = false;
  }
}

/** 刷新当前 run。 */
async function refreshCurrent() {
  await handleSearch();
}

/** 加载历史运行。 */
async function loadHistoryRuns() {
  if (!symbolInput.value) return;
  loadingHistory.value = true;
  try {
    const result = await listRuns(symbolInput.value.trim().toUpperCase(), { page: 1, page_size: 50 });
    historyRuns.value = result.items || [];
  } catch (e: any) {
    handleError(e, false);
  } finally {
    loadingHistory.value = false;
  }
}

/** 切换到其他 run。 */
async function switchToRun(runId: string) {
  if (!runId) return;
  loadingLatest.value = true;
  pageError.value = null;
  try {
    const bundle = await getRun(runId);
    latestBundle.value = bundle;
    selectedRunId.value = undefined;
    message.success(`已切换到运行 ${runId.slice(-12)}`);
  } catch (e: any) {
    handleError(e);
  } finally {
    loadingLatest.value = false;
  }
}

function handleSwitchRun(value: any) {
  if (value && typeof value === 'string') {
    switchToRun(value);
  }
}

/** 执行 diff。 */
async function handleDiff() {
  if (!selectedBaseRunId.value || !currentRunId.value) return;
  if (selectedBaseRunId.value === currentRunId.value) {
    message.warning('基线 run 与当前 run 不能相同');
    return;
  }
  loadingDiff.value = true;
  try {
    diffResult.value = await diffRuns(currentRunId.value, selectedBaseRunId.value);
  } catch (e: any) {
    handleError(e, false);
  } finally {
    loadingDiff.value = false;
  }
}

/** 统一错误处理。 */
function handleError(e: unknown, toast = true) {
  if (e instanceof CompanyResearchApiError) {
    pageError.value = {
      message: e.message,
      code: e.code,
      trace_id: e.trace_id,
    };
  } else {
    const responseError = (e as {
      response?: { data?: { error?: { message?: unknown; code?: unknown; trace_id?: unknown } } };
    } | null)?.response?.data?.error;
    pageError.value = {
      message: typeof responseError?.message === 'string'
        ? responseError.message
        : '研究服务请求失败，请稍后重试',
      code: typeof responseError?.code === 'string' ? responseError.code : 'REQUEST_FAILED',
      trace_id: typeof responseError?.trace_id === 'string' ? responseError.trace_id : undefined,
    };
  }
  if (toast) {
    message.error(pageError.value?.message || '请求失败');
  }
}

// ---------------------------------------------------------------------------
// CFA 报告业务逻辑
// ---------------------------------------------------------------------------

/** 加载最新 CFA 报告（PIT 过滤）。 */
async function loadLatestCfaReport() {
  if (!symbolInput.value) {
    cfaReport.value = null;
    cfaDegradedReason.value = '';
    return;
  }
  loadingCfa.value = true;
  cfaDegradedReason.value = '';
  try {
    const asOfStr = asOfInput.value ? dayjs(asOfInput.value).format('YYYY-MM-DD') : '';
    const report = await getLatestCfaReport(
      symbolInput.value.trim().toUpperCase(),
      asOfStr,
    );
    cfaReport.value = report;
    if (!report) {
      cfaDegradedReason.value = 'no_report_for_symbol';
    } else if (report.overall_status === 'degraded') {
      cfaDegradedReason.value = report.degradation_reasons.join('; ') || 'degraded';
    }
  } catch (e: any) {
    if (e instanceof CfaReportApiError) {
      cfaDegradedReason.value = `${e.code}: ${e.message}`;
    } else {
      cfaDegradedReason.value = '请求失败';
    }
    cfaReport.value = null;
  } finally {
    loadingCfa.value = false;
  }
}

/** 构建 CFA 报告（POST /cfa）。 */
async function handleBuildCfaReport() {
  if (!symbolInput.value || !asOfInput.value) {
    message.warning('请填写 symbol 与 as_of');
    return;
  }
  buildingCfa.value = true;
  try {
    const asOfStr = dayjs(asOfInput.value).format('YYYY-MM-DD');
    const report = await buildCfaReport({
      company_symbol: symbolInput.value.trim().toUpperCase(),
      as_of: asOfStr,
      lookback_days: lookbackInput.value,
    });
    cfaReport.value = report;
    if (report.overall_status === 'degraded') {
      cfaDegradedReason.value = report.degradation_reasons.join('; ') || 'degraded';
      message.warning(`CFA 报告已生成（降级状态）：${cfaDegradedReason.value}`);
    } else {
      cfaDegradedReason.value = '';
      message.success(`CFA 报告已生成：${report.report_id.slice(-12)}`);
    }
  } catch (e: any) {
    handleError(e);
  } finally {
    buildingCfa.value = false;
  }
}

// ---------------------------------------------------------------------------
// CFA 报告章节辅助函数
// ---------------------------------------------------------------------------

/** 整体状态中文标签。 */
function cfaOverallStatusLabel(status: string): string {
  const map: Record<string, string> = {
    ok: '完整',
    partial: '部分',
    degraded: '降级',
  };
  return map[status] || status;
}

/** 章节状态中文标签。 */
function cfaSectionStatusLabel(status: string): string {
  const map: Record<string, string> = {
    ok: '完整',
    partial: '部分',
    missing: '缺失',
    not_applicable: '不适用',
  };
  return map[status] || status;
}

/** 章节 icon。 */
function cfaSectionIcon(section: string): string {
  const map: Record<string, string> = {
    scope_and_as_of: 'fas fa-bookmark',
    strategy_business_model: 'fas fa-chess-knight',
    industry_competition: 'fas fa-industry',
    financial_quality: 'fas fa-chart-line',
    drivers_and_forecast: 'fas fa-gears',
    expectation_differences: 'fas fa-scale-balanced',
    valuation_sensitivity: 'fas fa-money-bill-trend-up',
    governance_capital: 'fas fa-people-group',
    risks_counterevidence: 'fas fa-triangle-exclamation',
    sources_and_versions: 'fas fa-link',
  };
  return map[section] || 'fas fa-circle-info';
}

/** 安全取章节内容字段。 */
function sectionContent(section: CfaReportSection, key: string, fallback: unknown): any {
  const v = section.content?.[key];
  if (v === null || v === undefined || v === '') return fallback;
  return v;
}

/** 安全取章节嵌套内容字段。 */
function sectionNestedContent(section: CfaReportSection, keys: string[], fallback: unknown): any {
  let cur: any = section.content;
  for (const k of keys) {
    if (cur == null || typeof cur !== 'object') return fallback;
    cur = cur[k];
  }
  if (cur === null || cur === undefined || cur === '') return fallback;
  return cur;
}

/** 章节 5：构造收入/利润/现金流图数据。 */
function revenueProfitCashflowData(section: CfaReportSection): PeriodPoint[] {
  const result: PeriodPoint[] = [];
  const integrated = sectionContent(section, 'integrated_forecast', null) as Record<string, any> | null;
  if (integrated) {
    // 若 integrated_forecast 含 history/forward 序列
    const history = Array.isArray(integrated.history) ? integrated.history : [];
    for (const h of history) {
      if (h && typeof h === 'object') {
        result.push({
          period: String(h.period || h.end_date || ''),
          revenue: h.revenue != null ? Number(h.revenue) : null,
          net_profit: h.net_profit != null ? Number(h.net_profit) : null,
          operating_cashflow: h.operating_cashflow != null ? Number(h.operating_cashflow) : null,
          is_forecast: false,
        });
      }
    }
    const forward = Array.isArray(integrated.forward) ? integrated.forward : [];
    for (const f of forward) {
      if (f && typeof f === 'object') {
        result.push({
          period: String(f.period || f.end_date || ''),
          revenue: f.revenue != null ? Number(f.revenue) : null,
          net_profit: f.net_profit != null ? Number(f.net_profit) : null,
          operating_cashflow: f.operating_cashflow != null ? Number(f.operating_cashflow) : null,
          is_forecast: true,
        });
      }
    }
  }
  return result;
}

/** 章节 5：构造利润桥瀑布图数据。 */
function profitBridgeItems(section: CfaReportSection): BridgeItem[] {
  const items: BridgeItem[] = [];
  // 从 forward_assumptions 构造驱动因素
  const assumptions = sectionContent(section, 'forward_assumptions', []) as any[];
  for (const asm of assumptions) {
    if (!asm || typeof asm !== 'object') continue;
    const value = typeof asm.value === 'number' ? asm.value : null;
    if (value == null) continue;
    items.push({
      name: String(asm.name || '未命名'),
      delta: value,
      kind: 'driver',
    });
  }
  return items;
}

/** 章节 5：利润桥起点值（来自历史末点净利润）。 */
function profitBridgeStart(section: CfaReportSection): number | null {
  const points = revenueProfitCashflowData(section);
  for (let i = points.length - 1; i >= 0; i--) {
    if (!points[i].is_forecast && points[i].net_profit != null) {
      return points[i].net_profit as number;
    }
  }
  return null;
}

/** 章节 5：利润桥终点值（来自前瞻末点净利润）。 */
function profitBridgeEnd(section: CfaReportSection): number | null {
  const points = revenueProfitCashflowData(section);
  for (let i = points.length - 1; i >= 0; i--) {
    if (points[i].is_forecast && points[i].net_profit != null) {
      return points[i].net_profit as number;
    }
  }
  return null;
}

/** 章节 7：构造历史估值带数据。 */
function valuationBandData(section: CfaReportSection): BandPoint[] {
  // 从 sensitivity_points 构造（每个时点输出 low/high 区间）
  const points = sectionContent(section, 'sensitivity_points', []) as any[];
  const grouped: Map<string, { lows: number[]; highs: number[] }> = new Map();
  for (const p of points) {
    if (!p || typeof p !== 'object') continue;
    const period = String(p.period || p.as_of || '未指定');
    const low = typeof p.output_low === 'number' ? p.output_low
      : typeof p.variable_value === 'number' ? p.variable_value : null;
    const high = typeof p.output_high === 'number' ? p.output_high : low;
    if (low == null || high == null) continue;
    if (!grouped.has(period)) grouped.set(period, { lows: [], highs: [] });
    grouped.get(period)!.lows.push(low);
    grouped.get(period)!.highs.push(high);
  }
  const result: BandPoint[] = [];
  for (const [period, agg] of grouped) {
    if (!agg.lows.length || !agg.highs.length) continue;
    const low = Math.min(...agg.lows);
    const high = Math.max(...agg.highs);
    result.push({
      period,
      low,
      high,
      mid: (low + high) / 2,
    });
  }
  return result;
}

/** 章节 7：构造 Forward 估值散点。 */
function valuationForwardPoints(section: CfaReportSection): ForwardPoint[] {
  const cases = sectionContent(section, 'valuation_cases', []) as any[];
  const result: ForwardPoint[] = [];
  for (const c of cases) {
    if (!c || typeof c !== 'object') continue;
    const interval = c.output_per_share || c.output_interval;
    if (!interval) continue;
    const low = typeof interval.low === 'number' ? interval.low : null;
    const high = typeof interval.high === 'number' ? interval.high : null;
    if (low == null || high == null) continue;
    result.push({
      method: String(c.method || '未命名'),
      value: (low + high) / 2,
      low,
      high,
    });
  }
  return result;
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------

onMounted(async () => {
  await handleSearch();
});
</script>

<style scoped>
.company-research-page {
  padding: 16px 24px;
  min-height: calc(100vh - 64px);
}

/* Header */
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}
.header-title {
  display: flex;
  align-items: center;
  gap: 12px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* Error / degraded banners */
.error-banner {
  background: rgba(255, 77, 79, 0.1);
  border: 1px solid #ff4d4f;
  border-radius: 6px;
  padding: 10px 16px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  color: #ffa39e;
  font-size: 13px;
  flex-wrap: wrap;
}
.error-banner i {
  color: #ff4d4f;
}
.error-message {
  flex: 1;
}
.error-code {
  font-family: monospace;
  background: rgba(255, 77, 79, 0.2);
  padding: 2px 6px;
  border-radius: 3px;
}
.error-trace {
  font-family: monospace;
  font-size: 11px;
  color: #787b86;
}

.degraded-banner {
  background: rgba(250, 173, 20, 0.1);
  border: 1px solid #faad14;
  border-radius: 6px;
  padding: 10px 16px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: #faad14;
  font-size: 13px;
}

.loading-state,
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 64px;
  color: #787b86;
  font-size: 14px;
}
.loading-state i,
.empty-state i {
  font-size: 32px;
  color: #5470c6;
}
.empty-title {
  font-size: 18px;
  color: #d1d4dc;
  font-weight: 500;
}

/* 九分区 grid */
.research-grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 16px;
}

.card {
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 8px;
  padding: 16px;
}

/* 卡片标题 */
.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.card-title i {
  color: #5470c6;
}
.card-subtitle {
  font-size: 11px;
  color: #787b86;
  font-weight: 400;
  margin-left: 4px;
}

/* 卡片布局 */
.card-company {
  grid-column: span 12;
}
.card-readiness {
  grid-column: span 12;
}
.card-facts {
  grid-column: span 12;
}
.card-financial {
  grid-column: span 6;
}
.card-causal {
  grid-column: span 6;
}
.card-valuation {
  grid-column: span 12;
}
.card-technical {
  grid-column: span 6;
}
.card-gaps {
  grid-column: span 6;
}
.card-history {
  grid-column: span 12;
}

@media (max-width: 1440px) {
  .card-financial, .card-causal, .card-technical, .card-gaps {
    grid-column: span 12;
  }
}

@media (max-width: 1280px) {
  .research-grid {
    grid-template-columns: 1fr;
  }
  .card-company, .card-readiness, .card-facts, .card-financial, .card-causal,
  .card-valuation, .card-technical, .card-gaps, .card-history {
    grid-column: span 1;
  }
}

/* 公司区 */
.company-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}
.company-name {
  font-size: 22px;
  font-weight: 700;
  color: #fff;
}
.company-symbol {
  font-family: monospace;
  font-size: 14px;
  color: #5470c6;
  padding: 2px 8px;
  background: rgba(84, 112, 198, 0.15);
  border-radius: 4px;
}
.company-meta {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.meta-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.meta-label {
  font-size: 11px;
  color: #787b86;
}
.meta-value {
  font-size: 13px;
  color: #d1d4dc;
  word-break: break-all;
}
.mono {
  font-family: monospace;
}

/* 状态徽章 */
.status-badge {
  display: inline-block;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 500;
}
.status-ok { background: rgba(82, 196, 26, 0.18); color: #52c41a; }
.status-partial { background: rgba(250, 173, 20, 0.18); color: #faad14; }
.status-blocked, .status-failed { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.status-not_computable, .status-missing, .status-empty { background: rgba(120, 123, 134, 0.18); color: #787b86; }
.status-verified { background: rgba(82, 196, 26, 0.18); color: #52c41a; }
.status-rejected { background: rgba(255, 77, 79, 0.18); color: #ff4d4f; }
.status-market_implied { background: rgba(84, 112, 198, 0.18); color: #5470c6; }
.status-degraded { background: rgba(250, 173, 20, 0.18); color: #faad14; }

/* 事实/观点/假设 三分栏 */
.facts-columns {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
@media (max-width: 1280px) {
  .facts-columns {
    grid-template-columns: 1fr;
  }
}
.facts-col {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 10px;
}
.col-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid #2a2e39;
}
.col-label {
  font-size: 13px;
  font-weight: 600;
}
.col-label.verified { color: #52c41a; }
.col-label.opinion { color: #faad14; }
.col-label.assumption { color: #5470c6; }
.col-count {
  font-size: 11px;
  color: #787b86;
  font-family: monospace;
}
.col-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.col-empty {
  color: #787b86;
  font-size: 12px;
  text-align: center;
  padding: 16px 0;
}
.fact-item {
  background: rgba(255, 255, 255, 0.02);
  border-radius: 4px;
  padding: 6px 8px;
  border-left: 2px solid #2a2e39;
}
.fact-item:hover {
  background: rgba(255, 255, 255, 0.05);
}
.fact-title {
  font-size: 12px;
  color: #d1d4dc;
  font-weight: 500;
  margin-bottom: 4px;
}
.fact-meta {
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: #a0a4b0;
  flex-wrap: wrap;
}
.fact-value {
  color: #d1d4dc;
  font-family: monospace;
}
.fact-period {
  color: #787b86;
}
.fact-evidence {
  font-size: 11px;
  color: #5470c6;
  margin-top: 2px;
}

/* 表格通用 */
.financial-table table,
.history-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.financial-table th,
.financial-table td,
.history-table th,
.history-table td {
  padding: 6px 8px;
  text-align: left;
  border-bottom: 1px solid #1f2430;
}
.financial-table th,
.history-table th {
  color: #787b86;
  font-weight: 600;
  background: rgba(20, 30, 50, 0.3);
}
.financial-table td,
.history-table td {
  color: #d1d4dc;
}
.text-right {
  text-align: right;
}

/* 因果关系 */
.causal-grid,
.valuation-grid,
.gaps-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
@media (max-width: 1280px) {
  .causal-grid, .valuation-grid, .gaps-grid {
    grid-template-columns: 1fr;
  }
}
.causal-block,
.valuation-block,
.gaps-block {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 10px;
}
.block-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  color: #d1d4dc;
  font-weight: 600;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid #2a2e39;
}
.count {
  font-size: 11px;
  color: #787b86;
  font-family: monospace;
}
.block-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.block-empty {
  color: #787b86;
  font-size: 12px;
  text-align: center;
  padding: 12px 0;
}
.causal-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #d1d4dc;
  flex-wrap: wrap;
}
.causal-head { color: #5470c6; }
.causal-rel { color: #787b86; font-style: italic; }
.causal-tail { color: #d1d4dc; }

/* 估值/技术 KV */
.valuation-body,
.technical-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 8px;
}
.kv-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 4px 0;
}
.kv-label {
  font-size: 11px;
  color: #787b86;
}
.kv-value {
  font-size: 13px;
  color: #d1d4dc;
  font-family: monospace;
}
.kv-hint {
  font-size: 10px;
  color: #787b86;
  margin-left: 4px;
}

/* 技术状态警告 */
.warning-tag {
  font-size: 11px;
  padding: 2px 6px;
  background: rgba(250, 173, 20, 0.18);
  color: #faad14;
  border-radius: 3px;
  font-weight: 500;
  margin-left: 4px;
}
.technical-disclaimer {
  margin-top: 10px;
  font-size: 11px;
  color: #787b86;
  background: rgba(120, 123, 134, 0.08);
  padding: 6px 10px;
  border-radius: 4px;
}

/* 缺口标签 */
.gap-tag {
  font-size: 11px;
  padding: 3px 8px;
  background: rgba(255, 77, 79, 0.1);
  color: #ffa39e;
  border-radius: 3px;
  border: 1px solid rgba(255, 77, 79, 0.2);
  margin-bottom: 4px;
  display: inline-block;
}

.source-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #d1d4dc;
  padding: 4px 0;
  border-bottom: 1px solid #1f2430;
}
.source-item:last-child {
  border-bottom: none;
}
.source-name {
  flex: 1;
  color: #5470c6;
  font-weight: 500;
}
.source-count {
  font-size: 11px;
  color: #787b86;
  font-family: monospace;
}

/* 历史运行 */
.history-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.card-empty {
  color: #787b86;
  font-size: 13px;
  text-align: center;
  padding: 16px 0;
}

/* diff */
.diff-result {
  margin-top: 16px;
  background: rgba(20, 30, 50, 0.5);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 12px;
}
.diff-title {
  font-size: 13px;
  color: #fff;
  font-weight: 600;
  margin-bottom: 8px;
  font-family: monospace;
}
.diff-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}
@media (max-width: 1280px) {
  .diff-grid {
    grid-template-columns: 1fr;
  }
}
.diff-block {
  background: rgba(255, 255, 255, 0.02);
  border-radius: 4px;
  padding: 8px;
}
.diff-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.diff-item {
  font-size: 12px;
  font-family: monospace;
  color: #d1d4dc;
  padding: 2px 0;
}
.diff-item.add { color: #52c41a; }
.diff-item.rem { color: #ff4d4f; }

/* ============================================================ */
/* P1.9 CFA 报告样式 */
/* ============================================================ */
.cfa-report-section {
  margin-top: 24px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 8px;
  padding: 16px;
}
.cfa-report-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 12px;
}
.cfa-report-header .header-title {
  display: flex;
  align-items: center;
  gap: 12px;
}
.cfa-report-header .header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cfa-report-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cfa-meta-bar {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 10px;
}
.cfa-notice-banner {
  background: rgba(84, 112, 198, 0.1);
  border: 1px solid rgba(84, 112, 198, 0.3);
  border-radius: 6px;
  padding: 8px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: #5470c6;
  font-size: 12px;
}
.cfa-sections {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cfa-section-card {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 12px;
}
.cfa-section-card.status-missing {
  opacity: 0.7;
  border-style: dashed;
}
.cfa-section-card.status-partial {
  border-color: rgba(250, 173, 20, 0.3);
}
.cfa-section-card.status-ok {
  border-color: rgba(82, 196, 26, 0.3);
}
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid #2a2e39;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #d1d4dc;
  font-size: 14px;
  font-weight: 600;
}
.section-title i {
  color: #5470c6;
}
.section-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.section-empty {
  color: #787b86;
  font-size: 12px;
  padding: 16px 0;
  text-align: center;
}
.empty-reason {
  font-size: 11px;
  color: #787b86;
  margin-left: 4px;
}
.section-note {
  font-size: 11px;
  color: #787b86;
  background: rgba(120, 123, 134, 0.08);
  padding: 6px 8px;
  border-radius: 4px;
  line-height: 1.5;
}
.kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 8px;
  padding: 6px 0;
}
.sub-block {
  background: rgba(255, 255, 255, 0.02);
  border-radius: 4px;
  padding: 8px;
}
.sub-title {
  font-size: 12px;
  color: #d1d4dc;
  font-weight: 600;
  margin-bottom: 6px;
}
.sub-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.sub-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #d1d4dc;
  padding: 3px 0;
}
.sub-item.warning .sub-item-name {
  color: #faad14;
}
.sub-item-name {
  flex: 1;
}
.sub-item-value {
  color: #d1d4dc;
  font-family: monospace;
  font-size: 11px;
}
.source-tag {
  font-size: 11px;
  font-family: monospace;
  color: #5470c6;
  background: rgba(84, 112, 198, 0.1);
  padding: 2px 6px;
  border-radius: 3px;
  border: 1px solid rgba(84, 112, 198, 0.2);
  margin-bottom: 4px;
  display: inline-block;
}
.section-limitations {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  margin-top: 4px;
  padding-top: 6px;
  border-top: 1px dashed #1f2430;
}
.lim-label {
  font-size: 11px;
  color: #faad14;
  font-weight: 600;
}
.lim-tag {
  font-size: 10px;
  padding: 2px 6px;
  background: rgba(250, 173, 20, 0.1);
  color: #faad14;
  border-radius: 3px;
  border: 1px solid rgba(250, 173, 20, 0.2);
}
</style>
