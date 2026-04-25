import type { Config } from 'tailwindcss';
import typography from '@tailwindcss/typography';

export default {
  content: ['./index.html', './src/**/*.{svelte,ts}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        slate: {
          950: '#020617',
        },
        emerald: {
          accent: '#10b981',
        },
        amber: {
          accent: '#f59e0b',
        },
        rose: {
          accent: '#f43f5e',
        },
      },
    },
  },
  plugins: [typography],
} satisfies Config;
