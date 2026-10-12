import { createApp } from 'vue';
import { createPinia } from 'pinia';
import router from './router';

// 设计系统样式（必须先引入，提供CSS变量）
import './styles/design-system.css';

import './style.css';
import App from './App.vue';
import './index.css';
import '@fortawesome/fontawesome-free/css/all.css';
import VueVirtualScroller from 'vue-virtual-scroller';
import 'vue-virtual-scroller/dist/vue-virtual-scroller.css';
import Antd from 'ant-design-vue';
import 'ant-design-vue/dist/reset.css';

// 引入 Axios 实例 (会自动注入 Axios Mock 拦截器)
import './api/index';

async function bootstrap() {
  // 开发环境：仅在明确开启 Mock 时加载拦截器，避免生产主包携带 mock 数据。
  if (import.meta.env.DEV && import.meta.env.VITE_USE_MOCK === 'true') {
    const { setupFetchMock } = await import('./api/fetchMock');
    setupFetchMock();
  }

  const app = createApp(App);
  const pinia = createPinia();

  app.use(pinia);
  app.use(router);
  app.use(VueVirtualScroller);
  app.use(Antd);
  app.mount('#app');
}

void bootstrap();
