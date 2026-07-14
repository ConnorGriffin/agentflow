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
});
