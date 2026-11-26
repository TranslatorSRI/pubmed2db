#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-stage
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=32G
#SBATCH --cpus-per-task=16
#SBATCH --time=04:00:00
#
# Parse every downloaded file that is not yet loaded into per-file Parquet
# shards under $STAGE_DIR, $STAGE_WORKERS files at a time, for 04-load.sbatch
# to insert in bulk. No database is written here, which is what lets this
# step use every CPU of its allocation: DuckDB has a single writer, and a
# serial parse-and-insert of the corpus was a day (#68).
#
# Memory is per worker: the parse of a 30,000-article baseline file peaks at
# ~1.1 GiB since it streams the XML (#25), plus the Arrow tables it writes,
# so 16 workers under 32G leaves headroom; measure the first run's
# "worker peak" figures in the log and resize. The parse is ~7 s/file on a
# laptop core, so the whole 2026 corpus (1,636 files) is ~3 h of CPU, which
# 16 workers cover many times over inside the limit above.
#
# The progress lines report s/file, elapsed and an ETA; each staged file's
# line carries its worker's peak RSS. Read the next run's --time off the
# rate and the next --mem off the peaks.

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh
log_run_settings

uv run pubmed2db --data-dir "$DATA_DIR" --stage-dir "$STAGE_DIR" stage --workers "$STAGE_WORKERS"
