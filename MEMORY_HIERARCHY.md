# Memory-hierarchy counters in `amd_topdown.py`

This document describes the always-on memory-hierarchy block added to the
top-down profiler, why it exists, and how to read it alongside the TMA funnel.

## Why this exists

The TMA (Top-down Microarchitecture Analysis) funnel tells you *which pipeline
slot* is the bottleneck — front-end, back-end, bad speculation, or retiring. For
DRAM-latency-bound workloads (random pointer chases, big-data "chasers", graph
walks) the funnel collapses to a flat ~99% `Backend_Bound → Memory_Bound` and
**stops being informative**. Two runs that differ enormously in real behavior —
for example THP off vs. THP on — can show an essentially identical funnel.

The memory-hierarchy block fixes that blind spot. It is measured on **every**
collect, surfaced in both the single-run view and the compare view, and is built
**only** from AMD Zen 4 perf metricgroups and the shipped event JSON
(`l1_dcache`, `l2_cache`, `tlb`, best-effort `l3_cache`). No metric is
fabricated or estimated.

## What it reports

| Line | Source | Meaning |
|---|---|---|
| `L1D fills (misses)` | `l1_dcache` | L1 data-cache fills (i.e. L1 misses) |
| `├── from DRAM` | `l1_dcache` | share of those fills served from DRAM |
| `L2 accesses` / `L2 hit rate (all\|data)` | `l2_cache` | L2 access stream and hit composition |
| `L3 hit rate` | `l3_cache` (best-effort) | `n/a` when the uncore group errors |
| `dTLB L1 misses` / `page-table walks` | `tlb` | data-TLB pressure and walk volume |
| `TLB reloads 4K / 2M / 1G` | `tlb` | page-size mix on reload |
| `huge-page reload share` | `tlb` | fraction of reloads landing on huge pages |
| `IPC (instructions/cycle)` | computed, stored | instruction throughput |

## How to read it (THP example)

On a 2 GiB single-thread random pointer chase pinned to one CCD, comparing
THP=never (A) against THP=always (B):

```
  Useful work (Retiring)                 0.2%      0.2%   +0.0%
  IPC (instructions/cycle)             0.013     0.015    ×1.15

  Memory Hierarchy (AMD perf counts)
  L1D fills (misses)                155.31M       92.20M   ÷2  (B fewer)
  ├── from DRAM                      73.24M       86.56M   ×1.2  (B more)
  dTLB page-table walks              72.78M       49.28K   ÷1,477  (B fewer)
  TLB reloads 2M (huge)               9.60K       82.91M   ×8,632.7
  L2 hit rate (data)                   9.4%         1.7%   -7.7pp
  dTLB walk rate                      99.6%         0.1%   -99.5pp
  huge-page reload share               9.3%        99.8%   +90.5pp
```

Reading order:

1. **Funnel is flat** — both runs are ~99% Memory_Bound. THP does not move the
   pipeline bottleneck because the workload is DRAM-latency-bound by
   construction. The funnel alone would tell you "nothing changed."
2. **IPC nudges ×1.15** — small but real throughput gain.
3. **The memory walls show the actual win.** dTLB page-table walks collapse
   ÷1,477 (72.78M → 49.28K) and reloads shift almost entirely to huge pages
   (9.3% → 99.8%). THP eliminated the page-walk wall; that is invisible in the
   funnel and is the whole point of the feature.

### A subtle composition effect

Note `L2 hit rate (data)` *drops* under THP (9.4% → 1.7%) even though caching did
not get worse. THP removes ~72.78M well-hitting page-table-walk PTE accesses
(L1-miss-but-L2-hit) from the L2 access stream. Those accesses were inflating the
measured L2 hit rate; removing them lowers the *rate* while the data-side miss
behavior is unchanged. Confirmed independently by the L1D-from-DRAM share rising
47.2% → 93.9%. Always read the rate together with the access *volume*, not alone.

## How to run it

Collect a run (memory-hierarchy counters are gathered automatically).
`collect` attaches to an already-running process — start the workload first,
then point `collect` at it by name with `--process`:

```bash
export TOPDOWN_DB_PATH=$PWD/data.db
taskset -c 0-7 ./your_microbench &          # start the workload
python3 amd_topdown.py collect \
    --process your_microbench --duration 30 \
    --label thp=never --label workload=mb2g_ccd1_mem
```

Each `collect` prints `✓ Saved run <id>` — that 12-char hex is the run ID you
pass to `compare`.

Single-run unified panel (funnel + IPC + memory walls):

```bash
TOPDOWN_DB_PATH=$PWD/data.db python3 amd_topdown.py \
    query --label thp=never --label workload=mb2g_ccd1_mem --funnel
```

Side-by-side compare of two run IDs:

```bash
TOPDOWN_DB_PATH=$PWD/data.db python3 amd_topdown.py compare <run_a_id> <run_b_id>
```

## Notes and gotchas

- **One bad event aborts the whole `-M` list.** When probing perf metricgroups,
  test each group individually against a real workload/PID — an idle `sleep`
  reports `<not counted>` falsely. `l3_cache` genuinely errors on this part
  (uncore L3/XI event syntax), 