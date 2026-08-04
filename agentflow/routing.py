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

    LEAD_POOL = "claude"     # the only pool that can launch the session lead (ADR 498)

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
            raise RoutingConfigError("fable must be the session-lead model")
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
        if stage in self._SESSION_LED and pool == self.LEAD_POOL:
            return "fable"
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
                                   codex_spent: bool = False) -> str:
        """Render the session-lead brief. ``codex_spent`` is the caller's render-time capacity
        fact (see :func:`agentflow.runner.codex_spent_at_render`) — routing has no seam of its
        own onto Codex account state, so a caller that knows it passes it in; a caller that
        doesn't (or the fact was unreadable) gets the ordinary brief."""
        if stage not in {"build", "revise"}:
            raise RoutingConfigError(f"stage {stage!r} has no session lead")
        table = "\n".join(self._area_line(area) for area in self._areas.values())
        launch_ids = "; ".join(
            f"{name.title()} ({facts['provider']}): {facts['cli_id']}"
            for name, facts in self._models.items()
        )
        rung = self.worker_reasoning(effort)
        preamble = ""
        if codex_spent:
            preamble = (
                "\nCodex is currently unavailable (spent): every Codex rung is closed for this "
                "session — enter each ladder at its first Claude rung instead.\n"
            )
            codex_only = [area.title for area in self._areas.values()
                         if not self._has_claude_rung(area)]
            if codex_only:
                names = ", ".join(codex_only)
                preamble += (
                    f"Exception: {names} has no Claude rung to fall back to — with Codex closed, "
                    "do that area's work yourself rather than delegating off-table.\n"
                )
        return f"""
{preamble}
## Session lead — benchmarked capability routing

You are the accountable Session lead. Do not write the implementation directly. Plan the work,
delegate exploration, implementation, and fix work, verify every result, and ship only verified
work. Fable is lead-only and is never a delegate target.

worker reasoning rung: {rung}. Give that rung in every worker prompt. Claude workers use native
sub-agents. Reach Codex workers by shelling out to `codex exec` with the CLI id named below; do not
consult pool headroom from inside this running session.

Routes (workers enter at the first rung; a banned model never takes that area's work):
{table}

Provider launch identifiers: {launch_ids}

For each delegated result, inspect the diff and citations, then run the repository test gate.
Never ship after a failed test gate. On the first failed verification, re-engage the same worker
with the findings when continuation exists, otherwise re-delegate at the same rung with the
findings. On the second failure, start a fresh worker one rung higher. At the ladder top, stop and
surface both failed attempts in the final handoff; do not claim success. Verify, do not trust.

If a `codex exec` worker fails to launch or dies on a provider error (rate limit, quota
exhausted, API unreachable) rather than on the work itself, treat every Codex rung in that
ladder as unavailable for the rest of this session: re-enter at the first Claude rung of the
same ladder instead of retrying Codex, and record the substitution in the final handoff. This is
separate from a failed verification — a provider failure is never a finding to re-delegate
against.
"""



routing = CapabilityRouting.from_path(Path(__file__).with_name("model-routing.json"))
