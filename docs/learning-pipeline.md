# Learning pipeline

## Claim ledger

| Claim | Status | Immutable revision | Source/contract | Focused test | ADR | Missing/fail-closed behavior |
| --- | --- | --- | --- | --- | --- | --- |
| `agentflow learning report` observes terminal review/revise outcomes and emits canonical JSON | active | `91aa9ee2d7a15a7a906e25b56f560689c02a2600` | `agentflow/learning.py`, `agentflow/cli.py`, #629 contract | `tests/test_learning.py::test_learning_report_public_command_is_compact_deterministic_and_aggregates` | #629 contract; no separate ADR | Missing, old-schema, or unreadable durable coordinator state exits 2 without JSON, creation, or migration |
| Cohorts use exact repository, UTC half-open dates, terminal records, matching ended telemetry, and immutable pointers | active | `91aa9ee2d7a15a7a906e25b56f560689c02a2600` | #629 cohort contract; Store/ReviewState/AttemptTelemetry readers | `test_learning_report_uses_utc_half_open_window_and_degrades_for_bad_telemetry` | #629 contract; no separate ADR | Malformed telemetry entries are skipped and degrade the report; malformed or unreadable durable coordinator state exits 2 without JSON, creation, or migration; no current config, GitHub text, prompt, transcript, or source fallback |
| Degraded telemetry remains visible and numeric missingness stays explicit | active | `91aa9ee2d7a15a7a906e25b56f560689c02a2600` | #629 telemetry-health contract; `read_attempts_with_health` | `test_learning_report_empty_telemetry_is_complete_and_non_mapping_usage_is_skipped` | #629 contract; no separate ADR | Bad entries count as skipped; unreadable directory yields `status=degraded` and skipped `null` |
| The report has no provider, evaluation, promotion, policy, admission, routing, Safety, or canary action path | active | `91aa9ee2d7a15a7a906e25b56f560689c02a2600` | #629 forbidden-call contract | `test_learning_cli_invokes_no_forbidden_operational_actions` | #629 contract; no separate ADR | The command stays read-only; unavailable durable coordinator state produces no JSON |
| Production admission reads the exact promoted Evaluation authority and composes capability, RouteCell, Safety, and canary attribution facts | active | `9e3838b1251750690c90532eedafee878b077f0e`; dependencies `46e0109a10e08a9ea6a8dc0621dcafde5a1d3d2f`, `4ffde0671ff496feb6cad697e7536bb8e4dc0454`, `80f5a144621a990953d8ccacc08dd93a76090eaa`, `b1ae64543761b808f7c0d357eded8551d684db3a` | `agentflow/pipeline.py`, `agentflow/effective_policy.py`, `agentflow/coordinator/store.py`, ADR 627/628/646/648 | `tests/test_issue_627_admission.py`, `tests/test_evaluation_authority.py` | ADR 627, 628, 646, 648 | Missing, stale, corrupt, or mismatched authority becomes a closed hold; this path is not reached by the learning report |
| The typed Evidence producer pipeline is shipped without a production caller | shipped but unwired | `7a23cccaec0bba24c78ec0d26b505ffe801ccf79` | `agentflow/evidence_pipeline.py`, ADR 596 | `tests/test_evidence_pipeline.py` | ADR 596 | No producer fact is inferred from the learning report; absent wiring means absent output |
| Immutable canary reporting is a content-free report contract, not an action executor | shipped but unwired | `e5a38657cd502a5328caefa21b1eb5ade3ae08f9` | `agentflow/canary_report.py`, ADR 635 | `tests/test_canary_report.py` | ADR 635 | Missing/refused report remains missing; it does not retry, rollback, or block by itself |
| An operator may manually compare two bounded reports after a human-reviewed methodology PR; the comparison remains observational | active | `91aa9ee2d7a15a7a906e25b56f560689c02a2600` | #629 contract; `agentflow/cli.py`; repository PR/merge authority | `tests/test_learning.py::test_learning_report_public_command_is_compact_deterministic_and_aggregates`; `tests/test_learning.py::test_learning_cli_invokes_no_forbidden_operational_actions` | ADR 0003/0004/0047; #629 contract | No automatic later cohort, lesson, prompt, skill, routing, or policy mutation |
| Paired/synthetic provider evaluation, causal claims, automatic mutation, containment activation, broad projection, and slice attribution are not available | deferred | #629 contract; #469 remains open | #629 out-of-scope contract; slice attribution belongs to #469 | `test_learning_cli_invokes_no_forbidden_operational_actions` | ADR 585/635; #469 boundary | Fail closed by omission: no claim or action is emitted |
| Codebase Graph #649 is separate from this learning pipeline | deferred | #649 issue boundary | Codebase Memory/graph work is not a learning-report input | N/A | ADR 0042 | No graph facts are inferred or projected |

## What the pipeline means

The active path is deliberately small:

```text
real terminal outcomes → observational report → human-reviewed methodology PR → later bounded observational cohort
```

The report observes association only. It does not diagnose why a defect
happened, generate lessons, call providers, mutate policy, or activate
Evaluation/Safety/canary actions. Methodology changes require an ordinary
reviewed PR.

Missingness is part of the result. A missing or malformed telemetry entry is
counted or marks the report degraded; unknown duration, tokens, and cost remain
unknown instead of becoming zero. Malformed or unreadable durable coordinator
state uses the normal CLI error path and neither emits JSON nor creates or
migrates state. Durable fact authorities likewise fail closed on unreadable,
stale, mismatched, or unauthorized facts.

A human may use one report to propose a methodology change through an ordinary
reviewed PR. After that change lands, an operator may run a second bounded report
and compare the two reports manually; the comparison remains observational, not
causal.

## Run the report

```bash
uv run agentflow learning report --help
uv run agentflow learning report --repo OWNER/REPO --from YYYY-MM-DD --to YYYY-MM-DD
```

Dates are UTC; `--from` is inclusive and `--to` is exclusive. Output is one
compact, sorted JSON object followed by a newline. It contains counts, rates,
numeric telemetry, stored enums, and bounded source pointers—not finding prose,
prompts, transcripts, source bodies, or credentials.
