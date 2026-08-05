# Running pubmed2db on Slurm

Notes for running the loader and the export on the shared cluster (the same one Babel uses; see
[Babel's slurm/README](https://github.com/NCATSTranslator/Babel/blob/main/slurm/README.md)).

## TL;DR

```bash
# Put uv's package cache somewhere writable (~/.cache/uv may not be).
export UV_CACHE_DIR="$PWD/../uv-cache"

# Don't run on the login node. Cap DuckDB's cache below --mem, or it will size
# itself from the node's RAM and get OOM-killed hours in (see below).
export PUBMED2DB_DUCKDB_MEMORY_LIMIT=48GB

srun --mem=64G --time=24:00:00 uv run pubmed2db --data-dir data load
```

`download → journals → load` can each be a separate `srun`, or use
`uv run pubmed2db update` to do all three. `export` is a **much** bigger job than
the load — see [Running `export`](#running-export) below:

```bash
srun --mem=256G --cpus-per-task=8 --time=02:00:00 \
  uv run pubmed2db export --format json --out data/json --shards 16
```

## Running `load`: how much memory? (`--mem`)

**Short answer: `--mem=64G` with `PUBMED2DB_DUCKDB_MEMORY_LIMIT` set below it.
Our own per-file working set is ~1 GiB; everything above that is DuckDB's cache,
and it will not restrain itself unless told to.**

The loader holds one file at a time (full lxml tree + that file's parsed records
+ the Arrow batch), then inserts it and moves on. *Our* footprint does not grow
with the number of files or the size of the database.

DuckDB's does. It runs in the same process, and **it sets its buffer-pool limit
to ~80% of the machine's physical RAM, not your Slurm allocation** — the same
mistake `--threads` makes with core count. On a big node that means a limit of
hundreds of GB inside a `--mem=64G` cgroup: DuckDB caches ever more of a growing
database, RSS climbs run-long, and the job is eventually OOM-killed with nothing
to show for it. Cap it explicitly:

```bash
export PUBMED2DB_DUCKDB_MEMORY_LIMIT=48GB     # comfortably under --mem=64G
```

### Reading the logged numbers

Each file logs both figures, and the difference between them is the point:

```
INFO pubmed2db.load: loaded pubmed26n1201.xml.gz: 30000 articles, 0 deletions, 0 failed to parse (RSS 12.4 GiB, peak 42.1 GiB)
```

- **`RSS`** is what the process holds *right now*. It can fall. This is the one
  to watch for genuine growth.
- **`peak`** is `ru_maxrss`, a high-water mark that **only ever rises**. Logged
  once per file it is the maximum over the whole run so far, *not* that file's
  footprint. A climbing `peak` on its own is therefore not evidence of a leak —
  it is what a high-water mark does.

So a run that reaches `peak 42.1 GiB` by file 1201 hit 42 GiB *at some point*;
whether it is still there is what `RSS` tells you. If both climb together and
`RSS` never comes down, DuckDB's limit is too high — lower
`PUBMED2DB_DUCKDB_MEMORY_LIMIT` rather than raising `--mem`.

(`RSS` reads `/proc`, so it shows `n/a` on macOS. That only affects local
development; on the cluster it is always available.)

## Running `load`: how long? (`--time`)

After the Arrow bulk-insert change the load is ~5–6 s/file (≈2 s parse + ≈4 s
insert) on a warm run, so ~1,500 files is **2–3 hours** single-threaded. A full
baseline year from cold has run considerably slower, which is why the TL;DR asks
for `--time=24:00:00`: over-requesting time is free, and being killed at hour six
of a re-parse is not.

Don't guess the next run's limit — the progress line reports the rate and the
elapsed time to date, which is exactly what scales:

```
INFO pubmed2db.load: progress: 4/360 files this run, 356 remaining · 89.7 s/file · elapsed 5m 59s · ~8h 52m to go
```

Multiply `s/file` by the total file count for the next `--time`, and compare
`elapsed` against what you asked for. If the rate is far off ~5–6 s/file, suspect
DuckDB spilling (see `--temp-dir`) or a memory limit set so low that it thrashes.

## Monitoring memory and runtime

Three independent ways, in rough order of convenience:

1. **The progress log lines** — per file for `load` (current RSS, high-water
   peak, rate, elapsed and ETA), every 10 s with an ETA for the JSON export. The simplest in-process signal,
   no Slurm tooling needed, and the only one of the three available on `ht1`.

2. **`sstat` while it runs** / **`sacct` after** — live or historical MaxRSS:

   ```bash
   sstat -j <jobid> --format=JobID,MaxRSS,MaxVMSize        # running job
   sacct -j <jobid> --format=JobID,JobName,MaxRSS,Elapsed,State,ReqMem  # finished
   ```

3. **`/usr/bin/time -v`** (what Babel does) — captures `Maximum resident set
   size` and wall time to stderr, independent of Slurm accounting:

   ```bash
   srun --mem=64G /usr/bin/time -v uv run pubmed2db load
   ```

> **Note:** `seff` is **not installed on `ht1.renci.org`** — use `sacct` or the
> logged peak RSS instead. Long interactive runs are easiest inside `screen`
> with a log: `screen -L -Logfile data/logs/$(date +%Y%b%d).log`.

## Running `export`

**Short answer: `--mem=256G --cpus-per-task=8 --time=02:00:00`, run as its own
job. Memory is the constraint here, not time — the JSON export is fast.**

Unlike the load, export is a *whole-corpus* operation, so its memory scales with
the size of the database rather than with the largest input file:

- `export_json` first materializes `latest_article` into a temp table
  (`_latest_snapshot`) — a window function over the entire `article` table.
- The export query then joins that against a `string_agg` of every abstract and
  sorts the whole result by PMID (`ORDER BY la.pmid`).

Neither step can be done a file at a time, which is why the numbers are an order
of magnitude above the loader's.

**Observed on full JSON exports of the whole corpus (`--shards 16`):**

| Run | Documents | Peak RSS | Wall time |
| --- | --- | --- | --- |
| Earlier | — | 199.6 GiB | "a few hours" (before progress logging; never timed) |
| 2026-07-30 | 40,901,984 | 201.1 GiB | **23m13s**, ≈30k documents/s |

The 2026-07-30 run, on `ht1`, was exactly:

```bash
srun --mem=256G --time=08:00:00 --cpus-per-task 8 \
  uv run pubmed2db export --format json --out data/json --shards 16
# 01:25:01 started, 01:48:14 finished
```

Treat **256 GB** as the working memory figure — it has been stable across runs
and is why this needs a big node; do not copy the loader's 64 GB. Time is the
cheap dimension: two hours is generous margin on 23 minutes.

Both `export_json` and `export_parquet` log peak RSS on completion, and JSON
logs progress with an ETA every 10 s, so a real run tells you what to request
next time:

```
INFO pubmed2db.export: progress: 33385000/40901984 documents (81.6%), ~4m 14s remaining
INFO pubmed2db.export: exported 40901984 documents to 16 shard(s) in data/json (peak RSS 201.1 GiB)
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
  serialization stays single-threaded. `--cpus-per-task 8` is what the measured
  run used and is a sensible default. Note it did *not* pass `--threads`, so
  DuckDB sized its own pool from the node's cores, and the run was still fast —
  so treat `--threads` as insurance against contention on a busy node, not as
  something the export needs.
- **Parquet should be the lighter of the two — but is unmeasured.**
  `export_parquet` builds the same `_latest_snapshot`, then writes each table
  with a DuckDB `COPY ... TO`, so no full result set is pulled through Python.
  It has never been run against the full corpus, so that is reasoning rather
  than a number: request the same 256 GB the first time and read the logged
  peak RSS.
- **Run `export` in a separate `srun` from `load`.** `update` deliberately does
  not chain into it, and sizing one job for both means paying the export's memory
  for the load's several hours.
- **Grab the memory before the run, not during.** Slurm will not grow `--mem`
  mid-job, so an under-requested export gets OOM-killed partway through with
  nothing to show for it.

## DuckDB tuning: `--threads`, `--memory-limit` and `--temp-dir`

All three are **group-level** options, so they go before the subcommand, and all
three also read an environment variable — usually the easier form in a batch
script:

```bash
export PUBMED2DB_THREADS="${SLURM_CPUS_PER_TASK:-4}"
export PUBMED2DB_DUCKDB_MEMORY_LIMIT=200GB
export PUBMED2DB_DUCKDB_TEMP_DIR=/local/scratch/duckdb_tmp

srun --mem=256G --cpus-per-task=8 --time=08:00:00 \
  uv run pubmed2db export --format json --out data/json --shards 16

# Equivalently, explicit flags (note: before the subcommand):
uv run pubmed2db --threads 8 --memory-limit 200GB \
  --temp-dir /local/scratch/duckdb_tmp export ...
```

**The common thread: DuckDB sizes itself from the machine, not the cgroup.** It
reads the node's core count for `threads` and the node's physical RAM for
`memory_limit`, and Slurm's limits are invisible to it. On a shared cluster both
defaults are wrong in the same direction — too big — and the memory one is the
expensive mistake, because exceeding it is an OOM kill rather than contention.

**`--threads`** caps DuckDB's thread pool. Left alone, DuckDB sizes the pool
from the *machine's* core count rather than your allocation, so on a 64-core
node with `--cpus-per-task=8` it may run 64 threads inside a cgroup that permits
8 — contention, plus per-thread operator state that isn't free at a 200 GiB
peak. In practice the measured export ran fine without it, so reach for this
only if a run is slower than the numbers above or the node is busy.

**`--memory-limit`** caps the buffer pool. Left alone DuckDB picks ~80% of
physical RAM — on a 512 GB node that is ~410 GB, regardless of your `--mem=64G`.
It is the likeliest cause of a load whose RSS climbs steadily and then dies hours
in. Set it a comfortable margin below `--mem` (the process needs room for lxml
and the Arrow batch on top of it): `48GB` under `--mem=64G`, `200GB` under
`--mem=256G`. Setting it *too* low is not free either — DuckDB will spill to
`--temp-dir` instead of caching, which shows up as a collapsed s/file rate.

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
