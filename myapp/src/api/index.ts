/**
 * Axios 实例封装
 * 统一配置请求拦截器、响应拦截器和 Mock 拦截器
 *
 * NOTE: This axios instance is the legacy HTTP client. New code should use
 * the unified `request()` from `../services/api.ts` instead. This file is
 * kept for backward compatibility with components that haven't been migrated yet.
 */

import axios from 'axios';

// 创建 Axios 实例
// 开发环境使用空 baseURL，让请求通过 Vite 代理
// 生产环境使用环境变量配置的 API 地址
const service = axios.create({
  baseURL: import.meta.env.PROD ? (import.meta.env.VITE_API_URL || '') : '',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 请求拦截器
service.interceptors.request.use(
  (config) => {
    // 可以在这里添加 token 等全局请求头
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// 响应拦截器
service.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    // 统一错误处理
    console.error('[API Error]', error.message);
    return Promise.reject(error);
  }
);

// ✅ 注入 Mock (这一行是关键)
// Mock 模式通过环境变量 VITE_USE_MOCK 控制
// 只有显式设置 VITE_USE_MOCK=true 时才启用 Mock
if (import.meta.env.DEV && import.meta.env.VITE_USE_MOCK === 'true') {
  void import('./mockAdapter').then(({ setupMock }) => setupMock(service));
}

export default service;
