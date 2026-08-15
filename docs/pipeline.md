# Understand the pipeline

AgentFlow executes one approved GitHub Build Issue through a durable path:

```text
intake → dispatch → build → review → revise → gate/merge → recover as needed
```

Intake grounds the issue, writes an Agent Brief, and returns one closed route:
`ready`, `mockup`, `grill`, or `nothing-new`. Planning artifacts and every
`wayfinder:*` ticket stay upstream; only a clear Build Issue enters intake.
Malformed output, missing dials, and unreadable routes become a human-visible
hold. They do not become accidental builds.

Dispatch chooses a provider with headroom and reserves durable capacity. Build
uses an isolated worktree and opens a PR. Review examines the exact pushed head.
Revise handles findings, feedback, and conflicts with bounded retries. Gate
checks CI, review proof, taint, collision safety, and repository policy. PR-bound
work drains before new issue work.

## Authority split

GitHub and checked-in repository artifacts own issues, PRs, branch state, CI,
policy, and merge authority. The coordinator owns local claims, attempts,
permits, recovery, and state transitions. Provider adapters report facts; they
do not own pipeline state. The read-only console projects daemon state and never
becomes a planner, tracker, or mutation surface.

The repository autonomy profile changes grounding, review, and merge authority:
`autonomous` may auto-merge after its gates; `reviewed` (the default) needs a
human merge; `guarded` needs full human-controlled review and merge. There is one
pipeline, not one implementation per profile.

## Review, revise, and merge

Reviewers may ship a clear fix. The other tool then inspects the new exact head.
Conflicts are a Revise boundary, not an invitation to force-resolve or restart
the pipeline. Repeated cross-tool disagreement, unavailable required review,
taint, or a policy refusal parks the work for a human. A clean review does not
itself grant merge authority: the profile and repository policy do.

## Recovery

The daemon resumes from durable records, claims, worktrees, and exact launch
facts. It does not duplicate comments, labels, issues, attempts, or claims.
Pause stops cold submissions while the resident daemon observes running work
and settles it. Stopping the process is not a drain; restart the same
coordinator-aware binary. For pause, drain, upgrade, diagnosis, and rollback,
see [Coordinator operations](coordinator-operations.md).
