<template>
  <div class="dragon-eye-page bg-[#0a0a0a] min-h-screen">
    <DebugButton
      :data-date="latestDate || targetDate"
      :stale="isStale"
      :partial-success="hasMissingParts"
    />
    <!-- Header -->
    <header class="h-14 px-4 flex items-center justify-between bg-[#111] border-b border-[#1a1a1a]">
      <div class="flex items-center gap-3">
        <h1 class="text-lg font-bold text-white flex items-center gap-2">
          <i class="fas fa-dragon text-orange-500"></i>
          DragonEye
        </h1>
        <span class="text-xs text-slate-500 pl-3 border-l border-[#2a2a2a]">龙虎榜监控</span>
      </div>

      <div class="flex items-center gap-3">
        <div class="flex items-center gap-2">
          <span class="text-xs text-slate-500">日期</span>
          <input
            type="date"
            v-model="targetDate"
            class="h-7 px-2 text-xs text-white bg-[#1a1a1a] rounded border-none outline-none focus:ring-2 focus:ring-blue-500/20"
          />
        </div>

        <div class="w-px h-5 bg-[#2a2a2a]"></div>

        <div class="flex gap-2">
          <button
            @click="startCrawl"
            :disabled="!!(isCrawling || activeJobId)"
            class="h-7 px-3 bg-[#1a1a1a] hover:bg-[#252525] text-slate-300 rounded text-xs font-medium transition-all disabled:opacity-50 flex items-center gap-1.5"
          >
            <i class="fas" :class="isCrawling ? 'fa-spinner fa-spin' : 'fa-spider'"></i>
            <span>启动抓取</span>
          </button>

          <button
            @click="startPipeline"
            :disabled="!!(isProcessing || activeJobId)"
            class="h-7 px-3 bg-blue-600 hover:bg-blue-700 text-white rounded text-xs font-medium transition-all shadow-lg shadow-blue-500/20 disabled:opacity-50 flex items-center gap-1.5"
          >
            <i class="fas" :class="isProcessing ? 'fa-spinner fa-spin' : 'fa-magic'"></i>
            <span>一键全流程</span>
          </button>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <div v-if="activeJobId" class="flex items-center gap-2 px-2 py-1 bg-[#1a1a1a] rounded">
          <span class="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse"></span>
          <span class="text-xs text-slate-400">{{ jobProgress }}%</span>
        </div>
        <button @click="showLogs = !showLogs" class="w-7 h-7 flex items-center justify-center text-slate-400 hover:text-white transition-colors">
          <i class="fas fa-terminal text-xs"></i>
        </button>
        <button class="w-7 h-7 flex items-center justify-center text-slate-400 hover:text-white transition-colors">
          <i class="fas fa-cog text-xs"></i>
        </button>
      </div>
    </header>

    <!-- Progress Bar -->
    <div v-if="activeJobId" class="h-0.5 bg-[#1a1a1a]">
      <div
        class="h-full transition-all duration-300 rounded-full"
        :class="{
          'bg-gradient-to-r from-orange-500 to-red-500': jobType === 'crawl',
          'bg-gradient-to-r from-blue-500 to-indigo-500': jobType === 'full_pipeline'
        }"
        :style="{ width: `${jobProgress}%` }"
      ></div>
    </div>

    <!-- 顶部错误条 -->
    <ErrorState
      v-if="topLevelError"
      class="m-3"
      :title="topLevelError.title"
      :message="topLevelError.message"
      :detail="topLevelError.detail"
      :status="topLevelError.status"
      :code="topLevelError.code"
      :endpoint="topLevelError.endpoint"
      :retryable="topLevelError.retryable"
      :last-request-time="topLevelError.lastRequestTime"
      @retry="handleTopLevelRetry"
    />

    <!-- 证据面板 -->
    <section class="evidence-panel">
      <div class="evidence-heading">
        <div>
          <h2>本地证据完整度</h2>
          <p>缺失组件不会按 0 或完整结果展示。</p>
        </div>
        <span class="evidence-mode">{{ evidenceMode }}</span>
      </div>
      <LoadingState v-if="evidenceLoading" title="正在读取 DragonEye 证据" />
      <ErrorState
        v-else-if="evidenceError"
        :title="evidenceError.title"
        :message="evidenceError.message"
        :detail="evidenceError.detail"
        :status="evidenceError.status"
        :code="evidenceError.code"
        :endpoint="evidenceError.endpoint"
        :retryable="evidenceError.retryable"
        @retry="fetchEvidenceStatus"
      />
      <EmptyState
        v-else-if="!evidenceStatus"
        title="暂无证据完整度"
        description="暂无本地证据"
        :hint="emptyDiagnosticsHint"
      />
      <dl v-else class="evidence-grid">
        <div><dt>has_ladder</dt><dd>{{ formatAvailability(evidenceStatus.has_ladder) }}</dd></div>
        <div><dt>has_limit_up</dt><dd>{{ formatAvailability(evidenceStatus.has_limit_up) }}</dd></div>
        <div><dt>has_sentiment</dt><dd>{{ formatAvailability(evidenceStatus.has_sentiment) }}</dd></div>
        <div><dt>has_theme_flow</dt><dd>{{ formatAvailability(evidenceStatus.has_theme_flow) }}</dd></div>
        <div><dt>证据日期</dt><dd>{{ evidenceStatus.evidence_date || 'unavailable' }}</dd></div>
        <div><dt>完整度分数</dt><dd>{{ formatScore(evidenceStatus.completeness_score) }}</dd></div>
        <div class="wide"><dt>缺失组件</dt><dd>{{ formatMissingParts(evidenceStatus.missing_parts) }}</dd></div>
        <div class="wide"><dt>状态</dt><dd>{{ evidenceMode }}</dd></div>
      </dl>
    </section>

    <!-- 数据看板: 5 个数据卡片 -->
    <section class="dashboard-row">
      <div class="stat-card">
        <div class="stat-card__label">股票数</div>
        <div class="stat-card__value" :class="statValueClass(stockCount != null)">
          {{ stockCount != null ? stockCount : '—' }}
        </div>
        <span v-if="stockCount == null" class="stat-card__badge">无本地证据</span>
      </div>
      <div class="stat-card">
        <div class="stat-card__label">情感分</div>
        <div class="stat-card__value" :class="statValueClass(sentimentScore != null)">
          {{ formatSentimentScore(sentimentScore) }}
        </div>
        <span v-if="sentimentScore == null" class="stat-card__badge">无本地证据</span>
      </div>
      <div class="stat-card">
        <div class="stat-card__label">主题数</div>
        <div class="stat-card__value" :class="statValueClass(themeCount != null)">
          {{ themeCount != null ? themeCount : '—' }}
        </div>
        <span v-if="themeCount == null" class="stat-card__badge">无本地证据</span>
      </div>
      <div class="stat-card">
        <div class="stat-card__label">最新日期</div>
        <div class="stat-card__value text-slate-200">
          {{ latestDate || '—' }}
        </div>
        <span v-if="!latestDate" class="stat-card__badge">无本地证据</span>
      </div>
      <div class="stat-card">
        <div class="stat-card__label">状态</div>
        <div class="stat-card__value" :class="statusValueClass">
          {{ statusLabel }}
        </div>
        <span v-if="activeJobId" class="stat-card__badge stat-card__badge--live">
          {{ jobStatus || 'running' }} · {{ jobProgress }}%
        </span>
      </div>
    </section>

    <!-- 主内容: 双列 -->
    <div class="main-grid p-3">
      <!-- 左列 -->
      <div class="left-column">
        <!-- 指标行 -->
        <div class="grid grid-cols-4 gap-2">
          <div class="bg-[#111] rounded-lg p-3 flex items-center">
            <div class="flex flex-col gap-0.5">
              <span class="text-[10px] text-slate-500 uppercase tracking-wider">涨停家数</span>
              <span class="text-lg font-semibold font-mono" :class="(metrics.limitUp ?? 0) >= 50 ? 'text-[#00d084]' : 'text-slate-300'">{{ metrics.limitUp ?? 'unavailable' }}</span>
            </div>
          </div>
          <div class="bg-[#111] rounded-lg p-3 flex items-center">
            <div class="flex flex-col gap-0.5">
              <span class="text-[10px] text-slate-500 uppercase tracking-wider">跌停家数</span>
              <span class="text-lg font-semibold font-mono" :class="(metrics.limitDown ?? 0) > 10 ? 'text-[#ff4757]' : 'text-slate-300'">{{ metrics.limitDown ?? 'unavailable' }}</span>
            </div>
          </div>
          <div class="bg-[#111] rounded-lg p-3 flex items-center">
            <div class="flex flex-col gap-0.5">
              <span class="text-[10px] text-slate-500 uppercase tracking-wider">最高连板</span>
              <span class="text-lg font-semibold font-mono text-[#ffa502]">{{ metrics.maxHeight }}</span>
            </div>
          </div>
          <div class="bg-[#111] rounded-lg p-3 flex items-center">
            <div class="flex flex-col gap-0.5">
              <span class="text-[10px] text-slate-500 uppercase tracking-wider">炸板率</span>
              <span class="text-lg font-semibold font-mono" :class="parseFloat(metrics.brokenRatio) > 20 ? 'text-[#ff4757]' : 'text-[#ffa502]'">{{ metrics.brokenRatio }}</span>
            </div>
          </div>
        </div>

        <!-- 股票表 -->
        <div class="bg-[#111] rounded-lg flex-1 min-h-0 flex flex-col">
          <div class="px-3 py-2 flex items-center justify-between border-b border-[#1a1a1a]">
            <span class="text-xs font-medium text-white">龙头股实时因子</span>
            <div class="flex gap-1">
              <button class="w-6 h-6 flex items-center justify-center text-slate-500 hover:text-white transition-colors">
                <i class="fas fa-filter text-xs"></i>
              </button>
              <button class="w-6 h-6 flex items-center justify-center text-slate-500 hover:text-white transition-colors">
                <i class="fas fa-download text-xs"></i>
              </button>
            </div>
          </div>
          <div class="flex-1 overflow-auto">
            <table class="w-full text-left text-xs">
              <thead class="sticky top-0 bg-[#111] z-10">
                <tr class="text-[10px] text-slate-500 uppercase tracking-wider">
                  <th class="px-3 py-2 font-medium">个股</th>
                  <th class="px-3 py-2 font-medium">连板</th>
                  <th class="px-3 py-2 font-medium text-right">封单额</th>
                  <th class="px-3 py-2 font-medium text-right">换手</th>
                  <th class="px-3 py-2 font-medium text-center">监管</th>
                  <th class="px-3 py-2 font-medium text-center">机构</th>
                  <th class="px-3 py-2 font-medium">题材</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="stock in dragonStocks" :key="stock.stock_code" class="hover:bg-[#1a1a1a] transition-colors">
                  <td class="px-3 py-2">
                    <div class="flex flex-col">
                      <span class="text-blue-400 font-medium">{{ stock.stock_name }}</span>
                      <span class="text-[10px] text-slate-600 font-mono">{{ stock.stock_code }}</span>
                    </div>
                  </td>
                  <td class="px-3 py-2">
                    <span class="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium"
                      :class="stock.continue_num >= 5 ? 'bg-[#ff4757]/20 text-[#ff4757]' : stock.continue_num >= 3 ? 'bg-[#ffa502]/20 text-[#ffa502]' : 'bg-[#787b86]/20 text-[#787b86]'">
                      {{ stock.continue_num }}板
                    </span>
                  </td>
                  <td class="px-3 py-2 text-right font-mono text-slate-300">{{ formatYi(stock.order_amount) }}</td>
                  <td class="px-3 py-2 text-right font-mono text-slate-300">{{ formatPercentValue(stock.turnover_rate) }}</td>
                  <td class="px-3 py-2 text-center">
                    <i v-if="stock.is_regulation" class="fas fa-exclamation-triangle text-[#ff4757] text-xs" title="处于监管监控"></i>
                    <span v-else class="text-slate-600">-</span>
                  </td>
                  <td class="px-3 py-2 text-center">
                    <i v-if="stock.is_institution_buy" class="fas fa-university text-[#00d084] text-xs" title="机构专用买入"></i>
                    <span v-else class="text-slate-600">-</span>
                  </td>
                  <td class="px-3 py-2">
                    <span class="text-[10px] text-slate-400 truncate max-w-[100px] block" :title="stock.leader_tag">{{ stock.leader_tag }}</span>
                  </td>
                </tr>
                <tr v-if="!stocksLoading && dragonStocks.length === 0">
                  <td colspan="7" class="px-3 py-2">
                    <EmptyState
                      :title="stocksEmptyTitle"
                      :description="stocksEmptyDescription"
                      :hint="stocksEmptyHint"
                    />
                  </td>
                </tr>
              </tbody>
            </table>
            <LoadingState v-if="stocksLoading" title="正在加载龙头股数据" />
          </div>
        </div>

        <!-- 情绪图表 -->
        <div class="bg-[#111] rounded-lg h-[200px] flex flex-col">
          <div class="px-3 py-2 flex items-center justify-between border-b border-[#1a1a1a]">
            <span class="text-xs font-medium text-white">市场情绪走势</span>
            <button class="w-6 h-6 flex items-center justify-center text-slate-500 hover:text-white transition-colors">
              <i class="fas fa-expand text-xs"></i>
            </button>
          </div>
          <div ref="sentimentChartRef" class="flex-1"></div>
        </div>
      </div>

      <!-- 右列 -->
      <div class="right-column">
        <!-- 任务状态卡片: 启动抓取后展示 -->
        <div v-if="activeJobId || jobStatus === 'failed'" class="bg-[#111] rounded-lg p-3 space-y-2">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <i class="fas fa-tasks text-blue-400 text-xs"></i>
              <span class="text-xs font-medium text-white">任务状态</span>
            </div>
            <span class="text-[10px] text-slate-500">job_id: {{ activeJobId || lastJobId || '—' }}</span>
          </div>
          <div class="grid grid-cols-3 gap-2 text-[11px]">
            <div class="bg-[#0a0a0a] rounded p-2">
              <div class="text-[10px] text-slate-500">状态</div>
              <div class="font-medium mt-0.5" :class="jobStatusClass">{{ jobStatusLabel }}</div>
            </div>
            <div class="bg-[#0a0a0a] rounded p-2">
              <div class="text-[10px] text-slate-500">进度</div>
              <div class="font-medium text-slate-200 mt-0.5">{{ jobProgress }}%</div>
            </div>
            <div class="bg-[#0a0a0a] rounded p-2">
              <div class="text-[10px] text-slate-500">耗时</div>
              <div class="font-medium text-slate-200 mt-0.5">{{ jobElapsedLabel }}</div>
            </div>
          </div>
          <div v-if="jobStats" class="grid grid-cols-3 gap-2 text-[11px]">
            <div class="bg-[#0a0a0a] rounded p-2">
              <div class="text-[10px] text-slate-500">爬取股票</div>
              <div class="font-medium text-[#00d084] mt-0.5">{{ jobStats.stock_count ?? '—' }}</div>
            </div>
            <div class="bg-[#0a0a0a] rounded p-2">
              <div class="text-[10px] text-slate-500">情感条数</div>
              <div class="font-medium text-[#00d084] mt-0.5">{{ jobStats.sentiment_count ?? '—' }}</div>
            </div>
            <div class="bg-[#0a0a0a] rounded p-2">
              <div class="text-[10px] text-slate-500">主题数</div>
              <div class="font-medium text-[#00d084] mt-0.5">{{ jobStats.theme_count ?? '—' }}</div>
            </div>
          </div>
          <div>
            <div class="text-[10px] text-slate-500 mb-1">最近 5 条日志</div>
            <ul class="text-[10px] font-mono space-y-0.5 max-h-24 overflow-auto">
              <li
                v-for="(log, idx) in recentJobLogs"
                :key="`${log.timestamp}-${idx}`"
                class="flex gap-2"
              >
                <span class="text-slate-600 shrink-0">{{ formatTime(log.timestamp) }}</span>
                <span
                  class="shrink-0 w-12 text-right"
                  :class="{
                    'text-blue-400': log.level === 'info',
                    'text-green-400': log.level === 'success',
                    'text-yellow-400': log.level === 'warning',
                    'text-red-400': log.level === 'error'
                  }"
                >[{{ log.level?.toUpperCase() || 'INFO' }}]</span>
                <span class="text-slate-300 break-all">{{ log.message }}</span>
              </li>
              <li v-if="recentJobLogs.length === 0" class="text-slate-600">暂无日志</li>
            </ul>
          </div>
          <div v-if="jobErrorMessage" class="text-[11px] text-red-400 bg-red-500/10 rounded p-2">
            <div class="font-medium">任务失败: {{ jobErrorMessage }}</div>
            <div v-if="jobErrorDetail" class="text-[10px] text-red-300/80 mt-1 break-all">{{ jobErrorDetail }}</div>
            <div class="text-[10px] text-slate-400 mt-1">建议: {{ jobErrorSuggestionText }}</div>
          </div>
        </div>

        <!-- AI 简报面板 -->
        <div class="bg-[#111] rounded-lg overflow-hidden" :class="{ 'collapsed': aiBriefCollapsed }">
          <div class="px-3 py-2 flex items-center justify-between cursor-pointer hover:bg-[#1a1a1a] transition-colors" @click="aiBriefCollapsed = !aiBriefCollapsed">
            <div class="flex items-center gap-2">
              <i class="fas fa-robot text-blue-400 text-xs"></i>
              <span class="text-xs font-medium text-white">AI 每日复盘简报</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="text-[10px] text-slate-500">{{ targetDate }}</span>
              <i class="fas fa-chevron-down text-[10px] text-slate-500 transition-transform" :class="{ 'rotate-180': !aiBriefCollapsed }"></i>
            </div>
          </div>

          <div v-show="!aiBriefCollapsed" class="px-3 pb-3 space-y-3">
            <!-- 关键指标 -->
            <div class="grid grid-cols-3 gap-2">
              <div class="bg-[#0a0a0a] rounded p-2 text-center">
                <div class="text-[10px] text-slate-500 mb-0.5">市场情绪</div>
                <div class="text-xs font-medium" :class="sentimentColor">{{ sentimentText }}</div>
              </div>
              <div class="bg-[#0a0a0a] rounded p-2 text-center">
                <div class="text-[10px] text-slate-500 mb-0.5">涨停家数</div>
                <div class="text-xs font-medium text-[#00d084]">{{ metrics.limitUp ?? 'unavailable' }}</div>
              </div>
              <div class="bg-[#0a0a0a] rounded p-2 text-center">
                <div class="text-[10px] text-slate-500 mb-0.5">最高连板</div>
                <div class="text-xs font-medium text-[#ffa502]">{{ metrics.maxHeight }}</div>
              </div>
            </div>

            <!-- 市场概况关键词 -->
            <div>
              <div class="flex items-center gap-1.5 mb-1.5">
                <i class="fas fa-chart-line text-[10px] text-slate-400"></i>
                <span class="text-[10px] text-slate-400 uppercase tracking-wider">市场概况</span>
              </div>
              <div class="flex flex-wrap gap-1.5">
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-[#00d084]/10 text-[#00d084]">
                  <i class="fas fa-arrow-up text-[8px]"></i>
                  涨停{{ metrics.limitUp ?? 'unavailable' }}家
                </span>
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-[#ff4757]/10 text-[#ff4757]">
                  <i class="fas fa-arrow-down text-[8px]"></i>
                  跌停{{ metrics.limitDown ?? 'unavailable' }}家
                </span>
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-[#ffa502]/10 text-[#ffa502]">
                  <i class="fas fa-layer-group text-[8px]"></i>
                  最高{{ metrics.maxHeight }}
                </span>
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-[#6366f1]/10 text-[#6366f1]">
                  <i class="fas fa-percentage text-[8px]"></i>
                  炸板{{ metrics.brokenRatio }}
                </span>
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px]" :class="sentimentTagClass">
                  <i class="fas fa-thermometer-half text-[8px]"></i>
                  {{ sentimentText }}
                </span>
              </div>
            </div>

            <!-- 题材标签 -->
            <div>
              <div class="flex items-center gap-1.5 mb-1.5">
                <i class="fas fa-fire text-[10px] text-slate-400"></i>
                <span class="text-[10px] text-slate-400 uppercase tracking-wider">题材热点</span>
              </div>
              <div class="flex flex-wrap gap-1.5">
                <span
                  v-for="theme in hotThemes"
                  :key="theme.name"
                  @click="filterByTheme(theme.name)"
                  class="inline-flex items-center gap-1 px-2 py-1 rounded-full text-[10px] cursor-pointer transition-all"
                  :class="activeTheme === theme.name ? 'bg-blue-500/20 text-blue-400' : 'bg-[#1a1a1a] text-slate-400 hover:bg-[#252525]'"
                  :style="{ color: activeTheme === theme.name ? theme.color : '' }"
                >
                  <i class="fas" :class="theme.icon" :style="{ color: theme.color }"></i>
                  {{ theme.name }}
                </span>
                <span v-if="hotThemes.length === 0" class="text-[10px] text-slate-500">暂无主题</span>
              </div>
            </div>

            <!-- 操作按钮 -->
            <div class="flex gap-2 pt-1">
              <button @click="fetchBrief" class="flex-1 h-7 flex items-center justify-center gap-1.5 bg-[#1a1a1a] hover:bg-[#252525] text-slate-300 rounded-full text-[10px] transition-all">
                <i class="fas fa-sync-alt" :class="{ 'fa-spin': isLoadingBrief }"></i>
                <span>刷新简报</span>
              </button>
              <button
                @click="confirmAndPush"
                :disabled="isPushing"
                class="flex-1 h-7 flex items-center justify-center gap-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-full text-[10px] transition-all shadow-lg shadow-blue-500/20 disabled:opacity-50"
              >
                <i class="fas" :class="isPushing ? 'fa-spinner fa-spin' : 'fa-paper-plane'"></i>
                <span>推送飞书</span>
              </button>
            </div>
          </div>
        </div>

        <!-- 题材流向图 -->
        <div class="bg-[#111] rounded-lg flex-1 min-h-0 flex flex-col">
          <div class="px-3 py-2 flex items-center justify-between border-b border-[#1a1a1a]">
            <div class="flex items-center gap-2">
              <span class="text-xs font-medium text-white">题材流向路径图</span>
              <span class="text-[10px] text-slate-500">{{ chartStartDate?.slice(5) }} → {{ chartEndDate?.slice(5) }}</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="flex items-center gap-1 text-[10px] text-slate-400">
                <i class="fas fa-circle text-[6px]" style="color: #00FF88"></i>
                主流
              </span>
              <span class="flex items-center gap-1 text-[10px] text-slate-400">
                <i class="fas fa-circle text-[6px]" style="color: #787B86"></i>
                普通
              </span>
            </div>
          </div>
          <div class="flex-1 min-h-0">
            <ThemeFlowChart
              ref="themeFlowChartRef"
              :data="themeFlowData"
              :loading="flowLoading"
            />
          </div>
          <div class="px-3 py-2 border-t border-[#1a1a1a]">
            <div class="grid grid-cols-3 gap-2 text-[10px]">
              <div class="flex items-center gap-1.5 text-slate-400">
                <i class="fas fa-arrow-right text-[#00d084]"></i>
                <span>龙头引领</span>
              </div>
              <div class="flex items-center gap-1.5 text-slate-400">
                <i class="fas fa-arrows-alt text-[#ffa502]"></i>
                <span>跟风跟进</span>
              </div>
              <div class="flex items-center gap-1.5 text-slate-400">
                <i class="fas fa-expand text-[#ff6b81]"></i>
                <span>扩散补涨</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 趋势图 -->
        <div class="bg-[#111] rounded-lg h-[140px] flex flex-col">
          <div class="px-3 py-2 flex items-center justify-between border-b border-[#1a1a1a]">
            <span class="text-xs font-medium text-white">涨停趋势分析</span>
            <div class="flex items-center gap-3">
              <span class="flex items-center gap-1 text-[10px] text-slate-400">
                <span class="w-2 h-2 rounded-sm bg-[#ef4444]"></span>
                涨停数
              </span>
              <span class="flex items-center gap-1 text-[10px] text-slate-400">
                <span class="w-2 h-2 rounded-sm bg-[#f59e0b]"></span>
                最高连板
              </span>
            </div>
          </div>
          <div class="flex-1 min-h-0">
            <LimitUpTrendChart
              :data="limitUpTrendData"
              :loading="chartsLoading"
            />
          </div>
        </div>

        <!-- 气泡矩阵 -->
        <div class="bg-[#111] rounded-lg h-[140px] flex flex-col">
          <div class="px-3 py-2 flex items-center justify-between border-b border-[#1a1a1a]">
            <span class="text-xs font-medium text-white">涨停强度矩阵</span>
            <span class="text-[10px] text-slate-500">来源：本地结构化证据</span>
          </div>
          <div class="flex-1 min-h-0">
            <BubbleMatrixChart
              :data="bubbleMatrixData"
              :loading="bubbleLoading"
            />
          </div>
        </div>
      </div>
    </div>

    <!-- 日志面板 -->
    <div v-if="showLogs" class="fixed bottom-0 left-0 right-0 h-48 bg-[#111] border-t border-[#1a1a1a] z-50">
      <div class="h-8 px-3 flex items-center justify-between border-b border-[#1a1a1a]">
        <span class="text-xs font-medium text-white flex items-center gap-2">
          <i class="fas fa-terminal text-slate-400"></i>
          实时日志
        </span>
        <div class="flex gap-2">
          <button @click="clearLogs" class="text-[10px] text-slate-500 hover:text-white transition-colors">清空</button>
          <button @click="showLogs = false" class="text-[10px] text-slate-500 hover:text-white transition-colors">收起</button>
        </div>
      </div>
      <div ref="logContainer" class="h-40 overflow-y-auto p-3 space-y-1 font-mono text-[10px]">
        <div v-for="(log, index) in logs" :key="index" class="flex gap-2">
          <span class="text-slate-600 shrink-0">{{ formatTime(log.timestamp) }}</span>
          <span class="shrink-0 w-14 text-right" :class="{
            'text-blue-400': log.level === 'info',
            'text-green-400': log.level === 'success',
            'text-yellow-400': log.level === 'warning',
            'text-red-400': log.level === 'error'
          }">[{{ log.level.toUpperCase() }}]</span>
          <span class="text-slate-300 break-all">{{ log.message }}</span>
        </div>
        <div v-if="logs.length === 0" class="text-slate-600 text-center py-4">等待任务启动...</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * DragonEyePage - 龙虎榜监控页
 *
 * T2.2 改造说明:
 *  - 统一调用 request() / apiService, 不再直接用 axios
 *  - 后端返回 {success, data, meta, error}; request() 已解包 data, 暴露 meta
 *  - 错误统一走 useErrorHandler + ErrorState, 不再 alert
 *  - 启动抓取走 POST /api/research/research/dragon/crawl 拿 job_id, 再走 SSE /api/research/research/dragon/stream/<job_id>
 *  - 兼容后端新 error 事件 {type:'error', data:{code, message, detail, retryable}}
 *  - 无数据时 EmptyState 展示当前查询日期 / 最新日期 / 缺失组件 / 建议命令
 *  - 失败按 code 分类建议 (DRAGON_EYE_DATA_MISSING / NETWORK_ERROR / TIMEOUT 等)
 *  - 5 个数据看板卡片缺数据时显示 "—" + 灰色"无本地证据"角标
 */
import { ref, computed, onMounted, watch, onUnmounted, nextTick } from 'vue';
import * as echarts from 'echarts';
import { request, ApiError } from '../services/api';
import { useErrorHandler } from '../composables/useErrorHandler';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingState from '../components/common/LoadingState.vue';
import EmptyState from '../components/common/EmptyState.vue';
import DebugButton from '../components/common/DebugButton.vue';

import LimitUpTrendChart from '../components/charts/LimitUpTrendChart.vue';
import BubbleMatrixChart from '../components/charts/BubbleMatrixChart.vue';
import ThemeFlowChart from '../components/charts/ThemeFlowChart.vue';

const { handleError, clearError } = useErrorHandler();

// =====================================================
// 状态定义
// =====================================================

/** 目标查询日期 (YYYY-MM-DD) */
const targetDate = ref('');

/** 顶部错误条 (聚合 useErrorHandler) */
const topLevelError = ref<{
  title: string;
  message: string;
  detail: string;
  status: number | string;
  code: string;
  endpoint: string;
  retryable: boolean;
  lastRequestTime: string;
} | null>(null);

/** 抓取 / 处理状态 */
const isCrawling = ref(false);
const isProcessing = ref(false);
const isPushing = ref(false);
const isLoadingBrief = ref(false);

/** 龙头股列表 */
const stocksLoading = ref(false);
const dragonStocks = ref<any[]>([]);

/** AI 简报 */
const aiBrief = ref('');
const aiBriefCollapsed = ref(false);
const activeTheme = ref<string | null>(null);

/** 证据面板 */
const evidenceLoading = ref(false);
const evidenceError = ref<{
  title: string;
  message: string;
  detail: string;
  status: number | string;
  code: string;
  endpoint: string;
  retryable: boolean;
} | null>(null);
const evidenceStatus = ref<any>(null);
const evidenceOnly = ref(false);

/** 指标 */
const metrics = ref({
  limitUp: null as number | null,
  limitDown: null as number | null,
  maxHeight: 'N/A',
  brokenRatio: 'N/A',
});

/** 图表 */
const sentimentChartRef = ref<HTMLElement | null>(null);
let sentimentChart: echarts.ECharts | null = null;
const themeFlowChartRef = ref<InstanceType<typeof ThemeFlowChart> | null>(null);
const chartsLoading = ref(false);
const bubbleLoading = ref(false);
const flowLoading = ref(false);
const limitUpTrendData = ref<any>(null);
const bubbleMatrixData = ref<any>(null);
const themeFlowData = ref<any>(null);
const chartStartDate = ref('');
const chartEndDate = ref('');

/** Job / Logs */
const activeJobId = ref<string | null>(null);
const lastJobId = ref<string | null>(null);
const jobStatus = ref<string>('');
const jobType = ref<string>('');
const jobProgress = ref(0);
const jobStartedAt = ref<number>(0);
const jobElapsedMs = ref(0);
const jobErrorMessage = ref('');
const jobErrorDetail = ref('');
const jobErrorCode = ref<string>('');
const jobStats = ref<{ stock_count?: number; sentiment_count?: number; theme_count?: number } | null>(null);
const logs = ref<any[]>([]);
const showLogs = ref(false);
const logContainer = ref<HTMLElement | null>(null);
let eventSource: EventSource | null = null;
let elapsedTimer: ReturnType<typeof setInterval> | null = null;

/** 热门题材 */
const hotThemes = ref<any[]>([]);

/** 最新日期 (从 /api/research/research/dragon/latest-date) */
const latestDate = ref('');

/** 情感分 (从 /api/research/research/dragon/sentiment/score) */
const sentimentScore = ref<number | null>(null);

/** 主题数 (themeFlowData nodes 长度) */
const themeCount = computed(() => {
  if (!themeFlowData.value) return null;
  const nodes = themeFlowData.value?.nodes;
  return Array.isArray(nodes) ? nodes.length : null;
});

/** 股票数 */
const stockCount = computed(() => dragonStocks.value.length);

/** 缺失组件 (从 evidenceStatus.missing_parts) */
const missingPartsList = computed<string[]>(() => {
  if (!evidenceStatus.value) return [];
  const raw = evidenceStatus.value.missing_parts;
  return Array.isArray(raw) ? raw : [];
});

/** 是否部分字段缺失 (用于 DebugButton partialSuccess) */
const hasMissingParts = computed(() => missingPartsList.value.length > 0);

/** 数据是否过期 (latestDate 与 targetDate 不一致视为过期) */
const isStale = computed(() => {
  if (!targetDate.value || !latestDate.value) return false;
  return targetDate.value !== latestDate.value;
});

// =====================================================
// 工具: 错误归一
// =====================================================

/**
 * 把 ApiError / ApiFailure 归一为 ErrorState 字段
 * - 顶层 ApiError (网络/超时) → 通过 useErrorHandler
 * - ApiFailure (业务 ok=false) → 直接组装
 */
function buildErrorPayload(input: {
  message: string;
  status?: number | string;
  code?: string;
  detail?: any;
  endpoint?: string;
  retryable?: boolean;
  defaultCode?: string;
}): {
  title: string;
  message: string;
  detail: string;
  status: number | string;
  code: string;
  endpoint: string;
  retryable: boolean;
  lastRequestTime: string;
} {
  const code = (input.code || input.defaultCode || 'INTERNAL_ERROR').toString();
  const status = input.status ?? '';
  return {
    title: pickErrorTitle(code, status),
    message: input.message,
    detail: stringifyDetail(input.detail),
    status,
    code,
    endpoint: input.endpoint || '',
    retryable: input.retryable ?? false,
    lastRequestTime: new Date().toISOString(),
  };
}

/** 不同 code 给出不同标题 */
function pickErrorTitle(code: string, status: number | string): string {
  const num = typeof status === 'number' ? status : parseInt(String(status || ''), 10);
  if (code === 'DRAGON_EYE_DATA_MISSING' || code === 'DATA_MISSING') return '数据缺失';
  if (code === 'DRAGON_EYE_NETWORK_ERROR' || code === 'NETWORK') return '网络错误';
  if (code === 'DRAGON_EYE_AUTH_ERROR') return '鉴权失败';
  if (code === 'DRAGON_EYE_TIMEOUT' || code === 'TIMEOUT') return '请求超时';
  if (code === 'DRAGON_EYE_JOB_NOT_FOUND') return '任务不存在';
  if (code === 'DRAGON_EYE_STREAM_ERROR') return 'SSE 流异常';
  if (code === 'DRAGON_EYE_BAD_REQUEST') return '请求参数错误';
  if (code === 'DRAGON_EYE_PUSH_FAILED') return '飞书推送失败';
  if (code === 'DRAGON_EYE_INTERNAL_ERROR') return '服务内部错误';
  if (num === 404) return '资源未找到';
  if (num === 500) return '服务器错误';
  if (num === 408) return '请求超时';
  return '加载失败';
}

/** 不同 code 给出不同建议 (任务失败时使用) */
function pickJobErrorSuggestion(code: string): string {
  if (code === 'DRAGON_EYE_DATA_MISSING') {
    return '确认目标日期是否交易日, 并检查本地结构化证据是否齐备';
  }
  if (code === 'DRAGON_EYE_NETWORK_ERROR') {
    return '检查外网/代理, 稍后重试';
  }
  if (code === 'DRAGON_EYE_TIMEOUT') {
    return '爬取目标可能较慢, 可调高客户端 timeout 或重试';
  }
  if (code === 'DRAGON_EYE_AUTH_ERROR') {
    return '检查抓取 cookie / token 是否过期';
  }
  if (code === 'DRAGON_EYE_JOB_NOT_FOUND') {
    return '任务已过期或被清理, 请重新启动';
  }
  if (code === 'DRAGON_EYE_STREAM_ERROR') {
    return 'SSE 链路中断, 点击重试或刷新页面';
  }
  if (code === 'DRAGON_EYE_BAD_REQUEST') {
    return '请求参数不合法, 检查日期格式';
  }
  if (code === 'DRAGON_EYE_PUSH_FAILED') {
    return '检查飞书 webhook / 鉴权, 稍后重试';
  }
  return '查看底部日志或服务日志, 定位失败原因';
}

function stringifyDetail(detail: any): string {
  if (detail == null || detail === '') return '';
  if (typeof detail === 'string') return detail;
  try {
    return JSON.stringify(detail, null, 2);
  } catch {
    return String(detail);
  }
}

/** 通用兜底: 把 request() 返回的 ApiResult 处理为 success/failure (基于 ApiResult<T> 判别) */
function unwrapResult<T>(
  res: { ok: boolean; data?: T; message?: string; code?: string; status?: number; detail?: any; retryable?: boolean; endpoint?: string }
): { ok: true; data: T } | { ok: false; error: ReturnType<typeof buildErrorPayload> } {
  if (res.ok === true) {
    return { ok: true, data: res.data as T };
  }
  return {
    ok: false,
    error: buildErrorPayload({
      message: res.message || '请求失败',
      status: res.status,
      code: res.code,
      detail: res.detail,
      endpoint: res.endpoint,
      retryable: res.retryable,
    }),
  };
}

// =====================================================
// 数据看板辅助
// =====================================================

function statValueClass(hasValue: boolean): string {
  return hasValue ? 'text-white' : 'text-slate-500';
}

function formatSentimentScore(score: number | null): string {
  if (score == null || !Number.isFinite(score)) return '—';
  return score.toFixed(4);
}

const statusLabel = computed(() => {
  if (activeJobId.value) return '抓取中';
  if (jobStatus.value === 'failed') return '失败';
  if (jobStatus.value === 'success' || jobStatus.value === 'completed') return '已完成';
  if (dragonStocks.value.length > 0) return '已就绪';
  if (latestDate.value) return '等待数据';
  return '无数据';
});

const statusValueClass = computed(() => {
  if (jobStatus.value === 'failed') return 'text-[#ff4757]';
  if (activeJobId.value) return 'text-blue-400';
  if (dragonStocks.value.length > 0) return 'text-[#00d084]';
  return 'text-slate-400';
});

const jobStatusLabel = computed(() => {
  if (jobStatus.value === 'failed') return '失败';
  if (jobStatus.value === 'success' || jobStatus.value === 'completed') return '完成';
  if (jobStatus.value === 'running') return '运行中';
  return jobStatus.value || '排队中';
});

const jobStatusClass = computed(() => {
  if (jobStatus.value === 'failed') return 'text-[#ff4757]';
  if (jobStatus.value === 'success' || jobStatus.value === 'completed') return 'text-[#00d084]';
  if (jobStatus.value === 'running') return 'text-blue-400';
  return 'text-slate-200';
});

const jobElapsedLabel = computed(() => {
  const ms = jobElapsedMs.value;
  if (!ms) return '—';
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
});

/** 最近 5 条日志 */
const recentJobLogs = computed(() => logs.value.slice(-5).reverse());

/** 任务失败建议 (按 code 给出) */
const jobErrorSuggestionText = computed(() => pickJobErrorSuggestion(jobErrorCode.value));

// =====================================================
// 证据面板: 无数据时的诊断信息
// =====================================================

const emptyDiagnosticsHint = computed(() => {
  const parts: string[] = [];
  parts.push(`当前查询日期: ${targetDate.value || '未指定'}`);
  parts.push(`DragonEye latest_date: ${latestDate.value || '暂无'}`);
  parts.push('已检查的数据目录: data/parquet_data, data/lancedb');
  if (missingPartsList.value.length) {
    parts.push(`实际缺失组件: ${missingPartsList.value.join('、')}`);
  } else {
    parts.push('实际缺失组件: 暂无');
  }
  parts.push('建议运行: python core/pipeline/quant_flow_pipeline.py');
  return parts.join(' | ');
});

const stocksEmptyTitle = computed(() => {
  if (!targetDate.value) return '请选择查询日期';
  if (!latestDate.value) return '暂无本地证据';
  return '暂无当日龙头个股数据';
});

const stocksEmptyDescription = computed(() => {
  if (!targetDate.value) return '尚未选择日期';
  return `目标日期 ${targetDate.value} 无股票数据`;
});

const stocksEmptyHint = computed(() => {
  const missing: string[] = [];
  if (!latestDate.value) missing.push('latest_date 缺失');
  if (missingPartsList.value.length) missing.push(`缺失组件: ${missingPartsList.value.join('、')}`);
  if (missing.length === 0) return '建议: 点击"启动抓取"或"一键全流程"获取最新数据';
  return `建议: ${missing.join('; ')}; 可执行 python core/pipeline/quant_flow_pipeline.py`;
});

// =====================================================
// API: 各种数据获取
// =====================================================

/** 获取数据库最新日期 */
const fetchLatestDate = async (): Promise<void> => {
  try {
    const res = await request<{ latest_date?: string | null; has_data?: boolean }>(
      '/research/dragon/latest-date',
      { method: 'GET', meta: { kind: 'dragonEyeLatestDate' } }
    );
    const unwrapped = unwrapResult(res);
    if (unwrapped.ok) {
      const data = unwrapped.data as { latest_date?: string | null } | null | undefined;
      const ld = data?.latest_date;
      targetDate.value = (ld && String(ld)) || '';
    } else {
      // 静默降级: 不阻塞主流程, targetDate 留空即可
      targetDate.value = '';
      // 不在顶部红条展示, 避免噪音; evidenceError 会兜住
    }
  } catch (e) {
    targetDate.value = '';
    // 网络/超时归到顶部错误条
    const err = e instanceof ApiError ? e : new Error(String(e));
    topLevelError.value = buildErrorPayload({
      message: err.message || '最新日期加载失败',
      code: (err as ApiError).code,
      status: (err as ApiError).status,
      detail: (err as ApiError).detail,
      endpoint: (err as ApiError).endpoint,
      retryable: (err as ApiError).retryable ?? true,
      defaultCode: 'NETWORK',
    });
  }
};

/** 证据完整度 */
const fetchEvidenceStatus = async (): Promise<void> => {
  evidenceLoading.value = true;
  evidenceError.value = null;
  try {
    const res = await request<any>('/quant-flow/latest', {
      method: 'GET',
      meta: { kind: 'quantFlowLatest' },
    });
    const unwrapped = unwrapResult(res);
    if (!unwrapped.ok) {
      evidenceError.value = unwrapped.error;
      evidenceStatus.value = null;
      return;
    }
    const report = unwrapped.data;
    const stage = report?.stages?.find((item: any) => item.stage === 'dragon_eye_summary');
    const stageData = stage?.data;
    if (!stageData) {
      evidenceStatus.value = null;
      evidenceOnly.value = false;
      return;
    }
    const partial = stageData.latest_partial_status;
    evidenceStatus.value = partial?.evidence_date === report.global_latest_trade_date
      ? { ...partial, partial_success: true }
      : stageData;
    evidenceOnly.value = ['EVIDENCE_ONLY', 'evidence_only'].includes(report?.final_brief?.data?.research_output_level);
    const selectedCurrentEvidence = evidenceStatus.value === stageData
      && stageData.evidence_date === report.global_latest_trade_date;
    const themes = selectedCurrentEvidence
      ? stageData.evidence?.sector_heat_stats?.data
      : [];
    hotThemes.value = Array.isArray(themes)
      ? themes.slice(0, 8).map((item: any) => ({
          name: item.name,
          icon: 'fa-circle',
          color: '#787B86',
        }))
      : [];
  } catch (e) {
    evidenceStatus.value = null;
    const err = e instanceof ApiError ? e : null;
    evidenceError.value = buildErrorPayload({
      message: err?.message || '证据完整度加载失败',
      code: err?.code,
      status: err?.status,
      detail: err?.detail,
      endpoint: err?.endpoint,
      retryable: err?.retryable ?? true,
      defaultCode: 'DRAGON_EYE_INTERNAL_ERROR',
    });
  } finally {
    evidenceLoading.value = false;
  }
};

/** 龙头股 */
const fetchStocks = async (): Promise<void> => {
  if (!targetDate.value) {
    dragonStocks.value = [];
    return;
  }
  stocksLoading.value = true;
  try {
    const params = new URLSearchParams({
      start_date: targetDate.value,
      end_date: targetDate.value,
    });
    const res = await request<any[] | null>(`/research/dragon/stocks?${params.toString()}`, {
      method: 'GET',
      meta: { kind: 'dragonStocks', date: targetDate.value },
    });
    const unwrapped = unwrapResult(res);
    if (!unwrapped.ok) {
      // 不弹顶部红条, 走 EmptyState 展示
      dragonStocks.value = [];
      handleError(new ApiError(unwrapped.error.message, unwrapped.error.status as number, unwrapped.error.code, {
        retryable: unwrapped.error.retryable,
        endpoint: unwrapped.error.endpoint,
        detail: unwrapped.error.detail,
      }));
      return;
    }
    const data = unwrapped.data;
    dragonStocks.value = Array.isArray(data) ? data : [];
    if (dragonStocks.value.length > 0) {
      const maxBoard = Math.max(...dragonStocks.value.map((s: any) => Number(s.continue_num) || 0));
      metrics.value.maxHeight = `${maxBoard}板`;
    }
  } catch (e) {
    dragonStocks.value = [];
    const err = e instanceof ApiError ? e : new ApiError(String(e), 0, 'NETWORK', { retryable: true });
    handleError(err);
  } finally {
    stocksLoading.value = false;
  }
};

/** AI 简报 */
const fetchBrief = async (): Promise<void> => {
  if (!targetDate.value) {
    aiBrief.value = '';
    return;
  }
  isLoadingBrief.value = true;
  try {
    const params = new URLSearchParams({ date: targetDate.value });
    const res = await request<{ content?: string; has_data?: boolean } | null>(`/research/dragon/brief?${params.toString()}`, {
      method: 'GET',
      meta: { kind: 'dragonBrief', date: targetDate.value },
    });
    const unwrapped = unwrapResult(res);
    if (!unwrapped.ok) {
      aiBrief.value = '';
      handleError(new ApiError(unwrapped.error.message, unwrapped.error.status as number, unwrapped.error.code, {
        retryable: unwrapped.error.retryable,
        endpoint: unwrapped.error.endpoint,
        detail: unwrapped.error.detail,
      }));
      return;
    }
    aiBrief.value = (unwrapped.data && unwrapped.data.content) || '';
  } catch (e) {
    aiBrief.value = '';
    const err = e instanceof ApiError ? e : new ApiError(String(e), 0, 'NETWORK', { retryable: true });
    handleError(err);
  } finally {
    isLoadingBrief.value = false;
  }
};

/** 情绪历史 */
const fetchSentimentHistory = async (): Promise<void> => {
  try {
    const end = targetDate.value;
    if (!end) return;
    const start = new Date(new Date(end).getTime() - 15 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];
    const params = new URLSearchParams({ start_date: start, end_date: end });
    const res = await request<any[] | null>(`/research/dragon/sentiment?${params.toString()}`, {
      method: 'GET',
      meta: { kind: 'dragonSentiment', start, end },
    });
    const unwrapped = unwrapResult(res);
    if (!unwrapped.ok) {
      return; // 静默降级: 图表区暂不画
    }
    const data = Array.isArray(unwrapped.data) ? unwrapped.data : [];
    updateSentimentChart(data);
  } catch (e) {
    // 静默: 图表容错
    console.error('[DragonEye] sentiment history failed:', e);
  }
};

/** 涨停趋势 */
const fetchLimitUpTrend = async (): Promise<void> => {
  if (!chartStartDate.value || !chartEndDate.value) return;
  chartsLoading.value = true;
  try {
    const params = new URLSearchParams({
      start_date: chartStartDate.value,
      end_date: chartEndDate.value,
    });
    const res = await request<any>(`/research/dragon/limit-up-trend?${params.toString()}`, {
      method: 'GET',
      meta: { kind: 'dragonLimitUpTrend' },
    });
    const unwrapped = unwrapResult(res);
    if (unwrapped.ok) {
      limitUpTrendData.value = unwrapped.data;
    } else {
      limitUpTrendData.value = null;
    }
  } catch (e) {
    limitUpTrendData.value = null;
    console.error('[DragonEye] limit-up-trend failed:', e);
  } finally {
    chartsLoading.value = false;
  }
};

/** 气泡矩阵 */
const fetchBubbleMatrix = async (): Promise<void> => {
  if (!targetDate.value) return;
  bubbleLoading.value = true;
  try {
    const params = new URLSearchParams({ date: targetDate.value });
    const res = await request<any>(`/research/dragon/bubble-matrix?${params.toString()}`, {
      method: 'GET',
      meta: { kind: 'dragonBubbleMatrix', date: targetDate.value },
    });
    const unwrapped = unwrapResult(res);
    if (unwrapped.ok) {
      bubbleMatrixData.value = unwrapped.data;
    } else {
      bubbleMatrixData.value = null;
    }
  } catch (e) {
    bubbleMatrixData.value = null;
    console.error('[DragonEye] bubble-matrix failed:', e);
  } finally {
    bubbleLoading.value = false;
  }
};

/** 题材流向 */
const fetchThemeFlow = async (): Promise<void> => {
  if (!chartStartDate.value || !chartEndDate.value) return;
  flowLoading.value = true;
  try {
    const params = new URLSearchParams({
      start_date: chartStartDate.value,
      end_date: chartEndDate.value,
    });
    const res = await request<any>(`/research/dragon/theme-flow?${params.toString()}`, {
      method: 'GET',
      meta: { kind: 'dragonThemeFlow' },
    });
    const unwrapped = unwrapResult(res);
    if (unwrapped.ok) {
      themeFlowData.value = unwrapped.data;
    } else {
      themeFlowData.value = null;
    }
  } catch (e) {
    themeFlowData.value = null;
    console.error('[DragonEye] theme-flow failed:', e);
  } finally {
    flowLoading.value = false;
  }
};

/** 情感评分 (供数据看板) */
const fetchSentimentScore = async (): Promise<void> => {
  if (!targetDate.value) {
    sentimentScore.value = null;
    return;
  }
  try {
    const params = new URLSearchParams({ date: targetDate.value });
    const res = await request<any>(`/research/dragon/sentiment/score?${params.toString()}`, {
      method: 'GET',
      meta: { kind: 'dragonSentimentScore', date: targetDate.value },
    });
    const unwrapped = unwrapResult(res);
    if (unwrapped.ok && unwrapped.data && typeof unwrapped.data === 'object') {
      const data = unwrapped.data as Record<string, any>;
      const candidate = data.summary ?? data.score ?? data.value ?? data.sentiment_score;
      if (typeof candidate === 'number' && Number.isFinite(candidate)) {
        sentimentScore.value = candidate;
        return;
      }
    }
    sentimentScore.value = null;
  } catch (e) {
    sentimentScore.value = null;
    console.error('[DragonEye] sentiment score failed:', e);
  }
};

// =====================================================
// 图表
// =====================================================

const updateSentimentChart = (data: any[]): void => {
  if (!sentimentChart) return;
  if (!Array.isArray(data) || data.length === 0) {
    sentimentChart.clear();
    return;
  }
  const option = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '10%', containLabel: true },
    xAxis: {
      type: 'category',
      data: data.map(d => String(d.trade_date || '').slice(5)),
      axisLine: { lineStyle: { color: '#1a1a1a' } },
      axisLabel: { color: '#64748b', fontSize: 9 }
    },
    yAxis: [
      {
        type: 'value',
        min: 0,
        axisLabel: { color: '#64748b', fontSize: 9 },
        splitLine: { lineStyle: { color: '#1a1a1a' } }
      },
      {
        type: 'value',
        max: 1,
        axisLabel: { color: '#64748b', fontSize: 9, formatter: (v: number) => (v * 100).toFixed(0) + '%' },
        splitLine: { show: false }
      }
    ],
    series: [
      {
        name: '最高板',
        type: 'line',
        data: data.map(d => d.max_height),
        smooth: true,
        lineStyle: { color: '#00d084', width: 2 },
        symbol: 'none',
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(0, 208, 132, 0.2)' }, { offset: 1, color: 'transparent' }]) }
      },
      {
        name: '炸板率',
        type: 'bar',
        yAxisIndex: 1,
        data: data.map(d => d.broken_ratio),
        itemStyle: { color: '#6366f1', borderRadius: [1, 1, 0, 0] },
        barWidth: '30%'
      }
    ]
  };
  sentimentChart.setOption(option);
};

// =====================================================
// 题材筛选
// =====================================================

const filterByTheme = (theme: string): void => {
  if (activeTheme.value === theme) {
    activeTheme.value = null;
    themeFlowChartRef.value?.highlightTheme(null);
  } else {
    activeTheme.value = theme;
    themeFlowChartRef.value?.highlightTheme(theme);
  }
};

// =====================================================
// 任务: 启动 / SSE / 状态
// =====================================================

function startElapsedTimer(): void {
  stopElapsedTimer();
  jobStartedAt.value = Date.now();
  jobElapsedMs.value = 0;
  elapsedTimer = setInterval(() => {
    if (jobStartedAt.value > 0) {
      jobElapsedMs.value = Date.now() - jobStartedAt.value;
    }
  }, 250);
}

function stopElapsedTimer(): void {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

/** 重置 job 状态 (启动新任务前调用) */
function resetJobState(): void {
  logs.value = [];
  jobProgress.value = 0;
  jobStatus.value = '';
  jobErrorMessage.value = '';
  jobErrorDetail.value = '';
  jobErrorCode.value = '';
  jobStats.value = null;
  jobElapsedMs.value = 0;
}

const startCrawl = async (): Promise<void> => {
  isCrawling.value = true;
  showLogs.value = true;
  resetJobState();
  topLevelError.value = null;
  clearError();
  try {
    const res = await request<{ job_id?: string; status?: string; message?: string }>('/research/dragon/crawl', {
      method: 'POST',
      body: JSON.stringify({ date: targetDate.value }),
      meta: { kind: 'dragonCrawl' },
    });
    const unwrapped = unwrapResult(res);
    if (!unwrapped.ok) {
      isCrawling.value = false;
      topLevelError.value = unwrapped.error;
      handleError(new ApiError(unwrapped.error.message, unwrapped.error.status as number, unwrapped.error.code, {
        retryable: unwrapped.error.retryable,
        endpoint: unwrapped.error.endpoint,
        detail: unwrapped.error.detail,
      }));
      return;
    }
    const data = unwrapped.data || ({} as any);
    if (!data.job_id) {
      isCrawling.value = false;
      topLevelError.value = buildErrorPayload({
        message: '启动抓取失败: 后端未返回 job_id',
        code: 'DRAGON_EYE_INTERNAL_ERROR',
        endpoint: '/research/dragon/crawl',
        retryable: true,
      });
      return;
    }
    activeJobId.value = data.job_id;
    lastJobId.value = data.job_id;
    jobType.value = 'crawl';
    jobStatus.value = data.status || 'running';
    startElapsedTimer();
    connectToStream(data.job_id);
  } catch (e) {
    isCrawling.value = false;
    const err = e instanceof ApiError ? e : new ApiError(String(e), 0, 'NETWORK', { retryable: true });
    topLevelError.value = buildErrorPayload({
      message: err.message,
      code: err.code,
      status: err.status,
      detail: err.detail,
      endpoint: err.endpoint,
      retryable: err.retryable,
    });
  }
};

const startPipeline = async (): Promise<void> => {
  isProcessing.value = true;
  showLogs.value = true;
  resetJobState();
  topLevelError.value = null;
  clearError();
  try {
    const res = await request<{ job_id?: string; status?: string; message?: string }>('/research/dragon/pipeline', {
      method: 'POST',
      body: JSON.stringify({ date: targetDate.value, push_feishu: true }),
      meta: { kind: 'dragonPipeline' },
    });
    const unwrapped = unwrapResult(res);
    if (!unwrapped.ok) {
      isProcessing.value = false;
      topLevelError.value = unwrapped.error;
      handleError(new ApiError(unwrapped.error.message, unwrapped.error.status as number, unwrapped.error.code, {
        retryable: unwrapped.error.retryable,
        endpoint: unwrapped.error.endpoint,
        detail: unwrapped.error.detail,
      }));
      return;
    }
    const data = unwrapped.data || ({} as any);
    if (!data.job_id) {
      isProcessing.value = false;
      topLevelError.value = buildErrorPayload({
        message: '启动工作流失败: 后端未返回 job_id',
        code: 'DRAGON_EYE_INTERNAL_ERROR',
        endpoint: '/research/dragon/pipeline',
        retryable: true,
      });
      return;
    }
    activeJobId.value = data.job_id;
    lastJobId.value = data.job_id;
    jobType.value = 'full_pipeline';
    jobStatus.value = data.status || 'running';
    startElapsedTimer();
    connectToStream(data.job_id);
  } catch (e) {
    isProcessing.value = false;
    const err = e instanceof ApiError ? e : new ApiError(String(e), 0, 'NETWORK', { retryable: true });
    topLevelError.value = buildErrorPayload({
      message: err.message,
      code: err.code,
      status: err.status,
      detail: err.detail,
      endpoint: err.endpoint,
      retryable: err.retryable,
    });
  }
};

/** SSE 订阅 */
const connectToStream = (jobId: string): void => {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  eventSource = new EventSource(`/api/research/research/dragon/stream/${jobId}`);

  eventSource.onmessage = (event) => {
    let payload: any;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      console.error('[DragonEye] SSE invalid json:', err, event.data);
      return;
    }
    handleStreamData(payload);
  };

  eventSource.onerror = () => {
    // 浏览器原生 onerror 触发, SSE 通常会自动重连
    // 若后端关闭, 后续 complete/error 事件不会再发; 此处仅做日志
    console.warn('[DragonEye] SSE connection error (browser will retry)');
  };
};

/**
 * 处理 SSE 事件
 * 后端事件结构 (server/routes/dragon_eye_routes.py):
 *  - {type: 'connected', job_id, data: null}
 *  - {type: 'log', data: {timestamp, level, message, ...}}
 *  - {type: 'heartbeat', data: null}
 *  - {type: 'complete', status, progress?, data: null}
 *  - {type: 'error', data: {code, message, detail, retryable}}
 */
const handleStreamData = (data: any): void => {
  const type = data?.type;
  if (!type) return;
  switch (type) {
    case 'connected': {
      jobStatus.value = 'running';
      break;
    }
    case 'heartbeat': {
      // 心跳, 静默
      break;
    }
    case 'log': {
      const entry = data.data;
      if (entry && typeof entry === 'object') {
        logs.value.push(entry);
        // 简易进度解析: 消息中包含 "%N" 形式
        if (typeof entry.message === 'string') {
          const match = entry.message.match(/(\d+)%/);
          if (match) {
            const v = parseInt(match[1], 10);
            if (Number.isFinite(v) && v >= 0 && v <= 100) {
              jobProgress.value = v;
            }
          }
        }
        scrollToBottom();
      }
      break;
    }
    case 'complete': {
      jobStatus.value = data.status || 'success';
      const p = Number(data.progress);
      if (Number.isFinite(p) && p >= 0 && p <= 100) {
        jobProgress.value = p;
      } else {
        jobProgress.value = 100;
      }
      // complete 时尝试从 data.data / data.stats 解析 stats
      const stats = data.stats || data.data?.stats || null;
      if (stats && typeof stats === 'object') {
        jobStats.value = {
          stock_count: stats.stock_count ?? stats.stocks ?? undefined,
          sentiment_count: stats.sentiment_count ?? stats.sentiments ?? undefined,
          theme_count: stats.theme_count ?? stats.themes ?? undefined,
        };
      }
      activeJobId.value = null;
      isCrawling.value = false;
      isProcessing.value = false;
      stopElapsedTimer();
      eventSource?.close();
      eventSource = null;
      // 任务完成后刷新各视图
      fetchStocks();
      fetchBrief();
      fetchSentimentHistory();
      fetchSentimentScore();
      fetchEvidenceStatus();
      break;
    }
    case 'error': {
      const errPayload = data.data || {};
      jobStatus.value = 'failed';
      jobErrorCode.value = errPayload.code || 'DRAGON_EYE_STREAM_ERROR';
      jobErrorMessage.value = errPayload.message || '任务执行失败';
      jobErrorDetail.value = errPayload.detail || '';
      jobStats.value = null;
      activeJobId.value = null;
      isCrawling.value = false;
      isProcessing.value = false;
      stopElapsedTimer();
      eventSource?.close();
      eventSource = null;
      break;
    }
    default:
      // 未知事件类型: 仅日志
      console.warn('[DragonEye] SSE unknown event type:', type, data);
  }
};

const scrollToBottom = (): void => {
  nextTick(() => {
    if (logContainer.value) {
      logContainer.value.scrollTop = logContainer.value.scrollHeight;
    }
  });
};

const clearLogs = (): void => {
  logs.value = [];
};

// =====================================================
// 飞书推送
// =====================================================

const confirmAndPush = async (): Promise<void> => {
  isPushing.value = true;
  topLevelError.value = null;
  try {
    const res = await request<any>('/research/dragon/push', {
      method: 'POST',
      body: JSON.stringify({ date: targetDate.value }),
      meta: { kind: 'dragonPush' },
    });
    const unwrapped = unwrapResult(res);
    if (!unwrapped.ok) {
      topLevelError.value = unwrapped.error;
      handleError(new ApiError(unwrapped.error.message, unwrapped.error.status as number, unwrapped.error.code, {
        retryable: unwrapped.error.retryable,
        endpoint: unwrapped.error.endpoint,
        detail: unwrapped.error.detail,
      }));
    } else {
      // 成功: 使用顶部错误条作为成功提示的替代; 这里只关闭 loading
    }
  } catch (e) {
    const err = e instanceof ApiError ? e : new ApiError(String(e), 0, 'NETWORK', { retryable: true });
    topLevelError.value = buildErrorPayload({
      message: err.message,
      code: err.code,
      status: err.status,
      detail: err.detail,
      endpoint: err.endpoint,
      retryable: err.retryable,
    });
  } finally {
    isPushing.value = false;
  }
};

// =====================================================
// 工具 / 格式化
// =====================================================

const formatTime = (timestamp: string | undefined | null): string => {
  if (!timestamp) return '';
  return new Date(timestamp).toLocaleTimeString('zh-CN', { hour12: false });
};

const formatAvailability = (value: unknown): string => typeof value === 'boolean' ? (value ? '是' : '否') : 'unavailable';
const formatScore = (value: unknown): string => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(4) : 'unavailable';
const formatMissingParts = (value: unknown): string => Array.isArray(value) ? (value.length ? value.join('、') : '无') : 'unavailable';
const formatYi = (value: unknown): string => typeof value === 'number' && Number.isFinite(value) ? `${(value / 1e8).toFixed(2)}亿` : 'unavailable';
const formatPercentValue = (value: unknown): string => typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)}%` : 'unavailable';

// =====================================================
// Computed
// =====================================================

const sentimentText = computed(() => {
  const limitUp = metrics.value.limitUp;
  const limitDown = metrics.value.limitDown;
  if (limitUp == null || limitDown == null || limitUp + limitDown <= 0) return 'unavailable';
  const ratio = limitUp / (limitUp + limitDown);
  if (ratio > 0.9) return '强势';
  if (ratio > 0.8) return '偏暖';
  if (ratio > 0.6) return '震荡';
  return '偏冷';
});

const evidenceMode = computed(() => {
  if (!evidenceStatus.value) return 'unavailable';
  const partial = evidenceStatus.value.partial_success || evidenceStatus.value.status === 'partial';
  if (evidenceOnly.value && partial) return 'evidence_only / partial_success';
  if (evidenceOnly.value) return 'evidence_only';
  if (partial) return 'partial_success';
  return 'complete';
});

const sentimentColor = computed(() => {
  const text = sentimentText.value;
  if (text === '强势') return 'text-[#00d084]';
  if (text === '偏暖') return 'text-[#00d084]';
  if (text === '震荡') return 'text-[#ffa502]';
  return 'text-[#ff4757]';
});

const sentimentTagClass = computed(() => {
  const text = sentimentText.value;
  if (text === '强势') return 'bg-[#00d084]/10 text-[#00d084]';
  if (text === '偏暖') return 'bg-[#00d084]/10 text-[#00d084]';
  if (text === '震荡') return 'bg-[#ffa502]/10 text-[#ffa502]';
  return 'bg-[#ff4757]/10 text-[#ff4757]';
});

// =====================================================
// 顶部错误条重试
// =====================================================

async function handleTopLevelRetry(): Promise<void> {
  topLevelError.value = null;
  clearError();
  await fetchLatestDate();
  await fetchEvidenceStatus();
  await fetchStocks();
  await fetchBrief();
  await fetchSentimentHistory();
  await fetchSentimentScore();
}

// =====================================================
// 生命周期
// =====================================================

onMounted(async () => {
  if (sentimentChartRef.value) {
    sentimentChart = echarts.init(sentimentChartRef.value);
    window.addEventListener('resize', () => sentimentChart?.resize());
  }

  // 先获取数据库最新日期
  await Promise.all([fetchLatestDate(), fetchEvidenceStatus()]);

  // 初始化日期范围
  if (targetDate.value) {
    const endDate = new Date(targetDate.value);
    const startDate = new Date(endDate.getTime() - 15 * 24 * 60 * 60 * 1000);
    chartStartDate.value = startDate.toISOString().split('T')[0];
    chartEndDate.value = targetDate.value;
  } else if (latestDate.value) {
    // 兜底: 若 latest-date 接口直接给出 latest_date, 补画图表范围
    const endDate = new Date(latestDate.value);
    const startDate = new Date(endDate.getTime() - 15 * 24 * 60 * 60 * 1000);
    chartStartDate.value = startDate.toISOString().split('T')[0];
    chartEndDate.value = latestDate.value;
  }

  // 拉取数据
  fetchStocks();
  fetchBrief();
  fetchSentimentHistory();
  fetchSentimentScore();
  fetchLimitUpTrend();
  fetchBubbleMatrix();
  fetchThemeFlow();
});

watch(targetDate, (val) => {
  if (!val) {
    dragonStocks.value = [];
    return;
  }
  const endDate = new Date(val);
  if (!isNaN(endDate.getTime())) {
    const startDate = new Date(endDate.getTime() - 15 * 24 * 60 * 60 * 1000);
    chartStartDate.value = startDate.toISOString().split('T')[0];
    chartEndDate.value = val;
  }
  fetchStocks();
  fetchBrief();
  fetchSentimentHistory();
  fetchSentimentScore();
  fetchBubbleMatrix();
});

onUnmounted(() => {
  sentimentChart?.dispose();
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  stopElapsedTimer();
});
</script>

<style scoped>
.dragon-eye-page {
  display: flex;
  flex-direction: column;
}

.main-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.dashboard-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin: 12px 12px 0;
}

.stat-card {
  position: relative;
  background: #111;
  border: 1px solid #1f1f1f;
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 64px;
}
.stat-card__label {
  font-size: 10px;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.stat-card__value {
  font-size: 18px;
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  line-height: 1.1;
}
.stat-card__badge {
  position: absolute;
  top: 8px;
  right: 8px;
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(100, 116, 139, 0.2);
  color: #94a3b8;
}
.stat-card__badge--live {
  background: rgba(59, 130, 246, 0.15);
  color: #93c5fd;
}

.evidence-panel {
  margin: 12px 12px 0;
  padding: 12px;
  border: 1px solid #242936;
  border-radius: 8px;
  color: #D1D4DC;
  background: #111;
}

.evidence-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.evidence-heading h2,
.evidence-heading p {
  margin: 0;
}

.evidence-heading h2 {
  font-size: 13px;
}

.evidence-heading p {
  margin-top: 3px;
  color: #64748b;
  font-size: 10px;
}

.evidence-mode {
  padding: 3px 7px;
  border-radius: 999px;
  color: #93c5fd;
  background: rgba(59, 130, 246, 0.12);
  font-size: 10px;
}

.evidence-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
}

.evidence-grid div {
  padding: 8px;
  border-radius: 6px;
  background: #0a0a0a;
}

.evidence-grid .wide {
  grid-column: span 2;
}

.evidence-grid dt {
  color: #64748b;
  font-size: 10px;
}

.evidence-grid dd {
  margin: 3px 0 0;
  overflow-wrap: anywhere;
  font-size: 12px;
}

.left-column,
.right-column {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
}

/* Custom scrollbar */
::-webkit-scrollbar {
  width: 4px;
  height: 4px;
}

::-webkit-scrollbar-track {
  background: transparent;
}

::-webkit-scrollbar-thumb {
  background: #333;
  border-radius: 2px;
}

::-webkit-scrollbar-thumb:hover {
  background: #444;
}

/* Collapsed state for AI brief */
.collapsed .tv-ai-brief-content {
  display: none;
}
</style>
