"""The one-off v5 chunk-ID remap matches each stored reference to its new chunk (ADR-0052)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from remap_chunk_ids import match_new_id  # type: ignore[import-not-found]

_NEW = [
    ("9_7_p0", "Revenue rose 12% on data center demand."),
    ("9_7_p1", "These commitments were $95.2 billion as of January 25, 2026."),
]


def test_a_stored_quote_finds_its_new_chunk() -> None:  # CIT-002
    old_text = "These commitments were $\n\n95.2\n\nbillion as of January 25, 2026."
    assert match_new_id("commitments were $95.2 billion", old_text, _NEW) == "9_7_p1"


def test_without_a_quote_the_old_chunk_words_decide() -> None:
    old_text = "Revenue rose 12% on data center demand."
    assert match_new_id(None, old_text, _NEW) == "9_7_p0"


def test_no_match_keeps_the_reference_failing_loudly() -> None:  # ADR-0041
    assert (
        match_new_id("a sentence no chunk holds", "Nothing like this anywhere at all.", _NEW)
        is None
    )
