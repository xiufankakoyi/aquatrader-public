/**
 * 移动端手势组合式函数
 * 支持下拉刷新和左右滑动切换
 */
import { ref, onMounted, onUnmounted } from 'vue';

interface GestureOptions {
  onPullRefresh?: () => void;
  onSwipeLeft?: () => void;
  onSwipeRight?: () => void;
  pullThreshold?: number;
  swipeThreshold?: number;
}

export function useMobileGestures(options: GestureOptions = {}) {
  const {
    onPullRefresh,
    onSwipeLeft,
    onSwipeRight,
    pullThreshold = 80,
    swipeThreshold = 50,
  } = options;

  const isRefreshing = ref(false);
  const pullDistance = ref(0);
  const isPulling = ref(false);

  let startY = 0;
  let startX = 0;
  let element: HTMLElement | null = null;

  function handleTouchStart(e: TouchEvent) {
    const touch = e.touches[0];
    startY = touch.clientY;
    startX = touch.clientX;
    isPulling.value = false;
    pullDistance.value = 0;
  }

  function handleTouchMove(e: TouchEvent) {
    const touch = e.touches[0];
    const deltaY = touch.clientY - startY;

    // 下拉刷新：只在页面顶部时触发
    if (deltaY > 0 && (element?.scrollTop === 0 || window.scrollY === 0)) {
      isPulling.value = true;
      pullDistance.value = Math.min(deltaY * 0.5, pullThreshold * 1.5);
      if (deltaY > 10) {
        e.preventDefault();
      }
    }
  }

  function handleTouchEnd(e: TouchEvent) {
    const touch = e.changedTouches[0];
    const deltaX = touch.clientX - startX;
    const deltaY = touch.clientY - startY;

    // 下拉刷新触发
    if (isPulling.value && pullDistance.value >= pullThreshold && onPullRefresh && !isRefreshing.value) {
      isRefreshing.value = true;
      Promise.resolve(onPullRefresh()).finally(() => {
        isRefreshing.value = false;
        pullDistance.value = 0;
        isPulling.value = false;
      });
    } else {
      pullDistance.value = 0;
      isPulling.value = false;
    }

    // 左右滑动
    if (Math.abs(deltaX) > swipeThreshold && Math.abs(deltaY) < swipeThreshold) {
      if (deltaX < 0 && onSwipeLeft) {
        onSwipeLeft();
      } else if (deltaX > 0 && onSwipeRight) {
        onSwipeRight();
      }
    }
  }

  function bind(el?: HTMLElement) {
    element = el || document.body;
    element.addEventListener('touchstart', handleTouchStart, { passive: true });
    element.addEventListener('touchmove', handleTouchMove, { passive: false });
    element.addEventListener('touchend', handleTouchEnd, { passive: true });
  }

  function unbind() {
    if (element) {
      element.removeEventListener('touchstart', handleTouchStart);
      element.removeEventListener('touchmove', handleTouchMove);
      element.removeEventListener('touchend', handleTouchEnd);
    }
  }

  onMounted(() => bind());
  onUnmounted(() => unbind());

  return {
    isRefreshing,
    pullDistance,
    isPulling,
    bind,
    unbind,
  };
}
