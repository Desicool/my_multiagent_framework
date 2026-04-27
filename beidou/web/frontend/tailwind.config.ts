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
        surface: {
          DEFAULT: '#020617', // slate-950   — primary background
          raised:  '#0f172a', // slate-900   — raised card backgrounds
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
    },
  },
  plugins: [typography],
} satisfies Config;
