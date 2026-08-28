#!/usr/bin/env bash
# run_all.sh — provision (if needed) then run the whole Firecracker bench suite
# on THIS host and emit one combined JSON result. Designed to be driven over SSM.
# Uploads the result to S3 if awscli + a bucket prefix are available.
#
# Usage: run_all.sh [s3_prefix]
#   s3_prefix e.g. s3://amd-pmc-toolkit-pradeepn/firecracker
# The per-host object key is <prefix>/<instance-type>-<instance-id>/result.json
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"

S3_PREFIX="${1:-}"

log "=== run_all on $(hostname) ==="
# 1) make sure the host is provisioned (idempotent)
bash "$HERE/install_firecracker.sh" >/tmp/install.log 2>&1 || { cat /tmp/install.log >&2; die "install failed"; }
tail -1 /tmp/install.log >&2

# 2) gather host facts
FACTS=$(host_facts_json)
ITYPE=$(echo "$FACTS" | awk -F'"' '/instance_type/{print $4}')
[ -n "$ITYPE" ] && [ "$ITYPE" != "unknown" ] || ITYPE=$(hostname)

# helper: run a bench, capture JSON (last {...} block), tolerate failure
_run() { # _run <name> <script> [args...]
  local name=$1; shift
  log ">> bench: $name"
  local out; out=$(bash "$@" 2>>/tmp/bench_$name.err)
  # keep only from first { to last } (strip any stray log lines)
  out=$(echo "$out" | sed -n '/^{/,/^}/p')
  [ -n "$out" ] || out="{\"metric\":\"$name\",\"error\":\"no_output\"}"
  echo "$out"
}

BOOT=$(_run boot    "$HERE/bench_boot_latency.sh" 50)
DENS=$(_run density "$HERE/bench_density.sh" 256 1 0.80)
NET=$(_run net      "$HERE/bench_net_iperf3.sh" 2 1024 4 10)
BLK=$(_run block    "$HERE/bench_block_fio.sh" 2 1024 512M 15)
CMP=$(_run compute  "$HERE/bench_guest_compute.sh" 8 2048 10)

# 3) assemble combined result
RESULT="$FC_RESULTS/result.json"
cat > "$RESULT" <<EOF
{
  "schema": "firecracker-bench/v1",
  "host": $FACTS,
  "results": {
    "boot_latency": $BOOT,
    "density": $DENS,
    "net_iperf3": $NET,
    "block_fio": $BLK,
    "guest_compute": $CMP
  }
}
EOF
log "wrote $RESULT ($(wc -c < "$RESULT") bytes)"

# 4) upload to S3 if we can
if [ -n "$S3_PREFIX" ]; then
  IID=$(curl -s -H "X-aws-ec2-metadata-token: $(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" \
        http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)
  KEY="$S3_PREFIX/${ITYPE}-${IID:-unknown}/result.json"
  if command -v aws >/dev/null 2>&1; then
    aws s3 cp "$RESULT" "$KEY" >/dev/null 2>&1 && log "uploaded -> $KEY" || log "S3 upload FAILED ($KEY)"
  else
    log "aws CLI not on host; skipping S3 upload"
  fi
fi

echo "RUN_ALL_DONE ${ITYPE}"
cat "$RESULT"
