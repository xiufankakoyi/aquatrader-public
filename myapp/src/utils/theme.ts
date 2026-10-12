export type ThemeMode = 'light' | 'dark' | 'auto';

const THEME_STORAGE_KEY = 'aquatrade_theme_mode';

export function loadTheme(): ThemeMode {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === 'light' || saved === 'dark' || saved === 'auto') {
      return saved;
    }
  } catch (e) {
    console.error('Failed to load theme:', e);
  }
  return 'auto';
}

export function saveTheme(mode: ThemeMode): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch (e) {
    console.error('Failed to save theme:', e);
  }
}

export function applyTheme(mode: ThemeMode): void {
  const root = document.documentElement;
  const actualMode = mode === 'auto' 
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : mode;

  if (actualMode === 'dark') {
    root.classList.add('dark');
  } else {
    root.classList.remove('dark');
  }
}

