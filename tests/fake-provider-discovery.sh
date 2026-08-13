#!/bin/sh
set -eu
provider=$1
root=$2
skill=$3
marker=$4
mode=$5
case $provider in claude|codex) ;; *) exit 64;; esac
case $provider:$mode in
  claude:positive|claude:negative)
    expected="Invoke the project-local skill named $skill using only native skill discovery. Do not use shell commands, search files, read files, or inspect configuration. If it is unavailable, reply exactly SKILL_UNAVAILABLE."
    ;;
  codex:positive) expected="\$$skill";;
  codex:negative)
    expected="Do not invoke any skill or use any tool. Report whether the exact project-local skill named $skill is available in this session. If it is unavailable, reply exactly SKILL_UNAVAILABLE."
    ;;
  *) exit 64;;
esac
test "${AGENTFLOW_PROVIDER_PROBE_PROMPT:-}" = "$expected"
agent_skill="$root/.agents/skills/$skill/SKILL.md"
claude_skill="$root/.claude/skills/$skill"
if test -f "$agent_skill" && test -f "$claude_skill/SKILL.md" \
    && test ! -L "$agent_skill" && test ! -L "$claude_skill"; then
  if test "$provider" = claude; then
    printf '%s\n' "{\"name\":\"Skill\",\"input\":{\"skill\":\"$skill\"}}"
  fi
  printf '%s\n' "$marker"
elif ! test -e "$agent_skill" && ! test -e "$claude_skill"; then
  if test "${AGENTFLOW_PROVIDER_PROBE_REQUIRE_EMPTY:-}" = 1 \
      && find "$root/.agents/skills" "$root/.claude/skills" -name SKILL.md -print \
      | grep -q .; then
    echo "a SKILL.md remained under a provider discovery root" >&2
    exit 1
  fi
  if test "${AGENTFLOW_PROVIDER_PROBE_FAIL:-}" = 1; then
    echo "forced negative probe failure" >&2
    exit 1
  fi
  if test "${AGENTFLOW_PROVIDER_PROBE_PREFIX_COLLISION:-}" = 1; then
    test -f "$root/.agents/skills/agentflow/SKILL.md"
    test -f "$root/.claude/skills/agentflow/SKILL.md"
    test "$provider" = codex
    test "$mode" = negative
  fi
  if test "${AGENTFLOW_PROVIDER_PROBE_TOOL_EVENT:-}" = 1; then
    printf '%s\n' '{"type":"item.started","item":{"type":"command_execution"}}'
  fi
  if test "$provider" = codex; then
    printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"SKILL_UNAVAILABLE"}}'
    if test "${AGENTFLOW_PROVIDER_PROBE_NONTERMINAL:-}" != 1; then
      printf '%s\n' '{"type":"turn.completed"}'
    fi
  else
    printf '%s\n' SKILL_UNAVAILABLE
  fi
else
  echo "partial discovery fixture" >&2
  exit 1
fi
