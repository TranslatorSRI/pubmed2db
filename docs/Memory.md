# Memory on the cluster

How pubmed2db's memory behaves under Slurm on the cluster, what has been
measured,
and how to tell one memory failure from another. It applies to every step.
What each step should ask for is in [`slurm/README.md`](../slurm/README.md),
with its dated run tables. The `#SBATCH` headers in `slurm/` are the only place
an allocation is written down.

- [What DuckDB takes from the allocation](#what-duckdb-takes-from-the-allocation)
- [Reading the memory figures](#reading-the-memory-figures)
- [Three kinds of memory failure](#three-kinds-of-memory-failure)
- [A refused allocation: `(bad allocation)`](#a-refused-allocation-bad-allocation)

## What DuckDB takes from the allocation

**DuckDB reads the Slurm cgroup, and both of its sized defaults do.** We
guessed otherwise twice and were wrong twice: both claims reached the docs, the
`--help` text and an issue before anyone ran the one-line probe in #36/#38 that
disproves them. Measured on duckdb 1.5.4, `memory_limit` is ~76% of `--mem`
(6.1 GiB under `--mem=8G`, 47.3 GiB under `--mem=62G`; ~80% of physical RAM
off a cluster), and `threads` is `--cpus-per-task` (2 under
`--cpus-per-task=2` on a 64-core node, read from the cgroup's CPU quota, not
an affinity mask, which showed all 64). Re-taken on 1.5.5 on the
cluster (2026-10-21): 6.1 GiB and 2 under `--mem=8G --cpus-per-task=2`, unchanged.

These are measurements, so re-take them after a DuckDB upgrade rather than
assume they carried over. Every Slurm step's log has a `DuckDB <version>:
memory_limit=…, threads=…` line, and the download, journals and validate steps
pass no limit, so theirs are the cgroup-derived defaults
([`slurm/README.md` → "Monitoring memory and runtime"](../slurm/README.md#monitoring-memory-and-runtime));
compare them with that step's `--mem` and `--cpus-per-task`. Off the
pipeline, it is one line inside an allocation, and should print ~76% of
`--mem` and `--cpus-per-task`:

```bash
srun --mem=8G --cpus-per-task=2 uv run python -c "import duckdb; c = duckdb.connect(); print(c.execute(\"SELECT current_setting('memory_limit'), current_setting('threads')\").fetchone())"
```

**`memory_limit` governs only DuckDB's own buffer pool.** Everything else in
the process shares the same cgroup and counts against the same `--mem`: the
loader's parsed records and its Arrow batch, and, since #42, the
freed memory the export holds instead of returning (see
[below](#a-refused-allocation-bad-allocation)). A default that claims
three quarters of the allocation leaves the rest of the process a quarter. So
the buffer pool is not what overruns an allocation; what its limit does not
cover is.

`config.sh` sets a limit for the load and the export. Both are in DuckDB's
decimal units, so `48GB` is 44.7 GiB and `160GB` is 149.0 GiB, and the two buy
back very different amounts:

- **The load** (`LOAD_MEMORY_LIMIT=48GB` under `--mem=64G`) is only ~4 GiB
  below DuckDB's default there, ~48.6 GiB. The load fits because the loader's
  own footprint is small, not because of the cap: its measured peak is
  53.6 GiB, ~10 GiB under `--mem`. Don't lower `--mem` on the strength of the
  cap; the runs are in
  [`slurm/README.md` → "Running `load`: how much memory?"](../slurm/README.md#running-load-how-much-memory---mem).
- **The export** (`EXPORT_MEMORY_LIMIT=160GB` under `--mem=256G`) is ~46 GiB
  below its default of ~195 GiB, and since #42 the ~55 GiB it holds more than
  uses that up: its 205.1 GiB peak is above what the default alone would
  claim.

## Reading the memory figures

- **RSS and peak.** `util.peak_rss_gib` is `ru_maxrss`, a high-water mark that
  only ever rises, so a climbing per-file "peak" is not evidence of a leak;
  `current_rss_gib` is the one that can fall. The load and validate progress
  lines log both. How to read them over a load is
  [`slurm/README.md` → "Reading the logged numbers"](../slurm/README.md#reading-the-logged-numbers).
- **VSZ, the node's commit charge and the mapping count**
  (`util.memory_summary`) are what explain a `(bad allocation)`. Only the
  export's heartbeat and its out-of-memory line log them; load and validate
  log RSS alone (#82). If load or validate is ever refused an allocation,
  switch its progress line to `memory_summary` before reading anything into
  its RSS.
- **Each step's log opens with the limits that are not `--mem`**:
  `ulimit -v`, `vm.overcommit_memory` and its ratio, `vm.max_map_count`, and
  the node's `CommitLimit` and `Committed_AS` at job start (`config.sh`'s
  `log_memory_limits`).
- **The step log is the only record of a run's memory on the cluster**:
  `sacct` gathers none there
  ([`slurm/README.md` → "Monitoring memory and runtime"](../slurm/README.md#monitoring-memory-and-runtime)).

## Three kinds of memory failure

They look different in the log, and only one of them is about `--mem`:

- **DuckDB's own `memory_limit`** names the limit:
  `could not allocate block of size 256.0 KiB (149.0 GiB/149.0 GiB used)`.
- **The Slurm cgroup** (`--mem`) does not refuse anything; the kernel kills the
  process with SIGKILL, the job ends `OUT_OF_MEMORY`, and the log has no
  traceback.
- **`(bad allocation)`** means `malloc`/`mmap` itself returned nothing: the
  kernel refused the request, whatever DuckDB's limit and the cgroup allowed.

## A refused allocation: `(bad allocation)`

Two exports from the v1.2 branch failed with DuckDB's `Out of Memory Error:
Failed to allocate block of … bytes (bad allocation)`, and neither was
OOM-killed (#42):

| Job | Node | `--mem` | Peak RSS | At failure | Node commit, peak |
| --- | --- | --- | --- | --- | --- |
| 145739, 2026-10-22 | `compute-13-3` | 192G | 152.2 GiB | RSS 68.1 GiB, 6m 41s | not logged |
| 155199, 2026-11-09 | `largemem-6-1` | 256G | 154.3 GiB | RSS 67.9 GiB, VSZ 209.2 GiB, 6m 03s | 161.2 of 1,359.2 GiB |

Both ran with `EXPORT_MEMORY_LIMIT=160GB` and 8 threads, on nodes with
`vm.overcommit_memory=2` (ratio 90) and `ulimit -v` unlimited. Both failed
at the same point: RSS climbed to the buffer limit, DuckDB began to spill,
RSS fell by ~85 GiB, and the next allocation was refused. A failure that
repeats this exactly, on different nodes with different allocations, is
coming from inside the process, not from the neighbours. That rules out
the explanation first given (a node-wide `CommitLimit` shared with other
jobs): job 155199's node never charged more than an eighth of it.

**The cause, confirmed in #80: the process runs out of memory mappings.**
Past `vm.max_map_count` mappings (65,530 by default), `mmap` fails with
ENOMEM however much memory is free. When the OS does not overcommit, jemalloc
(which DuckDB uses on Linux) hands memory back by remapping it `PROT_NONE`
rather than leaving it mapped, and each remap inside a mapping splits it.
Job 155199's figures fit this. As RSS fell by 85 GiB, VSZ stayed at 209 GiB
and the node's commit charge fell by only 16 GiB, so the memory went back
as remapped regions rather than unmapped ones. Neither failed run logged its
mapping count; the export's heartbeat and its out-of-memory line log it now,
and the run that confirmed it is
[below](#confirmed-duckdbs-half-of-the-hold-alone-runs-out-of-mappings).

**Holding freed memory instead of returning it avoids the failure.**
`export._HOLD_ALLOCATOR` raises DuckDB's two allocator-flush thresholds out of
reach for the snapshot and the `COPY`. By default DuckDB flushes after any task
that peaked above 128 MiB and after any deallocation above 512 MiB.
`EXPORT_JE_MALLOC_CONF` in `config.sh` turns off jemalloc's own time-based
release (`dirty_decay_ms:-1, muzzy_decay_ms:-1`, passed as
`DUCKDB_JE_MALLOC_CONF`). Job 155264 ran with both, on `largemem-6-1`, the
node that refused job 155199 the same day, and finished in 15m 00s:

| Elapsed | Shards written | RSS | VSZ | Node commit | `maps` | Spill |
| --- | --- | --- | --- | --- | --- | --- |
| 3m 17s | 0.0 GiB | 139.4 GiB | 153.0 GiB | 148.3 GiB | 842 | 0.0 GiB |
| 5m 19s | 0.0 GiB | 193.5 GiB | 214.1 GiB | 202.1 GiB | 875 | 20.9 GiB |
| 6m 20s | 0.5 GiB | 204.0 GiB | 221.4 GiB | 213.4 GiB | 881 | 11.5 GiB |
| 13m 27s | 15.8 GiB | 204.0 GiB | 221.4 GiB | 213.4 GiB | 881 | 11.4 GiB |

Three things follow from it:

- **The held memory is outside DuckDB's `memory_limit`.** RSS plateaued at
  204 GiB against a 149 GiB limit; the ~55 GiB above it is freed memory
  jemalloc kept. That makes `--mem=256G` load-bearing while the hold is on:
  under 192G this run would have been OOM-killed. Lower `EXPORT_MEMORY_LIMIT`
  before lowering `--mem`, and expect the gap to stay.
- **With the hold on, `maps` stays flat.** It peaked at 881 of 65,530. On
  its own that was equally consistent with any cause that only bites when
  memory is handed back; the run below is what told them apart.

### Confirmed: DuckDB's half of the hold alone runs out of mappings

Job 165849 (2026-11-26, #80) exported the 2026nov25 database on
`largemem-6-1` with DuckDB's flush thresholds held
(`export._HOLD_ALLOCATOR`) and jemalloc's time-based release back on
(`EXPORT_JE_MALLOC_CONF=` set empty), with everything else as job 155264:

| Elapsed | RSS | VSZ | Node commit | `maps` | Spill |
| --- | --- | --- | --- | --- | --- |
| 1m 17s | 46.2 GiB | 55.8 GiB | 54.5 GiB | 8,523 | 0.0 GiB |
| 2m 17s | 78.0 GiB | 92.9 GiB | 86.8 GiB | 10,138 | 0.0 GiB |
| 3m 17s | 141.4 GiB | 153.1 GiB | 150.2 GiB | 10,328 | 0.0 GiB |
| 4m 17s | 152.7 GiB | 209.2 GiB | 161.3 GiB | 41,889 | 4.6 GiB |
| ~5m 10s, refused | 53.7 GiB | 213.2 GiB | 117.8 GiB | 55,469 | 11.7 GiB |

It failed with `Failed to allocate block of 263176 bytes (bad allocation)`,
peak RSS 155.6 GiB, the same shape as jobs 145739 and 155199. The mappings
quadrupled in the minute RSS reached the buffer limit and DuckDB began to
spill, and were still climbing at the refusal, while every other limit was
far off: a twelfth of the node's commit limit, RSS well under `--mem`,
`ulimit -v` unlimited. `maps` is sampled when the error is caught, after
RSS had fallen by ~100 GiB, so the count at the refusal itself is not
logged; 55,469 is a floor. An earlier attempt the same day (job 165821)
died of an unrelated read error at 2m 21s with `maps` on the same path
(7,693, then 10,002), against 746 and 782 at the same points of the
2026nov25 build's export, which ran with both knobs.

Three things follow:

- **`vm.max_map_count` is what refuses the allocation.** Raising it on the
  export's nodes (Elasticsearch's documented minimum, 262,144, is a common
  setting) is the fix; these figures are what to take to the cluster admins.
  Once it is raised, an export with the hold off would show whether the hold,
  and the ~55 GiB of `--mem` it costs, can go.
- **DuckDB's flush thresholds alone do not avoid it; jemalloc's decay
  setting is needed.** So `EXPORT_JE_MALLOC_CONF` keeps its default and
  `--mem=256G` stays, since the held memory is jemalloc's. Whether
  jemalloc's setting alone would do, without `_HOLD_ALLOCATOR`, is untested,
  and only decides whether that code can go.
- **The run without any hold that #80 planned was not needed.** It would have
  confirmed the same thing from further away.

If a later run fails anyway: RSS climbing past ~205 GiB until the job is
OOM-killed means jemalloc could not reuse what it held; a `(bad allocation)`
with `maps` near the limit is the cause above, back again; one with `maps`
far below it is something new. Record the figures here before guessing
again.

**The node did not change, so the database or the code did.** `largemem-6-1`
ran the 2026-08-21 and 2026-09-26 exports that succeeded (jobs 119057 and
138349, 256G, DuckDB 1.5.5, without the hold). It has been up since
2026-05-21, on the same kernel, and `vm.overcommit_memory = 2` has been in its
`/etc/sysctl.conf` since that day. So strict overcommit and `max_map_count`
were the same for the runs that succeeded. The 2026-09-26 run's RSS was still
144 GiB 9½ minutes in (the excerpt under
[`slurm/README.md` → "Running `export`"](../slurm/README.md#running-export)),
while both failed runs had fallen to ~68 GiB by
minute 6. Candidates for the difference: v1.2 builds a fresh database rather
than continuing one, and data model 1.0 reshaped the tables, though the
export query's structure is unchanged. Both successful runs' logs are gone.

Job 155199 also ran the snapshot and the `COPY` with `preserve_insertion_order`
off, as DuckDB's error message suggests (`export._UNORDERED` says why that is
free). It failed at the same point anyway, so that setting does not fix this.
What it saves on a run that finishes is still unmeasured.
