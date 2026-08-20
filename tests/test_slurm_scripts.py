"""Checks on the Slurm helper scripts in ``slurm/``.

These are shell, so the Python suite cannot import them — but the failure modes
found while writing them were all cheap to catch and expensive to hit for real
(a chain that dies hours in on a missing email, or a ``set -e`` footgun that
aborts a job before it starts). Each test here pins one of those.

Nothing runs ``sbatch``: ``submit.sh --dry-run`` prints what it would submit.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SLURM_DIR = REPO_ROOT / "slurm"
SUBMIT = SLURM_DIR / "submit.sh"

#: Pipeline order. The chain is only meaningful if these stay in this sequence.
STEPS = ["download", "journals", "load", "export", "validate"]

SCRIPTS = [
    SLURM_DIR / "01-download.sbatch",
    SLURM_DIR / "02-journals.sbatch",
    SLURM_DIR / "03-load.sbatch",
    SLURM_DIR / "04-export.sbatch",
    SLURM_DIR / "05-validate.sbatch",
]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs bash to run the Slurm helper scripts"
)


def run_submit(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run ``submit.sh`` from the repo root with a minimal environment."""
    return subprocess.run(
        ["bash", str(SUBMIT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO_ROOT), **env},
    )


@pytest.mark.parametrize("script", SCRIPTS + [SUBMIT, SLURM_DIR / "config.sh"])
def test_scripts_are_syntactically_valid(script: Path) -> None:
    """`bash -n` every script, so a typo cannot reach the cluster."""
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, f"{script.name}: {result.stderr}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_step_declares_its_allocation(script: Path) -> None:
    """The #SBATCH headers are the source of truth, so they must be present.

    slurm/README.md deliberately no longer repeats these values; a script that
    silently lost its header would fall back to the partition default, which is
    exactly the "why did the export get OOM-killed" mystery that costs hours.
    """
    text = script.read_text()
    for directive in ("--job-name=", "--output=", "--mem=", "--time="):
        assert f"#SBATCH {directive}" in text, f"{script.name} is missing {directive}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_step_fails_loudly(script: Path) -> None:
    """`set -e` is what makes --dependency=afterok mean anything.

    Without it a step that fails still exits 0, and the chain cheerfully runs
    the export against a database the load never finished writing.
    """
    assert "set -euo pipefail" in script.read_text()


def test_dry_run_chains_all_steps_in_order() -> None:
    """`all` submits every step, each gated on the previous one."""
    result = run_submit("--dry-run", "all", NCBI_EMAIL="someone@example.org")
    assert result.returncode == 0, result.stderr

    lines = [line for line in result.stdout.splitlines() if line.startswith("sbatch")]
    assert len(lines) == len(STEPS)

    for step, line in zip(STEPS, lines):
        assert step in line, f"expected {step} in {line!r}"

    # The first is unconditional; every later one waits on its predecessor.
    assert "--dependency" not in lines[0]
    for previous, line in zip(STEPS, lines[1:]):
        assert f"--dependency=afterok:<{previous}>" in line


def test_single_step_has_no_dependency() -> None:
    """The step-at-a-time workflow submits exactly one job, ungated."""
    result = run_submit("--dry-run", "load")
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("sbatch")]
    assert len(lines) == 1
    assert "03-load.sbatch" in lines[0]
    assert "--dependency" not in lines[0]


def test_online_validate_without_an_email_is_refused_before_submitting() -> None:
    """Fail at submit time, not after the load has already run for hours."""
    result = run_submit("--dry-run", "load", "validate")
    assert result.returncode == 64
    assert "NCBI_EMAIL" in result.stderr
    # Nothing should have been submitted, including the step that was fine.
    assert "sbatch" not in result.stdout


def test_offline_validate_needs_no_email() -> None:
    result = run_submit("--dry-run", "validate", VALIDATE_OFFLINE="1")
    assert result.returncode == 0, result.stderr
    assert "05-validate.sbatch" in result.stdout


def test_unknown_step_is_rejected() -> None:
    result = run_submit("--dry-run", "frobnicate")
    assert result.returncode == 64
    assert "unknown step" in result.stderr


def test_no_arguments_prints_usage() -> None:
    """Bare `submit.sh` must not fall through to submitting something."""
    result = run_submit()
    assert result.returncode == 64
    assert "sbatch" not in result.stdout


def test_readme_does_not_duplicate_the_allocations() -> None:
    """slurm/README.md explains the numbers; the scripts declare them.

    Keeping both runnable meant two places to edit when the cluster's profile
    changed, with nothing to catch a missed one. Measurements and dated records
    of past runs stay in the README — those are the evidence for the headers,
    not a second copy of them — so this only guards against a *runnable*
    `--mem=`/`--time=` flag creeping back into an `srun` line.
    """
    readme = (SLURM_DIR / "README.md").read_text()
    offenders = [
        line.strip()
        for line in readme.splitlines()
        if "srun " in line
        and ("--mem=" in line or "--time=" in line)
        and "--mem=..." not in line
        # "For the record, that run was submitted as ..." lines are history.
        and "for the record" not in line.lower()
        and "was submitted" not in line.lower()
    ]
    assert not offenders, (
        "runnable srun allocations are back in slurm/README.md; the #SBATCH "
        f"headers are the source of truth: {offenders}"
    )
