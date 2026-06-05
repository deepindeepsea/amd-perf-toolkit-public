#!/usr/bin/env python3
"""
amd_topdown.py — Top-Down Microarchitecture Analysis for AMD EPYC (Zen4/Zen5)

Subcommands:
  collect     Collect a TMA run and store it with labels
  compare     Compare two runs (by ID or label set)
  explain     Explain a TMA metric with AMD-specific tuning hints
  agent       Continuous collection daemon
  mcp-serve   MCP server for AI-assisted querying (stdio or HTTP)
  list        List stored runs
  query       Query stored runs (--bottleneck, --funnel, --label)

Storage: SQLite at ~/.amd_topdown/data.db by default
         Override: TOPDOWN_DB_PATH env var

Usage examples:
  # Collect while a benchmark runs
  python3 amd_topdown.py collect --process redis-server --duration 30 \\
    --label git_branch=unstable --label test_name=mget-100

  # Compare two branches
  python3 amd_topdown.py compare \\
    --label-a git_branch=unstable --label-b git_branch=mget-pr

  # Compare by run ID
  python3 amd_topdown.py compare abc123 def456

  # Show pipeline funnel for a run
  python3 amd_topdown.py query --funnel --label git_branch=mget-pr

  # Explain a metric
  python3 amd_topdown.py explain Fetch_Latency

  # Continuous daemon (collect every 5 min)
  python3 amd_topdown.py agent --process redis-server --every 300 --duration 30

  # MCP server (stdio)
  python3 amd_topdown.py mcp-serve
"""
from __future__ import annotations
import argparse, json, math, os, re, shlex, signal, sqlite3, subprocess
import sys, textwrap, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Shared engine — single source of truth for collection + funnel math.
# We reuse amd_perf_html_report.py (validated EVENT_GROUPS + calculate_metrics)
# instead of re-deriving the TMA formulas here. If the engine isn't importable
# (e.g. the single file was copied out on its own), we fall back to a built-in
# event list and the shared math is skipped — but no metric is ever fabricated.
# ─────────────────────────────────────────────────────────────────────────────

_ENGINE = None
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import amd_perf_html_report as _ENGINE  # type: ignore
except Exception:
    _ENGINE = None

# CCD / cache topology helper (Seam 3). Reuses amd_cpu_placement.py so the
# topology logic (lstopo → sysfs die_id → fallback) lives in one place.
_PLACEMENT = None
try:
    import amd_cpu_placement as _PLACEMENT  # type: ignore
except Exception:
    _PLACEMENT = None

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = Path(os.environ.get("TOPDOWN_DB_PATH", Path.home() / ".amd_topdown" / "data.db"))

# ─────────────────────────────────────────────────────────────────────────────
# Storage layer — SQLite
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    process     TEXT,
    duration_s  REAL,
    host        TEXT,
    cpu_model   TEXT,
    cpu_family  TEXT,
    metrics     TEXT NOT NULL,  -- JSON blob of metric floats
    labels      TEXT NOT NULL,  -- JSON dict of user + auto labels
    raw_events  TEXT            -- JSON blob of raw perf counters
);
"""

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

def save_run(process: str, duration_s: float, metrics: dict,
             labels: dict, raw_events: dict) -> str:
    run_id = uuid.uuid4().hex[:12]
    ts = datetime.now(timezone.utc).isoformat()
    host = os.uname().nodename
    cpu_model, cpu_family = _cpu_info()
    labels["host"] = host
    labels["cpu"] = cpu_model
    labels.setdefault("process", process or "")
    conn = get_db()
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, ts, process, duration_s, host, cpu_model, cpu_family,
         json.dumps(metrics), json.dumps(labels), json.dumps(raw_events))
    )
    conn.commit()
    conn.close()
    return run_id

def ingest_run(ts: str, process: str, duration_s: float, host: str,
               cpu_model: str, cpu_family: str, metrics: dict,
               labels: dict, raw_events: dict) -> str:
    """Insert a historical run, preserving its ORIGINAL host/cpu/ts.

    Unlike save_run (which stamps the *current* machine), this is used by
    `ingest` to backfill the store from an existing run corpus so that
    query/compare work over historical data. Idempotent per source file:
    if a run with the same labels['source_file'] already exists it is
    replaced rather than duplicated.
    """
    conn = get_db()
    src_file = labels.get("source_file")
    if src_file:
        for row in conn.execute("SELECT id, labels FROM runs").fetchall():
            try:
                if json.loads(row["labels"]).get("source_file") == src_file:
                    conn.execute("DELETE FROM runs WHERE id=?", (row["id"],))
            except Exception:
                pass
    run_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, ts, process, duration_s, host, cpu_model, cpu_family,
         json.dumps(metrics), json.dumps(labels), json.dumps(raw_events))
    )
    conn.commit()
    conn.close()
    return run_id

def load_runs(label_filter: dict | None = None, limit: int = 50) -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["metrics"] = json.loads(d["metrics"])
        d["labels"]  = json.loads(d["labels"])
        d["raw_events"] = json.loads(d["raw_events"] or "{}")
        if label_filter and not all(d["labels"].get(k) == v for k,v in label_filter.items()):
            continue
        result.append(d)
    return result

def load_run_by_id(run_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not row: return None
    d = dict(row)
    d["metrics"] = json.loads(d["metrics"])
    d["labels"]  = json.loads(d["labels"])
    d["raw_events"] = json.loads(d["raw_events"] or "{}")
    return d

def _best_run(label_filter: dict) -> dict | None:
    runs = load_runs(label_filter, limit=1)
    return runs[0] if runs else None

def _cpu_info() -> tuple[str, str]:
    model, family = "unknown", "unknown"
    try:
        for ln in Path("/proc/cpuinfo").read_text().splitlines():
            if ln.startswith("model name") and model == "unknown":
                model = ln.split(":",1)[1].strip()
            if ln.startswith("cpu family") and family == "unknown":
                family = ln.split(":",1)[1].strip()
    except Exception:
        pass
    return model, family

def _ccd_topology() -> dict:
    """Return CCD / cache-complex topology for this host as a plain dict.

    Reuses amd_cpu_placement.build_ccd_topology() (lstopo → sysfs die_id →
    fallback) so there is a single source of topology truth. CCD affinity is
    central to AMD EPYC tuning: keeping a workload's threads on one CCD shares
    the 32 MB L3 (96 MB on Genoa-X 3D V-Cache) and avoids cross-CCD latency.
    """
    cpu_to_ccd = ccd_to_cpus = None
    source = "unavailable"
    if _PLACEMENT is not None:
        try:
            cpu_to_ccd, ccd_to_cpus, source = _PLACEMENT.build_ccd_topology()
            if not cpu_to_ccd:
                cpu_to_ccd, ccd_to_cpus, source = _PLACEMENT.fallback_topology()
        except Exception as ex:
            source = f"error: {ex}"
    if not cpu_to_ccd:
        # last-resort inline fallback so the tool still answers without the helper
        n = os.cpu_count() or 1
        cpu_to_ccd = {i: 0 for i in range(n)}
        ccd_to_cpus = {0: list(range(n))}
        if source == "unavailable":
            source = "fallback (amd_cpu_placement not importable)"
    ccds = {int(k): sorted(int(c) for c in v) for k, v in ccd_to_cpus.items()}
    sizes = [len(v) for v in ccds.values()]
    model, family = _cpu_info()
    l3 = None
    if _PLACEMENT is not None:
        l3 = getattr(_PLACEMENT, "L3_PER_CCD_MB_DEFAULT", None)
    return {
        "source":        source,
        "cpu_model":     model,
        "cpu_family":    family,
        "n_cpus":        sum(sizes),
        "n_ccds":        len(ccds),
        "cores_per_ccd": (sizes[0] if sizes and len(set(sizes)) == 1 else sizes),
        "l3_per_ccd_mb": l3,
        "ccd_to_cpus":   {str(k): v for k, v in sorted(ccds.items())},
    }

def _render_topology(t: dict) -> str:
    lines = []
    lines.append("\n  CCD / Cache-Complex Topology")
    lines.append("  " + "─" * 63)
    lines.append(f"  CPU            {t['cpu_model']}  (family {t['cpu_family']})")
    lines.append(f"  Source         {t['source']}")
    cpc = t["cores_per_ccd"]
    cpc_s = str(cpc) if not isinstance(cpc, list) else "varies " + str(cpc)
    l3 = t.get("l3_per_ccd_mb")
    l3_s = f"{l3} MB" if l3 else "n/a"
    lines.append(f"  CCDs           {t['n_ccds']}    CPUs {t['n_cpus']}    cores/CCD {cpc_s}    L3/CCD {l3_s}")
    lines.append("  " + "─" * 63)
    for ccd, cpus in t["ccd_to_cpus"].items():
        rng = _compact_cpu_list(cpus)
        lines.append(f"  CCD {ccd:<3}  ({len(cpus):>3} cpus)  {rng}")
    lines.append("  " + "─" * 63)
    if t["n_ccds"] > 1:
        lines.append("  Tip: pin a latency-sensitive workload to ONE CCD to keep its")
        lines.append("       working set in that CCD's shared L3 and avoid cross-CCD hops.")
    lines.append("")
    return "\n".join(lines)

def _compact_cpu_list(cpus: list) -> str:
    """[0,1,2,3,8,9] → '0-3,8-9' for readable CCD listings."""
    if not cpus:
        return "-"
    cpus = sorted(cpus)
    out, start, prev = [], cpus[0], cpus[0]
    for c in cpus[1:]:
        if c == prev + 1:
            prev = c; continue
        out.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = c
    out.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(out)

# ─────────────────────────────────────────────────────────────────────────────
# perf collection — reuses amd_perf_html_report.py logic if available,
# otherwise inline (so this file is self-contained)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_div(n, d, default=0.0):
    # Prefer the engine's safe_div so rounding/zero-handling stay identical.
    if _ENGINE is not None:
        return _ENGINE.safe_div(n, d, default)
    return n / d if d else default

# ── Metric-YAML driven collection (Seam 2) ──────────────────────────────────
# The event set is sourced from the project's canonical metric definitions in
# pmc_test/metrics/*.yaml, family-gated by each file's `requires.cpu_family`, so
# the events collected here track the same defs the rest of the toolkit uses
# instead of a list that can silently drift. The engine's EVENT_GROUPS stay the
# source of truth for calculate_metrics; matching YAMLs union in their symbolic
# mnemonics and supply the funnel events outright when the engine isn't importable.

_YAML_ZEN4 = "pipeline_zen4_genoa.yaml"
_YAML_ZEN5 = "table58_pipeline_zen5.yaml"

def _normalize_family(v) -> Optional[int]:
    """Accept 25, '25', '0x19', 0x19 → int; None on failure."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        return None

def _metrics_dir() -> Optional[Path]:
    d = Path(__file__).resolve().parent / "pmc_test" / "metrics"
    return d if d.is_dir() else None

def _load_metric_yaml(name: str) -> Optional[dict]:
    """Load one metric YAML → {families:set[int], events:[{name,perf}], metrics, name}.
    Returns None if pyyaml is missing or the file can't be parsed — callers then
    fall back to engine groups / hardcoded events, so this never hard-fails."""
    mdir = _metrics_dir()
    if mdir is None:
        return None
    path = mdir / name
    if not path.is_file():
        return None
    try:
        import yaml  # lazy: keep amd_topdown importable without pyyaml installed
    except Exception:
        return None
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return None
    req = (doc.get("requires") or {}).get("cpu_family") or []
    fams = {f for f in (_normalize_family(x) for x in req) if f is not None}
    events = [e for e in (doc.get("events") or []) if isinstance(e, dict) and e.get("perf")]
    return {"families": fams, "events": events, "metrics": doc.get("metrics") or {}, "name": name}

def _yaml_matches_family(doc: Optional[dict], family) -> bool:
    if not doc:
        return False
    f = _normalize_family(family)
    return f is not None and f in doc["families"]

def _is_raw_perf(spec: str) -> bool:
    """True for raw cpu/event=.../ descriptors (Zen5 codes), False for mnemonics."""
    return spec.strip().startswith("cpu/")

def _base_events(family=None) -> list[str]:
    """Core funnel event list. Primary source is the engine's EVENT_GROUPS so the
    collected events can never drift from what calculate_metrics expects; matching
    family YAMLs (Seam 2) union in their symbolic mnemonics and supply the funnel
    events outright when the engine is not importable. Hardcoded list is the last
    resort only."""
    if family is None:
        _, family = _cpu_info()

    seen, out = set(), []
    def _add(ev):
        ev = (ev or "").strip()
        if ev and ev not in seen:
            seen.add(ev); out.append(ev)

    # 1) engine groups — source of truth (includes L2 / freq / task-clock events)
    if _ENGINE is not None:
        for group in _ENGINE.EVENT_GROUPS.values():
            for ev in group.split(","):
                _add(ev)

    # 2) YAML-driven mnemonics, family-gated (confirms on Zen4; supplies if engine absent)
    z4 = _load_metric_yaml(_YAML_ZEN4)
    if _yaml_matches_family(z4, family):
        for e in z4["events"]:
            if not _is_raw_perf(e["perf"]):
                _add(e["perf"])

    if out:
        return out

    # 3) Fallback (no engine, no usable YAML): confirmed-good symbolic events.
    return [
        "task-clock", "cpu-cycles", "instructions",
        "de_no_dispatch_per_slot.no_ops_from_frontend",
        "de_no_dispatch_per_slot.backend_stalls",
        "de_src_op_disp.all", "ex_ret_ops", "ls_not_halted_cyc",
        "ex_no_retire.load_not_complete", "ex_no_retire.not_complete",
        "ex_ret_brn_misp", "ex_ret_brn",
        "l2_cache_req_stat.dc_hit_in_l2", "l2_cache_req_stat.ls_rd_blk_c",
    ]

# Zen5-only raw events (AMD Family 1Ah) for ACCURATE sub-splits. Codes validated
# in pmc_test/metrics/table58_pipeline_zen5.yaml. These let us compute
# Fetch_Latency vs Fetch_Bandwidth and Light vs Heavy (microcode) from real
# counters instead of fabricated ratios. On Zen4 they silently return 0, so we
# only request them on Family 1Ah and otherwise leave those sub-metrics unset.
_ZEN5_EXTRA = [
    "cpu/event=0x1a0,umask=0x01,edge,name=fe_lat_edge/",  # fetch-latency stall edges
    "cpu/event=0x1a0,umask=0x01,name=fe_stall_cyc/",      # frontend stall cycles (denom)
    "cpu/event=0x1c2,name=mc_ret/",                       # microcode ops retired
]

def _is_zen5(family: str | int | None) -> bool:
    f = _normalize_family(family)
    return f == 26  # 0x1A

def _want_zen5_extras(family) -> bool:
    """Add the Zen5 sub-split raw events only when the Zen5 metric YAML declares
    support for this family (Seam 2 gate). Falls back to the Family 1Ah literal
    check if the YAML is unavailable. The _ZEN5_EXTRA codes mirror the raw events
    in table58_pipeline_zen5.yaml but keep the names _metrics_from_raw expects."""
    z5 = _load_metric_yaml(_YAML_ZEN5)
    if z5 is not None:
        return _yaml_matches_family(z5, family)
    return _is_zen5(family)

# Zen4 (AMD Family 19h / Genoa) sub-splits come from AMD's OWN perf metricgroups,
# which perf computes from the shipped AMD JSON event definitions — so they are
# reused, not fabricated. `frontend_bound_latency` is derived by perf from
# `cpu/de_no_dispatch_per_slot.no_ops_from_frontend,cmask=0x6/` (full-width fetch
# stalls), `frontend_bound_bandwidth` from the plain frontend-stall slots, and
# `retiring_microcode` from `ex_ret_ucode_ops`. We request them with `perf -M`
# and read perf's metric-value directly. Zen5 keeps its raw-event path above.
_ZEN4_METRICS = [
    "frontend_bound_latency",
    "frontend_bound_bandwidth",
    "retiring_microcode",
]

# Always-on memory-hierarchy metricgroups. perf computes these from AMD's shipped
# JSON event definitions, so the values are AMD's own (reused, not fabricated).
# `l1_dcache` → L1D fill counts by source (incl. from-DRAM), `l2_cache` → L2
# access/hit/miss counts (all + data-side), `tlb` → L1/L2 dTLB miss counts.
# These run for every family — the memory hierarchy matters regardless of TMA.
_MEM_METRICS = ["l1_dcache", "l2_cache", "tlb"]
# L3 is requested in a SEPARATE best-effort run: the l3_cache group uses L3/XI
# uncore events that fail to parse on some perf builds, and a single bad event in
# a -M list aborts the whole perf invocation. Keeping it apart protects L1/L2/TLB.
_MEM_METRICS_L3 = ["l3_cache"]
# Page-size dTLB reload events (raw AMD events). The tlb metricgroup gives the
# page-walk count (l2_dtlb_misses) but not the page-size breakdown; these reveal
# whether 2M/1G huge pages (THP) are actually backing the working set.
_MEM_EVENTS = [
    "ls_l1_d_tlb_miss.tlb_reload_4k_l2_hit",
    "ls_l1_d_tlb_miss.tlb_reload_2m_l2_hit",
    "ls_l1_d_tlb_miss.tlb_reload_1g_l2_hit",
]

def _is_zen4(family: str | int | None) -> bool:
    f = _normalize_family(family)
    return f == 25  # 0x19

def _want_zen4_metrics(family) -> bool:
    """Use AMD's perf-defined TMA L2 metrics for the Zen4 sub-splits. Gated to
    Family 19h so we never shadow the Zen5 raw-event path. Best-effort: if the
    running perf lacks these metric defs the collect falls back to -e only and the
    sub-splits stay 'n/a' (same as before), never a hard failure."""
    return _is_zen4(family)

def _collect_perf(pid: int | None, duration: float) -> dict:
    """Run perf stat in attach (-p) or system-wide (-a) mode, return raw event
    dict. The event set is family-gated off the metric YAMLs (Seam 2); Zen5 raw
    sub-split events are added on Family 1Ah for accurate Fetch/Heavy splits."""
    _, family = _cpu_info()
    events = _base_events(family)
    if _want_zen5_extras(family):
        events = events + _ZEN5_EXTRA
    events = events + _MEM_EVENTS          # always-on dTLB page-size reloads
    evlist = ",".join(events)

    # -M list: Zen4 TMA L2 sub-splits (family-gated) + memory-hierarchy groups
    # (always-on). L3 is deliberately excluded here (separate best-effort below).
    mgroups = (list(_ZEN4_METRICS) if _want_zen4_metrics(family) else []) + list(_MEM_METRICS)

    def _build(with_metrics: bool) -> list[str]:
        cmd = ["perf", "stat", "-j", "-e", evlist]
        if with_metrics and mgroups:
            cmd += ["-M", ",".join(mgroups)]
        cmd += (["-p", str(pid)] if pid else ["-a"])
        cmd += ["--", "sleep", str(int(duration))]
        return cmd

    def _parse(stdout: str) -> dict:
        raw: dict = {}
        for line in stdout.splitlines():
            if not line.strip().startswith("{"): continue
            try:
                obj = json.loads(line)
                ev  = (obj.get("event") or "").strip()
                val_s = (obj.get("counter-value") or "0").replace(",","").strip()
                if val_s in ("<not counted>", "<not supported>", ""): val_s = "0"
                if ev:
                    raw[ev] = float(val_s)
                mval = obj.get("metric-value", "")
                munit = (obj.get("metric-unit") or "").strip()
                if mval:
                    fval = None
                    try: fval = float(str(mval).replace(",",""))
                    except ValueError: pass
                    if fval is not None:
                        if ev:
                            raw[ev + "__metric"] = fval
                        # TMA L2 metrics render as "%  <metric_name>"; cache/TLB
                        # metricgroups render the metric name alone (a count). Key
                        # both by the trailing name so _metrics_from_raw can read
                        # perf's own AMD-defined values rather than approximate them.
                        if "  " in munit:
                            raw["__M__" + munit.split("  ", 1)[1].strip()] = fval
                        elif munit:
                            raw["__M__" + munit.strip()] = fval
            except Exception:
                pass
        return raw

    want_m = bool(mgroups)
    p = subprocess.run(_build(want_m), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, timeout=duration + 30)
    raw = _parse(p.stdout)
    # Safety net: if the -M form failed (old perf lacking these metric defs),
    # perf may bail before counting the -e events. Re-run -e only so the core
    # funnel is never lost — the sub-splits/cache metrics simply stay 'n/a'.
    if want_m and (p.returncode != 0 or "ls_not_halted_cyc" not in raw):
        p2 = subprocess.run(_build(False), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=duration + 30)
        raw = _parse(p2.stdout)

    # Best-effort L3 (separate short sample). The l3_cache group uses L3/XI uncore
    # events that error on some perf builds; isolating it means a failure leaves L3
    # as 'n/a' without disturbing the L1/L2/TLB metrics already collected above.
    try:
        l3cmd = ["perf", "stat", "-j", "-M", ",".join(_MEM_METRICS_L3)]
        l3cmd += (["-p", str(pid)] if pid else ["-a"])
        l3cmd += ["--", "sleep", "1"]
        pl = subprocess.run(l3cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=30)
        for k, v in _parse(pl.stdout).items():
            if k.startswith("__M__") and k not in raw:
                raw[k] = v
    except Exception:
        pass
    return raw

def _find_pid(process_name: str) -> int | None:
    try:
        out = subprocess.check_output(["pgrep", "-x", process_name], text=True)
        pids = [int(x) for x in out.split() if x.strip()]
        return pids[0] if pids else None
    except Exception:
        return None

def _mem_metrics_from_raw(raw: dict) -> dict:
    """Derive the memory-hierarchy view (L1D / L2 / dTLB, best-effort L3) from
    AMD's own perf metric counts in `raw` (keys `__M__<amd_metric_name>` produced
    by the cache/TLB metricgroups) plus the raw page-size dTLB reload events.

    Every value here is an AMD-defined hardware count or a ratio of two such
    counts — nothing is approximated or invented. Any metric whose inputs were
    not collected (e.g. L3 on a perf build that can't parse the group) is left as
    None and renders as 'n/a', exactly like the TMA sub-splits.

    This is the layer that makes THP's effect visible: a random-access workload
    can show an essentially unchanged TMA funnel while its dTLB page-walk count
    (L2 dTLB misses) collapses by orders of magnitude under huge pages, and the
    2M/1G reload share rises — none of which appears in Retiring%/IPC."""
    def g(name):
        return raw.get("__M__" + name)

    out: dict = {}

    # ── L1 data cache: fills (= L1D misses) and how many reach DRAM ──
    l1_total = g("all_l1_data_cache_fills")
    l1_mem   = g("l1_data_cache_fills_from_memory")          # serviced from DRAM
    out["L1D_Fills_Total"]      = l1_total
    out["L1D_Fills_From_Mem"]   = l1_mem
    out["L1D_Fill_From_Mem_Pct"] = (round(_safe_div(l1_mem, l1_total) * 100, 2)
                                    if l1_total not in (None, 0) and l1_mem is not None
                                    else None)

    # ── L2: overall and data-side hit rates ──
    l2_acc  = g("all_l2_cache_accesses")
    l2_hit  = g("all_l2_cache_hits")
    l2_miss = g("all_l2_cache_misses")
    out["L2_Accesses"] = l2_acc
    out["L2_Hits"]     = l2_hit
    out["L2_Misses"]   = l2_miss
    out["L2_Hit_Rate"] = (round(_safe_div(l2_hit, l2_acc) * 100, 2)
                          if l2_acc not in (None, 0) and l2_hit is not None else None)
    dc_hit  = g("l2_cache_hits_from_l1_dc_miss")
    dc_miss = g("l2_cache_misses_from_l1_dc_miss")
    out["L2_DC_Hits"]   = dc_hit
    out["L2_DC_Misses"] = dc_miss
    if dc_hit is not None and dc_miss is not None and (dc_hit + dc_miss) > 0:
        out["L2_DC_Hit_Rate_Mem"] = round(dc_hit / (dc_hit + dc_miss) * 100, 2)
    else:
        out["L2_DC_Hit_Rate_Mem"] = None

    # ── dTLB: L1 misses, page-walks (L2 dTLB misses) and page-size reloads ──
    l1_dtlb = g("l1_dtlb_misses")
    walks   = g("l2_dtlb_misses")          # L1 dTLB miss that also misses L2 TLB = walk
    out["dTLB_L1_Misses"] = l1_dtlb
    out["dTLB_Walks"]     = walks          # headline THP metric
    out["dTLB_Walk_Rate"] = (round(_safe_div(walks, l1_dtlb) * 100, 2)
                             if l1_dtlb not in (None, 0) and walks is not None else None)
    r4k = raw.get("ls_l1_d_tlb_miss.tlb_reload_4k_l2_hit")
    r2m = raw.get("ls_l1_d_tlb_miss.tlb_reload_2m_l2_hit")
    r1g = raw.get("ls_l1_d_tlb_miss.tlb_reload_1g_l2_hit")
    out["dTLB_Reload_4K"] = r4k
    out["dTLB_Reload_2M"] = r2m
    out["dTLB_Reload_1G"] = r1g
    if None not in (r4k, r2m, r1g) and (r4k + r2m + r1g) > 0:
        out["Huge_Page_Reload_Pct"] = round((r2m + r1g) / (r4k + r2m + r1g) * 100, 2)
    else:
        out["Huge_Page_Reload_Pct"] = None

    # ── L3 (best-effort; usually n/a if the l3_cache group failed) ──
    l3_acc  = g("l3_cache_accesses")
    l3_miss = g("l3_misses")
    out["L3_Accesses"] = l3_acc
    out["L3_Misses"]   = l3_miss
    if l3_acc not in (None, 0) and l3_miss is not None:
        out["L3_Hit_Rate"] = round((1 - _safe_div(l3_miss, l3_acc)) * 100, 2)
    else:
        out["L3_Hit_Rate"] = None
    return out

def _metrics_from_raw(raw: dict) -> dict:
    """Compute TMA metrics. The L1 funnel + backend/branch/L2 math comes from the
    shared engine (amd_perf_html_report.calculate_metrics) — one source of truth.
    Fetch_Latency/Bandwidth and Light/Heavy are computed from real Zen5 events
    when present, and left as None (rendered 'n/a') otherwise — never fabricated."""
    if _ENGINE is not None:
        em = _ENGINE.calculate_metrics(raw)
        fe_pct  = em.get("Frontend Bound %", 0.0)
        be_pct  = em.get("Backend Bound %", 0.0)
        bad_pct = max(em.get("Bad Speculation %", 0.0), 0.0)
        ret_pct = em.get("Retiring %", 0.0)
        be_mem  = em.get("Backend Memory Bound %", 0.0)
        be_cpu  = em.get("Backend CPU Bound %", 0.0)
        misp_rt = em.get("Branch Misprediction Rate %", 0.0)
        ipc     = em.get("IPC", 0.0)
        eff_ghz = em.get("CPU Operating Frequency (GHz)", 0.0)
        l2_hit  = em.get("L2 Data Cache Hit Rate %", 0.0)
        cpus_u  = em.get("CPUs Utilized (abs)", 0.0)
    else:
        # Engine absent: compute the core funnel inline (same formulas, fallback).
        frontend   = raw.get("de_no_dispatch_per_slot.no_ops_from_frontend", 0)
        backend    = raw.get("de_no_dispatch_per_slot.backend_stalls", 0)
        dispatched = raw.get("de_src_op_disp.all", 0)
        retired    = raw.get("ex_ret_ops", 0)
        cycles     = raw.get("ls_not_halted_cyc", 1)
        load_nc    = raw.get("ex_no_retire.load_not_complete", 0)
        not_comp   = raw.get("ex_no_retire.not_complete", 1)
        misp       = raw.get("ex_ret_brn_misp", 0)
        branches   = raw.get("ex_ret_brn", 1)
        cpu_cyc    = raw.get("cpu-cycles", 1)
        instr      = raw.get("instructions", 0)
        l2_dchit   = raw.get("l2_cache_req_stat.dc_hit_in_l2", 0)
        l2_dcmiss  = raw.get("l2_cache_req_stat.ls_rd_blk_c", 0)
        task_ms    = raw.get("task-clock", 1)
        cpus_u     = raw.get("task-clock__metric", 0)
        total_slots = cycles * 6
        fe_pct  = _safe_div(frontend, total_slots) * 100
        be_pct  = _safe_div(backend,  total_slots) * 100
        bad_pct = max(_safe_div(dispatched - retired, total_slots) * 100, 0.0)
        ret_pct = _safe_div(retired,  total_slots) * 100
        mem_ratio = _safe_div(load_nc, not_comp)
        be_mem  = be_pct * mem_ratio
        be_cpu  = be_pct * (1 - mem_ratio)
        misp_rt = _safe_div(misp, branches) * 100
        ipc     = _safe_div(instr, cpu_cyc)
        eff_ghz = _safe_div(cpu_cyc, task_ms * 1e6)
        l2_hit  = _safe_div(l2_dchit, l2_dchit + l2_dcmiss) * 100

    # ── Accurate sub-splits from real Zen5 events (None if not measured) ──
    fe_lat_pct = fe_bw_pct = None
    fe_lat_edge = raw.get("fe_lat_edge", 0)
    fe_stall_cyc = raw.get("fe_stall_cyc", 0)
    if fe_stall_cyc > 0:
        # table58 formula: fe_latency share = 8 * edges / stall_cycles, bounded.
        lat_frac = min(1.0, _safe_div(8 * fe_lat_edge, fe_stall_cyc))
        fe_lat_pct = round(fe_pct * lat_frac, 2)
        fe_bw_pct  = round(fe_pct - fe_lat_pct, 2)

    light_pct = heavy_pct = None
    mc_ret = raw.get("mc_ret", 0)
    retired_ops = raw.get("ex_ret_ops", 0)
    if retired_ops > 0 and ("mc_ret" in raw):
        mc_frac = min(1.0, _safe_div(mc_ret, retired_ops))
        heavy_pct = round(ret_pct * mc_frac, 2)        # microcode / complex
        light_pct = round(ret_pct - heavy_pct, 2)      # fast single-cycle ops

    # ── Zen4 (Family 19h): AMD's own perf TMA L2 metrics fill the sub-splits ──
    # perf computes frontend_bound_latency/_bandwidth and retiring_microcode from
    # the shipped AMD JSON defs (see _ZEN4_METRICS); we read those values rather
    # than approximate them. Only used when the Zen5 raw path above left them None.
    if fe_lat_pct is None:
        m_lat = raw.get("__M__frontend_bound_latency")
        m_bw  = raw.get("__M__frontend_bound_bandwidth")
        if m_lat is not None and m_bw is not None:
            fe_lat_pct = round(m_lat, 2)
            fe_bw_pct  = round(m_bw, 2)
    if light_pct is None:
        m_uc = raw.get("__M__retiring_microcode")
        if m_uc is not None:
            heavy_pct = round(m_uc, 2)                  # microcode (AMD metric)
            light_pct = round(ret_pct - heavy_pct, 2)  # fast single-cycle ops

    result = {
        "Frontend_Bound":     round(fe_pct, 2),
        "Fetch_Latency":      fe_lat_pct,
        "Fetch_Bandwidth":    fe_bw_pct,
        "Backend_Bound":      round(be_pct, 2),
        "Memory_Bound":       round(be_mem, 2),
        "Core_Bound":         round(be_cpu, 2),
        "Bad_Speculation":    round(bad_pct, 2),
        "Branch_Mispredicts": round(misp_rt, 2),
        "Machine_Clears":     0.0,   # not separately counted in this event set
        "Retiring":           round(ret_pct, 2),
        "Light_Operations":   light_pct,
        "Heavy_Operations":   heavy_pct,
        "IPC":                round(ipc, 3),
        "Effective_Freq_GHz": round(eff_ghz, 3),
        "L2_DC_Hit_Rate":     round(l2_hit, 2),
        "CPUs_Utilized":      round(cpus_u, 3),
        "Branch_Misp_Rate":   round(misp_rt, 2),
    }
    # Always-on memory-hierarchy view (L1D/L2/dTLB, best-effort L3). Merged into
    # the metrics dict so it persists to the DB and surfaces in report + compare.
    result.update(_mem_metrics_from_raw(raw))
    return result

# ─────────────────────────────────────────────────────────────────────────────
# AMD TMA Knowledge Base — explain subcommand
# ─────────────────────────────────────────────────────────────────────────────

KNOWLEDGE: dict[str, dict] = {
    "Frontend_Bound": {
        "full_name": "Frontend_Bound",
        "parent": None,
        "desc": (
            "The frontend could not supply enough instructions to keep the backend busy. "
            "Every slot here is a cycle where the CPU was starved of work before it even "
            "started executing. On AMD Zen4/5 the frontend is 6-wide; stalls here are "
            "especially costly because no useful work was dispatched at all."
        ),
        "causes": [
            "Instruction cache (ICache) misses — working set of code exceeds the 32 KB L1I",
            "ITLB misses — large or scattered code footprint",
            "Branch misprediction resteers — pipeline flushed, frontend refills from new PC",
            "Fetch bandwidth saturation — very dense instruction streams, many short loops",
        ],
        "amd_hints": [
            "On Zen5 (EPYC 9R45/Turin), the ICache is 64 KB; code that fits in 64 KB avoids L1I misses",
            "Profile-guided optimization (PGO) reduces code scatter — run `clang -fprofile-use`",
            "Place hot code near cold code with `__attribute__((section))` or linker scripts",
            "For Redis/Valkey: jemalloc and YJIT both inflate code footprint — measure ICache miss separately",
            "Bolt or LLVM's `-z ifunc-noplt` can improve code layout for large binaries",
        ],
        "perf_events": [
            "de_no_dispatch_per_slot.no_ops_from_frontend   # stall slots",
            "l2_cache_req_stat.ic_fill_miss                 # ICache L2 misses",
            "l2_cache_req_stat.ic_hit_in_l2                 # ICache L2 hits",
        ],
    },
    "Fetch_Latency": {
        "full_name": "Frontend_Bound.Fetch_Latency",
        "parent": "Frontend_Bound",
        "desc": (
            "The fetch unit could not deliver instructions because of a latency event: "
            "a branch resteer, an ICache miss, or an ITLB miss. This is the dominant "
            "frontend stall on most real workloads and is distinct from Fetch_Bandwidth, "
            "which is a throughput-limit even when the cache is warm."
        ),
        "causes": [
            "Branch mispredictions followed by a pipeline flush and refetch from the correct PC",
            "ICache cold misses on first access to code pages",
            "ITLB misses — the TLB for instruction pages is small (~256 entries on Zen5)",
            "Instruction page faults (first-time access)",
        ],
        "amd_hints": [
            "Branch mispredicts show up here AND in Bad_Speculation — check both",
            "Huge pages for text segments (`madvise(MADV_HUGEPAGE)`) can eliminate ITLB pressure",
            "On Zen5 the ITLB is 64 fully associative entries (L1) + larger L2 ITLB; scattered calls thrash it",
            "For Redis: the command dispatch table is a hot branch prediction target — keep it cache-hot",
        ],
        "perf_events": [
            "ex_ret_brn_misp   # mispredictions → resteers",
            "ex_ret_brn        # total branches",
        ],
    },
    "Fetch_Bandwidth": {
        "full_name": "Frontend_Bound.Fetch_Bandwidth",
        "parent": "Frontend_Bound",
        "desc": (
            "The fetch unit can deliver instructions but cannot keep the decode/dispatch "
            "pipeline full — a throughput limit even when instruction cache is warm. "
            "Typically seen with very dense loops where the decoder itself becomes the bottleneck."
        ),
        "causes": [
            "Very tight loops where all 6 decode slots cannot be filled every cycle",
            "Instruction alignment issues — fetch crosses a cache-line boundary",
            "Legacy decode paths (MSROM) consuming bandwidth",
        ],
        "amd_hints": [
            "Loop alignment (`-falign-loops=64`) helps when the loop body crosses a cache line",
            "AVX-512 / complex instructions widen decode but each takes multiple decode slots",
            "Rare on typical server workloads — if Frontend is dominated by Bandwidth not Latency, check loop structure",
        ],
        "perf_events": [
            "de_no_dispatch_per_slot.no_ops_from_frontend   # frontend stall slots",
        ],
    },
    "Backend_Bound": {
        "full_name": "Backend_Bound",
        "parent": None,
        "desc": (
            "The execution backend could not retire work even though instructions were "
            "available. Split into Memory_Bound (waiting for data) and Core_Bound "
            "(execution unit contention). On server workloads Backend_Bound is usually "
            "dominated by Memory_Bound."
        ),
        "causes": [
            "Cache misses propagating to L3 or DRAM",
            "Execution unit port contention (Core_Bound)",
            "Store buffer / load buffer pressure",
        ],
        "amd_hints": [
            "Drill into Memory_Bound vs Core_Bound first — they have different fixes",
            "High Backend on Zen4/5 with low Memory is unusual; check for div/sqrt chains (Core_Bound)",
            "On CCD-scale runs: cross-CCD access adds ~100 ns latency — use numactl or taskset to keep data local",
        ],
        "perf_events": [
            "de_no_dispatch_per_slot.backend_stalls",
            "ex_no_retire.load_not_complete",
            "ex_no_retire.not_complete",
        ],
    },
    "Memory_Bound": {
        "full_name": "Backend_Bound.Memory_Bound",
        "parent": "Backend_Bound",
        "desc": (
            "Backend stalls caused by memory system latency — loads that could not complete "
            "because data was not in the cache. On EPYC this includes L3 misses, DRAM accesses, "
            "and cross-CCD NUMA traffic. This is the most common backend bottleneck for "
            "data-intensive workloads like Redis, Postgres, and key-value stores."
        ),
        "causes": [
            "Working set exceeds L3 cache (32 MB per CCD on Genoa, 96 MB on Genoa-X with V-Cache)",
            "Random access patterns with poor spatial locality",
            "Pointer chasing (linked lists, hash table open addressing with collisions)",
            "Cross-CCD or cross-NUMA memory traffic on multi-socket or large EPYC dies",
        ],
        "amd_hints": [
            "Genoa-X (9684X) has 96 MB L3 per CCD — 3D V-Cache; if Memory_Bound drops on this vs Genoa, working set fits",
            "Use `numactl --membind=0` to keep allocations on local NUMA node",
            "Transparent Huge Pages reduce TLB pressure: `echo always > /sys/kernel/mm/transparent_hugepage/enabled`",
            "For Redis: `activerehashing yes` and smaller hash tables reduce pointer-chasing",
            "Software prefetch (`__builtin_prefetch`) helps pointer-chasing patterns if access pattern is predictable",
            "Check L2 DC hit rate — Zen4/5 has 1 MB L2 per core (4x Intel); high L2 hits are good",
        ],
        "perf_events": [
            "ex_no_retire.load_not_complete   # loads stalling retirement",
            "ex_no_retire.not_complete        # all stall events (denominator)",
            "l2_cache_req_stat.ls_rd_blk_c   # L2 misses → L3/DRAM",
            "l2_cache_req_stat.dc_hit_in_l2  # L2 hits",
        ],
    },
    "Core_Bound": {
        "full_name": "Backend_Bound.Core_Bound",
        "parent": "Backend_Bound",
        "desc": (
            "Backend stalls caused by execution unit contention — the data arrived but "
            "there was no available execution port to process it. Seen with dividers, "
            "square roots, and workloads with tight serial dependency chains."
        ),
        "causes": [
            "Division-heavy code (integer or floating point dividers are not fully pipelined)",
            "Long dependency chains where each instruction waits for the previous result",
            "Port contention when many instructions compete for the same execution unit",
            "Memory barrier instructions serializing the pipeline",
        ],
        "amd_hints": [
            "Replace divisions by constants with multiplication by reciprocal (compiler does this at -O2)",
            "Unroll loops to break dependency chains and expose instruction-level parallelism",
            "On Zen5: the ALU is 6-wide with improved throughput for integer ops vs Zen4",
            "If Core_Bound > 5% with Memory_Bound < 5%, profile at instruction level with `perf record -e cycles:pp`",
        ],
        "perf_events": [
            "de_no_dispatch_per_slot.backend_stalls",
            "ex_no_retire.not_complete",
        ],
    },
    "Bad_Speculation": {
        "full_name": "Bad_Speculation",
        "parent": None,
        "desc": (
            "Slots that were consumed by instructions that were later squashed — "
            "branch mispredictions and machine clears. Work was done by the CPU but "
            "the results were thrown away. On Zen4/5 the branch predictor is excellent "
            "so values above 5% usually indicate a genuinely hard-to-predict pattern."
        ),
        "causes": [
            "Data-dependent branches (e.g., parsing variable-length data)",
            "Indirect branches to many possible targets (virtual dispatch, function pointers)",
            "Machine clears from memory ordering violations (false sharing, self-modifying code)",
        ],
        "amd_hints": [
            "AMD Zen5 has a 3-cycle misprediction penalty for simple branches — very low vs Intel",
            "For indirect branches: sort dispatch tables by frequency so the common case predicts correctly",
            "Use `__builtin_expect` to hint the branch predictor for very biased conditions",
            "Redis command dispatch: move high-frequency commands first in the command table",
            "Machine clears are rare; if seen, look for aliased stores or self-modifying JIT code",
        ],
        "perf_events": [
            "ex_ret_brn_misp   # mispredicted branches retired",
            "ex_ret_brn        # all branches retired",
            "cpu/event=0x9f,umask=0x01/   # pipeline restarts (Zen5 raw)",
        ],
    },
    "Retiring": {
        "full_name": "Retiring",
        "parent": None,
        "desc": (
            "The fraction of pipeline slots that completed useful work. Higher is better. "
            "A well-optimized workload on modern silicon typically retires 30–45% of slots; "
            "the rest is structural overhead. On AMD Zen4/5 the 6-wide dispatch model means "
            "100% retiring would require 6 independent micro-ops every cycle — rare in practice."
        ),
        "causes": [
            "Higher Retiring = more useful work per cycle",
            "Light_Operations = simple single-cycle ops (adds, loads that hit L1, shifts)",
            "Heavy_Operations ≈ microcode assists, complex ops taking multiple slots",
        ],
        "amd_hints": [
            "Use retiring as the 'score' — after tuning, Retiring should go up and Frontend/Backend down",
            "IPC + Retiring together tell the full story: high IPC + low Retiring = fast but infrequent ops",
            "On Zen5: Light_Operations benefit from the improved 6-wide integer execution",
        ],
        "perf_events": [
            "ex_ret_ops                      # ops retired",
            "ls_not_halted_cyc               # cycles (denominator)",
            "cpu/event=0x1c2/                # microcode ops retired (Zen5 raw)",
        ],
    },
    "IPC": {
        "full_name": "IPC",
        "parent": None,
        "desc": (
            "Instructions Per Cycle — the throughput of the pipeline. Higher is better "
            "but must be read alongside Retiring: IPC measures executed instructions, not "
            "pipeline slot utilization. On Zen4/5 peak theoretical IPC is 6 (one per slot)."
        ),
        "causes": ["Low IPC: memory stalls, mispredictions, frontend stalls",
                   "High IPC: data-parallel, cache-hot, branch-predictable code"],
        "amd_hints": [
            "Genoa (Zen4): typical server workload IPC is 1.5–3.5",
            "Turin (Zen5): IPC improvements of 10–17% over Zen4 at same frequency",
            "If IPC > 4: excellent — usually memory-local, compute-bound code",
            "If IPC < 1.0: likely severe memory stalls or mispredictions — check Backend_Bound",
        ],
        "perf_events": ["instructions", "cpu-cycles"],
    },
    "L2_DC_Hit_Rate": {
        "full_name": "L2_DC_Hit_Rate",
        "parent": "Memory_Bound",
        "desc": (
            "Fraction of L1D cache misses that are satisfied by the L2 cache (1 MB per core "
            "on Zen4/Zen5). A high hit rate means the working set fits in L2 and avoids "
            "the more expensive L3 or DRAM trips."
        ),
        "causes": ["Low hit rate: working set per thread > 1 MB", "High hit rate: well-locality-structured access"],
        "amd_hints": [
            "Zen4/5 has 1 MB L2 per core — 4x Intel's 256 KB. Workloads that thrash Intel L2 may be fine on EPYC.",
            "Redis per-thread working set in MGET mode is typically 256–512 KB — fits in Zen L2",
            "If L2 hit rate drops below 70%, look at data structure layout and access patterns",
        ],
        "perf_events": [
            "l2_cache_req_stat.dc_hit_in_l2   # L2 hits from L1D misses",
            "l2_cache_req_stat.ls_rd_blk_c    # L2 misses → L3/DRAM",
        ],
    },
    "Effective_Freq_GHz": {
        "full_name": "Effective_Freq_GHz",
        "parent": None,
        "desc": (
            "The actual operating frequency during the workload, measured via perf counters "
            "(cpu-cycles / task-clock), not the static base or boost clock. This reflects "
            "turbo boost, thermal throttling, and power capping effects."
        ),
        "causes": ["Low freq: thermal throttling, power cap, idle threads"],
        "amd_hints": [
            "EPYC Turin base clock is 2.2–3.7 GHz depending on SKU; boost to 4.5+ GHz for lightly-loaded CCDs",
            "On AWS M8A, the effective frequency is typically 3.0–3.6 GHz under full load",
            "If frequency drops > 20% under load, check `turbostat` or `/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq`",
        ],
        "perf_events": ["cpu-cycles", "task-clock"],
    },
}

def _find_metric(name: str) -> dict | None:
    """Case-insensitive prefix match against knowledge base."""
    name_lower = name.lower().replace("-", "_")
    for k, v in KNOWLEDGE.items():
        if k.lower() == name_lower or v["full_name"].lower() == name_lower:
            return v
    for k, v in KNOWLEDGE.items():
        if k.lower().startswith(name_lower) or name_lower in k.lower():
            return v
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Rendering helpers
# ─────────────────────────────────────────────────────────────────────────────

_W = 67   # table width

def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, int(round((pct or 0) / 100 * width))))
    return "█" * filled + "░" * (width - filled)

def _pct(v) -> str:
    """Format a percentage cell; show 'n/a' when the metric wasn't measured."""
    return "  n/a " if v is None else f"{v:5.1f}%"

def _hcount(v) -> str:
    """Human-format a raw event count (K/M/B); 'n/a' when not measured."""
    if v is None:
        return "n/a"
    v = float(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v/div:,.2f}{suf}"
    return f"{v:,.0f}"

def _render_mem_hierarchy(m: dict) -> list[str]:
    """Always-on memory-hierarchy block: L1D/L2/dTLB (+best-effort L3). Surfaces
    the cache and page-walk behaviour that the TMA funnel can completely hide —
    e.g. a flat funnel while dTLB page-walks collapse under THP/huge pages."""
    def rate(v):  # percentage rates use _pct; counts use _hcount
        return _pct(v)
    lines = [
        "  " + "─" * (_W - 2),
        "  Memory Hierarchy (AMD perf counts — always measured)",
        f"  L1D fills (misses)          {_hcount(m.get('L1D_Fills_Total')):>12}",
        f"  ├── from DRAM               {_hcount(m.get('L1D_Fills_From_Mem')):>12}  ({rate(m.get('L1D_Fill_From_Mem_Pct')).strip()})",
        f"  L2 accesses                 {_hcount(m.get('L2_Accesses')):>12}",
        f"  ├── L2 hit rate (all)       {rate(m.get('L2_Hit_Rate'))}",
        f"  ├── L2 hit rate (data)      {rate(m.get('L2_DC_Hit_Rate_Mem'))}",
        f"  L3 hit rate                 {rate(m.get('L3_Hit_Rate'))}",
        f"  dTLB L1 misses              {_hcount(m.get('dTLB_L1_Misses')):>12}",
        f"  ├── page-table walks        {_hcount(m.get('dTLB_Walks')):>12}  ({rate(m.get('dTLB_Walk_Rate')).strip()} of L1 misses)",
        f"  TLB reloads  4K             {_hcount(m.get('dTLB_Reload_4K')):>12}",
        f"  ├── 2M (huge)               {_hcount(m.get('dTLB_Reload_2M')):>12}",
        f"  ├── 1G (huge)               {_hcount(m.get('dTLB_Reload_1G')):>12}",
        f"  └── huge-page reload share  {rate(m.get('Huge_Page_Reload_Pct'))}",
    ]
    return lines

def _render_funnel(metrics: dict, run_id: str = "", ts: str = "") -> str:
    m = metrics
    lines = [
        f"  Pipeline Slots (100%)        {'Run: ' + run_id[:8] if run_id else ''}  {ts[:19]}",
        "  " + "─" * (_W - 2),
        f"  Frontend_Bound              {_pct(m.get('Frontend_Bound', 0))}  {_bar(m.get('Frontend_Bound',0))}",
        f"  ├── Fetch_Latency           {_pct(m.get('Fetch_Latency'))}",
        f"  ├── Fetch_Bandwidth         {_pct(m.get('Fetch_Bandwidth'))}",
        f"  Retiring                    {_pct(m.get('Retiring', 0))}  {_bar(m.get('Retiring',0))}  ← useful work",
        f"  ├── Light_Operations        {_pct(m.get('Light_Operations'))}",
        f"  ├── Heavy_Operations        {_pct(m.get('Heavy_Operations'))}",
        f"  Backend_Bound               {_pct(m.get('Backend_Bound', 0))}  {_bar(m.get('Backend_Bound',0))}",
        f"  ├── Memory_Bound            {_pct(m.get('Memory_Bound'))}",
        f"  ├── Core_Bound              {_pct(m.get('Core_Bound'))}",
        f"  Bad_Speculation             {_pct(m.get('Bad_Speculation', 0))}  {_bar(m.get('Bad_Speculation',0))}",
        f"  ├── Branch_Mispredicts      {_pct(m.get('Branch_Mispredicts'))}",
        f"  ├── Machine_Clears         {_pct(m.get('Machine_Clears'))}",
        "  " + "─" * (_W - 2),
        f"  Useful work (Retiring)      {_pct(m.get('Retiring', 0))}",
        f"  IPC                         {m.get('IPC', 0):5.3f}",
        f"  Effective Freq (GHz)        {m.get('Effective_Freq_GHz', 0):5.3f}",
        f"  L2 DC Hit Rate              {_pct(m.get('L2_DC_Hit_Rate', 0))}",
    ]
    lines += _render_mem_hierarchy(m)
    if m.get("Fetch_Latency") is None or m.get("Light_Operations") is None:
        lines.append("  " + "─" * (_W - 2))
        lines.append("  n/a = sub-split not measured (needs Zen4 Family 19h AMD perf metrics or Zen5 raw events)")
    return "\n".join(lines)

def _render_mem_compare(a: dict, b: dict, label_a: str, label_b: str) -> list[str]:
    """Memory-hierarchy A/B block. Cache/TLB are counts and rates, not pipeline
    slots, so they get their own table with a reduction *factor* (×) for counts —
    this is where THP's effect lands: page-walks can drop by orders of magnitude
    while the slot funnel above barely moves."""
    ma = a.get("metrics", a); mb = b.get("metrics", b)
    COUNTS = [
        ("L1D_Fills_Total",   "L1D fills (misses)"),
        ("L1D_Fills_From_Mem","├── from DRAM"),
        ("dTLB_Walks",        "dTLB page-table walks"),
        ("dTLB_Reload_2M",    "TLB reloads 2M (huge)"),
    ]
    RATES = [
        ("L2_Hit_Rate",          "L2 hit rate (all)"),
        ("L2_DC_Hit_Rate_Mem",   "L2 hit rate (data)"),
        ("L3_Hit_Rate",          "L3 hit rate"),
        ("dTLB_Walk_Rate",       "dTLB walk rate"),
        ("Huge_Page_Reload_Pct", "huge-page reload share"),
    ]
    hdr_a = label_a[:12].center(12); hdr_b = label_b[:12].center(12)
    lines = ["", "  Memory Hierarchy (AMD perf counts)",
             f"  {'':<28} {hdr_a} {hdr_b}   Factor / Delta",
             "  " + "─" * (_W + 6)]
    for key, label in COUNTS:
        va = ma.get(key); vb = mb.get(key)
        if va is None or vb is None:
            fac = "n/a"
        elif va == 0 or vb == 0:
            fac = "—"
        elif vb < va:
            fac = f"÷{va/vb:,.0f}  (B fewer)"
        else:
            fac = f"×{vb/va:,.1f}  (B more)"
        lines.append(f"  {label:<28} {_hcount(va):>12} {_hcount(vb):>12}   {fac}")
    for key, label in RATES:
        va = ma.get(key); vb = mb.get(key)
        ca = _pct(va); cb = _pct(vb)
        dl = "n/a" if (va is None or vb is None) else f"{vb-va:+.1f}pp"
        lines.append(f"  {label:<28} {ca:>12} {cb:>12}   {dl}")
    return lines

def _render_compare(a: dict, b: dict,
                    label_a: str = "A", label_b: str = "B") -> str:
    KEYS = [
        ("Frontend_Bound",    "Frontend_Bound"),
        ("Fetch_Latency",     "├── Fetch_Latency"),
        ("Fetch_Bandwidth",   "├── Fetch_Bandwidth"),
        ("Retiring",          "Retiring"),
        ("Light_Operations",  "├── Light_Operations"),
        ("Heavy_Operations",  "├── Heavy_Operations"),
        ("Backend_Bound",     "Backend_Bound"),
        ("Memory_Bound",      "├── Memory_Bound"),
        ("Core_Bound",        "├── Core_Bound"),
        ("Bad_Speculation",   "Bad_Speculation"),
        ("Branch_Mispredicts","├── Branch_Mispredicts"),
        ("Machine_Clears",    "├── Machine_Clears"),
    ]
    hdr_a = label_a[:10].center(10)
    hdr_b = label_b[:10].center(10)
    lines = [
        f"  Pipeline Slots (100%)               {hdr_a}   {hdr_b}   Delta",
        "  " + "─" * (_W + 6),
    ]
    improvements, regressions = [], []
    for key, label in KEYS:
        va = a.get("metrics", a).get(key, 0.0)
        vb = b.get("metrics", b).get(key, 0.0)
        if va is None or vb is None:
            # Sub-split not measured on one/both runs — show without a fake delta.
            ca = "  n/a " if va is None else f"{va:6.1f}%"
            cb = "  n/a " if vb is None else f"{vb:6.1f}%"
            lines.append(f"  {label:<28} {ca}   {cb}      n/a")
            continue
        delta = vb - va
        sign  = "+" if delta >= 0 else ""
        ann   = ""
        if abs(delta) >= 0.3:
            if key in ("Retiring",):
                ann = "← more useful work" if delta > 0 else "← less useful work"
                (improvements if delta > 0 else regressions).append((label, delta))
            elif key in ("Frontend_Bound", "Fetch_Latency", "Backend_Bound",
                         "Memory_Bound", "Bad_Speculation"):
                ann = "← B wins" if delta < 0 else "← regression"
                (improvements if delta < 0 else regressions).append((label, delta))
        lines.append(
            f"  {label:<28} {va:6.1f}%   {vb:6.1f}%   {sign}{delta:+.1f}%  {ann}"
        )
    ipc_a = a.get('metrics', a).get('IPC', 0.0) or 0.0
    ipc_b = b.get('metrics', b).get('IPC', 0.0) or 0.0
    ipc_factor = (f"×{ipc_b/ipc_a:.2f}" if ipc_a else "n/a")
    lines += [
        "  " + "─" * (_W + 6),
        f"  Useful work (Retiring)              "
        f"{a.get('metrics',a).get('Retiring',0):6.1f}%   "
        f"{b.get('metrics',b).get('Retiring',0):6.1f}%   "
        f"{b.get('metrics',b).get('Retiring',0)-a.get('metrics',a).get('Retiring',0):+.1f}%",
        f"  IPC (instructions/cycle)            "
        f"{ipc_a:6.3f}    {ipc_b:6.3f}    {ipc_factor}",
    ]
    lines += _render_mem_compare(a, b, label_a, label_b)
    if improvements:
        lines += ["", "  ✓ Improvements in B:"]
        for lbl, d in improvements:
            lines.append(f"    {lbl.strip()}: {d:+.1f}%")
    if regressions:
        lines += ["", "  ✗ Regressions in B:"]
        for lbl, d in regressions:
            lines.append(f"    {lbl.strip()}: {d:+.1f}%")
    return "\n".join(lines)

def _render_explain(info: dict) -> str:
    w = _W
    def box(title: str, items: list[str]) -> str:
        top    = "  ╭" + "─" * (w - 2) + "╮"
        mid    = f"  │ {title:<{w-4}} │"
        sep    = "  │" + " " * (w - 2) + "│"
        rows   = [f"  │   {'  ' + ln if ln else '':<{w-4}} │" for ln in items]
        bottom = "  ╰" + "─" * (w - 2) + "╯"
        return "\n".join([top, mid, sep] + rows + [bottom])

    desc_lines = textwrap.wrap(info["desc"], width=w - 8)
    sections = [
        box(f"Description — {info['full_name']}", desc_lines),
        box("Typical Causes", ["- " + c for c in info["causes"]]),
        box("AMD EPYC Tuning Hints", ["• " + h for h in info["amd_hints"]]),
    ]
    if info.get("perf_events"):
        sections.append(box("Relevant perf Events", info["perf_events"]))
    return "\n\n".join(sections)

# ─────────────────────────────────────────────────────────────────────────────
# Subcommands
# ─────────────────────────────────────────────────────────────────────────────

def cmd_collect(args):
    """Collect a TMA run and persist it with labels."""
    labels = {}
    for lv in (args.label or []):
        if "=" in lv:
            k, v = lv.split("=", 1)
            labels[k.strip()] = v.strip()

    # Auto-detect git context
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True).strip()
        labels.setdefault("git_branch", branch)
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True).strip()
        labels.setdefault("git_hash", sha)
    except Exception:
        pass

    pid = None
    if args.process:
        pid = _find_pid(args.process)
        if pid:
            print(f"  Attaching to {args.process} (PID {pid})")
        else:
            print(f"  Warning: process '{args.process}' not found — falling back to system-wide collection")

    print(f"  Collecting {args.duration}s of TMA data ...")
    t0 = time.time()
    try:
        raw = _collect_perf(pid, args.duration)
    except subprocess.TimeoutExpired:
        print("  Error: perf stat timed out", file=sys.stderr)
        sys.exit(1)
    dur = time.time() - t0

    if not raw:
        print("  Error: no perf data collected. Check perf_event_paranoid:", file=sys.stderr)
        print("    cat /proc/sys/kernel/perf_event_paranoid", file=sys.stderr)
        print("    sudo sysctl -w kernel.perf_event_paranoid=1", file=sys.stderr)
        sys.exit(1)

    metrics = _metrics_from_raw(raw)
    run_id  = save_run(args.process or "", dur, metrics, labels, raw)

    print(f"\n  ✓ Saved run {run_id}  ({dur:.1f}s)  Labels: {labels}\n")
    print(_render_funnel(metrics, run_id=run_id, ts=datetime.now().isoformat()))
    print(f"\n  To compare: amd_topdown.py compare {run_id} <other-id>")
    print(f"  To explain: amd_topdown.py explain Frontend_Bound")

# ─────────────────────────────────────────────────────────────────────────────
# Corpus ingestion (Seam 4) — backfill the store from existing run summaries
# (pmc_test/run_pmc_tests.py *_summary.json + the m8a sweep) so that
# query / compare / funnel work over historical data, not just live runs.
# ─────────────────────────────────────────────────────────────────────────────

# Suite → (cpu_model, cpu_family) for the dirs we know about. cpu_family is the
# decimal CPUID family the rest of the tool uses (Zen4=25/0x19, Zen5=26/0x1A).
_CORPUS_SUITES = {
    "results_m8a":        ("AMD EPYC (Turin/Zen5, AWS m8a)", "26"),
    "cross_cpu_results":  ("AMD EPYC (cross-CPU corpus)",    ""),
    "results_genoa":      ("AMD EPYC 9684X (Genoa-X/Zen4)",  "25"),
    "results":           ("AMD EPYC (Zen)",                  ""),
}

def _corpus_raw_from_summary(d: dict, workload: str = "all") -> dict:
    """Flatten a run_pmc_tests summary into a raw event→value dict for one
    workload. Pulls measured counter values from the sanity / bounds /
    ppr_extras sections (skipping the programmability-only probe entries,
    which carry no 'value')."""
    raw: dict = {}
    for sect in ("sanity", "bounds", "ppr_extras"):
        for r in d.get(sect, []):
            if r.get("workload") != workload:
                continue
            v = r.get("value")
            ev = r.get("event")
            if ev is None or v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv != fv:          # NaN
                continue
            # keep the largest sample if an event repeats across sections
            if ev not in raw or fv > raw[ev]:
                raw[ev] = fv
    return raw

def _ingest_summary_file(path: Path, extra_labels: dict) -> Optional[str]:
    """Ingest one *_summary.json. Returns the run id, or None if the file
    has no measured data (e.g. a programmability-only probe run)."""
    try:
        d = json.loads(path.read_text())
    except Exception as e:
        print(f"    skip {path.name}: unreadable ({e})")
        return None
    meta = d.get("meta", {}) if isinstance(d, dict) else {}

    # Pick the workload to fund the funnel: prefer 'all', else the first
    # workload that actually has counter values.
    workloads = sorted({r.get("workload") for r in d.get("bounds", [])
                        if r.get("workload")})
    workload = "all" if "all" in workloads else (workloads[0] if workloads else "all")
    raw = _corpus_raw_from_summary(d, workload)
    if not raw or "ls_not_halted_cyc" not in raw:
        return None  # nothing to compute a funnel from

    metrics = _metrics_from_raw(raw)

    suite = path.parent.name
    cpu_model, cpu_family = _CORPUS_SUITES.get(suite, ("AMD EPYC (Zen)", ""))
    ts_raw = meta.get("ts") or path.stem.split("_")[0]
    # normalise compact stamp 20260525T084709Z → ISO-ish for sane sorting
    ts = ts_raw
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$", str(ts_raw))
    if m:
        ts = f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}+00:00"

    ccd_topo = meta.get("ccd_topology") or []
    labels = dict(extra_labels)
    labels.update({
        "source":      "corpus",
        "suite":       suite,
        "source_file": str(path),
        "workload":    workload,
        "mode":        meta.get("mode", ""),
        "host":        meta.get("host", ""),
        "ncpu":        str(meta.get("ncpu", "")),
        "ccd_count":   str(len(ccd_topo)),
    })
    return ingest_run(ts, f"corpus:{suite}", 0.0, meta.get("host", ""),
                      cpu_model, cpu_family, metrics, labels, raw)

def _discover_corpus(root: Path) -> list[Path]:
    """Find *_summary.json files under the known corpus directories."""
    dirs = ["pmc_test/results_m8a", "pmc_test/results_genoa",
            "pmc_test/results", "pmc_test/cross_cpu_results"]
    found: list[Path] = []
    for rel in dirs:
        base = root / rel
        if base.is_dir():
            found.extend(sorted(base.glob("*_summary.json")))
    return found

def cmd_ingest(args):
    """Backfill the store from an existing run-summary corpus."""
    extra_labels = {}
    for lv in (args.label or []):
        if "=" in lv:
            k, v = lv.split("=", 1)
            extra_labels[k.strip()] = v.strip()

    if args.paths:
        files: list[Path] = []
        for p in args.paths:
            pp = Path(p)
            if pp.is_dir():
                files.extend(sorted(pp.glob("*_summary.json")))
            elif pp.is_file():
                files.append(pp)
            else:
                print(f"  Warning: path not found: {p}")
    else:
        root = Path(args.root) if args.root else Path(__file__).resolve().parent
        files = _discover_corpus(root)

    if not files:
        print("  No *_summary.json files found to ingest.")
        print("  Pass paths explicitly or run from the repo root.")
        return

    print(f"  Scanning {len(files)} summary file(s) ...\n")
    ingested = skipped = 0
    for f in files:
        if args.dry_run:
            d = json.loads(f.read_text()) if f.suffix == ".json" else {}
            raw = _corpus_raw_from_summary(d)
            ok = "ls_not_halted_cyc" in raw
            print(f"    [{'OK   ' if ok else 'empty'}] {f}")
            if ok: ingested += 1
            else:  skipped += 1
            continue
        rid = _ingest_summary_file(f, extra_labels)
        if rid:
            print(f"    ✓ {f.name}  → run {rid}")
            ingested += 1
        else:
            print(f"    – {f.name}  (no measured data, skipped)")
            skipped += 1

    verb = "would ingest" if args.dry_run else "ingested"
    print(f"\n  {verb} {ingested} run(s), skipped {skipped}.")
    if not args.dry_run and ingested:
        print(f"  Now try:  amd_topdown.py list --label source=corpus")
        print(f"            amd_topdown.py query --funnel --label suite=results_m8a")

def cmd_list(args):
    label_filter = {}
    for lv in (args.label or []):
        if "=" in lv:
            k, v = lv.split("=", 1)
            label_filter[k.strip()] = v.strip()

    runs = load_runs(label_filter or None, limit=args.limit)
    if not runs:
        print("  No runs found. Run: amd_topdown.py collect ...")
        return

    header = f"  {'ID':13}  {'Timestamp':19}  {'Process':16}  {'Dur':6}  Labels"
    print(header)
    print("  " + "─" * 80)
    for r in runs:
        lab = ", ".join(f"{k}={v}" for k,v in r["labels"].items()
                        if k not in ("host","cpu","process"))
        print(f"  {r['id']:13}  {r['ts'][:19]}  {(r['process'] or ''):16}  "
              f"{r['duration_s']:5.0f}s  {lab}")

def cmd_query(args):
    label_filter = {}
    for lv in (args.label or []):
        if "=" in lv:
            k, v = lv.split("=", 1)
            label_filter[k.strip()] = v.strip()

    runs = load_runs(label_filter or None, limit=args.limit)
    if not runs:
        print("  No matching runs.")
        return

    if args.funnel:
        r = runs[0]
        print(_render_funnel(r["metrics"], run_id=r["id"], ts=r["ts"]))

    elif args.bottleneck:
        metric = args.bottleneck
        print(f"  Runs where {metric} is highest:\n")
        scored = [(r, r["metrics"].get(metric, 0)) for r in runs]
        scored.sort(key=lambda x: -x[1])
        for r, val in scored[:10]:
            lab = ", ".join(f"{k}={v}" for k,v in r["labels"].items()
                            if k not in ("host","cpu","process"))
            print(f"  {r['id'][:8]}  {val:5.1f}%  {r['ts'][:19]}  {lab}")

    elif args.bottlenecks:
        r = runs[0]
        m = r["metrics"]
        ranked = sorted(
            [(k, v) for k, v in m.items()
             if k in ("Frontend_Bound","Backend_Bound","Memory_Bound",
                      "Core_Bound","Bad_Speculation")],
            key=lambda x: -x[1]
        )
        print(f"\n  Top bottlenecks for run {r['id'][:8]}:\n")
        for k, v in ranked:
            info = KNOWLEDGE.get(k, {})
            print(f"  {k:<24} {v:5.1f}%  {_bar(v, 30)}")
            if info.get("causes"):
                print(f"             → {info['causes'][0]}")
        print()
    else:
        for r in runs[:3]:
            print(_render_funnel(r["metrics"], run_id=r["id"], ts=r["ts"]))
            print()

def cmd_compare(args):
    # Resolve run A
    if args.id_a:
        run_a = load_run_by_id(args.id_a)
        label_a = args.id_a[:8]
        if not run_a:
            print(f"  Run {args.id_a} not found", file=sys.stderr); sys.exit(1)
    else:
        filt_a = {}
        for lv in (args.label_a or []):
            if "=" in lv:
                k, v = lv.split("=", 1); filt_a[k.strip()] = v.strip()
        run_a = _best_run(filt_a)
        label_a = " ".join(f"{k}={v}" for k,v in filt_a.items()) or "A"
        if not run_a:
            print(f"  No run matching {filt_a}", file=sys.stderr); sys.exit(1)

    # Resolve run B
    if args.id_b:
        run_b = load_run_by_id(args.id_b)
        label_b = args.id_b[:8]
        if not run_b:
            print(f"  Run {args.id_b} not found", file=sys.stderr); sys.exit(1)
    else:
        filt_b = {}
        for lv in (args.label_b or []):
            if "=" in lv:
                k, v = lv.split("=", 1); filt_b[k.strip()] = v.strip()
        run_b = _best_run(filt_b)
        label_b = " ".join(f"{k}={v}" for k,v in filt_b.items()) or "B"
        if not run_b:
            print(f"  No run matching {filt_b}", file=sys.stderr); sys.exit(1)

    print()
    print(f"  Comparing:  A = {run_a['id'][:8]} ({label_a})")
    print(f"              B = {run_b['id'][:8]} ({label_b})")
    print()
    print(_render_compare(run_a, run_b, label_a=label_a, label_b=label_b))
    print()

def cmd_explain(args):
    metric = " ".join(args.metric)
    info = _find_metric(metric)
    if not info:
        available = ", ".join(KNOWLEDGE.keys())
        print(f"  Unknown metric '{metric}'. Available:\n  {available}", file=sys.stderr)
        sys.exit(1)
    print()
    print(_render_explain(info))
    print()

def cmd_topology(args):
    """Show this host's CCD / cache topology (Seam 3)."""
    topo = _ccd_topology()
    if getattr(args, "json", False):
        print(json.dumps(topo, indent=2))
    else:
        print()
        print(_render_topology(topo))
        print()

def cmd_agent(args):
    """Continuous collection daemon — collect every N seconds."""
    labels = {}
    for lv in (args.label or []):
        if "=" in lv:
            k, v = lv.split("=", 1); labels[k.strip()] = v.strip()

    interval = args.every
    duration = args.duration
    print(f"  Agent starting: process={args.process or 'system-wide'}, "
          f"every={interval}s, duration={duration}s")
    print(f"  Storage: {DB_PATH}")
    print("  Ctrl-C to stop.\n")

    def _sigint(sig, frame):
        print("\n  Agent stopped."); sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    run_count = 0
    while True:
        pid = _find_pid(args.process) if args.process else None
        if args.process and not pid:
            print(f"  [{datetime.now():%H:%M:%S}] process '{args.process}' not running — waiting ...")
            time.sleep(interval)
            continue

        print(f"  [{datetime.now():%H:%M:%S}] collecting {duration}s ...", end="", flush=True)
        try:
            raw = _collect_perf(pid, duration)
        except Exception as e:
            print(f" error: {e}")
            time.sleep(interval)
            continue

        if raw:
            metrics = _metrics_from_raw(raw)
            lbl = dict(labels)
            run_id = save_run(args.process or "", duration, metrics, lbl, raw)
            run_count += 1
            print(f" run {run_id[:8]}  retiring={metrics.get('Retiring',0):.1f}%  "
                  f"frontend={metrics.get('Frontend_Bound',0):.1f}%  "
                  f"backend={metrics.get('Backend_Bound',0):.1f}%  total={run_count}")
        else:
            print(" no data")

        sleep_remaining = interval - duration
        if sleep_remaining > 0:
            time.sleep(sleep_remaining)

# ─────────────────────────────────────────────────────────────────────────────
# MCP Server — stdio transport (JSON-RPC 2.0)
# Implements the MCP tool protocol so Claude Desktop / Claude Code can query
# profiling data directly.
# ─────────────────────────────────────────────────────────────────────────────

MCP_TOOLS = [
    {
        "name": "collect_topdown",
        "description": "Run a TMA collection on this AMD EPYC host for a given duration and store it with labels.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "process": {"type": "string", "description": "Process name to attach to (e.g. redis-server). Omit for system-wide."},
                "duration": {"type": "integer", "description": "Collection duration in seconds (default 30).", "default": 30},
                "labels": {"type": "object", "description": "Key-value labels (git_branch, test_name, topology, etc.)"},
            },
        },
    },
    {
        "name": "get_funnel",
        "description": "Return the VTune-style pipeline slot funnel for the most recent run matching the given labels.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "labels": {"type": "object", "description": "Label filters (e.g. {git_branch: 'main'})"},
                "run_id": {"type": "string", "description": "Specific run ID"},
            },
        },
    },
    {
        "name": "compare_funnel",
        "description": "Compare two runs side-by-side — show delta per TMA category.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "label_a": {"type": "object", "description": "Labels to select run A"},
                "label_b": {"type": "object", "description": "Labels to select run B"},
                "id_a": {"type": "string"},
                "id_b": {"type": "string"},
            },
        },
    },
    {
        "name": "query_bottlenecks",
        "description": "Return ranked TMA bottlenecks for the most recent run matching labels.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "labels": {"type": "object"},
                "run_id": {"type": "string"},
            },
        },
    },
    {
        "name": "explain_metric",
        "description": "Explain a TMA metric with AMD-specific causes and tuning hints.",
        "inputSchema": {
            "type": "object",
            "required": ["metric"],
            "properties": {
                "metric": {"type": "string", "description": "e.g. Frontend_Bound, Memory_Bound, Fetch_Latency"},
            },
        },
    },
    {
        "name": "list_profiling_runs",
        "description": "List recent profiling runs stored in the database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "labels": {"type": "object"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "compare_runs",
        "description": "Compare two runs by their run IDs.",
        "inputSchema": {
            "type": "object",
            "required": ["id_a", "id_b"],
            "properties": {
                "id_a": {"type": "string"},
                "id_b": {"type": "string"},
            },
        },
    },
    {
        "name": "get_ccd_topology",
        "description": "Report this host's CCD / cache topology (cores per CCD, L3 per CCD, CPU lists) "
                       "for correct per-CCD pinning. Reuses amd_cpu_placement.py.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

def _mcp_call_tool(name: str, args: dict) -> dict:
    try:
        if name == "collect_topdown":
            labels = args.get("labels", {})
            duration = int(args.get("duration", 30))
            process = args.get("process")
            pid = _find_pid(process) if process else None
            raw = _collect_perf(pid, duration)
            if not raw:
                return {"content": [{"type":"text","text":"No data collected — check perf_event_paranoid."}]}
            metrics = _metrics_from_raw(raw)
            run_id = save_run(process or "", duration, metrics, dict(labels), raw)
            text = f"Collected run {run_id} ({duration}s)\n\n" + _render_funnel(metrics, run_id=run_id)
            return {"content": [{"type":"text","text":text}]}

        elif name == "get_funnel":
            run_id = args.get("run_id")
            if run_id:
                r = load_run_by_id(run_id)
            else:
                r = _best_run(args.get("labels", {}))
            if not r:
                return {"content": [{"type":"text","text":"No matching run found."}]}
            text = _render_funnel(r["metrics"], run_id=r["id"], ts=r["ts"])
            return {"content": [{"type":"text","text":text}]}

        elif name == "compare_funnel":
            id_a, id_b = args.get("id_a"), args.get("id_b")
            run_a = load_run_by_id(id_a) if id_a else _best_run(args.get("label_a", {}))
            run_b = load_run_by_id(id_b) if id_b else _best_run(args.get("label_b", {}))
            if not run_a or not run_b:
                return {"content": [{"type":"text","text":"Could not resolve one or both runs."}]}
            label_a = id_a[:8] if id_a else str(args.get("label_a","A"))
            label_b = id_b[:8] if id_b else str(args.get("label_b","B"))
            text = _render_compare(run_a, run_b, label_a=label_a, label_b=label_b)
            return {"content": [{"type":"text","text":text}]}

        elif name == "query_bottlenecks":
            run_id = args.get("run_id")
            r = load_run_by_id(run_id) if run_id else _best_run(args.get("labels", {}))
            if not r:
                return {"content": [{"type":"text","text":"No matching run."}]}
            m = r["metrics"]
            ranked = sorted(
                [(k, m.get(k,0)) for k in
                 ("Frontend_Bound","Backend_Bound","Memory_Bound","Core_Bound","Bad_Speculation")],
                key=lambda x: -x[1]
            )
            lines = [f"Top bottlenecks for run {r['id'][:8]}:\n"]
            for k, v in ranked:
                info = KNOWLEDGE.get(k, {})
                lines.append(f"  {k:<24} {v:5.1f}%  {_bar(v, 30)}")
                if info.get("causes"):
                    lines.append(f"             → {info['causes'][0]}")
            return {"content": [{"type":"text","text":"\n".join(lines)}]}

        elif name == "explain_metric":
            info = _find_metric(args["metric"])
            if not info:
                return {"content": [{"type":"text","text":f"Unknown metric: {args['metric']}. Known: {', '.join(KNOWLEDGE.keys())}"}]}
            return {"content": [{"type":"text","text":_render_explain(info)}]}

        elif name == "list_profiling_runs":
            runs = load_runs(args.get("labels") or None, limit=args.get("limit",20))
            lines = [f"{'ID':13}  {'Timestamp':19}  {'Process':16}  {'Dur':5}  Labels"]
            lines.append("─" * 80)
            for r in runs:
                lab = ", ".join(f"{k}={v}" for k,v in r["labels"].items()
                                if k not in ("host","cpu","process"))
                lines.append(f"{r['id']:13}  {r['ts'][:19]}  {(r['process'] or ''):16}  "
                             f"{r['duration_s']:4.0f}s  {lab}")
            return {"content": [{"type":"text","text":"\n".join(lines)}]}

        elif name == "compare_runs":
            run_a = load_run_by_id(args["id_a"])
            run_b = load_run_by_id(args["id_b"])
            if not run_a or not run_b:
                return {"content": [{"type":"text","text":"One or both run IDs not found."}]}
            text = _render_compare(run_a, run_b, label_a=args["id_a"][:8], label_b=args["id_b"][:8])
            return {"content": [{"type":"text","text":text}]}

        elif name == "get_ccd_topology":
            return {"content": [{"type":"text","text":_render_topology(_ccd_topology())}]}

        else:
            return {"content": [{"type":"text","text":f"Unknown tool: {name}"}]}
    except Exception as ex:
        return {"content": [{"type":"text","text":f"Error in {name}: {ex}"}], "isError": True}

def cmd_mcp_serve(args):
    """MCP server over stdio (JSON-RPC 2.0). Compatible with Claude Desktop / Claude Code."""
    import io
    stdin  = io.TextIOWrapper(sys.stdin.buffer,  encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

    def send(obj):
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()

    # MCP initialization handshake
    for line in stdin:
        line = line.strip()
        if not line: continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method","")
        req_id = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            send({"jsonrpc":"2.0","id":req_id,"result":{
                "protocolVersion":"2024-11-05",
                "capabilities":{"tools":{}},
                "serverInfo":{"name":"amd-topdown","version":"1.0"},
            }})
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":req_id,"result":{"tools":MCP_TOOLS}})
        elif method == "tools/call":
            tool_name = params.get("name","")
            tool_args = params.get("arguments",{})
            result = _mcp_call_tool(tool_name, tool_args)
            send({"jsonrpc":"2.0","id":req_id,"result":result})
        elif method == "notifications/initialized":
            pass  # no response needed
        else:
            send({"jsonrpc":"2.0","id":req_id,"error":{
                "code":-32601,"message":f"Method not found: {method}"}})

# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amd_topdown.py",
        description="AMD EPYC Top-Down Microarchitecture Analysis — collect, compare, explain",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", metavar="subcommand")

    # collect
    sc = sub.add_parser("collect", help="Collect a TMA run and store it with labels")
    sc.add_argument("--process", "-p", help="Process name to attach to (e.g. redis-server)")
    sc.add_argument("--duration", "-d", type=float, default=30, help="Collection duration in seconds (default 30)")
    sc.add_argument("--label", "-l", action="append", metavar="KEY=VALUE",
                    help="Label to attach to the run (repeatable). e.g. --label git_branch=unstable")

    # ingest
    sc = sub.add_parser("ingest", help="Backfill the store from an existing run-summary corpus")
    sc.add_argument("paths", nargs="*", help="Summary JSON files or dirs (default: auto-discover corpus under the repo)")
    sc.add_argument("--root", help="Repo root to auto-discover corpus dirs under")
    sc.add_argument("--label", "-l", action="append", metavar="KEY=VALUE",
                    help="Extra label to attach to every ingested run (repeatable)")
    sc.add_argument("--dry-run", action="store_true", help="List what would be ingested without writing")

    # list
    sc = sub.add_parser("list", help="List stored runs")
    sc.add_argument("--label", "-l", action="append", metavar="KEY=VALUE")
    sc.add_argument("--limit", type=int, default=20)

    # query
    sc = sub.add_parser("query", help="Query stored runs")
    sc.add_argument("--label", "-l", action="append", metavar="KEY=VALUE")
    sc.add_argument("--funnel", action="store_true", help="Show pipeline slot funnel")
    sc.add_argument("--bottlenecks", action="store_true", help="Show ranked bottlenecks")
    sc.add_argument("--bottleneck", metavar="METRIC", help="Find runs with highest value for METRIC")
    sc.add_argument("--limit", type=int, default=10)

    # compare
    sc = sub.add_parser("compare", help="Compare two runs")
    sc.add_argument("id_a", nargs="?", help="Run ID for A (or omit and use --label-a)")
    sc.add_argument("id_b", nargs="?", help="Run ID for B (or omit and use --label-b)")
    sc.add_argument("--label-a", action="append", metavar="KEY=VALUE", dest="label_a")
    sc.add_argument("--label-b", action="append", metavar="KEY=VALUE", dest="label_b")

    # explain
    sc = sub.add_parser("explain", help="Explain a TMA metric with AMD tuning hints")
    sc.add_argument("metric", nargs="+", help="Metric name, e.g. Frontend_Bound or Memory_Bound")

    # topology
    sc = sub.add_parser("topology", help="Show this host's CCD / cache topology for per-CCD pinning")
    sc.add_argument("--json", action="store_true", help="Emit raw topology JSON instead of the formatted block")

    # agent
    sc = sub.add_parser("agent", help="Continuous collection daemon")
    sc.add_argument("--process", "-p", help="Process name to watch")
    sc.add_argument("--every", type=float, default=300, metavar="SECONDS",
                    help="Collection interval in seconds (default 300)")
    sc.add_argument("--duration", "-d", type=float, default=30, metavar="SECONDS",
                    help="Perf collection window per run (default 30)")
    sc.add_argument("--label", "-l", action="append", metavar="KEY=VALUE")

    # mcp-serve
    sc = sub.add_parser("mcp-serve", help="Start MCP server (stdio, for Claude Desktop/Code)")
    sc.add_argument("--transport", default="stdio", choices=["stdio"],
                    help="Transport (stdio only for now)")

    return p

def main():
    parser = build_parser()
    args   = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "collect":   cmd_collect,
        "ingest":    cmd_ingest,
        "list":      cmd_list,
        "query":     cmd_query,
        "compare":   cmd_compare,
        "explain":   cmd_explain,
        "topology":  cmd_topology,
        "agent":     cmd_agent,
        "mcp-serve": cmd_mcp_serve,
    }
    dispatch[args.cmd](args)

if __name__ == "__main__":
    main()
# end of amd_topdown.py
