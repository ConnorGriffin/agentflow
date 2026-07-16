import { mount } from 'svelte';

// Two surfaces share one build: the fleet console and the Project workspace. The workspace lives
// at its own hash URL (`#/<project>/...`, ADR 0033), so a deep link opens it directly; every
// other URL is the fleet console. Cross-surface links are page-level (a fresh load picks the
// surface); routing *within* the workspace is hash-only, no reload.
const isWorkspace = location.hash.startsWith('#/');

async function boot() {
  if (isWorkspace) {
    await import('./lib/workspace.css');
    const { default: Workspace } = await import('./Workspace.svelte');
    return mount(Workspace, { target: document.getElementById('app') });
  }
  await import('./styles.css');
  const { default: App } = await import('./App.svelte');
  return mount(App, { target: document.getElementById('app') });
}

export default boot();
