#!/usr/bin/env python3
"""
amd_perf_html_analyze.py — AMD EPYC Performance Post-Analysis HTML Report

Three modes:
  1. Direct run (collects PMCs itself):
       python3 amd_perf_html_analyze.py "workload cmd" [output.html]

  2. Called from amd_pipeline_metrics.sh with --from-env:
       python3 amd_perf_html_analyze.py --from-env output.html KEY=val ...

  3. Parse saved terminal output:
       python3 amd_perf_html_analyze.py --from-terminal output.txt [report.html]

Generates a self-contained single-file HTML report with:
  - Bottleneck scorecard (visual bar chart)
  - Pattern analysis with likely cause and confidence
  - IPC deep-dive vs Zen4 theoretical/practical max
  - Frequency & power (perf vs APERF/MPERF cross-check)
  - Cache health (L2 DC / IC hit rates)
  - Parallelism & topology
  - Optimization recommendations
"""

import sys, os, re, json, datetime, subprocess, tempfile, textwrap
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# AMD Zen4 constants
# ─────────────────────────────────────────────────────────────────────────────
ZEN4_DISPATCH_WIDTH = 6.0   # micro-ops/cycle theoretical max
ZEN4_PRACTICAL_MAX  = 4.5   # typical integer workloads
ZEN4_L2_PER_CORE_MB = 1.0
ZEN4_L3_PER_CCD_MB  = 96.0  # 9684X: 32 MB base + 64 MB 3D V-Cache

ALL_EVENTS = (
    "task-clock,cpu-cycles,instructions,"
    "de_no_dispatch_per_slot.no_ops_from_frontend,"
    "de_no_dispatch_per_slot.backend_stalls,"
    "de_src_op_disp.all,ex_ret_ops,ls_not_halted_cyc,"
    "ex_no_retire.load_not_complete,ex_no_retire.not_complete,"
    "ex_ret_brn_misp,ex_ret_brn,"
    "l2_cache_req_stat.dc_hit_in_l2,l2_cache_req_stat.ls_rd_blk_c,"
    "l2_cache_req_stat.ic_fill_miss,l2_cache_req_stat.ic_hit_in_l2"
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def safe_div(a, b, default=0.0):
    try:
        return a / b if b else default
    except:
        return default

def pct(a, b):
    return safe_div(a, b) * 100

def severity(v, mild=5, moderate=15, severe=35):
    if v < mild:      return "negligible"
    if v < moderate:  return "mild"
    if v < severe:    return "moderate"
    return "severe"

SEV_COLOR = {
    "negligible": "#22c55e",   # green
    "mild":       "#84cc16",   # lime
    "moderate":   "#f59e0b",   # amber
    "severe":     "#ef4444",   # red
    "excellent":  "#22c55e",
    "good":       "#84cc16",
    "ok":         "#60a5fa",
}

def ipc_label(v):
    if v > 3.5: return ("Excellent", "excellent")
    if v > 2.5: return ("Good", "good")
    if v > 1.5: return ("Moderate — dependency-limited", "moderate")
    return ("Low — significant stalls", "severe")

def cache_label(pct_val):
    if pct_val > 95: return ("Excellent", "excellent")
    if pct_val > 85: return ("Good", "good")
    if pct_val > 70: return ("Moderate", "moderate")
    return ("Poor", "severe")

# ─────────────────────────────────────────────────────────────────────────────
# PMC collection (mode 1)
# ─────────────────────────────────────────────────────────────────────────────

def collect_perf(workload_cmd):
    """Run perf stat -j on workload_cmd and return parsed event dict."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        perf_out = tf.name

    cmd = f"perf stat -j -e {ALL_EVENTS} -- {workload_cmd} > /dev/null 2>{perf_out}"
    subprocess.run(cmd, shell=True)

    events = {}
    try:
        with open(perf_out) as fh:
            for line in fh:
                line = line.strip()
                if '"event"' not in line:
                    continue
                try:
                    obj   = json.loads(line)
                    name  = obj.get("event", "").strip()
                    val   = obj.get("counter-value", "0").replace(",", "").strip()
                    mval  = obj.get("metric-value", "")
                    unit  = obj.get("unit", "").strip()
                    if not name:
                        continue
                    events[name] = float(val) if val not in ("", "<not counted>", "<not supported>") else 0.0
                    if unit:
                        events[name + "__unit"] = unit
                    if mval and mval not in ("<not counted>", "<not supported>", ""):
                        try:
                            events[name + "__metric"] = float(str(mval).replace(",", ""))
                        except:
                            pass
                except:
                    pass
    finally:
        os.unlink(perf_out)

    return events

def compute_metrics(ev, total_cores=None):
    """Compute derived metrics from raw event dict."""
    if total_cores is None:
        total_cores = os.cpu_count() or 1

    task_clock_raw  = ev.get("task-clock", 0)
    task_clock_unit = ev.get("task-clock__unit", "msec")
    cpu_cycles      = ev.get("cpu-cycles", 1)
    instructions    = ev.get("instructions", 0)
    cpus_utilized   = ev.get("task-clock__metric", 0)

    # task-clock unit bug: perf sometimes reports ns but labels "msec"
    if task_clock_unit in ("ns", "nsec") or (task_clock_raw / 1e9 < 3600 and task_clock_raw > 1e6):
        eff_freq_ghz = safe_div(cpu_cycles, task_clock_raw)
    else:
        eff_freq_ghz = safe_div(cpu_cycles, task_clock_raw * 1e6)

    frontend_stalls = ev.get("de_no_dispatch_per_slot.no_ops_from_frontend", 0)
    backend_stalls  = ev.get("de_no_dispatch_per_slot.backend_stalls", 0)
    dispatched      = ev.get("de_src_op_disp.all", 0)
    retired         = ev.get("ex_ret_ops", 0)
    halted_cyc      = ev.get("ls_not_halted_cyc", 1)
    load_nc         = ev.get("ex_no_retire.load_not_complete", 0)
    not_complete    = ev.get("ex_no_retire.not_complete", 1)
    brn_misp        = ev.get("ex_ret_brn_misp", 0)
    brn_total       = ev.get("ex_ret_brn", 1)
    dc_hit          = ev.get("l2_cache_req_stat.dc_hit_in_l2", 0)
    dc_miss         = ev.get("l2_cache_req_stat.ls_rd_blk_c", 0)
    ic_hit          = ev.get("l2_cache_req_stat.ic_hit_in_l2", 0)
    ic_miss         = ev.get("l2_cache_req_stat.ic_fill_miss", 0)

    total_slots   = halted_cyc * ZEN4_DISPATCH_WIDTH
    frontend_pct  = pct(frontend_stalls, total_slots)
    backend_pct   = pct(backend_stalls,  total_slots)
    badspec_pct   = pct(dispatched - retired, total_slots)
    retiring_pct  = pct(retired, total_slots)

    mem_ratio     = safe_div(load_nc, not_complete)
    bmem_pct      = backend_pct * mem_ratio
    bcpu_pct      = backend_pct * (1 - mem_ratio)

    misp_rate     = pct(brn_misp, brn_total)
    ipc_val       = safe_div(instructions, cpu_cycles)
    dc_hit_rate   = pct(dc_hit, dc_hit + dc_miss)
    ic_hit_rate   = pct(ic_hit, ic_hit + ic_miss)
    cpu_util_pct  = safe_div(cpus_utilized, total_cores) * 100

    return dict(
        eff_freq_ghz = round(eff_freq_ghz, 3),
        frontend_pct = round(frontend_pct, 2),
        backend_pct  = round(backend_pct,  2),
        bmem_pct     = round(bmem_pct,     2),
        bcpu_pct     = round(bcpu_pct,     2),
        badspec_pct  = round(badspec_pct,  2),
        retiring_pct = round(retiring_pct, 2),
        ipc          = round(ipc_val,      3),
        misp_rate    = round(misp_rate,    3),
        dc_hit_rate  = round(dc_hit_rate,  2),
        ic_hit_rate  = round(ic_hit_rate,  2),
        cpu_util_pct = round(cpu_util_pct, 2),
        cpus_utilized= round(cpus_utilized, 3),
        total_cores  = total_cores,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Terminal output parser (mode 3)
# ─────────────────────────────────────────────────────────────────────────────

def parse_terminal(text):
    """Extract metrics from amd_pipeline_metrics.sh terminal output."""
    def grab(pattern, default=""):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else default

    def grabf(pattern, default=0.0):
        m = re.search(pattern, text)
        try: return float(m.group(1).replace(",", "")) if m else default
        except: return default

    workload    = grab(r"Workload:\s+(.+)")
    cpu_model   = grab(r"CPU:\s+(.+)")
    total_cores = int(grabf(r"Total Cores[^:]*:\s+(\d+)", 96))

    return dict(
        workload     = workload,
        cpu_model    = cpu_model,
        total_cores  = total_cores,
        bzy_ghz      = grabf(r"Bzy_MHz \(turbostat[^)]*\)\s+([\d.]+)\s+GHz"),
        eff_freq_ghz = grabf(r"Eff\. Freq[^)]*\)\s+([\d.]+)\s+GHz"),
        frontend_pct = grabf(r"Frontend Bound\s+([\d.]+)%"),
        backend_pct  = grabf(r"Backend Bound\s+([\d.]+)%"),
        bmem_pct     = grabf(r"Backend Memory(?:\s+Bound)?\s+([\d.]+)%"),
        bcpu_pct     = grabf(r"Backend CPU(?:\s+Bound)?\s+([\d.]+)%"),
        badspec_pct  = grabf(r"Bad Speculation\s+([\d.]+)%"),
        retiring_pct = grabf(r"Retiring[^\d]+([\d.]+)%"),
        ipc          = grabf(r"IPC[^(]*\s+([\d.]+)"),
        misp_rate    = grabf(r"Branch Misprediction Rate\s+([\d.]+)%"),
        dc_hit_rate  = grabf(r"L2 DC Hit Rate\s+([\d.]+)%"),
        ic_hit_rate  = grabf(r"L2 IC Hit Rate\s+([\d.]+)%"),
        cpu_util_pct = grabf(r"CPU Utilization[^)]*\)\s+([\d.]+)%"),
        peak_cpus    = grab(r"Peak Parallel CPUs\s+(\S+)"),
        exec_mode    = grab(r"Execution Mode\s+(.+)"),
        cross_ccd    = grab(r"Cross-CCD Execution\s+(\w+)"),
        n_ccds       = grab(r"CCDs Used\s+(\S+)"),
        cloud_ppl    = grabf(r"PPL=(\d+)W", 0),
        cloud_csp    = grab(r"Cloud[^:]*:\s+(\w+)"),
    )

# ─────────────────────────────────────────────────────────────────────────────
# Analysis engine
# ─────────────────────────────────────────────────────────────────────────────

def analyze(m):
    """Return analysis dict from metrics dict m."""
    frontend  = m.get("frontend_pct", 0)
    backend   = m.get("backend_pct",  0)
    bmem      = m.get("bmem_pct",     0)
    bcpu      = m.get("bcpu_pct",     0)
    badspec   = m.get("badspec_pct",  0)
    retiring  = m.get("retiring_pct", 0)
    ipc_val   = m.get("ipc",          0)
    misp      = m.get("misp_rate",    0)
    dc_hit    = m.get("dc_hit_rate",  0)
    ic_hit    = m.get("ic_hit_rate",  0)
    eff_freq  = m.get("eff_freq_ghz", 0)
    bzy_ghz   = m.get("bzy_ghz",      None)
    ppl       = m.get("cloud_ppl",    0)

    candidates = {"Frontend Bound": frontend, "Backend CPU": bcpu,
                  "Backend Memory": bmem, "Bad Speculation": badspec}
    primary     = max(candidates, key=candidates.get)
    primary_pct = candidates[primary]
    primary_sev = severity(primary_pct)

    patterns = []

    # Serial dependency chain
    if bcpu > 30 and ipc_val < 2.5 and bmem < 10 and misp < 1.0:
        patterns.append({
            "name": "Serial dependency chain",
            "confidence": "HIGH" if (bcpu > 50 and ipc_val < 2.0) else "MEDIUM",
            "detail": (
                f"Backend CPU stalls at {bcpu:.1f}% with IPC {ipc_val:.3f} and "
                f"near-zero memory pressure ({bmem:.2f}%). Execution units are stalling "
                f"waiting for prior instruction results — the out-of-order engine cannot "
                f"find independent work to execute in parallel."
            ),
            "examples": [
                "AES-CBC: each block XORs the previous ciphertext with plaintext before encrypting",
                "SHA/MD5: each round feeds its accumulator into the next",
                "CRC: feedback polynomial requires the prior result",
                "Pointer chasing: each load address comes from the previous load's data",
            ],
            "recommendations": [
                "For AES: switch to CTR or GCM mode — blocks are independent and parallelize across SIMD lanes",
                "Loop unrolling: expose multiple independent iterations to the out-of-order engine",
                "SIMD widening: process N independent data lanes per instruction (AVX-512 on Zen4)",
                "Compiler: <code>-O3 -march=znver4 -mtune=znver4</code> enables auto-vectorization",
                "For hash functions: consider BLAKE3 (parallelism-native) over SHA-256",
            ],
        })

    # DRAM thrashing
    if bmem > 20 and dc_hit < 70:
        patterns.append({
            "name": "DRAM / LLC thrashing",
            "confidence": "HIGH",
            "detail": (
                f"L2 DC hit rate {dc_hit:.1f}% — working set exceeds the 1 MB L2 per core. "
                f"Data is spilling to L3 or DRAM. Bare-metal Genoa DRAM latency ~80 ns; "
                f"EPYC 9684X has 96 MB L3 per CCD (3D V-Cache) which helps but random "
                f"access still creates pipeline bubbles."
            ),
            "examples": [
                "Large in-memory databases with random key access",
                "Sparse matrix operations",
                "Graph traversal with pointer-heavy structures",
            ],
            "recommendations": [
                "Tiling/blocking: keep hot working set under ~800 KB to stay in L2",
                "Software prefetching: <code>__builtin_prefetch()</code> 30-60 cycles ahead",
                "NUMA-aware allocation: <code>numactl --localalloc</code>",
                "Data layout: columnar (SoA) outperforms row-major (AoS) for SIMD and cache",
            ],
        })
    elif bmem > 20:
        patterns.append({
            "name": "L3 latency stalls",
            "confidence": "MEDIUM",
            "detail": (
                f"L2 DC hit rate {dc_hit:.1f}% is good but backend memory stalls are "
                f"{bmem:.1f}%. Data hits L3 but Genoa L3 latency (~35 cycles) creates "
                f"pipeline bubbles. Strides or pointer indirection are likely causes."
            ),
            "examples": ["Hash tables with collision chains", "B-tree traversal", "Streaming aggregation"],
            "recommendations": [
                "Software prefetch 30-60 cycles ahead for predictable access patterns",
                "Flat data structures (open-addressing) over pointer-linked (chained) for better cache behavior",
            ],
        })

    # Frontend
    if frontend > 15:
        patterns.append({
            "name": "Frontend bottleneck",
            "confidence": "HIGH" if frontend > 25 else "MEDIUM",
            "detail": (
                f"{frontend:.1f}% of dispatch slots lost in the frontend. The fetch/decode "
                f"stage is not keeping the backend supplied. Likely: large instruction "
                f"footprint exceeding L1I (32 KB), micro-op cache thrashing, or "
                f"branch resteer penalties."
            ),
            "examples": ["JVM / CPython bytecode dispatch", "Heavy virtual dispatch (vtables)", "Hot path spanning many cache lines"],
            "recommendations": [
                "Profile-guided optimization (PGO): <code>-fprofile-generate → run → -fprofile-use</code>",
                "Link-time optimization: <code>-flto=thin</code> (clang) or <code>-flto</code> (gcc)",
                "BOLT / Propeller for post-link code layout optimization",
            ],
        })

    # Branch misprediction
    if badspec > 5 or misp > 2.0:
        patterns.append({
            "name": "Branch misprediction pressure",
            "confidence": "HIGH" if misp > 5 else "MEDIUM",
            "detail": (
                f"{misp:.2f}% branch misprediction rate, {badspec:.2f}% of slots wasted. "
                f"Each misprediction on Zen4 costs ~15-20 cycles. "
                f"At {eff_freq:.3f} GHz that is ~4-5 ns per event."
            ),
            "examples": ["Data-dependent conditionals on unpredictable input", "Interpreter dispatch loops", "Sort/search on unsorted data"],
            "recommendations": [
                "Branchless code: replace if/else with conditional moves (cmov)",
                "Lookup tables: replace branch trees with array-indexed dispatch",
                "Sort input data before processing to improve branch predictability",
                "PGO: let the compiler see real branch probabilities",
            ],
        })

    if not patterns:
        patterns.append({
            "name": "Well-balanced — no dominant bottleneck",
            "confidence": "HIGH",
            "detail": "No single bottleneck dominates. The pipeline is being used efficiently.",
            "examples": [],
            "recommendations": ["Consider scaling out (more threads/cores) rather than micro-optimizing the serial path."],
        })

    # Frequency verdict
    if bzy_ghz:
        delta = abs(eff_freq - bzy_ghz)
        if ppl > 0 and bzy_ghz < 3.2:
            freq_verdict = f"⚠ THROTTLED — Bzy {bzy_ghz:.3f} GHz below expected (PPL={ppl:.0f} W active)"
            freq_color   = "#ef4444"
        elif bzy_ghz >= 3.5:
            freq_verdict = f"✓ Near max boost ({bzy_ghz:.3f} GHz) — no throttling detected"
            freq_color   = "#22c55e"
        else:
            freq_verdict = f"△ Moderate frequency ({bzy_ghz:.3f} GHz) — check idle states"
            freq_color   = "#f59e0b"
        freq_delta = f"{delta*1000:.0f} MHz delta (perf vs APERF/MPERF) — {'consistent ✓' if delta < 0.05 else 'check idle states'}"
    else:
        freq_verdict = "N/A — turbostat not available"
        freq_color   = "#6b7280"
        freq_delta   = ""

    ipc_efficiency = safe_div(ipc_val, ZEN4_PRACTICAL_MAX) * 100
    ipc_lbl, ipc_sev = ipc_label(ipc_val)

    return dict(
        primary=primary, primary_pct=primary_pct, primary_sev=primary_sev,
        patterns=patterns,
        freq_verdict=freq_verdict, freq_color=freq_color, freq_delta=freq_delta,
        ipc_efficiency=ipc_efficiency, ipc_lbl=ipc_lbl, ipc_sev=ipc_sev,
        bzy_ghz=bzy_ghz,
    )


# ─────────────────────────────────────────────────────────────────────────────
# System metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_meta(path):
    """Load system metadata JSON written by collect_sys_metadata()."""
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def _vuln_badge(val):
    val = val or ""
    if "Mitigation" in val or "Not affected" in val:
        bg, fg = "#166534", "#bbf7d0"
    elif "Vulnerable" in val:
        bg, fg = "#7f1d1d", "#fecaca"
    else:
        bg, fg = "#1e293b", "#94a3b8"
    short = val[:60] + ("…" if len(val) > 60 else "")
    return f'<span style="background:{bg};color:{fg};padding:1px 7px;border-radius:4px;font-size:11px">{short}</span>'


def build_meta_section(meta):
    """Build HTML for the Platform Configuration section."""
    if not meta:
        return "<p style='color:#64748b'>No metadata collected.</p>"

    parts = []

    # ── System / BIOS ─────────────────────────────────────────────────────────
    def mrow(label, val, color=None):
        if not val:
            return ""
        cs = f"color:{color};" if color else ""
        return (f'<div style="display:flex;justify-content:space-between;padding:5px 0;'
                f'border-bottom:1px solid #1e293b">'
                f'<span style="color:#94a3b8;font-size:12px">{label}</span>'
                f'<span style="font-size:12px;font-weight:600;{cs}">{val}</span></div>')

    sys_rows = (
        mrow("Hostname",           meta.get("hostname",""))
      + mrow("OS",                 meta.get("os",""))
      + mrow("Kernel",             meta.get("kernel",""))
      + mrow("Architecture",       meta.get("arch",""))
      + mrow("System Vendor",      meta.get("sys_vendor",""))
      + mrow("System Product",     meta.get("sys_product",""))
      + mrow("Baseboard",          f"{meta.get('baseboard_vendor','')} {meta.get('baseboard_product','')}".strip())
      + mrow("Chassis",            meta.get("chassis_type",""))
      + mrow("BIOS",               f"{meta.get('bios_vendor','')} {meta.get('bios_version','')} ({meta.get('bios_date','')})")
      + mrow("Sockets / Cores / Threads",
             f"{meta.get('sockets','')} socket(s) · {meta.get('cores_per_socket','')} cores · "
             f"{meta.get('threads_per_core','')} thread(s)/core")
      + mrow("CPU Family / Stepping",
             f"Family {meta.get('cpu_family','')} Model {meta.get('model_id','')} Stepping {meta.get('stepping','')}")
      + mrow("CPU Max / Min MHz",  f"{meta.get('cpu_max_mhz','N/A')} / {meta.get('cpu_min_mhz','N/A')} MHz")
      + mrow("L1d / L1i",         f"{meta.get('l1d_cache','')} / {meta.get('l1i_cache','')}")
      + mrow("L2 cache",           meta.get("l2_cache",""))
      + mrow("L3 cache",           meta.get("l3_cache",""))
      + mrow("Uptime",             meta.get("uptime",""))
      + mrow("Load Average",       meta.get("load",""))
    )
    # Power (from turbostat)
    pkg_w  = meta.get("pkg_watt","")
    sys_w  = meta.get("sys_watt","")
    ct_max = meta.get("core_temp_max","")
    if pkg_w:
        sys_rows += mrow("Package Power (PkgWatt)", f"{pkg_w} W", color="#fbbf24")
    if sys_w:
        sys_rows += mrow("System Power (SysWatt)",  f"{sys_w} W", color="#fbbf24")
    if ct_max:
        sys_rows += mrow("Core Temp Max",           f"{ct_max} °C",
                         color="#ef4444" if float(ct_max or 0) > 80 else "#22c55e")

    parts.append(f'<div style="margin-bottom:18px"><h4 style="color:#00C2DE;font-size:12px;'
                 f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">System & BIOS</h4>'
                 f'{sys_rows}</div>')

    # ── Memory ────────────────────────────────────────────────────────────────
    mem_rows = (
        mrow("Total Memory",  meta.get("mem_total",""))
      + mrow("Used / Free",   f"{meta.get('mem_used','')} / {meta.get('mem_free','')}")
      + mrow("Swap",          meta.get("swap_total",""))
      + mrow("DIMMs populated", f"{meta.get('dimm_populated',0)} of {meta.get('dimm_count',0)} slots")
    )
    dimms = meta.get("dimms", [])
    if dimms:
        tbl_hdr = ('<table style="width:100%;border-collapse:collapse;font-size:11px;margin-top:8px">'
                   '<tr style="color:#64748b;border-bottom:1px solid #334155">'
                   '<th style="text-align:left;padding:3px 6px">Slot</th>'
                   '<th style="text-align:left;padding:3px 6px">Size</th>'
                   '<th style="text-align:left;padding:3px 6px">Type</th>'
                   '<th style="text-align:left;padding:3px 6px">Speed</th>'
                   '<th style="text-align:left;padding:3px 6px">Manufacturer</th>'
                   '<th style="text-align:left;padding:3px 6px">Part #</th>'
                   '<th style="text-align:left;padding:3px 6px">Rank</th>'
                   '</tr>')
        tbl_rows = ""
        for i, d in enumerate(dimms):
            bg = "#0f172a" if i % 2 else "#1e293b"
            tbl_rows += (f'<tr style="background:{bg}">'
                         f'<td style="padding:3px 6px;color:#94a3b8">{d.get("locator","")}</td>'
                         f'<td style="padding:3px 6px;color:#e2e8f0;font-weight:600">{d.get("size","")}</td>'
                         f'<td style="padding:3px 6px;color:#e2e8f0">{d.get("type","")}</td>'
                         f'<td style="padding:3px 6px;color:#e2e8f0">{d.get("config_speed") or d.get("speed","")}</td>'
                         f'<td style="padding:3px 6px;color:#94a3b8">{d.get("manufacturer","")}</td>'
                         f'<td style="padding:3px 6px;color:#94a3b8;font-family:monospace">{d.get("part","")}</td>'
                         f'<td style="padding:3px 6px;color:#94a3b8">{d.get("rank","")}</td>'
                         f'</tr>')
        mem_rows += tbl_hdr + tbl_rows + '</table>'
    parts.append(f'<div style="margin-bottom:18px"><h4 style="color:#00C2DE;font-size:12px;'
                 f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Memory Configuration</h4>'
                 f'{mem_rows}</div>')

    # ── NUMA ─────────────────────────────────────────────────────────────────
    numa_info  = meta.get("numa_info", "").strip()
    numa_nodes = meta.get("numa_nodes", "")
    numa_cpus  = meta.get("numa_cpus", {})
    numa_html  = mrow("NUMA nodes", numa_nodes)
    for node, cpus in sorted(numa_cpus.items()):
        numa_html += mrow(f"  {node} CPUs", cpus)
    if numa_info:
        numa_html += (f'<pre style="background:#0f172a;color:#94a3b8;font-size:10px;'
                      f'padding:8px;border-radius:4px;overflow-x:auto;margin-top:6px;'
                      f'white-space:pre-wrap">{numa_info[:1200]}</pre>')
    parts.append(f'<div style="margin-bottom:18px"><h4 style="color:#00C2DE;font-size:12px;'
                 f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">NUMA Topology</h4>'
                 f'{numa_html}</div>')

    # ── CPU Vulnerabilities / Mitigations ─────────────────────────────────────
    vulns = meta.get("vulnerabilities", {})
    if vulns:
        vuln_rows = ""
        for cve, status in sorted(vulns.items()):
            vuln_rows += (f'<div style="display:flex;align-items:flex-start;gap:10px;'
                          f'padding:4px 0;border-bottom:1px solid #1e293b">'
                          f'<span style="color:#94a3b8;font-size:11px;min-width:180px;'
                          f'font-family:monospace">{cve}</span>'
                          f'{_vuln_badge(status)}</div>')
        parts.append(f'<div style="margin-bottom:18px"><h4 style="color:#00C2DE;font-size:12px;'
                     f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">'
                     f'CPU Vulnerabilities / Mitigations</h4>{vuln_rows}</div>')

    # ── Network ───────────────────────────────────────────────────────────────
    net_ifaces = meta.get("network", [])
    pci_net    = meta.get("pci_net", "").strip()
    if net_ifaces:
        net_tbl = ('<table style="width:100%;border-collapse:collapse;font-size:11px;margin-bottom:8px">'
                   '<tr style="color:#64748b;border-bottom:1px solid #334155">'
                   '<th style="text-align:left;padding:3px 6px">Interface</th>'
                   '<th style="text-align:left;padding:3px 6px">State</th>'
                   '<th style="text-align:left;padding:3px 6px">Speed</th>'
                   '<th style="text-align:left;padding:3px 6px">Driver</th>'
                   '<th style="text-align:left;padding:3px 6px">Firmware</th>'
                   '</tr>')
        for i, d in enumerate(net_ifaces):
            bg    = "#0f172a" if i % 2 else "#1e293b"
            state = d.get("state","")
            sc    = "#22c55e" if state == "UP" else "#ef4444" if state == "DOWN" else "#94a3b8"
            net_tbl += (f'<tr style="background:{bg}">'
                        f'<td style="padding:3px 6px;color:#e2e8f0;font-weight:600">{d.get("iface","")}</td>'
                        f'<td style="padding:3px 6px;color:{sc};font-weight:600">{state}</td>'
                        f'<td style="padding:3px 6px;color:#e2e8f0">{d.get("speed","—")}</td>'
                        f'<td style="padding:3px 6px;color:#94a3b8">{d.get("driver","—")}</td>'
                        f'<td style="padding:3px 6px;color:#64748b;font-family:monospace;font-size:10px">'
                        f'{d.get("firmware","—")[:30]}</td></tr>')
        net_tbl += '</table>'
    else:
        net_tbl = ""
    pci_net_html = ""
    if pci_net:
        pci_net_html = (f'<pre style="background:#0f172a;color:#94a3b8;font-size:10px;'
                        f'padding:8px;border-radius:4px;overflow-x:auto;white-space:pre-wrap">'
                        f'{pci_net}</pre>')
    if net_tbl or pci_net_html:
        parts.append(f'<div style="margin-bottom:18px"><h4 style="color:#00C2DE;font-size:12px;'
                     f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Network</h4>'
                     f'{net_tbl}{pci_net_html}</div>')

    # ── Storage ───────────────────────────────────────────────────────────────
    storage    = meta.get("storage", [])
    pci_storage = meta.get("pci_storage","").strip()
    df_root    = meta.get("df_root","")
    stor_html  = ""
    if storage:
        stbl = ('<table style="width:100%;border-collapse:collapse;font-size:11px;margin-bottom:6px">'
                '<tr style="color:#64748b;border-bottom:1px solid #334155">'
                '<th style="text-align:left;padding:3px 6px">Device</th>'
                '<th style="text-align:left;padding:3px 6px">Size</th>'
                '<th style="text-align:left;padding:3px 6px">Media</th>'
                '<th style="text-align:left;padding:3px 6px">Transport</th>'
                '<th style="text-align:left;padding:3px 6px">Model</th>'
                '</tr>')
        for i, d in enumerate(storage):
            bg    = "#0f172a" if i % 2 else "#1e293b"
            media = d.get("media","")
            mc    = "#22c55e" if "NVMe" in media else "#60a5fa" if "SSD" in media else "#94a3b8"
            stbl += (f'<tr style="background:{bg}">'
                     f'<td style="padding:3px 6px;color:#e2e8f0;font-family:monospace">/dev/{d.get("name","")}</td>'
                     f'<td style="padding:3px 6px;color:#e2e8f0;font-weight:600">{d.get("size","")}</td>'
                     f'<td style="padding:3px 6px;color:{mc};font-weight:600">{media}</td>'
                     f'<td style="padding:3px 6px;color:#94a3b8">{d.get("tran","")}</td>'
                     f'<td style="padding:3px 6px;color:#94a3b8">{d.get("model","")}</td></tr>')
        stbl += '</table>'
        stor_html += stbl
    if df_root:
        stor_html += f'<div style="color:#64748b;font-size:11px">Root fs: {df_root}</div>'
    if pci_storage:
        stor_html += (f'<pre style="background:#0f172a;color:#94a3b8;font-size:10px;'
                      f'padding:8px;border-radius:4px;overflow-x:auto;white-space:pre-wrap;margin-top:6px">'
                      f'{pci_storage}</pre>')
    if stor_html:
        parts.append(f'<div style="margin-bottom:18px"><h4 style="color:#00C2DE;font-size:12px;'
                     f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Storage</h4>'
                     f'{stor_html}</div>')

    # ── lstopo ────────────────────────────────────────────────────────────────
    lstopo = meta.get("lstopo","").strip()
    if lstopo:
        parts.append(f'<div style="margin-bottom:8px"><h4 style="color:#00C2DE;font-size:12px;'
                     f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">lstopo Topology</h4>'
                     f'<details><summary style="cursor:pointer;color:#94a3b8;font-size:12px">'
                     f'Click to expand topology tree</summary>'
                     f'<pre style="background:#0f172a;color:#94a3b8;font-size:10px;'
                     f'padding:10px;border-radius:4px;overflow-x:auto;white-space:pre;margin-top:6px">'
                     f'{lstopo}</pre></details></div>')

    content = "".join(parts)
    return content or "<p style='color:#64748b'>Metadata not available on this platform.</p>"

# ─────────────────────────────────────────────────────────────────────────────
# HTML generation
# ─────────────────────────────────────────────────────────────────────────────

def bar(value, max_val=100, color=None, height=20):
    if color is None:
        color = SEV_COLOR.get(severity(value), "#60a5fa")
    w = min(100, safe_div(value, max_val) * 100)
    return (
        f'<div style="background:#1e293b;border-radius:4px;height:{height}px;width:100%">'
        f'<div style="background:{color};width:{w:.1f}%;height:100%;border-radius:4px;'
        f'transition:width 0.5s"></div></div>'
    )

def gauge_svg(value, max_val, color, label, unit="%"):
    """Simple SVG arc gauge."""
    import math
    frac  = min(1.0, safe_div(value, max_val))
    angle = frac * 180
    r = 55
    cx, cy = 80, 75
    rad = math.radians(180 - angle)
    ex  = cx + r * math.cos(rad)
    ey  = cy - r * math.sin(rad)
    large = 1 if angle > 180 else 0
    arc_d = f"M {cx-r},{cy} A {r},{r} 0 {large},1 {ex:.1f},{ey:.1f}"
    val_str = f"{value:.1f}{unit}"
    return f"""
<svg width="160" height="115" viewBox="0 0 160 115">
  <path d="M {cx-r},{cy} A {r},{r} 0 1,1 {cx+r},{cy}" fill="none" stroke="#1e293b" stroke-width="11"/>
  <path d="{arc_d}" fill="none" stroke="{color}" stroke-width="11" stroke-linecap="round"/>
  <text x="{cx}" y="{cy+2}" text-anchor="middle" fill="#f1f5f9" font-size="20" font-weight="bold">{val_str}</text>
  <text x="{cx}" y="{cy+18}" text-anchor="middle" fill="#94a3b8" font-size="11">{label}</text>
</svg>"""

def conf_badge(conf):
    colors = {"HIGH": ("#22c55e", "#052e16"), "MEDIUM": ("#f59e0b", "#1c1000"), "LOW": ("#6b7280", "#111")}
    bg, fg = colors.get(conf, ("#6b7280", "#111"))
    return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:700">{conf}</span>'

def sev_badge(sev):
    color = SEV_COLOR.get(sev, "#6b7280")
    return f'<span style="background:{color};color:#000;padding:2px 10px;border-radius:9999px;font-size:11px;font-weight:700;text-transform:uppercase">{sev}</span>'

def metric_row(label, value, unit="", color=None, bar_val=None, bar_max=100):
    color_style = f"color:{color};" if color else ""
    bar_html = ""
    if bar_val is not None:
        bar_html = f'<div style="margin-top:4px">{bar(bar_val, bar_max, color)}</div>'
    return f"""
<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #1e293b">
  <span style="color:#94a3b8;font-size:13px">{label}</span>
  <span style="font-weight:700;font-size:14px;{color_style}">{value}{unit}</span>
</div>{bar_html}"""

def card(title, content, accent="#00C2DE"):
    return f"""
<div style="background:#0f172a;border:1px solid #1e293b;border-top:3px solid {accent};
     border-radius:8px;padding:20px;margin-bottom:20px">
  <h3 style="margin:0 0 16px;font-size:15px;font-weight:700;color:{accent};
       text-transform:uppercase;letter-spacing:1px">{title}</h3>
  {content}
</div>"""

def build_html(m, a, workload, cpu_model, ts, meta=None):
    primary_color = SEV_COLOR.get(a["primary_sev"], "#f59e0b")
    bzy_ghz  = a["bzy_ghz"]
    eff_freq = m.get("eff_freq_ghz", 0)

    # ── Scorecard bars ──
    ret_color = "#22c55e" if m["retiring_pct"] > 40 else "#84cc16" if m["retiring_pct"] > 25 else "#f59e0b"
    scorecard_rows = [
        ("Retiring (useful work ↑)", m["retiring_pct"],  ret_color, 100),
        ("Frontend Bound ↓",         m["frontend_pct"],  SEV_COLOR.get(severity(m["frontend_pct"]), "#f59e0b"), 100),
        ("Backend CPU Bound ↓",      m["bcpu_pct"],      SEV_COLOR.get(severity(m["bcpu_pct"]), "#f59e0b"), 100),
        ("Backend Memory Bound ↓",   m["bmem_pct"],      SEV_COLOR.get(severity(m["bmem_pct"]), "#f59e0b"), 100),
        ("Bad Speculation ↓",        m["badspec_pct"],   SEV_COLOR.get(severity(m["badspec_pct"]), "#f59e0b"), 100),
    ]
    sc_html = ""
    for label, val, color, maxv in scorecard_rows:
        sc_html += f"""
<div style="margin-bottom:12px">
  <div style="display:flex;justify-content:space-between;margin-bottom:4px">
    <span style="color:#e2e8f0;font-size:13px">{label}</span>
    <span style="color:{color};font-weight:700;font-size:13px">{val:.2f}%</span>
  </div>
  {bar(val, maxv, color, 14)}
</div>"""

    # ── Patterns ──
    pat_html = ""
    for p in a["patterns"]:
        recs = "".join(f'<li style="margin:4px 0;color:#cbd5e1">{r}</li>' for r in p["recommendations"])
        exs  = "".join(f'<li style="margin:4px 0;color:#94a3b8">{e}</li>' for e in p["examples"]) if p["examples"] else ""
        pat_html += f"""
<div style="background:#1e293b;border-radius:6px;padding:16px;margin-bottom:12px">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
    <span style="font-weight:700;color:#f1f5f9">{p["name"]}</span>
    {conf_badge(p["confidence"])}
  </div>
  <p style="color:#94a3b8;font-size:13px;margin:0 0 10px;line-height:1.6">{p["detail"]}</p>
  {"<p style='color:#64748b;font-size:12px;margin:0 0 4px'>Common workloads:</p><ul style='margin:0 0 10px;padding-left:18px;font-size:12px'>" + exs + "</ul>" if exs else ""}
  <p style="color:#00C2DE;font-size:12px;margin:0 0 4px;font-weight:600">Recommendations:</p>
  <ul style="margin:0;padding-left:18px;font-size:12px">{recs}</ul>
</div>"""

    # ── Freq card ──
    freq_rows = metric_row("Effective Freq (perf cycles/task-clock)", f"{eff_freq:.3f}", " GHz")
    if bzy_ghz:
        freq_rows += metric_row("Bzy_MHz (APERF/MPERF — gold standard)", f"{bzy_ghz:.3f}", " GHz", color="#22c55e")
        freq_rows += metric_row("Cross-check delta", a["freq_delta"])
    ppl = m.get("cloud_ppl", 0)
    freq_rows += metric_row("Package Power Limit", "unconstrained" if not ppl else f"{ppl:.0f} W", color="#22c55e" if not ppl else "#f59e0b")
    freq_rows += f'<div style="margin-top:12px;padding:10px;background:#1e293b;border-radius:6px;color:{a["freq_color"]};font-weight:700">{a["freq_verdict"]}</div>'

    # ── IPC gauges ──
    ipc_val = m["ipc"]
    ipc_col = SEV_COLOR.get(a["ipc_sev"], "#f59e0b")
    ipc_eff = a["ipc_efficiency"]
    gauges  = f"""
<div style="display:flex;gap:20px;flex-wrap:wrap;justify-content:center;margin-bottom:16px">
  {gauge_svg(ipc_val, ZEN4_PRACTICAL_MAX, ipc_col, "IPC", "")}
  {gauge_svg(ipc_eff, 100, ipc_col, "% of Zen4 max")}
</div>
<div style="text-align:center;margin-bottom:12px">
  <span style="color:{ipc_col};font-weight:700">{a["ipc_lbl"]}</span>
</div>"""
    ipc_detail = f"""
{metric_row("Zen4 dispatch width (theoretical)", f"{ZEN4_DISPATCH_WIDTH:.0f}", " ops/cycle")}
{metric_row("Zen4 practical max (integer)", f"{ZEN4_PRACTICAL_MAX:.1f}", " ops/cycle")}
{metric_row("IPC efficiency", f"{ipc_eff:.1f}", "%", color=ipc_col)}
{metric_row("Retiring % (useful work ↑)", f"{m['retiring_pct']:.2f}", "%", color="#22c55e" if m['retiring_pct'] > 40 else "#84cc16" if m['retiring_pct'] > 25 else "#f59e0b")}"""

    # ── Cache ──
    dc_lbl, dc_sev = cache_label(m["dc_hit_rate"])
    ic_lbl, ic_sev = cache_label(m["ic_hit_rate"])
    cache_html = f"""
{metric_row("L2 Data Cache Hit Rate", f"{m['dc_hit_rate']:.2f}", "%", color=SEV_COLOR.get(dc_sev,"#f59e0b"), bar_val=m['dc_hit_rate'])}
{metric_row("L2 Instr Cache Hit Rate", f"{m['ic_hit_rate']:.2f}", "%", color=SEV_COLOR.get(ic_sev,"#f59e0b"), bar_val=m['ic_hit_rate'])}
<div style="margin-top:12px;background:#1e293b;border-radius:6px;padding:12px;font-size:12px;color:#94a3b8">
  <div>L2 per core: <strong style="color:#e2e8f0">{ZEN4_L2_PER_CORE_MB:.0f} MB</strong> (4× Intel 256 KB)</div>
  <div>L3 per CCD (EPYC 9684X): <strong style="color:#e2e8f0">{ZEN4_L3_PER_CCD_MB:.0f} MB</strong> (32 MB base + 64 MB 3D V-Cache)</div>
  <div style="margin-top:6px">DC: {dc_lbl} &nbsp;|&nbsp; IC: {ic_lbl}</div>
</div>"""

    # ── Topology ──
    exec_mode  = m.get("exec_mode",  "—")
    peak_cpus  = m.get("peak_cpus",  "—")
    cross_ccd  = m.get("cross_ccd",  "—")
    n_ccds     = m.get("n_ccds",     "—")
    cpu_util   = m.get("cpu_util_pct", 0)
    total_c    = m.get("total_cores", 0)
    topo_html  = f"""
{metric_row("Execution mode", exec_mode)}
{metric_row("Peak concurrent CPUs", str(peak_cpus))}
{metric_row("CCDs active", str(n_ccds))}
{metric_row("Cross-CCD execution", cross_ccd, color="#22c55e" if cross_ccd == "NO" else "#f59e0b")}
{metric_row("System CPU utilization", f"{cpu_util:.2f}", "%", bar_val=cpu_util, bar_max=100)}
<p style="color:#64748b;font-size:12px;margin-top:8px">
  System util = CPUs utilized / {total_c} total cores.
  Single-threaded on 96-core system ≈ 1% — expected.
</p>"""

    # ── Summary hero ──
    hero = f"""
<div style="background:linear-gradient(135deg,#0f172a,#1e293b);border:1px solid #334155;
     border-radius:10px;padding:24px;margin-bottom:24px;display:flex;
     flex-wrap:wrap;gap:20px;align-items:center">
  <div style="flex:1;min-width:220px">
    <div style="color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:1px">Primary Bottleneck</div>
    <div style="font-size:22px;font-weight:800;margin:4px 0;color:{primary_color}">{a["primary"]} — {a["primary_pct"]:.1f}%</div>
    {sev_badge(a["primary_sev"])}
  </div>
  <div style="flex:1;min-width:180px">
    <div style="color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:1px">Detected Pattern</div>
    <div style="font-size:15px;font-weight:700;color:#e2e8f0;margin:4px 0">{a["patterns"][0]["name"]}</div>
    {conf_badge(a["patterns"][0]["confidence"])}
  </div>
  <div style="flex:1;min-width:160px">
    <div style="color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:1px">IPC</div>
    <div style="font-size:22px;font-weight:800;color:{ipc_col};margin:4px 0">{ipc_val:.3f}</div>
    <div style="color:#64748b;font-size:12px">{a["ipc_efficiency"]:.0f}% of Zen4 max</div>
  </div>
  <div style="flex:1;min-width:200px">
    <div style="color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:1px">Frequency</div>
    <div style="font-size:15px;font-weight:700;margin:4px 0;color:{a['freq_color']}">{a["freq_verdict"]}</div>
  </div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMD EPYC Performance Analysis</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #020617;
    color: #e2e8f0;
    padding: 24px;
    min-height: 100vh;
  }}
  h1 {{ font-size: 22px; font-weight: 800; color: #00C2DE; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 12px; margin-bottom: 20px; line-height: 1.7; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 20px; }}
  ul {{ padding-left: 18px; }}
  code {{ background: #1e293b; padding: 1px 5px; border-radius: 3px; font-size: 11px; }}
  @media (max-width: 600px) {{ body {{ padding: 12px; }} }}
</style>
</head>
<body>

<div style="max-width:1100px;margin:0 auto">

  <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">
    <div style="width:32px;height:32px;background:#EE3124;border-radius:4px;display:flex;align-items:center;justify-content:center">
      <span style="color:#fff;font-weight:900;font-size:14px">A</span>
    </div>
    <h1>AMD EPYC Performance Analysis</h1>
  </div>

  <div class="meta">
    <div><strong>Workload:</strong> {workload}</div>
    <div><strong>Hardware:</strong> {cpu_model}</div>
    <div><strong>Generated:</strong> {ts}</div>
  </div>

  {hero}

  <div class="grid">
    {card("A · Bottleneck Scorecard", sc_html)}
    {card("B · Pattern Analysis", pat_html, accent="#a78bfa")}
  </div>

  <div class="grid">
    {card("C · IPC Analysis", gauges + ipc_detail, accent="#34d399")}
    {card("D · Frequency &amp; Power", freq_rows, accent="#fbbf24")}
  </div>

  <div class="grid">
    {card("E · Cache Health", cache_html, accent="#fb923c")}
    {card("F · Parallelism &amp; Topology", topo_html, accent="#60a5fa")}
  </div>

  {card("G · Platform Configuration", build_meta_section(meta or {}), accent="#818cf8")}

  <div style="text-align:center;color:#334155;font-size:11px;margin-top:24px;padding-top:12px;border-top:1px solid #1e293b">
    AMD EPYC Performance Toolkit · amd_perf_html_analyze.py · {ts}
  </div>

</div>
</body>
</html>"""

    return html

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not args:
        print("Usage:")
        print("  python3 amd_perf_html_analyze.py \"workload cmd\" [output.html]")
        print("  python3 amd_perf_html_analyze.py --from-terminal terminal.txt [output.html]")
        print("  python3 amd_perf_html_analyze.py --from-env output.html KEY=val ...")
        sys.exit(1)

    workload  = ""
    cpu_model = ""
    m         = {}

    # ── Mode: --from-env (called by amd_pipeline_metrics.sh) ──
    if args[0] == "--from-env":
        out_html = args[1] if len(args) > 1 else "amd_analysis.html"
        kv = {}
        for tok in args[2:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv[k] = v
        workload  = kv.get("WORKLOAD", "")
        cpu_model = kv.get("CPU_MODEL", "")
        def gf(k, d=0.0):
            try: return float(kv.get(k, d))
            except: return d
        def gs(k, d=""):
            return kv.get(k, d)
        m = dict(
            total_cores  = int(gf("TOTAL_CORES", os.cpu_count() or 1)),
            eff_freq_ghz = gf("EFF_FREQ_GHZ"),
            frontend_pct = gf("FRONTEND_PCT"),
            backend_pct  = gf("BACKEND_PCT"),
            bmem_pct     = gf("BACKEND_MEM_PCT"),
            bcpu_pct     = gf("BACKEND_CPU_PCT"),
            badspec_pct  = gf("BADSPEC_PCT"),
            retiring_pct = gf("RETIRING_PCT"),
            ipc          = gf("IPC"),
            misp_rate    = gf("MISP_RATE"),
            dc_hit_rate  = gf("DC_HIT_RATE"),
            ic_hit_rate  = gf("IC_HIT_RATE"),
            cpu_util_pct = gf("CPU_UTIL_PCT"),
            bzy_ghz      = gf("BZY_GHZ") if gs("BZY_GHZ") not in ("N/A", "", "0") else None,
            cloud_ppl    = gf("CLOUD_PPL"),
            peak_cpus    = gs("PEAK_CPUS"),
            exec_mode    = gs("EXEC_MODE"),
            cross_ccd    = gs("CROSS_CCD"),
            n_ccds       = gs("N_CCDS"),
            cloud_csp    = gs("CLOUD_CSP"),
        )
        meta = load_meta(gs("METADATA_JSON"))

    # ── Mode: --from-terminal ──
    elif args[0] == "--from-terminal":
        if len(args) < 2:
            print("Error: provide path to terminal output file")
            sys.exit(1)
        txt_file = args[1]
        out_html = args[2] if len(args) > 2 else txt_file.replace(".txt", "") + "_analysis.html"
        text     = Path(txt_file).read_text()
        parsed   = parse_terminal(text)
        workload  = parsed.pop("workload", "")
        cpu_model = parsed.pop("cpu_model", "")
        m = parsed

    # ── Mode: direct perf run ──
    else:
        workload_cmd = args[0]
        out_html     = args[1] if len(args) > 1 else "amd_analysis.html"
        workload     = workload_cmd
        cpu_model    = subprocess.check_output(
            "lscpu | grep 'Model name' | head -1 | cut -d: -f2 | xargs", shell=True
        ).decode().strip()
        total_cores = os.cpu_count() or 1
        print(f"Collecting PMCs for: {workload_cmd}")
        ev = collect_perf(workload_cmd)
        m  = compute_metrics(ev, total_cores)
        m["total_cores"] = total_cores

    if not cpu_model:
        try:
            cpu_model = subprocess.check_output(
                "lscpu | grep 'Model name' | head -1 | cut -d: -f2 | xargs", shell=True
            ).decode().strip()
        except:
            cpu_model = "AMD EPYC"

    a    = analyze(m)
    html = build_html(m, a, workload, cpu_model, ts, meta=meta)

    Path(out_html).write_text(html)
    print(f"Analysis written to: {out_html}")

if __name__ == "__main__":
    main()
