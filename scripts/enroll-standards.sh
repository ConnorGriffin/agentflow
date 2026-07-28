#!/usr/bin/env bash
#
# enroll-standards.sh — wire shared global instructions and the engineering
# charter into BOTH tools with zero drift. See ADRs 0013 and 0032.
#
# Each concern has one canonical file:
#   - machine global: dotfiles/agents/AGENTS.md is canonical; both tools symlink it
#   - engineering charter: both tools follow the global's charter reference
#   - per repo: AGENTS.md is canonical; CLAUDE.md symlinks to it
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
# Verified compatibility facts:
#   - that Claude honors an `@<path>` import line in the shared global file
#   - that Codex reads a *symlinked* ~/.codex/AGENTS.md and follows referenced docs
# Codex reference traversal was smoke-tested in an ephemeral read-only session.

set -euo pipefail

CHARTER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/standards/CHARTER.md"
SHARED_GLOBAL="$HOME/Code/ConnorGriffin/dotfiles/agents/AGENTS.md"
CLAUDE_GLOBAL="$HOME/.claude/CLAUDE.md"
CODEX_GLOBAL="$HOME/.codex/AGENTS.md"
RETIRED_CLAUDE_GLOBAL="$HOME/Code/ConnorGriffin/dotfiles/claude/CLAUDE.md"
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

wire_global_link() { # <tool> <target> <retired-link-source>
  local tool="$1" target="$2" retired_src="$3"

  if [ -L "$target" ] && [ "$(readlink "$target")" = "$SHARED_GLOBAL" ]; then
    note "ok:   $tool global already symlinks the shared global"
  elif [ -L "$target" ] && [ "$(readlink "$target")" = "$retired_src" ]; then
    do_or_show "migrate $tool global -> shared global" ln -sfn "$SHARED_GLOBAL" "$target"
  elif [ -e "$target" ] && [ -s "$target" ]; then
    backup "$target"
    note "WARN: $target is non-empty — review $target.pre-agentflow; refusing to clobber it."
  elif [ -L "$target" ]; then
    note "WARN: $target points to an unknown source — refusing to clobber it."
  else
    do_or_show "create $tool config directory" mkdir -p "$(dirname "$target")"
    do_or_show "symlink $tool global -> shared global" ln -sfn "$SHARED_GLOBAL" "$target"
  fi
}

wire_global() {
  echo "Global instruction wiring — canonical: $SHARED_GLOBAL"

  if [ ! -f "$SHARED_GLOBAL" ]; then
    note "ERROR: shared global is missing: $SHARED_GLOBAL"
    return 1
  fi

  # Keep the charter canonical and referenced by the shared global.
  if grep -qF "$IMPORT_LINE" "$SHARED_GLOBAL"; then
    note "ok:   shared global already imports the charter ($SHARED_GLOBAL)"
  else
    backup "$SHARED_GLOBAL"
    do_or_show "append charter @import to $SHARED_GLOBAL" \
      bash -c 'printf "\n# --- agentflow engineering charter (managed) ---\n%s\n" "$1" >> "$2"' _ "$IMPORT_LINE" "$SHARED_GLOBAL"
  fi

  wire_global_link "Claude" "$CLAUDE_GLOBAL" "$RETIRED_CLAUDE_GLOBAL"
  wire_global_link "Codex" "$CODEX_GLOBAL" "$CHARTER"
}

enroll_repo() { # <dir>
  local dir; dir="$(cd "$1" && pwd -P)"
  local ag="$dir/AGENTS.md" cl="$dir/CLAUDE.md" ignore="$dir/.gitignore"
  echo "Per-repo enroll — $dir"

  if [ -f "$ignore" ] && grep -qxF '.agentflow/' "$ignore"; then
    note "ok:   .agentflow/ already ignored"
  else
    backup "$ignore"
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
    # The seeded `ui-surfaces:` line starts every repo declared. `none` is the safe seed —
    # it leaves the UI-evidence gate inert, exactly as an unenrolled repo is today — but a
    # repo with a frontend must be corrected, or a visual change merges without proof.
    # The seed is provisional, not an answer: the surfaces command below rewrites it when the
    # checkout has a UI, and the fleet audit names any repo still claiming to be headless.
    do_or_show "seed AGENTS.md (add repo facts + 'profile:' by hand)" \
      bash -c 'printf "# %s\n\n<!-- repo facts, hazards, and: profile: reviewed -->\n<!-- ui-surfaces: comma-separated path prefixes of this repo'"'"'s user-facing surfaces,\n     or none when it is headless on purpose (ADR 0018) -->\nui-surfaces: none\n" "$1" > "$2"' _ "$(basename "$dir")" "$ag"
    note "NOTE: seeded 'ui-surfaces: none' provisionally. If this repo has a UI, correct it —"
    note "      'python -m agentflow.enroll surfaces $dir --apply' rewrites the seeded line."
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
