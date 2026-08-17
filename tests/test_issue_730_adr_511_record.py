"""Pin that ADR 511 records in-session slicing as shipped, not open work (issue 730)."""

from __future__ import annotations

from pathlib import Path

ADR_511_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "adr"
    / "adr-511-slicing-survives-under-the-session-lead.md"
)
ADR_INDEX_PATH = ADR_511_PATH.with_name("README.md")


def adr_511_index_entry() -> str:
    """Return ADR 511's entry from the documentation index."""
    index = ADR_INDEX_PATH.read_text()
    start = index.index("- [ADR 511]")
    end = index.index("\n- [ADR ", start + 1)
    return index[start:end]


def test_adr_511_records_in_session_slicing_as_shipped() -> None:
    text = ADR_511_PATH.read_text()
    assert (
        "In-session slicing, the commit-per-slice ledger, and the slice-bearing work-order "
        "mechanism\n  shipped in commit `9fa005d` (PR [#722]"
    ) in text


def test_adr_511_index_entry_records_the_shipped_citation() -> None:
    entry = adr_511_index_entry()
    assert "not implemented today" not in entry
    assert "remains open work" not in entry
    assert "in-session slicing shipped in `9fa005d`\n  ([#722]" in entry
