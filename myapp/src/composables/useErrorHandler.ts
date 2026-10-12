import { ref } from 'vue';
import { ApiError } from '../services/api';

export function useErrorHandler() {
  const error = ref<string | null>(null);
  const isError = ref(false);

  function handleError(err: unknown) {
    if (err instanceof ApiError) {
      error.value = err.message;
      isError.value = true;
      
      if (err.status === 401) {
        error.value = '未授权，请重新登录';
      } else if (err.status === 403) {
        error.value = '权限不足';
      } else if (err.status === 404) {
        error.value = '资源未找到';
      } else if (err.status === 500) {
        error.value = '服务器错误，请稍后重试';
      } else if (err.status === 408) {
        error.value = '请求超时，请检查网络连接';
      }
    } else if (err instanceof Error) {
      error.value = err.message;
      isError.value = true;
    } else {
      error.value = '发生未知错误';
      isError.value = true;
    }
  }

  function clearError() {
    error.value = null;
    isError.value = false;
  }

  return {
    error,
    isError,
    handleError,
    clearError,
  };
}

