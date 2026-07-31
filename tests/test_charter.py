"""The canonical charter every unattended stage receives, read through the runner.

Charter text is load-bearing: a clause that silently disappears changes what builders
and reviewers are grounded in, with nothing else failing.
"""

from agentflow.runner import _canonical_charter


def _unwrapped() -> str:
    """The charter as one flowed string, so assertions survive a rewrap."""
    return " ".join(_canonical_charter().split())


def test_charter_earns_every_guard():
    charter = _unwrapped()

    # The reachability bar, and what makes an invariant count as enforced.
    assert "Earn every guard." in charter
    assert "reachable under the system's **enforced** invariants" in charter
    assert "rejected or made unrepresentable before the guard" in charter
    assert "by code or by a pinned test" in charter

    # The trust-boundary carve-out, explicitly open-ended.
    assert "trust boundary are never speculative" in charter
    assert "an illustrative list, not a closed one" in charter

    # Deletion is legitimate, but only against stated evidence.
    assert "adding one is a defect" in charter
    assert "only when the removal names the enforced invariant" in charter

    # And the keep-side, so the clause can never be read as delete-only.
    assert "Keep every guard that acceptance criteria" in charter
