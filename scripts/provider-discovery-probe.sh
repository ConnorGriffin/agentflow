#!/bin/sh
# Release verification for #582.  This deliberately proves only the pinned local contract;
# provider flags are not represented as isolation from every ambient instruction source.
set -eu

checkout=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
root=${AGENTFLOW_PROVIDER_PROBE_ROOT:-$checkout}
skill=agentflow-582-probe-4bab5ff0
marker=AGENTFLOW_582_DISCOVERED_4BAB5FF0_AEE6_4D44_BEA3_1BE5D089256F
agent_skill="$root/.agents/skills/$skill"
claude_skill="$root/.claude/skills/$skill"
prompt="Invoke the project-local skill named $skill using only native skill discovery. Do not use shell commands, search files, read files, or inspect configuration. If it is unavailable, reply exactly SKILL_UNAVAILABLE."

usage() { echo "usage: $0 {claude|codex} {positive|negative}" >&2; exit 64; }
require_fixture() { test -f "$agent_skill/SKILL.md"; test -L "$claude_skill"; }
run_claude() {
  claude -p "$prompt" --model sonnet --output-format stream-json --verbose \
    --permission-mode acceptEdits --setting-sources project --strict-mcp-config \
    --settings '{"sandbox":{"enabled":true,"failIfUnavailable":true,"allowUnsandboxedCommands":false}}'
}
run_codex() {
  codex exec -m gpt-5.6-terra --json --sandbox workspace-write --cd "$root" \
    --ignore-user-config --ephemeral --skip-git-repo-check "$prompt"
}
run_provider() {
  if test -n "${AGENTFLOW_PROVIDER_PROBE_RUNNER:-}"; then
    "$AGENTFLOW_PROVIDER_PROBE_RUNNER" "$1" "$root" "$skill" "$marker"
    return
  fi
  case $1 in claude) run_claude;; codex) run_codex;; *) usage;; esac
}
positive() { require_fixture; output=$(run_provider "$1"); printf '%s\n' "$output"; printf '%s' "$output" | grep -Fq "$marker"; }
negative() {
  require_fixture
  mv "$agent_skill" "$agent_skill.disabled"; mv "$claude_skill" "$claude_skill.disabled"
  trap 'mv "$claude_skill.disabled" "$claude_skill"; mv "$agent_skill.disabled" "$agent_skill"' EXIT HUP INT TERM
  output=$(run_provider "$1"); printf '%s\n' "$output"
  ! printf '%s' "$output" | grep -Fq "$marker"; printf '%s' "$output" | grep -Fq SKILL_UNAVAILABLE
}
test $# = 2 || usage
case $1 in claude|codex) ;; *) usage;; esac
case $2 in positive) positive "$1";; negative) negative "$1";; *) usage;; esac
