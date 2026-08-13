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
require_fixture() {
  test -d "$agent_skill"; test ! -L "$agent_skill"; test -f "$agent_skill/SKILL.md"
  test -d "$claude_skill"; test ! -L "$claude_skill"; test -f "$claude_skill/SKILL.md"
}
run_real_provider() {
  AGENTFLOW_PROVIDER_PROBE_PROMPT=$prompt "$checkout/.venv/bin/python" - "$1" "$root" <<'PY'
import os
import sys

from agentflow.runner import ClaudeRunner, CodexRunner

provider, root = sys.argv[1:]
prompt = os.environ["AGENTFLOW_PROVIDER_PROBE_PROMPT"]
if provider == "claude":
    argv = ClaudeRunner().structured_argv(prompt, "sonnet", root)
elif provider == "codex":
    argv = CodexRunner().structured_argv(prompt, "terra", root)
else:
    raise SystemExit(64)
os.execvp(argv[0], argv)
PY
}
run_provider() {
  if test -n "${AGENTFLOW_PROVIDER_PROBE_RUNNER:-}"; then
    "$AGENTFLOW_PROVIDER_PROBE_RUNNER" "$1" "$root" "$skill" "$marker"
    return
  fi
  run_real_provider "$1"
}
positive() {
  require_fixture
  if test -z "${AGENTFLOW_PROVIDER_PROBE_RUNNER:-}"; then
    "$checkout/.venv/bin/python" - "$root" "$1" <<'PY'
import sys
from agentflow.provider_skills import clear_native_discovery_receipt
clear_native_discovery_receipt(sys.argv[1], sys.argv[2])
PY
  fi
  output=$(run_provider "$1")
  printf '%s\n' "$output"
  printf '%s' "$output" | grep -Fq "$marker"
  case $1 in
    claude)
      printf '%s' "$output" | grep -Fq '"name":"Skill"'
      printf '%s' "$output" | grep -Fq "\"skill\":\"$skill\""
      ;;
    codex)
      ! printf '%s' "$output" | grep -Fq '"type":"command_execution"'
      ;;
  esac
  if test -z "${AGENTFLOW_PROVIDER_PROBE_RUNNER:-}"; then
    "$checkout/.venv/bin/python" - "$root" "$1" <<'PY'
import sys
from agentflow.provider_skills import record_native_discovery_receipt
print(record_native_discovery_receipt(sys.argv[1], sys.argv[2]))
PY
  fi
}
negative() {
  require_fixture
  mv "$agent_skill" "$agent_skill.disabled"; mv "$claude_skill" "$claude_skill.disabled"
  trap 'mv "$claude_skill.disabled" "$claude_skill"; mv "$agent_skill.disabled" "$agent_skill"' EXIT HUP INT TERM
  output=$(run_provider "$1"); printf '%s\n' "$output"
  ! printf '%s' "$output" | grep -Fq "$marker"
  printf '%s' "$output" | grep -Fq SKILL_UNAVAILABLE
  ! printf '%s' "$output" | grep -Fq '"name":"Skill"'
  ! printf '%s' "$output" | grep -Fq '"type":"command_execution"'
}
test $# = 2 || usage
case $1 in claude|codex) ;; *) usage;; esac
case $2 in positive) positive "$1";; negative) negative "$1";; *) usage;; esac
