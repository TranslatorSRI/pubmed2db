#!/usr/bin/env bash
#SBATCH --job-name=pubmed2db-journals
#SBATCH --output=data/logs/%x-%j.out
#SBATCH --mem=8G
#SBATCH --cpus-per-task=1
#SBATCH --time=02:00:00
#
# Refresh the journal dimension from the NLM Catalog. Network-bound; the work
# itself is small (streaming the ~449 MB serial catalog is ~5 s at ~50 MiB RSS,
# so the 8G above is untouched by it). Separate from the load because `load`
# writes article data only, and the export reads the journal dimension this
# populates.
#
# Needs outbound HTTPS from the compute node to ftp.ncbi.nlm.nih.gov (the journal
# overview) and ftp.nlm.nih.gov (the serial catalog), plus ~890 MB of cache under
# $DATA_DIR. A catalog fetch that fails degrades to NULL publication years rather
# than failing the job -- see docs/journal-catalog.md.

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
source slurm/config.sh

uv run pubmed2db --data-dir "$DATA_DIR" journals
