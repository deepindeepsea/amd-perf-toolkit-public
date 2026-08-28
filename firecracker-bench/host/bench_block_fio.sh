#!/usr/bin/env bash
# bench_block_fio.sh — virtio-block I/O inside one full microVM.
# Runs fio against a scratch file on the guest's writable overlay rootfs:
#   * 4K random read  IOPS (iodepth 32, libaio)
#   * 4K random write IOPS
#   * 1M sequential read bandwidth
# Direct=1 so we exercise the virtio-blk path, not the guest page cache.
#
# Output: JSON {randread_iops, randwrite_iops, seqread_mibps, vcpus, mem_mib}.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"
source "$HERE/vm.sh"

VCPUS="${1:-2}"; MEM="${2:-1024}"; SIZE="${3:-512M}"; RUNT="${4:-15}"
IDX=1
log "block: booting full microVM (vcpus=$VCPUS mem=${MEM}MiB) for fio"

gip=$(fc_boot_full "$IDX" "$VCPUS" "$MEM")
if [ "$gip" = "FAIL" ]; then
  fc_kill "$IDX"; echo '{"metric":"block_fio","error":"guest_boot_failed"}'; exit 0
fi

# helper: run one fio job in the guest, echo the requested JSON scalar
_fio() { # _fio <name> <rw> <bs> <jqpath-awk-key>
  fc_ssh "$IDX" "fio --name=$1 --filename=/root/fio.dat --size=$SIZE --rw=$2 --bs=$3 \
    --ioengine=libaio --iodepth=32 --direct=1 --runtime=$RUNT --time_based \
    --group_reporting --output-format=json 2>/dev/null" 2>/dev/null
}

rr=$(_fio randread randread 4k  | awk -F'[:,]' '/"read"/{f=1} f&&/"iops"/{gsub(/ /,"",$2);print $2; exit}')
rw=$(_fio randwrite randwrite 4k | awk -F'[:,]' '/"write"/{f=1} f&&/"iops"/{gsub(/ /,"",$2);print $2; exit}')
sb=$(_fio seqread read 1m | awk -F'[:,]' '/"read"/{f=1} f&&/"bw"/{gsub(/ /,"",$2);print $2; exit}')

fc_ssh "$IDX" "rm -f /root/fio.dat" 2>/dev/null
fc_kill "$IDX" 2>/dev/null

# fio bw is in KiB/s -> MiB/s
sbm=$(awk -v k="${sb:-0}" 'BEGIN{printf "%.1f", k/1024}')
ri=$(awk -v v="${rr:-0}" 'BEGIN{printf "%.0f", v}')
wi=$(awk -v v="${rw:-0}" 'BEGIN{printf "%.0f", v}')

cat <<EOF
{
  "metric": "block_fio",
  "vcpus": $VCPUS,
  "mem_mib": $MEM,
  "file_size": "$SIZE",
  "runtime_s": $RUNT,
  "randread_iops": $ri,
  "randwrite_iops": $wi,
  "seqread_mibps": $sbm
}
EOF
