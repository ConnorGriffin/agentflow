/**
 * Build the issue-374 screenshot config: the locked operator surface rendering the two
 * Decision Map states term 23 names, and the built console rendering the same two.
 *
 *   node docs/screenshots/issue-374/shotgen.mjs
 *   node scripts/screenshots.mjs docs/screenshots/issue-374/<ROUND>/shots.json
 *
 * Each capture round writes into its own directory named for the branch head it was taken at,
 * so a later round can never repaint the images an earlier PR comment points at. Bump ROUND
 * below when re-capturing.
 *
 * The build shots are driven by `agentflow/webui/src/fixtures/operator-briefing-states.json`,
 * which the Python suite builds from the daemon's own shaping and pins — so a shot can never
 * picture a map the projection would not publish. The mock shots are driven by the locked
 * capture matrix in `mockups/operator-surface.screenshots.json`, whose payloads are authored
 * from the manifest's terms rather than derived from the build, so the pair compares the
 * locked wording and treatment against what actually renders.
 */

import { readFileSync, mkdirSync, writeFileSync } from 'fs';
import { dirname, join, resolve } from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '../../..');
const ROUND = 'e7cd3c4';
const OUT = join(HERE, ROUND);

const CONSOLE_URL = pathToFileURL(join(ROOT, 'agentflow/webui/dist/index.html')).href;
const MOCKUP_URL = pathToFileURL(join(ROOT, 'mockups/operator-surface-finalist.html')).href;
const BRIEFING_TAB = 'nav .tab:nth-child(5)';
const THEME_TOGGLE = '.briefing button';

/* Tall enough for all four maps with their supporting records disclosed — the overflow
   counts and the landed-evidence wording are the whole point of these two states, and a
   shot that crops them proves nothing. */
const VIEWPORT = { width: 1280, height: 1320 };

const STATES = ['map-frontier-matrix', 'map-overflow-evidence'];

const snapshots = JSON.parse(readFileSync(
  join(ROOT, 'agentflow/webui/src/fixtures/operator-briefing-states.json'), 'utf8'));
const matrix = JSON.parse(readFileSync(
  join(ROOT, 'mockups/operator-surface.screenshots.json'), 'utf8'));

/** The locked mockup's own capture payload for one state, by its output filename. */
function lockedCapture(state) {
  const shot = matrix.shots.find((s) => s.out.endsWith(`operator-surface-finalist-${state}.png`));
  if (!shot) throw new Error(`no locked capture for ${state} in the capture matrix`);
  return shot.fetchStub['operator-surface.capture.json'];
}

/* Both surfaces keep every map's supporting records behind a native disclosure. The shot has
   to open them, and clicking each summary is exactly how an operator does it. */
const openAllRecords = (count) =>
  Array.from({ length: count }, (_, i) => `.map:nth-of-type(${i + 1}) details:nth-of-type(2) summary`);

function mock(state, theme, mapCount) {
  return {
    url: MOCKUP_URL, theme, viewport: VIEWPORT, settle: 350,
    out: join(OUT, `mock-${state}-${theme}.png`),
    clicks: openAllRecords(mapCount),
    fetchStub: { 'operator-surface.capture.json': lockedCapture(state) },
  };
}

function build(state, theme, mapCount) {
  const clicks = [BRIEFING_TAB, ...openAllRecords(mapCount)];
  if (theme === 'dark') clicks.splice(1, 0, THEME_TOGGLE);
  return {
    url: CONSOLE_URL, theme, viewport: VIEWPORT, settle: 350,
    out: join(OUT, `build-${state}-${theme}.png`),
    clicks,
    fetchStub: { 'api/snapshot': snapshots[state] },
  };
}

const shots = [];
for (const state of STATES) {
  const mapCount = snapshots[state].repositories
    .reduce((n, r) => n + r.maps.active.length, 0);
  const locked = lockedCapture(state);
  if (locked.maps.length !== mapCount) {
    throw new Error(`${state}: the locked capture shows ${locked.maps.length} maps, `
      + `the projection publishes ${mapCount}`);
  }
  for (const theme of ['light', 'dark']) {
    shots.push(mock(state, theme, mapCount));
    shots.push(build(state, theme, mapCount));
  }
}

mkdirSync(OUT, { recursive: true });
writeFileSync(join(OUT, 'shots.json'), JSON.stringify({ shots }, null, 1));
console.log(`wrote ${join(OUT, 'shots.json')} — ${shots.length} shots`);
