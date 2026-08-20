# Running pubmed2db on Slurm

Notes for running the loader and the export on the shared cluster (the same one Babel uses; see
[Babel's slurm/README](https://github.com/NCATSTranslator/Babel/blob/main/slurm/README.md)).

## TL;DR

Each pipeline step is an `sbatch` script in this directory, sized from the
measurements below. Submit them one at a time and read each log before starting
the next:

```bash
export NCBI_EMAIL=you@example.org     # required by validate; NCBI_API_KEY optional

./slurm/submit.sh download
./slurm/submit.sh journals
./slurm/submit.sh load
./slurm/submit.sh export
./slurm/submit.sh validate
```

Or chain the whole pipeline and walk away — each step is submitted with
`--dependency=afterok` on the one before, so a failure cancels the rest:

```bash
./slurm/submit.sh all
./slurm/submit.sh --dry-run all       # print the sbatch commands without submitting
```

Logs land in `data/logs/pubmed2db-<step>-<jobid>.out`. Between steps,
`uv run pubmed2db status` reports what has been downloaded, loaded and is ready
to export, which is the quickest way to confirm a step did what you expected.

> **The `#SBATCH` headers in those scripts are the source of truth for what each
> step requests.** This file explains *why* each figure is what it is and records
> the runs it came from; it deliberately no longer repeats the flags, so a
> changed profile is a one-file edit. `slurm/config.sh` holds the settings shared
> across steps (data directory, shard count, DuckDB caps, NCBI credentials).

| Step | Script | Shape |
| --- | --- | --- |
| `download` | `01-download.sbatch` | small, network-bound, long |
| `journals` | `02-journals.sbatch` | small, network-bound |
| `load` | `03-load.sbatch` | the long pole; memory-capped |
| `export` | `04-export.sbatch` | the big one; whole-corpus, memory is the constraint |
| `validate` | `05-validate.sbatch` | small, needs internet, writes the PMID manifest |

`validate` passes a dated `--manifest` on every run: it is the only record of
*which* PMIDs an export contained, and the next run cannot diff against a
manifest nobody wrote (#32). The script picks the newest earlier manifest as
`--previous-manifest` automatically. A run that *fails* writes no manifest, so a
truncated export cannot become the next run's baseline.

## The scripts

```
slurm/
  config.sh            settings shared across steps; every value overridable from the environment
  01-download.sbatch   \
  02-journals.sbatch    |  one step each. The #SBATCH headers are the only place
  03-load.sbatch        |  an allocation is written down.
  04-export.sbatch      |
  05-validate.sbatch   /
  submit.sh            submits one step, several, or `all` chained with --dependency=afterok
```

Each script `cd`s to `$SLURM_SUBMIT_DIR` and sources `config.sh`, so **submit
from the repository root**. `submit.sh` checks that for you, creates the log
directory (sbatch refuses to start when it is missing, with an error that does
not name the path), and refuses to submit a chain ending in an online `validate`
when `NCBI_EMAIL` is unset — better than discovering it after the load has run.

Override anything for one submission without editing a file:

```bash
SHARDS=32 ./slurm/submit.sh export
DATA_DIR=/scratch/$USER/pubmed ./slurm/submit.sh all
VALIDATE_OFFLINE=1 ./slurm/submit.sh validate
```

Steps remain ordinary scripts: `sbatch slurm/03-load.sbatch` works, and so does
running the `uv run` line inside one directly on an interactive node. One catch
if you bypass `submit.sh`: `#SBATCH --output` is a static directive, so it
cannot follow a `DATA_DIR` you overrode — a bare `sbatch` writes its log under
`data/logs/` whatever `DATA_DIR` says, and fails outright if that directory does
not exist. `submit.sh` passes `--output` on the command line (which overrides
the directive) and creates the directory first, which is why it is the
recommended entry point rather than a convenience.

## Running `load`: how much memory? (`--mem`)

**Short answer: `03-load.sbatch` asks for a generous allocation and caps DuckDB's
buffer pool below it. Our own per-file working set is ~1 GiB; everything above
that is DuckDB's cache, and it will not restrain itself unless told to.**

The loader holds one file at a time (full lxml tree + that file's parsed records
+ the Arrow batch), then inserts it and moves on. *Our* footprint does not grow
with the number of files or the size of the database.

DuckDB's does. It runs in the same process and caches ever more of a growing
database, so its buffer pool is what makes a long load's RSS climb run-long.

**DuckDB does see the Slurm cgroup**, so its default is not the node-sized
disaster it looks like: measured on duckdb 1.5.4, it takes ~76% of `--mem`
(6.1 GiB under `--mem=8G`, 47.3 GiB under `--mem=62G`; off a cluster it is ~80%
of physical RAM). The problem with the default is subtler — **its limit governs
only its own buffer pool**, while the lxml tree, the parsed records and the
Arrow batch live in the same cgroup and count against the same `--mem`. A
default that claims three quarters of the allocation leaves the rest of the
process a quarter, and the loader's own footprint is not small.

So cap it to buy that headroom back, not to rescue DuckDB from itself. That is
what `LOAD_MEMORY_LIMIT` in `slurm/config.sh` does.

The scripts pass it per-invocation (`env PUBMED2DB_DUCKDB_MEMORY_LIMIT=… uv run
…`) rather than exporting it, and that detail matters if you run a step by hand:
it is a group-level setting every subcommand reads, so a shell-wide value
silently caps a later export at the load's much smaller figure.

### Reading the logged numbers

Each file logs both figures, and the difference between them is the point:

```
INFO pubmed2db.load: loaded pubmed26n1201.xml.gz: 30000 articles, 0 deletions, 0 failed to parse, 0 book record(s) skipped (RSS 12.4 GiB, peak 42.1 GiB)
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
baseline year from cold has run considerably slower, which is why
`03-load.sbatch` asks for far more `--time` than the warm figure needs:
over-requesting time is free, and being killed at hour six of a re-parse is not.

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
   peak, rate, elapsed and ETA), once a minute with an ETA for the JSON export.
   The simplest in-process signal,
   no Slurm tooling needed, and the only one of the three available on `ht1`.

2. **`sstat` while it runs** / **`sacct` after** — live or historical MaxRSS:

   ```bash
   sstat -j <jobid> --format=JobID,MaxRSS,MaxVMSize        # running job
   sacct -j <jobid> --format=JobID,JobName,MaxRSS,Elapsed,State,ReqMem  # finished
   ```

3. **`/usr/bin/time -v`** (what Babel does) — captures `Maximum resident set
   size` and wall time to stderr, independent of Slurm accounting:

   ```bash
   # Wrap the `uv run` line inside the relevant sbatch script, or interactively:
   srun --mem=... /usr/bin/time -v uv run pubmed2db load
   ```

> **Note:** `seff` is **not installed on `ht1.renci.org`** — use `sacct` or the
> logged peak RSS instead. Long interactive runs are easiest inside `screen`
> with a log: `screen -L -Logfile data/logs/$(date +%Y%b%d).log`.

## Running `export`

**Short answer: `./slurm/submit.sh export`, as its own job. Memory is the
constraint here, not time — the JSON export is fast.**

Unlike the load, export is a *whole-corpus* operation, so its memory scales with
the size of the database rather than with the largest input file:

- `export_json` first materializes `latest_article` into a temp table
  (`_latest_snapshot`) — a window function over the entire `article` table.
- The export query then joins that against a `string_agg` of every abstract.

Neither step can be done a file at a time, which is why the numbers are an order
of magnitude above the loader's.

**Observed on full JSON exports of the whole corpus (`--shards 16`):**

| Run | Documents | Peak RSS | Wall time |
| --- | --- | --- | --- |
| Earlier | — | 199.6 GiB | "a few hours" (before progress logging; never timed) |
| 2026-07-30 | 40,901,984 | 201.1 GiB | 23m 13s, ≈30k documents/s |
| 2026-08-05 | 40,923,261 | 201.0 GiB | 18m 06s, ≈38k documents/s |

Those three runs all predate the `COPY`-based writer. **They are the numbers to
beat, not the numbers to request** — see "What changed" below; the first run of
the new export should be sized from the table above and will re-baseline it.

For the record, the 2026-08-05 run on `ht1` was submitted as `srun --mem=256G
--cpus-per-task=8 --time=02:00:00` (12:14:50 started, 12:32:56 finished) — kept
here as the provenance of the numbers above, not as a command to copy;
`04-export.sbatch` is what to run.

Treat **~200 GiB** as the working memory figure until a new one is measured — it
was stable across all three runs, and is why this needs a big node; do not copy
the loader's allocation. Time is the cheap dimension: the script's limit is
generous margin on 18 minutes.

### What changed (and what to record next run)

The export used to serialize every document in a single Python loop and sort the
whole corpus by PMID first. Both are gone: DuckDB now writes the NDJSON itself
(`COPY ... FORMAT JSON`, one file per writer thread) and the query has no
`ORDER BY`. On a 2M-document copy of the database, same machine, same query:

| | Wall time | Rate |
| --- | --- | --- |
| Python loop + `ORDER BY` | 112.9s | 17.7k docs/s |
| `COPY`, no sort | **35.5s** | **56.2k docs/s** |

Both wrote byte-identical record sets (2M rows, `EXCEPT` in both directions
returns nothing). Two things to read off the next cluster run, since neither can
be predicted from a laptop:

1. **Peak RSS.** The sort was the export's peak-memory event, so the `--mem` in
   `04-export.sbatch` is probably now over-provisioned — but *how* over is a
   measurement, and asking for less than the job needs is an OOM kill several
   minutes in.
2. **Wall time**, which sets whether the header's `--time` is still generous.

Tracked in #42; the header is the thing to edit once the numbers exist.

Because the whole export is one statement, there are no per-batch progress
lines any more; a heartbeat logs output size and current RSS once a minute
instead, and `-v` additionally enables DuckDB's own progress bar:

```
INFO pubmed2db.export: starting JSON export: 40923261 document(s) to at most 16 shard(s) in data/json
INFO pubmed2db.export: writing: 22.4 GiB across 16 shard(s) · 187 MiB/s · elapsed 2m 03s · RSS 143.1 GiB
INFO pubmed2db.export: exported 40923261 documents to 16 shard(s) in data/json in 5m 12s (peak RSS 88.0 GiB)
```

(The `writing:` and completion figures above are shapes, not measurements — the
new export has not been run on the cluster yet.)

Notes on the knobs:

- **`--shards N` is a maximum, and it *is* the write parallelism.** DuckDB
  writes one file per writer thread, so `--shards` caps the thread count for
  that statement; a run can produce fewer files than asked for, never more.
  Match it to `--cpus-per-task` rather than to the ingest's ideal file count —
  the two are the same number now, and `config.sh` derives `SHARDS` from
  `SLURM_CPUS_PER_TASK` so they stay matched without anyone remembering to.
  Setting it by hand still wins, but note what it costs: `export_json` runs
  `SET threads = $SHARDS` for the statement, so a `SHARDS` above the allocation
  oversubscribes the step that gets OOM-killed, and one set deliberately below
  it is also the only way `--shards` interacts with a group-level `--threads` —
  the `SET` overrides it for the duration of the COPY.
- **`--sample-size` is per shard, so `05-validate.sbatch` derives it from a
  target total.** `validate` samples that many records from *each* shard, and
  since the export writes one shard per writer thread the shard count follows
  `--cpus-per-task`. Left alone, halving the export's allocation would halve how
  many records the next `validate` compares against Entrez without either flag
  changing. The script divides `VALIDATE_SAMPLE_TOTAL` (240 by default, what the
  pre-2026 runs sampled as 15 × 16) by the shards actually on disk, so runs stay
  comparable across allocations; set it empty to pass nothing and take the CLI's
  own per-shard default. The report still says what a run checked —
  `(240 records sampled: 30/shard x 8 shards, seed 0)` — and that line, not the
  flag, is the one to read.
- **Shards are gzipped by default.** `--gzip` is on unless you pass
  `--no-gzip`; compression happens as each shard is written (no separate
  re-read pass) and costs CPU rather than memory. A full corpus lands at
  roughly 12 GiB rather than 52, which is the same reduction in what the next
  `validate` has to read back — and `validate` needs no flag to read it.
- **CPUs now help the writing too.** DuckDB parallelizes the snapshot, the
  `string_agg` and — since the `COPY` rewrite — the JSON serialization across
  cores, which is where the 3x came from. `--cpus-per-task 8` is what the
  measured runs used; more cores should now buy more throughput rather than
  idling behind a single Python thread. Those runs did *not* pass `--threads`
  and did not need to: DuckDB reads the cgroup, so the pool was already the 8
  CPUs asked for. Treat `--threads` as a way to run below your allocation on a
  busy node, not as something the export needs.
- **Parquet should be the lighter of the two — but is unmeasured.**
  `export_parquet` builds the same `_latest_snapshot`, then writes each table
  with a DuckDB `COPY ... TO`, so no full result set is pulled through Python.
  It has never been run against the full corpus, so that is reasoning rather
  than a number: give it the same allocation as the JSON export the first time
  and read the logged peak RSS.
- **Run `export` in a separate `srun` from `load`.** `update` deliberately does
  not chain into it, and sizing one job for both means paying the export's memory
  for the load's several hours.
- **Grab the memory before the run, not during.** Slurm will not grow `--mem`
  mid-job, so an under-requested export gets OOM-killed partway through with
  nothing to show for it.

## Running `validate`

**Short answer: `./slurm/submit.sh validate`, as its own job right after the
export, on a node that can reach the internet. Its allocation is a small
fraction of the export's — `validate` never touches `latest_article`.**

```bash
export NCBI_EMAIL=you@example.org     # NCBI_API_KEY too, if you have one
./slurm/submit.sh validate
```

`05-validate.sbatch` handles the manifest bookkeeping that
`drops_since_previous` needs: it writes this run's PMID set to
`data/manifests/pmids-<today>.txt.gz` and passes the newest *earlier* manifest as
`--previous-manifest`. The first run after adopting this reports `skip` — it has
nothing to compare against yet — and the run after it is the first that can
actually catch a silent drop (#32).

Two details are load-bearing, and both were bugs before they were features.
Today's manifest is excluded from the `--previous-manifest` search **by base
name**, not by comparing paths as strings: `MANIFEST_DIR` is overridable, and a
trailing slash on it makes `data/manifests//pmids-…` and `data/manifests/pmids-…`
the same file but never string-equal — enough to make a same-day re-run diff
against itself and report zero drops. And `validate` skips the manifest write
entirely when the run status is `fail`: a truncated or half-written export
yields a short PMID set, and adopting that as the baseline makes the *next* run
report the recovered corpus as "N added" while the real drops go unnoticed. A
`warn` run still writes one, because the usual warning is "Entrez was
unreachable", which says nothing about the PMID set the shard read built.

**The script also moves the report.** `validate` on its own writes
`validation_report.json` into the export directory; `05-validate.sbatch` passes
`--out data/manifests/validation_report-<today>.json` instead, so it lands beside
the manifest it belongs with. Same reasoning as the manifest: the next `export`
republishes the export directory, so a report left there would outlive the
corpus it describes while still looking current — and one fixed name would be
overwritten by the next run, leaving nothing to pass as `--previous-report`.
Running `validate` by hand keeps the default location.

**And feeds the newest earlier one back**, by the same rule the manifest uses:
archiving dated reports while never passing one back would leave the
`vs-previous` coverage check reporting `skip` on every run this script launches,
however many had piled up. Unlike the manifest, a *failed* run's report is still
eligible — `vs-previous` compares coverage percentages rather than absolute
counts, so a short export surfaces as a drift warning on the next run rather
than as a silent pass, and the dated report it came from is right there to
check.

**The API key is handed over as an environment variable, never as `--api-key`.**
`argv` is world-readable through `ps` on a shared node, and the CLI's option
already declares `envvar="NCBI_API_KEY"`, so the two are equivalent to `click`
and only one of them leaks. It is exported only when non-empty: exported empty,
`click` sees `""` — a key that is present but blank — instead of nothing at all.
`NCBI_EMAIL` stays an explicit flag; it is contact details, not a credential.

Set `VALIDATE_OFFLINE=1` for a node without egress, and `VALIDATE_FAIL_ON_WARN=1`
to make warnings non-zero too.

It reads the *export*, not the database: every line of every shard is
`json.loads`-ed once (gzipped shards are decompressed on the fly), and the only
thing held for the whole run is a Python set of every exported PMID. The DuckDB
connection is opened only if the database exists and has articles, and is used
for counts and a `deleted_pmid` sample, so the group-level `--memory-limit`
matters far less here than it does for `load`.

**Measured on full-corpus runs:**

| Run | Records | Shards | Wall time | Peak RSS |
| --- | --- | --- | --- | --- |
| earlier (no API key) | 40,901,984 | 16 | 10m 51s | 5.2 GiB |
| 2026-08-05 (API key) | 40,923,261 | 16, 52.0 GiB | **7m 57s** | **5.182 GiB** |

The script's allocation is roughly 3× that peak, which is the margin to keep if you pass
`--previous-manifest`: that manifest is read into a second PMID set of
comparable size. Both figures are in every report (`duration`,
`peak_rss_gib`) — size the next run from those, not from this note. (The
2026-08-05 run was submitted with `--mem=256G --time=06:00:00`, copied from the
export. It used 2% of that memory and 2% of the time; there is no reason to hold
a big node for this job.)

Nearly all of it is one thing: **7m 38s of that 7m 57s is the shard read.** Every
Entrez check together took 19 seconds. The read is a single-threaded
`json.loads` per line — 89k records/s, ~116 MiB/s — so if this job ever needs to
be faster, that pass is the only place worth touching (issue #13).

The log tells you the same while it runs. The start line confirms what was picked
up before any of the slow work (the key itself is never logged, here or in the
report), the shard read reports progress once a minute, and each phase after it
is announced — which is what distinguishes "still reading shards" from "hung on
an NCBI call", since only the first is local:

```
INFO pubmed2db.validate: starting validation: 16 shard(s) in data/json, 52.0 GiB · database available · online with an NCBI API key (10 req/s)
INFO pubmed2db.validate: reading shards (structure check)...
INFO pubmed2db.validate: progress: 21,667,845 record(s), shard 9/16, 52.2% of 52.0 GiB read · elapsed 4m 00s · RSS 1.7 GiB · ~3m 39s remaining
INFO pubmed2db.validate: read 40,923,261 record(s) in 7m 38s (peak RSS 4.3 GiB)
INFO pubmed2db.validate: checking coverage...
INFO pubmed2db.validate: validation finished in 7m 57s (peak RSS 5.2 GiB)
```

(That run's RSS climbed 0.5 → 2.2 GiB while the peak reached 4.3: the gap is the
PMID set reallocating as it doubles, not a second copy of anything.)

(Progress is measured in bytes of shard consumed, not records: the record count
is what that pass is computing. So the percentage tracks the compressed size on
disk, and it works for a single-shard export as well as sixteen.)

Notes on running it under Slurm specifically:

- **The online checks need outbound HTTPS to `eutils.ncbi.nlm.nih.gov`.** If the
  compute node has no egress, the coverage/field/deletion checks fail rather than
  quietly skipping. Run `--offline` on the node (structure + DB checks only, and
  a truthful `skipped_checks` list), then re-run online where there is egress.
- **Set `NCBI_EMAIL`, and `NCBI_API_KEY` if you have one.** Requests are
  self-throttled to 3/s without a key, 10/s with one; the sample is small
  (`--sample-size` is **per shard**, so a 16-shard export at the default samples
  240 records) so this is minutes, not hours, but a shared cluster
  IP is exactly the case NCBI blocks for anonymous hammering.
- **Exit status is the gate**: `0` pass, `1` errors, `2` for warnings under
  `--fail-on-warn`. A batch script with `set -e` therefore stops on a bad export
  instead of publishing it.
- **Keep the manifest between runs, and date-stamp it outside the export
  directory.** `--manifest` writes a sorted gzipped PMID list; passing it to the
  next run as `--previous-manifest` (along with `--previous-report`) is what
  catches an export that has the same record count but silently lost records.
  Write it to `data/manifests/pmids-<date>.txt.gz`, **not** into `data/json/`,
  for two reasons. One name for every run means the flags collide — the obvious
  next invocation passes the same path as both `--previous-manifest` and
  `--manifest`, so the run overwrites the very file it just diffed against, and
  you can never compare run N against run N+2. And a manifest inside the export
  directory describes data that gets republished under it: the next `export`
  rewrites the shards in place, leaving a manifest that still looks current but
  now describes the previous corpus. (It does *survive* — the export's stale
  sweep only globs `pubmed_metadata_*.ndjson*` — which is the trap, not the
  reassurance.) `validate` creates the parent directory itself, so the path
  needs no setup.

## DuckDB tuning: `--threads`, `--memory-limit` and `--temp-dir`

All three are **group-level** options, so they go before the subcommand, and all
three also read an environment variable — usually the easier form in a batch
script:

```bash
# No PUBMED2DB_THREADS here on purpose: DuckDB already takes the pool size from
# --cpus-per-task, so exporting SLURM_CPUS_PER_TASK by hand sets it to what it
# would have been anyway. See below.
export PUBMED2DB_DUCKDB_MEMORY_LIMIT="$EXPORT_MEMORY_LIMIT"   # see slurm/config.sh
export PUBMED2DB_DUCKDB_TEMP_DIR=/local/scratch/duckdb_tmp

# Equivalently, explicit flags (note: before the subcommand):
uv run pubmed2db --memory-limit "$EXPORT_MEMORY_LIMIT" \
  --temp-dir /local/scratch/duckdb_tmp export ...
```

The sbatch scripts already do this — `EXPORT_MEMORY_LIMIT`, `LOAD_MEMORY_LIMIT`
and `DUCKDB_TEMP_DIR` in `slurm/config.sh` are where to change it. The forms
above are for running a step by hand.

**DuckDB reads your allocation, not the node.** Both of its sized defaults come
from the Slurm cgroup, measured on duckdb 1.5.4: `threads` from the CPU quota
(2 under `--cpus-per-task=2`, on a node whose `os.cpu_count()` is 64) and
`memory_limit` from the memory quota (~76% of `--mem`). Neither flag below is
rescuing DuckDB from a node-sized default — that was this repo's belief twice
over, and both halves of it were wrong (#36, #38).

**`--threads`** caps DuckDB's thread pool, and on Slurm you almost never need
it: the pool is already `--cpus-per-task`. Note DuckDB is reading the cgroup's
CPU quota, not a CPU affinity mask — affinity in the measured run was the full
64. Reach for `--threads` to run *below* your allocation (leaving a busy node
headroom), not to stop an oversubscription that does not happen.

**`--memory-limit`** caps the buffer pool. Left alone DuckDB takes ~76% of
`--mem`, which is the right shape but leaves only the remaining quarter for
lxml, the parsed records and the Arrow batch — all of which count against the
same `--mem`. Set it a comfortable margin below the allocation to widen that
headroom; `LOAD_MEMORY_LIMIT` and `EXPORT_MEMORY_LIMIT` in `slurm/config.sh`
carry the current values. Both are starting points chosen to leave headroom
rather than measured optima (#37). Setting it *too* low is not free
either — DuckDB will spill to `--temp-dir` instead of caching, which shows up as
a collapsed s/file rate.

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
