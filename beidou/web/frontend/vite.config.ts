import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: {
    outDir: '../static',
    emptyOutDir: false,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8765',
      '/ws': {
        target: 'ws://localhost:8765',
        ws: true,
      },
    },
  },
});
