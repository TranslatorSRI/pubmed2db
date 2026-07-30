# Running pubmed2db on Slurm

Notes for running the loader and the export on the shared cluster (the same one Babel uses; see
[Babel's slurm/README](https://github.com/NCATSTranslator/Babel/blob/main/slurm/README.md)).

## TL;DR

```bash
# Put uv's package cache somewhere writable (~/.cache/uv may not be).
export UV_CACHE_DIR="$PWD/../uv-cache"

# Don't run on the login node. Request a job with a sane memory cap and time limit:
srun --mem=16G --time=06:00:00 uv run pubmed2db load
```

`download → journals → load` can each be a separate `srun`, or use
`uv run pubmed2db update` to do all three. `export` is a **much** bigger job than
the load — see [Running `export`](#running-export) below:

```bash
srun --mem=256G --cpus-per-task=8 --time=08:00:00 \
  uv run pubmed2db --threads 8 export --format json --out data/json --shards 16
```

## Running `load`: how much memory? (`--mem`)

**Short answer: 16 GB is plenty; 100 GB was ~10–50× too much.**

The loader holds one file in memory at a time (full lxml tree + that file's
parsed records + the Arrow batch), then inserts it and moves on — memory does
**not** grow with the number of files or the size of the database. So peak RSS is
driven by the single largest file, not the whole corpus.

The load logs **peak RSS after every file** so you can size this from real data
instead of guessing:

```
INFO pubmed2db.load: loaded pubmed26n1334.xml.gz: 4989 articles, 0 deletions (peak RSS 0.8 GiB)
```

Observed: a ~5k-article baseline file peaks at ~0.8 GiB; a full ~30k-article file
should stay comfortably under ~8 GiB. Start at `--mem=16G`, watch the logged peak
on a real run, and trim from there.

## Running `load`: how long? (`--time`)

After the Arrow bulk-insert change the load is ~5–6 s/file (≈2 s parse + ≈4 s
insert), so ~1,500 files is **2–3 hours** single-threaded. Request a few hours of
margin, e.g. `--time=06:00:00`. (Before the change it was ~20 min/file — see the
"Performance" note below.)

## Monitoring memory and runtime

Three independent ways, in rough order of convenience:

1. **The per-file log line above** — the simplest in-process signal, no Slurm
   tooling needed.

2. **`seff` after the job finishes** — peak memory and CPU efficiency:

   ```bash
   seff <jobid>
   #   Memory Utilized: 5.20 GB
   #   Memory Efficiency: 32.50% of 16.00 GB
   ```

3. **`sstat` while it runs** / **`sacct` after** — live or historical MaxRSS:

   ```bash
   sstat -j <jobid> --format=JobID,MaxRSS,MaxVMSize        # running job
   sacct -j <jobid> --format=JobID,JobName,MaxRSS,Elapsed,State,ReqMem  # finished
   ```

You can also wrap the command in `/usr/bin/time -v` (what Babel does) to capture
`Maximum resident set size` and wall time to stderr:

```bash
srun --mem=16G /usr/bin/time -v uv run pubmed2db load
```

## Running `export`

**Short answer: `--mem=256G --cpus-per-task=8 --time=08:00:00` with a matching
`--threads 8`, and run it as its own job.**

Unlike the load, export is a *whole-corpus* operation, so its memory scales with
the size of the database rather than with the largest input file:

- `export_json` first materializes `latest_article` into a temp table
  (`_latest_snapshot`) — a window function over the entire `article` table.
- The export query then joins that against a `string_agg` of every abstract and
  sorts the whole result by PMID (`ORDER BY la.pmid`).

Neither step can be done a file at a time, which is why the numbers are an order
of magnitude above the loader's.

**Observed on a full run:** peak RSS **199.6 GiB** with `--mem=256G`, wall time a
few hours. Treat 256 GB as the working figure; do not copy the loader's 16 GB.
Both `export_json` and `export_parquet` log peak RSS on completion, and JSON
logs progress with an ETA every 10 s, so a real run tells you what to request
next time (shape of the lines, counts illustrative):

```
INFO pubmed2db.export: progress: 12500000/38000000 documents (32.9%), ~2h14m remaining
INFO pubmed2db.export: exported 38000000 documents to 16 shard(s) in data/json (peak RSS 199.6 GiB)
```

Notes on the knobs:

- **`--shards N` does not reduce memory, and does not want a CPU each.** All
  shards are written by a single Python loop over one query, round-robining
  lines across open file handles; there is no thread or process per shard, so
  `--cpus-per-task=16` for `--shards 16` would leave 15 cores idle on that step.
  Sharding is for the convenience of whatever ingests the NDJSON. Same for
  `--gzip`, which compresses each shard as it is written (no separate re-read
  pass) and costs CPU rather than memory.
- **CPUs help the query, not the writer.** DuckDB parallelizes the snapshot,
  the `string_agg` and the sort across cores, while the per-row JSON
  serialization stays single-threaded. `--cpus-per-task=8` is a reasonable ask;
  pair it with `--threads` (below) or DuckDB will ignore the allocation. If
  `seff` reports low CPU efficiency afterwards, the writer is the bottleneck and
  more cores won't help.
- **Parquet is the lighter of the two.** `export_parquet` builds the same
  `_latest_snapshot`, but each table is written by a DuckDB `COPY ... TO`, so no
  full result set is pulled through Python. It has not been measured at full
  scale — request the same 256 GB the first time and check `seff`.
- **Run `export` in a separate `srun` from `load`.** `update` deliberately does
  not chain into it, and sizing one job for both means paying the export's memory
  for the load's several hours.
- **Grab the memory before the run, not during.** Slurm will not grow `--mem`
  mid-job; an under-requested export gets OOM-killed hours in, after the snapshot
  and sort have already been paid for.

## DuckDB tuning: `--threads` and `--temp-dir`

Both are **group-level** options, so they go before the subcommand, and both
also read an environment variable — usually the easier form in a batch script:

```bash
export PUBMED2DB_THREADS="${SLURM_CPUS_PER_TASK:-4}"
export PUBMED2DB_DUCKDB_TEMP_DIR=/local/scratch/duckdb_tmp

srun --mem=256G --cpus-per-task=8 --time=08:00:00 \
  uv run pubmed2db export --format json --out data/json --shards 16

# Equivalently, explicit flags (note: before the subcommand):
uv run pubmed2db --threads 8 --temp-dir /local/scratch/duckdb_tmp export ...
```

**`--threads`** caps DuckDB's thread pool. Left alone, DuckDB sizes the pool
from the *machine's* core count, not your allocation — on a 64-core node with
`--cpus-per-task=8` it will try to run 64 threads inside a cgroup that permits
8, which costs time to contention and memory to per-thread operator state (not
free at a 200 GiB peak). Set it to match `--cpus-per-task`.

**`--temp-dir`** is where DuckDB spills when a query exceeds its memory budget.
The loader inserts file-by-file so it should never need to, but the `export`
queries above are exactly the case that can. Point it at fast local scratch
rather than the (probably networked) database directory. (Babel exposes the same
knob as `BABEL_DUCKDB_TEMP_DIR`.)

## Performance note

The load used to take ~20 min/file because rows were inserted one at a time via
`executemany` (~2.5k rows/s). They are now batched into Arrow tables and inserted
columnar (~200k+ rows/s), a ~25–90× speedup on the insert step. To re-measure on
any file:

```bash
uv run python scripts/benchmark_load.py data/pubmed/baseline/pubmed26nNNNN.xml.gz
```
