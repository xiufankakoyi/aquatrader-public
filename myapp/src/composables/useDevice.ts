/**
 * useDevice - 设备类型检测与响应式断点
 *
 * 提供：
 * - isMobile：是否为移动设备（UA + 屏幕宽度判断）
 * - isTablet：是否为平板
 * - isDesktop：是否为桌面端
 * - viewportWidth：当前视口宽度
 * - isPortrait：是否竖屏
 *
 * 使用方式：
 *   const { isMobile } = useDevice();
 *   if (isMobile.value) { ... }
 */

import { ref, computed, onMounted, onBeforeUnmount } from 'vue';

type DeviceType = 'mobile' | 'tablet' | 'desktop';

const MOBILE_MAX_WIDTH = 768;
const TABLET_MAX_WIDTH = 1024;

// 全局共享状态（避免每个组件都监听 resize）
const viewportWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1920);
const viewportHeight = ref(typeof window !== 'undefined' ? window.innerHeight : 1080);

let listenerCount = 0;
let resizeHandler: (() => void) | null = null;

function startListening() {
  if (resizeHandler) return;
  resizeHandler = () => {
    viewportWidth.value = window.innerWidth;
    viewportHeight.value = window.innerHeight;
  };
  window.addEventListener('resize', resizeHandler, { passive: true });
  window.addEventListener('orientationchange', resizeHandler, { passive: true });
}

function stopListening() {
  if (resizeHandler && listenerCount === 0) {
    window.removeEventListener('resize', resizeHandler);
    window.removeEventListener('orientationchange', resizeHandler);
    resizeHandler = null;
  }
}

/** 通过 User Agent 判断是否为移动设备 */
function detectMobileByUA(): boolean {
  if (typeof navigator === 'undefined') return false;
  const ua = navigator.userAgent || '';
  return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini|Mobile|Silk/i.test(ua);
}

/** 通过 UA 检测平板 */
function detectTabletByUA(): boolean {
  if (typeof navigator === 'undefined') return false;
  const ua = navigator.userAgent || '';
  return /iPad|Tablet|PlayBook|Silk/i.test(ua) || (/Android/i.test(ua) && !/Mobile/i.test(ua));
}

export function useDevice() {
  const uaIsMobile = detectMobileByUA();
  const uaIsTablet = detectTabletByUA();

  // 设备类型：综合 UA 与视口宽度
  const deviceType = computed<DeviceType>(() => {
    // 优先看 UA
    if (uaIsMobile && !uaIsTablet) return 'mobile';
    if (uaIsTablet) return 'tablet';
    // 再看视口宽度（桌面端缩窗时也按移动端渲染）
    if (viewportWidth.value <= MOBILE_MAX_WIDTH) return 'mobile';
    if (viewportWidth.value <= TABLET_MAX_WIDTH) return 'tablet';
    return 'desktop';
  });

  const isMobile = computed(() => deviceType.value === 'mobile');
  const isTablet = computed(() => deviceType.value === 'tablet');
  const isDesktop = computed(() => deviceType.value === 'desktop');
  const isPortrait = computed(() => viewportHeight.value >= viewportWidth.value);

  onMounted(() => {
    listenerCount++;
    startListening();
  });

  onBeforeUnmount(() => {
    listenerCount = Math.max(0, listenerCount - 1);
    stopListening();
  });

  return {
    deviceType,
    isMobile,
    isTablet,
    isDesktop,
    isPortrait,
    viewportWidth,
    viewportHeight,
  };
}
