#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
script="$root/scripts/provider-discovery-probe.sh"
test "$("$script" codex print-command)" = 'provider=codex mode=runner-equivalent pinned-project-local-contract'
if "$script" unknown print-command >/dev/null 2>&1; then exit 1; fi
if "$script" codex unknown >/dev/null 2>&1; then exit 1; fi
echo 'probe helper checks passed'
