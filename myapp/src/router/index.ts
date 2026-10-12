/**
 * 路由配置
 * 定义应用的主要路由：Dashboard 和策略详情页
 */
import { createRouter, createWebHistory } from 'vue-router';
import type { RouteRecordRaw } from 'vue-router';

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: () => import('../layout/MainLayout.vue'),
    children: [
      {
        path: '',
        redirect: '/portfolio'
      },
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('../pages/DashboardOverview.vue'),
        meta: {
          title: '总览看板',
          keepAlive: true
        }
      },
      {
        path: 'strategy/:id?',
        name: 'StrategyDetail',
        component: () => import('../pages/StrategyDetailKline.vue'),
        meta: {
          title: '策略详情 & K线'
        },
        props: true
      },
      {
        path: 'param-optimization',
        name: 'ParamOptimization',
        component: () => import('../pages/ParamOptimizationPage.vue'),
        meta: {
          title: '参数优化'
        }
      },
      // 旧路由重定向到新页面（兼容旧链接）
      {
        path: 'grid-search',
        redirect: '/param-optimization'
      },
      {
        path: 'param-compare',
        redirect: '/param-optimization'
      },
      {
        path: 'stock-sentiment',
        name: 'StockSentiment',
        component: () => import('../pages/StockSentimentPage.vue'),
        meta: {
          title: '股票风评'
        }
      },
      {
        path: 'portfolio',
        name: 'Portfolio',
        component: () => import('../pages/PortfolioAnalysisPage.vue'),
        meta: {
          title: '实盘持仓分析'
        }
      },
      {
        path: 'history',
        name: 'History',
        component: () => import('../pages/HistoryRecordsPage.vue'),
        meta: {
          title: '历史记录'
        }
      },
      {
        path: 'strategy-center',
        name: 'StrategyCenter',
        component: () => import('../pages/StrategyCenterPage.vue'),
        meta: {
          title: '策略中心'
        }
      },
      {
        path: 'dragon-eye',
        name: 'DragonEye',
        component: () => import('../pages/DragonEyePage.vue'),
        meta: {
          title: 'DragonEye 龙虎榜'
        }
      },
      {
        path: 'kline-game',
        name: 'KlineGame',
        component: () => import('../pages/KlineGamePage.vue'),
        meta: {
          title: 'K线盘感训练'
        }
      },
      {
        path: 'stock-screener',
        name: 'StockScreener',
        component: () => import('../views/StockScreenerPage.vue'),
        meta: {
          title: '股票量化筛选器'
        }
      },
      {
        path: 'similarity',
        name: 'Similarity',
        component: () => import('../views/SimilarityPage.vue'),
        meta: {
          title: 'K线形态研究'
        }
      },
      {
        path: 'pattern-radar',
        redirect: (to) => ({
          path: '/similarity',
          query: { ...to.query, module: 'radar' }
        })
      },
      {
        path: 'concept-research',
        redirect: '/industry-chain'
      },
      {
        path: 'industry-chain',
        name: 'IndustryChainRadar',
        component: () => import('../views/IndustryChainRadar.vue'),
        meta: {
          title: '产业链雷达',
          keepAlive: true
        }
      },
      {
        path: 'research/company',
        name: 'CompanyResearch',
        component: () => import('../pages/research/CompanyResearchPage.vue'),
        meta: {
          title: '公司研究档案',
          keepAlive: true
        }
      },
      {
        path: 'research/workbench',
        name: 'ResearchWorkbench',
        component: () => import('../pages/research/ResearchWorkbenchPage.vue'),
        meta: {
          title: '机构级研究工作台',
          keepAlive: true
        }
      },
      {
        path: 'finance-radar',
        name: 'FinanceRadar',
        component: () => import('../pages/FinanceRadarPage.vue'),
        meta: {
          title: '财务雷达',
          keepAlive: true
        }
      },
      {
        path: 'knowledge-graph',
        name: 'KnowledgeGraph',
        component: () => import('../views/KnowledgeGraphPage.vue'),
        meta: {
          title: '证据边知识图谱',
          keepAlive: true
        }
      }
    ]
  },
  // 移动端登录页（独立于 MobileLayout，无底部导航）
  {
    path: '/mobile/login',
    name: 'MobileLogin',
    component: () => import('../pages/mobile/MobileLogin.vue'),
    meta: {
      title: '登录'
    }
  },
  // 移动端路由组（使用 MobileLayout，带底部导航）
  {
    path: '/mobile',
    component: () => import('../layout/MobileLayout.vue'),
    children: [
      {
        path: '',
        name: 'MobileHome',
        component: () => import('../pages/mobile/MobileHome.vue'),
        meta: {
          title: '投研首页'
        }
      },
      {
        path: 'daily',
        name: 'MobileDaily',
        component: () => import('../pages/mobile/MobileDaily.vue'),
        meta: {
          title: '日报'
        }
      },
      {
        path: 'inbox',
        name: 'MobileInbox',
        component: () => import('../pages/mobile/MobileInbox.vue'),
        meta: {
          title: '收件箱'
        }
      },
      {
        path: 'watchlist',
        name: 'MobileWatchlist',
        component: () => import('../pages/mobile/MobileWatchlist.vue'),
        meta: {
          title: '观察池'
        }
      },
      {
        path: 'tasks',
        name: 'MobileTasks',
        component: () => import('../pages/mobile/MobileTasks.vue'),
        meta: {
          title: '任务'
        }
      },
      {
        path: 'tasks/:taskId',
        name: 'MobileTaskDetail',
        component: () => import('../pages/mobile/MobileTasks.vue'),
        meta: {
          title: '任务详情'
        }
      },
      {
        path: 'report/:reportId',
        name: 'MobileReport',
        component: () => import('../pages/mobile/MobileReport.vue'),
        meta: {
          title: '报告'
        }
      },
      {
        path: 'command',
        name: 'MobileCommand',
        component: () => import('../pages/mobile/MobileCommand.vue'),
        meta: {
          title: '命令'
        }
      },
      {
        path: 'chain',
        name: 'MobileChain',
        component: () => import('../pages/mobile/MobileChain.vue'),
        meta: {
          title: '产业链'
        }
      }
    ]
  }
];

const router = createRouter({
  history: createWebHistory(),
  routes
});

export default router;

