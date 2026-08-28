#!/usr/bin/env bash
# bench_net_iperf3.sh — virtio-net throughput host<->guest over the TAP /30.
# Boots one full microVM (systemd, virtio-net), runs iperf3 server in the guest
# and the client on the host. Measures both directions plus a parallel-stream
# run to expose multi-queue / vhost scaling.
#
# Output: JSON {fwd_gbps (host->guest), rev_gbps (guest->host), parallel_gbps,
# streams, vcpus, mem_mib}.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"
source "$HERE/vm.sh"

VCPUS="${1:-2}"; MEM="${2:-1024}"; STREAMS="${3:-4}"; DUR="${4:-10}"
IDX=1
log "net: booting full microVM (vcpus=$VCPUS mem=${MEM}MiB) for iperf3"

gip=$(fc_boot_full "$IDX" "$VCPUS" "$MEM")
if [ "$gip" = "FAIL" ]; then
  fc_kill "$IDX"; echo '{"metric":"net_iperf3","error":"guest_boot_failed"}'; exit 0
fi

# start iperf3 server in guest
fc_ssh "$IDX" "pkill iperf3 2>/dev/null; nohup iperf3 -s >/tmp/ip.log 2>&1 &" 2>/dev/null
sleep 2

# host->guest (default), guest->host (-R), and parallel streams
fwd=$(iperf3 -c "$gip" -t "$DUR" -J 2>/dev/null | awk -F'[:,]' '/sum_received/{f=1} f&&/bits_per_second/{print $2; exit}')
rev=$(iperf3 -c "$gip" -t "$DUR" -R -J 2>/dev/null | awk -F'[:,]' '/sum_received/{f=1} f&&/bits_per_second/{print $2; exit}')
par=$(iperf3 -c "$gip" -t "$DUR" -P "$STREAMS" -J 2>/dev/null | awk -F'[:,]' '/sum_received/{f=1} f&&/bits_per_second/{print $2; exit}')

fc_kill "$IDX" 2>/dev/null

g() { awk -v b="${1:-0}" 'BEGIN{printf "%.3f", b/1e9}'; }
cat <<EOF
{
  "metric": "net_iperf3",
  "vcpus": $VCPUS,
  "mem_mib": $MEM,
  "streams": $STREAMS,
  "duration_s": $DUR,
  "fwd_gbps": $(g "$fwd"),
  "rev_gbps": $(g "$rev"),
  "parallel_gbps": $(g "$par")
}
EOF
