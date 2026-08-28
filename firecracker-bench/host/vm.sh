#!/usr/bin/env bash
# vm.sh — boot/stop one Firecracker microVM. Sourced by the bench_*.sh scripts.
# Two boot modes:
#   fast  -> kernel arg init=/fcready, guest prints FC_READY_MARKER then powers off
#            (measures pure VMM+kernel+init cold start, the ~125ms-class number)
#   full  -> normal systemd boot with virtio-net + writable rootfs, used by the
#            net / block / compute workloads (reached over ssh on the guest IP)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"

COMMON_ARGS="console=ttyS0 reboot=k panic=1 pci=off nomodules random.trust_cpu=on i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd"

# write a firecracker config file for VM index $1
# args: idx vcpus mem_mib mode(fast|full) rootfs_path
_fc_write_config() {
  local i=$1 vcpus=$2 mem=$3 mode=$4 rootfs=$5
  local cfg="$FC_RUN/cfg-$i.json" boot_args net_block=""
  if [ "$mode" = fast ]; then
    boot_args="$COMMON_ARGS init=/fcready"
  else
    # full boot: configure guest NIC via kernel ip= (point-to-point /30)
    boot_args="$COMMON_ARGS ip=$(guest_ip "$i")::$(host_ip "$i"):255.255.255.252::eth0:off"
    net_block=$(cat <<NET
  ,"network-interfaces": [
    {"iface_id":"eth0","host_dev_name":"$(tap_name "$i")","guest_mac":"$(tap_mac "$i")"}
  ]
NET
)
  fi
  cat > "$cfg" <<EOF
{
  "boot-source": {"kernel_image_path": "$FC_KERNEL", "boot_args": "$boot_args"},
  "drives": [
    {"drive_id":"rootfs","path_on_host":"$rootfs","is_root_device":true,"is_read_only":false}
  ],
  "machine-config": {"vcpu_count": $vcpus, "mem_size_mib": $mem}${net_block}
}
EOF
  echo "$cfg"
}

# fc_boot_fast <idx> -> echoes boot latency in milliseconds (marker-based)
fc_boot_fast() {
  local i=$1; local sock="$FC_RUN/fc-$i.sock" con="$FC_RUN/con-$i.log"
  local ov; ov=$(make_overlay "$i")
  local cfg; cfg=$(_fc_write_config "$i" 1 128 fast "$ov")
  rm -f "$sock" "$con"
  local t0 t1
  t0=$(date +%s.%N)
  "$FC_BIN" --api-sock "$sock" --config-file "$cfg" >"$con" 2>&1 &
  local pid=$!
  # spin until the guest prints the marker (or timeout 5s)
  local deadline=$((SECONDS+5))
  while ! grep -q FC_READY_MARKER "$con" 2>/dev/null; do
    kill -0 "$pid" 2>/dev/null || break
    [ $SECONDS -ge $deadline ] && break
    :
  done
  t1=$(date +%s.%N)
  kill -9 "$pid" 2>/dev/null
  rm -f "$ov" "$sock" "$cfg"
  if grep -q FC_READY_MARKER "$con" 2>/dev/null; then
    awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f",(b-a)*1000}'
  else
    echo "-1"   # boot failed / timed out
  fi
}

# fc_boot_full <idx> <vcpus> <mem_mib> -> echoes guest IP once ssh answers (or FAIL)
fc_boot_full() {
  local i=$1 vcpus=${2:-2} mem=${3:-1024}
  local sock="$FC_RUN/fc-$i.sock" con="$FC_RUN/con-$i.log"
  setup_tap "$i"
  local ov; ov=$(make_overlay "$i")
  local cfg; cfg=$(_fc_write_config "$i" "$vcpus" "$mem" full "$ov")
  rm -f "$sock" "$con"
  "$FC_BIN" --api-sock "$sock" --config-file "$cfg" >"$con" 2>&1 &
  echo $! > "$FC_RUN/pid-$i"
  local gip; gip=$(guest_ip "$i")
  # wait for ssh (full systemd boot ~1-3s)
  local deadline=$((SECONDS+40))
  while [ $SECONDS -lt $deadline ]; do
    if ssh -i "$FC_SSHKEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
         -o ConnectTimeout=2 -o LogLevel=ERROR root@"$gip" true 2>/dev/null; then
      echo "$gip"; return 0
    fi
    sleep 1
  done
  echo "FAIL"; return 1
}

# run a command inside guest <idx>
fc_ssh() {
  local i=$1; shift
  ssh -i "$FC_SSHKEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=3 -o LogLevel=ERROR root@"$(guest_ip "$i")" "$@"
}

fc_kill() {
  local i=$1
  [ -f "$FC_RUN/pid-$i" ] && kill -9 "$(cat "$FC_RUN/pid-$i")" 2>/dev/null
  rm -f "$FC_RUN/pid-$i" "$FC_RUN/fc-$i.sock" "$FC_RUN/cfg-$i.json" "$FC_RUN/rootfs-$i.ext4"
  teardown_tap "$i"
}

# kill every VM this harness started
fc_kill_all() {
  pkill -9 -f "$FC_BIN" 2>/dev/null || true
  for t in "$FC_RUN"/pid-*; do [ -e "$t" ] || continue; rm -f "$t"; done
  for l in $(ip -o link show | awk -F': ' '/fc-tap/{print $2}'); do ip link del "$l" 2>/dev/null; done
  rm -f "$FC_RUN"/rootfs-*.ext4 "$FC_RUN"/fc-*.sock "$FC_RUN"/cfg-*.json 2>/dev/null || true
}
