<template>
  <div class="mobile-login">
    <div class="login-card">
      <div class="login-header">
        <span class="login-logo">AquaTrade</span>
        <span class="login-subtitle">投研操作台</span>
      </div>

      <div class="login-form">
        <div class="form-group">
          <label class="form-label">访问令牌</label>
          <a-input-password
            v-model:value="token"
            placeholder="请输入 Token"
            size="large"
            @pressEnter="handleLogin"
          />
        </div>

        <div class="form-divider">
          <span class="divider-text">或</span>
        </div>

        <div class="form-group">
          <label class="form-label">PIN 码</label>
          <a-input-password
            v-model:value="pin"
            placeholder="请输入 PIN"
            size="large"
            :maxlength="6"
            @pressEnter="handleLogin"
          />
        </div>

        <div v-if="errorMsg" class="login-error">
          <i class="fa-solid fa-circle-exclamation" />
          {{ errorMsg }}
        </div>

        <a-button
          type="primary"
          block
          size="large"
          :loading="loading"
          class="login-btn"
          @click="handleLogin"
        >
          登录
        </a-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileLogin - 移动端登录页
 * 支持 Token 和 PIN 两种登录方式
 */
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { loginWithToken, loginWithPin } from '../../api/mobile';

const router = useRouter();

const token = ref('');
const pin = ref('');
const loading = ref(false);
const errorMsg = ref('');

/** 执行登录 */
async function handleLogin() {
  errorMsg.value = '';

  if (!token.value && !pin.value) {
    errorMsg.value = '请输入 Token 或 PIN 码';
    return;
  }

  loading.value = true;
  try {
    let result;
    if (token.value) {
      result = await loginWithToken(token.value);
    } else {
      result = await loginWithPin(pin.value);
    }

    // 存储认证信息
    if (result?.data?.token) {
      localStorage.setItem('mobile_auth_token', result.data.token);
    } else if (result?.token) {
      localStorage.setItem('mobile_auth_token', result.token);
    } else {
      // 登录成功但无 token 返回，标记已认证
      localStorage.setItem('mobile_auth_token', 'authenticated');
    }

    router.replace('/mobile');
  } catch (err: any) {
    const msg =
      err?.response?.data?.error ||
      err?.response?.data?.detail ||
      err?.response?.data?.message ||
      '登录失败，请检查凭证';
    errorMsg.value = msg;
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.mobile-login {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0a0a0a;
  padding: 24px;
}

.login-card {
  width: 100%;
  max-width: 380px;
  background: linear-gradient(
    145deg,
    rgba(30, 30, 46, 0.9) 0%,
    rgba(26, 26, 46, 0.95) 100%
  );
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 16px;
  padding: 32px 24px;
  position: relative;
  overflow: hidden;
}

.login-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(
    90deg,
    transparent 0%,
    #4c7fff 50%,
    transparent 100%
  );
  opacity: 0.6;
}

.login-header {
  text-align: center;
  margin-bottom: 32px;
}

.login-logo {
  display: block;
  font-size: 28px;
  font-weight: 700;
  color: #4c7fff;
  letter-spacing: -0.02em;
}

.login-subtitle {
  display: block;
  font-size: 14px;
  color: var(--text-muted);
  margin-top: 4px;
}

.form-group {
  margin-bottom: 16px;
}

.form-label {
  display: block;
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.form-divider {
  display: flex;
  align-items: center;
  margin: 20px 0;
}

.form-divider::before,
.form-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: rgba(255, 255, 255, 0.06);
}

.divider-text {
  padding: 0 12px;
  font-size: 12px;
  color: var(--text-muted);
}

.login-error {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  margin-bottom: 16px;
  background: rgba(242, 54, 69, 0.1);
  border: 1px solid rgba(242, 54, 69, 0.2);
  border-radius: 8px;
  color: #f55763;
  font-size: 13px;
}

.login-btn {
  height: 44px;
  border-radius: 10px;
  font-size: 15px;
  font-weight: 500;
  margin-top: 8px;
}
</style>
