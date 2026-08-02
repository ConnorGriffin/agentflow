import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/svelte';

// Every component test's `render()` mounts into a fresh container, but without this the
// mounted DOM survives into the next test — a second `render()` in the same file then finds
// duplicate headings/links from the first. Runs for every test file; a no-op for the pure
// derive.js tests that never render anything.
afterEach(() => cleanup());
