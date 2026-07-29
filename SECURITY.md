# Security policy

## Supported versions

AgentFlow is a clone-only beta. Only the current `main` branch is supported.
There are no maintained release branches or security backport guarantees.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** form in the repository's Security tab.
Do not disclose suspected vulnerabilities, credentials, private repository
data, or exploit details in a public issue.

If private vulnerability reporting is unavailable, open a public issue that
only asks the maintainer to establish a private contact channel. Include no
sensitive details.

Reports should include the affected commit, impact, reproduction conditions,
and any known mitigation. Receipt and remediation are best effort; this beta
has no response-time or disclosure-time SLA. The maintainer will coordinate
credit and disclosure timing with the reporter when practical.

## Scope

AgentFlow launches authenticated local coding agents and GitHub tooling with the
operator's existing permissions. Reports involving command execution,
credential handling, untrusted repository content, GitHub Actions, filesystem
boundaries, or unintended publication are in scope. Provider, GitHub, Git, and
operating-system vulnerabilities should be reported to their respective
maintainers unless AgentFlow creates or amplifies the issue.
