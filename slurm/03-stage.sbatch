#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-stage
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=48G
#SBATCH --cpus-per-task=16
#SBATCH --time=04:00:00
#
# Parse every downloaded file that is not yet loaded into per-file Parquet
# shards under $STAGE_DIR, $STAGE_WORKERS files at a time, for 04-load.sbatch
# to insert in bulk. No database is written here, which is what lets this
# step use every CPU of its allocation: DuckDB has a single writer, and a
# serial parse-and-insert of the corpus was a day (#68).
#
# Memory is per worker: on the cluster a 30,000-article file took a worker
# to 1.8-2.4 GiB (2026nov25), so 48G is 3 GiB for each of 16; 32G would not
# have held 16 of those at once. A worker's peak is its high-water mark over
# every file it has parsed, so check a full run's later "worker peak" figures
# against that. The same run's rate puts the whole corpus at ~2-3 h on 16
# workers: slurm/README.md, "Running `stage`" has both.
#
# The progress lines report s/file, elapsed and an ETA; each staged file's
# line carries its worker's peak RSS. Read the next run's --time off the
# rate and the next --mem off the peaks.

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh
log_run_settings

uv run pubmed2db --data-dir "$DATA_DIR" --stage-dir "$STAGE_DIR" stage --workers "$STAGE_WORKERS"
