# ADR 744 — A build on agentflow itself may edit the pinned screenshot harness

Status: Accepted

Date: 2026-08-17

## Context

The screenshot harness (`scripts/screenshots.mjs`) is pinned by content. Before any session
starts, launch materialization compares the copy in the launch checkout against the digest in
`agentflow/capabilities.toml` and refuses to launch on a mismatch. That refusal is the reason a
session can trust the harness it runs: nobody can quietly substitute a different capture recipe.

The pin has one degenerate case. When the repo being worked on *is* the repo that ships the
harness, editing the harness **is** the assigned work. The builder edits it, the next
continuation's launch check sees bytes that do not match the pin, and refuses with
`capability_environment_failure:incompatible`. The work can never be finished, and any issue
asking for it can never build.

This was not hypothetical. Issue 735 — which asks for a sanctioned repo-local extension seam so
downstream repos stop having to break the digest — burned its entire continuation budget against
this refusal and stopped, held, with roughly 575 lines of finished work stranded uncommitted in
its retained worktree. The gate meant to protect the harness was the thing preventing the harness
from being improved.

## Decision

Launch materialization exempts a drifted harness from refusal when, and only when, all three of
the following hold:

1. **The enrolled source is the repository that ships this package.** Established by comparing the
   source checkout's own GitHub identity against the identity of the checkout the running
   `agentflow` package came from. Both are read through a sanitized git environment
   (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`) so global or system git config cannot
   redirect discovery, and the comparison is anchored at the checkout root — a subdirectory does
   not qualify.
2. **The harness file is tracked in the launch checkout's index.** An untracked file sitting at
   the harness path is still refused. That is the tampering case the pin exists for, and it stays
   blocked.
3. **It is a launch materialization.** Advisory inspection of the main checkout keeps full drift
   detection, so `agentflow doctor` continues to report a drifted harness exactly as before.

Ownership is always derived from the *source*, never from the destination worktree's remote — a
destination remote is attacker-influenceable in a way the packaged source identity is not.

## What this does not change

- **Every other repo is refused exactly as before.** A downstream repo whose harness copy drifts
  still cannot launch. That is the situation issue 735 exists to solve properly, and this decision
  does not solve it.
- **Untracked harness files are still refused**, including on agentflow itself.
- **`agentflow doctor` still reports drift** on the main checkout.
- **The digest itself is unchanged.** A harness change still has to update the pinned digest in
  `agentflow/capabilities.toml` in lockstep; this exemption only stops the gate from deadlocking
  mid-change.

## Alternatives

- **Drop the content pin.** Rejected: the pin is the only thing that makes a session's capture
  recipe trustworthy, and the failure it prevents is silent.
- **Let the builder update the pinned digest as it goes.** Rejected: the digest would then be
  whatever the session last wrote, which is not a pin at all — it authenticates nothing.
- **Key the exemption on the destination worktree's remote.** Rejected: the destination remote is
  the least trustworthy identity available at that point, and keying on it would let any checkout
  claim ownership by setting a remote.
- **Add a manual override flag.** Rejected: an override that a session can set is an override an
  incorrect session will set. The three conditions are checkable facts, not a request.

## Consequences

Issue 735's build can proceed — its live preflight returns `ready` where it returned
`incompatible` before this shipped. Agentflow gains a narrow self-modification path that no other
enrolled repo has, which is correct: it is the only repo for which changing the harness is
in-scope work.

The exemption is deliberately the *narrow* fix. The durable answer is the sanctioned repo-local
extension seam that issue 735 asks for, so that downstream repos never need a digest-breaking edit
in the first place. This decision does not substitute for that, and widening the exemption to
non-owner repos is explicitly out of scope.
