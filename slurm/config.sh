# Shared settings for the sbatch scripts in this directory. Sourced by each of
# them after it cds to the repo root; not executable on its own.
#
# Every value below can be overridden from the environment at submit time:
#
#   SHARDS=32 ./slurm/submit.sh export
#
# The `:=` form means "unless already set", so an exported value wins.

# Whether EXPORT_DIR and MANIFEST_DIR were set, read before the defaults below
# fill them in; see the check after them.
_export_dir_given=${EXPORT_DIR+x}
_manifest_dir_given=${MANIFEST_DIR+x}

# Where downloads and the DuckDB database live (the CLI's --data-dir).
: "${DATA_DIR:=data}"

# Where the JSON export is written, and where validate looks for it.
: "${EXPORT_DIR:=$DATA_DIR/json}"

# Where PMID manifests accumulate. Deliberately *outside* EXPORT_DIR: one shared
# name would mean a run overwrites the manifest it is diffing against, and a
# manifest inside the export directory survives the next export while
# describing the previous corpus. See slurm/README.md.
: "${MANIFEST_DIR:=$DATA_DIR/manifests}"

# Until both defaulted under DATA_DIR, they were data/json and data/manifests
# whatever DATA_DIR said. A deployment with DATA_DIR set elsewhere keeps its
# manifest chain, and perhaps an unarchived export, at those old paths, and
# following the new defaults silently would cost it both: validate finds no
# previous manifest and skips the drop check, and the export is invisible to
# the archive guard. So refuse until the operator moves them or names the old
# path. Paths are relative to the repo root, where every caller sources this.
if [[ -z "$_export_dir_given" && -f data/json/metadata.json \
      && ! -f "$EXPORT_DIR/metadata.json" ]]; then
    echo "error: a finished export is in data/json, but EXPORT_DIR now defaults to $EXPORT_DIR." >&2
    echo "Archive it (uv run python scripts/archive_build.py --export-dir data/json) or set EXPORT_DIR=data/json." >&2
    exit 64
fi
if [[ -z "$_manifest_dir_given" ]] && compgen -G "data/manifests/pmids-*.txt.gz" >/dev/null \
      && ! compgen -G "$MANIFEST_DIR/pmids-*.txt.gz" >/dev/null; then
    echo "error: the PMID manifests are in data/manifests, but MANIFEST_DIR now defaults to $MANIFEST_DIR." >&2
    echo "Move them there, or set MANIFEST_DIR=data/manifests, so validate keeps diffing against them." >&2
    exit 64
fi
unset _export_dir_given _manifest_dir_given

# JSON shard count. Since the COPY rewrite this is also the export's *thread*
# count -- PER_THREAD_OUTPUT gives one file per writer thread, so export_json
# runs `SET threads = $SHARDS` for the statement. Derived from the allocation
# rather than fixed, because a fixed 16 against 04-export.sbatch's
# --cpus-per-task=8 oversubscribes 2:1 on the one step that gets OOM-killed,
# and would silently raise a group-level --threads set to run *below* an
# allocation on a busy node. Change the sbatch header and this follows.
# `validate` samples this many records *in total* for the Entrez field check,
# not per shard — the CLI's --sample-size is per shard, and since the export
# writes one shard per writer thread that would make the sample silently track
# --cpus-per-task. 240 is what the pre-2026 corpus runs sampled (15 x 16
# shards), so runs stay comparable when the shard count changes. Set it empty
# to pass no --sample-size at all and take the CLI's own default.
: "${VALIDATE_SAMPLE_TOTAL=240}"

: "${SHARDS:=${SLURM_CPUS_PER_TASK:-8}}"

# DuckDB's buffer-pool cap per step. These are NOT exported: the CLI reads
# PUBMED2DB_DUCKDB_MEMORY_LIMIT, and a shell-wide value would silently cap the
# export at the load's figure. Each script passes its own via `env`.
#
# Both sit below each step's --mem to leave room for the lxml tree, the parsed
# records and the Arrow batch, which share the same cgroup and are not covered
# by DuckDB's limit. The load figure is measured: RSS settles at the cap plus
# ~5-6 GiB at both 32GB and 48GB, with no change in rate, so 48GB under 64G
# leaves ~11 GiB spare (slurm/README.md -> "Running `load`: how much memory?").
# The export figure too: DuckDB fills whatever it is given (peak 153.3 GiB at
# 160GB, 191.9 GiB at 200GB, same wall time), so 160GB is the measured one.
#
# `=` rather than `:=`, so `LOAD_MEMORY_LIMIT= ./slurm/submit.sh load` really
# does leave DuckDB's own cgroup-derived default in place. With `:=` an empty
# value is silently replaced by the default below, which is the opposite of what
# passing it empty means — the same mistake DUCKDB_TEMP_DIR was fixed for and
# these two were not. An empty value is safe all the way down: the CLI's option
# is falsy, so `db.connect` never issues a SET.
: "${LOAD_MEMORY_LIMIT=48GB}"
: "${EXPORT_MEMORY_LIMIT=160GB}"

# Fast local scratch for DuckDB to spill into, e.g. /local/scratch/duckdb_tmp
# on a node that has one. Empty leaves DuckDB's default (next to the database
# file), which is what every ht1 run has actually used: the old default path
# does not exist there, so 04-export.sbatch warned and fell back on every run,
# and neither export spilled anyway -- RSS stayed under the buffer-pool limit.
: "${DUCKDB_TEMP_DIR=}"

# uv's package cache. ~/.cache/uv is not always writable on the cluster.
: "${UV_CACHE_DIR:=$PWD/../uv-cache}"
export UV_CACHE_DIR

# NCBI contact details for validate's Entrez calls. NCBI_EMAIL is required by
# the validate step (it fails fast without one); NCBI_API_KEY is optional and
# raises the rate limit from 3 to 10 requests/second. Neither is ever logged.
: "${NCBI_EMAIL:=}"
: "${NCBI_API_KEY:=}"

# What this step runs with, printed at the top of its log: the commit, the
# allocation Slurm granted, and every setting above. A run's logs are the only
# record it leaves, and the v1.1 build's memory limits had to be confirmed from
# memory after the fact. NCBI_EMAIL and NCBI_API_KEY are left out: see above.
# Each sbatch script calls this right after sourcing the file, so the values
# printed are the ones the step then uses, overrides included.
run_commit() {
    git describe --always --dirty --match 'v*' 2>/dev/null || echo '(not a git checkout)'
}

log_run_settings() {
    local name
    echo "pubmed2db $(run_commit)"
    for name in SLURM_JOB_ID SLURM_JOB_NAME SLURMD_NODENAME SLURM_MEM_PER_NODE \
                SLURM_CPUS_PER_TASK DATA_DIR EXPORT_DIR MANIFEST_DIR SHARDS \
                VALIDATE_SAMPLE_TOTAL LOAD_MEMORY_LIMIT EXPORT_MEMORY_LIMIT \
                DUCKDB_TEMP_DIR UV_CACHE_DIR; do
        echo "  $name=${!name-}"
    done
}
