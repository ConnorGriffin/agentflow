#!/bin/sh
set -eu
provider=$1
root=$2
skill=$3
marker=$4
case $provider in claude|codex) ;; *) exit 64;; esac
agent_skill="$root/.agents/skills/$skill/SKILL.md"
claude_skill="$root/.claude/skills/$skill"
if test -f "$agent_skill" && test -f "$claude_skill/SKILL.md" \
    && test ! -L "$agent_skill" && test ! -L "$claude_skill"; then
  if test "$provider" = claude; then
    printf '%s\n' "{\"name\":\"Skill\",\"input\":{\"skill\":\"$skill\"}}"
  fi
  printf '%s\n' "$marker"
elif ! test -e "$agent_skill" && ! test -e "$claude_skill"; then
  printf '%s\n' SKILL_UNAVAILABLE
else
  echo "partial discovery fixture" >&2
  exit 1
fi
