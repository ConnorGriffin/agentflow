"""Pin that ADR 511 records in-session slicing as shipped, not open work (issue 730)."""

from __future__ import annotations

from pathlib import Path

ADR_511_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "adr"
    / "adr-511-slicing-survives-under-the-session-lead.md"
)


def test_adr_511_no_longer_claims_slicing_is_unimplemented() -> None:
    text = ADR_511_PATH.read_text()
    assert "not implemented today" not in text
    assert "remains open work" not in text
    assert "9fa005d" in text
    assert "#722" in text
