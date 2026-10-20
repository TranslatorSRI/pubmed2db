#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-load
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=64G
#SBATCH --cpus-per-task=1
#SBATCH --time=36:00:00
#
# Parse downloaded files into DuckDB, keeping full version history. The long
# pole: a from-scratch load of the 2026 corpus took 21h 29m for 1,636 files
# (2026-09-25; 47 s/file on average, climbing from ~30 s/file on an empty
# database to ~65 s/file by the end of the baseline). That used 90% of a 24h
# limit, and every month's update files add to the count, hence 36h. A
# killed load resumes where it stopped, so the cost of running over is a
# resubmit, not lost work.
#
# --mem=64G with the buffer pool capped below it. DuckDB's own default is
# already allocation-scaled (~76% of --mem), so the cap is not rescuing it from
# a node-sized cache -- it is leaving room for the lxml tree, the parsed records
# and the Arrow batch, which share the cgroup and are not covered by that limit.
# Measured: RSS settles at the cap plus ~5-6 GiB (53.3 GiB peak under 48GB),
# so 48GB under 64G leaves ~11 GiB spare.
#
# The progress lines report s/file, elapsed, current RSS and peak RSS. Read the
# next run's --time off the rate and the next --mem off the RSS ramp; see
# slurm/README.md -> "Reading the logged numbers".

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh
log_run_settings

# Passed per-invocation rather than exported: a shell-wide
# PUBMED2DB_DUCKDB_MEMORY_LIMIT would silently cap the 256G export at this
# figure too.
env PUBMED2DB_DUCKDB_MEMORY_LIMIT="$LOAD_MEMORY_LIMIT" \
  uv run pubmed2db --data-dir "$DATA_DIR" load

uv run pubmed2db --data-dir "$DATA_DIR" status
