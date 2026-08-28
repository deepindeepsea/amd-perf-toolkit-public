#!/usr/bin/env bash
# bench_density.sh — how many microVMs can this host pack, and how fast do they
# come up when launched concurrently (the at-scale cold-start story).
#
# Strategy: pick a per-VM mem footprint (default 256 MiB, 1 vcpu — a typical
# Lambda-class slice). Compute a target count bounded by BOTH:
#   * RAM budget   : (usable_mem * SAFETY) / per_vm_mem
#   * vcpu density : THREADS * VCPU_OVERSUB   (microVMs oversubscribe CPU)
# Launch them all fast-init, in parallel, and measure how many reach
# FC_READY_MARKER plus the wall-clock to bring the whole fleet up.
#
# Output: JSON with target, launched, ready, fleet_boot_seconds, per_vm_mem_mib,
# vms_per_core, vms_per_gib.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"
source "$HERE/vm.sh"

PER_VM_MEM="${1:-256}"      # MiB per microVM
VCPU_OVERSUB="${2:-1}"      # microVMs per hardware thread
SAFETY="${3:-0.80}"         # fraction of RAM we allow VMs to claim

threads=$(nproc)
memkb=$(awk '/MemTotal/{print $2}' /proc/meminfo)
mem_mib=$((memkb/1024))
# RAM-bound and CPU-bound caps; target is the smaller
ram_cap=$(awk -v m="$mem_mib" -v p="$PER_VM_MEM" -v s="$SAFETY" 'BEGIN{printf "%d", (m*s)/p}')
cpu_cap=$((threads*VCPU_OVERSUB))
target=$(( ram_cap < cpu_cap ? ram_cap : cpu_cap ))
[ "$target" -lt 1 ] && target=1

log "density: per_vm=${PER_VM_MEM}MiB threads=$threads mem=${mem_mib}MiB ram_cap=$ram_cap cpu_cap=$cpu_cap -> target=$target"

# launch all fast-init VMs concurrently; each writes its own console log
rm -f "$FC_RUN"/con-*.log "$FC_RUN"/dpid-* 2>/dev/null
t0=$(date +%s.%N)
for i in $(seq 1 "$target"); do
  (
    ov=$(make_overlay "$i")
    cfg=$(_fc_write_config "$i" 1 "$PER_VM_MEM" fast "$ov")
    "$FC_BIN" --api-sock "$FC_RUN/fc-$i.sock" --config-file "$cfg" >"$FC_RUN/con-$i.log" 2>&1 &
    echo $! > "$FC_RUN/dpid-$i"
  ) &
done
wait   # all launchers dispatched

# poll until every VM has printed the marker or we hit the deadline
deadline=$((SECONDS+120))
ready=0
while [ $SECONDS -lt $deadline ]; do
  ready=$(grep -l FC_READY_MARKER "$FC_RUN"/con-*.log 2>/dev/null | wc -l)
  [ "$ready" -ge "$target" ] && break
  sleep 0.5
done
t1=$(date +%s.%N)
ready=$(grep -l FC_READY_MARKER "$FC_RUN"/con-*.log 2>/dev/null | wc -l)
launched=$(ls "$FC_RUN"/dpid-* 2>/dev/null | wc -l)
fleet_s=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.2f",(b-a)}')

fc_kill_all 2>/dev/null

vpc=$(awk -v r="$ready" -v t="$threads" 'BEGIN{printf "%.2f", (t>0)? r/t:0}')
vpg=$(awk -v r="$ready" -v m="$mem_mib" 'BEGIN{printf "%.2f", (m>0)? r/(m/1024.0):0}')

cat <<EOF
{
  "metric": "density",
  "per_vm_mem_mib": $PER_VM_MEM,
  "vcpu_oversub": $VCPU_OVERSUB,
  "ram_safety": $SAFETY,
  "host_threads": $threads,
  "host_mem_mib": $mem_mib,
  "ram_cap": $ram_cap,
  "cpu_cap": $cpu_cap,
  "target": $target,
  "launched": $launched,
  "ready": $ready,
  "fleet_boot_seconds": $fleet_s,
  "vms_per_core": $vpc,
  "vms_per_gib": $vpg
}
EOF
