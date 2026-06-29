# Running pubmed2db on Slurm

Notes for running the loader on the shared cluster (the same one Babel uses; see
[Babel's slurm/README](https://github.com/NCATSTranslator/Babel/blob/main/slurm/README.md)).

## TL;DR

```bash
# Don't run on the login node. Request a job with a sane memory cap and time limit:
srun --mem=16G --time=06:00:00 \
  bash -c 'uv --cache-dir ../../uv-cache run pubmed2db load'
```

`download → journals → load` can each be a separate `srun`, or use
`pubmed2db update` to do all three. Pass `--cache-dir` to `uv` so the package
cache lands on a writable path (as in the command above).

## How much memory? (`--mem`)

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

## How long? (`--time`)

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

## DuckDB temp / spill directory

DuckDB spills to disk when a query exceeds its memory budget. The loader inserts
file-by-file so it should never need to, but if you run large `export` /
`latest_article` queries and hit memory pressure, point DuckDB's temp dir at fast
local scratch rather than the database directory:

```sql
PRAGMA temp_directory='/local/scratch/duckdb_tmp';
```

(Babel exposes this as `BABEL_DUCKDB_TEMP_DIR`; pubmed2db has no such env var yet
— set the pragma manually if needed, or open an issue.)

## Performance note

The load used to take ~20 min/file because rows were inserted one at a time via
`executemany` (~2.5k rows/s). They are now batched into Arrow tables and inserted
columnar (~200k+ rows/s), a ~25–90× speedup on the insert step. To re-measure on
any file:

```bash
uv run python scripts/benchmark_load.py data/pubmed/baseline/pubmed26nNNNN.xml.gz
```
