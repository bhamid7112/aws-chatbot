# Infrastructure

Two deployment targets for one application, as two independent Terraform root
stacks. Either can be applied, destroyed, or left alone without affecting the
other — they keep separate state and create disjoint resource types, so both can
coexist in one account.

This directory is an index. Neither stack lives here.

| Path | Target | Read |
| --- | --- | --- |
| [server/](server/) | One EC2 instance behind Caddy, HTTPS on a bare Elastic IP | [server/README.md](server/README.md) |
| [serverless/](serverless/) | CloudFront + S3 + Lambda, SSE end to end | [serverless/README.md](serverless/README.md) |
| [modules/bedrock_access/](modules/bedrock_access/) | The one thing genuinely shared: permission to invoke exactly one model | — |

## Choosing a target

Both run the identical application. The differences are operational.

| | `server/` | `serverless/` |
| --- | --- | --- |
| Cost when idle | ≈$12–13/month, flat | Pennies of storage |
| Cost at ~1,000 chats/month | the same ≈$12–13 | under $2 |
| Break-even | — | ≈25,000–30,000 chats/month |
| Latency, warm | consistent | consistent (≈10 ms health) |
| Latency, cold | none — it is always running | ≈6.4 s init, and it is billed |
| TLS | Let's Encrypt for an IP, renewed every ~6 days | AWS-managed, nothing to renew |
| Hostname | a bare IP address | `*.cloudfront.net` |
| Release | `git push` + one SSM command | `release-api.sh` / `release-web.sh` |
| Build runs on | the instance | your workstation (Docker + buildx) |
| Prerequisites | Terraform, AWS CLI | Terraform, AWS CLI, git, **Docker with buildx** |
| Debugging | SSM session, `docker compose logs` | `aws logs tail` across two log groups — no host, no shell |
| What is deployed | `git log -1` on the box | an image digest, and **both functions must match** |
| Resources | 17 | 43 (42 without `alert_email`) |
| Apply time | ~2 min (+5–10 min first boot) | 3–8 min |
| Destroy time | ~2 min | 15+ min (CloudFront) |
| Transports | streamed only | streamed **and** polled; polled is the default |
| A reply survives a dropped connection | no | yes, on the polled transport |
| Stopping a reply stops the billed work | no | yes, on the polled transport |
| Conversation content at rest | none | replies in DynamoDB, 1 h TTL |
| Idle risk | an unattached Elastic IP bills hourly | an abandoned **streamed** reply bills to timeout; a polled one is cancelled |

Rules of thumb:

- **Low or bursty traffic, and nobody minding a slow first request** — take
  `serverless/`. It also removes the most fragile thing in the whole repository:
  a trusted certificate for a bare IP, with a six-day renewal cliff.
- **Steady traffic, or a hard latency floor** — take `server/`. No cold start,
  and it is cheaper past roughly 25,000 chats a month.
- **Needing a real hostname** — neither is ready as-is. `server/` deliberately
  has no domain; `serverless/` needs ACM in us-east-1 and a provider alias.

## Shared variables

Five settings belong to both stacks: `aws_region`, `aws_profile`, `project`,
`bedrock_model_id`, `bedrock_region`.

Terraform has no mechanism for sharing a tfvars file across root modules, so the
**invocation** carries it. Copy `shared.tfvars.example` to `shared.tfvars`
(gitignored) and pass it to either stack:

```powershell
terraform -chdir=infra/server     apply -var-file=../shared.tfvars
terraform -chdir=infra/serverless apply -var-file=../shared.tfvars
```

Forgetting `-var-file` is not an error — Terraform falls back to the defaults in
`variables.tf`, which may name a different region or account than intended. Each
stack additionally has its own `terraform.tfvars` for the settings only it has.

## Both at once

Nothing prevents it, and it is worth doing once to confirm the claim that one
codebase serves both. Every resource is prefixed with `var.project`, the
serverless stack creates no VPC to collide with, and the two states are separate.
The `Target` tag distinguishes them in Cost Explorer.

## Local state

Both stacks keep state locally, for the reason `versions.tf` gives in each: one
operator, one environment, and a remote backend would need its own bucket and
lock table created *before* either configuration can run — the bootstrapping
problem a small deployment does not need.

`terraform.tfstate` is gitignored in both, and in both it is the only record of
what exists. Back it up before anything destructive. Moving to S3 later is a
`backend "s3"` block plus `terraform init -migrate-state`, per stack.
