<template>
  <div class="strategy-workflow">
    <!-- 顶部进度条 -->
    <header class="workflow-header">
      <div class="workflow-title">
        <span class="workflow-title__icon" aria-hidden="true">AI</span>
        <h2 class="workflow-title__text">策略生成工作流</h2>
        <span class="workflow-title__hint">描述 → 生成 → 保存 → 热重载 → 干跑 → 回测</span>
      </div>
      <div class="workflow-progress" role="progressbar" :aria-valuenow="progressPercent" aria-valuemin="0" aria-valuemax="100">
        <div class="workflow-progress__bar" :style="{ width: progressPercent + '%' }"></div>
        <span class="workflow-progress__label">{{ passedCount }} / {{ steps.length }} 步通过</span>
      </div>
    </header>

    <!-- 主体: 8 步 stepper 卡片 -->
    <ol class="workflow-steps" aria-label="AI 策略生成工作流步骤">
      <li
        v-for="(step, idx) in steps"
        :key="step.key"
        class="step-card"
        :class="['step-card--' + step.status, { 'is-active': currentStepKey === step.key }]"
      >
        <!-- stepper 连接线 (非最后一步) -->
        <span
          v-if="idx < steps.length - 1"
          class="step-connector"
          :class="['step-connector--' + stepConnectorStatus(idx)]"
          aria-hidden="true"
        ></span>

        <div class="step-card__head">
          <span class="step-card__index" :class="['step-card__index--' + step.status]" aria-hidden="true">
            {{ stepStatusIcon(step) }}
          </span>
          <div class="step-card__title">
            <strong>Step {{ idx + 1 }} · {{ step.title }}</strong>
            <small class="step-card__sub">{{ step.subtitle }}</small>
          </div>
          <span class="step-card__tag" :class="['step-card__tag--' + step.status]">
            {{ stepStatusLabel(step) }}
          </span>
        </div>

        <div class="step-card__body">
          <!-- Step 1: 描述输入 -->
          <div v-if="step.key === 'describe'" class="step-section">
            <div class="form-row">
              <label class="form-label" for="strategy-name">策略名称</label>
              <input
                id="strategy-name"
                v-model="form.strategyName"
                type="text"
                placeholder="例如：AI 双均线策略"
                class="form-input"
                :disabled="busy"
              />
            </div>
            <div class="form-row">
              <label class="form-label" for="strategy-desc">策略描述（自然语言）</label>
              <textarea
                id="strategy-desc"
                v-model="form.description"
                rows="5"
                placeholder="例如：5 日均线上穿 20 日均线买入, 下穿卖出, RSI > 70 卖出, 持仓不超过 5 天"
                class="form-textarea"
                :disabled="busy"
              ></textarea>
            </div>
            <div class="form-row">
              <label class="form-label" for="strategy-tags">标签（逗号分隔, 可选）</label>
              <input
                id="strategy-tags"
                v-model="form.tagsText"
                type="text"
                placeholder="AI, 均线, 短线"
                class="form-input"
                :disabled="busy"
              />
            </div>
            <div class="step-actions">
              <button
                class="btn btn--primary"
                :disabled="!canDescribe || busy"
                @click="markDescribeDone"
              >
                确认描述
              </button>
              <button
                class="btn btn--ghost"
                :disabled="busy"
                @click="resetForm"
              >
                清空
              </button>
            </div>
          </div>

          <!-- Step 2: API 连接状态 -->
          <div v-else-if="step.key === 'api'" class="step-section">
            <div class="api-status">
              <div class="api-status__row">
                <span class="api-status__label">baseURL</span>
                <code class="api-status__value">{{ apiState.baseURL }}</code>
              </div>
              <div class="api-status__row">
                <span class="api-status__label">连通性</span>
                <span class="api-status__value" :class="['tag', 'tag--' + apiState.connectivity]">
                  {{ apiState.connectivityLabel }}
                </span>
              </div>
              <div class="api-status__row" v-if="apiState.lastCheckedAt">
                <span class="api-status__label">最近检测</span>
                <span class="api-status__value">{{ apiState.lastCheckedAt }}</span>
              </div>
            </div>
            <div v-if="step.error" class="step-error">
              <ErrorState
                title="API 不可达"
                :message="step.error.message"
                :detail="step.error.detail"
                :endpoint="step.error.endpoint"
                :status="step.error.status"
                :code="step.error.code"
                :retryable="step.error.retryable"
                :last-request-time="step.error.lastRequestTime"
                @retry="checkApiConnection(true)"
              />
            </div>
            <div v-else-if="step.status === 'pending'" class="step-pending">
              <EmptyState
                title="尚未连通"
                description="点击下方按钮进行连通性检测"
                hint="AI 策略生成依赖后端 LLM 服务, 请确认 .env 中 LLM_API_BASE 已配置"
              />
            </div>
            <div class="step-actions">
              <button class="btn btn--primary" :disabled="busy" @click="checkApiConnection(true)">
                检测连通性
              </button>
              <button class="btn btn--ghost" :disabled="busy" @click="markApiDone">
                跳过 (使用 fallback)
              </button>
            </div>
          </div>

          <!-- Step 3: 生成策略 -->
          <div v-else-if="step.key === 'generate'" class="step-section">
            <div v-if="step.status === 'pending'">
              <EmptyState
                title="尚未生成"
                description="点击下方按钮调用 LLM 生成策略代码"
                hint="LLM 不可用时将自动使用本地模板策略"
              />
            </div>
            <div v-else-if="step.status === 'loading'">
              <LoadingState
                title="正在生成策略"
                :description="step.message || 'LLM 正在生成代码, 请稍候...'"
                :elapsed-ms="step.elapsedMs"
                :timeout-ms="60000"
              />
            </div>
            <div v-else-if="step.status === 'success'" class="step-result">
              <div class="result-grid">
                <div class="result-item">
                  <span class="result-item__label">filename</span>
                  <code class="result-item__value">{{ receipt.filename || '—' }}</code>
                </div>
                <div class="result-item">
                  <span class="result-item__label">strategy_id</span>
                  <code class="result-item__value">{{ receipt.strategy_id || '—' }}</code>
                </div>
                <div class="result-item">
                  <span class="result-item__label">fallback</span>
                  <span class="result-item__value tag" :class="receipt.fallback ? 'tag--warning' : 'tag--success'">
                    {{ receipt.fallback ? '本地模板' : 'LLM 生成' }}
                  </span>
                </div>
                <div class="result-item">
                  <span class="result-item__label">reload_ok</span>
                  <span class="result-item__value tag" :class="boolTagClass(receipt.reload_ok)">
                    {{ formatBool(receipt.reload_ok) }}
                  </span>
                </div>
                <div class="result-item">
                  <span class="result-item__label">import_ok</span>
                  <span class="result-item__value tag" :class="boolTagClass(receipt.import_ok)">
                    {{ formatBool(receipt.import_ok) }}
                  </span>
                </div>
                <div class="result-item">
                  <span class="result-item__label">smoke_ok</span>
                  <span class="result-item__value tag" :class="boolTagClass(receipt.smoke_ok)">
                    {{ formatBool(receipt.smoke_ok) }}
                  </span>
                </div>
              </div>
              <!-- 代码预览: 使用 LightCodeEditor (轻量, 不引入 monaco) -->
              <div v-if="receipt.code" class="code-preview">
                <header class="code-preview__head">
                  <strong>代码预览</strong>
                  <div class="code-preview__head-actions">
                    <button
                      type="button"
                      class="code-preview__toggle"
                      @click="codePreviewExpanded = !codePreviewExpanded"
                    >
                      {{ codePreviewExpanded ? '收起' : '展开' }}
                    </button>
                    <span class="code-preview__meta">
                      {{ codeLineCount }} 行
                      <span v-if="receipt.errorLine" class="code-preview__err">L{{ receipt.errorLine }} 异常</span>
                    </span>
                  </div>
                </header>
                <LightCodeEditor
                  v-show="codePreviewExpanded"
                  v-model="receipt.code"
                  language="python"
                  :error-line="receipt.errorLine"
                  :errors="receipt.errors"
                  :max-body-height="codePreviewExpanded ? '420px' : '0px'"
                  :readonly="false"
                  @save="onEditorSave"
                />
              </div>
            </div>
            <div v-else-if="step.status === 'error' && step.error" class="step-error">
              <ErrorState
                title="生成失败"
                :message="step.error.message"
                :detail="step.error.detail"
                :endpoint="step.error.endpoint"
                :status="step.error.status"
                :code="step.error.code"
                :retryable="step.error.retryable"
                :last-request-time="step.error.lastRequestTime"
                @retry="runGenerate(true)"
              />
            </div>
            <div class="step-actions">
              <button class="btn btn--primary" :disabled="busy" @click="runGenerate(false)">
                调用 LLM 生成
              </button>
              <button class="btn btn--secondary" :disabled="busy" @click="runGenerate(true)">
                强制 fallback
              </button>
            </div>
          </div>

          <!-- Step 4: 保存策略 -->
          <div v-else-if="step.key === 'save'" class="step-section">
            <div v-if="step.status === 'pending'">
              <EmptyState
                title="等待生成完成"
                description="请先完成 Step 3 生成策略"
                hint="本步骤会写入 core/strategies/user/ 目录"
              />
            </div>
            <div v-else-if="step.status === 'loading'">
              <LoadingState title="正在保存策略" :elapsed-ms="step.elapsedMs" :timeout-ms="15000" />
            </div>
            <div v-else-if="step.status === 'success'" class="step-result">
              <div class="result-grid">
                <div class="result-item">
                  <span class="result-item__label">filename</span>
                  <code class="result-item__value">{{ receipt.filename || '—' }}</code>
                </div>
                <div class="result-item result-item--full">
                  <span class="result-item__label">filepath</span>
                  <code class="result-item__value">{{ receipt.filepath || '—' }}</code>
                </div>
              </div>
              <p v-if="step.message" class="step-message">{{ step.message }}</p>
            </div>
            <div v-else-if="step.status === 'error' && step.error" class="step-error">
              <ErrorState
                title="保存失败"
                :message="step.error.message"
                :detail="step.error.detail"
                :endpoint="step.error.endpoint"
                :status="step.error.status"
                :code="step.error.code"
                :retryable="step.error.retryable"
                :last-request-time="step.error.lastRequestTime"
                @retry="runSave"
              />
            </div>
            <div class="step-actions">
              <button class="btn btn--primary" :disabled="busy" @click="runSave">
                保存到 user 目录
              </button>
            </div>
          </div>

          <!-- Step 5: 热重载 -->
          <div v-else-if="step.key === 'reload'" class="step-section">
            <div v-if="step.status === 'pending'">
              <EmptyState
                title="等待保存完成"
                description="请先完成 Step 4 保存策略"
                hint="热重载会重新发现策略并刷新工厂"
              />
            </div>
            <div v-else-if="step.status === 'loading'">
              <LoadingState title="正在热重载" :elapsed-ms="step.elapsedMs" :timeout-ms="15000" />
            </div>
            <div v-else-if="step.status === 'success'" class="step-result">
              <div class="result-grid">
                <div class="result-item">
                  <span class="result-item__label">reload_ok</span>
                  <span class="result-item__value tag" :class="boolTagClass(receipt.reload_ok)">
                    {{ formatBool(receipt.reload_ok) }}
                  </span>
                </div>
                <div class="result-item">
                  <span class="result-item__label">已发现策略</span>
                  <span class="result-item__value">{{ receipt.strategiesCount ?? 0 }} 个</span>
                </div>
              </div>
              <ul v-if="receipt.strategiesList && receipt.strategiesList.length" class="reload-list">
                <li v-for="(s, i) in receipt.strategiesList.slice(0, 5)" :key="i">{{ s }}</li>
                <li v-if="receipt.strategiesList.length > 5" class="reload-list__more">
                  ... 等 {{ receipt.strategiesList.length }} 个
                </li>
              </ul>
              <p v-if="step.message" class="step-message">{{ step.message }}</p>
            </div>
            <div v-else-if="step.status === 'error' && step.error" class="step-error">
              <ErrorState
                title="热重载失败"
                :message="step.error.message"
                :detail="step.error.detail"
                :endpoint="step.error.endpoint"
                :status="step.error.status"
                :code="step.error.code"
                :retryable="step.error.retryable"
                :last-request-time="step.error.lastRequestTime"
                @retry="runReload"
              />
            </div>
            <div class="step-actions">
              <button class="btn btn--primary" :disabled="busy" @click="runReload">
                热重载策略
              </button>
            </div>
          </div>

          <!-- Step 6: dry-run -->
          <div v-else-if="step.key === 'dryrun'" class="step-section">
            <div v-if="step.status === 'pending'">
              <EmptyState
                title="等待热重载"
                description="请先完成 Step 5 热重载"
                hint="dry-run 验证策略文件能正常 import 并找到 StrategyBase 子类"
              />
            </div>
            <div v-else-if="step.status === 'loading'">
              <LoadingState title="正在执行 dry-run" :elapsed-ms="step.elapsedMs" :timeout-ms="15000" />
            </div>
            <div v-else-if="step.status === 'success'" class="step-result">
              <div class="result-grid">
                <div class="result-item">
                  <span class="result-item__label">import_ok</span>
                  <span class="result-item__value tag" :class="boolTagClass(receipt.import_ok)">
                    {{ formatBool(receipt.import_ok) }}
                  </span>
                </div>
                <div class="result-item">
                  <span class="result-item__label">smoke_ok</span>
                  <span class="result-item__value tag" :class="boolTagClass(receipt.smoke_ok)">
                    {{ formatBool(receipt.smoke_ok) }}
                  </span>
                </div>
              </div>
              <p v-if="receipt.import_error" class="step-message step-message--error">
                import 错误: {{ receipt.import_error }}
              </p>
              <p v-if="receipt.smoke_error" class="step-message step-message--error">
                smoke 错误: {{ receipt.smoke_error }}
              </p>
              <p v-else-if="step.message" class="step-message">{{ step.message }}</p>
            </div>
            <div v-else-if="step.status === 'error' && step.error" class="step-error">
              <ErrorState
                title="dry-run 失败"
                :message="step.error.message"
                :detail="step.error.detail"
                :endpoint="step.error.endpoint"
                :status="step.error.status"
                :code="step.error.code"
                :retryable="step.error.retryable"
                :last-request-time="step.error.lastRequestTime"
                @retry="runSmoke"
              />
            </div>
            <div class="step-actions">
              <button class="btn btn--primary" :disabled="busy" @click="runSmoke">
                执行 dry-run
              </button>
            </div>
          </div>

          <!-- Step 7: 回测 -->
          <div v-else-if="step.key === 'backtest'" class="step-section">
            <div v-if="step.status === 'pending'">
              <EmptyState
                title="等待 dry-run"
                description="请先完成 Step 6 干跑"
                hint="回测会调用 POST /api/backtest/run, 默认 30s 超时, 长任务自动切到 job 模式"
              />
            </div>
            <div v-else-if="step.status === 'loading'">
              <LoadingState
                :title="receipt.backtestStatus === 'pending' ? '回测排队中' : '正在运行回测'"
                :description="receipt.backtestMessage || '回测计算中, 请稍候...'"
                :elapsed-ms="step.elapsedMs"
                :timeout-ms="35000"
              />
              <div v-if="receipt.backtestProgress > 0" class="step-progress">
                <div class="step-progress__bar" :style="{ width: receipt.backtestProgress + '%' }"></div>
                <span class="step-progress__label">进度 {{ receipt.backtestProgress }}%</span>
              </div>
              <div v-if="receipt.backtestJobId" class="step-meta">
                <span class="step-meta__label">job_id</span>
                <code class="step-meta__value">{{ receipt.backtestJobId }}</code>
              </div>
            </div>
            <div v-else-if="step.status === 'timeout'">
              <TimeoutState
                title="回测请求超时"
                :endpoint="receipt.backtestEndpoint || '/api/backtest/run'"
                :elapsed-ms="step.elapsedMs"
                @retry="runBacktest"
              />
              <div v-if="receipt.backtestJobId" class="step-meta">
                <span class="step-meta__label">后台 job_id (可继续轮询)</span>
                <code class="step-meta__value">{{ receipt.backtestJobId }}</code>
              </div>
            </div>
            <div v-else-if="step.status === 'success'" class="step-result">
              <div class="result-grid">
                <div class="result-item">
                  <span class="result-item__label">status</span>
                  <span class="result-item__value tag" :class="backtestStatusTagClass">
                    {{ receipt.backtestStatus || 'completed' }}
                  </span>
                </div>
                <div class="result-item">
                  <span class="result-item__label">job_id</span>
                  <code class="result-item__value">{{ receipt.backtestJobId || '—' }}</code>
                </div>
                <div class="result-item">
                  <span class="result-item__label">elapsed_ms</span>
                  <code class="result-item__value">{{ receipt.backtestElapsedMs ?? '—' }}</code>
                </div>
                <div v-if="receipt.backtestStats" class="result-item">
                  <span class="result-item__label">总收益</span>
                  <code class="result-item__value">{{ formatPercent(receipt.backtestStats.totalReturn) }}</code>
                </div>
                <div v-if="receipt.backtestStats" class="result-item">
                  <span class="result-item__label">最大回撤</span>
                  <code class="result-item__value">{{ formatPercent(receipt.backtestStats.maxDrawdown) }}</code>
                </div>
                <div v-if="receipt.backtestStats" class="result-item">
                  <span class="result-item__label">夏普</span>
                  <code class="result-item__value">{{ formatFraction(receipt.backtestStats.sharpeRatio) }}</code>
                </div>
                <div v-if="receipt.backtestStats" class="result-item">
                  <span class="result-item__label">胜率</span>
                  <code class="result-item__value">{{ formatPercent(receipt.backtestStats.winRate) }}</code>
                </div>
              </div>
              <p v-if="step.message" class="step-message">{{ step.message }}</p>
              <!-- 收益曲线 (ECharts 简化版) -->
              <div v-if="hasEquityCurve" class="equity-chart-wrap">
                <header class="equity-chart-wrap__head">
                  <strong>收益曲线</strong>
                  <span class="equity-chart-wrap__hint">{{ equityPoints.length }} 个点</span>
                </header>
                <div ref="equityChartRef" class="equity-chart" aria-label="回测收益曲线"></div>
              </div>
              <div v-else class="step-message step-message--info">
                收益曲线数据缺失 (equity_curve 为空), 但统计指标已返回
              </div>
            </div>
            <div v-else-if="step.status === 'error' && step.error" class="step-error">
              <ErrorState
                :title="backtestErrorTitle"
                :message="step.error.message"
                :detail="backtestErrorDetail"
                :endpoint="step.error.endpoint"
                :status="step.error.status"
                :code="step.error.code"
                :retryable="step.error.retryable"
                :last-request-time="step.error.lastRequestTime"
                @retry="runBacktest"
              />
              <p v-if="backtestSuggestion" class="step-suggestion">
                <strong>建议:</strong> {{ backtestSuggestion }}
              </p>
            </div>
            <div class="step-actions">
              <button class="btn btn--primary" :disabled="busy" @click="runBacktest">
                {{ receipt.backtestJobId ? '重新运行回测' : '运行回测' }}
              </button>
            </div>
            <!-- T2.5 Debug 按钮: 显示 endpoint / status / code / elapsed_ms -->
            <DebugButton
              v-if="debugInfoVisible"
              :data-date="receipt.backtestCompletedAt"
              :demo-mode="false"
              :stale="receipt.backtestStatus === 'failed'"
              :partial-success="receipt.backtestStatus === 'success' && !hasEquityCurve"
            />
          </div>

          <!-- Step 8: 错误诊断 (独立卡片) -->
          <div v-else-if="step.key === 'diagnose'" class="step-section">
            <div v-if="failedSteps.length === 0">
              <EmptyState
                title="暂无失败"
                description="所有步骤均通过, 无需诊断"
                hint="如需重置, 请点击下方按钮"
              />
            </div>
            <ul v-else class="diagnose-list">
              <li v-for="fs in failedSteps" :key="fs.key" class="diagnose-item">
                <header class="diagnose-item__head">
                  <span class="diagnose-item__step">Step {{ stepIndexOf(fs.key) + 1 }} · {{ fs.title }}</span>
                  <span class="diagnose-item__endpoint" v-if="fs.error?.endpoint">
                    <code>{{ fs.error.endpoint }}</code>
                  </span>
                </header>
                <p class="diagnose-item__cause">
                  <strong>原因:</strong> {{ fs.error?.message || '未知错误' }}
                </p>
                <p class="diagnose-item__suggest">
                  <strong>建议:</strong> {{ suggestFix(fs) }}
                </p>
              </li>
            </ul>
            <div class="step-actions">
              <button class="btn btn--ghost" :disabled="busy" @click="resetAll">
                重置工作流
              </button>
            </div>
          </div>
        </div>
      </li>
    </ol>

    <!-- 右下角: 生成回执 (固定展示) -->
    <aside v-if="hasAnyReceipt" class="receipt-card" aria-label="策略生成回执">
      <header class="receipt-card__head">
        <strong>生成回执</strong>
        <span class="receipt-card__hint">{{ receipt.strategy_id || '尚未生成' }}</span>
      </header>
      <dl class="receipt-card__list">
        <div class="receipt-card__row">
          <dt>filename</dt>
          <dd><code>{{ receipt.filename || '—' }}</code></dd>
        </div>
        <div class="receipt-card__row">
          <dt>filepath</dt>
          <dd><code class="receipt-card__path">{{ receipt.filepath || '—' }}</code></dd>
        </div>
        <div class="receipt-card__row">
          <dt>strategy_id</dt>
          <dd><code>{{ receipt.strategy_id || '—' }}</code></dd>
        </div>
        <div class="receipt-card__row">
          <dt>reload_ok</dt>
          <dd>
            <span class="tag" :class="boolTagClass(receipt.reload_ok)">
              {{ formatBool(receipt.reload_ok) }}
            </span>
          </dd>
        </div>
        <div class="receipt-card__row">
          <dt>import_ok</dt>
          <dd>
            <span class="tag" :class="boolTagClass(receipt.import_ok)">
              {{ formatBool(receipt.import_ok) }}
            </span>
          </dd>
        </div>
        <div class="receipt-card__row">
          <dt>smoke_ok</dt>
          <dd>
            <span class="tag" :class="boolTagClass(receipt.smoke_ok)">
              {{ formatBool(receipt.smoke_ok) }}
            </span>
          </dd>
        </div>
        <div v-if="receipt.errorReason" class="receipt-card__row receipt-card__row--error">
          <dt>错误原因</dt>
          <dd>{{ receipt.errorReason }}</dd>
        </div>
      </dl>
    </aside>
  </div>
</template>

<script setup lang="ts">
/**
 * StrategyGenerator - 8 步 AI 策略生成工作流卡片
 *
 * 工作流步骤:
 *  1. describe  - 策略描述输入 (名称 / 自然语言 / 标签)
 *  2. api       - 后端 API 连通性检测
 *  3. generate  - 调用 LLM 生成策略 (失败时自动 fallback 到本地模板)
 *  4. save      - 保存策略到 user 目录
 *  5. reload    - 热重载策略工厂
 *  6. dryrun    - 干跑 (importlib + 继承链验证)
 *  7. backtest  - 调 /api/run_backtest 跑一次最小回测
 *  8. diagnose  - 错误诊断 + 修复建议 (独立卡片)
 *
 * 设计原则:
 *  - DRY: 状态/动作集中在 workflowStore, 模板只做映射
 *  - KISS: 每步卡片独立, 状态机扁平 (pending/loading/success/error)
 *  - 防御性: 字段缺失一律降级, 不抛错
 *  - 字段缺失降级: 详见 receipts.receipt 初始值
 */
import { computed, markRaw, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { apiService, ApiError } from '@/services/api'
import ErrorState from '@/components/common/ErrorState.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingState from '@/components/common/LoadingState.vue'
import TimeoutState from '@/components/common/TimeoutState.vue'
import DebugButton from '@/components/common/DebugButton.vue'
import * as echarts from 'echarts'

defineOptions({ name: 'StrategyGenerator' })

/** 步骤状态 */
export type StepStatus = 'pending' | 'loading' | 'success' | 'error' | 'timeout'

/** 步骤定义 */
export interface WorkflowStep {
  key: 'describe' | 'api' | 'generate' | 'save' | 'reload' | 'dryrun' | 'backtest' | 'diagnose'
  title: string
  subtitle: string
  status: StepStatus
  message?: string
  error?: StepError | null
  elapsedMs?: number
}

export interface StepError {
  message: string
  detail?: string
  endpoint?: string
  status?: number | string
  code?: string
  retryable: boolean
  lastRequestTime?: string
}

/** 表单 */
interface FormState {
  strategyName: string
  description: string
  tagsText: string
}

/** 回执 - 字段缺失一律降级为 undefined/null */
interface Receipt {
  filename: string | null
  filepath: string | null
  strategy_id: string | null
  fallback: boolean
  reload_ok: boolean | null
  import_ok: boolean | null
  smoke_ok: boolean | null
  import_error: string | null
  smoke_error: string | null
  strategiesList: string[] | null
  strategiesCount: number | null
  // T2.5: 真实回测入口 - 新增字段 (字段缺失一律降级)
  backtestStatus: string | null
  backtestJobId: string | null
  backtestMode: 'sync' | 'async' | null
  backtestProgress: number
  backtestMessage: string
  backtestEndpoint: string
  backtestElapsedMs: number | null
  backtestStats: Record<string, any> | null
  backtestEquityCurve: Array<{ date: string; equity: number }> | null
  backtestStartedAt: string | null
  backtestCompletedAt: string | null
  errorReason: string | null
  /** LLM/模板返回的策略源码 (用于 LightCodeEditor 预览) */
  code: string
  /** smoke 失败时的行号 (1-based) */
  errorLine: number | null
  /** 错误列表 (联动 LightCodeEditor gutter) */
  errors: Array<{ line: number; message: string; severity?: 'error' | 'warning' | 'info' }>
}

// ============================================
// 状态
// ============================================
const form = reactive<FormState>({
  strategyName: 'AI策略',
  description: '',
  tagsText: '',
})

const receipt = reactive<Receipt>({
  filename: null,
  filepath: null,
  strategy_id: null,
  fallback: false,
  reload_ok: null,
  import_ok: null,
  smoke_ok: null,
  import_error: null,
  smoke_error: null,
  strategiesList: null,
  strategiesCount: null,
  // T2.5 真实回测入口
  backtestStatus: null,
  backtestJobId: null,
  backtestMode: null,
  backtestProgress: 0,
  backtestMessage: '',
  backtestEndpoint: '/api/backtest/run',
  backtestElapsedMs: null,
  backtestStats: null,
  backtestEquityCurve: null,
  backtestStartedAt: null,
  backtestCompletedAt: null,
  errorReason: null,
  code: '',
  errorLine: null,
  errors: [],
})

const apiState = reactive({
  baseURL: '/api',
  connectivity: 'unknown' as 'ok' | 'fail' | 'unknown',
  connectivityLabel: '未检测',
  lastCheckedAt: '',
})

const currentStepKey = ref<WorkflowStep['key'] | null>(null)
const elapsedTimers: Record<string, ReturnType<typeof setInterval>> = {}
let elapsedStartAt = 0

const steps = reactive<WorkflowStep[]>([
  { key: 'describe', title: '描述策略', subtitle: '自然语言 + 策略名 + 标签', status: 'pending' },
  { key: 'api', title: 'API 连接状态', subtitle: '检测后端 / LLM 服务可达性', status: 'pending' },
  { key: 'generate', title: '生成策略', subtitle: '调 LLM 生成, 失败自动 fallback', status: 'pending' },
  { key: 'save', title: '保存策略', subtitle: '写入 core/strategies/user/', status: 'pending' },
  { key: 'reload', title: '热重载', subtitle: '刷新策略工厂, 重新发现模块', status: 'pending' },
  { key: 'dryrun', title: '干跑 (Dry-run)', subtitle: 'importlib 加载 + 继承链验证', status: 'pending' },
  { key: 'backtest', title: '回测', subtitle: '调 /api/run_backtest smoke 测试', status: 'pending' },
  { key: 'diagnose', title: '错误诊断', subtitle: '列失败原因 + 修复建议', status: 'pending' },
])

// ============================================
// 计算属性
// ============================================
const busy = computed(() => steps.some((s) => s.status === 'loading'))
const passedCount = computed(
  () => steps.filter((s) => s.status === 'success' && s.key !== 'diagnose').length
)
const progressPercent = computed(() => {
  const denom = steps.length - 1 // 排除 diagnose 自身
  if (denom <= 0) return 0
  return Math.round((passedCount.value / denom) * 100)
})
const canDescribe = computed(
  () => form.strategyName.trim().length > 0 && form.description.trim().length > 0
)
const failedSteps = computed(() =>
  steps.filter((s) => s.status === 'error' && s.key !== 'diagnose')
)
const hasAnyReceipt = computed(
  () =>
    !!(
      receipt.filename ||
      receipt.strategy_id ||
      receipt.filepath ||
      steps.some((s) => s.status === 'success' || s.status === 'error')
    )
)
const backtestStatusTagClass = computed(() => {
  const s = (receipt.backtestStatus || '').toLowerCase()
  if (s === 'completed' || s === 'success' || s === 'finished') return 'tag--success'
  if (s === 'failed' || s === 'error') return 'tag--error'
  if (s === 'pending' || s === 'running' || s === 'queued') return 'tag--warning'
  return 'tag--info'
})

// ============================================
// T2.5: 真实回测入口 - 状态/计算属性
// ============================================

/** ECharts 实例 (markRaw 防止 Vue 深度代理) */
let equityChartInstance: echarts.ECharts | null = null
const equityChartRef = ref<HTMLDivElement | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null
const pollRetries = ref(0)

/** 归一化收益曲线点 (字段缺失一律降级) */
const equityPoints = computed<Array<{ date: string; equity: number }>>(() => {
  const raw = receipt.backtestEquityCurve
  if (!Array.isArray(raw)) return []
  const out: Array<{ date: string; equity: number }> = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const date = typeof item.date === 'string' ? item.date : ''
    const equity = Number(item.equity)
    if (!date || !Number.isFinite(equity)) continue
    out.push({ date, equity })
  }
  return out
})

/** 是否有可绘制的收益曲线 */
const hasEquityCurve = computed(() => equityPoints.value.length >= 2)

/** 失败时根据 code 给出具体建议 (8 类错误码) */
const backtestErrorTitle = computed(() => {
  const code = (steps.find((s) => s.key === 'backtest')?.error?.code || '').toUpperCase()
  const map: Record<string, string> = {
    BACKTEST_STRATEGY_NOT_REGISTERED: '策略未注册',
    BACKTEST_STRATEGY_LOAD_FAILED: '策略加载失败',
    BACKTEST_MISSING_PARAMETERS: '缺少必要参数',
    BACKTEST_MISSING_DATA_FIELDS: '数据字段缺失',
    BACKTEST_DATA_QUERY_UNAVAILABLE: '数据查询不可用',
    BACKTEST_LANCEDB_NO_DATA: 'LanceDB 无数据',
    BACKTEST_MATRIX_CACHE_MISSING: 'matrix_cache 缺失',
    BACKTEST_RUNTIME_EXCEPTION: '回测运行时异常',
  }
  return map[code] || '回测失败'
})

/** 失败时 detail (后端 raw detail) */
const backtestErrorDetail = computed(() => {
  return steps.find((s) => s.key === 'backtest')?.error?.detail || ''
})

/** 失败时 suggestion (后端返回的 suggestion 字段) */
const backtestSuggestion = computed(() => {
  const code = (steps.find((s) => s.key === 'backtest')?.error?.code || '').toUpperCase()
  const localMap: Record<string, string> = {
    BACKTEST_STRATEGY_NOT_REGISTERED: '请回到 Step 5 重新触发热重载, 或在 Step 3 重新生成策略',
    BACKTEST_STRATEGY_LOAD_FAILED: '请检查策略文件 import 语法, 回到 Step 3 强制 fallback 使用本地模板',
    BACKTEST_MISSING_PARAMETERS: '请确认 strategy_id / start_date / end_date 三个参数都已提供',
    BACKTEST_MISSING_DATA_FIELDS: '请检查数据源或运行 /api/preload 预加载, 然后重试',
    BACKTEST_DATA_QUERY_UNAVAILABLE: '请检查后端日志, 可能 LanceDB / Parquet 缓存未就绪',
    BACKTEST_LANCEDB_NO_DATA: '请缩短 start_date / end_date 到已有数据区间, 或先补充数据',
    BACKTEST_MATRIX_CACHE_MISSING: '请先运行 /api/preload 预加载回测数据',
    BACKTEST_RUNTIME_EXCEPTION: '请查看后端 trace, 或尝试缩短回测区间',
  }
  return localMap[code] || '请重试, 或查看后端日志获取更多上下文'
})

/** Debug 按钮是否显示 (回测步骤发生过交互即显示) */
const debugInfoVisible = computed(() => {
  return Boolean(
    receipt.backtestJobId ||
    receipt.backtestStatus ||
    receipt.backtestStartedAt ||
    steps.find((s) => s.key === 'backtest')?.status === 'error' ||
    steps.find((s) => s.key === 'backtest')?.status === 'timeout'
  )
})

const codePreviewExpanded = ref(false)
const codeLineCount = computed(() => {
  const c = receipt.code || ''
  if (!c) return 0
  return c.split('\n').length
})

// ============================================
// 通用工具
// ============================================
function stepByKey(key: WorkflowStep['key']): WorkflowStep {
  const s = steps.find((x) => x.key === key)
  if (!s) throw new Error(`step not found: ${key}`)
  return s
}

function stepIndexOf(key: WorkflowStep['key']): number {
  return steps.findIndex((x) => x.key === key)
}

function setStepStatus(
  key: WorkflowStep['key'],
  status: StepStatus,
  payload?: { message?: string; error?: StepError | null; elapsedMs?: number }
): void {
  const s = stepByKey(key)
  s.status = status
  if (payload?.message !== undefined) s.message = payload.message
  if (payload?.error !== undefined) s.error = payload.error
  if (payload?.elapsedMs !== undefined) s.elapsedMs = payload.elapsedMs
  if (status === 'success') s.error = null
}

function startElapsedTimer(key: WorkflowStep['key']): void {
  stopElapsedTimer(key)
  elapsedStartAt = performance.now()
  setStepStatus(key, 'loading', { elapsedMs: 0 })
  elapsedTimers[key] = setInterval(() => {
    const step = stepByKey(key)
    step.elapsedMs = Math.round(performance.now() - elapsedStartAt)
  }, 200)
}

function stopElapsedTimer(key: WorkflowStep['key'], finalStatus: StepStatus): void {
  if (elapsedTimers[key]) {
    clearInterval(elapsedTimers[key])
    delete elapsedTimers[key]
  }
  setStepStatus(key, finalStatus, { elapsedMs: Math.round(performance.now() - elapsedStartAt) })
}

function captureError(err: unknown, endpoint: string): StepError {
  if (err instanceof ApiError) {
    return {
      message: err.message,
      detail: err.detail?.detail?.error || err.detail?.smoke_error || err.detail?.import_error,
      endpoint: err.endpoint || endpoint,
      status: err.status,
      code: err.code,
      retryable: err.retryable,
      lastRequestTime: new Date().toISOString(),
    }
  }
  if (err instanceof Error) {
    return {
      message: err.message,
      endpoint,
      retryable: false,
      lastRequestTime: new Date().toISOString(),
    }
  }
  return {
    message: '未知错误',
    endpoint,
    retryable: false,
    lastRequestTime: new Date().toISOString(),
  }
}

function formatBool(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value ? 'true' : 'false'
}

function boolTagClass(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return 'tag--unknown'
  return value ? 'tag--success' : 'tag--error'
}

function stepStatusIcon(step: WorkflowStep): string {
  if (step.status === 'success') return '✓'
  if (step.status === 'error') return '✕'
  if (step.status === 'loading') return '…'
  if (step.key === 'diagnose' && failedSteps.value.length > 0) return '!'
  return '·'
}

function stepStatusLabel(step: WorkflowStep): string {
  if (step.status === 'success') return '通过'
  if (step.status === 'error') return '失败'
  if (step.status === 'timeout') return '超时'
  if (step.status === 'loading') return '进行中'
  if (step.key === 'diagnose' && failedSteps.value.length > 0) {
    return `${failedSteps.value.length} 个问题`
  }
  return '待开始'
}

function stepConnectorStatus(idx: number): 'done' | 'active' | 'idle' {
  const s = steps[idx]
  if (s.status === 'success') return 'done'
  if (s.status === 'loading' || s.status === 'error') return 'active'
  return 'idle'
}

function suggestFix(step: WorkflowStep): string {
  const code = (step.error?.code || '').toUpperCase()
  if (code === 'TIMEOUT') return '检查网络/后端状态, 适当延长 timeout 后重试'
  if (code === 'NETWORK') return '确认后端进程已启动, 检查 Vite 代理配置'
  if (step.error?.status === 500) return '查看后端日志, 可能是 LLM 配置缺失或代码语法错误'
  if (step.error?.status === 404) return '策略文件可能已被删除, 重新生成后再试'
  if (step.key === 'generate') return '尝试点击"强制 fallback"使用本地模板策略'
  if (step.key === 'save') return '检查 core/strategies/user/ 目录写入权限'
  if (step.key === 'reload') return '手动触发 watch 失效, 检查 hot_reload 配置'
  if (step.key === 'dryrun') return '查看代码语法, 确认继承 AIStrategyBase / StrategyBase'
  if (step.key === 'backtest') return '缩短回测区间或检查 data_query 是否可用'
  return '请重试, 或查看 ApiDebugPanel 获取更多上下文'
}

/**
 * 从错误文本中解析行号 (兼容: "line 12", "line 12,", "Line: 12", "第 12 行")
 * 解析失败返回 null
 */
function parseErrorLine(text: string | null | undefined): number | null {
  if (!text) return null
  const m = String(text).match(/line\s*(\d+)|第\s*(\d+)\s*行/i)
  if (!m) return null
  const n = Number(m[1] || m[2])
  return Number.isFinite(n) && n >= 1 ? n : null
}

/**
 * LightCodeEditor 保存事件: 复用 runSave 走保存 API
 * 防御性: 没有 strategy_id 时不保存
 */
function onEditorSave(code: string): void {
  receipt.code = code || ''
  if (!receipt.strategy_id) {
    setStepStatus('generate', 'error', {
      error: {
        message: '未生成 strategy_id, 请先完成 Step 3',
        retryable: true,
        lastRequestTime: new Date().toISOString(),
      },
    })
    return
  }
  void runSave()
}

// ============================================
// 步骤动作
// ============================================
function markDescribeDone(): void {
  if (!canDescribe.value) return
  setStepStatus('describe', 'success', { message: '已确认描述' })
  currentStepKey.value = 'api'
}

function resetForm(): void {
  form.strategyName = 'AI策略'
  form.description = ''
  form.tagsText = ''
}

function resetAll(): void {
  // 清空回执
  receipt.filename = null
  receipt.filepath = null
  receipt.strategy_id = null
  receipt.fallback = false
  receipt.reload_ok = null
  receipt.import_ok = null
  receipt.smoke_ok = null
  receipt.import_error = null
  receipt.smoke_error = null
  receipt.strategiesList = null
  receipt.strategiesCount = null
  receipt.backtestStatus = null
  receipt.backtestJobId = null
  receipt.backtestMode = null
  receipt.backtestProgress = 0
  receipt.backtestMessage = ''
  receipt.backtestEndpoint = '/api/backtest/run'
  receipt.backtestElapsedMs = null
  receipt.backtestStats = null
  receipt.backtestEquityCurve = null
  receipt.backtestStartedAt = null
  receipt.backtestCompletedAt = null
  receipt.errorReason = null
  // 编辑器相关
  receipt.code = ''
  receipt.errorLine = null
  receipt.errors = []
  codePreviewExpanded.value = false
  // T2.5: 清理运行时资源
  stopPolling()
  disposeEquityChart()
  // 重置所有步骤
  for (const s of steps) {
    s.status = 'pending'
    s.error = null
    s.message = undefined
    s.elapsedMs = undefined
  }
  currentStepKey.value = null
}

async function checkApiConnection(force: boolean): Promise<void> {
  if (!force && apiState.connectivity === 'ok') {
    setStepStatus('api', 'success', { message: '已连通' })
    return
  }
  apiState.connectivity = 'unknown'
  apiState.connectivityLabel = '检测中…'
  apiState.lastCheckedAt = ''
  setStepStatus('api', 'loading', { error: null })
  startElapsedTimer('api')
  try {
    // 触发一次低开销接口 (data/health) 验证连通性
    const res = await apiService.getStrategies()
    apiState.connectivity = 'ok'
    apiState.connectivityLabel = '已连通'
    apiState.lastCheckedAt = new Date().toLocaleString('zh-CN', { hour12: false })
    void res // 仅用于触发请求
    stopElapsedTimer('api', 'success')
    setStepStatus('api', 'success', { message: '后端可达' })
    currentStepKey.value = 'generate'
  } catch (err) {
    apiState.connectivity = 'fail'
    apiState.connectivityLabel = '不可达'
    apiState.lastCheckedAt = new Date().toLocaleString('zh-CN', { hour12: false })
    const stepError = captureError(err, '/api/strategies')
    stopElapsedTimer('api', 'error')
    setStepStatus('api', 'error', { error: stepError })
    // 不阻断流程, 用户可点击"跳过"继续
  }
}

function markApiDone(): void {
  setStepStatus('api', 'success', { message: '已跳过, 启用 fallback' })
  currentStepKey.value = 'generate'
}

async function runGenerate(forceFallback: boolean): Promise<void> {
  if (form.description.trim().length === 0) {
    setStepStatus('describe', 'error', {
      error: {
        message: '请先填写策略描述',
        retryable: true,
        lastRequestTime: new Date().toISOString(),
      },
    })
    currentStepKey.value = 'describe'
    return
  }
  // 描述步骤未确认时自动确认
  if (steps.find((s) => s.key === 'describe')?.status !== 'success') {
    markDescribeDone()
  }
  receipt.fallback = false
  receipt.errorReason = null
  // 清空上一次错误行, 避免遗留
  receipt.errorLine = null
  receipt.errors = []
  // 编辑器默认收起, 让用户主动展开
  codePreviewExpanded.value = false
  setStepStatus('generate', 'loading', { error: null, message: forceFallback ? '已切换为本地模板' : '调用 LLM 中…' })
  startElapsedTimer('generate')
  try {
    const res = await apiService.generateStrategy(form.description, form.strategyName, {
      use_fallback: forceFallback,
      tags: form.tagsText
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
    })
    // 字段缺失降级
    receipt.filename = res.filename ?? null
    receipt.filepath = res.filepath ?? null
    receipt.strategy_id = res.strategy_id ?? null
    receipt.fallback = Boolean(res.fallback)
    receipt.reload_ok = res.reload_ok ?? null
    receipt.import_ok = res.import_ok ?? null
    receipt.smoke_ok = res.smoke_ok ?? null
    receipt.import_error = res.import_error ?? null
    receipt.smoke_error = res.smoke_error ?? null
    // 关键: 捕获 LLM/模板返回的 code (供 LightCodeEditor 预览)
    receipt.code = typeof res.code === 'string' ? res.code : ''
    // 解析 LLM 返回的初始错误 (极少见, 主要给后端把 syntax_error 透传使用)
    const line = parseErrorLine(res.import_error) ?? parseErrorLine(res.smoke_error)
    if (line && receipt.code) {
      receipt.errorLine = line
      receipt.errors = [
        { line, message: res.import_error || res.smoke_error || '解析错误', severity: 'error' },
      ]
    }
    stopElapsedTimer('generate', 'success')
    setStepStatus('generate', 'success', {
      message: receipt.fallback
        ? '已切换为本地模板策略'
        : 'LLM 策略生成成功',
    })
    currentStepKey.value = 'save'
  } catch (err) {
    // LLM 失败时自动 fallback 重试一次
    if (!forceFallback) {
      try {
        const res = await apiService.generateStrategy(form.description, form.strategyName, {
          use_fallback: true,
          tags: form.tagsText
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean),
        })
        receipt.filename = res.filename ?? null
        receipt.filepath = res.filepath ?? null
        receipt.strategy_id = res.strategy_id ?? null
        receipt.fallback = true
        receipt.reload_ok = res.reload_ok ?? null
        receipt.import_ok = res.import_ok ?? null
        receipt.smoke_ok = res.smoke_ok ?? null
        receipt.import_error = res.import_error ?? null
        receipt.smoke_error = res.smoke_error ?? null
        receipt.code = typeof res.code === 'string' ? res.code : ''
        const line2 = parseErrorLine(res.import_error) ?? parseErrorLine(res.smoke_error)
        if (line2 && receipt.code) {
          receipt.errorLine = line2
          receipt.errors = [
            { line: line2, message: res.import_error || res.smoke_error || '解析错误', severity: 'error' },
          ]
        }
        receipt.errorReason = String((err as any)?.message || 'LLM 不可用')
        stopElapsedTimer('generate', 'success')
        setStepStatus('generate', 'success', {
          message: '已切换为本地模板策略 (fallback)',
        })
        currentStepKey.value = 'save'
        return
      } catch (fallbackErr) {
        const stepError = captureError(fallbackErr, '/api/strategies/generate')
        receipt.errorReason = stepError.message
        stopElapsedTimer('generate', 'error')
        setStepStatus('generate', 'error', { error: stepError })
        currentStepKey.value = 'generate'
        return
      }
    }
    const stepError = captureError(err, '/api/strategies/generate')
    receipt.errorReason = stepError.message
    stopElapsedTimer('generate', 'error')
    setStepStatus('generate', 'error', { error: stepError })
    currentStepKey.value = 'generate'
  }
}

async function runSave(): Promise<void> {
  if (!receipt.strategy_id) {
    setStepStatus('generate', 'error', {
      error: {
        message: '未生成 strategy_id, 请先完成 Step 3',
        retryable: true,
        lastRequestTime: new Date().toISOString(),
      },
    })
    return
  }
  setStepStatus('save', 'loading', { error: null })
  startElapsedTimer('save')
  try {
    const res = await apiService.saveStrategy({
      name: receipt.strategy_id,
      description: form.description.slice(0, 200),
      code: receipt.code || '',
    })
    receipt.filename = res.filename ?? receipt.filename
    receipt.filepath = res.filepath ?? receipt.filepath
    stopElapsedTimer('save', 'success')
    setStepStatus('save', 'success', { message: res.message || '已保存' })
    currentStepKey.value = 'reload'
  } catch (err) {
    const stepError = captureError(err, '/api/strategies/save')
    stopElapsedTimer('save', 'error')
    setStepStatus('save', 'error', { error: stepError })
  }
}

async function runReload(): Promise<void> {
  if (!receipt.strategy_id) {
    setStepStatus('save', 'error', {
      error: {
        message: '未生成 strategy_id, 请先完成 Step 3',
        retryable: true,
        lastRequestTime: new Date().toISOString(),
      },
    })
    return
  }
  setStepStatus('reload', 'loading', { error: null })
  startElapsedTimer('reload')
  try {
    const res = await apiService.reloadStrategies({ refresh_all: true })
    receipt.reload_ok = true
    receipt.strategiesList = res.strategies || []
    receipt.strategiesCount = res.count ?? (res.strategies?.length ?? 0)
    stopElapsedTimer('reload', 'success')
    setStepStatus('reload', 'success', { message: res.message || '已重载' })
    currentStepKey.value = 'dryrun'
  } catch (err) {
    const stepError = captureError(err, '/api/strategies/reload')
    receipt.reload_ok = false
    stopElapsedTimer('reload', 'error')
    setStepStatus('reload', 'error', { error: stepError })
  }
}

async function runSmoke(): Promise<void> {
  if (!receipt.strategy_id) {
    setStepStatus('dryrun', 'error', {
      error: {
        message: '未生成 strategy_id',
        retryable: true,
        lastRequestTime: new Date().toISOString(),
      },
    })
    return
  }
  setStepStatus('dryrun', 'loading', { error: null })
  startElapsedTimer('dryrun')
  try {
    const res = await apiService.smokeStrategy(
      receipt.strategy_id,
      receipt.filepath || undefined
    )
    receipt.import_ok = res.import_ok ?? null
    receipt.smoke_ok = res.smoke_ok ?? null
    receipt.import_error = res.import_error ?? null
    receipt.smoke_error = res.smoke_error ?? null
    // 解析 smoke 错误行号, 联动 LightCodeEditor gutter
    const smokeMsg = res.smoke_error || res.import_error || ''
    const line = parseErrorLine(smokeMsg)
    if (line && !res.smoke_ok) {
      receipt.errorLine = line
      receipt.errors = [
        {
          line,
          message: smokeMsg || 'dry-run 失败',
          severity: 'error',
        },
      ]
      // 自动展开预览, 方便用户看到错误行
      codePreviewExpanded.value = true
    } else if (res.smoke_ok) {
      receipt.errorLine = null
      receipt.errors = []
    }
    stopElapsedTimer('dryrun', res.smoke_ok ? 'success' : 'error')
    if (res.smoke_ok) {
      setStepStatus('dryrun', 'success', { message: res.message || 'dry-run 通过' })
      currentStepKey.value = 'backtest'
    } else {
      setStepStatus('dryrun', 'error', {
        error: {
          message: res.smoke_error || res.import_error || 'dry-run 失败',
          endpoint: `/api/strategies/${receipt.strategy_id}/smoke`,
          retryable: true,
          lastRequestTime: new Date().toISOString(),
        },
      })
    }
  } catch (err) {
    const stepError = captureError(err, `/api/strategies/${receipt.strategy_id}/smoke`)
    receipt.smoke_ok = false
    // 错误响应里也尝试解析行号
    const line = parseErrorLine(stepError.detail) ?? parseErrorLine(stepError.message)
    if (line) {
      receipt.errorLine = line
      receipt.errors = [
        { line, message: stepError.message, severity: 'error' },
      ]
      codePreviewExpanded.value = true
    }
    stopElapsedTimer('dryrun', 'error')
    setStepStatus('dryrun', 'error', { error: stepError })
  }
}

// ============================================
// T2.5 真实回测入口 (POST /api/backtest/run + 轮询 + ECharts 收益曲线)
// ============================================

/** 停止轮询 (幂等) */
function stopPolling(): void {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/** 格式化百分比 (项目规范: 2 位小数) */
function formatPercent(v: any): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n.toFixed(2)}%`
}

/** 格式化分数 (项目规范: 4 位小数) */
function formatFraction(v: any): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(4)
}

/** 销毁 ECharts 实例 (幂等) */
function disposeEquityChart(): void {
  if (equityChartInstance) {
    try {
      equityChartInstance.dispose()
    } catch (e) {
      // 防御性: dispose 失败不影响主流程
      // eslint-disable-next-line no-console
      console.warn('[StrategyGenerator] dispose chart 失败:', e)
    }
    equityChartInstance = null
  }
}

/** 渲染收益曲线 (字段缺失一律降级) */
function renderEquityChart(): void {
  if (!equityChartRef.value) return
  if (equityPoints.value.length < 2) {
    disposeEquityChart()
    return
  }
  if (!equityChartInstance) {
    equityChartInstance = markRaw(echarts.init(equityChartRef.value))
  }
  const option: echarts.EChartsOption = {
    backgroundColor: 'transparent',
    grid: { left: 50, right: 16, top: 20, bottom: 30 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: equityPoints.value.map((p) => p.date),
      axisLabel: { color: '#787b86', fontSize: 10, formatter: (v: string) => (v ? v.slice(5) : '') },
      axisLine: { lineStyle: { color: '#2a2e39' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#787b86', fontSize: 10 },
      splitLine: { lineStyle: { color: '#2a2e39', type: 'dashed' } },
    },
    series: [
      {
        name: '权益',
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: equityPoints.value.map((p) => p.equity),
        lineStyle: { color: '#2962ff', width: 1.5 },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(41, 98, 255, 0.35)' },
              { offset: 1, color: 'rgba(41, 98, 255, 0)' },
            ],
          },
        },
      },
    ],
  }
  equityChartInstance.setOption(option, true)
}

/** T2.5 真实回测入口: 同步 → 异步 → 轮询 → 完成 */
async function runBacktest(): Promise<void> {
  // 重置上轮状态 (字段缺失降级)
  stopPolling()
  disposeEquityChart()
  receipt.backtestProgress = 0
  receipt.backtestMessage = '准备运行回测'
  receipt.backtestEndpoint = '/api/backtest/run'

  if (!receipt.strategy_id) {
    setStepStatus('backtest', 'error', {
      error: {
        message: '未生成 strategy_id, 请先完成 Step 3',
        code: 'BACKTEST_MISSING_PARAMETERS',
        retryable: true,
        endpoint: '/api/backtest/run',
        lastRequestTime: new Date().toISOString(),
      },
    })
    return
  }

  setStepStatus('backtest', 'loading', { error: null })
  startElapsedTimer('backtest')
  receipt.backtestStartedAt = new Date().toISOString()

  const startDate = '2024-01-01'
  const endDate = '2024-03-31'

  try {
    const res = await apiService.runBacktestJob({
      strategy_id: receipt.strategy_id,
      start_date: startDate,
      end_date: endDate,
      initial_capital: 1_000_000,
      params: {},
    })

    // 字段缺失一律降级
    receipt.backtestMode = res.mode || 'sync'
    receipt.backtestJobId = res.job_id || null
    receipt.backtestStatus = res.status || 'pending'
    receipt.backtestProgress = res.progress ?? 0
    receipt.backtestMessage = res.message || ''
    receipt.backtestEndpoint = res.endpoint || '/api/backtest/run'
    receipt.backtestElapsedMs = res.elapsed_ms ?? null

    if (res.status === 'success') {
      // ---- 同步模式直接完成 ----
      receipt.backtestStats = res.stats || null
      receipt.backtestEquityCurve = res.equity_curve || null
      receipt.backtestCompletedAt = new Date().toISOString()
      stopElapsedTimer('backtest', 'success')
      setStepStatus('backtest', 'success', {
        message: `回测完成 (${res.mode}, ${receipt.backtestElapsedMs ?? 0}ms)`,
      })
      // 渲染收益曲线
      await nextTick()
      renderEquityChart()
      return
    }

    if (res.status === 'failed') {
      // ---- 同步模式业务失败 ----
      receipt.backtestCompletedAt = new Date().toISOString()
      const errPayload = res.error || {
        code: 'BACKTEST_RUNTIME_EXCEPTION',
        message: res.message || '回测失败',
        detail: '',
        retryable: true,
        endpoint: receipt.backtestEndpoint,
      }
      stopElapsedTimer('backtest', 'error')
      setStepStatus('backtest', 'error', {
        error: {
          message: errPayload.message || '回测失败',
          code: errPayload.code,
          detail: errPayload.detail,
          endpoint: errPayload.endpoint || receipt.backtestEndpoint,
          status: 200,
          retryable: Boolean(errPayload.retryable),
          lastRequestTime: new Date().toISOString(),
        },
      })
      return
    }

    // ---- 异步模式: 切换到轮询 ----
    if (res.status === 'pending' || res.status === 'running' || res.status === 'queued') {
      receipt.backtestStatus = 'pending'
      receipt.backtestMessage = res.message || '任务已提交, 正在轮询进度...'
      // 设置 step 为 loading, 等轮询完成
      setStepStatus('backtest', 'loading', { error: null })
      startBacktestPolling()
    }
  } catch (err) {
    // ---- 网络 / 超时 / 协议错误 ----
    const apiErr = err as ApiError
    stopPolling()
    const stepError = captureError(err, '/api/backtest/run')
    receipt.backtestStatus = 'failed'

    if (apiErr && apiErr.code === 'TIMEOUT') {
      // 30s 仍未完成 → 视为 timeout, 但 job_id 可能已生成, 让用户继续轮询
      stopElapsedTimer('backtest', 'timeout')
      setStepStatus('backtest', 'timeout', { error: stepError })
      receipt.backtestMessage = '请求超时, 但后台 job 可能仍在跑, 可使用 job_id 继续轮询'
    } else {
      stopElapsedTimer('backtest', 'error')
      setStepStatus('backtest', 'error', { error: stepError })
    }
  }
}

/** 异步模式: 每 3s 轮询一次, 最长 5 分钟 */
function startBacktestPolling(): void {
  stopPolling()
  const POLL_INTERVAL_MS = 3_000
  const MAX_POLL_MS = 5 * 60_000
  const startedAt = Date.now()
  const jobId = receipt.backtestJobId
  if (!jobId) {
    // 防御性: 没有 job_id 直接失败
    setStepStatus('backtest', 'error', {
      error: {
        message: '异步模式但缺少 job_id',
        code: 'BACKTEST_RUNTIME_EXCEPTION',
        retryable: true,
        endpoint: '/api/backtest/run',
        lastRequestTime: new Date().toISOString(),
      },
    })
    return
  }

  pollTimer = setInterval(async () => {
    if (Date.now() - startedAt > MAX_POLL_MS) {
      stopPolling()
      stopElapsedTimer('backtest', 'timeout')
      setStepStatus('backtest', 'timeout', {
        error: {
          message: '轮询超过 5 分钟, 自动停止',
          code: 'BACKTEST_RUNTIME_EXCEPTION',
          retryable: true,
          endpoint: `/api/backtest/jobs/${jobId}`,
          lastRequestTime: new Date().toISOString(),
        },
      })
      return
    }
    try {
      const res = await apiService.getBacktestJob(jobId)
      receipt.backtestProgress = res.progress ?? receipt.backtestProgress
      receipt.backtestMessage = res.message || receipt.backtestMessage
      receipt.backtestStatus = res.status
      receipt.backtestElapsedMs = res.elapsed_ms ?? receipt.backtestElapsedMs

      if (res.status === 'success') {
        stopPolling()
        receipt.backtestStats = res.stats || null
        receipt.backtestEquityCurve = res.equity_curve || null
        receipt.backtestCompletedAt = new Date().toISOString()
        stopElapsedTimer('backtest', 'success')
        setStepStatus('backtest', 'success', {
          message: `回测完成 (async, ${receipt.backtestElapsedMs ?? 0}ms)`,
        })
        // 渲染收益曲线
        await nextTick()
        renderEquityChart()
      } else if (res.status === 'failed') {
        stopPolling()
        receipt.backtestCompletedAt = new Date().toISOString()
        const errPayload = (res as any).error || {
          code: 'BACKTEST_RUNTIME_EXCEPTION',
          message: res.message || '回测失败',
          detail: '',
          retryable: false,
        }
        stopElapsedTimer('backtest', 'error')
        setStepStatus('backtest', 'error', {
          error: {
            message: errPayload.message,
            code: errPayload.code,
            detail: errPayload.detail,
            endpoint: receipt.backtestEndpoint,
            status: 200,
            retryable: Boolean(errPayload.retryable),
            lastRequestTime: new Date().toISOString(),
          },
        })
      }
    } catch (pollErr) {
      // 单次轮询失败不立刻停止, 继续下一次; 累计 3 次失败再停
      pollRetries.value += 1
      if (pollRetries.value >= 3) {
        stopPolling()
        const stepError = captureError(pollErr, `/api/backtest/jobs/${jobId}`)
        stopElapsedTimer('backtest', 'error')
        setStepStatus('backtest', 'error', { error: stepError })
      }
    }
  }, POLL_INTERVAL_MS)
}

// ============================================
// 生命周期
// ============================================
onMounted(() => {
  // 初始化当前步骤
  currentStepKey.value = 'describe'
})

onBeforeUnmount(() => {
  // 清理所有 elapsed timer
  for (const k of Object.keys(elapsedTimers)) {
    clearInterval(elapsedTimers[k])
    delete elapsedTimers[k]
  }
  // T2.5: 清理回测相关资源
  stopPolling()
  disposeEquityChart()
  // 移除 window resize 监听
  if (typeof window !== 'undefined') {
    window.removeEventListener('resize', handleEquityResize)
  }
})

/** ECharts resize 处理 (空操作, 占位避免 lint 报错) */
function handleEquityResize(): void {
  if (equityChartInstance) {
    try {
      equityChartInstance.resize()
    } catch (e) {
      // 防御性: resize 失败不阻断
    }
  }
}
</script>

<style scoped>
/* ============================================
 * 容器
 * ============================================ */
.strategy-workflow {
  position: relative;
  padding: 24px;
  max-width: 1100px;
  margin: 0 auto;
  color: #d1d4dc;
}

/* ============================================
 * 顶部
 * ============================================ */
.workflow-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 24px;
  padding: 16px 20px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 8px;
}
.workflow-title {
  display: flex;
  align-items: center;
  gap: 10px;
}
.workflow-title__icon {
  display: inline-grid;
  place-items: center;
  width: 32px;
  height: 32px;
  background: #2962ff;
  color: #fff;
  border-radius: 6px;
  font-weight: 700;
  font-size: 12px;
}
.workflow-title__text {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: #fff;
}
.workflow-title__hint {
  color: #787b86;
  font-size: 12px;
}
.workflow-progress {
  position: relative;
  flex: 0 0 280px;
  height: 24px;
  background: #0a0a0a;
  border: 1px solid #2a2e39;
  border-radius: 999px;
  overflow: hidden;
}
.workflow-progress__bar {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #2962ff, #28a745);
  transition: width 0.4s ease;
}
.workflow-progress__label {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: 11px;
  color: #d1d4dc;
  font-weight: 600;
}

/* ============================================
 * Step 卡片
 * ============================================ */
.workflow-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.step-card {
  position: relative;
  padding: 18px 20px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 8px;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.step-card.is-active {
  border-color: #2962ff;
  box-shadow: 0 0 0 2px rgba(41, 98, 255, 0.18);
}
.step-card--success {
  border-color: rgba(40, 167, 69, 0.5);
}
.step-card--error {
  border-color: rgba(220, 53, 69, 0.5);
}
.step-card--loading {
  border-color: rgba(41, 98, 255, 0.7);
}
.step-connector {
  position: absolute;
  left: 30px;
  bottom: -16px;
  width: 2px;
  height: 16px;
  background: #2a2e39;
  z-index: 1;
}
.step-connector--done {
  background: #28a745;
}
.step-connector--active {
  background: #2962ff;
}

.step-card__head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.step-card__index {
  display: inline-grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  font-weight: 700;
  font-size: 13px;
  background: #2a2e39;
  color: #d1d4dc;
}
.step-card__index--success { background: #28a745; color: #fff; }
.step-card__index--error { background: #dc3545; color: #fff; }
.step-card__index--loading { background: #2962ff; color: #fff; }
.step-card__title {
  display: flex;
  flex-direction: column;
  flex: 1;
}
.step-card__title strong {
  font-size: 14px;
  color: #fff;
}
.step-card__sub {
  font-size: 11px;
  color: #787b86;
  margin-top: 2px;
}
.step-card__tag {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  background: #2a2e39;
  color: #d1d4dc;
}
.step-card__tag--success { background: rgba(40, 167, 69, 0.2); color: #28a745; }
.step-card__tag--error { background: rgba(220, 53, 69, 0.2); color: #ff6b78; }
.step-card__tag--loading { background: rgba(41, 98, 255, 0.2); color: #4d8aff; }
.step-card__tag--pending { background: #2a2e39; color: #d1d4dc; }

.step-card__body {
  padding-left: 40px;
}

/* ============================================
 * 表单
 * ============================================ */
.form-row { margin-bottom: 12px; }
.form-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: #d1d4dc;
  font-weight: 500;
}
.form-input, .form-textarea {
  width: 100%;
  padding: 9px 12px;
  background: #0a0a0a;
  border: 1px solid #2a2e39;
  border-radius: 4px;
  font-size: 13px;
  font-family: inherit;
  color: #d1d4dc;
  transition: border-color 0.2s;
}
.form-input:focus, .form-textarea:focus {
  outline: none;
  border-color: #2962ff;
}
.form-textarea { resize: vertical; min-height: 100px; }

/* ============================================
 * API 状态
 * ============================================ */
.api-status {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  margin-bottom: 12px;
  padding: 12px 14px;
  background: #0a0a0a;
  border-radius: 6px;
  border: 1px solid #2a2e39;
}
.api-status__row {
  display: grid;
  grid-template-columns: 100px 1fr;
  gap: 12px;
  align-items: center;
  font-size: 12px;
}
.api-status__label {
  color: #787b86;
}
.api-status__value {
  color: #d1d4dc;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}

/* ============================================
 * 步骤结果
 * ============================================ */
.step-result {
  padding: 12px 14px;
  background: rgba(40, 167, 69, 0.06);
  border: 1px solid rgba(40, 167, 69, 0.25);
  border-radius: 6px;
  margin-bottom: 12px;
}
.result-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
.result-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  background: #0a0a0a;
  border: 1px solid #2a2e39;
  border-radius: 4px;
}
.result-item--full { grid-column: 1 / -1; }
.result-item__label {
  font-size: 11px;
  color: #787b86;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.result-item__value {
  font-size: 12px;
  color: #d1d4dc;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}
.step-message {
  margin: 8px 0 0;
  font-size: 12px;
  color: #d1d4dc;
}
.step-message--error {
  color: #ff6b78;
  background: rgba(220, 53, 69, 0.1);
  padding: 6px 10px;
  border-radius: 4px;
  border-left: 3px solid #dc3545;
}
.code-preview {
  margin-top: 10px;
  background: #0a0a0a;
  border: 1px solid #2a2e39;
  border-radius: 4px;
  overflow: hidden;
}
.code-preview__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #131722;
  border-bottom: 1px solid #2a2e39;
  font-size: 12px;
  color: #d1d4dc;
}
.code-preview__head strong {
  font-size: 12px;
  color: #fff;
  font-weight: 600;
}
.code-preview__head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.code-preview__toggle {
  padding: 2px 10px;
  font-size: 11px;
  background: transparent;
  color: #4d8aff;
  border: 1px solid rgba(41, 98, 255, 0.4);
  border-radius: 3px;
  cursor: pointer;
  transition: all 0.15s;
}
.code-preview__toggle:hover {
  background: rgba(41, 98, 255, 0.15);
  color: #fff;
}
.code-preview__meta {
  font-size: 11px;
  color: #787b86;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.code-preview__err {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(220, 53, 69, 0.2);
  color: #ff6b78;
  font-weight: 600;
}
.code-preview__body {
  margin: 0;
  padding: 12px;
  max-height: 240px;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 1.6;
  color: #d1d4dc;
  background: #000;
}

/* ============================================
 * 按钮
 * ============================================ */
.step-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.btn {
  padding: 8px 16px;
  border: none;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn--primary { background: #2962ff; color: #fff; }
.btn--primary:hover:not(:disabled) { background: #1e4bd8; }
.btn--secondary { background: #28a745; color: #fff; }
.btn--secondary:hover:not(:disabled) { background: #218838; }
.btn--ghost {
  background: transparent;
  color: #d1d4dc;
  border: 1px solid #2a2e39;
}
.btn--ghost:hover:not(:disabled) { background: #1a1a1a; }

/* ============================================
 * 标签 / 徽标
 * ============================================ */
.tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  background: #2a2e39;
  color: #d1d4dc;
}
.tag--success { background: rgba(40, 167, 69, 0.2); color: #28a745; }
.tag--error { background: rgba(220, 53, 69, 0.2); color: #ff6b78; }
.tag--warning { background: rgba(255, 193, 7, 0.2); color: #ffc107; }
.tag--info { background: rgba(41, 98, 255, 0.2); color: #4d8aff; }
.tag--unknown { background: #2a2e39; color: #d1d4dc; font-style: italic; }

/* ============================================
 * 诊断
 * ============================================ */
.diagnose-list {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.diagnose-item {
  padding: 12px 14px;
  background: rgba(220, 53, 69, 0.06);
  border: 1px solid rgba(220, 53, 69, 0.3);
  border-radius: 6px;
  border-left: 3px solid #dc3545;
}
.diagnose-item__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 600;
  color: #ff6b78;
}
.diagnose-item__endpoint code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  background: rgba(220, 53, 69, 0.1);
  padding: 1px 6px;
  border-radius: 3px;
  color: #d1d4dc;
}
.diagnose-item__cause,
.diagnose-item__suggest {
  margin: 4px 0;
  font-size: 12px;
  color: #d1d4dc;
}
.diagnose-item__cause strong,
.diagnose-item__suggest strong {
  color: #ff6b78;
  margin-right: 4px;
}
.diagnose-item__suggest strong { color: #28a745; }

/* ============================================
 * 加载 / 错误 / 步骤内
 * ============================================ */
.step-error { margin-bottom: 12px; }
.step-pending { margin-bottom: 12px; }
.step-error :deep(.error-state),
.step-pending :deep(.empty-state),
.step-error :deep(.loading-state) {
  background: transparent;
  border: none;
  min-height: 100px;
  padding: 0;
  text-align: left;
}

/* ============================================
 * reload 列表
 * ============================================ */
.reload-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 8px 12px;
  background: #0a0a0a;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #d1d4dc;
}
.reload-list li { padding: 2px 0; }
.reload-list__more { color: #787b86; font-style: italic; }

/* ============================================
 * 回执卡片 (右下角 fixed)
 * ============================================ */
.receipt-card {
  position: fixed;
  right: 16px;
  bottom: 16px;
  width: 320px;
  max-height: 60vh;
  overflow: auto;
  padding: 14px 16px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 10px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  z-index: 100;
}
.receipt-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid #2a2e39;
}
.receipt-card__head strong { font-size: 13px; color: #fff; }
.receipt-card__hint {
  font-size: 11px;
  color: #787b86;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.receipt-card__list {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.receipt-card__row {
  display: grid;
  grid-template-columns: 90px 1fr;
  gap: 8px;
  align-items: center;
  font-size: 11px;
}
.receipt-card__row dt {
  color: #787b86;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 600;
}
.receipt-card__row dd {
  margin: 0;
  color: #d1d4dc;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}
.receipt-card__path {
  font-size: 10px;
  color: #787b86;
  max-height: 40px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.receipt-card__row--error {
  grid-template-columns: 1fr;
  background: rgba(220, 53, 69, 0.1);
  border-left: 3px solid #dc3545;
  padding: 6px 8px;
  border-radius: 4px;
  margin-top: 4px;
}
.receipt-card__row--error dt { color: #ff6b78; margin-bottom: 2px; }
.receipt-card__row--error dd { color: #ff6b78; font-family: inherit; }

/* ============================================
 * T2.5: 真实回测入口 - 进度条 / 收益曲线 / 建议
 * ============================================ */
.step-progress {
  position: relative;
  height: 8px;
  margin: 8px 0 12px;
  background: #0a0a0a;
  border: 1px solid #2a2e39;
  border-radius: 999px;
  overflow: hidden;
}
.step-progress__bar {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #2962ff, #28a745);
  transition: width 0.3s ease;
}
.step-progress__label {
  position: absolute;
  inset: -18px 0 auto 0;
  font-size: 10px;
  color: #787b86;
  text-align: right;
}
.step-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0 0;
  font-size: 11px;
  color: #787b86;
}
.step-meta__label { white-space: nowrap; }
.step-meta__value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: #d1d4dc;
  word-break: break-all;
}
.step-suggestion {
  margin: 8px 0 0;
  padding: 8px 12px;
  background: rgba(255, 193, 7, 0.08);
  border: 1px solid rgba(255, 193, 7, 0.3);
  border-left: 3px solid #ffc107;
  border-radius: 4px;
  font-size: 12px;
  color: #d1d4dc;
}
.step-suggestion strong { color: #ffc107; margin-right: 4px; }
.step-message--info {
  color: #4d8aff;
  background: rgba(41, 98, 255, 0.08);
  padding: 6px 10px;
  border-radius: 4px;
  border-left: 3px solid #2962ff;
}
.equity-chart-wrap {
  margin-top: 12px;
  background: #0a0a0a;
  border: 1px solid #2a2e39;
  border-radius: 6px;
  overflow: hidden;
}
.equity-chart-wrap__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid #2a2e39;
}
.equity-chart-wrap__head strong { font-size: 12px; color: #fff; }
.equity-chart-wrap__hint { font-size: 11px; color: #787b86; }
.equity-chart {
  width: 100%;
  height: 240px;
}
</style>
