"""Every pointer from one file to a section of a doc resolves.

The docs, the code and the Slurm scripts send readers to sections by two
routes: a Markdown link with an anchor (``[…](../docs/Memory.md#…)``), and a
prose pointer naming a doc and a section (``docs/Memory.md, "A refused
allocation"``), which is what a log line, a comment or a docstring can carry.
Renaming or moving a section breaks both silently, and the pointers that
matter most (the out-of-memory log line's) are read at the moment the reader
cannot afford a dead end. These tests resolve all of them against the files
and their headings, so a rename fails here instead.

``test_data_model.py`` checks the data-model docs' absolute links to ``main``;
this file checks everything relative.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.doc_links import REPO_ROOT, anchors, headings, resolve

#: Directories whose files are not the repository's own docs or code.
_SKIP_DIRS = {".git", ".venv", "data", "node_modules", ".pytest_cache", ".ruff_cache"}

#: An inline link's target, or a reference-style definition's.
_LINK = re.compile(r"\]\(([^)\s]+)\)|^\s*\[[^\]]+\]:\s*(\S+)", re.M)
#: ``<path>.md`` (optionally in backticks), then ``,``, ``->`` or ``→``, then a
#: quoted section name. The section matches a heading it is a prefix of, so
#: ``"A refused allocation"`` names ``## A refused allocation: `(bad allocation)` ``.
_POINTER = re.compile(r"""([\w./-]+\.md)`?\s*(?:,|->|→)\s*(["'])([\w`].*?)\2""")
#: A doc named by its repository path in code, with or without a section.
_DOC_PATH = re.compile(r"(?<![\w/.])((?:docs|slurm)/[\w./-]+\.md)")


def _files(*patterns: str) -> list[Path]:
    found = set()
    for pattern in patterns:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file() and not _SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
                found.add(path)
    return sorted(found)


def test_relative_markdown_links_resolve():
    broken = []
    for source in _files("*.md", "**/*.md"):
        for inline, reference in _LINK.findall(source.read_text()):
            target = inline or reference
            if re.match(r"[a-z][a-z0-9+.-]*:", target):  # http:, mailto:, …
                continue
            path, _, anchor = target.partition("#")
            resolved = (source.parent / path).resolve() if path else source
            where = f"{source.relative_to(REPO_ROOT)}: {target}"
            if not resolved.exists():
                broken.append(f"{where} (no such file)")
            elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
                broken.append(f"{where} (no such heading)")
    assert not broken, "\n".join(broken)


def _pointers(source: Path) -> set[tuple[str, str]]:
    """The ``(doc, section)`` pairs a file names in ``_POINTER``'s form.

    Comments and docstrings wrap, so a pointer can break across lines: the
    text is read with comment markers and line breaks folded to spaces. A
    Python pointer can also be split across adjacent string literals
    (``"…(docs/data-model/README.md, " '"Versioning…"'``), which no folding of
    the source rejoins, so a Python file's string constants are read too, as
    the parser joins them."""
    text = source.read_text()
    texts = [text if source.suffix == ".md" else re.sub(r"\n\s*(?:#:?|//)?\s*", " ", text)]
    if source.suffix == ".py":
        texts += [
            node.value
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
    return {
        (name, section)
        for text in texts
        for name, _, section in _POINTER.findall(re.sub(r"\s+", " ", text))
    }


def test_named_sections_exist():
    """``<doc>.md, "<section>"`` in code, scripts, tests and docs names a real heading."""
    sources = _files("*.md", "**/*.md", "src/**/*.py", "tests/*.py", "slurm/*", "scripts/*")
    broken, checked = [], 0
    for source in sources:
        for name, section in sorted(_pointers(source)):
            target = resolve(name, source)
            where = f'{source.relative_to(REPO_ROOT)}: {name}, "{section}"'
            if target is None:
                broken.append(f"{where} (no such file)")
            elif not any(h.startswith(section) for h in headings(target)):
                broken.append(f"{where} (no such heading)")
            checked += 1
    assert not broken, "\n".join(broken)
    # A pattern that stopped matching would pass here while checking nothing.
    assert checked > 5


def test_docs_named_in_code_exist():
    """A comment that says ``docs/Memory.md asks …`` names no section, but
    the file still has to be there."""
    missing = sorted(
        f"{source.relative_to(REPO_ROOT)}: {name}"
        for source in _files("src/**/*.py", "tests/*.py", "slurm/*", "scripts/*")
        for name in _DOC_PATH.findall(source.read_text())
        if not (REPO_ROOT / name).is_file()
    )
    assert not missing, "\n".join(missing)


def test_the_out_of_memory_pointer_is_one_of_those_checked():
    """The log line an operator reads on a refused allocation names a section
    in ``_POINTER``'s form, so ``test_named_sections_exist`` covers it."""
    pointers = _pointers(REPO_ROOT / "src/pubmed2db/export.py")
    assert ("docs/Memory.md", "A refused allocation") in pointers


def test_a_pointer_split_across_string_literals_is_checked():
    """``test_the_patch_level_is_unused``'s message splits its pointer across
    two literals, with the section's quotes in the second; reading the source
    alone skipped it."""
    pointers = _pointers(REPO_ROOT / "tests/test_data_model.py")
    assert ("docs/data-model/README.md", "Versioning and stability") in pointers
