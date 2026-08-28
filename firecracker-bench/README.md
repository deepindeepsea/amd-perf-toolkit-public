# Firecracker microVM benchmark — AMD vs Intel on AWS bare-metal

A self-contained harness that measures **Firecracker** microVM performance on AWS
`.metal` instances and compares AMD EPYC against Intel Xeon on a **vendor-neutral**
basis (stock OS + native tunings only — no AOCL/AOCC, no host knobs).

Firecracker is the KVM-based microVM VMM that powers AWS Lambda and Fargate. It
requires `/dev/kvm`, so it only runs on bare-metal EC2 (`*.metal*`). This suite
quantifies the five things that actually matter for a microVM/serverless fleet:

| Metric | What it answers | How |
|---|---|---|
| **Boot latency** | How fast does one microVM cold-start? (Lambda cold start) | 50× fast-init boots (`init=/fcready`), marker on ttyS0; min/p50/p90/p99 |
| **Density** | How many microVMs fit, and how fast does the whole fleet come up? | Pack to RAM/CPU cap @256 MiB/VM, launch concurrently, count ready + fleet boot time |
| **Network** | virtio-net throughput host↔guest | iperf3 fwd / rev / parallel over a /30 TAP |
| **Block I/O** | virtio-blk IOPS & bandwidth | fio 4K randread/randwrite IOPS + 1M seqread (direct=1) |
| **Guest compute** | Useful work delivered inside a guest | openssl AES-256 + stress-ng matrixprod, 1-thread and full-vcpu |

## Instances under test (launched 2026-06-29, us-west-2a)

| Type | Vendor / CPU | vCPU | RAM | Class |
|---|---|---|---|---|
| `c8a.metal-48xl` | AMD EPYC 9R45 (Turin / Zen5) | 192 | 375 GiB | compute (2 GB/vCPU) |
| `m8a.metal-48xl` | AMD EPYC 9R45 (Turin / Zen5) | 192 | 752 GiB | general (4 GB/vCPU) |
| `c8i.metal-48xl` | Intel Xeon 6975P-C | 192 | 377 GiB | compute |
| `m8i.metal-48xl` | Intel Xeon 6975P-C | 192 | 755 GiB | general |

All four: AMI `ami-0370d56c6f7906c70` (Ubuntu 22.04), 150 GB gp3 root, tags
`claude-ssm=enabled` + `Project=firecracker-bench`, instance profile `instanceRole`
(SSM + S3). ~$10/hr each.

## Layout

```
firecracker-bench/
├── aws/                       IAM policy + role docs for the driving user
│   ├── claude-ssm-ruby-policy.json
│   └── SETUP_ACCESS.md
├── host/                      runs ON each .metal host (pushed via SSM)
│   ├── lib.sh                 shared helpers, host facts, TAP net, percentiles
│   ├── install_firecracker.sh idempotent provisioner (FC bin + CI kernel/rootfs + bake tools + NAT)
│   ├── vm.sh                  microVM launcher (fc_boot_fast / fc_boot_full / fc_ssh / fc_kill)
│   ├── bench_boot_latency.sh
│   ├── bench_density.sh
│   ├── bench_net_iperf3.sh
│   ├── bench_block_fio.sh
│   ├── bench_guest_compute.sh
│   └── run_all.sh             provision + run all 5 + emit combined JSON + S3 upload
├── orchestrate/
│   └── drive.sh               from sandbox: package→S3, SSM-dispatch detached, collect
└── results/
    ├── instances.txt
    └── collected/             result.json per host, pulled from S3
```

## How it was run (reproduction)

Prereqs on the driving box (Cowork sandbox or any host with the `claude-ssm-ruby`
credentials): `aws` CLI, AWS creds wired from `.secrets/aws-credentials`,
`AWS_DEFAULT_REGION=us-west-2`. The four `.metal` hosts must already be running and
SSM-managed (they carry tag `claude-ssm=enabled` and the `instanceRole` profile).

```bash
export AWS_SHARED_CREDENTIALS_FILE=<path>/.secrets/aws-credentials
export AWS_PROFILE=default AWS_DEFAULT_REGION=us-west-2

# one shot: upload harness to S3, dispatch detached runs, poll+collect results
bash orchestrate/drive.sh all
```

What `drive.sh` does:
1. `tar` the `host/` dir and upload to `s3://amd-pmc-toolkit-pradeepn/firecracker/harness/host.tgz`.
2. For each instance, `ssm send-command` a base64-encoded bootstrap that installs
   `awscli`, pulls + extracts the harness, and runs `run_all.sh` **detached**
   (the suite is long; the SSM call returns immediately).
3. `run_all.sh` on each host: runs `install_firecracker.sh` (idempotent), then the
   five benches, assembles one `result.json`, and uploads it to
   `s3://amd-pmc-toolkit-pradeepn/firecracker/<type>-<id>/result.json`.
4. `drive.sh` polls S3 and downloads each `result.json` into `results/collected/`.

### Running a single bench by hand (on a host)

```bash
sudo bash host/install_firecracker.sh          # once; prints INSTALL_OK
sudo bash host/bench_boot_latency.sh 50         # 50 cold boots -> JSON
sudo bash host/bench_density.sh 256 1 0.80      # 256 MiB/VM, 1 VM/thread, 80% RAM
sudo bash host/bench_net_iperf3.sh 2 1024 4 10
sudo bash host/bench_block_fio.sh 2 1024 512M 15
sudo bash host/bench_guest_compute.sh 8 2048 10
```

## Result schema (`result.json`, `firecracker-bench/v1`)

```jsonc
{
  "schema": "firecracker-bench/v1",
  "host": { instance_type, availability_zone, cpu_model, cpu_vendor,
            sockets, cores_per_socket, threads_total, numa_nodes,
            mem_total_kb, thp_enabled, kernel, fc_version, timestamp_utc },
  "results": {
    "boot_latency":  { min, p50, p90, p99, max, mean (ms), succeeded, samples[] },
    "density":       { target, ready, fleet_boot_seconds, vms_per_core, vms_per_gib, ... },
    "net_iperf3":    { fwd_gbps, rev_gbps, parallel_gbps },
    "block_fio":     { randread_iops, randwrite_iops, seqread_mibps },
    "guest_compute": { aes256_1t_mibps, aes256_nt_mibps, matrixprod_1t_bogops, matrixprod_nt_bogops }
  }
}
```

## Vendor-neutrality

Both vendors run the **same** stock Ubuntu rootfs, the **same** upstream Firecracker
release, and identical guest configs. No CPU-vendor libraries, compilers, or host
BIOS/power knobs are applied — the comparison reflects out-of-the-box microVM
behavior a customer would get on each instance family. Every result records full
host facts (CPU model, THP state, kernel, FC version) so any later question about
provenance is answerable from the JSON itself.

## Cost discipline

The four boxes are **Stopped** (never terminated) when the campaign finishes, so
they can be restarted for follow-ups without re-provisioning the root volume. All
artifacts (harness + results) live in S3 under
`s3://amd-pmc-toolkit-pradeepn/firecracker/`.
