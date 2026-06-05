# amd-perf-toolkit-public — CLAUDE.md

Project context for AI agents. Read this before touching any file in this repo.

## What This Project Is

Workload-agnostic AMD CPU performance analysis toolkit. Wraps any command and
produces pipeline utilization, CCD topology, cache, and branch prediction metrics
using Linux `perf stat`. No PerfSpect installation required.

Target hardware: AMD Zen4 (EPYC Genoa) and Zen5 (EPYC Turin) — bare-metal and
EC2 (M7A / M8A instances). Primary test machine: AMD EPYC 9684X (96-core, 12 CCDs).

## Scripts — What Each File Does

| File | Language | Purpose |
|------|----------|---------|
| `amd_pipeline_metrics.sh` | Bash | Terminal report: pipeline, CCD topology, backend, branch, L2, freq, utilization |
| `amd_perf_html_report.py` | Python | PerfSpect-style self-contained HTML with Chart.js |
| `amd_perf_excel_report.py` | Python | Netflix PerfSpect benchmark profile format (openpyxl) |
| `amd_cpu_placement.py` | Python | Core placement + CCD chiplet monitor (standalone or imported as module) |

## How to Run

```bash
# Terminal report — any workload
./amd_pipeline_metrics.sh "openssl speed -elapsed aes-256-cbc"
./amd_pipeline_metrics.sh "python3 my_script.py"

# HTML report
python3 amd_perf_html_report.py "openssl speed md5" report.html

# CCD placement only
python3 amd_cpu_placement.py -- openssl speed -elapsed aes-256-cbc
python3 amd_cpu_placement.py --json -- ./my_benchmark
python3 amd_cpu_placement.py --pid 12345
```

## AMD Hardware — Critical Facts

- **Dispatch model**: 6 slots per cycle (NOT Intel's 4-wide TopDown model)
- **L2 cache**: 1 MB per core (Zen4/5) — much larger than Intel (256–512 KB)
- **CCD**: Core Complex Die — 8 cores per CCD, each CCD has its own L3 cache
  - Standard Genoa (9004 series): 32 MB L3 per CCD
  - Genoa X (9004X series, e.g. 9684X): 96 MB L3 per CCD (32 MB base + 64 MB 3D V-Cache stacked)
  - `lscpu` reports full 96 MB; `lstopo`/hwloc may only see 32 MB base — use `--l3-per-ccd 96` flag
- **EPYC 9684X**: 96 cores = 12 CCDs × 8 cores; cross-CCD = separate L3 domains → ~100 ns latency
- **Topology sources**: `lstopo --of xml` (preferred) → sysfs `die_id` (fallback)

## Confirmed Working perf Events (Zen4/Zen5 bare-metal)

```
# Pipeline (PipelineL1 group)
de_no_dispatch_per_slot.no_ops_from_frontend   # frontend stall slots
de_no_dispatch_per_slot.backend_stalls          # backend stall slots
de_src_op_disp.all                              # total dispatched ops
ex_ret_ops                                      # retired ops
ls_not_halted_cyc                               # non-halted cycles

# Backend breakdown
ex_no_retire.load_not_complete                  # load stalls (memory-bound)
ex_no_retire.not_complete                       # total non-retire events

# Branch prediction
ex_ret_brn_misp                                 # mispredictions
ex_ret_brn                                      # total branches retired
cpu-cycles
instructions

# L2 cache
l2_cache_req_stat.dc_hit_in_l2                  # L2 hits from L1D misses
l2_cache_req_stat.ls_rd_blk_c                   # L2 misses from L1D (→ L3/DRAM)
l2_cache_req_stat.ic_fill_miss                  # instruction cache L2 misses
l2_cache_req_stat.ic_hit_in_l2                  # instruction cache L2 hits

# Software events (always work)
task-clock                                      # CPU time in ms; metric-value = CPUs utilized
```

## Events That Do NOT Work on This System

```
l3_lookup_state.*    # L3 lookup state — not supported on user's EPYC 9684X
```

Do not add these back. They were tested and confirmed broken.

## Key Metric Formulas

```
Total Slots          = ls_not_halted_cyc × 6
Frontend Bound %     = (frontend_stalls  / Total_Slots) × 100
Backend Bound %      = (backend_stalls   / Total_Slots) × 100
Bad Speculation %    = ((dispatched - retired) / Total_Slots) × 100
Retiring %           = (retired / Total_Slots) × 100

Backend Memory %     = Backend_Bound% × (load_not_complete / not_complete)
Backend CPU %        = Backend_Bound% × (1 − load_not_complete / not_complete)

Branch Misp Rate %   = (ex_ret_brn_misp / ex_ret_brn) × 100
IPC                  = instructions / cpu-cycles

Effective Freq (GHz) = cpu-cycles / (task-clock_ms × 1e6)
CPU Util %           = (CPUs_utilized / total_cores) × 100
  where CPUs_utilized = task-clock metric-value field from perf -j output

L2 DC Hit Rate %     = dc_hit_in_l2 / (dc_hit_in_l2 + ls_rd_blk_c) × 100
```

## Architecture Rules — Do Not Break These

- Use `perf stat -j` (JSON mode) for all event collection — reliable Python parsing
- Parse metric-value field from task-clock JSON line to get CPUs utilized float
- Use symbolic event names only — NOT raw hex (e.g. never `r08:64`)
- Use AMD's 6-slot dispatch model — never Intel's 4-wide assumption
- Effective frequency must come from perf events, not lscpu (lscpu gives static base clock)
- `amd_cpu_placement.py` must be importable as a module (used by the HTML report)

## perf JSON Parsing Pattern

```python
# perf stat -j emits one JSON object per line on stderr, redirected to stdout via 2>&1
obj = json.loads(line)
event = obj.get('event', '').strip()
val   = float(obj.get('counter-value', '0').replace(',', ''))
# Special: task-clock also emits a metric-value ("CPUs utilized")
mval  = obj.get('metric-value', '')   # store as event + "__metric"
```

## Reference Files in This Repo

| File | Use for |
|------|---------|
| `perfspect_genoa_metrics.json` | All Genoa metric formulas from PerfSpect — reference before adding new metrics |
| `AMD_OFFICIAL_PMC_EVENTS.md` | Official AMD Table 26 event codes (Family 19h) |
| `AMD_PMC_REGISTER_REFERENCE.md` | PMC register format |
| `amd_metric_groups.yaml` | PerfSpect metric group structure |

## Output Formats

- **Terminal**: `printf` with fixed-width columns, unicode separators, color via ANSI codes
- **HTML**: PureCSS + Chart.js from CDN, self-contained single file, sidebar navigation
- **Excel**: openpyxl, Netflix PerfSpect format (metric | System A | System B | % delta)

## CPU Placement Notes

`amd_cpu_placement.py` distinguishes:
- `peak_parallel_cpus` = max CPUs active in one 50 ms poll = true thread parallelism
- `unique_cores_seen` = all cores ever touched including OS context-switch migrations

A single-threaded workload will show `peak=1` even if `unique_cores_seen=4` (OS migrated
the thread across cores). Always report both — they mean different things.
