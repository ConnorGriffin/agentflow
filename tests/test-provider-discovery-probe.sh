#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
script="$root/scripts/provider-discovery-probe.sh"
fake="$root/tests/fake-provider-discovery.sh"
grep -Fq 'ClaudeRunner().structured_argv' "$script"
grep -Fq 'CodexRunner().structured_argv' "$script"
fixture=$(mktemp -d "${TMPDIR:-/tmp}/agentflow-provider-probe.XXXXXX")
trap 'rm -rf "$fixture"' EXIT HUP INT TERM
skill=agentflow-582-probe-4bab5ff0
mkdir -p "$fixture/.agents/skills" "$fixture/.claude/skills"
cp -R "$root/.agents/skills/$skill" "$fixture/.agents/skills/$skill"
cp -R "$root/.claude/skills/$skill" "$fixture/.claude/skills/$skill"
for provider in claude codex; do
  AGENTFLOW_PROVIDER_PROBE_ROOT="$fixture" AGENTFLOW_PROVIDER_PROBE_RUNNER="$fake" \
    "$script" "$provider" positive | grep -Fq AGENTFLOW_582_DISCOVERED
  AGENTFLOW_PROVIDER_PROBE_ROOT="$fixture" AGENTFLOW_PROVIDER_PROBE_RUNNER="$fake" \
    "$script" "$provider" negative | grep -Fxq SKILL_UNAVAILABLE
done
if "$script" unknown positive >/dev/null 2>&1; then exit 1; fi
if "$script" codex unknown >/dev/null 2>&1; then exit 1; fi
echo 'probe helper checks passed'
