"""Markdown removal for model prose that is verified sentence by sentence (chat, commentary).

The verifier splits text into sentences and checks each against its citations. Headings and bullet
points carry no sentence punctuation, so a Markdown answer reads as one long "sentence": a claim cut
off its citation, or a whole answer checked as a single claim (found 2026-11-22, ADR-0071).
"""

from __future__ import annotations

import re

_MD_RULE = re.compile(r"^[ \t]*([-*_])\1{2,}[ \t]*$\n?", re.M)
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t].*$\n?", re.M)
_MD_BULLET = re.compile(r"^[ \t]*(?:[*\-\u2022]|\d{1,2}[.)])[ \t]+", re.M)


def plain_text(answer: str) -> str:
    """The model's answer with Markdown removed, keeping its sentences and citation markers."""
    text = _MD_HEADING.sub("", _MD_RULE.sub("", answer))
    text = _MD_BULLET.sub("", text).replace("**", "").replace("__", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
