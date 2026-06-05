#!/usr/bin/env python3
"""
cloud_context.py — AMD Cloud Performance Context Module
========================================================
Detects or emulates a cloud environment and provides context that changes
how perf toolkit metrics should be interpreted:

  - Package Power Limit (PPL) → caps effective frequency, sets Feff expectations
  - SMT on/off               → changes IPC / L2-hit / branch-prediction meaning
  - Virtualization stack      → deterministic vs. non-deterministic topology
  - PMC availability          → which events to trust, which to sanity-check
  - NUMA boundary             → vCPU count at which cross-socket traffic begins
  - Topology visibility       → is CCX topology correctly exposed in the guest?
  - Local NVMe availability   → affects storage-bound workload interpretation

Usage (standalone):
  python3 cloud_context.py                         # auto-detect + print banner
  python3 cloud_context.py --json                  # JSON output for shell parsing
  python3 cloud_context.py --emulate aws m8a.8xlarge        # emulate an instance
  python3 cloud_context.py --emulate gcp c4d-standard-16   # GCP emulation
  python3 cloud_context.py --emulate aws m8a.8xlarge --json

Usage (as a module):
  from cloud_context import detect, from_instance, CloudContext
  ctx = detect()
  ctx = from_instance("aws", "m8a.8xlarge")
  print(ctx.banner())
  warning = ctx.feff_warning(measured_ghz=3.1, boost_max_ghz=4.4)

Embedded knowledge base: Cloud 201 (SKO session, Lewis Carroll)
Auto-detection: DMI tables, hypervisor type, CPU brand string, /proc/cpuinfo
"""

import os
import re
import sys
import json
import subprocess
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# PMC support levels
# ─────────────────────────────────────────────────────────────────────────────
PMC_FULL      = "full"      # bare metal — all PMCs including uncore and restricted/advanced events
PMC_CORE      = "core"      # most public core PMCs; no uncore (L3, DF, MC)
PMC_LIMITED   = "limited"   # reduced core PMC set; some unit masks may be unsupported
PMC_NONE      = "none"      # no PMC support at all (Oracle Cloud VMs)

# ─────────────────────────────────────────────────────────────────────────────
# Feff ratios: expected effective_freq / boost_max
# Under full load the effective clock stays this fraction of the boost max.
# Below these thresholds the toolkit will emit a throttling warning.
# ─────────────────────────────────────────────────────────────────────────────
FEFF_RATIO_PPL_280W = 0.75   # AWS Genoa 280W: ~25% below Fmax at high load
FEFF_RATIO_PPL_320W = 0.75   # AWS Turin 320W: ~25% below Fmax at high load
FEFF_RATIO_PPL_400W = 0.85   # GCP/Azure Turin 400W: ~15% below Fmax
FEFF_RATIO_PPL_450W = 0.88   # Oracle Turin 450W: within normal boost range
FEFF_RATIO_PPL_500W = 0.90   # AWS M8azn 500W / baremetal range
FEFF_RATIO_BARE     = 0.92   # bare metal: light throttling only from thermal

# Typical boost max by AMD generation (GHz) — conservative median across SKUs
FMAX_GHZBY_GEN = {
    "zen5":    4.4,   # Turin
    "zen4":    4.0,   # Genoa (standard); Genoa-X similar
    "zen3":    3.7,   # Milan
    "zen4c":   3.7,   # Bergamo (dense)
    "unknown": 4.0,
}

# ─────────────────────────────────────────────────────────────────────────────
# Instance database
# Each entry is keyed by (csp, family_key).  family_key is a normalized string
# that matches the leading part of the instance type after normalization.
#
# Fields:
#   cpu_gen        : "turin" | "genoa" | "milan" | "bergamo"
#   zen_gen        : "zen5"  | "zen4"  | "zen3"  | "zen4c"
#   ppl_watts      : int  — package power limit in Watts (0 = unknown/unconstrained)
#   feff_ratio     : float — expected Feff/Fmax ratio at sustained load
#   smt            : bool — SMT enabled on host (True = 2 vCPUs per core)
#   deterministic  : bool — same VM size always gets same CCX topology
#   pmc            : str  — PMC_CORE | PMC_LIMITED | PMC_NONE | PMC_FULL
#   pmc_notes      : list[str] — extra caveats
#   sockets        : int — host socket count (1P or 2P)
#   numa_vcpu_max  : int — largest vCPU count still within single socket (0=N/A)
#   local_nvme     : bool — AMD instances have local NVMe at this CSP
#   topology_vis   : "correct" | "obfuscated" | "unreliable" | "none"
#   notes          : list[str] — FAE-facing interpretation notes
# ─────────────────────────────────────────────────────────────────────────────

_DB: Dict[Tuple[str, str], dict] = {

    # ── AWS ──────────────────────────────────────────────────────────────────

    ("aws", "m8a"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=320, feff_ratio=FEFF_RATIO_PPL_320W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[
            "Most public core PMCs work.",
            ".24xlarge/.48xlarge also expose some MSR-accessible uncore PMCs.",
            "SMN-only PMCs not supported.",
        ],
        sockets=2, numa_vcpu_max=96,
        local_nvme=False,
        topology_vis="correct",
        notes=[
            "SMT off: each vCPU = 1 full core. IPC reflects single-thread behavior.",
            "PPL 320W vs. 400W on GCP/Azure: expect ~25% Feff deficit at full load.",
            "No local NVMe — only X8aedz (EDA specialty) has it.",
            "2XLarge Stream scores artificially low: GMI limits memory BW per CCD.",
            "Largest non-NUMA size: 24XLarge (96 vCPUs / 1 socket).",
        ],
    ),
    ("aws", "c8a"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=320, feff_ratio=FEFF_RATIO_PPL_320W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=["Same as m8a — most core PMCs available."],
        sockets=2, numa_vcpu_max=96, local_nvme=False, topology_vis="correct",
        notes=["Compute-optimized Turin. Same PPL/SMT constraints as m8a."],
    ),
    ("aws", "r8a"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=320, feff_ratio=FEFF_RATIO_PPL_320W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=["Same as m8a — most core PMCs available."],
        sockets=2, numa_vcpu_max=96, local_nvme=False, topology_vis="correct",
        notes=[
            "Memory-optimized Turin (8 GiB/vCPU). SQL Server TCO optimization applies.",
            "SQL Server perf constant beyond ~50% cores — use Optimize Cores feature.",
        ],
    ),
    ("aws", "hpc8a"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=2, numa_vcpu_max=96, local_nvme=False, topology_vis="correct",
        notes=["HPC Turin. Higher PPL exception — 400W like GCP/Azure."],
    ),
    ("aws", "m8azn"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=500, feff_ratio=FEFF_RATIO_PPL_500W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=1, numa_vcpu_max=96, local_nvme=False,
        topology_vis="correct",
        notes=[
            "Turin HF (High Frequency) variant. 8+1 die, down-core to 48c.",
            "500W PPL — effectively unconstrained vs. standard Turin.",
        ],
    ),
    ("aws", "x8aedz"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=500, feff_ratio=FEFF_RATIO_PPL_500W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=1, numa_vcpu_max=96, local_nvme=True,
        topology_vis="correct",
        notes=[
            "EDA specialty instance — only AMD instance at AWS with local NVMe.",
            "Turin HF variant (8+1, down-core to 48c).",
        ],
    ),
    ("aws", "m7a"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=280, feff_ratio=FEFF_RATIO_PPL_280W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=["Same as m8a family for core PMCs."],
        sockets=2, numa_vcpu_max=96, local_nvme=False,
        topology_vis="correct",
        notes=[
            "SMT off: each vCPU = 1 full core.",
            "PPL 280W is the harshest constraint: ~25% Feff deficit at full load.",
            "Memory limited to 4400 MT/s (vs. 4800 MT/s on C7a/HPC7a).",
            "2XLarge Stream scores artificially low vs. Intel (GMI BW limit).",
        ],
    ),
    ("aws", "c7a"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=2, numa_vcpu_max=96, local_nvme=False,
        topology_vis="correct",
        notes=["Compute Genoa. 400W exception — same as GCP/Azure. Memory at 4800 MT/s."],
    ),
    ("aws", "r7a"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=280, feff_ratio=FEFF_RATIO_PPL_280W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=2, numa_vcpu_max=96, local_nvme=False,
        topology_vis="correct",
        notes=["Memory Genoa 280W. SQL Server TCO optimization opportunity."],
    ),
    ("aws", "hpc7a"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=2, numa_vcpu_max=96, local_nvme=False,
        topology_vis="correct",
        notes=["HPC Genoa 400W exception. Memory at 4800 MT/s."],
    ),
    ("aws", "r7an"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=False, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=2, numa_vcpu_max=96, local_nvme=False,
        topology_vis="correct",
        notes=["High-memory Genoa 400W exception."],
    ),
    ("aws", "m6a"): dict(
        cpu_gen="milan", zen_gen="zen3",
        ppl_watts=0, feff_ratio=FEFF_RATIO_BARE,
        smt=True, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=2, numa_vcpu_max=64, local_nvme=False,
        topology_vis="correct",
        notes=[
            "Milan 6+1 die (48c). SMT on — 2 vCPUs share each physical core.",
            "IPC/L2-hit/branch metrics reflect two-thread execution context.",
        ],
    ),
    ("aws", "c6a"): dict(
        cpu_gen="milan", zen_gen="zen3",
        ppl_watts=0, feff_ratio=FEFF_RATIO_BARE,
        smt=True, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=2, numa_vcpu_max=64, local_nvme=False,
        topology_vis="correct",
        notes=["Milan 6+1 compute instance. SMT on."],
    ),
    ("aws", "r6a"): dict(
        cpu_gen="milan", zen_gen="zen3",
        ppl_watts=0, feff_ratio=FEFF_RATIO_BARE,
        smt=True, deterministic=True,
        pmc=PMC_CORE, pmc_notes=[],
        sockets=2, numa_vcpu_max=64, local_nvme=False,
        topology_vis="correct",
        notes=["Milan 6+1 memory instance. SMT on."],
    ),

    # ── Google Cloud ──────────────────────────────────────────────────────────

    ("gcp", "c4d"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_CORE,
        pmc_notes=["Must launch instance with --performance-monitoring-unit flag."],
        sockets=1, numa_vcpu_max=192, local_nvme=True,
        topology_vis="correct",
        notes=[
            "SMT on: 2 vCPUs per core. IPC/L2-hit metrics reflect two-thread context.",
            "No hold-back cores (unlike C3d). Topology is clean and correct.",
            "Local NVMe via -lssd option. 400W PPL — ~15% Feff deficit at full load.",
            "1P host: no cross-socket NUMA boundary.",
            "GCP 4th gen Turin Classic (16+1 down-CCD to 12+1 for memory harmonics).",
        ],
    ),
    ("gcp", "h4d"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=False, deterministic=True,
        pmc=PMC_CORE,
        pmc_notes=["Must launch instance with --performance-monitoring-unit flag."],
        sockets=2, numa_vcpu_max=96, local_nvme=False,
        topology_vis="correct",
        notes=[
            "HPC Turin, SMT off. Optimized for MPI workloads.",
            "400W PPL — ~15% Feff deficit at full load.",
        ],
    ),
    ("gcp", "c3d"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_CORE,
        pmc_notes=["Must launch instance with --performance-monitoring-unit flag."],
        sockets=2, numa_vcpu_max=96, local_nvme=True,
        topology_vis="correct",
        notes=[
            "SMT on: 2 vCPUs per core. IPC metrics reflect two-thread context.",
            "Hold-back cores: Google steals 2 cores/row for hypervisor offload.",
            "No 16-core VM size; VMs scale 30 vCPUs at a time, not 32.",
            "Local NVMe via -lssd. Deterministic topology despite hold-backs.",
        ],
    ),
    ("gcp", "c2d"): dict(
        cpu_gen="milan", zen_gen="zen3",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_CORE,
        pmc_notes=["Must launch instance with --performance-monitoring-unit flag."],
        sockets=2, numa_vcpu_max=64, local_nvme=True,
        topology_vis="correct",
        notes=["Milan, SMT on. Deterministic. Local NVMe via -lssd."],
    ),
    ("gcp", "n4d"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=0, feff_ratio=FEFF_RATIO_BARE,
        smt=True, deterministic=False,
        pmc=PMC_CORE,
        pmc_notes=["Must launch instance with --performance-monitoring-unit flag."],
        sockets=2, numa_vcpu_max=0, local_nvme=False,
        topology_vis="unreliable",
        notes=[
            "NON-DETERMINISTIC: host may be oversubscribed with internal workloads.",
            "NUMA misalignment possible: cores on P0, memory allocated on P1.",
            "CCX misalignment possible: vCPU set may span 2-3 CCXs instead of 1-2.",
            "High Backend Memory% may reflect NUMA misalignment, not workload behavior.",
            "Good for single-threaded or small-container (<1 vCPU) workloads only.",
            "VM-to-VM performance variability is expected and by design.",
        ],
    ),
    ("gcp", "n2d"): dict(
        cpu_gen="milan", zen_gen="zen3",
        ppl_watts=0, feff_ratio=FEFF_RATIO_BARE,
        smt=True, deterministic=False,
        pmc=PMC_CORE,
        pmc_notes=["Must launch instance with --performance-monitoring-unit flag."],
        sockets=2, numa_vcpu_max=0, local_nvme=False,
        topology_vis="unreliable",
        notes=[
            "NON-DETERMINISTIC: Milan, same oversubscription caveats as N4d.",
            "Variable performance from VM to VM is expected.",
            "High Backend Memory% may reflect NUMA misalignment.",
        ],
    ),

    # ── Azure ─────────────────────────────────────────────────────────────────

    ("azure", "dasv8"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=["Reduced core PMC set; some AMD unit masks may return zero."],
        sockets=1, numa_vcpu_max=192, local_nvme=False,
        topology_vis="correct",
        notes=[
            "1P host (Azure GP = single socket). No cross-socket NUMA.",
            "SMT on: 2 vCPUs per core. IPC metrics reflect two-thread context.",
            "CPU numbering: Azure uses Windows order (cores 0&1 together). Linux tools see this.",
            "400W PPL — ~15% Feff deficit at full load.",
            "'d' variants (e.g. D32ads_v8) have local NVMe.",
        ],
    ),
    ("azure", "easv8"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=["Reduced core PMC set."],
        sockets=1, numa_vcpu_max=192, local_nvme=False,
        topology_vis="correct",
        notes=["Memory-optimized Turin 1P. Same constraints as Dasv8."],
    ),
    ("azure", "fasv8"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=False, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=["Reduced core PMC set."],
        sockets=1, numa_vcpu_max=192, local_nvme=False,
        topology_vis="correct",
        notes=["Compute Turin 1P. SMT off."],
    ),
    ("azure", "dasv6"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=["Reduced core PMC set."],
        sockets=1, numa_vcpu_max=192, local_nvme=False,
        topology_vis="correct",
        notes=["Genoa 1P host. SMT on. 'd' variants have local NVMe (D32ads_v6 etc.)."],
    ),
    ("azure", "easv6"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=[],
        sockets=1, numa_vcpu_max=192, local_nvme=False,
        topology_vis="correct",
        notes=["Memory Genoa 1P. SMT on."],
    ),
    ("azure", "hbv4"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=False, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=["Reduced core PMC set."],
        sockets=2, numa_vcpu_max=176, local_nvme=False,
        topology_vis="obfuscated",
        notes=[
            "TOPOLOGY OBFUSCATED on full-size (HB176rs_v4): 44 cores appear to share one 96MB L3.",
            "This is intentional — enables NUMA-based MPI to work out of the box (NPS2, 4 NUMA nodes).",
            "For CFD (Ansys Fluent): use constrained-cores variant (HB176-96rs_v4 = 4 cores/CCD).",
            "Constrained-cores variants expose correct topology — all EPYC HPC optimizations work.",
            "Azure steals some physical cores for hypervisor offload (shown as blue in topology diagrams).",
            "Genoa-X: 96 MB L3 per CCD (32 MB base + 64 MB 3D V-Cache).",
        ],
    ),
    ("azure", "hbv5"): dict(
        cpu_gen="milan", zen_gen="zen3",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=False, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=[],
        sockets=2, numa_vcpu_max=176, local_nvme=False,
        topology_vis="correct",
        notes=["HPC Milan-based. SMT off. Topology correctly exposed."],
    ),
    ("azure", "lasv3"): dict(
        cpu_gen="milan", zen_gen="zen3",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=[],
        sockets=2, numa_vcpu_max=64, local_nvme=True,
        topology_vis="correct",
        notes=["Milan with local NVMe. SMT on."],
    ),
    ("azure", "lasv4"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_LIMITED, pmc_notes=[],
        sockets=2, numa_vcpu_max=96, local_nvme=True,
        topology_vis="correct",
        notes=["Genoa with local NVMe. SMT on."],
    ),

    # ── Oracle Cloud ──────────────────────────────────────────────────────────

    ("oracle", "e6"): dict(
        cpu_gen="turin", zen_gen="zen5",
        ppl_watts=450, feff_ratio=FEFF_RATIO_PPL_450W,
        smt=True, deterministic=True,  # bare metal; VMs are non-deterministic
        pmc=PMC_NONE,
        pmc_notes=["Oracle Cloud VMs have NO PMC support. Profile on bare metal."],
        sockets=2, numa_vcpu_max=0, local_nvme=True,
        topology_vis="none",
        notes=[
            "PMC SUPPORT: NONE — perf stat events will return zero or fail silently.",
            "Topology not visible to guest: cores have L3 but no shared-L3 grouping exposed.",
            "VMs are non-deterministic; bare metal E6 is deterministic.",
            "Oracle oCPU pricing: 1 oCPU = 1 core = 2 vCPUs. Best SMT value model.",
            "450W PPL — effectively unconstrained. Feff close to bare metal.",
            "Dense-IO families have local NVMe.",
        ],
    ),
    ("oracle", "e5"): dict(
        cpu_gen="genoa", zen_gen="zen4",
        ppl_watts=400, feff_ratio=FEFF_RATIO_PPL_400W,
        smt=True, deterministic=True,
        pmc=PMC_NONE,
        pmc_notes=["Oracle Cloud VMs have NO PMC support."],
        sockets=2, numa_vcpu_max=0, local_nvme=True,
        topology_vis="none",
        notes=["Genoa-based Oracle. Same PMC/topology caveats as E6."],
    ),

    # ── Bare metal (fallback / explicit) ─────────────────────────────────────
    # NOTE: smt is intentionally None here — it will be overridden at detection
    # time by _detect_smt() so the banner reflects the actual BIOS setting.

    ("baremetal", "epyc"): dict(
        cpu_gen="unknown", zen_gen="unknown",
        ppl_watts=0, feff_ratio=FEFF_RATIO_BARE,
        smt=None, deterministic=True,
        pmc=PMC_FULL, pmc_notes=["All PMCs available including uncore and restricted/advanced events."],
        sockets=1, numa_vcpu_max=0, local_nvme=True,
        topology_vis="correct",
        notes=["Bare metal: full PMC access, no virtualization overhead."],
    ),
}

# Aliases — map common instance-type prefixes to DB keys
_FAMILY_ALIASES: Dict[str, Tuple[str, str]] = {
    # AWS
    "m8a":    ("aws", "m8a"),   "c8a":  ("aws", "c8a"),
    "r8a":    ("aws", "r8a"),   "hpc8a":("aws","hpc8a"),
    "m8azn":  ("aws","m8azn"),  "x8aedz":("aws","x8aedz"),
    "m7a":    ("aws", "m7a"),   "c7a":  ("aws", "c7a"),
    "r7a":    ("aws", "r7a"),   "hpc7a":("aws","hpc7a"),
    "r7an":   ("aws","r7an"),
    "m6a":    ("aws", "m6a"),   "c6a":  ("aws", "c6a"),
    "r6a":    ("aws", "r6a"),
    # GCP
    "c4d":    ("gcp", "c4d"),   "h4d":  ("gcp","h4d"),
    "c3d":    ("gcp", "c3d"),   "c2d":  ("gcp","c2d"),
    "n4d":    ("gcp", "n4d"),   "n2d":  ("gcp","n2d"),
    # Azure
    "standarddasv8": ("azure","dasv8"),  "d":("azure","dasv8"),
    "standardeasv8": ("azure","easv8"),
    "standardfasv8": ("azure","fasv8"),
    "standarddasv6": ("azure","dasv6"),
    "standardeasv6": ("azure","easv6"),
    "hbv4":          ("azure","hbv4"),
    "hbv5":          ("azure","hbv5"),
    "lasv3":         ("azure","lasv3"),  "lasv4": ("azure","lasv4"),
    # Oracle
    "e6":     ("oracle","e6"),
    "bm.standard.e6": ("oracle","e6"),
    "e5":     ("oracle","e5"),
}


# ─────────────────────────────────────────────────────────────────────────────
# CloudContext dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CloudContext:
    csp:               str    = "unknown"   # aws | gcp | azure | oracle | baremetal | unknown
    instance_type:     str    = "unknown"   # e.g. "m8a.8xlarge"
    instance_family:   str    = "unknown"   # e.g. "m8a"
    cpu_gen:           str    = "unknown"   # turin | genoa | milan
    zen_gen:           str    = "unknown"   # zen5 | zen4 | zen3
    ppl_watts:         int    = 0           # 0 = unconstrained / unknown
    feff_ratio:        float  = 0.90        # expected Feff/Fmax at sustained load
    fmax_ghz:          float  = 4.0         # estimated boost max for this gen
    smt_enabled:       bool   = False       # True if host has SMT on
    deterministic:     bool   = True        # True if topology is predictable
    pmc_support:       str    = PMC_CORE
    pmc_notes:         List[str] = field(default_factory=list)
    sockets:           int    = 2
    numa_vcpu_max:     int    = 0           # 0 = 1P host or N/A
    local_nvme:        bool   = False
    topology_vis:      str    = "correct"   # correct | obfuscated | unreliable | none
    notes:             List[str] = field(default_factory=list)
    emulated:          bool   = False       # True if --emulate flag was used
    detected_vcpus:    int    = 0           # vCPU count seen on this guest
    detected_smt:      bool   = False       # SMT detected in /proc/cpuinfo
    detected_numa:     int    = 1           # NUMA node count from sysfs
    raw_cpu_brand:     str    = ""

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def feff_expected_min_ghz(self) -> float:
        """Minimum Feff we expect at full sustained load (PPL floor)."""
        return round(self.fmax_ghz * self.feff_ratio, 2)

    @property
    def is_numa_crossing(self) -> bool:
        """True if current guest vCPU count crosses the socket boundary."""
        if self.numa_vcpu_max <= 0:
            return False
        return self.detected_vcpus > self.numa_vcpu_max

    @property
    def ppl_label(self) -> str:
        if self.ppl_watts > 0:
            return f"{self.ppl_watts}W"
        return "unconstrained"

    # ── Key interpretation methods ────────────────────────────────────────────

    def feff_warning(self, measured_ghz: float, boost_max_ghz: Optional[float] = None) -> Optional[str]:
        """
        Returns a warning string if measured Feff suggests PPL throttling,
        or None if Feff is within expected range.
        """
        fmax = boost_max_ghz or self.fmax_ghz
        if fmax <= 0:
            return None
        ratio = measured_ghz / fmax
        expected = self.feff_ratio
        # Warn if measured is more than 5 percentage points below expected
        if ratio < (expected - 0.05):
            delta_pct = round((1 - ratio) * 100, 1)
            exp_pct   = round((1 - expected) * 100, 1)
            lines = [
                f"⚠  FREQ THROTTLE: Feff {measured_ghz:.2f} GHz = {ratio*100:.1f}% of Fmax "
                f"({fmax:.1f} GHz) — {delta_pct}% below Fmax.",
            ]
            if self.csp == "aws" and self.ppl_watts in (280, 320):
                lines.append(
                    f"   AWS PPL {self.ppl_watts}W constraint. Expected floor: "
                    f"~{exp_pct}% below Fmax at full load."
                )
                lines.append("   Action: ask customer to log /proc/cpuinfo clock freq. "
                              "Clock drop + perf drop = confirmed AWS PPL throttling.")
            elif self.csp in ("gcp", "azure") and self.ppl_watts == 400:
                lines.append(f"   {self.csp.upper()} 400W PPL. Expected ~{exp_pct}% below Fmax. "
                              "Slightly higher than expected — check thermal or workload burst pattern.")
            else:
                lines.append(f"   Investigate: thermal throttle, PPL cap, or low-utilization workload.")
            return "\n".join(lines)
        return None

    def pmc_sanity_check(self) -> List[str]:
        """
        Returns list of perf events that should return non-zero on any active workload.
        If these return zero, PMC support is broken in this VM.
        """
        if self.pmc_support == PMC_NONE:
            return []
        return [
            "cpu-cycles",
            "instructions",
            "ls_not_halted_cyc",
            "ex_ret_ops",
        ]

    def interpret_backend_memory(self, backend_mem_pct: float) -> Optional[str]:
        """
        Context-aware interpretation of Backend Memory % metric.
        Returns an additional interpretation string, or None.
        """
        if backend_mem_pct < 20:
            return None
        lines = []
        if self.topology_vis == "unreliable":
            lines.append(
                f"⚠  Backend Memory {backend_mem_pct:.1f}%: on non-deterministic stack "
                f"({self.csp.upper()} {self.instance_family}), this may reflect "
                f"NUMA/CCX misalignment by the hypervisor — not workload memory pressure."
            )
        elif self.is_numa_crossing:
            lines.append(
                f"⚠  Backend Memory {backend_mem_pct:.1f}%: this instance ({self.detected_vcpus} vCPUs) "
                f"crosses the NUMA boundary (>{self.numa_vcpu_max} vCPUs = 2 sockets). "
                f"Remote socket memory latency (~100 ns) may be contributing."
            )
        elif self.smt_enabled:
            lines.append(
                f"   Backend Memory {backend_mem_pct:.1f}%: SMT on — sibling thread's cache "
                f"footprint may be evicting your L2 working set, inflating this metric."
            )
        return "\n".join(lines) if lines else None

    def interpret_ipc(self, ipc: float) -> Optional[str]:
        """Returns context note about IPC interpretation."""
        if self.smt_enabled:
            return (
                f"   IPC {ipc:.2f}: SMT on — this is per-thread IPC with a sibling thread "
                f"competing for dispatch slots, execution units, and L2. "
                f"Single-thread equivalent IPC would be higher."
            )
        return None

    def interpret_l2_hitrate(self, hit_pct: float) -> Optional[str]:
        """Returns context note about L2 hit rate."""
        if self.smt_enabled and hit_pct < 70:
            return (
                f"   L2 DC Hit {hit_pct:.1f}%: SMT on — sibling thread's working set "
                f"competes for L2 cache lines. Single-thread L2 hit rate would be higher."
            )
        return None

    # ── Output formatting ─────────────────────────────────────────────────────

    def banner(self) -> str:
        """Multi-line banner for embedding in terminal report headers."""
        W = 58
        sep = "━" * W
        lines = [sep]
        tag = " [EMULATED]" if self.emulated else ""
        lines.append(f"  CLOUD CONTEXT{tag}")
        lines.append(sep)

        def row(label, value):
            return f"  {label:<30} {str(value):<24}"

        csp_disp   = self.csp.upper() if self.csp != "unknown" else "Bare Metal / Unknown"
        inst_disp  = self.instance_type if self.instance_type != "unknown" else "—"
        gen_disp   = f"{self.cpu_gen.title()} ({self.zen_gen.upper()})" if self.cpu_gen != "unknown" else "—"
        ppl_disp   = f"{self.ppl_watts}W" if self.ppl_watts else "unconstrained"
        feff_disp  = f"≥{self.feff_expected_min_ghz:.2f} GHz (≥{self.feff_ratio*100:.0f}% of Fmax)"
        smt_disp   = "ON (2 vCPUs/core)" if self.smt_enabled else "OFF (1 vCPU = 1 core)"
        det_disp   = "Yes (predictable)" if self.deterministic else "⚠ No (variable VM-to-VM)"
        pmc_disp   = {"full":"Full (all PMCs)","core":"Core PMCs only",
                      "limited":"Limited core PMCs","none":"⚠ NONE"}[self.pmc_support]
        numa_disp  = (f"Crosses at >{self.numa_vcpu_max} vCPUs"
                      if self.numa_vcpu_max > 0 else
                      ("1P host — no NUMA" if self.sockets == 1 else "N/A"))
        topo_disp  = {"correct":"Correct","obfuscated":"⚠ Obfuscated (see notes)",
                      "unreliable":"⚠ Unreliable","none":"⚠ Not exposed"}[self.topology_vis]
        nvme_disp  = "Yes" if self.local_nvme else "No (network-attached only)"

        lines.append(row("CSP", csp_disp))
        lines.append(row("Instance", inst_disp))
        lines.append(row("CPU Generation", gen_disp))
        lines.append(row("Sockets (host)", f"{self.sockets}P"))
        lines.append(row("Guest vCPUs (detected)", self.detected_vcpus or "—"))
        lines.append(row("NUMA Node(s) (detected)", self.detected_numa))
        lines.append("")
        lines.append(row("Package Power Limit", ppl_disp))
        lines.append(row("Expected Feff floor", feff_disp))
        lines.append(row("SMT on host", smt_disp))
        lines.append(row("Virtualization stack", det_disp))
        lines.append(row("CCX topology visible", topo_disp))
        lines.append(row("PMC support in VM", pmc_disp))
        lines.append(row("Local NVMe", nvme_disp))

        if self.numa_vcpu_max > 0:
            numa_cross = "⚠ YES — cross-socket" if self.is_numa_crossing else "No"
            lines.append(row("NUMA crossing (this guest)", numa_cross))

        if self.pmc_notes or self.notes:
            lines.append("")
            lines.append("  Interpretation Notes:")
            for n in self.notes:
                lines.append(f"  • {n}")
            for n in self.pmc_notes:
                lines.append(f"  • PMC: {n}")

        lines.append(sep)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "csp":               self.csp,
            "instance_type":     self.instance_type,
            "instance_family":   self.instance_family,
            "cpu_gen":           self.cpu_gen,
            "zen_gen":           self.zen_gen,
            "ppl_watts":         self.ppl_watts,
            "feff_ratio":        self.feff_ratio,
            "fmax_ghz":          self.fmax_ghz,
            "feff_expected_min_ghz": self.feff_expected_min_ghz,
            "smt_enabled":       self.smt_enabled,
            "deterministic":     self.deterministic,
            "pmc_support":       self.pmc_support,
            "pmc_notes":         self.pmc_notes,
            "sockets":           self.sockets,
            "numa_vcpu_max":     self.numa_vcpu_max,
            "local_nvme":        self.local_nvme,
            "topology_vis":      self.topology_vis,
            "notes":             self.notes,
            "emulated":          self.emulated,
            "detected_vcpus":    self.detected_vcpus,
            "detected_smt":      self.detected_smt,
            "detected_numa":     self.detected_numa,
            "is_numa_crossing":  self.is_numa_crossing,
            "raw_cpu_brand":     self.raw_cpu_brand,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Auto-detection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def _detect_csp() -> Tuple[str, str]:
    """Returns (csp, hint_instance) — hint_instance may be empty."""
    vendor = _read("/sys/class/dmi/id/sys_vendor").lower()
    product = _read("/sys/class/dmi/id/product_name").lower()
    bios_vendor = _read("/sys/class/dmi/id/bios_vendor").lower()

    if "amazon" in vendor or "amazon" in product or "amazon" in bios_vendor:
        return "aws", ""
    if "google" in vendor or "google" in product:
        return "gcp", ""
    if "microsoft" in vendor or "microsoft" in product:
        return "azure", ""
    if "oracle" in vendor or "oracle" in product:
        return "oracle", ""
    # Try hypervisor type
    hyp = _read("/sys/hypervisor/type").lower()
    if "xen" in hyp:
        return "aws", ""   # AWS uses Nitro (KVM/Xen)
    # Check environment variables (GCP sets some)
    if os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("CLOUDSDK_COMPUTE_REGION"):
        return "gcp", ""
    if os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_EXECUTION_ENV"):
        return "aws", ""
    return "baremetal", ""


def _detect_cpu_brand() -> str:
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def _detect_zen_gen(brand: str) -> Tuple[str, str]:
    """Returns (cpu_gen, zen_gen) from CPU brand string."""
    b = brand.lower()
    # Turin family IDs: 9005 series or CSP custom like 9B45, 9R45, 9J45
    if re.search(r'90[0-9][0-9]|9[bcjrh][0-9][0-9]|9005', b):
        return "turin", "zen5"
    # Genoa: 9004 series
    if re.search(r'9[0-9][0-9][4-9]|9[0-9]54|genoa', b):
        return "genoa", "zen4"
    # Milan: 7003 series
    if re.search(r'7[0-9][0-9][3-9]|milan', b):
        return "milan", "zen3"
    # Bergamo / Siena
    if re.search(r'bergamo|siena|97[0-9][0-9]', b):
        return "bergamo", "zen4c"
    # Fallback: look for "zen 5", "zen 4", etc.
    if "zen 5" in b:
        return "turin", "zen5"
    if "zen 4" in b:
        return "genoa", "zen4"
    if "zen 3" in b:
        return "milan", "zen3"
    return "unknown", "unknown"


def _detect_vcpus() -> int:
    try:
        return int(subprocess.check_output(["nproc"], text=True).strip())
    except Exception:
        pass
    try:
        count = 0
        for line in open("/proc/cpuinfo"):
            if line.startswith("processor"):
                count += 1
        return count
    except Exception:
        return 0


def _detect_smt() -> bool:
    """True if SMT (Hyper-Threading) is active on this host.

    Detection order (most to least reliable):
    1. /sys/devices/system/cpu/smt/active  — kernel's own SMT state (0/1)
    2. lscpu 'Thread(s) per core'         — direct threads-per-core count
    3. /proc/cpuinfo siblings vs cpu cores comparison
    4. thread_siblings_list in /sys       — bitmask approach (least reliable)
    """
    try:
        # 1. Kernel SMT control file — most authoritative
        val = _read("/sys/devices/system/cpu/smt/active").strip()
        if val in ("0", "1"):
            return val == "1"
    except Exception:
        pass

    try:
        # 2. lscpu threads-per-core — definitive and always present
        import subprocess
        out = subprocess.check_output(["lscpu"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "Thread(s) per core" in line:
                threads = int(line.split(":")[1].strip())
                return threads > 1
    except Exception:
        pass

    try:
        # 3. /proc/cpuinfo siblings vs cpu cores
        siblings = cores = 0
        for line in open("/proc/cpuinfo"):
            if line.startswith("siblings"):
                siblings = int(line.split(":")[1])
            if line.startswith("cpu cores"):
                cores = int(line.split(":")[1])
            if siblings and cores:
                break
        if cores and siblings:
            return siblings > cores
    except Exception:
        pass

    try:
        # 4. thread_siblings_list — bitmask approach
        val = _read("/sys/devices/system/cpu/cpu0/topology/thread_siblings_list")
        if "," in val or "-" in val:
            parts = re.split(r'[,\-]', val)
            if len(parts) > 1 and parts[0] != parts[-1]:
                return True
    except Exception:
        pass

    return False


def _detect_numa_nodes() -> int:
    try:
        nodes = [d for d in os.listdir("/sys/devices/system/node/")
                 if d.startswith("node") and d[4:].isdigit()]
        return max(1, len(nodes))
    except Exception:
        return 1


def _normalize_instance(csp: str, raw: str) -> Tuple[str, str]:
    """
    Normalize instance type to (family_key, full_instance) for DB lookup.
    Returns the matched (csp, family_key) tuple to use against _DB.
    """
    s = raw.lower().strip()

    # AWS: "m8a.8xlarge" → family "m8a"
    if csp == "aws":
        m = re.match(r'^([a-z0-9]+)\.\d*xlarge$', s)
        if m:
            return m.group(1), raw
        m = re.match(r'^([a-z0-9]+)', s)
        if m:
            return m.group(1), raw

    # GCP: "c4d-standard-16" → family "c4d"
    if csp == "gcp":
        m = re.match(r'^([a-z0-9]+)', s)
        if m:
            return m.group(1), raw

    # Azure: "Standard_D16as_v6" → try family matching
    if csp == "azure":
        s_clean = s.replace("standard_", "").replace("_", "")
        # Attempt series match: Dasv8, Easv6, HBv4, etc.
        m = re.match(r'^([a-z]+v\d+)', s_clean)
        if m:
            return m.group(1), raw
        m = re.match(r'^([a-z0-9]+)', s_clean)
        if m:
            return m.group(1), raw

    # Oracle: "BM.Standard.E6.192" → "e6"
    if csp == "oracle":
        m = re.search(r'e\d+', s)
        if m:
            return m.group(0), raw

    return s, raw


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def _build_context(csp: str, family_key: str, instance_type: str,
                   emulated: bool = False) -> CloudContext:
    """Populate a CloudContext from the DB + live system detection."""
    db_key = (csp, family_key)
    # Try alias lookup if direct key not found
    if db_key not in _DB:
        alias_key = _FAMILY_ALIASES.get(family_key)
        if alias_key:
            db_key = alias_key
    db = _DB.get(db_key, {})

    brand   = _detect_cpu_brand()
    cpu_gen = db.get("cpu_gen", "unknown")
    zen_gen = db.get("zen_gen", "unknown")
    if cpu_gen == "unknown":
        cpu_gen, zen_gen = _detect_zen_gen(brand)

    ctx = CloudContext(
        csp             = db_key[0] if db_key in _DB else csp,
        instance_type   = instance_type,
        instance_family = family_key,
        cpu_gen         = cpu_gen,
        zen_gen         = zen_gen,
        ppl_watts       = db.get("ppl_watts", 0),
        feff_ratio      = db.get("feff_ratio", FEFF_RATIO_BARE),
        fmax_ghz        = FMAX_GHZBY_GEN.get(zen_gen, 4.0),
        smt_enabled     = db.get("smt") if db.get("smt") is not None else _detect_smt(),
        deterministic   = db.get("deterministic", True),
        pmc_support     = db.get("pmc", PMC_CORE),
        pmc_notes       = list(db.get("pmc_notes", [])),
        sockets         = db.get("sockets", 2),
        numa_vcpu_max   = db.get("numa_vcpu_max", 0),
        local_nvme      = db.get("local_nvme", False),
        topology_vis    = db.get("topology_vis", "correct"),
        notes           = list(db.get("notes", [])),
        emulated        = emulated,
        detected_vcpus  = _detect_vcpus(),
        detected_smt    = _detect_smt(),
        detected_numa   = _detect_numa_nodes(),
        raw_cpu_brand   = brand,
    )
    return ctx


def detect() -> CloudContext:
    """
    Auto-detect cloud environment from DMI tables, hypervisor, and /proc/cpuinfo.
    Falls back to "baremetal" if no cloud signature is found.
    """
    csp, _ = _detect_csp()
    if csp == "baremetal":
        return _build_context("baremetal", "epyc", "bare-metal", emulated=False)
    # On cloud, we know the CSP but usually not the instance type.
    # Build a generic context using only the CPU brand for gen detection.
    brand = _detect_cpu_brand()
    cpu_gen, zen_gen = _detect_zen_gen(brand)
    # Try to infer a family_key from vcpu count + gen (best-effort)
    vcpus = _detect_vcpus()
    family_key = f"{csp}-detected"
    ctx = CloudContext(
        csp             = csp,
        instance_type   = f"{csp.upper()} (instance type unknown — use --emulate)",
        instance_family = family_key,
        cpu_gen         = cpu_gen,
        zen_gen         = zen_gen,
        ppl_watts       = 0,
        feff_ratio      = FEFF_RATIO_PPL_320W if csp == "aws" else FEFF_RATIO_PPL_400W,
        fmax_ghz        = FMAX_GHZBY_GEN.get(zen_gen, 4.0),
        smt_enabled     = _detect_smt(),
        deterministic   = csp != "oracle",
        pmc_support     = PMC_NONE if csp == "oracle" else PMC_CORE,
        pmc_notes       = (["Oracle Cloud: NO PMC support."] if csp == "oracle"
                           else ["Run with --emulate CSP INSTANCE for full context."]),
        sockets         = 2,
        numa_vcpu_max   = 96 if csp == "aws" else 0,
        local_nvme      = False,
        topology_vis    = "correct" if csp in ("aws",) else "unknown",
        notes           = [
            f"CSP detected: {csp.upper()}. Pass --emulate {csp} <instance_type> for full context.",
            f"CPU: {brand}",
        ],
        emulated        = False,
        detected_vcpus  = vcpus,
        detected_smt    = _detect_smt(),
        detected_numa   = _detect_numa_nodes(),
        raw_cpu_brand   = brand,
    )
    return ctx


def from_instance(csp: str, instance_type: str) -> CloudContext:
    """
    Build a CloudContext for a specific CSP + instance type.
    Used for --emulate mode or when the instance is known ahead of time.
    """
    csp = csp.lower().strip()
    family_key, full_name = _normalize_instance(csp, instance_type)
    return _build_context(csp, family_key, full_name, emulated=True)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def _usage():
    print(__doc__)
    sys.exit(0)


def main():
    args = sys.argv[1:]

    emit_json  = "--json"  in args
    emit_help  = "--help"  in args or "-h" in args
    emulate    = "--emulate" in args

    if emit_help:
        _usage()

    if emulate:
        idx = args.index("--emulate")
        try:
            csp           = args[idx + 1]
            instance_type = args[idx + 2]
        except IndexError:
            print("Usage: --emulate <csp> <instance_type>", file=sys.stderr)
            sys.exit(1)
        ctx = from_instance(csp, instance_type)
    else:
        ctx = detect()

    if emit_json:
        print(json.dumps(ctx.to_dict(), indent=2))
    else:
        print(ctx.banner())

        # NOTE: Do NOT read /proc/cpuinfo here for frequency.
        # /proc/cpuinfo reflects idle frequency before any workload runs.
        # Idle cores operate at low frequency by design — this is not throttling.
        # Effective frequency must be measured by perf stat *during* workload execution
        # using: cpu-cycles / (task-clock_ms * 1e6). See amd_pipeline_metrics.sh Section 0.


if __name__ == "__main__":
    main()
