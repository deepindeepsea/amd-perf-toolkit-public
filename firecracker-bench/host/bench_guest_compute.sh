#!/usr/bin/env bash
# bench_guest_compute.sh — compute throughput delivered INSIDE a microVM.
# Two complementary numbers, both run in one full guest:
#   * openssl speed aes-256-cbc  (real crypto, AES-NI path) -> single & N-thread
#   * stress-ng --cpu matrixprod  bogo-ops/s  (FP/integer mix) -> single & N-thread
# Reporting both 1-thread and full-vcpu lets the report show per-core efficiency
# AND aggregate scaling without any host-side tuning (vendor-neutral).
#
# Output: JSON with aes_1t_mibps, aes_nt_mibps, sng_1t_bogo, sng_nt_bogo, vcpus.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"
source "$HERE/vm.sh"

VCPUS="${1:-8}"; MEM="${2:-2048}"; SNG_T="${3:-10}"
IDX=1
log "compute: booting full microVM (vcpus=$VCPUS mem=${MEM}MiB)"

gip=$(fc_boot_full "$IDX" "$VCPUS" "$MEM")
if [ "$gip" = "FAIL" ]; then
  fc_kill "$IDX"; echo '{"metric":"guest_compute","error":"guest_boot_failed"}'; exit 0
fi

# --- openssl AES throughput (16KB block line of `openssl speed`) ---
# parse the last column (16384-byte block) in k/s, convert to MiB/s
_aes() { # _aes <threads>
  fc_ssh "$IDX" "openssl speed -evp aes-256-cbc -multi $1 2>/dev/null | awk '/^evp/{print \$NF}' | tail -1"
}
aes1=$(_aes 1)
aesn=$(_aes "$VCPUS")

# --- stress-ng matrixprod bogo-ops/s ---
_sng() { # _sng <workers>
  fc_ssh "$IDX" "stress-ng --cpu $1 --cpu-method matrixprod --metrics-brief -t ${SNG_T}s 2>&1 \
    | awk '/cpu /{print \$(NF-1); exit}'"
}
sng1=$(_sng 1)
sngn=$(_sng "$VCPUS")

fc_kill "$IDX" 2>/dev/null

# openssl speed -evp prints k/s (1000 bytes/s units) -> MiB/s = k*1000/1048576
mib() { awk -v k="${1:-0}" 'BEGIN{printf "%.1f", (k*1000)/1048576}'; }

cat <<EOF
{
  "metric": "guest_compute",
  "vcpus": $VCPUS,
  "mem_mib": $MEM,
  "aes256_1t_mibps": $(mib "$aes1"),
  "aes256_nt_mibps": $(mib "$aesn"),
  "matrixprod_1t_bogops": ${sng1:-0},
  "matrixprod_nt_bogops": ${sngn:-0}
}
EOF
