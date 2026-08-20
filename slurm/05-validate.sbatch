#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-validate
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=16G
#SBATCH --cpus-per-task=1
#SBATCH --time=02:00:00
#
# Check what the export actually shipped, and record its PMID set.
#
# Small: it reads the export rather than the database, holding one Python set of
# every exported PMID. Measured at 7m 57s and 5.2 GiB peak on the full corpus,
# so 16G is ~3x headroom -- which is the margin to keep, because
# --previous-manifest reads a second PMID set of comparable size.
#
# Needs outbound HTTPS to eutils.ncbi.nlm.nih.gov. Without egress the
# coverage/field/deletion checks fail rather than quietly skipping; set
# VALIDATE_OFFLINE=1 to run structure and DB checks only, then re-run online
# somewhere with egress.
#
# Exit status is the gate: 0 pass, 1 errors, 2 warnings under --fail-on-warn.
# That is what makes this a meaningful last link in a --dependency chain.

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh

: "${VALIDATE_OFFLINE:=0}"
: "${VALIDATE_FAIL_ON_WARN:=0}"

args=(--data-dir "$DATA_DIR")
validate_args=("$EXPORT_DIR")

# This export's PMID set, dated so runs accumulate instead of overwriting each
# other. Written last, after the shard read, so picking the previous manifest
# below cannot pick this one up.
today="$(date +%Y%m%d)"
manifest="$MANIFEST_DIR/pmids-$today.txt.gz"
validate_args+=(--manifest "$manifest")

# The newest earlier manifest, if there is one. Names sort chronologically, so
# the last line wins; today's is skipped so that re-running on the same day
# still diffs against the previous *run*, not against itself.
#
# Today's is excluded by `find -name`, i.e. by base name, rather than by
# comparing candidates against "$manifest" as strings. MANIFEST_DIR is
# environment-overridable, and a trailing slash on it (data/manifests/) makes
# "$manifest" read `data/manifests//pmids-...` while find prints a single
# slash: the same file, never string-equal. The string form silently picked
# today's manifest as the baseline and then overwrote it, so the drop check
# compared the export against itself and reported zero drops however many
# there were.
previous=""
if [[ -d "$MANIFEST_DIR" ]]; then
    while IFS= read -r candidate; do
        previous="$candidate"
    done < <(find "$MANIFEST_DIR" -maxdepth 1 -name 'pmids-*.txt.gz' \
                  ! -name "pmids-$today.txt.gz" | sort)
fi

if [[ -n "$previous" ]]; then
    echo "comparing against previous manifest: $previous"
    validate_args+=(--previous-manifest "$previous")
else
    echo "no previous manifest in $MANIFEST_DIR; drops_since_previous will report 'skip'."
    echo "This run writes one, so the *next* run is the first that can compare (#32)."
fi

# Keep the report beside the manifest as well as in the export directory: the
# next export republishes EXPORT_DIR in place, so a report left only there
# describes a corpus that no longer exists.
report="$MANIFEST_DIR/validation_report-$today.json"
validate_args+=(--out "$report")

if [[ "$VALIDATE_OFFLINE" == "1" ]]; then
    validate_args+=(--offline)
else
    if [[ -z "$NCBI_EMAIL" ]]; then
        echo "error: NCBI_EMAIL is unset and this is an online run." >&2
        echo "Set it (NCBI usage etiquette) or pass VALIDATE_OFFLINE=1." >&2
        exit 64
    fi
    validate_args+=(--email "$NCBI_EMAIL")
    # Raises the rate limit from 3 to 10 req/s. Passed only when present; the
    # key itself is never logged, here or in the report.
    if [[ -n "$NCBI_API_KEY" ]]; then
        validate_args+=(--api-key "$NCBI_API_KEY")
    fi
fi

# `if` rather than `[[ ... ]] && ...`: under `set -e` the latter aborts the whole
# script when the condition is false, because the && list's exit status is 1.
if [[ "$VALIDATE_FAIL_ON_WARN" == "1" ]]; then
    validate_args+=(--fail-on-warn)
fi

mkdir -p "$MANIFEST_DIR"
uv run pubmed2db "${args[@]}" validate "${validate_args[@]}"
