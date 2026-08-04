"""Benchmarked capability routing behind one resolver interface (#498).

Callers ask :data:`routing` for a route, a stage model, a launchable provider model,
or the rendered session-lead contract. The provenance-stamped artifact is private to
this module: dispatch, admission, prompts, and runners never parse or mirror it.
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
    """One area's escalation ladder. Bans are enforced when the table loads, so a route that
    exists can only name models the area allows."""

    ladder: tuple[str, ...]

    @property
    def model(self) -> str:
        """The rung a worker enters at."""
        return self.ladder[0]


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
        self._routes = self._validate_routes(areas)

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

    def _validate_routes(self, areas) -> dict[tuple[str, str], Route]:
        routes = {}
        for area, facts in areas.items():
            if not isinstance(facts, dict) or not isinstance(facts.get("routes"), dict):
                raise RoutingConfigError(f"area {area!r} has no routes")
            banned = tuple(facts.get("banned") or ())
            for model in banned:
                if model not in self._models:
                    raise RoutingConfigError(f"area {area!r} bans unknown model {model!r}")
            for variant, ladder_value in facts["routes"].items():
                ladder = tuple(ladder_value) if isinstance(ladder_value, list) else ()
                if not ladder:
                    raise RoutingConfigError(f"area {area!r} variant {variant!r} has an empty ladder")
                for model in ladder:
                    if model not in self._models:
                        raise RoutingConfigError(f"area {area!r} names unknown model {model!r}")
                    if model == "fable":
                        raise RoutingConfigError("fable is never a delegate target")
                    if model in banned:
                        raise RoutingConfigError(f"area {area!r} routes banned model {model!r}")
                routes[(area, variant)] = Route(ladder)
            if (area, "default") not in routes:
                raise RoutingConfigError(f"area {area!r} needs a default route")
        return routes

    def route(self, area: str, *, variant: str = "default") -> Route:
        if area not in self._AREAS:
            raise RoutingConfigError(f"unknown routing area {area!r}")
        try:
            return self._routes[(area, variant)]
        except KeyError as exc:
            raise RoutingConfigError(f"unknown {area!r} route variant {variant!r}") from exc

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
                        builder_complexity: str | None = None) -> str:
        """Resolve the accountable parent/reviewer while leaving other stages unchanged."""
        if stage in self._SESSION_LED and pool == self.LEAD_POOL:
            # Public submission mappings choose Claude/Fable. A durable pre-#498 Codex record
            # keeps its pinned attempt lineage so an upgrade cannot strand in-flight work.
            return "fable"
        tier = builder_complexity or complexity
        if stage == "review":
            if tier == "standard":
                return "luna" if pool == "codex" else "sonnet"
            if tier == "deep":
                return "sol" if pool == "codex" else "opus"
        legacy = {
            ("claude", "standard"): "sonnet", ("claude", "deep"): "opus",
            ("codex", "standard"): "terra", ("codex", "deep"): "sol",
        }
        try:
            return legacy[(pool, complexity)]
        except KeyError as exc:
            raise RoutingConfigError(
                f"no model for stage={stage!r}, pool={pool!r}, complexity={complexity!r}") from exc

    def review_complexity(self, builder_complexity: str | None,
                          fallback: str | None) -> str:
        tier = builder_complexity or fallback or "deep"
        if tier not in {"standard", "deep"}:
            raise RoutingConfigError(f"unknown review complexity {tier!r}")
        return tier

    def worker_reasoning(self, effort: str | None) -> str:
        return self._reasoning.get(effort or "medium", "medium")

    def session_lead_instructions(self, stage: str, effort: str | None) -> str:
        if stage not in {"build", "revise"}:
            raise RoutingConfigError(f"stage {stage!r} has no session lead")
        arrows = {
            area: " → ".join(self.route(area).ladder)
            for area in ("exploration", "implementation", "plan", "prototype",
                         "brainstorm", "documentation", "review")
        }
        arrows = {area: value.title() for area, value in arrows.items()}
        launch_ids = "; ".join(
            f"{name.title()} ({facts['provider']}): {facts['cli_id']}"
            for name, facts in self._models.items()
        )
        rung = self.worker_reasoning(effort)
        return f"""

## Session lead — benchmarked capability routing

You are the accountable Session lead. Do not write the implementation directly. Plan the work,
delegate exploration, implementation, and fix work, verify every result, and ship only verified
work. Fable is lead-only and is never a delegate target.

worker reasoning rung: {rung}. Give that rung in every worker prompt. Claude workers use native
sub-agents. Reach Codex workers by shelling out to `codex exec` with the CLI id named below; do not
consult pool headroom from inside this running session.

Routes (workers enter at the first rung):
- exploration: bounded Luna / full-system Sonnet; ladder {arrows['exploration']}; Haiku banned
- hermetic implementation: {arrows['implementation']}
- plan/spec: {arrows['plan']}
- prototyping/UI mockups: {arrows['prototype']}; Luna banned
- brainstorming: {arrows['brainstorm']}
- documentation: {arrows['documentation']}
- code review: routine {arrows['review']}; load-bearing Opus only; Haiku never reviews

Provider launch identifiers: {launch_ids}

For each delegated result, inspect the diff and citations, then run the repository test gate.
Never ship after a failed test gate. On the first failed verification, re-engage the same worker
with the findings when continuation exists, otherwise re-delegate at the same rung with the
findings. On the second failure, start a fresh worker one rung higher. At the ladder top, stop and
surface both failed attempts in the final handoff; do not claim success. Verify, do not trust.
"""


routing = CapabilityRouting.from_path(Path(__file__).with_name("model-routing.json"))
