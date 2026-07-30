"""The locked operator mockup is a public, browser-read visual specification."""

from pathlib import Path


SPEC = Path(__file__).parent.parent / "mockups" / "operator-surface-finalist.html"


def test_operator_surface_spec_binds_only_to_its_capture_and_declares_the_locked_path():
    source = SPEC.read_text()

    assert source.startswith("<!--\nLOCKED visual contract — issue #183\n")
    assert "fetch('./operator-surface.capture.json')" in source
    assert "inline fallback" not in source
    assert "Attention" in source and "Decision Maps" in source and "Fleet health" in source
    assert "<details>" in source and "Open in GitHub" in source
