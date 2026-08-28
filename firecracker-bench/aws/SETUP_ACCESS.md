# Firecracker bench — AWS access setup (one-time, admin)

Account `323508741427`, region `us-west-2`. Two pieces:

1. **`FirecrackerBenchRole`** — an EC2 *instance* role the bench hosts assume so they
   (a) self-register with SSM (`AmazonSSMManagedInstanceCore`) and (b) can write
   results to `s3://amd-pmc-toolkit-pradeepn/firecracker/`.
2. **Policy additions to the `claude-ssm-ruby` user** — so the agent can launch the
   metal hosts, attach that role, drive them via SSM, and collect from S3.

Currently `claude-ssm-ruby` is **launch-only and SSM-to-already-managed-only**:
verified denials on `iam:PassRole`, `ec2:CreateKeyPair`, `ec2:CreateSecurityGroup`,
`ec2:StartInstances`. The grants below close exactly the gaps needed — no SSH key /
security-group creation required, because everything runs over SSM.

## 1. Create the instance role + profile (admin)

```bash
# trust + role
aws iam create-role --role-name FirecrackerBenchRole \
  --assume-role-policy-document file://instance-role-trust.json

# SSM agent registration (the thing that makes the host reachable)
aws iam attach-role-policy --role-name FirecrackerBenchRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# result-bucket write
aws iam put-role-policy --role-name FirecrackerBenchRole \
  --policy-name FirecrackerResultsS3 \
  --policy-document file://instance-role-s3-inline.json

# instance profile (EC2 attaches profiles, not roles directly)
aws iam create-instance-profile --instance-profile-name FirecrackerBenchRole
aws iam add-role-to-instance-profile \
  --instance-profile-name FirecrackerBenchRole --role-name FirecrackerBenchRole
```

## 2. Grant the agent user the missing actions (admin)

```bash
aws iam put-user-policy --user-name claude-ssm-ruby \
  --policy-name FirecrackerBenchDrive \
  --policy-document file://claude-ssm-ruby-policy.json
```

`claude-ssm-ruby-policy.json` is least-privilege: `iam:PassRole` is scoped to
**only** `FirecrackerBenchRole` and only when passed to `ec2.amazonaws.com`. No
broad IAM, no key/SG creation.

## If you'd rather launch the hosts yourself (option 2)

Launch each of `c8a.metal-48xl`, `m8a.metal-48xl`, `c8i.metal-48xl`,
`m8i.metal-48xl` in us-west-2 with:

- **IAM instance profile:** `FirecrackerBenchRole` (so SSM registers them)
- AMI: Ubuntu 22.04 `ami-0370d56c6f7906c70` (or AL2023 `ami-0ec639a1a47d21afc`)
- a root EBS volume ≥ 100 GB gp3 (guest images + result space)
- tag `Project=firecracker-bench` (lets me find them; also handy if SendCommand
  ever gets tag-scoped)

Then just give me the four instance IDs — I drive the rest over SSM. In this case
the agent still needs the **SSM + S3** statements above (`DriveHostsViaSSM`,
`ResultsBucket`) but **not** `iam:PassRole` / `RunInstances`.

## Notes
- SSM `SendCommand` already works against managed+online instances (verified), so
  once a host carries `FirecrackerBenchRole` and boots, it's reachable in ~1-2 min.
- Bare-metal is mandatory: Firecracker needs `/dev/kvm`, which only `.metal`
  instances expose. Verified a virtualized box (`g4ad.4xlarge`) reports `NO_KVM`.
