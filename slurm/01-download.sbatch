#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-download
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=8G
#SBATCH --cpus-per-task=1
#SBATCH --time=12:00:00
#
# Fetch baseline/update files and record their MD5s. Network-bound, not
# memory-bound: it holds one file at a time. --time is generous because a cold
# baseline year is a lot of bytes over a shared link, and over-requesting time
# is free while being killed at hour six of a resumable download is not.
#
# Needs outbound HTTPS to ftp.ncbi.nlm.nih.gov from the compute node.

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh

uv run pubmed2db --data-dir "$DATA_DIR" download

# Re-running revalidates what is already on disk rather than refetching, so this
# is safe to repeat after a timeout.
uv run pubmed2db --data-dir "$DATA_DIR" status
