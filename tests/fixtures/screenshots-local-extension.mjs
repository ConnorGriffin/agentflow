#!/usr/bin/env node
/**
 * Reference repo-local extension of the pinned screenshot harness.
 *
 * This is what a repo like ciq-autotune keeps as `scripts/screenshots.local.mjs` INSTEAD of
 * forking the whole harness: a thin wrapper that imports the pinned `main` and contributes two
 * hooks. Everything else — the sandbox flags, serveRoot, the per-shot schema — comes from the
 * pinned file and re-pins cleanly on enrollment.
 *
 * In a real repo the import is a plain relative path to the sibling harness:
 *   import { main } from './screenshots.mjs';
 * Here the tests exercise it from a throwaway temp repo, so the harness path arrives via the
 * AF_HARNESS env var; the relative import is the honest fallback.
 */

import { pathToFileURL } from 'url';

const harnessPath = process.env.AF_HARNESS;
const { main } = await import(
  harnessPath ? pathToFileURL(harnessPath).href : './screenshots.mjs'
);

await main(process.argv.slice(2), {
  // Stub a vendor bundle the sandbox can't fetch (no network, or a CDN that would 404 under
  // serveRoot's abort-on-miss rule). Returning a body here satisfies the request before the
  // on-disk lookup runs — the harness stays generic; this special case stays repo-local.
  serve(url) {
    if (url.includes('vendor')) {
      return { body: 'window.__vendorLoaded = true;', contentType: 'application/javascript' };
    }
    return null;
  },

  // Extend theme application: this app's stylesheet keys off a `dark` class and its layout
  // recomputes on a resize event, neither of which the default data-theme toggle triggers.
  applyTheme(page, theme) {
    return page.evaluate((t) => {
      document.documentElement.dataset.theme = t;
      document.documentElement.classList.toggle('dark', t === 'dark');
      window.dispatchEvent(new Event('resize'));
    }, theme);
  },
});
