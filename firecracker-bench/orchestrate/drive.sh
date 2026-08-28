#!/usr/bin/env bash
# drive.sh — orchestrate the Firecracker bench across the 4 .metal hosts via SSM.
# Runs FROM the Cowork sandbox (or any box with aws CLI + claude-ssm-ruby creds).
#
# Flow:
#   1. tar the host/ harness, upload to S3 (sandbox has s3:Put on the bucket).
#   2. SSM each instance: install awscli, pull+extract harness, run run_all.sh
#      DETACHED (nohup) so the SSM call returns immediately (the suite is long).
#   3. Poll S3 for each host's result.json; collect them locally when they land.
#
# Usage: drive.sh [start|collect|all]
#   start   - upload harness + kick off detached runs on all hosts
#   collect - poll S3 and download results that have appeared
#   all     - start, then poll-collect until all 4 are in (default)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# --- config -----------------------------------------------------------------
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
BUCKET="amd-pmc-toolkit-pradeepn"
S3_FC="s3://$BUCKET/firecracker"
S3_HARNESS="$S3_FC/harness/host.tgz"
INSTANCES=(
  i-03715154062628431   # c8a.metal-48xl  AMD Turin
  i-0e3981d986ced8234   # m8a.metal-48xl  AMD Turin
  i-0f4f63489da467524   # c8i.metal-48xl  Intel
  i-09de4ef7e0ce8c641   # m8i.metal-48xl  Intel
)
LOCAL_OUT="$ROOT/results/collected"
mkdir -p "$LOCAL_OUT"

aws() { command aws --region "$REGION" "$@"; }
log() { echo "[$(date -u +%H:%M:%S)] $*" >&2; }

# --- 1. package + upload harness -------------------------------------------
upload_harness() {
  log "packaging harness from $ROOT/host"
  tar -C "$ROOT" -czf /tmp/host.tgz host
  aws s3 cp /tmp/host.tgz "$S3_HARNESS" >/dev/null
  log "uploaded harness -> $S3_HARNESS ($(du -h /tmp/host.tgz | cut -f1))"
}

# --- 2. kick off a detached run on one host --------------------------------
# Bootstrap: ensure awscli, pull harness, run run_all detached, drop a heartbeat.
# The bootstrap is base64-encoded so the SSM command is ONE clean line with no
# embedded quotes/newlines (avoids the worst SSM CLI quoting pitfalls).
start_one() {
  local iid=$1
  local boot b64
  boot=$(cat <<BOOT
set -e
export DEBIAN_FRONTEND=noninteractive
command -v aws >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq awscli >/dev/null 2>&1) || (snap install aws-cli --classic >/dev/null 2>&1) || true
mkdir -p /opt/fcbench
aws s3 cp $S3_HARNESS /tmp/host.tgz --region $REGION
tar -C /opt/fcbench -xzf /tmp/host.tgz
chmod +x /opt/fcbench/host/*.sh
echo "STARTED \$(date -u +%FT%TZ)" > /opt/fcbench/run.heartbeat
nohup bash /opt/fcbench/host/run_all.sh $S3_FC > /opt/fcbench/run.log 2>&1 &
echo "DISPATCHED pid=\$!"
BOOT
)
  b64=$(echo "$boot" | base64 -w0)
  local cid
  cid=$(aws ssm send-command \
    --instance-ids "$iid" \
    --document-name "AWS-RunShellScript" \
    --comment "firecracker-bench start" \
    --parameters "commands=[\"echo $b64 | base64 -d > /tmp/fcboot.sh\",\"nohup bash /tmp/fcboot.sh > /opt/fcbench-boot.log 2>&1 &\",\"sleep 1; echo dispatched\"]" \
    --query "Command.CommandId" --output text 2>&1)
  log "  $iid -> command $cid"
}

start_all() {
  upload_harness
  for iid in "${INSTANCES[@]}"; do start_one "$iid"; done
  log "all hosts dispatched; results will appear under $S3_FC/<type>-<id>/result.json"
}

# --- 3. collect results from S3 --------------------------------------------
collect_once() {
  local got=0
  # list every result.json under the firecracker prefix
  for key in $(aws s3 ls "$S3_FC/" --recursive 2>/dev/null | awk '/result\.json$/{print $4}'); do
    local base; base=$(echo "$key" | awk -F/ '{print $(NF-1)}')
    local dest="$LOCAL_OUT/${base}.json"
    aws s3 cp "s3://$BUCKET/$key" "$dest" >/dev/null 2>&1 && got=$((got+1))
  done
  echo "$got"
}

collect_loop() {
  local deadline=$(( $(date +%s) + 2400 ))   # up to 40 min
  while :; do
    local n; n=$(collect_once)
    log "collected $n/4 result.json so far"
    [ "$n" -ge 4 ] && { log "all 4 in."; break; }
    [ "$(date +%s)" -ge "$deadline" ] && { log "timeout waiting for results"; break; }
    sleep 60
  done
  ls -la "$LOCAL_OUT"
}

case "${1:-all}" in
  start)   start_all ;;
  collect) collect_loop ;;
  all)     start_all; collect_loop ;;
  *) echo "usage: drive.sh [start|collect|all]"; exit 1 ;;
esac
