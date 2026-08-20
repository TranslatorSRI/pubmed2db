# Shared settings for the sbatch scripts in this directory. Sourced by each of
# them after it cds to the repo root; not executable on its own.
#
# Every value below can be overridden from the environment at submit time:
#
#   SHARDS=32 ./slurm/submit.sh export
#
# The `:=` form means "unless already set", so an exported value wins.

# Where downloads and the DuckDB database live (the CLI's --data-dir).
: "${DATA_DIR:=data}"

# Where the JSON export is written, and where validate looks for it.
: "${EXPORT_DIR:=data/json}"

# Where PMID manifests accumulate. Deliberately *outside* EXPORT_DIR: one shared
# name would mean a run overwrites the manifest it is diffing against, and a
# manifest inside the export directory survives the next export while
# describing the previous corpus. See slurm/README.md.
: "${MANIFEST_DIR:=data/manifests}"

# JSON shard count. Since the COPY rewrite this is also the export's *thread*
# count -- PER_THREAD_OUTPUT gives one file per writer thread, so export_json
# runs `SET threads = $SHARDS` for the statement. Derived from the allocation
# rather than fixed, because a fixed 16 against 04-export.sbatch's
# --cpus-per-task=8 oversubscribes 2:1 on the one step that gets OOM-killed,
# and would silently raise a group-level --threads set to run *below* an
# allocation on a busy node. Change the sbatch header and this follows.
: "${SHARDS:=${SLURM_CPUS_PER_TASK:-8}}"

# DuckDB's buffer-pool cap per step. These are NOT exported: the CLI reads
# PUBMED2DB_DUCKDB_MEMORY_LIMIT, and a shell-wide value would silently cap the
# export at the load's figure. Each script passes its own via `env`.
#
# Both are starting points rather than measured optima (#37). They sit below
# each step's --mem to leave room for the lxml tree, the parsed records and the
# Arrow batch, which share the same cgroup and are not covered by DuckDB's limit.
: "${LOAD_MEMORY_LIMIT:=48GB}"
: "${EXPORT_MEMORY_LIMIT:=200GB}"

# Fast local scratch for DuckDB to spill into. Only the export is likely to need
# it; the loader inserts file-by-file. Set it to empty (DUCKDB_TEMP_DIR=) to
# leave DuckDB's default -- note the `=` rather than `:=` here, which is what
# makes an explicit empty value stick instead of falling back to the default.
: "${DUCKDB_TEMP_DIR=/local/scratch/duckdb_tmp}"

# uv's package cache. ~/.cache/uv is not always writable on the cluster.
: "${UV_CACHE_DIR:=$PWD/../uv-cache}"
export UV_CACHE_DIR

# NCBI contact details for validate's Entrez calls. NCBI_EMAIL is required by
# the validate step (it fails fast without one); NCBI_API_KEY is optional and
# raises the rate limit from 3 to 10 requests/second. Neither is ever logged.
: "${NCBI_EMAIL:=}"
: "${NCBI_API_KEY:=}"
