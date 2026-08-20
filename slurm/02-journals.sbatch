#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-journals
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=8G
#SBATCH --cpus-per-task=1
#SBATCH --time=02:00:00
#
# Refresh the journal dimension from the NLM Catalog. Small and network-bound.
# Separate from the load because `load` writes article data only, and the export
# reads the journal dimension this populates.
#
# Needs outbound HTTPS to ftp.ncbi.nlm.nih.gov from the compute node.

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh

uv run pubmed2db --data-dir "$DATA_DIR" journals
