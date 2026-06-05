#!/usr/bin/env python3
"""
amd_cpu_placement.py — CPU placement and CCD topology monitor for AMD workloads

Tracks which CPU cores a workload executes on (including context-switch
migrations) and maps them to CCD (Core Complex Die) chiplets.

KEY DISTINCTION:
  "Cores seen"    = all unique cores ever touched (includes context-switch migrations).
                    A single-threaded process may touch 3-4 cores over its lifetime
                    because the OS scheduler migrates it between time slices.
  "Peak parallel" = maximum CPUs active simultaneously in a single poll sample.
                    This tells you the true thread-level parallelism:
                      peak=1  → single-threaded (even if cores_seen > 1)
                      peak=4  → 4 threads ran concurrently at some point

WHY CCD TOPOLOGY MATTERS (AMD EPYC):
  Each CCD (Core Complex Die) contains 8 cores (SMT-off) sharing one 32 MB L3.
  Cross-CCD execution means threads work across separate L3 domains:
    - Cache-to-cache latency: ~100+ ns (vs ~10 ns within a CCD)
    - Effective L3 per thread is smaller when spread across CCDs

Usage:
  python3 amd_cpu_placement.py [options] -- <command> [args...]
  python3 amd_cpu_placement.py --pid <pid> [--duration <seconds>]

Options:
  --pid <pid>         Monitor an already-running process
  --duration <sec>    Stop monitoring after N seconds (with --pid)
  --json              Print JSON to stdout instead of human-readable text
  --json-file <path>  Write JSON report to file (in addition to stdout)
  --quiet             Suppress all output except errors (use with --json-file)
  --l3-per-ccd <mb>   L3 cache per CCD in MB (default: 32, for EPYC 9684X)

Examples:
  python3 amd_cpu_placement.py -- openssl speed -elapsed aes-256-cbc
  python3 amd_cpu_placement.py -- ./my_benchmark --threads 4
  python3 amd_cpu_placement.py --pid 12345 --duration 30
  python3 amd_cpu_placement.py --json -- sleep 5
"""

import sys
import os
import glob
import json
import time
import threading
import subprocess
import argparse
import shutil
from collections import defaultdict

POLL_INTERVAL_S  = 0.05   # 50 ms polling
L3_PER_CCD_MB_DEFAULT = 32  # AMD EPYC 9684X: 32 MB per CCD


# ---------------------------------------------------------------------------
# Topology  (sysfs primary, lstopo optional enrichment)
# ---------------------------------------------------------------------------

def _lstopo_ccd_topology():
    """
    Try to build CCD topology using `lstopo --of xml` (hwloc).

    lstopo labels AMD chiplets as L3Cache objects or Die objects in its XML.
    We parse out CPU→Die mappings if available.

    Returns (cpu_to_ccd, ccd_to_cpus) or (None, None) if lstopo unavailable.
    """
    if not shutil.which("lstopo") and not shutil.which("lstopo-no-graphics"):
        return None, None

    exe = shutil.which("lstopo") or shutil.which("lstopo-no-graphics")
    try:
        result = subprocess.run(
            [exe, "--of", "xml", "--no-io"],
            capture_output=True, text=True, timeout=15
        )
        xml = result.stdout
    except Exception:
        return None, None

    # Parse XML with stdlib (no lxml required)
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
    except Exception:
        return None, None

    cpu_to_ccd  = {}
    ccd_to_cpus = defaultdict(list)
    ccd_counter = [0]  # mutable for closure

    def walk(node, current_ccd):
        """Recursively walk the hwloc XML tree."""
        obj_type = node.get("type", "")

        # AMD CCDs appear as "Die" or "L3Cache" in lstopo's topology tree.
        # Each L3Cache/Die node represents one CCD (one shared L3).
        # Assign a new CCD id when we descend into one.
        is_ccd_boundary = obj_type in ("Die", "L3Cache")
        if is_ccd_boundary:
            my_ccd = ccd_counter[0]
            ccd_counter[0] += 1
        else:
            my_ccd = current_ccd

        if obj_type == "PU":
            # Processing Unit — leaf CPU logical index
            try:
                pu_idx = int(node.get("os_index", -1))
                if pu_idx >= 0 and my_ccd >= 0:
                    cpu_to_ccd[pu_idx] = my_ccd
                    ccd_to_cpus[my_ccd].append(pu_idx)
            except ValueError:
                pass

        for child in node:
            walk(child, my_ccd)

    walk(root, -1)

    if not cpu_to_ccd:
        return None, None

    for ccd in ccd_to_cpus:
        ccd_to_cpus[ccd].sort()

    return cpu_to_ccd, dict(ccd_to_cpus)


def build_ccd_topology():
    """
    Build CPU→CCD topology map.

    Strategy:
      1. Try lstopo (hwloc) — gives accurate Die/L3Cache groupings,
         including AMD CCD chiplet boundaries. Best source.
      2. Fall back to sysfs die_id — simpler but reliable on Zen3+.

    Returns:
        cpu_to_ccd  : dict {cpu_id (int) -> ccd_id (int)}
        ccd_to_cpus : dict {ccd_id (int) -> sorted list of cpu_ids}
        source      : str  description of which method was used
    """
    # ── Try lstopo first ────────────────────────────────────────────────────
    cpu_to_ccd, ccd_to_cpus = _lstopo_ccd_topology()
    if cpu_to_ccd:
        return cpu_to_ccd, ccd_to_cpus, "lstopo (hwloc)"

    # ── Fall back to sysfs die_id ───────────────────────────────────────────
    cpu_to_ccd  = {}
    ccd_to_cpus = defaultdict(list)

    paths = glob.glob('/sys/devices/system/cpu/cpu[0-9]*/topology/die_id')
    for path in paths:
        try:
            cpu_id = int(path.split('/cpu')[2].split('/')[0])
            die_id = int(open(path).read().strip())
            cpu_to_ccd[cpu_id]  = die_id
            ccd_to_cpus[die_id].append(cpu_id)
        except (ValueError, IOError, IndexError):
            continue

    for die_id in ccd_to_cpus:
        ccd_to_cpus[die_id].sort()

    if cpu_to_ccd:
        return cpu_to_ccd, dict(ccd_to_cpus), "sysfs die_id"

    return {}, {}, "unavailable"


def fallback_topology():
    """Fallback when no topology source works: treat all CPUs as CCD 0."""
    n = os.cpu_count() or 1
    cpu_to_ccd  = {i: 0 for i in range(n)}
    ccd_to_cpus = {0: list(range(n))}
    return cpu_to_ccd, ccd_to_cpus, "fallback (all CPUs = CCD 0)"


# ---------------------------------------------------------------------------
# /proc polling
# ---------------------------------------------------------------------------

def _parse_stat(data):
    """
    Parse /proc/pid/stat into (ppid, cpu).
    comm field can contain spaces/parens — split after the LAST closing paren.
    Fields after ')': [0]=state [1]=ppid [2]=pgrp ... [36]=processor
    """
    cp = data.rfind(')')
    if cp == -1:
        return None, None
    fields = data[cp + 2:].split()
    if len(fields) < 37:
        return None, None
    try:
        return int(fields[1]), int(fields[36]), fields[0]  # ppid, cpu, state
    except (ValueError, IndexError):
        return None, None, None


def _read_proc_table():
    """
    Scan all /proc/[0-9]*/stat in one pass.
    Returns dict: pid → (ppid, cpu)
    """
    table = {}
    for stat_path in glob.glob('/proc/[0-9]*/stat'):
        try:
            pid_s = stat_path.split('/')[2]
            with open(stat_path) as f:
                data = f.read()
            ppid, cpu, state = _parse_stat(data)
            if ppid is not None:
                table[int(pid_s)] = (ppid, cpu, state)
        except Exception:
            pass
    return table


def get_descendant_cpus(root_pid):
    """
    Return (cpus, n_processes) for root_pid and ALL its descendants.

    Walks the full parent→child tree from a single /proc scan so that
    forked workloads (e.g. openssl -multi 4, nginx workers, MPI ranks)
    are fully visible — not just threads of the root process.

    Also reads /proc/<pid>/task/<tid>/stat for multi-threaded processes
    so that pthreads are captured even within child processes.

    Returns:
        cpus       : set of int CPU IDs currently in use
        n_processes: int  number of live descendant processes found
    """
    table = _read_proc_table()   # pid → (ppid, cpu)

    # BFS to collect all descendants of root_pid
    desc_pids = set()
    queue     = [root_pid]
    while queue:
        pid = queue.pop()
        if pid in desc_pids:
            continue
        desc_pids.add(pid)
        for child_pid, (ppid, _, _state) in table.items():
            if ppid == pid and child_pid not in desc_pids:
                queue.append(child_pid)

    # Gather CPUs from every descendant + their threads
    cpus      = set()
    n_procs   = 0
    for pid in desc_pids:
        if pid not in table:
            continue
        _, main_cpu, main_state = table[pid]
        n_procs += 1
        if main_state == "R":  # only count actively running processes
            cpus.add(main_cpu)
        # Threads (may be on different CPUs than the main thread)
        task_dir = f'/proc/{pid}/task'
        if os.path.isdir(task_dir):
            for tid_s in os.listdir(task_dir):
                try:
                    tid = int(tid_s)
                    if tid == pid:
                        continue          # already counted via table
                    with open(f'{task_dir}/{tid}/stat') as tf:
                        tdata = tf.read()
                    _, tcpu, tstate = _parse_stat(tdata)
                    if tcpu is not None and tstate == "R":
                        cpus.add(tcpu)
                except Exception:
                    pass

    return cpus, n_procs


# Keep old name as alias so any external callers still work
def get_thread_cpus(pid):
    cpus, _ = get_descendant_cpus(pid)
    return cpus


class PlacementMonitor:
    """
    Polls /proc/<pid>/task/*/stat at POLL_INTERVAL_S.

    Tracks:
      all_cpus_seen : union of all CPUs ever observed (includes migrations)
      peak_parallel : maximum CPUs active in a single poll (true parallelism)
      samples       : list of (timestamp, frozenset_of_cpus) for each poll
    """

    def __init__(self, pid):
        self.pid           = pid
        self.all_cpus_seen = set()
        self.peak_parallel = 0
        self.peak_procs    = 0           # max forked child processes seen at once
        self.samples       = []          # (t, frozenset)
        self._stop         = threading.Event()
        self._thread       = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.is_set():
            cpus, n_procs = get_descendant_cpus(self.pid)
            if cpus:
                self.all_cpus_seen.update(cpus)
                if len(cpus) > self.peak_parallel:
                    self.peak_parallel = len(cpus)
                if n_procs > self.peak_procs:
                    self.peak_procs = n_procs
                self.samples.append((time.time(), frozenset(cpus)))
            time.sleep(POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Run workload or monitor existing PID
# ---------------------------------------------------------------------------

def run_workload_monitored(cmd_list):
    """
    Launch `cmd_list` as a subprocess, monitor it, return results.

    Returns:
        monitor     : PlacementMonitor (stopped, results populated)
        returncode  : int
        elapsed_s   : float  wall-clock seconds
    """
    proc = subprocess.Popen(
        cmd_list,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    monitor = PlacementMonitor(proc.pid)
    t_start = time.time()
    monitor.start()

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()

    elapsed = time.time() - t_start
    monitor.stop()

    return monitor, proc.returncode, elapsed


def monitor_existing_pid(pid, duration_s=None):
    """
    Monitor `pid` until it exits or `duration_s` elapses.

    Returns:
        monitor  : PlacementMonitor (stopped)
        elapsed_s: float
    """
    monitor = PlacementMonitor(pid)
    t_start = time.time()
    monitor.start()

    try:
        while True:
            # Check process alive AND not a zombie (zombies pass os.kill check)
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, ProcessError if False else OSError):
                break
            try:
                with open(f"/proc/{pid}/stat") as _sf:
                    _stat = _sf.read()
                _state = _stat.split(")")[1].strip().split()[0]
                if _state == "Z":
                    break  # zombie: process exited, waiting to be reaped
            except (FileNotFoundError, IndexError, ValueError):
                break  # /proc entry gone
            if duration_s and (time.time() - t_start) >= duration_s:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    elapsed = time.time() - t_start
    monitor.stop()
    return monitor, elapsed


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(monitor, cpu_to_ccd, ccd_to_cpus,
                 elapsed_s=None, l3_per_ccd_mb=L3_PER_CCD_MB_DEFAULT,
                 topology_source="unknown"):
    """
    Build a structured report dict from monitor results.

    Separates "cores seen" (migration-inclusive) from "peak parallel"
    (true concurrency), and maps both to CCD chiplets.
    """
    all_cpus = sorted(monitor.all_cpus_seen)
    peak     = monitor.peak_parallel

    # Map all seen CPUs to their CCDs
    seen_ccds = defaultdict(list)
    for cpu in all_cpus:
        ccd = cpu_to_ccd.get(cpu, -1)
        seen_ccds[ccd].append(cpu)
    seen_ccds = dict(seen_ccds)

    n_ccds  = len(seen_ccds)
    cross   = n_ccds > 1

    # Infer execution character
    peak_procs = getattr(monitor, "peak_procs", 0)
    if peak <= 1 and peak_procs <= 1:
        exec_mode = "single-threaded"
    elif peak_procs > 1 and peak_procs >= peak:
        # Forked multi-process workload (e.g. openssl -multi N, nginx, MPI)
        exec_mode = f"multi-process ({peak_procs} processes, {peak} CPUs peak)"
    elif peak <= 4:
        exec_mode = f"multi-threaded ({peak} threads peak)"
    else:
        exec_mode = f"highly parallel ({peak} threads peak)"

    # Migration analysis
    n_migrations  = max(0, len(all_cpus) - peak)   # rough lower bound
    migration_note = ""
    if peak == 1 and len(all_cpus) > 1:
        migration_note = (
            f"Single thread migrated across {len(all_cpus)} cores due to OS "
            f"scheduling (context switches). Only 1 core active at a time."
        )

    # L3 analysis
    total_l3_mb  = n_ccds * l3_per_ccd_mb
    l3_per_thread = round(total_l3_mb / max(peak, 1), 1)

    return {
        # Core counts
        "unique_cores_seen"   : len(all_cpus),
        "cores_seen"          : all_cpus,
        "peak_parallel_cpus"  : peak,
        "peak_processes"      : getattr(monitor, "peak_procs", 0),
        "execution_mode"      : exec_mode,
        "migration_note"      : migration_note,

        # CCD breakdown
        "n_ccds_used"         : n_ccds,
        "ccds_used"           : {str(k): v for k, v in sorted(seen_ccds.items())},
        "cross_ccd_execution" : cross,

        # L3 cache implications
        "l3_per_ccd_mb"       : l3_per_ccd_mb,
        "total_l3_accessible_mb" : total_l3_mb,
        "l3_per_thread_mb"    : l3_per_thread,

        # System totals
        "total_system_cpus"   : len(cpu_to_ccd),
        "total_system_ccds"   : len(ccd_to_cpus),

        # Metadata
        "topology_source"     : topology_source,
        "elapsed_s"           : round(elapsed_s, 3) if elapsed_s else None,
        "n_samples"           : len(monitor.samples),
    }


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------

BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
RESET  = "\033[0m"
DIM    = "\033[2m"


def print_report(report):
    """Print colour-coded human-readable report to stdout."""
    peak    = report["peak_parallel_cpus"]
    n_cores = report["unique_cores_seen"]
    ccds    = report["ccds_used"]
    n_ccds  = report["n_ccds_used"]
    cross   = report["cross_ccd_execution"]
    mode    = report["execution_mode"]

    divider = f"  {'─' * 62}"

    topo_src = report.get("topology_source", "")
    print()
    print(divider)
    print(f"  {BOLD}CPU Placement & CCD Topology{RESET}"
          + (f"  {DIM}[topology: {topo_src}]{RESET}" if topo_src else ""))
    print(divider)

    # --- Thread / concurrency summary ----------------------------------
    peak_procs = report.get("peak_processes", 0)
    print(f"  {'Execution mode':<42} {BOLD}{mode}{RESET}")
    print(f"  {'Peak concurrent CPUs (true parallelism)':<42} {BOLD}{peak}{RESET}")
    if peak_procs > 1:
        print(f"  {'Peak forked processes (e.g. -multi N)':<42} {BOLD}{CYAN}{peak_procs}{RESET}")
    print(f"  {'Unique cores touched (incl. migrations)':<42} {n_cores}")
    print(f"  {'Core numbers seen':<42} {report['cores_seen']}")

    if report["migration_note"]:
        print()
        print(f"  {DIM}Note: {report['migration_note']}{RESET}")

    # --- CCD breakdown -------------------------------------------------
    print()
    print(f"  {'CCDs (chiplets) active':<42} {BOLD}{n_ccds} / {report['total_system_ccds']}{RESET}")
    for ccd_id, cpu_list in sorted(ccds.items(), key=lambda x: int(x[0])):
        print(f"    CCD {ccd_id}: cores {cpu_list}")

    # --- Cross-CCD verdict ---------------------------------------------
    print()
    if cross:
        print(f"  {'Cross-CCD execution':<42} {RED}{BOLD}YES — {n_ccds} CCDs used{RESET}")
        print(f"  {'L3 accessible':<42} {report['total_l3_accessible_mb']} MB "
              f"({n_ccds} × {report['l3_per_ccd_mb']} MB, separate domains)")
        print(f"  {'L3 per thread (peak)':<42} {report['l3_per_thread_mb']} MB")
        print()
        print(f"  {YELLOW}Impact:{RESET}")
        print(f"  {YELLOW}  • Cross-CCD cache-to-cache latency: ~100+ ns "
              f"(vs ~10 ns within one CCD){RESET}")
        print(f"  {YELLOW}  • Data shared between threads may bounce between L3s{RESET}")
        print(f"  {YELLOW}  • Pin workload to one CCD to eliminate this latency:{RESET}")
        # Build numactl hint: pick first CCD's cores
        first_ccd_cores = list(ccds.values())[0]
        core_range = f"{first_ccd_cores[0]}-{first_ccd_cores[-1]}"
        print(f"  {CYAN}    taskset -c {core_range} <workload>{RESET}")
        print(f"  {CYAN}    numactl --physcpubind={core_range} <workload>{RESET}")
    else:
        print(f"  {'Cross-CCD execution':<42} {GREEN}{BOLD}NO — single CCD{RESET}")
        print(f"  {'L3 accessible':<42} {report['total_l3_accessible_mb']} MB "
              f"(shared by all cores in CCD {list(ccds.keys())[0]})")
        print(f"  {'L3 per thread (peak)':<42} {report['l3_per_thread_mb']} MB")
        if peak == 1 and n_cores > 1:
            print()
            print(f"  {GREEN}Single thread — L3 is exclusive to this thread's data.{RESET}")
            print(f"  {DIM}OS migrated the thread across {n_cores} cores "
                  f"but always within CCD {list(ccds.keys())[0]}.{RESET}")

    print(divider)

    if report.get("elapsed_s"):
        print(f"  {DIM}Monitoring duration: {report['elapsed_s']:.3f}s "
              f"({report['n_samples']} samples at 50 ms interval){RESET}")
    print()


# ---------------------------------------------------------------------------
# Module-level helper (called from amd_pipeline_metrics.sh / HTML report)
# ---------------------------------------------------------------------------

def collect_placement(cmd_list, l3_per_ccd_mb=L3_PER_CCD_MB_DEFAULT):
    """
    Run `cmd_list` and return a placement report dict.
    Safe to import and call from other scripts.

    Args:
        cmd_list      : list of str, e.g. ["openssl", "speed", "aes-256-cbc"]
        l3_per_ccd_mb : L3 size per CCD in MB (default 32)

    Returns:
        dict  (same structure as build_report() output)
    """
    cpu_to_ccd, ccd_to_cpus, topo_src = build_ccd_topology()
    if not cpu_to_ccd:
        cpu_to_ccd, ccd_to_cpus, topo_src = fallback_topology()

    monitor, _rc, elapsed = run_workload_monitored(cmd_list)
    return build_report(monitor, cpu_to_ccd, ccd_to_cpus,
                        elapsed_s=elapsed, l3_per_ccd_mb=l3_per_ccd_mb,
                        topology_source=topo_src)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Monitor CPU placement and CCD topology for AMD workloads",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--pid", type=int, default=None,
                        help="Monitor an already-running process by PID")
    parser.add_argument("--duration", type=float, default=None,
                        help="With --pid: stop monitoring after N seconds")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON to stdout")
    parser.add_argument("--json-file", default=None,
                        help="Write JSON report to this file")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress human-readable output (useful with --json-file)")
    parser.add_argument("--l3-per-ccd", type=int, default=L3_PER_CCD_MB_DEFAULT,
                        dest="l3_per_ccd",
                        help=f"L3 cache per CCD in MB (default: {L3_PER_CCD_MB_DEFAULT})")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="Workload command (after --)")
    args = parser.parse_args()

    # Strip leading '--' separator
    cmd = args.command
    if cmd and cmd[0] == '--':
        cmd = cmd[1:]

    # Build topology
    cpu_to_ccd, ccd_to_cpus, topo_src = build_ccd_topology()
    if not cpu_to_ccd:
        print("WARNING: No topology source found; treating all CPUs as CCD 0.",
              file=sys.stderr)
        cpu_to_ccd, ccd_to_cpus, topo_src = fallback_topology()
    else:
        print(f"  Topology source: {topo_src}", file=sys.stderr)

    # Run
    elapsed = None
    if args.pid:
        monitor, elapsed = monitor_existing_pid(args.pid, args.duration)
    elif cmd:
        monitor, _rc, elapsed = run_workload_monitored(cmd)
    else:
        parser.print_help()
        sys.exit(1)

    if not monitor.all_cpus_seen:
        print("WARNING: No CPU placements recorded. "
              "Workload may have completed before first poll.", file=sys.stderr)

    report = build_report(monitor, cpu_to_ccd, ccd_to_cpus,
                          elapsed_s=elapsed, l3_per_ccd_mb=args.l3_per_ccd,
                          topology_source=topo_src)

    # Output
    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.quiet:
        print_report(report)

    if args.json_file:
        with open(args.json_file, 'w') as f:
            json.dump(report, f, indent=2)
        if not args.quiet:
            print(f"  JSON written to: {args.json_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
