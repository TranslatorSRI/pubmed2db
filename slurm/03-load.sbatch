#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-load
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=64G
#SBATCH --cpus-per-task=1
#SBATCH --time=24:00:00
#
# Parse downloaded files into DuckDB, keeping full version history. The long
# pole: ~5-6 s/file warm, so ~1,500 files is 2-3 hours, and a full baseline year
# from cold has run considerably slower. Hence --time=24:00:00.
#
# --mem=64G with the buffer pool capped below it. DuckDB's own default is
# already allocation-scaled (~76% of --mem), so the cap is not rescuing it from
# a node-sized cache -- it is leaving room for the lxml tree, the parsed records
# and the Arrow batch, which share the cgroup and are not covered by that limit.
#
# The progress lines report s/file, elapsed, current RSS and peak RSS. Read the
# next run's --time off the rate and the next --mem off the RSS ramp; see
# slurm/README.md -> "Reading the logged numbers".

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh

# Passed per-invocation rather than exported: a shell-wide
# PUBMED2DB_DUCKDB_MEMORY_LIMIT would silently cap the 256G export at this
# figure too.
env PUBMED2DB_DUCKDB_MEMORY_LIMIT="$LOAD_MEMORY_LIMIT" \
  uv run pubmed2db --data-dir "$DATA_DIR" load

uv run pubmed2db --data-dir "$DATA_DIR" status
