"""agentflow enroll — migrate legacy label vocabulary after enrolling a repo.

Run after `enroll-standards.sh --apply <dir>` to sweep any bare pre-enrollment
needs-grilling / needs-mockup labels to the agentflow:* form.

Usage: python -m agentflow.enroll <owner/repo>
"""

import sys

from agentflow.intake import sweep_legacy_labels


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: python -m agentflow.enroll <owner/repo>", file=sys.stderr)
        sys.exit(2)
    repo = sys.argv[1]
    print(f"Sweeping legacy labels in {repo}...")
    changed = sweep_legacy_labels(repo)
    if not changed:
        print("  nothing to change — all issues already use agentflow:* vocabulary")
    else:
        for line in changed:
            print(f"  {line}")
        print(f"  {len(changed)} issue(s) updated")


if __name__ == "__main__":
    main()
