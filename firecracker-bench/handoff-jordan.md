# Firecracker microVM Benchmark — Handoff to Jordan

**From:** Pradeep Nallimelli (AMD FAE)  
**Date:** 2026-08-27  
**Subject:** AMD vs Intel Firecracker benchmark on AWS bare-metal — methodology, results, and porting guide for Oracle Cloud

---

## What This Is

A self-contained benchmark harness that measures Firecracker microVM performance and compares AMD EPYC Turin (Zen5) vs Intel Xeon Granite Rapids on AWS bare-metal instances. Firecracker is the KVM-based microVM monitor that powers AWS Lambda and Fargate — it's increasingly used by the agentic AI / sandbox-as-a-service market (E2B, Modal, Cursor, etc.) as well. The goal was a **vendor-neutral, reproducible** comparison covering every layer that actually matters for a microVM platform.

---

## Instances Tested (AWS, us-west-2a, run 2026-06-29)

| Instance | CPU | vCPU | RAM | Notes |
|---|---|---|---|---|
| `c8a.metal-48xl` | AMD EPYC 9R45 (Turin / Zen5) | 192 | 375 GiB | Compute class |
| `m8a.metal-48xl` | AMD EPYC 9R45 (Turin / Zen5) | 192 | 752 GiB | General purpose |
| `c8i.metal-48xl` | Intel Xeon 6975P-C (Granite Rapids) | 192 | 377 GiB | Compute class |
| `m8i.metal-48xl` | Intel Xeon 6975P-C (Granite Rapids) | 192 | 755 GiB | General purpose |

All four: Ubuntu 22.04 LTS (AMI `ami-0370d56c6f7906c70`), Firecracker v1.16.0, guest kernel 6.8.0-1057-aws. **Bare-metal is required** — Firecracker needs `/dev/kvm`, which AMD exposes only on metal instances on AWS (Intel virtual instances do support nested virt as of Feb 2026, but bare-metal was used for a fair, like-for-like comparison here).

---

## Benchmark Suite — 5 Metrics

| Metric | Tool | What it measures |
|---|---|---|
| **Boot latency** | Custom `init=/fcready` + ttyS0 marker | Cold-start p50/p90/p99 (ms) — 50 samples |
| **Fleet density** | Custom launcher | Max microVMs packed at 256 MiB/VM; fleet boot time |
| **virtio-net** | iperf3 over TAP | Host→guest, guest→host, 4-stream parallel (Gbps) |
| **virtio-blk** | fio direct I/O | 4K randread/randwrite IOPS + 1M seqread (MiB/s) |
| **Guest compute** | openssl speed EVP | AES-256-GCM throughput (1-thread + 8-thread, MiB/s) |

**Note on excluded metric:** stress-ng `matrixprod` was initially collected but removed from the report. ISA inspection (`/proc/cpuinfo`) confirmed `amx_tile` and `amx_bf16` are exposed on Intel Xeon 6975P-C guests, meaning `matrixprod` dispatches into Intel AMX tile registers — a hardware extension AMD EPYC Turin does not have. Including it would be like comparing GPUs on a CUDA-only workload. AES-256-GCM is retained as the compute benchmark because both vendors support AES-NI / VAES.

---

## Key Results (AMD vs Intel geomean ratios)

| Metric | Winner | Ratio |
|---|---|---|
| Cold-start boot latency | **AMD** | 1.76× faster (~100 ms vs ~176 ms) |
| virtio-net throughput | **AMD** | 1.79× higher |
| virtio-blk IOPS | **AMD** | 1.99× higher |
| AES-256-GCM (full socket, 8-thread) | Intel | 1.11× (10,545 vs 9,480 MiB/s) |
| **AES-256-GCM per-vCPU (8-vCPU vm, multi-buffer)** | **AMD** | **2.19× higher** |

The per-vCPU AES result is the more operationally relevant one for microVM tenants: a Firecracker guest gets 1–8 vCPUs, not a full 96-vCPU socket. On 8-vCPU virtual instances (m8a.2xlarge vs m8i.2xlarge, run separately), AMD delivers 2.19× higher aggregate AES-256-GCM at TLS record sizes (8 KB). AMD EPYC Turin has two 256-bit VAES execution units vs Intel GNR's single path.

---

## Repo Layout

```
firecracker-bench/
├── host/
│   ├── install_firecracker.sh    # idempotent provisioner (FC + CI kernel + rootfs + tools baked)
│   ├── lib.sh                    # helpers: host facts, TAP net, percentile math
│   ├── vm.sh                     # microVM launcher (fc_boot_fast / fc_boot_full / fc_kill)
│   ├── bench_boot_latency.sh
│   ├── bench_density.sh
│   ├── bench_net_iperf3.sh
│   ├── bench_block_fio.sh
│   ├── bench_guest_compute.sh
│   └── run_all.sh                # provision + run all 5 benches + emit result.json
├── orchestrate/
│   └── drive.sh                  # from driver box: push to S3, SSM-dispatch, poll+collect
├── results/
│   └── run-20260629/             # raw result.json per host (c8a, m8a, c8i, m8i)
├── firecracker-amd-vs-intel-report.html   # full report (dark theme)
├── firecracker-amd-vs-intel-report.pdf    # PDF version
└── README.md
```

All artifacts (harness + result JSONs) are also in S3:
`s3://amd-pmc-toolkit-pradeepn/firecracker/run-20260629/`

---

## How to Run (Reproduction Steps)

### Prerequisites on the driver box

- AWS CLI with credentials that have `ec2:*`, `ssm:SendCommand`, `ssm:GetCommandInvocation`, `s3:*` on the relevant bucket
- Bare-metal instances running, SSM agent active (tag `claude-ssm=enabled` + `instanceRole` IAM profile attached)

```bash
export AWS_SHARED_CREDENTIALS_FILE=<path>/.secrets/aws-credentials
export AWS_DEFAULT_REGION=us-west-2

# One-shot: package harness → S3, dispatch detached runs on all 4 hosts, poll + collect
bash orchestrate/drive.sh all
```

`drive.sh` does: (1) tars `host/` and uploads to S3, (2) SSM-dispatches a bootstrap on each host that pulls the harness and runs `run_all.sh` detached, (3) polls S3 until all `result.json` files appear, (4) downloads them to `results/collected/`.

### Running a single bench by hand (on the host)

```bash
sudo bash host/install_firecracker.sh            # once per host; prints INSTALL_OK
sudo bash host/bench_boot_latency.sh 50          # 50 cold boots → JSON
sudo bash host/bench_net_iperf3.sh 2 1024 4 10
sudo bash host/bench_block_fio.sh 2 1024 512M 15
sudo bash host/bench_guest_compute.sh 8 2048 10
```

---

## Porting to Oracle Cloud — What You Need to Know

Jordan, here are the key things to sort out before running this on OCI:

**1. Bare-metal instance selection**  
Firecracker requires `/dev/kvm`. On OCI the right shapes are:
- AMD: `BM.Standard.E5.192` (EPYC Genoa, 192 cores) or `BM.Standard.E4.128` (Milan)
- Intel: `BM.Standard3.64` (Ice Lake) or `BM.Optimized3.36` (Icelake, high-freq)
- Do NOT use VM shapes — nested virt behavior on OCI AMD shapes needs verification first (same issue as AWS)

**2. Provisioning differences**  
The harness uses `awscli` + SSM for remote execution. On OCI you'll replace these with either:
- OCI Bastion + SSH direct, or
- OCI Run Command (similar to SSM)
The actual benchmark scripts (`host/`) are cloud-agnostic bash — no AWS-specific calls inside them.

**3. Networking**  
`install_firecracker.sh` sets up a TAP interface + NAT (`iptables`) for guest networking. This is OS-level and cloud-agnostic. Verify the OCI bare-metal image allows `ip tuntap add` and `iptables -t nat` (should be fine on bare-metal).

**4. Guest kernel + rootfs**  
The harness pulls the Firecracker CI kernel (`vmlinux-6.8.0-1057-aws`) and Ubuntu 22.04 rootfs from the Firecracker GitHub release CDN. These are architecture-specific (x86_64) but not AWS-specific — they'll work identically on OCI bare-metal.

**5. S3 bucket**  
`run_all.sh` uploads `result.json` to `s3://amd-pmc-toolkit-pradeepn/`. You'll want to either:
- Point it at an OCI Object Storage bucket (swap the `aws s3 cp` lines for `oci os object put`), or
- Simply comment out the upload and collect results via SSH/scp instead — the result JSON is also written locally to `/var/tmp/fc-bench/result.json` on each host

**6. AMI → OCI Image**  
Replace `ami-0370d56c6f7906c70` with an Ubuntu 22.04 OCI image. The harness auto-detects CPU model and host facts from `/proc/cpuinfo` and `lscpu`, so no hardcoding to change.

**7. ISA flag check (important for compute benchmark validity)**  
Before running, verify ISA flags on your OCI Intel shape:
```bash
grep -oP '(amx_tile|amx_bf16|vaes|avx512f)' /proc/cpuinfo | sort -u
```
If `amx_tile` is present on the Intel shape, exclude the matrixprod stressor (it's already excluded in our current version) and stick to AES-256-GCM for the compute metric — same call we made here.

---

## Files to Send Jordan

- `firecracker-amd-vs-intel-report.pdf` — the full results report with methodology
- `host/` directory — the complete benchmark scripts
- `results/run-20260629/` — raw JSON results from AWS run (for baseline comparison)
- This handoff doc

---

## Contact / Questions

Pradeep Nallimelli — pradeepn@amd.com  
Repo: `github.com/deepindeepsea/amd-perf-toolkit` (private — share access if needed)  
S3 raw results: `s3://amd-pmc-toolkit-pradeepn/firecracker/run-20260629/`
