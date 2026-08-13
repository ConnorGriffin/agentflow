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
    AGENTFLOW_PROVIDER_PROBE_PROMPT=$prompt \
      "$AGENTFLOW_PROVIDER_PROBE_RUNNER" "$1" "$root" "$skill" "$marker"
    return
  fi
  run_real_provider "$1"
}
validate_output() {
  "$checkout/.venv/bin/python" -c '
import sys
from agentflow.provider_skills import (
    native_discovery_output_is_proof,
    native_discovery_output_is_unavailable,
)
provider, mode = sys.argv[1:]
output = sys.stdin.read()
predicate = (native_discovery_output_is_proof if mode == "proof"
             else native_discovery_output_is_unavailable)
raise SystemExit(0 if predicate(provider, output) else 1)
' "$1" "$2"
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
  printf '%s' "$output" | validate_output "$1" proof
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
  holder=$(mktemp -d "$root/.agentflow-provider-probe.XXXXXX")
  held_agent="$holder/agents-skill"
  held_claude="$holder/claude-skill"
  restore_fixtures() {
    result=$1
    trap - EXIT HUP INT TERM
    if test -d "$held_agent" && test -d "$held_claude" \
        && ! test -e "$agent_skill" && ! test -L "$agent_skill" \
        && ! test -e "$claude_skill" && ! test -L "$claude_skill"; then
      :
    elif test -d "$held_agent" && test -d "$claude_skill" \
        && ! test -e "$agent_skill" && ! test -L "$agent_skill" \
        && ! test -e "$held_claude" && ! test -L "$held_claude"; then
      if mv "$held_agent" "$agent_skill" && rmdir "$holder"; then exit "$result"; fi
      echo "provider probe partial setup restoration failed; inspect $holder" >&2
      exit 1
    elif test -d "$agent_skill" && test -d "$claude_skill" \
        && ! test -e "$held_agent" && ! test -L "$held_agent" \
        && ! test -e "$held_claude" && ! test -L "$held_claude"; then
      if rmdir "$holder"; then exit "$result"; fi
      echo "provider probe holder cleanup failed at $holder" >&2
      exit 1
    else
      echo "provider probe fixture restoration precondition failed; holder retained at $holder" >&2
      exit 1
    fi
    if ! mv "$held_agent" "$agent_skill"; then
      echo "provider probe fixture restoration failed; holder retained at $holder" >&2
      exit 1
    fi
    if ! mv "$held_claude" "$claude_skill"; then
      if ! mv "$agent_skill" "$held_agent"; then
        echo "provider probe fixture rollback failed; inspect $holder and $agent_skill" >&2
      else
        echo "provider probe fixture restoration failed; both fixtures retained at $holder" >&2
      fi
      exit 1
    fi
    if ! rmdir "$holder"; then
      echo "provider probe holder cleanup failed at $holder" >&2
      exit 1
    fi
    exit "$result"
  }
  trap 'restore_fixtures $?' EXIT
  trap 'restore_fixtures 129' HUP
  trap 'restore_fixtures 130' INT
  trap 'restore_fixtures 143' TERM
  if ! mv "$agent_skill" "$held_agent"; then
    exit 1
  fi
  if ! mv "$claude_skill" "$held_claude"; then
    exit 1
  fi
  output=$(run_provider "$1"); printf '%s\n' "$output"
  printf '%s' "$output" | validate_output "$1" unavailable
}
test $# = 2 || usage
case $1 in claude|codex) ;; *) usage;; esac
case $1 in
  claude)
    prompt="Invoke the project-local skill named $skill using only native skill discovery. Do not use shell commands, search files, read files, or inspect configuration. If it is unavailable, reply exactly SKILL_UNAVAILABLE."
    ;;
  codex) prompt="\$$skill";;
esac
case $2 in positive) positive "$1";; negative) negative "$1";; *) usage;; esac
