"""Reading the repository's Markdown docs the way GitHub renders them.

Shared by the tests that check links into the docs: ``test_doc_links.py``
(relative links and prose pointers) and ``test_data_model.py`` (the data-model
docs' absolute links to ``main``). Not a ``test_`` module, so pytest does not
collect it; it lives under ``tests/`` rather than ``src/`` because nothing in
the pipeline reads its own docs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^#{1,6} (.+?)\s*#*\s*$")


def headings(path: Path) -> list[str]:
    """The file's heading texts, as written, skipping fenced code blocks."""
    found, fenced = [], False
    for line in path.read_text().splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and (match := _HEADING.match(line)):
            found.append(match.group(1))
    return found


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: link text kept, then lower-cased, with
    punctuation other than ``-`` and ``_`` dropped and spaces made ``-``."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Every anchor GitHub gives the file's headings; a repeated heading's
    second anchor takes ``-1``, its third ``-2``, and so on."""
    found, seen = set(), {}
    for heading in headings(path):
        base = slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def resolve(name: str, source: Path) -> Path | None:
    """A repository-relative path, or failing that one relative to its source."""
    for base in (REPO_ROOT, source.parent):
        if (base / name).is_file():
            return (base / name).resolve()
    return None
