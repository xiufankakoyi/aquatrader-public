/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: '#2962FF',
        secondary: '#e2e8f0',
        success: '#089981',
        danger: '#F23645',
        warning: '#f59e0b',
        info: '#2962FF',
        light: '#f8fafc',
        dark: '#131722',
        'dark-card': '#1E222D',
        'border-color': '#2A2E39',
        'primary-light': '#93c5fd',
        'success-light': '#a7f3d0',
        'danger-light': '#fca5a5',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Roboto Mono', 'monospace'],
      },
      boxShadow: {
        card: '0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)',
        'card-hover': '0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05)',
      },
      borderRadius: {
        'sm': '4px',
        'md': '6px',
      }
    },
  },
  plugins: [],
}