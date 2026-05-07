import type { Config } from 'tailwindcss';
import typography from '@tailwindcss/typography';

export default {
  content: ['./index.html', './src/**/*.{svelte,ts}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // ── Semantic tokens ───────────────────────────────────────────────
        // Use with Tailwind opacity modifier: bg-success/15, text-error/80, etc.
        success: '#10b981',   // emerald-500 — tool ok, working, user bubble, send
        error:   '#f43f5e',   // rose-500    — blocked, errored, failures
        pending: '#f59e0b',   // amber-500   — awaiting input, running tool
        info:    '#0ea5e9',   // sky-500     — outbound message, turn divider
        accent:  '#8b5cf6',   // violet-500  — structural (team_created)
        muted:   '#64748b',   // slate-500   — timestamps, dt labels
        review:  '#06b6d4',   // cyan-500    — signal_review / request_termination cards & state pills
        surface: {
          DEFAULT: '#020617', // slate-950   — primary background
          raised:  '#0f172a', // slate-900   — raised card backgrounds
          2:       '#0b1322', // slightly lighter raised for spine/secondary panels
          border:  '#1e293b', // slate-800   — dividers
        },
        // ── Legacy accent shorthands (kept for backward compat) ───────────
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
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', "'SF Mono'", 'Consolas', 'monospace'],
      },
      fontVariantNumeric: {
        tabular: ['tabular-nums'],
      },
    },
  },
  plugins: [typography],
} satisfies Config;
