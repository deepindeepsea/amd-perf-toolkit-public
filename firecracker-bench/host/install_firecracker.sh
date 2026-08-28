#!/usr/bin/env bash
# install_firecracker.sh — provision one AWS .metal host to run the bench suite.
# Idempotent: safe to re-run. Downloads Firecracker + a CI guest kernel & rootfs,
# bakes iperf3/fio/stress-ng/openssh into the golden rootfs, enables KVM + NAT.
#
# Run as root (SSM runs as root). All artifacts land under $FC_ROOT (/opt/fcbench).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"

ARCH="$(uname -m)"   # x86_64 on all four target families
log "install_firecracker on $(hostname) arch=$ARCH"

# 1) host packages -----------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl jq iperf3 fio stress-ng openssl openssh-client \
    e2fsprogs iproute2 bc python3 >/dev/null 2>&1 || log "apt: some pkgs may already be present"

# 2) KVM sanity (hard gate) --------------------------------------------------
modprobe kvm 2>/dev/null || true
require_kvm

# 3) Firecracker binary (latest release) -------------------------------------
if [ ! -x "$FC_BIN" ]; then
  log "fetching latest Firecracker release..."
  REL=$(curl -s https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest | jq -r .tag_name)
  [ -n "$REL" ] && [ "$REL" != null ] || die "could not resolve Firecracker latest release"
  log "  release=$REL"
  cd /tmp
  curl -sL "https://github.com/firecracker-microvm/firecracker/releases/download/${REL}/firecracker-${REL}-${ARCH}.tgz" -o fc.tgz
  tar xzf fc.tgz
  cp "release-${REL}-${ARCH}/firecracker-${REL}-${ARCH}" "$FC_BIN"
  chmod +x "$FC_BIN"
fi
log "firecracker: $("$FC_BIN" --version | head -1)"

# 4) Guest kernel + rootfs from the Firecracker CI bucket --------------------
# NOTE: the old CI bucket "spec.ccfc.amazon.com" was retired in 2026 and now 404s.
# The live bucket is "spec.ccfc.min". v1.10 is the newest CI track that still ships
# a writable ubuntu-22.04.ext4 + matching id_rsa (v1.11+ moved to read-only squashfs,
# which our chroot tool-bake + ssh-driven tests can't use).
CI_BUCKET="https://s3.amazonaws.com/spec.ccfc.min"
CI_VERSION="v1.10"   # CI artifact track that ships a known-good vmlinux + ubuntu rootfs
if [ ! -f "$FC_KERNEL" ]; then
  log "fetching guest kernel (vmlinux) from CI bucket..."
  KKEY=$(curl -s "${CI_BUCKET}/?list-type=2&prefix=firecracker-ci/${CI_VERSION}/${ARCH}/vmlinux-" \
        | tr '<' '\n' | grep '^Key>' | sed 's/Key>//' \
        | grep -E 'vmlinux-[0-9][0-9.]*[0-9]$' | grep -v 'no-acpi' | sort -V | tail -1)
  [ -n "$KKEY" ] || die "no CI vmlinux found under ${CI_VERSION}/${ARCH}"
  curl -sL "${CI_BUCKET}/${KKEY}" -o "$FC_KERNEL"
  log "  kernel=$KKEY ($(du -h "$FC_KERNEL" | cut -f1))"
fi
if [ ! -f "$FC_ROOTFS" ]; then
  log "fetching guest rootfs (ubuntu ext4) from CI bucket..."
  RKEY=$(curl -s "${CI_BUCKET}/?list-type=2&prefix=firecracker-ci/${CI_VERSION}/${ARCH}/ubuntu-" \
        | tr '<' '\n' | grep '^Key>' | sed 's/Key>//' | grep '\.ext4$' | sort -V | tail -1)
  [ -n "$RKEY" ] || die "no CI ubuntu rootfs found under ${CI_VERSION}/${ARCH}"
  curl -sL "${CI_BUCKET}/${RKEY}" -o "$FC_ROOTFS"
  # matching ssh key (same basename, .id_rsa)
  IDKEY="${RKEY%.ext4}.id_rsa"
  curl -sL "${CI_BUCKET}/${IDKEY}" -o "$FC_SSHKEY" 2>/dev/null && chmod 600 "$FC_SSHKEY" || log "  (no ssh key alongside rootfs)"
  log "  rootfs=$RKEY ($(du -h "$FC_ROOTFS" | cut -f1))"
fi

# 5) Bake benchmark tools + fast-init into the golden rootfs (once) ----------
# The CI ubuntu-22.04 rootfs already ships iperf3, fio, ssh/sshd, but NOT
# openssl or stress-ng -- and its dpkg DB is stripped, so in-guest `apt` is
# broken ("flAbsPath on /var/lib/dpkg/status failed"). So instead of chroot+apt
# we COPY openssl + stress-ng (with their shared libs) from THIS host, which runs
# the same Ubuntu 22.04 family (glibc-compatible). Vendor-neutral: byte-identical
# stock binaries land in both the AMD and Intel guests.
STAMP="$FC_ROOT/img/.tools_baked"
if [ ! -f "$STAMP" ]; then
  log "preparing golden rootfs (stage openssl+stress-ng, fast-init, ssh)..."
  e2fsck -fy "$FC_ROOTFS" >/dev/null 2>&1 || true
  resize2fs "$FC_ROOTFS" 4G >/dev/null 2>&1 || true
  MNT=$(mktemp -d)
  mount -o loop "$FC_ROOTFS" "$MNT"
  mkdir -p "$MNT/tmp" && chmod 1777 "$MNT/tmp"

  # copy a host binary plus its dynamic-linker + shared libs into the guest root
  copy_with_libs() { # <binary-name>
    local src dst; src=$(command -v "$1" 2>/dev/null) || { log "  host missing $1"; return 1; }
    dst="$MNT$src"; mkdir -p "$(dirname "$dst")"; cp -a "$src" "$dst"
    ldd "$src" 2>/dev/null | awk '/=>/{print $3} /ld-linux/{print $1}' | while read -r lib; do
      [ -n "$lib" ] && [ -f "$lib" ] || continue
      # -L derefs symlinks so the real .so content lands (ldd only names libFoo.so.1,
      # not its libFoo.so.1.x.y target; plain cp would copy a dangling symlink).
      tgt="${lib/#\/lib\//\/usr\/lib\/}"; mkdir -p "$MNT$(dirname "$tgt")"; cp -anL "$lib" "$MNT$tgt" 2>/dev/null || true
    done
  }
  STAGED=""
  for t in openssl stress-ng; do
    copy_with_libs "$t" && STAGED="$STAGED $t" || log "  (could not stage $t; guest_compute degrades)"
  done
  # openssl 3.0 providers (AES lives in the built-in default provider, but copy
  # the ossl-modules dir too in case the legacy provider is referenced)
  if [ -d /usr/lib/x86_64-linux-gnu/ossl-modules ]; then
    mkdir -p "$MNT/usr/lib/x86_64-linux-gnu/ossl-modules"
    cp -an /usr/lib/x86_64-linux-gnu/ossl-modules/* "$MNT/usr/lib/x86_64-linux-gnu/ossl-modules/" 2>/dev/null || true
  fi
  ldconfig -r "$MNT" 2>/dev/null || true

  # fast-init marker for pure VMM+kernel+init boot-latency (bypasses systemd).
  # Boot the guest with kernel arg  init=/fcready  to use it.
  cat > "$MNT/fcready" <<'FCR'
#!/bin/sh
# earliest possible userspace signal, then power off immediately
echo "FC_READY_MARKER" > /dev/ttyS0
reboot -f
FCR
  chmod +x "$MNT/fcready"
  # ensure key-based root login works for ssh-driven tests
  mkdir -p "$MNT/root/.ssh" && chmod 700 "$MNT/root/.ssh"
  if [ -f "$FC_SSHKEY" ]; then ssh-keygen -y -f "$FC_SSHKEY" > "$MNT/root/.ssh/authorized_keys" 2>/dev/null || true; fi
  chmod 600 "$MNT/root/.ssh/authorized_keys" 2>/dev/null || true
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' "$MNT/etc/ssh/sshd_config" 2>/dev/null || true
  sync
  umount "$MNT" 2>/dev/null
  rmdir "$MNT" 2>/dev/null
  e2fsck -fy "$FC_ROOTFS" >/dev/null 2>&1 || true
  touch "$STAMP"
  log "  rootfs prepared (staged:${STAGED:- none}; iperf3/fio/ssh already in image)."
fi

# 6) host-side networking for guest internet (NAT) ---------------------------
DEFIF=$(ip route | awk '/default/{print $5; exit}')
sysctl -qw net.ipv4.ip_forward=1
iptables -t nat -C POSTROUTING -o "$DEFIF" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -o "$DEFIF" -j MASQUERADE
iptables -P FORWARD ACCEPT 2>/dev/null || true

log "INSTALL OK on $(hostname): firecracker + kernel + rootfs ready under $FC_ROOT"
echo "INSTALL_OK"
