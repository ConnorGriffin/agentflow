"""Content-free promotion scope vocabulary shared by Evidence and its verifier."""
from __future__ import annotations

from dataclasses import dataclass
import re


_VERSION = r"(0|[1-9][0-9]*)"
_NEW_VERSION = r"([1-9][0-9]*)"
_FLEET_SCOPE = re.compile(rf"^fleet-policy/{_VERSION}-to-{_NEW_VERSION}$")
_OVERLAY_SCOPE = re.compile(
    rf"^repository-policy/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/{_VERSION}-to-{_NEW_VERSION}$")


class PromotionAuthorityError(ValueError):
    """A public, content-free authority refusal."""


@dataclass(frozen=True)
class PromotionScope:
    kind: str
    repository: str
    prior: int
    new: int


def parse_promotion_scope(value: str) -> PromotionScope:
    """Parse exactly one version transition without admitting policy content."""
    if match := _FLEET_SCOPE.fullmatch(value):
        prior, new = (int(item) for item in match.groups())
        if new > prior:
            return PromotionScope("fleet", "", prior, new)
    elif match := _OVERLAY_SCOPE.fullmatch(value):
        owner, repo, prior_text, new_text = match.groups()
        if owner in {".", ".."} or repo in {".", ".."}:
            raise PromotionAuthorityError("promotion scope rejected")
        prior, new = int(prior_text), int(new_text)
        if new > prior:
            return PromotionScope("repository", f"{owner}/{repo}", prior, new)
    raise PromotionAuthorityError("promotion scope rejected")
