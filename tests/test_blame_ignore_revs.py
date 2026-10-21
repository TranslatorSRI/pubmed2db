"""Every commit `.git-blame-ignore-revs` names is in this checkout's history.

The file names commits by SHA, and a squash or rebase merge (or a rebase of the
branch) gives them new ones. git and GitHub then silently skip nothing, so this
fails instead, naming the commit to look up again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REVS_FILE = REPO_ROOT / ".git-blame-ignore-revs"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)


def _listed_revs() -> list[str]:
    lines = (line.strip() for line in REVS_FILE.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_every_ignored_rev_is_an_ancestor_of_head() -> None:
    if shutil.which("git") is None or _git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("not a git checkout")
    if _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        # CI checks out full history (tests.yml, fetch-depth: 0) so that this
        # runs there; a shallow clone on a dev box is not a failure.
        if os.environ.get("CI"):
            pytest.fail("shallow checkout in CI: tests.yml needs fetch-depth: 0")
        pytest.skip("shallow checkout: the listed commits may be beyond its depth")

    revs = _listed_revs()
    assert revs, "the file lists no commits"
    missing = [
        rev for rev in revs if _git("merge-base", "--is-ancestor", rev, "HEAD").returncode != 0
    ]
    assert not missing, (
        f"not in HEAD's history, so git blame does not skip them: {missing}. "
        "If a squash or rebase merge rewrote them, list the SHAs they landed as."
    )
