"""Benchmarked capability routing behind one resolver interface (#498).

Callers ask :data:`routing` for a stage model, a launchable provider model, or the rendered
session-lead contract — the routes, their bans, and the ladders reach a session only through
that contract. The provenance-stamped artifact is private to this module: dispatch, admission,
prompts, and runners never parse or mirror it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class RoutingConfigError(ValueError):
    """The shipped capability table cannot produce a safe launch decision."""


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    benchmark_date: str
    price_snapshot: str
    alias_note: str = ""


@dataclass(frozen=True, slots=True)
class Route:
    """One entry into an area's escalation ladder. ``when`` is the condition the lead is told
    to pick it under, empty for an area with a single route. Bans are enforced when the table
    loads, so a route that exists can only name models the area allows."""

    when: str
    ladder: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Area:
    """One area of delegated work: how the lead names it, its routes, and its bans."""

    title: str
    routes: tuple[Route, ...]
    banned: tuple[str, ...]


class CapabilityRouting:
    """One deep interface over validated routes, launch ids, and lead instructions."""

    _LEAD_MODELS = {"claude": "fable", "codex": "sol"}

    _AREAS = frozenset({
        "exploration", "implementation", "plan", "prototype", "brainstorm",
        "documentation", "review",
    })
    _SESSION_LED = frozenset({"build", "revise"})

    def __init__(self, data: dict):
        try:
            provenance = data["provenance"]
            models = data["models"]
            areas = data["areas"]
            reasoning = data["worker_reasoning"]
            self.provenance = Provenance(**provenance)
        except (KeyError, TypeError) as exc:
            raise RoutingConfigError(f"incomplete routing config: {exc}") from exc
        if set(areas) != self._AREAS:
            unknown = sorted(set(areas) - self._AREAS)
            missing = sorted(self._AREAS - set(areas))
            raise RoutingConfigError(f"routing areas mismatch; unknown={unknown}, missing={missing}")
        self._models = self._validate_models(models)
        self._reasoning = self._validate_reasoning(reasoning)
        self._areas = self._validate_areas(areas)
        self._rate_card = self._validate_rate_card(data.get("rate_card"), self._models)

    @classmethod
    def from_path(cls, path: Path | str) -> "CapabilityRouting":
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise RoutingConfigError(f"cannot read routing config: {exc}") from exc
        if not isinstance(data, dict):
            raise RoutingConfigError("routing config must be an object")
        return cls(data)

    @staticmethod
    def _validate_models(models) -> dict[str, dict[str, str]]:
        if not isinstance(models, dict):
            raise RoutingConfigError("models must be an object")
        checked = {}
        for name, facts in models.items():
            if not isinstance(name, str) or not isinstance(facts, dict):
                raise RoutingConfigError("every model needs named provider facts")
            provider, cli_id = facts.get("provider"), facts.get("cli_id")
            if provider not in {"claude", "codex"} or not isinstance(cli_id, str) or not cli_id:
                raise RoutingConfigError(f"model {name!r} has no launchable provider CLI identifier")
            checked[name] = {"provider": provider, "cli_id": cli_id,
                             "role": str(facts.get("role") or "worker")}
        if checked.get("fable", {}).get("role") != "session-lead":
            raise RoutingConfigError("fable must be the Claude session-lead model")
        if checked.get("sol", {}).get("role") != "session-lead":
            raise RoutingConfigError("sol must be the Codex session-lead model")
        return checked

    @staticmethod
    def _validate_rate_card(rate_card, models: dict) -> dict[str, tuple[float, float]]:
        """Per-million-token USD rates, keyed by internal model name. Absent entirely on a
        table with no priced models; a model priced here must already exist in ``models``, and
        a malformed rate is a load-time config error rather than a silent zero. No cached-read
        rate is ever carried — cached reads are excluded from every estimate that reads this."""
        if rate_card is None:
            return {}
        if not isinstance(rate_card, dict):
            raise RoutingConfigError("rate_card must be an object")
        checked: dict[str, tuple[float, float]] = {}
        for name, rates in rate_card.items():
            if name not in models:
                raise RoutingConfigError(f"rate_card prices unknown model {name!r}")
            if not isinstance(rates, dict):
                raise RoutingConfigError(f"rate_card entry {name!r} must be an object")
            try:
                input_rate = float(rates["input"])
                output_rate = float(rates["output"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RoutingConfigError(f"rate_card entry {name!r} has a malformed rate") from exc
            if isinstance(rates.get("input"), bool) or isinstance(rates.get("output"), bool):
                raise RoutingConfigError(f"rate_card entry {name!r} has a malformed rate")
            if input_rate < 0 or output_rate < 0:
                raise RoutingConfigError(f"rate_card entry {name!r} has a negative rate")
            checked[name] = (input_rate, output_rate)
        return checked

    @staticmethod
    def _validate_reasoning(reasoning) -> dict[str, str]:
        expected = {"low", "medium", "high", "extra"}
        if not isinstance(reasoning, dict) or set(reasoning) != expected \
                or not all(isinstance(value, str) and value for value in reasoning.values()):
            raise RoutingConfigError("worker reasoning must name all four effort rungs")
        return dict(reasoning)

    def _validate_areas(self, areas) -> dict[str, Area]:
        checked = {}
        for area, facts in areas.items():
            if not isinstance(facts, dict) or not isinstance(facts.get("routes"), list) \
                    or not facts["routes"] or not facts.get("title"):
                raise RoutingConfigError(f"area {area!r} needs a title and at least one route")
            banned = tuple(facts.get("banned") or ())
            for model in banned:
                if model not in self._models:
                    raise RoutingConfigError(f"area {area!r} bans unknown model {model!r}")
            routes = []
            for entry in facts["routes"]:
                ladder = tuple(entry.get("ladder") or ()) if isinstance(entry, dict) else ()
                when = str(entry.get("when") or "") if isinstance(entry, dict) else ""
                if not ladder:
                    raise RoutingConfigError(f"area {area!r} route {when!r} has an empty ladder")
                for model in ladder:
                    if model not in self._models:
                        raise RoutingConfigError(f"area {area!r} names unknown model {model!r}")
                    if model == "fable":
                        raise RoutingConfigError("fable is never a delegate target")
                    if model in banned:
                        raise RoutingConfigError(f"area {area!r} routes banned model {model!r}")
                routes.append(Route(when, ladder))
            checked[area] = Area(str(facts["title"]), tuple(routes), banned)
        return checked

    def _resolve_model_name(self, model: str) -> str | None:
        """The internal model name for ``model``, whether it already is one or is a
        provider/CLI id (e.g. ``gpt-5.6-terra`` → ``terra``). ``None`` for an unknown model —
        callers never guess past this."""
        if model in self._models:
            return model
        for name, facts in self._models.items():
            if facts["cli_id"] == model:
                return name
        return None

    def provider_for(self, model: str) -> str | None:
        """The provider (``claude``/``codex``) that launches ``model``, resolving internal
        names and CLI ids alike; ``None`` for a model this table does not know."""
        name = self._resolve_model_name(model)
        return self._models[name]["provider"] if name is not None else None

    def estimate_cost_usd(self, model: str, *, input_tokens: int | None = None,
                          output_tokens: int | None = None,
                          reasoning_output_tokens: int | None = None) -> float | None:
        """A dollar estimate for one model's fresh input plus output tokens, priced from the rate
        card (per-million-token USD). Cached reads are never priced — they are not even accepted
        here. ``reasoning_output_tokens`` is priced at the output rate only as a *fallback* when
        ``output_tokens`` is absent, never added on top of it: Codex's usage shape reports
        reasoning tokens as a subset of the blended output total (codex-rs's ``TokenUsage`` sums
        non-cached input and output for the blended total; reasoning is informational detail
        inside that output figure, not additional to it), so summing both here would double-count
        it. Resolves both internal model names and provider/CLI ids; returns ``None`` for an
        unknown or unpriced model, or when no token fact was given at all — never a guessed
        figure."""
        if input_tokens is None and output_tokens is None and reasoning_output_tokens is None:
            return None
        name = self._resolve_model_name(model)
        if name is None:
            return None
        rates = self._rate_card.get(name)
        if rates is None:
            return None
        input_rate, output_rate = rates
        fresh_in = input_tokens or 0
        out = output_tokens if output_tokens is not None else (reasoning_output_tokens or 0)
        return (fresh_in / 1_000_000) * input_rate + (out / 1_000_000) * output_rate

    def cli_identifier(self, provider: str, model: str) -> str:
        """Resolve an internal model name (or already-resolved id) for one provider."""
        for facts in self._models.values():
            if facts["provider"] == provider and facts["cli_id"] == model:
                return model
        facts = self._models.get(model)
        if facts is None or facts["provider"] != provider:
            raise RoutingConfigError(f"provider {provider!r} cannot launch model {model!r}")
        return facts["cli_id"]

    def model_for_stage(self, stage: str, pool: str, complexity: str,
                        builder_complexity: str | None = None) -> str | None:
        """The accountable parent or reviewer capability routing chooses, or ``None`` when it
        has no opinion and the caller's established per-pool model table still applies.

        A session-led stage on the lead pool is the fixed parent. A durable pre-#498 record
        pinned to the other pool has no routed parent, so it keeps its own lineage's model and
        an upgrade cannot strand in-flight work."""
        if stage in self._SESSION_LED:
            return self._LEAD_MODELS.get(pool)
        if stage == "review":
            tier = builder_complexity or complexity
            if tier == "standard":
                return "luna" if pool == "codex" else "sonnet"
            if tier == "deep":
                return "sol" if pool == "codex" else "opus"
        return None

    def review_complexity(self, builder_complexity: str | None,
                          fallback: str | None) -> str:
        tier = builder_complexity or fallback or "deep"
        if tier not in {"standard", "deep"}:
            raise RoutingConfigError(f"unknown review complexity {tier!r}")
        return tier

    def worker_reasoning(self, effort: str | None) -> str:
        return self._reasoning.get(effort or "medium", "medium")

    def _codex_worker_models(self) -> frozenset[str]:
        """Every Codex model reachable as a worker rung in any area's ladder, including Sol:
        ``role: session-lead`` on ``sol`` is parent eligibility only (:data:`_LEAD_MODELS`), and
        is separate from whether an area's ladder also routes delegated work to it — the plan/
        spec, prototype, and documentation ladders all do. Deriving from the ladders themselves
        (rather than the ``role`` tag) is what keeps this in sync with the routing table instead
        of needing a second, hand-maintained worker/lead split."""
        return frozenset(
            model for area in self._areas.values() for route in area.routes
            for model in route.ladder if self._models[model]["provider"] == "codex")

    def codex_worker_cli_identifier(self, worker: str) -> str:
        """Resolve one routed Codex worker to its pinned CLI model id.

        This is intentionally narrower than :meth:`cli_identifier`: the bounded worker launcher
        accepts only models a ladder actually delegates to, rather than arbitrary Codex models.
        """
        name = self._resolve_model_name(worker)
        if name not in self._codex_worker_models():
            raise RoutingConfigError(f"{worker!r} is not a routed Codex worker")
        return self._models[name]["cli_id"]

    @staticmethod
    def _area_line(area: Area) -> str:
        """One rendered table row: every route the area allows, then the models it bans."""
        routes = "; ".join(
            f"{route.when} {' → '.join(model.title() for model in route.ladder)}".strip()
            for route in area.routes)
        bans = "; never " + ", ".join(model.title() for model in area.banned) if area.banned else ""
        return f"- {area.title}: {routes}{bans}"

    def _has_claude_rung(self, area: Area) -> bool:
        """Whether any route in the area names a Claude model, so a Codex-closed ladder without
        one (plan/spec today) can be told apart from an ordinary ladder with a Claude fallback."""
        return any(self._models[model]["provider"] == "claude"
                   for route in area.routes for model in route.ladder)

    def session_lead_instructions(self, stage: str, effort: str | None, *,
                                   parent_provider: str = "claude",
                                   codex_spent: bool = False,
                                   unavailable_providers: frozenset[str] = frozenset()) -> str:
        """Render the session-lead brief. ``codex_spent`` is the caller's render-time capacity
        fact (see :func:`agentflow.runner.codex_spent_at_render`) — routing has no seam of its
        own onto Codex account state, so a caller that knows it passes it in; a caller that
        doesn't (or the fact was unreadable) gets the ordinary brief. ``unavailable_providers``
        is the caller's durable pool-pause snapshot, distinct from the render-time Codex capacity
        fact."""
        if stage not in {"build", "revise"}:
            raise RoutingConfigError(f"stage {stage!r} has no session lead")
        if parent_provider not in self._LEAD_MODELS:
            raise RoutingConfigError(f"provider {parent_provider!r} has no session lead")
        table = "\n".join(self._area_line(area) for area in self._areas.values())
        launch_ids = "; ".join(
            f"{name.title()} ({facts['provider']}): {facts['cli_id']}"
            for name, facts in self._models.items()
        )
        rung = self.worker_reasoning(effort)
        effort_label = effort if effort in self._reasoning else "medium"
        preamble = ""
        if unavailable_providers:
            for provider in ("claude", "codex"):
                if provider not in unavailable_providers:
                    continue
                title = provider.title()
                preamble += (
                    f"\n{title} is currently unavailable (pool paused): skip every {title} "
                    "rung in every ladder for this entire session. Where that leaves a "
                    "provider-only ladder with no remaining provider rung, do not delegate into "
                    "it, do not invent a substitute model, and do not do the work yourself — "
                    "hand back the provider failure by name in the final handoff.\n"
                )
        if codex_spent:
            preamble += (
                "\nCodex is currently unavailable (spent): every Codex rung is closed for this "
                "session — enter each ladder at its first Claude rung instead.\n"
            )
            codex_only = [area.title for area in self._areas.values()
                         if not self._has_claude_rung(area)]
            if codex_only:
                names = ", ".join(codex_only)
                preamble += (
                    f"Exception: {names} has no Claude rung to fall back to — with Codex closed, "
                    "that area has no remaining provider from the very start of this session. "
                    "Apply the same provider-failure handback rule below immediately: do not "
                    "delegate into it, do not invent a substitute model, and do not do the work "
                    "yourself — hand back the provider failure by name in the final handoff.\n"
                )
        opposite_provider = ("codex" if parent_provider == "claude" else "claude")
        opposite_cli = "`codex exec`" if opposite_provider == "codex" else "the installed `claude` CLI"
        codex_instruction = (
            "Codex workers use the bounded AgentFlow command. For a Codex rung, create a fresh "
            "file with `prompt_file=$(mktemp)` then run `trap 'rm -f \"$prompt_file\"' EXIT`; run "
            "`chmod 600 \"$prompt_file\"`; write the task into that file without placing task "
            "text in a shell command. Run `agentflow-codex-worker --worker <routed-name> --effort " + effort_label +
            " --timeout 900 < \"$prompt_file\"`. On its first attempt, request "
            "`sandbox_permissions=require_escalated` for exactly `agentflow-codex-worker --worker "
            "<routed-name> --effort " + effort_label + " --timeout 900 < \"$prompt_file\"`; capture its stdout/stderr and "
            "exit status. This AgentFlow-owned "
            "command reads the file without shell interpolation and enforces the routed CLI model, "
            "reasoning effort, wall timeout, and process-group termination. Read its stdout/stderr "
            "and non-zero result as the worker outcome. If a yielded agentflow-codex-worker command "
            "returns a running handle, poll that exact handle until terminal and never relaunch it "
            "while active; use its eventual result. Never use `spawn_agent`, `agent_type`, or hidden "
            "role fields."
        )
        return f"""
{preamble}
## Session lead — benchmarked capability routing

You are the accountable Session lead. Do not write the implementation directly. Plan the work,
delegate exploration, implementation, and fix work, verify every result, and ship only verified
work. Fable is lead-only and is never a delegate target.

worker reasoning rung: {rung}. {codex_instruction} Reach {opposite_provider.title()} workers through
{opposite_cli} with the routed CLI id named below; do not consult pool headroom from inside this running session.

Routes (workers enter at the first rung; a banned model never takes that area's work):
{table}

Provider launch identifiers: {launch_ids}

For each delegated result, inspect the diff and citations, then run the repository test gate.
Never ship after a failed test gate. On the first failed verification, re-engage the same worker
with the findings when continuation exists, otherwise re-delegate at the same rung with the
findings. On the second failure, start a fresh worker one rung higher. At the ladder top, stop and
surface both failed attempts in the final handoff; do not claim success. Verify, do not trust.

If a worker from either provider fails to launch or dies on a provider error (rate limit, quota
exhausted, API unreachable) rather than on the work itself, treat every rung from that provider in
that ladder as unavailable for the rest of this session: re-enter at the first remaining-provider
rung of the same ladder instead of retrying the failed provider; record the substitution in the final handoff.
If that specific ladder has no rung from any other provider (a single-provider ladder — check the
routes above; some areas mix providers only in one of their routes), do not invent a substitute
model and do not silently do the work yourself: stop delegating in that area and hand back the
provider failure by name in the final handoff instead of a result. This is separate from a failed
verification — it is never a finding to re-delegate against.
"""



routing = CapabilityRouting.from_path(Path(__file__).with_name("model-routing.json"))
