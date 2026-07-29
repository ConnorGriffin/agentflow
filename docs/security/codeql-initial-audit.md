# Initial CodeQL audit

Issue #351 reconciles the first public CodeQL run at
`3c98a818d3b08837f1fca015e38f866276d2d3b4` against the fresh default-setup run
at `db470640906a781a0e6e5032aadde715f92301b8`.

## Inventory reconciliation

| Analysis | Initial | Fresh `main` |
| --- | ---: | ---: |
| Actions | 0 | 0 |
| JavaScript/TypeScript | 16 | 16 |
| Python | 103 | 82 |
| Total SARIF results | 119 | 98 |

The fresh run contains 96 findings from the initial inventory and two later path
findings, alerts
[#130](https://github.com/ConnorGriffin/agentflow/security/code-scanning/130) and
[#131](https://github.com/ConnorGriffin/agentflow/security/code-scanning/131).
Those two were already dismissed with separate rationales: `AGENTFLOW_STATE` is
the operator-selected state root, and an enrolled checkout is an
operator-selected repository root. Twenty-three initial path findings no longer
appear in fresh SARIF. No new command, URL, JavaScript path, mockup, or Actions
finding appeared.

The 119 initial findings resolve as follows:

| Disposition | Count |
| --- | ---: |
| No longer present on fresh `main` | 23 |
| Trust-boundary fix in issue #351 | 9 |
| Production false positive | 68 |
| Test or non-shipping mockup | 19 |
| **Total** | **119** |

CodeQL default setup remains enabled for Actions, JavaScript/TypeScript, and
Python. After the required integration rebase, the later default-setup run at
`f9bb4e75d6d010f4a3321c5bc546735b37c5ae23` also completed all three analyses
successfully.

## Command findings

All six sinks receive an argument vector. None receives a command string and
none sets `shell=True`, so the Python subprocess default `shell=False` applies.
Prompt, issue, repository, and attachment text can occupy one argument but
cannot add arguments. Executable selection is either code-authored or explicit
operator configuration.

| Alert | Taint source and transformations | Executable and argument boundary | Working directory and shell | Disposition |
| --- | --- | --- | --- | --- |
| [#17](https://github.com/ConnorGriffin/agentflow/security/code-scanning/17) | CodeQL starts at the private child supervisor's `sys.argv`. The local launcher creates that vector from the coordinator store path, record identity, launch token, timeout, owned worktree, and the provider adapter's vector. Claude and Codex adapters append issue or operator text as one prompt argument after their code-authored flags. | The detached bootstrap executable is `sys.executable`. At the reported provider sink, Claude's executable is the code-authored `claude`; Codex's is `AGENTFLOW_CODEX_BIN` (operator configuration, default `codex`). | The coordinator-owned stage worktree; argument-list `Popen`, `shell=False`. | False positive. No repository, issue, PR, attachment, or prompt value selects the executable or changes vector structure. |
| [#18](https://github.com/ConnorGriffin/agentflow/security/code-scanning/18) | `AGENTFLOW_CAPACITY_HELPER`, with the legacy `AGENTFLOW_TRIAGE_GATE` fallback, is read once as operator configuration. | The configured local helper with the fixed argument `check`. | Inherited daemon directory; argument-list `run`, `shell=False`. | False positive. The operator selects the helper; remote content reaches neither executable nor arguments. |
| [#19](https://github.com/ConnorGriffin/agentflow/security/code-scanning/19) | Same operator-configured helper; the second check only adds the code-authored `TRIAGE_SKIP_ACTIVITY=1` environment fact. | The configured local helper with the fixed argument `check`. | Inherited daemon directory; argument-list `run`, `shell=False`. | False positive. |
| [#20](https://github.com/ConnorGriffin/agentflow/security/code-scanning/20) | Same operator-configured helper. | The configured local helper with the fixed argument `limits`. | Inherited daemon directory; argument-list `run`, `shell=False`. | False positive. |
| [#21](https://github.com/ConnorGriffin/agentflow/security/code-scanning/21) | The reported flows carry the private launcher's working directory or the `enroll` command's operator-selected checkout. They become `cwd` or an argument after a literal `-C`; they never become element zero. The shared runner also refuses provider executables. | The traced calls use code-authored `git` argument vectors. | Operator-selected enrolled checkout or coordinator-owned worktree; argument-list `run`, `shell=False`. | False positive. A path can be a single `git` argument or `cwd`, not an executable or structural argument. |
| [#22](https://github.com/ConnorGriffin/agentflow/security/code-scanning/22) | `AGENTFLOW_CAPACITY_HELPER`, with the legacy fallback, is explicit operator configuration. | The configured local helper with the fixed argument `limits`. | Inherited daemon directory; argument-list `run`, `shell=False`. | False positive. |

Notification dispatch was also checked because it is adjacent to these flows,
although it was not one of the six alerts. Its executable is the literal
`curl`; title, message, click URL, and operator-configured ntfy URL are separate
arguments; the working directory is inherited and `shell=False`.

## Python path findings

The table accounts for all 96 initial `py/path-injection` alerts. “Operator”
means a local path deliberately supplied through process configuration or a CLI
argument. “Internal” means a fixed segment, UUID, digest, sanitized repository
slug, or coordinator-owned record identity. Neither category is derived from
repository, issue, PR, attachment, or provider prompt data.

| Alerts | Count | Trust source and intended root | Result |
| --- | ---: | --- | --- |
| #23–#27 | 5 | Private launcher arguments: operator-owned coordinator store plus internal launch token and stage worktree. Roots are the coordinator state directory and enrolled checkout's owned worktree. | Dismiss: false positives. The public launcher constructs every argument; prompt text is not parsed as a path. |
| #28–#34 | 7 | Operator state root plus the public workspace command key under `workspace/commands`. The key was used in spool filenames and acknowledgement. | **Confirmed and fixed.** Keys are bounded identifiers, filenames are SHA-256 digests, all spool directories are resolved beneath the state root, symlink entries are ignored, and invalid keys fail closed. |
| #35 | 1 | Operator `AGENTFLOW_STATE`; fixed `capacity.json`. | Dismiss: false positive. |
| #36 | 1 | Operator `AGENTFLOW_STATE`; fixed daemon lock and state names used by the public CLI. | Dismiss: false positive. |
| #37–#38 | 2 | Operator `AGENTFLOW_CONFIG` or `XDG_CONFIG_HOME`; the config file is the object the operator asked agentflow to read. | Dismiss: false positives. |
| #39–#52 | 14 | Operator state root; fixed enable flag, daemon lock, PID, stale-lock, and snapshot artifacts. | Dismiss: false positives. Repository and issue values never name these files. |
| #53–#61 | 9 | The `enroll` CLI's operator-selected checkout root; enrollment inspects or writes fixed code-authored files beneath that checkout. | Dismiss: false positives. Choosing the checkout is the command's purpose. |
| #62–#63 | 2 | Coordinator store and owned worktree paths. | Superseded before the fresh run when stage ownership moved behind the current coordinator/worktree interfaces. |
| #64–#68 | 5 | Operator state root; fixed daemon-status and snapshot files. | Dismiss: false positives. |
| #69–#71 | 3 | Installed executable, current user's `Library` directories, and operator runtime/helper paths used to install the per-user LaunchAgent. | Dismiss: false positives. No remote input participates. |
| #72–#77 | 6 | Coordinator store root and internal quota-fact names. | Superseded before the fresh run by the contained state-path implementation. |
| #78–#79 | 2 | The `enroll` CLI's operator-selected checkout root. | Superseded before the fresh run by the pipeline/stage refactor. |
| #80–#88 | 9 | Operator state root or an explicit test/maintenance path; fixed `ratchet.json`. Repository names are JSON keys, not path segments. | Dismiss: false positives. |
| #89–#93 | 5 | Operator-owned coordinator store plus internal UUID launch token. Session files live beside that one store. | Dismiss: false positives. The private child cannot receive repository or prompt text as store/token structure. |
| #94–#96 | 3 | Coordinator store and internal session token. | Superseded before the fresh run by the durable session-result implementation. |
| #97–#101, #109 | 6 | Explicit coordinator store path. Production uses the one contained default under the operator state root; alternate paths are constructor injection for tests/maintenance. Temp names use PID and UUID. | Dismiss: false positives. |
| #102–#103 | 2 | Operator state root plus a filesystem-safe repository slug under `workspace`. | **Hardened in issue #351.** The workspace now enters through the contained state-path interface, so absolute, `..`, and symlink escapes are refused. |
| #104–#108 | 5 | Coordinator store and internal telemetry names. | Superseded before the fresh run by the current telemetry layout. |
| #110 | 1 | `tests/test_cli.py` temporary output selected by a pytest temporary-path fixture. | Dismiss: used in tests; it does not ship. |
| #111–#115 | 5 | Test-selected temporary coordinator stores. | Superseded before the fresh run; the test findings no longer exist. |
| #116–#118 | 3 | `tests/test_live.py` temporary snapshot/status files selected by pytest fixtures. | Dismiss: used in tests; they do not ship. |
| **Total** | **96** |  |  |

The confirmed command-spool bug is exercised through `POST /api/command` with
absolute, `..`, and nested path keys and with a workspace symlink that resolves
outside the state root. The acknowledgement interface is separately exercised
against a traversal key. A compatibility test proves a safely named command
left by the previous release is migrated and remains drainable.

## Other findings

| Alerts | Audit | Disposition |
| --- | --- | --- |
| [#1](https://github.com/ConnorGriffin/agentflow/security/code-scanning/1) | The screenshot harness reads the config path passed by the operator as its sole positional CLI argument. Reading that selected file is the command's interface; repository, issue, and page content do not select it. | Dismiss: false positive. |
| [#2–#16](https://github.com/ConnorGriffin/agentflow/security/code-scanning/2) | Every result is in `mockups/dashboard-v2-combined.js`. This committed locked mockup is design evidence, not a shipping asset: the web server serves `agentflow/webui/dist`, and the Svelte source/build neither imports nor serves the mockup script. Its fixture data is code-authored. | Dismiss each alert with its exact sink location as non-shipping mockup evidence. |
| [#119](https://github.com/ConnorGriffin/agentflow/security/code-scanning/119) | The substring checks only recognize image-looking links in issue Markdown. Credential attachment is a separate decision: URLs are parsed, must use HTTPS, and must have the exact `github.com` or `user-images.githubusercontent.com` hostname. Redirect targets are fetched with no authorization header. | Dismiss: false positive. The reported substring never authorizes a request. |

Related current flows were checked even where the initial run had no alert.
Attachment bodies and URLs never name a staged path: intake writes a fixed
`attachment-<index>` name with an extension selected from recognized magic
bytes beneath the coordinator-owned intake worktree. Worktree references
sanitize issue titles and repository identities before joining them beneath an
operator-selected enrolled checkout.

## GitHub dispositions

False positives are dismissed at their individual alert locations, not with a
blanket comment:

- command alerts name the source, executable, fixed structural arguments,
  working directory, argument-list boundary, and `shell=False`;
- path alerts name the operator/internal source and exact filesystem root;
- test alerts name the temporary-path fixture and use the “used in tests”
  reason;
- every mockup alert names its own sink location and records that the file is
  locked design evidence outside the served build;
- the JavaScript path and Python URL alerts record the exact CLI and
  authentication boundaries above.

Alerts #28–#34 and #102–#103 are not dismissed; they are resolved by the
contained workspace implementation and should close from the post-merge
analysis.
