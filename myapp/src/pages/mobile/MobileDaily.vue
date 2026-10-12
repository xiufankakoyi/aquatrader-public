<template>
  <div class="mobile-daily">
    <!-- 下拉刷新指示器 -->
    <div
      v-if="isPulling"
      class="pull-indicator"
      :style="{ height: pullDistance + 'px' }"
      aria-live="polite"
    >
      <span v-if="pullDistance < 60">下拉刷新</span>
      <span v-else>释放刷新</span>
    </div>
    <div v-if="isRefreshing" class="refresh-indicator" aria-live="polite">刷新中…</div>

    <!-- 页面头部 -->
    <header class="daily-header">
      <div class="daily-header-main">
        <div class="daily-title-wrap">
          <i class="fa-solid fa-newspaper daily-header-icon" aria-hidden="true" />
          <div class="daily-title-text">
            <h1 class="daily-main-title">每日简报</h1>
            <p class="daily-date">{{ todayLabel }}</p>
          </div>
        </div>
        <div class="daily-header-meta">
          <span v-if="currentSource" class="daily-source">{{ currentSource }}</span>
          <span v-if="currentSubtitle" class="daily-subtitle">{{ currentSubtitle }}</span>
        </div>
      </div>
    </header>

    <!-- 标签切换 -->
    <nav class="daily-tabs" role="tablist" aria-label="日报类型">
      <button
        id="tab-daily"
        class="daily-tab"
        :class="{ 'tab-active': activeTab === 'daily' }"
        role="tab"
        :aria-selected="activeTab === 'daily'"
        aria-controls="panel-daily"
        @click="setTab('daily')"
      >
        <i class="fa-regular fa-sun" aria-hidden="true" />
        日报
      </button>
      <button
        id="tab-perilla"
        class="daily-tab"
        :class="{ 'tab-active': activeTab === 'perilla' }"
        role="tab"
        :aria-selected="activeTab === 'perilla'"
        aria-controls="panel-perilla"
        @click="setTab('perilla')"
      >
        <i class="fa-solid fa-leaf" aria-hidden="true" />
        紫苏叶扫描
      </button>
    </nav>

    <!-- 加载状态 -->
    <div v-if="loading" class="daily-loading">
      <a-spin size="large" />
      <span>加载中…</span>
    </div>

    <template v-else>
      <!-- 日报内容 -->
      <section
        v-if="activeTab === 'daily'"
        id="panel-daily"
        class="daily-panel"
        role="tabpanel"
        aria-labelledby="tab-daily"
      >
        <MobileDailySection
          :sections="parsedDaily.sections"
          :fallback-html="dailyData?.daily_brief ? renderBrief(dailyData.daily_brief) : ''"
          empty-icon="fa-regular fa-newspaper"
          empty-text="暂无日报数据"
        />
      </section>

      <!-- 紫苏叶扫描内容 -->
      <section
        v-if="activeTab === 'perilla'"
        id="panel-perilla"
        class="daily-panel"
        role="tabpanel"
        aria-labelledby="tab-perilla"
      >
        <MobileDailySection
          :sections="parsedPerilla.sections"
          :fallback-html="dailyData?.perilla_discovery ? renderBrief(dailyData.perilla_discovery) : ''"
          empty-icon="fa-solid fa-leaf"
          empty-text="暂无紫苏叶扫描数据"
        />
      </section>
    </template>

    <!-- 底部占位，防止固定导航栏遮挡 -->
    <div class="mobile-bottom-spacer" />
  </div>
</template>

<script setup lang="ts">
/**
 * MobileDaily - 移动端日报页
 *
 * 标签切换"日报"和"紫苏叶扫描"，将 Markdown 按二级标题拆分为卡片区块展示。
 * Tab 状态同步到 URL ?tab= 参数，支持直接分享与刷新保持。
 */
import { ref, computed, onMounted } from 'vue';
import { getDaily } from '../../api/mobile';
import type { DailyBrief } from '../../api/mobile';
import { useMobileGestures } from '../../composables/useMobileGestures';
import { renderMarkdown } from '../../utils/markdown';
import MobileDailySection, { type DailySection } from '../../components/mobile/MobileDailySection.vue';

type TabKey = 'daily' | 'perilla';

const TAB_QUERY_KEY = 'tab';
const VALID_TABS: TabKey[] = ['daily', 'perilla'];

const activeTab = ref<TabKey>('daily');
const loading = ref(true);
const dailyData = ref<DailyBrief | null>(null);

const { isRefreshing, pullDistance, isPulling } = useMobileGestures({
  onPullRefresh: async () => {
    await loadDaily();
  },
  onSwipeLeft: () => {
    setTab('perilla');
  },
  onSwipeRight: () => {
    setTab('daily');
  },
});

/** 今日日期标签，使用 Intl.DateTimeFormat 本地化输出。 */
const todayLabel = computed(() => {
  const d = new Date();
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
  }).format(d);
});

/** 当前选中标签的数据源名称 */
const currentSource = computed(() => {
  const section = getActiveBrief();
  if (section && typeof section === 'object' && 'source' in section) {
    return (section as { source?: string }).source;
  }
  return '';
});

/** 当前选中标签的副标题/生成时间 */
const currentSubtitle = computed(() => {
  const md = extractMarkdown(getActiveBrief());
  if (!md) return '';
  const match = md.match(/生成时间[:：]\s*(.+)/);
  return match ? `生成于 ${match[1].trim()}` : '';
});

/** 解析后的日报 */
const parsedDaily = computed(() => parseDailySections(dailyData.value?.daily_brief));

/** 解析后的紫苏叶扫描 */
const parsedPerilla = computed(() => parseDailySections(dailyData.value?.perilla_discovery));

/** 获取当前标签对应的原始简报数据 */
function getActiveBrief(): unknown {
  const key = activeTab.value === 'daily' ? 'daily_brief' : 'perilla_discovery';
  return dailyData.value?.[key as keyof DailyBrief];
}

/** 从 URL 查询参数恢复选中的标签 */
function readTabFromUrl(): TabKey {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get(TAB_QUERY_KEY);
  return VALID_TABS.includes(raw as TabKey) ? (raw as TabKey) : 'daily';
}

/** 切换标签并同步到 URL（不刷新页面）。 */
function setTab(tab: TabKey) {
  if (activeTab.value === tab) return;
  activeTab.value = tab;

  const url = new URL(window.location.href);
  url.searchParams.set(TAB_QUERY_KEY, tab);
  window.history.replaceState(null, '', url.toString());
}

/** 加载日报数据 */
async function loadDaily() {
  loading.value = true;
  try {
    const res = await getDaily();
    dailyData.value = res?.data || res;
  } catch (err) {
    console.error('[MobileDaily] 加载日报失败:', err);
  } finally {
    loading.value = false;
  }
}

/**
 * 从后端返回的字符串或 {markdown, html} 对象中提取 Markdown 原文。
 */
function extractMarkdown(brief: unknown): string {
  if (!brief) return '';
  if (typeof brief === 'string') return brief;
  const obj = brief as { markdown?: string; html?: string };
  return obj.markdown || obj.html || '';
}

interface ParsedDaily {
  title: string;
  sections: DailySection[];
}

/**
 * 将 Markdown 文本解析为标题和卡片区块数组。
 * 按二级标题（##）拆分，自动识别图标与强调色。
 */
function parseDailySections(brief: unknown): ParsedDaily {
  const md = extractMarkdown(brief);
  if (!md) return { title: '', sections: [] };

  const lines = md.split('\n');
  let title = '';
  const sections: DailySection[] = [];
  let current: DailySection | null = null;

  const pushCurrent = () => {
    if (current) sections.push(current);
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;

    if (line.startsWith('# ')) {
      title = line.replace(/^#\s+/, '');
      continue;
    }

    if (line.startsWith('## ')) {
      pushCurrent();
      const sectionTitle = line.replace(/^##\s+/, '');
      const meta = sectionMeta(sectionTitle);
      current = {
        id: `section-${sections.length}`,
        title: sectionTitle,
        icon: meta.icon,
        accent: meta.accent,
        content: '',
        items: [],
      };
      continue;
    }

    if (!current) continue;

    if (line.startsWith('- ')) {
      current.items.push(line.replace(/^-\s+/, ''));
    } else {
      current.content += `${line}\n`;
    }
  }
  pushCurrent();

  return { title, sections };
}

/**
 * 根据区块标题关键词匹配图标与强调色。
 */
function sectionMeta(title: string): { icon: string; accent: string } {
  const t = title.toLowerCase();
  if (t.includes('新词') || t.includes('雷达')) {
    return { icon: 'fa-solid fa-bolt', accent: 'amber' };
  }
  if (t.includes('概念')) {
    return { icon: 'fa-solid fa-lightbulb', accent: 'gold' };
  }
  if (t.includes('证据') || t.includes('边')) {
    return { icon: 'fa-solid fa-diagram-project', accent: 'blue' };
  }
  if (t.includes('任务')) {
    return { icon: 'fa-solid fa-list-check', accent: 'purple' };
  }
  if (t.includes('统计')) {
    return { icon: 'fa-solid fa-chart-pie', accent: 'teal' };
  }
  if (t.includes('公司') || t.includes('标的')) {
    return { icon: 'fa-solid fa-building', accent: 'cyan' };
  }
  if (t.includes('说明') || t.includes('免责')) {
    return { icon: 'fa-solid fa-circle-info', accent: 'slate' };
  }
  if (t.includes('日报') || t.includes('简报')) {
    return { icon: 'fa-solid fa-newspaper', accent: 'indigo' };
  }
  return { icon: 'fa-solid fa-folder-open', accent: 'slate' };
}

/**
 * 渲染日报/报告内容。
 * 兼容后端返回的字符串和 {markdown, html} 对象。
 */
function renderBrief(brief: unknown): string {
  if (!brief) return '';
  if (typeof brief === 'string') {
    return renderMarkdown(brief);
  }
  const obj = brief as { html?: string; markdown?: string };
  const html = obj.html || '';
  const md = obj.markdown || '';
  if (html && /<[^>]+>/.test(html)) {
    return html;
  }
  return renderMarkdown(md || html);
}

onMounted(() => {
  activeTab.value = readTabFromUrl();
  loadDaily();
});
</script>

<style scoped>
.mobile-daily {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 80px;
}

/* 页面头部 */
.daily-header {
  background: linear-gradient(145deg, rgba(41, 98, 255, 0.12), rgba(76, 127, 255, 0.06));
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 14px;
  padding: 16px;
}

.daily-header-main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}

.daily-title-wrap {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.daily-title-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.daily-header-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: rgba(76, 127, 255, 0.18);
  color: #4c7fff;
  font-size: 18px;
  flex-shrink: 0;
}

.daily-main-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
  line-height: 1.3;
  text-wrap: balance;
}

.daily-date {
  font-size: 12px;
  color: var(--text-muted);
  margin: 0;
}

.daily-header-meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
  flex-shrink: 0;
  max-width: 45%;
}

.daily-source {
  font-size: 10px;
  color: var(--text-muted);
  padding: 4px 8px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.04);
  text-align: right;
  word-break: break-word;
}

.daily-subtitle {
  margin: 0;
  font-size: 10px;
  color: var(--text-secondary);
  text-align: right;
  word-break: break-word;
}

/* 标签切换 */
.daily-tabs {
  display: flex;
  gap: 4px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 12px;
  padding: 4px;
}

.daily-tab {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px;
  font-size: 13px;
  font-weight: 500;
  text-align: center;
  border: none;
  border-radius: 9px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: background-color 180ms ease, color 180ms ease, transform 120ms ease;
  touch-action: manipulation;
  -webkit-tap-highlight-color: transparent;
}

.daily-tab:hover {
  color: var(--text-primary);
  background: rgba(255, 255, 255, 0.04);
}

.daily-tab:focus-visible {
  outline: 2px solid rgba(76, 127, 255, 0.6);
  outline-offset: 2px;
}

.daily-tab:active {
  transform: scale(0.97);
}

.tab-active {
  background: rgba(41, 98, 255, 0.18);
  color: #4c7fff;
}

.tab-active:hover {
  background: rgba(41, 98, 255, 0.22);
  color: #5c8cff;
}

/* 加载状态 */
.daily-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 60px 0;
  color: var(--text-muted);
  font-size: 13px;
}

/* 面板 */
.daily-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* 下拉刷新 */
.pull-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(209, 212, 220, 0.5);
  font-size: 13px;
  overflow: hidden;
  transition: height 120ms ease;
}

.refresh-indicator {
  text-align: center;
  padding: 8px;
  color: rgba(209, 212, 220, 0.7);
  font-size: 13px;
}
</style>
