# amd-perf-toolkit-public

Workload-agnostic AMD EPYC performance analysis toolkit — pipeline utilization, CCD topology, L2 cache, branch prediction, effective boost frequency, and live cloud pricing. No PerfSpect installation required.

Targets AMD Zen4 (EPYC Genoa) and Zen5 (EPYC Turin) on bare-metal and cloud (AWS M7A/M8A, GCP C4D/N2D, Azure ECads).

---

## Scripts

| Script | Output | Purpose |
|--------|--------|---------|
| `amd_topdown.py` | Terminal / SQLite | Top-down (TMA) profiler with always-on memory-hierarchy counters — collect / list / query / compare runs |
| `amd_pipeline_metrics.sh` | Terminal | Human-readable pipeline + CCD report |
| `amd_perf_html_report.py` | HTML | PerfSpect-style report with Chart.js visualizations |
| `amd_perf_excel_report.py` | Excel (.xlsx) | Netflix PerfSpect benchmark profile format |
| `amd_cpu_placement.py` | Terminal / JSON | CPU core placement and CCD topology monitor |
| `cloud_context.py` | Terminal / JSON | PPL-aware cloud context (CSP, PPL limit, Feff ratio) |
| `chip_compare.py` | Terminal / JSON | Live cloud instance pricing via Vantage Instances API |

---

## Quick Start

### Top-down profiler with memory-hierarchy counters (`amd_topdown.py`)

`amd_topdown.py` runs the AMD 6-slot Top-down Microarchitecture Analysis (TMA)
funnel **and** an always-on memory-hierarchy block (L1D / L2 / dTLB, best-effort
L3) on every collect. Runs are stored in a SQLite database so you can list them,
view a single funnel, or compare any two side by side. See `MEMORY_HIERARCHY.md`
for what each counter means and how to read it.

| Subcommand | What it does |
|---|---|
| `collect` | Attach to a running process (or system-wide) for N seconds, store one run, print its run ID |
| `list` | List stored runs (run ID, label, timestamp) |
| `query` | Show the funnel + memory-hierarchy panel for one run (select by `--label`) |
| `compare` | Show two runs side by side (`compare <run_a_id> <run_b_id>`) |

The database location is set with `TOPDOWN_DB_PATH` (defaults to a temp path).
Use the same value for every command so they share one store.

#### How `collect` works

`collect` does **not** launch your workload — it attaches to one that is already
running. Start your benchmark in another shell, then point `collect` at it by
process name with `--process` (it resolves the PID for you); if the name is not
found it falls back to system-wide collection. Each `collect` prints a line like:

```
  ✓ Saved run cc1cb38067c0  (30.2s)  Labels: {'thp': 'never', ...}
```

That 12-character hex string is the **run ID** — that is where IDs like
`cc1cb38067c0` and `0d63019dd85d` come from. You pass two of them to `compare`.
You can always re-list them later with `list`.

#### Worked example — THP off vs THP on (A/B)

This reproduces the comparison in `MEMORY_HIERARCHY.md`. The workload is a 2 GiB
single-thread random pointer chase pinned to one CCD (cores 0–7). Run A is with
transparent huge pages disabled, run B with them enabled.

```bash
export TOPDOWN_DB_PATH=$PWD/data.db

# ---- Run A: THP = never ---------------------------------------------------
sudo sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled'
taskset -c 0-7 ./your_microbench &          # start workload in background
python3 amd_topdown.py collect \
    --process your_microbench --duration 30 \
    --label thp=never --label workload=mb2g_ccd1_mem
#   ✓ Saved run cc1cb38067c0   <-- this is run A's ID (yours will differ)
kill %1

# ---- Run B: THP = always --------------------------------------------------
sudo sh -c 'echo always > /sys/kernel/mm/transparent_hugepage/enabled'
taskset -c 0-7 ./your_microbench &
python3 amd_topdown.py collect \
    --process your_microbench --duration 30 \
    --label thp=always --label workload=mb2g_ccd1_mem
#   ✓ Saved run 0d63019dd85d   <-- this is run B's ID (yours will differ)
kill %1

# ---- Inspect ---------------------------------------------------------------
python3 amd_topdown.py list                 # re-list both run IDs any time

# Single-run funnel + memory walls (select by label):
python3 amd_topdown.py query \
    --label thp=never --label workload=mb2g_ccd1_mem --funnel

# Side-by-side compare — paste the two run IDs printed above:
python3 amd_topdown.py compare cc1cb38067c0 0d63019dd85d
```

The TMA funnel stays flat (~99% `Backend_Bound → Memory_Bound`) across both runs
because the workload is DRAM-latency-bound by construction. The win shows up only
in the memory-hierarchy block: dTLB page-table walks collapse and reloads shift
almost entirely onto huge pages. That blind-spot is exactly why the
memory-hierarchy counters exist.

### Profiling a workload (bare-metal or cloud VM)

```bash
# Terminal report — wrap any workload
./amd_pipeline_metrics.sh "openssl speed -elapsed aes-256-cbc"
./amd_pipeline_metrics.sh "dd if=/dev/zero of=/dev/null bs=1M count=1000"

# Emulate cloud PPL limits in report output
./amd_pipeline_metrics.sh --emulate aws m7a.48xlarge "openssl speed aes-256-cbc"
./amd_pipeline_metrics.sh --emulate gcp c4d-standard-96 "stream"

# HTML report
python3 amd_perf_html_report.py "openssl speed md5" report.html

# CCD topology monitor
python3 amd_cpu_placement.py -- openssl speed -elapsed aes-256-cbc
python3 amd_cpu_placement.py --json -- ./my_benchmark
python3 amd_cpu_placement.py --pid 12345
```

### Live cloud pricing (Vantage Instances API)

```bash
# Single instance — on-demand, spot, reserved pricing
python3 chip_compare.py --instance m7a.large --region us-east-1
python3 chip_compare.py --instance m8a.48xlarge --region us-east-1
python3 chip_compare.py --gcp --instance n2d-standard-96 --region us-central1
python3 chip_compare.py --azure --instance Standard_D96as_v5 --region eastus

# Side-by-side comparison (sorted by on-demand price)
python3 chip_compare.py --compare m7a.48xlarge,m6i.48xlarge,c7a.48xlarge --region us-east-1
python3 chip_compare.py --compare m8a.48xlarge,m7a.48xlarge,r7a.48xlarge --region us-east-2

# All instances in an AMD family
python3 chip_compare.py --amd-family m7a --region us-east-1
python3 chip_compare.py --amd-family c7a --region us-west-2

# JSON output for scripting
python3 chip_compare.py --instance m7a.large --region us-east-1 --json
python3 chip_compare.py --compare m7a.large,m6i.large --region us-east-1 --json

# Import as module
from chip_compare import get_price, get_instance_details, compare_instances
price = get_price("aws", "m7a.large", "us-east-1")
print(price["on_demand"], price["spot_avg"])
```

### Cloud context (PPL-aware frequency correction)

```bash
# Detect current cloud environment
python3 cloud_context.py

# Emulate a cloud instance context
python3 cloud_context.py --emulate aws m7a.48xlarge
python3 cloud_context.py --emulate gcp n2d-standard-96
python3 cloud_context.py --emulate azure Standard_D96as_v5
```

---

## What It Measures

### Pipeline Utilization (AMD dispatch slot model — 6 slots/cycle)

| Metric | Description |
|--------|-------------|
| Frontend Bound % | CPU waiting for instructions (fetch/decode stalls) |
| Backend Bound % | CPU waiting on execution units or memory |
| Bad Speculation % | Slots wasted on ops that don't retire (mispredictions) |
| Retiring % | Slots doing useful work — higher is better |

Backend is further subdivided into **Memory Bound** (load stalls) and **CPU Bound** (execution unit stalls).

### CPU Frequency & Utilization

Effective boost frequency derived from `cpu-cycles / task-clock` — the actual running frequency during the workload, not the static base clock from `lscpu`.

### CCD Topology & Core Placement

Tracks which CPU cores the workload uses during execution, maps them to CCD chiplets, and detects cross-CCD execution:

- **Peak parallel CPUs** — true thread-level parallelism
- **Unique cores seen** — includes OS context-switch migrations
- **Cross-CCD detection** — each AMD EPYC CCD has 8 cores sharing one L3; cross-CCD introduces ~100 ns cache-to-cache latency

### Cloud PPL Correction (cloud_context.py)

Package Power Limit reduces effective frequency on cloud instances:

| CSP | PPL | Feff ratio |
|-----|-----|------------|
| AWS M7A / M8A | 320 W | 0.75 (25% below bare-metal boost) |
| GCP C4D | 400 W | 0.85 |
| Azure HBv4 | 400 W | 0.85 |
| Oracle OCI | 450 W | 0.88 |
| Bare-metal | — | 0.92 |

---

## Confirmed Working Events (AMD Zen4 / Zen5 bare-metal)

```
# Pipeline
de_no_dispatch_per_slot.no_ops_from_frontend
de_no_dispatch_per_slot.backend_stalls
de_src_op_disp.all / ex_ret_ops / ls_not_halted_cyc

# Backend breakdown
ex_no_retire.load_not_complete / ex_no_retire.not_complete

# Branch & IPC
ex_ret_brn_misp / ex_ret_brn / cpu-cycles / instructions

# L2 cache
l2_cache_req_stat.dc_hit_in_l2 / l2_cache_req_stat.ls_rd_blk_c
l2_cache_req_stat.ic_fill_miss / l2_cache_req_stat.ic_hit_in_l2

# Gap events (verified — add to next perf stat pass)
bp_l1_tlb_miss_l2_tlb_miss.all   # L2 ITLB miss PTI
ls_l1_d_tlb_miss.all             # L2 DTLB miss PTI
l2_request_g1.cacheable_ic_read  # instruction cache pressure
op_cache_hit_miss.op_cache_miss  # op-cache efficiency
ls_bad_status2.stli_other        # store-to-load interlock
```

> `l3_lookup_state.*` events are not supported on all systems — do not use.

---

## Requirements

- Linux with `perf` installed (`perf stat -j` support)
- Python 3.10+
- For Excel output: `pip install openpyxl`
- For CCD topology: `lstopo` (hwloc, optional) or Zen3+ sysfs
- For PPTX generation: `pip install python-pptx`
- For cloud pricing: Python stdlib only (chip_compare.py uses urllib)

### perf_event_paranoid — required before first run

`perf stat` needs access to hardware performance counters. The default Linux
setting on many distros (including Ubuntu 22.04+) blocks this. Check your
current value and set it to `-1` for full bare-metal PMC access:

```bash
# Check current value
cat /proc/sys/kernel/perf_event_paranoid

# Fix for current session
sudo sysctl kernel.perf_event_paranoid=-1

# Fix permanently (survives reboot)
echo 'ker