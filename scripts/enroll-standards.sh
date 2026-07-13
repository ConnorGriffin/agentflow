#!/usr/bin/env bash
#
# enroll-standards.sh — wire the agentflow engineering charter into BOTH tools
# (Claude + Codex) with zero drift. See docs/adr/0013-engineering-charter.md.
#
# The charter is ONE canonical file; both tools read the same bytes:
#   - Claude global (~/.claude/CLAUDE.md, itself a symlink into dotfiles) @imports it
#   - Codex  global (~/.codex/AGENTS.md) is a symlink to it
#   - per repo:      AGENTS.md is canonical, CLAUDE.md is a symlink to AGENTS.md
#
# SAFE BY DEFAULT: prints a plan and changes nothing unless --apply is passed.
# Never clobbers a non-empty file — backs it up to <file>.pre-agentflow first.
# Idempotent: re-running is a no-op once wired.
#
# Usage:
#   enroll-standards.sh                    # dry-run: show the global-wiring plan
#   enroll-standards.sh --apply            # do the global wiring
#   enroll-standards.sh <repo-dir>         # dry-run: show a repo's enroll plan
#   enroll-standards.sh --apply <repo-dir> # enroll a repo (AGENTS.md + CLAUDE.md symlink)
#
# VERIFY before --apply (unproven assumptions, flagged honestly):
#   - that Claude honors an `@<path>` import line in dotfiles/claude/CLAUDE.md
#   - that Codex reads a *symlinked* ~/.codex/AGENTS.md as its global instructions
# Both are cheap to confirm with a throwaway session; do that first.

set -euo pipefail

CHARTER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/standards/CHARTER.md"
CLAUDE_GLOBAL="$HOME/.claude/CLAUDE.md"
CODEX_GLOBAL="$HOME/.codex/AGENTS.md"
IMPORT_LINE="@$CHARTER"

APPLY=0; REPO=""
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    -*) echo "unknown flag: $a" >&2; exit 2 ;;
    *) REPO="$a" ;;
  esac
done

[ -f "$CHARTER" ] || { echo "no charter at $CHARTER" >&2; exit 1; }

note() { printf '  %s\n' "$1"; }
do_or_show() { # <human desc> <command...>
  local desc="$1"; shift
  if [ "$APPLY" = 1 ]; then note "DO:   $desc"; "$@"
  else note "PLAN: $desc"; fi
}
backup() { # back up a real (non-symlink, non-empty) file once
  local f="$1"
  [ -f "$f" ] && [ ! -L "$f" ] && [ -s "$f" ] && [ ! -e "$f.pre-agentflow" ] \
    && do_or_show "back up $f -> $f.pre-agentflow" cp -p "$f" "$f.pre-agentflow" || true
}

wire_global() {
  echo "Global charter wiring — canonical: $CHARTER"

  # Claude: append the @import to the (dotfiles-backed) global, if absent.
  local target="$CLAUDE_GLOBAL"
  [ -L "$target" ] && target="$(readlink "$target")"  # edit the real dotfiles file
  if [ -f "$target" ] && grep -qF "$IMPORT_LINE" "$target"; then
    note "ok:   Claude global already imports the charter ($target)"
  else
    backup "$target"
    do_or_show "append charter @import to $target" \
      bash -c 'printf "\n# --- agentflow engineering charter (managed) ---\n%s\n" "$1" >> "$2"' _ "$IMPORT_LINE" "$target"
  fi

  # Codex: ~/.codex/AGENTS.md should be a symlink to the charter.
  if [ -L "$CODEX_GLOBAL" ] && [ "$(readlink "$CODEX_GLOBAL")" = "$CHARTER" ]; then
    note "ok:   Codex global already symlinks the charter"
  elif [ -e "$CODEX_GLOBAL" ] && [ -s "$CODEX_GLOBAL" ] && [ ! -L "$CODEX_GLOBAL" ]; then
    backup "$CODEX_GLOBAL"
    note "WARN: $CODEX_GLOBAL is a non-empty real file — review $CODEX_GLOBAL.pre-agentflow,"
    note "      then re-run; refusing to clobber hand-written Codex global instructions."
  else
    do_or_show "symlink $CODEX_GLOBAL -> charter" ln -sfn "$CHARTER" "$CODEX_GLOBAL"
  fi
}

enroll_repo() { # <dir>
  local dir; dir="$(cd "$1" && pwd -P)"
  local ag="$dir/AGENTS.md" cl="$dir/CLAUDE.md" ignore="$dir/.gitignore"
  echo "Per-repo enroll — $dir"

  if [ -f "$ignore" ] && grep -qxF '.agentflow/' "$ignore"; then
    note "ok:   .agentflow/ already ignored"
  else
    do_or_show "add .agentflow/ to $ignore" \
      bash -c 'if [ -s "$1" ] && [ -n "$(tail -c 1 "$1")" ]; then printf "\n" >> "$1"; fi; printf ".agentflow/\n" >> "$1"' _ "$ignore"
  fi

  if [ -L "$cl" ] && [ "$(readlink "$cl")" = "AGENTS.md" ] && [ -f "$ag" ]; then
    note "ok:   already AGENTS.md + CLAUDE.md symlink"
  elif [ -f "$ag" ] && [ -f "$cl" ] && [ ! -L "$cl" ]; then
    if cmp -s "$ag" "$cl"; then
      backup "$cl"
      do_or_show "replace duplicate CLAUDE.md with symlink -> AGENTS.md" \
        ln -sfn "AGENTS.md" "$cl"
    else
      note "WARN: AGENTS.md and CLAUDE.md differ — reconcile by hand, then re-run."
    fi
  elif [ -f "$cl" ] && [ ! -f "$ag" ]; then
    do_or_show "rename CLAUDE.md -> AGENTS.md" mv "$cl" "$ag"
    do_or_show "symlink CLAUDE.md -> AGENTS.md" ln -sfn "AGENTS.md" "$cl"
  elif [ ! -f "$ag" ]; then
    do_or_show "seed AGENTS.md (add repo facts + 'profile:' by hand)" \
      bash -c 'printf "# %s\n\n<!-- repo facts, hazards, and: profile: reviewed -->\n" "$1" > "$2"' _ "$(basename "$dir")" "$ag"
    do_or_show "symlink CLAUDE.md -> AGENTS.md" ln -sfn "AGENTS.md" "$cl"
  fi

  # Sweep bare pre-enrollment needs-* labels to the agentflow:* vocabulary (idempotent).
  local gh_repo
  if gh_repo="$(gh repo view "$dir" --json nameWithOwner -q .nameWithOwner 2>/dev/null)"; then
    do_or_show "sweep legacy label vocabulary in $gh_repo (python -m agentflow.enroll $gh_repo)" \
      python -m agentflow.enroll "$gh_repo"
  else
    note "SKIP: could not resolve GitHub repo — run 'python -m agentflow.enroll <owner/repo>' manually"
  fi
}

if [ -n "$REPO" ]; then enroll_repo "$REPO"; else wire_global; fi
[ "$APPLY" = 1 ] || echo "(dry run — nothing changed; pass --apply to execute)"
