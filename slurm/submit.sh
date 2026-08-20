#!/usr/bin/env bash
#
# Submit one pipeline step, or chain them all.
#
#   ./slurm/submit.sh load              # one step; read its log, then decide
#   ./slurm/submit.sh export validate   # two, the second gated on the first
#   ./slurm/submit.sh all               # download -> journals -> load -> export -> validate
#   ./slurm/submit.sh --dry-run all     # print the sbatch commands and stop
#
# Chained steps use --dependency=afterok plus --kill-on-invalid-dep=yes, so a
# step that exits non-zero cancels everything after it rather than leaving it
# pending forever. That is the automated form of reading each log before
# starting the next one -- but only for failures the exit status reports, which
# is why `all` is the hands-off option and one-step-at-a-time remains the
# careful one.
#
# Run from the repo root. Settings live in slurm/config.sh.

set -euo pipefail

usage() {
    sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
    echo
    echo "Steps: ${STEPS[*]}, or 'all'."
    exit "${1:-0}"
}

# Pipeline order; `all` runs them in this sequence. A `case` rather than an
# associative array so this works on bash 3.2 as well as 4+.
STEPS=(download journals load export validate)

step_file() {
    case "$1" in
        download) echo slurm/01-download.sbatch ;;
        journals) echo slurm/02-journals.sbatch ;;
        load)     echo slurm/03-load.sbatch ;;
        export)   echo slurm/04-export.sbatch ;;
        validate) echo slurm/05-validate.sbatch ;;
        *)        return 1 ;;
    esac
}

dry_run=0
requested=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run) dry_run=1 ;;
        -h|--help)    usage 0 ;;
        all)          requested+=("${STEPS[@]}") ;;
        -*)           echo "unknown option: $1" >&2; usage 64 >&2 ;;
        *)
            if ! step_file "$1" >/dev/null; then
                echo "unknown step: $1" >&2
                echo "Steps: ${STEPS[*]}, or 'all'." >&2
                exit 64
            fi
            requested+=("$1")
            ;;
    esac
    shift
done

if [[ ${#requested[@]} -eq 0 ]]; then
    usage 64 >&2
fi

# Drop repeats, keeping the first position of each. `all validate` would
# otherwise submit validate twice, the second gated on the first -- and a second
# validate rewrites the dated manifest and report, then (today's manifest now
# existing) diffs against the wrong baseline. A plain loop rather than an
# associative array, for bash 3.2.
deduped=()
for step in "${requested[@]}"; do
    seen=0
    for kept in ${deduped[@]+"${deduped[@]}"}; do
        if [[ "$kept" == "$step" ]]; then
            seen=1
            break
        fi
    done
    if [[ "$seen" == "0" ]]; then
        deduped+=("$step")
    else
        echo "note: $step was named more than once; submitting it once." >&2
    fi
done
requested=("${deduped[@]}")

if [[ ! -f pyproject.toml || ! -d slurm ]]; then
    echo "error: run this from the repository root (the sbatch scripts use paths relative to it)." >&2
    exit 64
fi

source slurm/config.sh

# sbatch refuses to submit when the --output directory does not exist, and the
# failure is a terse "Batch job submission failed" that does not name the path.
log_dir="$DATA_DIR/logs"
mkdir -p "$log_dir"

# The #SBATCH --output directive inside each script is a static default; pass it
# explicitly so the logs follow DATA_DIR when that is overridden.
common_args=(--output "$log_dir/%x-%j.out")

if [[ "$dry_run" == "0" ]] && ! command -v sbatch >/dev/null 2>&1; then
    echo "error: sbatch not found. Are you on the cluster? (--dry-run works anywhere.)" >&2
    exit 69
fi

# Fail before submitting a chain that would die hours later on a missing email,
# rather than after the load has already run.
for step in "${requested[@]}"; do
    if [[ "$step" == "validate" && "${VALIDATE_OFFLINE:-0}" != "1" && -z "$NCBI_EMAIL" ]]; then
        echo "error: NCBI_EMAIL is unset, and validate is an online run by default." >&2
        echo "Set NCBI_EMAIL=you@example.org, or VALIDATE_OFFLINE=1 to skip the Entrez checks." >&2
        exit 64
    fi
done

previous_job=""
for step in "${requested[@]}"; do
    args=("${common_args[@]}")
    if [[ -n "$previous_job" ]]; then
        # --kill-on-invalid-dep is not redundant with afterok. When the
        # dependency can never be satisfied, Slurm's *default* is to leave the
        # dependent pending forever with reason DependencyNeverSatisfied --
        # cancelling it only where the site set kill_invalid_depend in
        # slurm.conf. Without this flag a load that fails at hour three leaves
        # the export and validate squatting in the queue until someone notices,
        # which is not what this script's header promises.
        args+=(--dependency="afterok:$previous_job" --kill-on-invalid-dep=yes)
    fi
    args+=("$(step_file "$step")")

    if [[ "$dry_run" == "1" ]]; then
        echo "sbatch ${args[*]}"
        # Keep the printed chain readable: a fake id shows where the dependency
        # would be threaded without pretending to be a real job.
        previous_job="<$step>"
        continue
    fi

    job_id=$(sbatch --parsable "${args[@]}")
    # --parsable prints "jobid;clustername" on a federated/multi-cluster site
    # and a bare jobid elsewhere. The suffix would land inside the next
    # --dependency=afterok:... and be rejected at submit time, breaking the
    # chain on exactly the sites where it is hardest to debug.
    job_id="${job_id%%;*}"
    if [[ -n "$previous_job" ]]; then
        printf 'submitted %-9s job %s (after %s)\n' "$step" "$job_id" "$previous_job"
    else
        printf 'submitted %-9s job %s\n' "$step" "$job_id"
    fi
    previous_job="$job_id"
done

if [[ "$dry_run" == "0" ]]; then
    echo
    echo "Logs:    $log_dir/pubmed2db-<step>-<jobid>.out"
    echo "Watch:   squeue -u \"\$USER\""
    echo "Cancel:  scancel <jobid>   (afterok dependents are cancelled with it)"
fi
