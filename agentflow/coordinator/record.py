"""The durable continuation record — one per logical stage (ADR 0028).

This is the entity the store persists and the coordinator transitions through its four
states. Its ``running`` rows are the only permit ledger (ADR 0029/0030): there is no
second counter to reconcile. The coordinator owns every transition; nothing outside the
coordinator package should mutate a record's state, attempts, or reservation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The four persisted states (ADR 0028). `completed`/`held` are reconciliation states that
# retire once ownership transfers or the durable boundary is confirmed.
WAITING = "waiting"
RUNNING = "running"
COMPLETED = "completed"
HELD = "held"

# The durable launcher-handshake results (ADR 0030). Defined here, beside the record they are
# written onto, so the store can guard on them without importing the launcher (which imports
# the store); the launcher re-exports them.
STARTED = "started"
NOT_STARTED = "not_started"

# --- the preparation-refusal clock (#406) -------------------------------------------------
# A stage refused *before* launch reserves nothing and consumes no attempt, so exhaustion never
# calls anyone: one scratch file stalled a review for half an hour with the operator none the
# wiser (#399). These bounds are how long a refusal a human must clear may go unremarked, and
# then unactioned. They live beside the fields they read — like the launcher-handshake results
# above — so the published projection can honor them without importing the coordinator.

#: Two observations further apart than this are not one continuous refusal, so the clock restarts.
#: Head-of-line ordering can leave a record unoffered for a long while, and unobserved wall time
#: must never count toward escalating something nobody was watching.
STALL_OBSERVATION_MAX_GAP = 600
#: How long a continuously observed, human-clearable refusal runs before it is called stalled —
#: logged periodically and published — while still costing nothing and retrying every cycle.
STALL_STALLED_AFTER = 600
#: And how long before the stage gives up waiting and hands the work to a human.
STALL_PARK_AFTER = 3600
#: How often the stalled line reprints while the refusal persists.
STALL_LOG_EVERY = 600


@dataclass
class Record:
    """One logical stage's continuation state. Only the coordinator writes these fields."""

    identity: str          # stable (repo, subject, stage, target) key — submission is idempotent on it
    stage: str             # logical stage: intake | build | review | revise | mockup | respond
    pool: str              # claude | codex — the pool this record is charged against
    demand: int            # permits this attempt reserves on `pool` (from the admission matrix)
    repo: str = ""         # the originating repo — kept for the ADR 0028 logs and claim ownership
    subject: str = ""      # the issue/PR subject — kept for the ADR 0028 logs and claim ownership
    target: str | None = None  # immutable target (head SHA / comment id), part of the identity
    # #627 binds a logical stage to the one source observation and one immutable route chosen
    # at submission.  Empty values are readable legacy sentinels only; composed admission
    # refuses them rather than reconstructing them from mutable state.
    subject_revision: str = ""
    route_id: str = ""
    route_cell_digest: str = ""
    launch_config_digest: str = ""
    continuation: bool = False   # eligible ahead of cold work on its pool (ADR 0028 order)
    eligible_at: int = 0         # when a paused continuation may be admitted again
    created_at: int = 0          # epoch of first submission: continuation-queue tie-breaker, and
                                 # the anchor a Revise binds its non-code evidence to (issue #118)
    model: str = "opus"
    complexity: str = "deep"
    effort: str | None = None
    attempts: int = 0            # provider attempts consumed (initial + up to two continuations)
    attempt_committed: bool = False  # this attempt's count is already consumed (idempotent on recovery)
    daemon_generation: str | None = None  # the daemon-lifecycle identity that admitted the current
                                          # attempt; a family that dies leaving no supervisor end fact
                                          # under a *different* generation was taken down with the
                                          # daemon (restart/reboot), so its attempt resumes uncharged
    collision_main_sha: str | None = None  # the `origin/main` head a Build reported an integration
                                            # collision against; a continuation stays deferred while
                                            # main still equals it (an identical retry), and a second
                                            # collision hands the issue off instead of retrying (#209)
    restart_resumes: int = 0     # consecutive restart-resumes of the current attempt — bounded, so a
                                 # session that keeps dying with no end fact still parks eventually
    repairs: int = 0             # targeted repairs granted for a clean exit that only left its
                                 # required outcome missing; bounded so a read-only stage cannot
                                 # replay an identical no-outcome session more than once (issue #225)
    recovery_envelope: str | None = None  # bounded durable facts (attempt number, missing outcome,
                                          # retained worktree) stamped on a continuation so the fresh
                                          # session resumes instead of replaying the same prompt (#225)
    state: str = WAITING
    claim: bool = True           # holds the GitHub dedup claim while the stage is owned
    lineage: str | None = None   # pinned tool for code-writing stages; None once free to move
    source: str | None = None    # durable working-directory/worktree pointer for provider launch
    input_ptr: str | None = None # durable pointer the provider adapter rebuilds the prompt from
    capability_root: str | None = None  # checkout root whose project-local contracts are authoritative
    capability_context: str = "{}"     # serialized conditional prompt context
    capability_preflight: str = ""      # serialized non-ready preflight; retained across restart
    session_lead: bool = False   # missing historical JSON decodes false; never infer from model
    outcome: str | None = None   # stage-native durable outcome, captured before external projection
    started_at: int = 0                  # epoch when the current attempt was admitted
    deadline: int = 0                    # supervisor observe-until deadline, for the recovered-running log
    start_fact: str | None = None        # durable launcher handshake result: started | not_started
    launch_token: str | None = None      # nonce a reservation stamps; only the child holding it may record `started`
    revision: int = 0                    # optimistic concurrency generation; every durable write advances it
    family: str | None = None            # the provider process-family identity a `started` carries
    process_alive: bool = False          # whether that family is still executing
    descendants: set[str] = field(default_factory=set)  # subagents charged to the root reservation
    handoffs: int = 0
    handoff_kind: str | None = None
    notifications: int = 0
    handoff_proof: str | None = None     # proof the stage-native human handoff exists (crash-safe)
    hold_pending: bool = False           # classified as a hold, awaiting its durable handoff
    hold_reason: str | None = None       # exhaustion, permanent cause, or no-successor boundary
    refusal: str = ""                    # why this record is still waiting, as of the latest cycle
                                         # ("check: live detail") — the preparation collaborator or
                                         # the admission gate that refused it. Cleared the moment
                                         # nothing refuses, so it always describes now, never a
                                         # cause that has since gone away (#405)
    refusal_expected: bool = False       # that refusal is ordinary contention (a live sibling still
                                         # holding a checkout), not something a human should chase
    refusals: int = 0                    # consecutive cycles something has refused to prepare this
                                         # record, durable so the periodic breadcrumb survives a
                                         # daemon restart instead of re-arming at zero (#406);
                                         # zeroed the moment preparation succeeds
    stall_refusal_id: str = ""           # the stable typed check id of the human-clearable refusal
                                         # currently being clocked — the *identifier*, never the
                                         # mutable detail, so continuity survives a checkout whose
                                         # live values change under an unchanged cause
    stall_started_at: int = 0            # when that refusal was first observed continuously
    stall_last_observed_at: int = 0      # and when it was last observed, so a long gap in
                                         # observation restarts the clock rather than crediting
                                         # elapsed time nobody was watching
    verify_miss: str = ""                # the last attempt's first failed verification conjunct
                                         # ("check: live detail") — named in recovery envelopes,
                                         # hold reasons, telemetry, and the park comment, so a
                                         # park states what stopped it instead of "budget
                                         # exhausted"; cleared whenever an attempt verifies
    retired: bool = False
    builder_lineage: str | None = None   # original builder; current exact-head author is separate
    branch_lineage: str | None = None    # tool whose retained checkout owns the PR branch; an
                                         # orchestrated Revise can have a different Claude lead
    builder_complexity: str | None = None  # the original builder complexity, carried so a later
                                           # Revise never re-reads a mutable issue label (ADR 0018)
    builder_effort: str | None = None    # the original builder effort, carried separately so Review
                                         # keeps provider-default reasoning while Revise inherits it
    round: int = 0                       # completed auto-revise rounds behind this stage; part of
                                         # the identity so an evidence-only revision's re-review at
                                         # the same head SHA is still a fresh stage
    conflict_round: int = 0              # which conflict Revise this is in the PR's lifetime (ADR
                                         # 0038); >0 marks a survivor conflict resolution, budgeted
                                         # separately from the finding-driven `round` and joined to
                                         # the identity so each conflict is a genuinely fresh stage
    resume: int = 0                      # deliberate maintainer resume identity: an exhausted Build
                                         # (#245), or a manual Review kept distinct from an automatic
                                         # moved-head retarget (#501)
    auto_merge_allowed: bool = True
    review_depth: str = "targeted"       # focused | targeted | full (ADR 0047)
    depth_reason: str = ""
    review_axis: str = "combined"        # combined | product | standards | fix
    change_author_tool: str | None = None # author of the exact change set under review
    reviewed_from_sha: str | None = None  # start of the range supplied to this pass
    review_prior_push: str | None = None  # a moved final head whose push provenance is an earlier
                                          # attempt of this same logical review, proven at verify
                                          # time by the retained checkout owning it — settlement
                                          # re-parses the captured verdict with this durable fact
                                          # instead of re-reading a checkout that may be gone
    review_passes: int = 0                # consecutive change-making review passes
    review_sequence: int = 0              # same-head private handoffs/Full axes identity
    cross_tool_covered: bool = False      # another tool reviewed the current exact head
    review_tainted: bool = False          # same-tool override: human merge only
    review_taint_cleared: bool = False    # later exact-head cross-tool review cleared it
    review_handoff: str | None = None     # private bounded next-agent context
    review_findings: str = "[]"           # JSON action ledger across Full read-only axes
    review_fixes: str = "[]"              # JSON ledger; kept opaque by the coordinator
    review_follow_ups: str = "[]"         # historical references plus one current proposal
    review_checks: str = "[]"              # JSON proof ledger across serialized passes
    review_uncertainty: str | None = None # private options/guidance/recommendation JSON
    uncertainty_handoffs: int = 0         # at most one narrow cross-tool decision handoff
    root: str | None = None              # the root stage this descends from; it shares the root's reservation
    interactive: bool = False            # an operator-present turn (Ask) that outranks background
                                         # pipeline work at admission (ADR 0034); sorts to the head
                                         # of the queue and is exempt from the headroom/pacing/
                                         # ceiling gate (ADR 0034/0025 as amended by #162) — only a
                                         # true-zero-capacity permit reservation may still defer it
    floodgates: bool = False             # per-record floodgates override (ADR 0025 amendment):
                                         # same effect as the fleet-wide toggle — weekly allowance
                                         # lifted, ceiling raised to 100, pacing cap lifted — but
                                         # scoped to this one record; the permit ledger is untouched
    # Retained so pre-#555 durable session-lead records decode and refresh safely on restart.
    native_helpers_marker: str | None = None


def stalled_for(record: Record, now: int) -> int:
    """How long this record's current human-clearable refusal has been running, in seconds.

    ``0`` when no clock is set — a record nothing is refusing, one whose refusal declared no
    disposition, or one whose clock the coordinator cleared. Every escalation bound above is
    read off this one number, so the daemon line, the published projection, and the park can
    never disagree about how long something has been stuck.
    """
    if not record.stall_refusal_id:
        return 0
    return max(0, now - record.stall_started_at)
