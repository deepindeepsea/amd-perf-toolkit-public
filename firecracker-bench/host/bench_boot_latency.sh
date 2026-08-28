#!/usr/bin/env bash
# bench_boot_latency.sh — cold-start latency of a single microVM, fast-init path.
# Boots N throwaway microVMs (1 vcpu / 128 MiB, init=/fcready) one at a time and
# records the wall-clock from `firecracker exec` to the guest's FC_READY_MARKER.
# This is the pure VMM+kernel+init number (the ~125ms-class metric AWS quotes for
# Lambda cold starts) — it deliberately excludes systemd.
#
# Output: JSON {n, samples[], min, p50, p90, p99, max, mean} in ms.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"
source "$HERE/vm.sh"

N="${1:-50}"          # number of cold boots to sample
log "boot_latency: $N cold fast-init boots"

samples=()
ok=0
for n in $(seq 1 "$N"); do
  ms=$(fc_boot_fast "$n")
  fc_kill "$n" 2>/dev/null
  if [ "$ms" != "-1" ]; then
    samples+=("$ms"); ok=$((ok+1))
  else
    log "  boot $n FAILED/timed out"
  fi
done
fc_kill_all 2>/dev/null

# compute stats from the collected samples
printf '%s\n' "${samples[@]}" > "$FC_RUN/boot_samples.txt"
mn=$(printf '%s\n' "${samples[@]}" | sort -n | head -1)
mx=$(printf '%s\n' "${samples[@]}" | sort -n | tail -1)
p50=$(printf '%s\n' "${samples[@]}" | pctl 50)
p90=$(printf '%s\n' "${samples[@]}" | pctl 90)
p99=$(printf '%s\n' "${samples[@]}" | pctl 99)
avg=$(printf '%s\n' "${samples[@]}" | mean)
arr=$(printf '%s,' "${samples[@]}"); arr="[${arr%,}]"

cat <<EOF
{
  "metric": "boot_latency_ms",
  "mode": "fast_init",
  "requested": $N,
  "succeeded": $ok,
  "min": ${mn:-0},
  "p50": ${p50:-0},
  "p90": ${p90:-0},
  "p99": ${p99:-0},
  "max": ${mx:-0},
  "mean": ${avg:-0},
  "samples": $arr
}
EOF
