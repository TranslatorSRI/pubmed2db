#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-export
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=256G
#SBATCH --cpus-per-task=8
#SBATCH --time=02:00:00
#
# Export the latest version of every abstract to NDJSON shards.
#
# Memory is the constraint, not time: this is a whole-corpus operation (a window
# function over every article, then a string_agg and a global sort), so its
# footprint scales with the database rather than the largest input file.
# Measured at 201.1 GiB peak and 23m13s for 40.9M documents, hence 256G and a
# generous two hours. Do NOT copy the loader's 64G here.
#
# Slurm will not grow --mem mid-job, so an under-requested export is OOM-killed
# partway through with nothing to show for it. Grab the memory up front.
#
# --cpus-per-task=8 helps the query (DuckDB parallelizes the snapshot, the
# string_agg and the sort); the per-row JSON serialization is single-threaded,
# and --shards does not want a CPU each.

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh

temp_args=()
if [[ -n "$DUCKDB_TEMP_DIR" ]]; then
    # DuckDB errors rather than falling back if the spill directory is missing,
    # and node-local scratch is often not pre-created.
    if mkdir -p "$DUCKDB_TEMP_DIR" 2>/dev/null; then
        temp_args=(--temp-dir "$DUCKDB_TEMP_DIR")
    else
        echo "warning: cannot create $DUCKDB_TEMP_DIR; leaving DuckDB's default spill location" >&2
    fi
fi

# `${temp_args[@]+"${temp_args[@]}"}` rather than a bare "${temp_args[@]}":
# expanding an empty array under `set -u` is an "unbound variable" error on bash
# 3.2, and both paths that leave it empty (no spill dir configured, or one that
# could not be created) are exactly the cases that must not die here.
env PUBMED2DB_DUCKDB_MEMORY_LIMIT="$EXPORT_MEMORY_LIMIT" \
  uv run pubmed2db --data-dir "$DATA_DIR" ${temp_args[@]+"${temp_args[@]}"} \
    export --format json --out "$EXPORT_DIR" --shards "$SHARDS"
