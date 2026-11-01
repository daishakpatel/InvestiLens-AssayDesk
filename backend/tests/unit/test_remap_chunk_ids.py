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
# Chunks overlap: p6 ends with the 40 words p7 starts with (longer than any short window).
_SHARED = " ".join(f"word{i}" for i in range(40))
_OVERLAP = [
    ("9_8_p6", f"Earlier text about something else entirely. {_SHARED}"),
    ("9_8_p7", f"{_SHARED} We expect purchase obligations to grow as capacity comes online."),
]


def test_a_stored_quote_finds_its_new_chunk() -> None:  # CIT-002
    old_text = "These commitments were $\n\n95.2\n\nbillion as of January 25, 2026."
    assert match_new_id("commitments were $95.2 billion", "9_7_3", old_text, _NEW) == "9_7_p1"


def test_without_a_quote_the_old_chunk_words_decide() -> None:
    old_text = "Revenue rose 12% on data center demand."
    assert match_new_id(None, "9_7_2", old_text, _NEW) == "9_7_p0"


def test_an_unchanged_chunk_keeps_its_id() -> None:
    assert match_new_id(None, "9_7_p1", _NEW[1][1], _NEW) == "9_7_p1"


def test_the_overlapping_neighbour_is_not_chosen() -> None:
    old_text = _OVERLAP[1][1]  # p7's own text, cited under an older ID
    assert match_new_id(None, "9_8_7", old_text, _OVERLAP) == "9_8_p7"


def test_no_match_keeps_the_reference_failing_loudly() -> None:  # ADR-0041
    old = "Nothing like this anywhere at all in any chunk."
    assert match_new_id("a sentence no chunk holds", "9_7_9", old, _NEW) is None
