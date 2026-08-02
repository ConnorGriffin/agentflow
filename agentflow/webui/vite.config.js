import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// Build to dist/ with relative asset paths so FastAPI can serve it from any mount.
export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    // `npm run dev` proxies the snapshot to a running FastAPI/stdlib server.
    proxy: { '/api': 'http://127.0.0.1:8788' },
  },
  test: {
    // jsdom only: the derive.js pure-function tests don't need it, but Briefing.test.js
    // renders the real component (@testing-library/svelte) to exercise the daemon
    // projection → endpoint → rendered briefing path end to end.
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.js'],
  },
  // Under vitest, resolve Svelte's browser (client) build rather than its SSR build —
  // @testing-library/svelte mounts components as the browser would, not server-rendered.
  resolve: process.env.VITEST ? { conditions: ['browser'] } : undefined,
});
