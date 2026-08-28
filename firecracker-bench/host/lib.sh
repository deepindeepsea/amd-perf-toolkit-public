#!/usr/bin/env bash
# lib.sh — shared helpers for the Firecracker microVM benchmark harness.
# Sourced by install_firecracker.sh, bench_*.sh and run_all.sh.
# Runs ON each AWS .metal host (driven over SSM). No secrets, no network beacons.

set -uo pipefail

# ---- locations -------------------------------------------------------------
FC_ROOT="${FC_ROOT:-/opt/fcbench}"          # everything lives here
FC_BIN="$FC_ROOT/bin/firecracker"
FC_KERNEL="$FC_ROOT/img/vmlinux"            # guest kernel (uncompressed vmlinux)
FC_ROOTFS="$FC_ROOT/img/rootfs.ext4"        # golden read-only guest rootfs
FC_SSHKEY="$FC_ROOT/img/guest.id_rsa"       # ssh key baked into the CI rootfs
FC_RUN="$FC_ROOT/run"                        # per-VM scratch (sockets, overlays, logs)
FC_RESULTS="$FC_ROOT/results"                # JSON output collected by run_all
mkdir -p "$FC_ROOT/bin" "$FC_ROOT/img" "$FC_RUN" "$FC_RESULTS" 2>/dev/null || true

# ---- logging ---------------------------------------------------------------
log()  { echo "[$(date -u +%H:%M:%S)] $*" >&2; }
die()  { echo "FATAL: $*" >&2; exit 1; }

# ---- host facts (vendor-neutral; recorded with every result) ---------------
host_facts_json() {
  local model sockets cores threads vendor numa hpt
  model=$(grep -m1 'model name' /proc/cpuinfo | sed 's/.*: //')
  vendor=$(grep -m1 'vendor_id' /proc/cpuinfo | sed 's/.*: //')
  sockets=$(lscpu | awk -F: '/Socket\(s\)/{gsub(/ /,"",$2);print $2}')
  cores=$(lscpu | awk -F: '/Core\(s\) per socket/{gsub(/ /,"",$2);print $2}')
  threads=$(nproc)
  numa=$(lscpu | awk -F: '/NUMA node\(s\)/{gsub(/ /,"",$2);print $2}')
  hpt=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | sed 's/.*\[\(.*\)\].*/\1/')
  local memkb; memkb=$(awk '/MemTotal/{print $2}' /proc/meminfo)
  local itype="unknown" az="unknown"
  # IMDSv2 (token) — purely host-local metadata, never leaves the box
  local tok; tok=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null)
  if [ -n "$tok" ]; then
    itype=$(curl -s -H "X-aws-ec2-metadata-token: $tok" http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null)
    az=$(curl -s -H "X-aws-ec2-metadata-token: $tok" http://169.254.169.254/latest/meta-data/placement/availability-zone 2>/dev/null)
  fi
  cat <<EOF
{
  "instance_type": "$itype",
  "availability_zone": "$az",
  "cpu_model": "$model",
  "cpu_vendor": "$vendor",
  "sockets": ${sockets:-0},
  "cores_per_socket": ${cores:-0},
  "threads_total": ${threads:-0},
  "numa_nodes": ${numa:-0},
  "mem_total_kb": ${memkb:-0},
  "thp_enabled": "${hpt:-unknown}",
  "kernel": "$(uname -r)",
  "fc_version": "$("$FC_BIN" --version 2>/dev/null | head -1 | awk '{print $NF}')",
  "timestamp_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
}

# ---- KVM gate (Firecracker is impossible without /dev/kvm) ------------------
require_kvm() {
  [ -e /dev/kvm ] || die "NO_KVM: /dev/kvm missing — not a bare-metal host?"
  [ -w /dev/kvm ] || die "NO_KVM_WRITE: /dev/kvm not writable by $(id -un)"
  log "KVM OK: /dev/kvm present and writable"
}

# ---- networking: one TAP per microVM, /30 point-to-point -------------------
# VM index i -> host tap fc-tap<i>, host ip 10.<200+i/250>.<i%250>.1, guest .2
tap_name()  { echo "fc-tap$1"; }
host_ip()   { local i=$1; echo "10.$((200 + i/250)).$((i%250)).1"; }
guest_ip()  { local i=$1; echo "10.$((200 + i/250)).$((i%250)).2"; }
tap_mac()   { printf '06:00:AC:%02X:%02X:%02X' $(( ($1>>16)&0xff )) $(( ($1>>8)&0xff )) $(( $1&0xff )); }

setup_tap() {
  local i=$1 tap; tap=$(tap_name "$i")
  ip link del "$tap" 2>/dev/null || true
  ip tuntap add dev "$tap" mode tap 2>/dev/null
  ip addr add "$(host_ip "$i")/30" dev "$tap" 2>/dev/null
  ip link set "$tap" up 2>/dev/null
}
teardown_tap() { local tap; tap=$(tap_name "$1"); ip link del "$tap" 2>/dev/null || true; }

# ---- percentile helper (reads numbers on stdin) ----------------------------
pctl() { # pctl <p>  e.g. pctl 50  -> median
  local p=$1; sort -n | awk -v p="$p" '{a[NR]=$1} END{if(NR==0){print 0;exit} \
    idx=(p/100)*(NR-1)+1; lo=int(idx); hi=lo+1; f=idx-lo; \
    if(hi>NR)hi=NR; printf "%.3f", a[lo]+(a[hi]-a[lo])*f}'
}
mean() { awk '{s+=$1;n++} END{if(n==0)print 0; else printf "%.3f", s/n}'; }

# ---- build a writable overlay of the golden rootfs for one VM --------------
make_overlay() { # make_overlay <i> -> prints overlay path
  local i=$1 ov="$FC_RUN/rootfs-$i.ext4"
  cp --reflink=auto "$FC_ROOTFS" "$ov" 2>/dev/null || cp "$FC_ROOTFS" "$ov"
  echo "$ov"
}
