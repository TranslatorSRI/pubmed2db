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
import tempfile
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


def test_chained_steps_are_killed_when_their_dependency_can_never_run() -> None:
    """afterok alone does not cancel the rest of the chain; the flag does.

    Slurm's default for a dependency that can never be satisfied is to leave the
    dependent *pending forever* with reason DependencyNeverSatisfied, cancelling
    it only where the site set kill_invalid_depend in slurm.conf. submit.sh's
    header promises cancellation, so it has to ask for it explicitly -- otherwise
    a load that fails at hour three leaves the export and validate squatting in
    the queue.
    """
    result = run_submit("--dry-run", "all", NCBI_EMAIL="someone@example.org")
    assert result.returncode == 0, result.stderr

    lines = [line for line in result.stdout.splitlines() if line.startswith("sbatch")]
    for line in lines[1:]:
        assert "--kill-on-invalid-dep=yes" in line, line
    # The first job has nothing to depend on, so the flag would be noise.
    assert "--kill-on-invalid-dep" not in lines[0]


def test_a_step_named_twice_is_submitted_once() -> None:
    """`all validate` must not chain a second validate onto the first.

    Two validates run against the same export, and the second rewrites the dated
    manifest and report -- then, today's manifest now existing, skips it and
    diffs against the run before. Cheaper to refuse the duplicate than to explain
    the result.
    """
    result = run_submit("--dry-run", "all", "validate", NCBI_EMAIL="someone@example.org")
    assert result.returncode == 0, result.stderr

    lines = [line for line in result.stdout.splitlines() if line.startswith("sbatch")]
    assert len(lines) == len(STEPS)
    assert sum("05-validate.sbatch" in line for line in lines) == 1
    assert "more than once" in result.stderr


def test_a_federated_job_id_is_stripped_before_it_reaches_a_dependency() -> None:
    """`sbatch --parsable` prints "jobid;clustername" on a multi-cluster site.

    Interpolated whole, the suffix lands inside --dependency=afterok:12345;ht1,
    which sbatch rejects -- so the chain breaks at submit time on exactly the
    sites hardest to debug from here. This is the one test that runs the real
    submit path rather than --dry-run, against a stub sbatch.
    """
    with tempfile.TemporaryDirectory() as tmp:
        stub_dir = Path(tmp)
        sbatch = stub_dir / "sbatch"
        sbatch.write_text('#!/bin/sh\necho "12345;ht1"\n')
        sbatch.chmod(0o755)

        result = subprocess.run(
            ["bash", str(SUBMIT), "export", "validate"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={
                "PATH": f"{stub_dir}:/usr/bin:/bin",
                "HOME": str(REPO_ROOT),
                "NCBI_EMAIL": "someone@example.org",
                # Keep the stub run out of the real ./data directory.
                "DATA_DIR": str(stub_dir / "data"),
            },
        )
    assert result.returncode == 0, result.stderr
    assert "job 12345 " in result.stdout or result.stdout.rstrip().endswith("job 12345")
    assert "ht1" not in result.stdout


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


# --------------------------------------------------------------------------- #
# 05-validate.sbatch: manifest selection
# --------------------------------------------------------------------------- #
#
# This is the only real logic in the sbatch scripts, and getting it wrong is
# quiet: passing the wrong --previous-manifest still exits 0 and still writes a
# report, it just compares against the wrong corpus. The script is exercised
# with a stub `uv` on PATH that echoes its arguments, so the assertions are on
# the command line the script would have run.


VALIDATE_SCRIPT = SLURM_DIR / "05-validate.sbatch"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A minimal repo the sbatch scripts can run inside, with `uv` stubbed."""
    shutil.copytree(SLURM_DIR, tmp_path / "slurm")
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "data" / "json").mkdir(parents=True)
    (tmp_path / "data" / "manifests").mkdir(parents=True)

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    uv = stub_dir / "uv"
    uv.write_text('#!/bin/sh\necho "UV_ARGS: $*"\n')
    uv.chmod(0o755)
    return tmp_path


def run_validate(sandbox: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "slurm/05-validate.sbatch"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{sandbox / 'bin'}:/usr/bin:/bin",
            "HOME": str(sandbox),
            "SLURM_SUBMIT_DIR": str(sandbox),
            **env,
        },
    )


def manifests(sandbox: Path) -> Path:
    return sandbox / "data" / "manifests"


def test_validate_writes_a_dated_manifest(sandbox: Path) -> None:
    result = run_validate(sandbox, NCBI_EMAIL="me@example.org")
    assert result.returncode == 0, result.stderr
    assert "--manifest data/manifests/pmids-" in result.stdout


def test_validate_reports_no_previous_manifest_on_a_first_run(sandbox: Path) -> None:
    """The first run cannot compare, and should say so rather than stay silent."""
    result = run_validate(sandbox, NCBI_EMAIL="me@example.org")
    assert result.returncode == 0, result.stderr
    assert "--previous-manifest" not in result.stdout
    assert "no previous manifest" in result.stdout


def test_validate_picks_the_newest_earlier_manifest(sandbox: Path) -> None:
    """Names sort chronologically, so the latest earlier run wins."""
    (manifests(sandbox) / "pmids-20260701.txt.gz").touch()
    (manifests(sandbox) / "pmids-20260805.txt.gz").touch()

    result = run_validate(sandbox, NCBI_EMAIL="me@example.org")
    assert result.returncode == 0, result.stderr
    assert "--previous-manifest data/manifests/pmids-20260805.txt.gz" in result.stdout
    assert "20260701" not in result.stdout


def test_validate_never_diffs_a_rerun_against_itself(sandbox: Path) -> None:
    """A same-day re-run must compare against the previous *run*, not today's file.

    The script writes today's manifest at the end, so on a second run that file
    already exists. Picking it would compare the export against itself and
    report no drops however many there were.
    """
    import datetime

    today = datetime.date.today().strftime("%Y%m%d")
    (manifests(sandbox) / f"pmids-{today}.txt.gz").touch()
    (manifests(sandbox) / "pmids-20260805.txt.gz").touch()

    result = run_validate(sandbox, NCBI_EMAIL="me@example.org")
    assert result.returncode == 0, result.stderr
    assert "--previous-manifest data/manifests/pmids-20260805.txt.gz" in result.stdout
    assert f"--previous-manifest data/manifests/pmids-{today}" not in result.stdout


def test_validate_passes_the_api_key_only_when_set(sandbox: Path) -> None:
    without = run_validate(sandbox, NCBI_EMAIL="me@example.org")
    assert "--api-key" not in without.stdout

    with_key = run_validate(sandbox, NCBI_EMAIL="me@example.org", NCBI_API_KEY="k")
    assert "--api-key k" in with_key.stdout


def test_validate_offline_needs_no_email(sandbox: Path) -> None:
    result = run_validate(sandbox, VALIDATE_OFFLINE="1")
    assert result.returncode == 0, result.stderr
    assert "--offline" in result.stdout
    assert "--email" not in result.stdout


def test_validate_online_without_an_email_fails(sandbox: Path) -> None:
    result = run_validate(sandbox)
    assert result.returncode == 64
    assert "NCBI_EMAIL" in result.stderr
    assert "UV_ARGS" not in result.stdout


def test_fail_on_warn_is_off_by_default(sandbox: Path) -> None:
    """The flag is opt-in, and enabling it reaches the command line.

    The script spells this as a plain `if` rather than `[[ cond ]] && arr+=(...)`
    as a hedge, not a fix: mid-script that form is harmless under `set -e`
    (measured — the AND-OR list's non-final commands are exempt), but it returns
    1 as the last statement of a script or function, so it breaks the moment
    someone moves it or appends to the block. This test pins the behaviour, not
    the spelling.
    """
    default = run_validate(sandbox, VALIDATE_OFFLINE="1")
    assert default.returncode == 0, default.stderr
    assert "--fail-on-warn" not in default.stdout

    enabled = run_validate(sandbox, VALIDATE_OFFLINE="1", VALIDATE_FAIL_ON_WARN="1")
    assert enabled.returncode == 0, enabled.stderr
    assert "--fail-on-warn" in enabled.stdout


def test_export_survives_an_uncreatable_spill_directory(sandbox: Path) -> None:
    """The fallback path must not itself fail.

    Expanding an empty array under `set -u` is an error on bash 3.2, and that is
    exactly what this branch leaves behind when the spill directory cannot be
    made — a second failure on the path handling the first.
    """
    result = subprocess.run(
        ["bash", "slurm/04-export.sbatch"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{sandbox / 'bin'}:/usr/bin:/bin",
            "HOME": str(sandbox),
            "SLURM_SUBMIT_DIR": str(sandbox),
            "DUCKDB_TEMP_DIR": "/proc/nope/xyz",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "warning: cannot create" in result.stderr
    assert "--temp-dir" not in result.stdout
    assert "UV_ARGS" in result.stdout


def test_a_trailing_slash_on_manifest_dir_still_excludes_todays_manifest(
    sandbox: Path,
) -> None:
    """MANIFEST_DIR is environment-overridable, and the slash is a free typo.

    The exclusion used to compare `find` output against "$manifest" as strings,
    so `data/manifests/` made the two spellings of the same file -- one slash and
    two -- never match. Today's manifest became the baseline and was then
    overwritten by --manifest, i.e. drops_since_previous compared the export
    against itself and reported zero drops however many there were. Excluding by
    base name is what makes the spelling irrelevant.
    """
    import datetime

    today = datetime.date.today().strftime("%Y%m%d")
    (manifests(sandbox) / f"pmids-{today}.txt.gz").touch()
    (manifests(sandbox) / "pmids-20260805.txt.gz").touch()

    result = run_validate(
        sandbox, NCBI_EMAIL="me@example.org", MANIFEST_DIR="data/manifests/"
    )
    assert result.returncode == 0, result.stderr
    assert "--previous-manifest data/manifests/pmids-20260805.txt.gz" in result.stdout
    assert f"--previous-manifest data/manifests//pmids-{today}" not in result.stdout
    assert f"--previous-manifest data/manifests/pmids-{today}" not in result.stdout
