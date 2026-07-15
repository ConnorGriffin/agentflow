# Historical session demand for admission permits

Research for [Research historical session demand for static admission permits](https://github.com/ConnorGriffin/agentflow/issues/90), captured 2026-07-15.

## Conclusion

The history supports a small number of conservative demand bands, not a precise value for every model/effort cell yet:

- Triage is consistently the lightest stage. Deep triage stayed below `0.45m` gate-weighted tokens at P90 on both pools.
- Review is also short on Claude (`0.29m` at P90), but Codex review is materially heavier (`1.84m`) because a root session may fan out to subagents.
- Every code-writing stage should have an admission demand of at least three permits in a five-permit pool. This is a safety invariant rather than a claim that every small build burns three times a triage: it guarantees that two sessions owning changes cannot overlap on one pool.
- High/extra builds and Codex builds should start in the exclusive or near-exclusive band. The observed upper quartiles are large, while several cells have fewer than five completed roots.
- Unknown or undersampled combinations should inherit a heavier neighboring band, never a lighter one. The matrix should be monotone with effort and model depth even where this small sample is not.

Those constraints support a static five-permit matrix with triage at `1`, Claude review at `1`, Codex review at `2`, all code-writing work at `>=3`, and the sparse/heavy cells at `5`. The exact `3/4/5` assignments belong to the follow-on matrix decision; this evidence does not justify finer resolution.

## Aggregate evidence

“Spend” below is the same provider-local weighted-token proxy used by `triage-gate.sh`; Claude and Codex values must not be compared as if they were one currency. P75 is the calibration statistic; P90 shows tail risk. A dagger marks fewer than five completed root sessions, so the result is directional only.

### Builds

| Pool / model | Complexity | Effort | n | Spend P75 | Spend P90 | Duration P90 |
|---|---|---:|---:|---:|---:|---:|
| Claude / Sonnet | standard | low | 10 | 0.39m | 0.45m | 7.0m |
| Claude / Sonnet | standard | medium | 10 | 0.81m | 0.97m | 15.5m |
| Claude / Opus | deep | low | 3† | 0.35m | 0.43m | 10.2m |
| Claude / Opus | deep | medium | 8 | 0.57m | 0.64m | 8.5m |
| Claude / Opus | deep | high | 14 | 2.01m | 2.74m | 24.8m |
| Claude / Opus | deep | extra | 1† | 2.68m | 2.68m | 27.2m |
| Codex / Terra | standard | low | 5 | 1.03m | 1.11m | 5.1m |
| Codex / Terra | standard | medium | 9 | 2.63m | 8.15m | 17.4m |
| Codex / Terra | standard | high | 2† | 6.85m | 7.82m | 15.3m |
| Codex / Sol | deep | low | 3† | 5.49m | 7.78m | 17.1m |
| Codex / Sol | deep | medium | 4† | 3.10m | 3.73m | 11.0m |
| Codex / Sol | deep | high | 3† | 7.23m | 8.20m | 26.1m |

There were no completed current-model build samples for Sonnet standard high/extra, Terra standard extra, or Sol deep extra. The non-monotone Sol low/medium result is another reason to enforce a conservative monotone matrix rather than copy raw percentiles.

### Other current-policy stages

| Pool / model | Stage | Complexity | Effort | n | Spend P75 | Spend P90 | Duration P90 |
|---|---|---|---:|---:|---:|---:|---:|
| Claude / Opus | triage | deep | n/a | 60 | 0.30m | 0.38m | 3.9m |
| Claude / Opus | review | deep | n/a | 26 | 0.26m | 0.29m | 2.3m |
| Claude / Opus | revise | deep | n/a | 3† | 0.42m | 0.56m | 6.6m |
| Claude / Opus | respond | deep | n/a | 2† | 0.10m | 0.11m | 45.8m |
| Codex / Sol | triage | deep | n/a | 40 | 0.27m | 0.45m | 3.4m |
| Codex / Sol | review | deep | n/a | 31 | 1.41m | 1.84m | 5.8m |
| Codex / Sol | revise | deep | n/a | 3† | 1.06m | 1.56m | 6.2m |
| Codex / Sol | mockup | deep | n/a | 4† | 3.70m | 4.05m | 9.4m |

No completed Codex responder or Claude mockup root was present. Historical Sonnet/Terra review and revise sessions were measured but are omitted from this current-policy table because review now always uses the deep model; the code makes model depth a real pipeline rule, not an inference from transcript text ([runner model mapping](https://github.com/ConnorGriffin/agentflow/blob/e5753e2a86ce12ce029377ef1608551fb0d4f944/agentflow/runner.py#L339-L350), [deep review selection](https://github.com/ConnorGriffin/agentflow/blob/e5753e2a86ce12ce029377ef1608551fb0d4f944/agentflow/reviewer.py#L208-L220)).

## Claude headroom at the known limit

The transcript scan found two explicit “session limit” events in agentflow worktrees. They are one exhaustion episode, not two independent observations:

| Event | Proxy spent | Proxy free |
|---|---:|---:|
| Deep/extra build launched at 2026-07-15 10:02:15Z | 23.2% | 76.8% |
| That build reported the limit at 10:17:38Z | 28.5% | 71.5% |
| A deep review launched at 10:29:04Z and immediately reported the same limit | 28.5% | 71.5% |

At launch, three Claude roots began within 1.2 seconds: a standard/medium build, a deep triage, and the deep/extra build that later stopped. Because the proxy is reconstructed from usage events already written to transcripts, it could not reserve any of that newly admitted demand. The calibrated proxy therefore said more than 70% remained at both real provider-limit events.

The proxy is doing what its source defines: sum a rough weighted token formula over the trailing five hours and divide by a historical peak of `38,447,725` ([weighting](https://github.com/ConnorGriffin/dotfiles/blob/9f8b251aac0630e0c33fdee68b23aa3cd566eef3/scripts/triage-gate.sh#L152-L165), [peak calibration](https://github.com/ConnorGriffin/dotfiles/blob/9f8b251aac0630e0c33fdee68b23aa3cd566eef3/scripts/triage-gate.sh#L347-L371), [spend calculation](https://github.com/ConnorGriffin/dotfiles/blob/9f8b251aac0630e0c33fdee68b23aa3cd566eef3/scripts/triage-gate.sh#L407-L417)). It is not a provider-authored quota fact and has no reservation for sessions just admitted. This episode shows it is unsuitable as the correctness boundary for concurrent admission. Keep it as the cumulative-spend signal; use atomic permits for burst control.

## Reproducible method

1. Scan `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl` for root sessions whose working directory contains `/.agentflow/worktrees/`. The included completed roots span 2026-07-09 22:51:58Z through 2026-07-15 16:36:07Z.
2. Classify the stage from the stable harness prompt preamble: build, revise, respond, review, intake/triage, or mockup. These preambles and the build effort field are defined in the harness ([build/revise/respond prompts](https://github.com/ConnorGriffin/agentflow/blob/e5753e2a86ce12ce029377ef1608551fb0d4f944/agentflow/loop.py#L168-L235)). Read the actual model from transcript metadata and map it to the repository's standard/deep vocabulary ([complexity and effort](https://github.com/ConnorGriffin/agentflow/blob/e5753e2a86ce12ce029377ef1608551fb0d4f944/agentflow/runner.py#L39-L56)). Effort is `n/a` outside builds because the harness does not apply that dial there.
3. Exclude unclassified or zero-usage roots. Count a Claude root as completed only when its last real assistant message has `stop_reason: end_turn`; count a Codex root only when it has `task_complete` and no `turn_aborted`. Also exclude the two Claude roots carrying the explicit limit event. This leaves 164 Claude and 145 Codex completed, classified roots.
4. Charge every Codex descendant recursively to its root. The 149 observed Codex roots spawned 176 descendants across 84 roots, with at most four descendants for one root. Omitting them would systematically understate the admission demand of the reservation that allowed the root to start.
5. Reproduce the gate's formulas exactly: Claude = input + `1.25 × cache_creation_input` + `5 × output`; Codex = uncached input + `0.25 × cached input` + `5 × (output + reasoning output)`. Sum event weights per root family. Duration is first-to-last transcript timestamp across the family.
6. Group by pool, root model, stage, complexity, and build effort; calculate linearly interpolated P75/P90 values. Emit aggregates only—no prompts, repository contents, secrets, or unrelated transcript text.
7. For each explicit Claude limit timestamp, re-run the gate's trailing-five-hour calculation over all Claude assistant usage events immediately before that timestamp and divide by the locally calibrated peak.

The sample covers six days under changing harness behavior and includes sparse cells. It is suitable for a conservative initial matrix and replay test, not for self-tuning or claims about long-run provider capacity.

## Decision supported

Use static, per-pool admission permits as burst control, keep headroom as a separate cumulative signal, and calibrate the first matrix from conservative monotone bands rather than raw cell-by-cell percentiles. Record root-plus-descendant actual demand after launch so a later reviewed change can recalibrate from a larger sample.

This research itself does not warrant an ADR. The admission matrix and its invariants will constrain multiple builds and should be recorded when the follow-on decision sets them.
