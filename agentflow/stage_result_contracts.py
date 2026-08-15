"""Provider-neutral structured-result contracts shared by routing and provider launch."""

from __future__ import annotations


# Optional dials are nullable so a hold route omits them while still satisfying providers that
# require every property in a strict schema.
INTAKE_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "route": {"type": "string", "enum": ["ready", "grill", "mockup", "nothing-new"]},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "complexity": {"type": ["string", "null"], "enum": ["standard", "deep", None]},
        "effort": {"type": ["string", "null"],
                   "enum": ["low", "medium", "high", "extra", None]},
        "mockup_scope": {"type": ["string", "null"],
                         "enum": ["local", "surface", None]},
    },
    "required": ["route", "title", "body", "complexity", "effort", "mockup_scope"],
}


ATTACK_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "objections": {"type": "string"},
        "remedied": {"type": "boolean"},
        "fork": {"type": "string"},
    },
    "required": ["objections", "remedied", "fork"],
}


REVIEW_VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "BLOCK"]},
        "reviewed_sha": {"type": "string"},
        "final_sha": {"type": "string"},
        "pushed_sha": {"type": "string"},
        "fixes": {"type": "array", "items": {"type": "string"}},
        "depth": {"type": "string", "enum": ["focused", "targeted", "full"]},
        "depth_reason": {"type": "string"},
        "axis": {"type": "string", "enum": [
            "combined", "product", "standards", "fix", "decision"]},
        "change_author_tool": {"type": "string", "enum": ["claude", "codex"]},
        "checks": {"type": "array", "items": {"type": "string"}},
        "decision": {"type": "string"},
        "follow_ups": {
            "type": "array", "maxItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "evidence": {"type": "string"},
                    "desired_outcome": {"type": "string"},
                },
                "required": ["evidence", "desired_outcome"],
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "enum": [
                        "fix_before_completion", "necessary_follow_up", "ask_maintainer",
                        "discard_preference"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
                    "grounding": {"type": "string"},
                    "failure_class": {"type": "string", "enum": [
                        "fix_introduced_defect", "original_defect", "plan_gap",
                        "reviewer_false_claim", "slice_scope_error", "speculative_preference"]},
                },
                "required": ["action", "file", "line", "summary", "grounding", "failure_class"],
            },
        },
        "uncertainty": {
            "anyOf": [
                {"type": "null"},
                {"type": "object", "additionalProperties": False,
                 "properties": {
                     "options": {"type": "array", "minItems": 2, "maxItems": 2,
                                 "items": {"type": "string"}},
                     "missing_guidance": {"type": "string"},
                     "recommendation": {"type": "string"},
                 },
                 "required": ["options", "missing_guidance", "recommendation"]},
            ],
        },
    },
    "required": ["verdict", "depth", "depth_reason", "axis", "change_author_tool",
                 "reviewed_sha", "final_sha", "pushed_sha", "fixes", "follow_ups",
                 "checks", "decision", "findings", "uncertainty"],
}


_SCHEMAS = {
    "intake": INTAKE_RESULT_SCHEMA,
    "review": REVIEW_VERDICT_SCHEMA,
    "attack": ATTACK_RESULT_SCHEMA,
}


def stage_result_schema(stage: str) -> dict | None:
    """Return the structured-result contract for ``stage``, if that stage has one."""
    return _SCHEMAS.get(stage)
