# Public beta contract

AgentFlow's first public beta is a clone-only release for experienced macOS
operators already using authenticated Claude Code or Codex and GitHub CLI.
There is no PyPI publication, hosted service, account system, or paid support
contract.

## What the beta promises

- A documented clone, configure, validate, and macOS LaunchAgent path.
- A daemon that starts paused and fails closed when required capacity facts are
  unavailable.
- Repository-level autonomy profiles and human gates described in the checked-in
  ADRs.
- Best-effort issue support for the current `main` branch.

The beta does not promise stable Python imports, state or snapshot schemas,
cross-platform operation, uninterrupted provider compatibility, release
backports, response times, or unattended safety outside the configured policy.
The operator remains responsible for repository permissions, agent credentials,
review policy, and the consequences of autonomous commands.

## Publication order

1. Freeze one candidate commit and stop repository writes.
2. Verify the tracked tree, all reachable GitHub refs, collaboration text,
   Actions logs, repository settings, and release artifacts.
3. Run the Python tests, console tests/build, clone-only install smoke test, and
   documentation/link checks against that exact commit.
4. Record the go/no-go decision and candidate commit in issue #137.
5. Change only repository visibility.
6. Immediately enable public-compatible branch protection, private
   vulnerability reporting, dependency/secret scanning, and safe fork-PR
   approval policy.
7. Verify an unauthenticated clone, CI, links, license recognition, artifacts,
   console startup, and repository identity before announcing.

Any drift in the candidate commit, refs, settings, or scan result returns the
release to step 1.

## Rollback boundary

Making the repository private again can restrict future access, but it cannot
recall clones, public forks, caches, archives, or copied collaboration content.
Rollback is containment, not restoration of confidentiality. Suspected
credential disclosure requires rotation first; changing visibility is not a
credential-revocation mechanism.
