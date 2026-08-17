#!/usr/bin/env node
/**
 * Canonical agentflow screenshot harness — one script, baked-in sandbox recipe.
 * No npm install at session time; uses Playwright's globally managed Chromium.
 *
 * Usage:
 *   node scripts/screenshots.mjs <config.json>
 *   node scripts/screenshots.mjs --self-check [--out-dir <dir>]
 *
 * Also importable: a repo that needs a local special-case (a vendor bundle to
 * stub, a theme toggle the default one doesn't do) imports { main } and passes
 * hooks instead of forking the whole file — the seam is captureShots/runConfig/main.
 *
 * Config JSON schema:
 *   {
 *     "serveRoot": string | [ string | { "dir": string, "stripPrefix": string }, ... ],
 *                                        // optional: serve every http(s) request from disk.
 *                                        // The request pathname is looked up under each root
 *                                        // in order, stripping stripPrefix first; the first
 *                                        // on-disk file wins. An unmatched request ABORTS —
 *                                        // a screenshot must not silently succeed on a page
 *                                        // that quietly failed to load half its assets.
 *     "defaults": { <shot-fields> },     // optional: merged into every shot (shallow), with
 *                                        // "storage" and "fetchStub" merged key-wise so a shot
 *                                        // can add one key without restating the whole map.
 *     "shots": [
 *       {
 *         "url":       string,              // file:///abs/path, http://host/..., OR a checkout-
 *                                           // relative path (no URL scheme) resolved to file://
 *         "theme":     "light" | "dark",    // sets data-theme on <html> after page load
 *         "out":       string,              // PNG output path (absolute or relative to cwd)
 *         "viewport":  { "width": N, "height": N },           // optional; defaults to 1280x900
 *         "settle":    number,              // optional extra ms wait before the shot
 *         "clicks":    [ "<css-selector>", ... ],             // optional: trusted click each in order
 *         "jsClicks":  [ "<css-selector>", ... ],             // optional: el.click() for targets a
 *                                           // trusted click can't reach (overlays, detached hit areas)
 *         "focus":     "<css-selector>",    // optional: focus this element…
 *         "presses":   [ "<key>", ... ],    // …then press each keyboard key (200ms settles)
 *         "selector":  "<css-selector>",    // optional: crop to this element instead of the viewport
 *         "scrollTop": true | false,        // optional: reset scroll to top-left before the shot (default true)
 *         "storage":   { "<key>": <value>, ... },             // optional: localStorage seeded before boot
 *         "fetchStub": { "<url-substr>": <json-value>, ... }, // optional per-shot stubs
 *         "clock":     "<ISO-8601>",        // optional: pin Date.now() so relative ages don't move
 *         "assert": {                       // optional: checked after clicks/theme, fails the run
 *           "theme": { "selector": "<css>", "value": "<data-theme>",
 *                      "backgroundColor": "rgb(r, g, b)" },   // backgroundColor optional
 *           "noConsoleErrors": true,
 *           "ignoreConsole": [ "<substr>", ... ],   // optional: known-irrelevant console noise
 *           "noHorizontalOverflow": true | "<css-selector>"  // whole page, or one surface
 *         }
 *       }
 *     ]
 *   }
 *
 * Sandbox recipe baked in (agentflow memory: playwright-screenshots-in-sandbox):
 *   - --no-sandbox --disable-setuid-sandbox --single-process --allow-file-access-from-files
 *   - ONE context for ALL shots: --single-process browser dies on context.close()
 *   - fetch stubbed via addInitScript (page.route doesn't intercept file:// fetches)
 *   - theme set via page.evaluate AFTER content renders (before-boot sets race with the app)
 *   - Playwright's managed Chromium (avoids crashpad path and 104-char sockaddr_un limit)
 *   - no fullPage:true (composites onto white backdrop; fixed tall viewport instead)
 */

import { readFileSync, writeFileSync, existsSync, statSync, mkdirSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve, dirname, extname } from 'path';
import { pathToFileURL } from 'url';
import { execSync } from 'child_process';

// Flags that survive the Claude agent sandbox. Do NOT add --user-data-dir with a deep
// path — the worktree nesting pushes the socket name past the 104-char sockaddr_un limit.
// Playwright's managed browser picks its own short tempdir; we don't override it.
const CHROMIUM_ARGS = [
  '--no-sandbox',
  '--disable-setuid-sandbox',
  '--single-process',
  '--allow-file-access-from-files',
];

// Content types for the on-disk serveRoot path. Kept deliberately small — these are the
// asset kinds a locked mockup or a built SPA actually references. Anything else is served
// as text/plain, which is harmless for a screenshot (the browser just won't execute it).
const MIME = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.mjs': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
};

function mimeFor(path) {
  return MIME[extname(path).toLowerCase()] || 'text/plain';
}

function die(msg) {
  console.error('[screenshots] ' + msg);
  process.exit(1);
}

// Enrollment installs a pinned Playwright beside the drive-local-webapp skill. Prefer
// that reproducible project-local runtime; retain the older global locations so existing
// enrolled repositories keep working until they are re-enrolled.
function playwrightCandidates() {
  const candidates = [
    join(process.cwd(), '.agents', 'skills', 'drive-local-webapp',
      'node_modules', 'playwright', 'index.mjs'),
    'playwright',
  ];
  try {
    const globalRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
    if (globalRoot) candidates.push(join(globalRoot, 'playwright', 'index.mjs'));
  } catch {
    // npm not on PATH — fall through to the known fixed locations below.
  }
  candidates.push('/opt/homebrew/lib/node_modules/playwright/index.mjs');
  return candidates;
}

async function loadPlaywright() {
  const tried = [];
  for (const candidate of playwrightCandidates()) {
    try {
      const mod = await import(candidate);
      return mod.default || mod;
    } catch (e) {
      tried.push('  ' + candidate + '  (' + (e.code || e.message) + ')');
    }
  }
  die('Cannot load playwright. Tried:\n' + tried.join('\n') +
      '\nInstall Playwright where node can resolve it (a global install), or set NODE_PATH.');
}

/**
 * Build a single addInitScript payload that seeds state for every shot before its app boots.
 * Each shot navigates to url?shot=<idx>, and the init script reads that index to select the
 * right fetch stubs, pinned clock, and localStorage seed. Keyed by index (not name) so no
 * shot URL needs more than one query param — Bash's static analyzer treats & as a
 * background-process operator and blocks commands that embed file://...?a=1&b=2.
 */
function buildInitScript(shots) {
  const stubMap = Object.fromEntries(
    shots.map((s, i) => [String(i), s.fetchStub || {}])
  );
  // Optional pinned clock, same index keying. An app that words ages relative to Date.now()
  // otherwise renders a different string every run, so two captures of one state differ
  // where nothing changed. Only Date.now() is pinned — Date.parse and explicit timestamps
  // still work normally, which is all the relative-time helpers need.
  const clockMap = Object.fromEntries(
    shots.map((s, i) => [String(i), s.clock ? Date.parse(s.clock) : null])
  );
  // Optional localStorage seed, same index keying. Seeded before the app's own scripts run so
  // a screen that only renders when a flag/token is already present shows its post-boot state.
  const storageMap = Object.fromEntries(
    shots.map((s, i) => [String(i), s.storage || {}])
  );
  return `(function () {
  var idx = new URLSearchParams(location.search).get('shot') || '0';
  var clocks = ${JSON.stringify(clockMap)};
  var pinned = clocks[idx];
  if (pinned !== null && pinned !== undefined) Date.now = function () { return pinned; };
  var seeds = ${JSON.stringify(storageMap)};
  var seed = seeds[idx] || {};
  try {
    Object.keys(seed).forEach(function (k) { localStorage.setItem(k, String(seed[k])); });
  } catch (e) {
    // Some file:// origins deny storage access; a screenshot without the seed is better
    // than a hard crash, and the assert block (if any) will still catch a wrong render.
  }
  var map = ${JSON.stringify(stubMap)};
  var stubs = map[idx] || {};
  var keys = Object.keys(stubs);
  if (!keys.length) return;
  var _fetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function (url) {
    var s = String(url);
    for (var i = 0; i < keys.length; i++) {
      if (s.indexOf(keys[i]) !== -1) {
        return Promise.resolve(new Response(JSON.stringify(stubs[keys[i]]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        }));
      }
    }
    return _fetch ? _fetch.apply(window, arguments) : Promise.reject(new Error('fetch unavailable'));
  };
})();`;
}

/**
 * Append ?shot=<idx> to a URL so the init script picks the right fetch stub.
 * Uses & inside JS string concatenation (not a shell command), so the Bash
 * static analyzer never sees it. For file:// URLs with no existing query string,
 * the result is file:///path?shot=0 — a single param, no &.
 */
function withShotParam(url, idx) {
  return url.includes('?') ? url + '&shot=' + idx : url + '?shot=' + idx;
}

/**
 * Normalize the config-level serveRoot into a flat list of { dir, stripPrefix }.
 * A bare string is a root with no prefix to strip; an object carries its own prefix.
 * Directories are resolved to absolute so the on-disk lookup is cwd-independent.
 */
function normalizeServeRoot(serveRoot) {
  if (!serveRoot) return [];
  const list = Array.isArray(serveRoot) ? serveRoot : [serveRoot];
  return list.map((entry) =>
    typeof entry === 'string'
      ? { dir: resolve(entry), stripPrefix: '' }
      : { dir: resolve(entry.dir), stripPrefix: entry.stripPrefix || '' }
  );
}

/**
 * Serve every http(s) request from disk. page.route intercepts real network requests (unlike
 * file:// fetches, which is why the fetch stub above exists for the file:// case). The serve
 * hook runs first so a local wrapper can substitute a vendor bundle it ships out-of-band; a
 * null return falls through to the on-disk roots. An unmatched request is ABORTED, not left to
 * 404 — a screenshot of a page missing its stylesheet or app bundle looks "fine" but proves
 * nothing, so the run fails loudly instead.
 */
async function installServeRoot(page, roots, serveHook) {
  await page.route('**/*', async (route) => {
    const url = route.request().url();
    if (!/^https?:/i.test(url)) {
      // file://, data:, blob: etc. — untouched; the fetch stub covers the file:// data path.
      return route.continue();
    }
    if (serveHook) {
      const served = await serveHook(url);
      if (served) {
        return route.fulfill({ body: served.body, contentType: served.contentType });
      }
    }
    const pathname = new URL(url).pathname;
    for (const { dir, stripPrefix } of roots) {
      let rel = pathname;
      if (stripPrefix && rel.startsWith(stripPrefix)) rel = rel.slice(stripPrefix.length);
      const filePath = join(dir, rel.replace(/^\/+/, ''));
      if (existsSync(filePath) && statSync(filePath).isFile()) {
        return route.fulfill({ body: readFileSync(filePath), contentType: mimeFor(filePath) });
      }
    }
    return route.abort();
  });
}

/**
 * Opt-in per-shot checks, run after the clicks and the theme so they see the state the PNG
 * shows. A shot with no "assert" block is unchanged. Returns a list of failure strings.
 */
async function checkShot(page, shot, consoleErrors) {
  const want = shot.assert;
  if (!want) return [];
  const failures = [];

  if (want.theme) {
    const { selector, value, backgroundColor } = want.theme;
    const got = await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      return { theme: el.getAttribute('data-theme'),
               background: getComputedStyle(el).backgroundColor };
    }, selector);
    if (!got) failures.push('theme: no element matched ' + selector);
    else if (got.theme !== value) {
      failures.push('theme: ' + selector + ' is ' + got.theme + ', wanted ' + value);
    } else if (backgroundColor && got.background !== backgroundColor) {
      failures.push('theme: ' + selector + ' painted ' + got.background +
                    ', wanted ' + backgroundColor);
    }
  }

  if (want.noConsoleErrors) {
    const ignore = want.ignoreConsole || [];
    const real = consoleErrors.filter((e) => !ignore.some((skip) => e.includes(skip)));
    if (real.length) failures.push('console: ' + real.join(' | '));
  }

  if (want.noHorizontalOverflow) {
    // `true` measures the whole page; a selector measures one surface, for a page whose
    // surrounding chrome has its own (out-of-scope) width floor.
    const selector = typeof want.noHorizontalOverflow === 'string'
      ? want.noHorizontalOverflow : null;
    const overflow = await page.evaluate((sel) => {
      const el = sel ? document.querySelector(sel) : document.documentElement;
      return el ? el.scrollWidth - el.clientWidth : null;
    }, selector);
    if (overflow === null) {
      failures.push('overflow: no element matched ' + selector);
    } else if (overflow > 0) {
      failures.push('overflow: ' + (selector || 'the page') + ' scrolls ' + overflow +
                    'px horizontally');
    }
  }

  return failures;
}

/**
 * Capture a list of already-normalized shots. This is the deep seam: a repo-local wrapper
 * reaches it through main()/runConfig() and only supplies hooks —
 *   options.serveRoot   normalized [{ dir, stripPrefix }] roots (runConfig fills this in)
 *   options.serve(url)  called before the on-disk lookup; return { body, contentType } to
 *                       satisfy a request (e.g. a vendor bundle), or null to fall through
 *   options.applyTheme(page, theme)  overrides how a theme is applied; defaults to setting
 *                       data-theme on <html>, which is the historical behavior byte-for-byte
 */
export async function captureShots(shots, options = {}) {
  const roots = options.serveRoot || [];
  const serveHook = options.serve || null;
  const applyTheme = options.applyTheme || (async (page, theme) => {
    // Set theme AFTER content renders — setting it before boot races the app initializer
    // and the page silently stays light.
    await page.evaluate(function (t) {
      document.documentElement.dataset.theme = t;
    }, theme);
  });

  const pw = await loadPlaywright();

  const browser = await pw.chromium.launch({ args: CHROMIUM_ARGS });

  // ONE context for all shots — do NOT call context.close() with --single-process:
  // the browser process dies immediately, terminating before screenshot writes flush.
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
  });

  await context.addInitScript(buildInitScript(shots));
  const page = await context.newPage();

  // Serve http(s) requests from disk when a serveRoot or a serve hook is configured. A file://
  // config wants neither and keeps the untouched navigation path.
  if (roots.length || serveHook) {
    await installServeRoot(page, roots, serveHook);
  }

  // Collected per shot (cleared at each navigation) for the opt-in noConsoleErrors check.
  let consoleErrors = [];
  page.on('console', (msg) => {
    // A failed subresource reports only "Failed to load resource: ..." as its text; the URL
    // that failed is in the location, so record both or an ignore list has nothing to match.
    if (msg.type() === 'error') {
      const where = (msg.location() && msg.location().url) || '';
      consoleErrors.push(where ? msg.text() + ' [' + where + ']' : msg.text());
    }
  });
  page.on('pageerror', (e) => consoleErrors.push(String(e)));

  const violations = [];

  for (let i = 0; i < shots.length; i++) {
    const shot = shots[i];
    const { url, theme, out, viewport } = shot;

    // Optional per-shot viewport (e.g. a phone width) — defaults to the context
    // viewport when omitted, so existing single-size configs are unaffected.
    if (viewport) await page.setViewportSize(viewport);

    // Append the shot index so the init script selects the right fetch stub.
    consoleErrors = [];
    await page.goto(withShotParam(url, i), { waitUntil: 'networkidle' });

    // Optional interaction steps for screens that are only reached by clicking
    // (e.g. a Generate button, then opening a results card). Each entry is a CSS
    // selector; Playwright's click auto-waits for the element to be visible and
    // enabled. Omitted for static screens, so existing configs are unaffected.
    if (Array.isArray(shot.clicks)) {
      for (const selector of shot.clicks) {
        await page.click(selector);
        await page.waitForTimeout(200);
      }
    }

    // jsClicks fire the element's own click() from inside the page — for targets a trusted
    // Playwright click can't reach (a zero-size hit area, an element under an overlay that
    // still has a handler). Same 200ms settle as clicks so any resulting render completes.
    if (Array.isArray(shot.jsClicks)) {
      for (const selector of shot.jsClicks) {
        await page.locator(selector).first().evaluate((el) => el.click());
        await page.waitForTimeout(200);
      }
    }

    // Focus-then-type, for a keyboard-driven state (a menu opened with arrow keys, a field
    // that only reveals suggestions on input). Trusted key events, unlike jsClicks.
    if (shot.focus) await page.focus(shot.focus);
    if (Array.isArray(shot.presses)) {
      for (const key of shot.presses) {
        await page.keyboard.press(key);
        await page.waitForTimeout(200);
      }
    }

    if (theme) {
      await applyTheme(page, theme);
      // Brief settle so any theme-driven re-renders complete before the shot.
      await page.waitForTimeout(200);
    }

    // Optional extra settle for apps whose content arrives after a debounce or
    // a stubbed fetch (which doesn't register as network activity for
    // networkidle). Defaults to none, so existing configs are unaffected.
    if (shot.settle) await page.waitForTimeout(shot.settle);

    // A click scrolls its target into view; on a narrow viewport that can leave the page
    // scrolled sideways, so the shot would frame the wrong part of it. Always shoot from
    // the top-left — unless a shot explicitly opts out (scrollTop:false) to keep a scroll
    // position it just set up.
    if (shot.scrollTop !== false) {
      await page.evaluate(() => window.scrollTo(0, 0));
    }

    const outPath = resolve(out);
    mkdirSync(dirname(outPath), { recursive: true });
    // A "selector" crops the shot to one element (a card, a panel) instead of the viewport;
    // Playwright scrolls it into view and clips to its box. Omitted → full viewport as before.
    if (shot.selector) {
      await page.locator(shot.selector).screenshot({ path: outPath });
    } else {
      await page.screenshot({ path: outPath });
    }
    console.log('[screenshots] wrote ' + outPath + (theme ? ' (' + theme + ')' : ''));

    for (const failure of await checkShot(page, shot, consoleErrors)) {
      violations.push(out + ': ' + failure);
      console.error('[screenshots] FAIL ' + out + ': ' + failure);
    }
  }

  // Close the browser (not the context) — context.close() kills --single-process browsers.
  await browser.close();

  // Every PNG is written before anything fails, so the shots stay inspectable — but a run
  // whose assertions failed exits nonzero rather than handing back evidence that lies.
  if (violations.length) die(violations.length + ' assertion(s) failed');
}

/**
 * Apply the config-level conveniences (defaults merge, checkout-relative URL resolution,
 * serveRoot normalization) and then capture. Hooks in `options` (serve, applyTheme) pass
 * straight through to captureShots so a wrapper configures behavior without touching shots.
 */
export async function runConfig(config, options = {}) {
  const defaults = config.defaults || {};
  const shots = config.shots.map((shot) => {
    // Shallow spread, then merge the two map-valued fields key-wise so a shot can add one
    // storage key or one stub without restating the whole default map.
    const merged = { ...defaults, ...shot };
    if (defaults.storage || shot.storage) {
      merged.storage = { ...defaults.storage, ...shot.storage };
    }
    if (defaults.fetchStub || shot.fetchStub) {
      merged.fetchStub = { ...defaults.fetchStub, ...shot.fetchStub };
    }
    // A url with no URL scheme is a checkout-relative path — resolve it against cwd and turn
    // it into a proper file:// URL (pathToFileURL handles the leading-slash/escaping details).
    if (merged.url && !/^[a-z][a-z\d+.-]*:/i.test(merged.url)) {
      merged.url = pathToFileURL(resolve(merged.url)).href;
    }
    return merged;
  });
  const serveRoot = normalizeServeRoot(config.serveRoot);
  return captureShots(shots, { ...options, serveRoot });
}

async function selfCheck(outDir, options) {
  // Resolve to absolute up front: a relative --out-dir would otherwise produce
  // 'file://selfcheck.html' (no leading slash), an invalid URL that aborts goto.
  outDir = resolve(outDir || join(tmpdir(), 'af-selfcheck-' + Date.now()));
  mkdirSync(outDir, { recursive: true });

  const htmlPath = join(outDir, 'selfcheck.html');
  writeFileSync(htmlPath, [
    '<!DOCTYPE html>',
    '<html><head><meta charset="utf-8"></head>',
    '<body style="background:#00cc44;color:#000;font:48px monospace;padding:32px">',
    'agentflow screenshots self-check OK',
    '</body></html>',
  ].join('\n'));

  const outPng = join(outDir, 'selfcheck.png');
  // Self-check covers the three known failure modes:
  //   1. unwritable $HOME-adjacent dirs  → Playwright's managed browser uses its own tmpdir
  //   2. no npm at session time          → we import from PLAYWRIGHT_ESM, no npm install
  //   3. no crashpad                     → --no-sandbox/--single-process bypass it
  await captureShots([{ url: 'file://' + htmlPath, theme: 'light', out: outPng }], options);

  if (!existsSync(outPng)) {
    die('self-check: PNG was not written to ' + outPng);
  }
  console.log('[screenshots] self-check passed — PNG at ' + outPng);
}

/**
 * The CLI, parameterized so an importing wrapper runs the exact same argument handling and
 * self-check while contributing its own hooks via `options`. Existing invocations —
 * `node scripts/screenshots.mjs <config.json>` and `--self-check` — are unchanged.
 */
export async function main(argv, options = {}) {
  const args = argv;

  if (args[0] === '--self-check') {
    const outDirIdx = args.indexOf('--out-dir');
    const outDir = outDirIdx !== -1 ? args[outDirIdx + 1] : null;
    await selfCheck(outDir, options);
  } else if (args[0]) {
    const configPath = resolve(args[0]);
    let config;
    try {
      config = JSON.parse(readFileSync(configPath, 'utf8'));
    } catch (e) {
      die('Cannot read config ' + configPath + ': ' + e.message);
    }
    if (!Array.isArray(config.shots) || config.shots.length === 0) {
      die('Config must have a non-empty "shots" array');
    }
    await runConfig(config, options);
  } else {
    process.stderr.write(
      'Usage: node scripts/screenshots.mjs <config.json>\n' +
      '       node scripts/screenshots.mjs --self-check [--out-dir <dir>]\n'
    );
    process.exit(1);
  }
}

// --- run the CLI only when executed directly, never on import ---
// A wrapper `import { main } from './screenshots.mjs'` must not trigger a usage error or a
// capture run; only `node scripts/screenshots.mjs ...` should. process.argv[1] is the invoked
// script path; comparing its file:// URL to this module's URL distinguishes the two.
if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  await main(process.argv.slice(2));
}
