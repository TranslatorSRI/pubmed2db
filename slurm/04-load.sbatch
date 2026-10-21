#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-load
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=64G
#SBATCH --cpus-per-task=8
#SBATCH --time=36:00:00
#
# Insert the shards 03-stage.sbatch wrote into DuckDB, $LOAD_FILES_PER_TXN
# files per transaction, keeping full version history; a file with no shard
# is parsed here, one at a time, as every file was before v1.3. The serial
# form was the long pole: a from-scratch load of the 2026 corpus took 21h 29m
# for 1,636 files (2026-09-25; 47 s/file on average, climbing from ~30 s/file
# on an empty database to ~65 s/file by the end of the baseline), which is why
# the parse moved out to its own parallel step (#68). The 36h limit is the
# serial run's until the first staged run is measured; a killed load resumes
# where it stopped, so the cost of running over is a resubmit, not lost work.
#
# --cpus-per-task=8 because reading the shards and inserting them is DuckDB's
# work now, and it parallelizes both; measure before raising it further.
#
# --mem=64G with the buffer pool capped below it. DuckDB's own default is
# already allocation-scaled (~76% of --mem), so the cap is not rescuing it from
# a node-sized cache -- it is leaving room for the process's own working set,
# which shares the cgroup and is not covered by that limit. Measured on the
# serial loader: RSS settles at the cap plus ~5-6 GiB (53.3 GiB peak under
# 48GB), so 48GB under 64G leaves ~11 GiB spare; the staged loader holds no
# parsed file, so expect less.
#
# The progress lines report s/file, elapsed, current RSS and peak RSS, and
# each group's line says where its time went (delete, insert, commit). Read
# the next run's --time off the rate and the next --mem off the RSS ramp; see
# slurm/README.md -> "Reading the logged numbers".

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh
log_run_settings

# Passed per-invocation rather than exported: a shell-wide
# PUBMED2DB_DUCKDB_MEMORY_LIMIT would silently cap the 256G export at this
# figure too.
env PUBMED2DB_DUCKDB_MEMORY_LIMIT="$LOAD_MEMORY_LIMIT" \
    PUBMED2DB_DUCKDB_CHECKPOINT_THRESHOLD="$LOAD_CHECKPOINT_THRESHOLD" \
  uv run pubmed2db --data-dir "$DATA_DIR" --stage-dir "$STAGE_DIR" load \
    --files-per-txn "$LOAD_FILES_PER_TXN"

uv run pubmed2db --data-dir "$DATA_DIR" status
