# Infrastructure

One `terraform apply` produces a running, HTTPS-serving chatbot on a stable
public IP, with no domain name involved.

| File | Holds |
| --- | --- |
| `versions.tf` | Terraform and provider constraints; why state is local |
| `providers.tf` | Region and default tags. No credentials — those are ambient |
| `variables.tf` | Every input. Only `acme_email` is mandatory |
| `locals.tf` | Name prefix, tags, repo root, Compose download URL |
| `network.tf` | Dedicated VPC, public subnet, IGW, route table, AZ selection |
| `security.tf` | Security group and its rules. No port 22 anywhere |
| `iam.tf` | Instance role: SSM managed node access, and nothing else |
| `compute.tf` | Elastic IP, AL2023 instance, EIP association |
| `outputs.tf` | The URL, and the commands used after apply |
| `templates/user_data.sh.tftpl` | First-boot bootstrap and the deploy command it installs |

Three decisions explain most of the file:

- **The Elastic IP is allocated independently of the instance.** The certificate
  is issued for an IP address, so user_data has to contain the address before the
  instance exists. An EIP owned by the instance would make that a dependency
  cycle; a standalone one makes a single apply sufficient.
- **The instance clones the repository and builds the images itself.** No
  registry, no image-push step, no artifact bucket and no second build environment
  to keep in step with the Dockerfiles — and because the repository is public,
  no credential on the host either. The instance role can do exactly one thing: be
  an SSM managed node. The Vite build on a t3.micro is what the swapfile is for.
- **Terraform has no part in a release.** `user_data` names a repository and a
  ref, not a revision, so shipping code is `git push` followed by re-running the
  deploy script over SSM. Infrastructure changes can never half-restart a running
  site, and a release never touches infrastructure or needs an apply.

## Prerequisites

- Terraform ≥ 1.9 on `PATH`
- AWS CLI v2 with a live session. Set `aws_profile` in `terraform.tfvars` and
  refresh it before applying:

  ```powershell
  aws sso login --profile <name>
  aws sts get-caller-identity --profile <name>   # must return the intended account
  ```

  An expired IAM Identity Center session does not report itself as expired. The
  provider finds no usable credentials, falls through the chain to instance
  metadata, and fails with `No valid credential sources found` plus a timeout
  against `169.254.169.254` — which reads like a network fault and is not one.
- Permissions to create VPC, EC2, EIP, IAM role/policy/instance-profile, S3 and
  SSM resources.
- A real email address for Let's Encrypt.

- The application code **committed and pushed** to the ref named by `git_ref`
  (default `main`). The instance clones from GitHub, not from your disk, so
  uncommitted work does not deploy.

## First deploy

```powershell
cd infra
Copy-Item terraform.tfvars.example terraform.tfvars   # then edit acme_email
terraform init
terraform validate
terraform plan          # read it: 16 resources to add, none destroyed
terraform apply
```

Apply returns in a couple of minutes. The site is not up yet — the instance is
still installing Docker, building two images and requesting a certificate, which
takes roughly 5–10 minutes on a t3.micro. Watch it:

```powershell
terraform output -raw deploy_log_command    # copy and run the printed command
```

The log ends with `=== bootstrap finished` and a `docker compose ps` listing both
containers.

## Verify

Run these in **Git Bash**, not PowerShell: `curl` and `openssl` are both on its
`PATH`, and a JSON body survives its quoting unchanged.

```bash
IP=$(cd /d/aws-chatbot/infra && terraform output -raw public_ip)

# 1. A real certificate: 200, and no -k anywhere.
curl -sSI "https://$IP"

# 2. The right certificate. Issuer should be Let's Encrypt, the SAN should be this
#    IP, and notBefore..notAfter should span about six days — that last part is
#    the proof that the `shortlived` profile actually applied. No -servername:
#    there is no name to send, which is the whole point of `default_sni`.
openssl s_client -connect "$IP:443" </dev/null 2>/dev/null |
  openssl x509 -noout -issuer -subject -ext subjectAltName -dates

# 3. The application streams. -N matters: without it curl buffers and the
#    word-by-word delivery is invisible.
curl -N -sS -X POST "https://$IP/api/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello"}'
```

Then open the URL in a browser, confirm the padlock, and send a message.

**Do not skip the renewal check.** These certificates last about six days and
Caddy renews at roughly two thirds of that. Re-run step 2 in four to five days: if
`notAfter` has moved forward, renewal works and the deployment is durable. If it
has not, port 80 is the first thing to check — HTTP-01 needs it for every renewal,
not just the first issuance.

## Redeploy application code

```powershell
git push                                          # the release itself
terraform output -raw redeploy_command            # copy and run the printed command
```

No `terraform apply` — Terraform does not know or care what revision is deployed.
The instance fetches the current head of `git_ref`, resets its working tree to it
and rebuilds.

`user_data` stays byte-identical across releases, so Terraform never replaces the
instance: the Elastic IP, the certificate and Caddy's data volume all survive.

To watch a release land, or to recover from one that failed:

```
sudo tail -f /var/log/aws-chatbot-deploy.log
sudo /usr/local/bin/aws-chatbot-deploy          # idempotent; safe to re-run
```

## Shell access

```powershell
terraform output -raw ssm_session_command
```

There is no SSH, no port 22 and no key pair. Sessions land as `ssm-user`; use
`sudo` for Docker.

## Teardown

```powershell
terraform destroy
```

Removes everything including the VPC and the bucket. Worth doing when the
deployment is idle: an Elastic IP is free while attached to a running instance and
billed hourly once it is merely allocated, so a stopped-but-not-destroyed
deployment is the one way this costs money while doing nothing.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `InvalidClientTokenId` / `ExpiredToken` on apply | Credentials. `aws sts get-caller-identity` first. |
| Bootstrap log ends at `FATAL: public IPv4 is still …` | The EIP association never landed, so the deploy stopped *before* requesting a certificate — deliberately, to avoid spending rate limit on a guaranteed failure. Confirm the association in the console, then re-run `sudo /usr/local/bin/aws-chatbot-deploy`. |
| TLS handshake fails with no useful error | Almost always `default_sni` in the Caddyfile: a browser sends no SNI for an IP literal, so without it Caddy cannot pick a certificate. Check the Caddyfile in the artifact is the committed one. |
| Certificate never issues | `sudo docker compose logs caddy` in `/opt/aws-chatbot/deploy`. Check port 80 reachability from outside first. |
| Site works, then dies about a week later | A renewal failed. Port 80 closed after issuance is the usual cause. |
| Reply arrives all at once instead of word by word | Buffering between browser and API. `flush_interval -1` is already set on the reverse proxy; check nothing else (a corporate proxy, a CDN) sits in front. |
| `compose build requires buildx 0.17.0 or later` | The buildx plugin is missing or too old. `user_data` installs it; if it could not reach `api.github.com` to resolve the latest tag, pin `docker_buildx_version` (e.g. `v0.36.1`). |
| Deploy log ends at a git error | `fatal: could not read Username` means the repository is no longer public. `couldn't find remote ref` means `git_ref` names something that was never pushed. |
| A release ships nothing new | The commit was not pushed. `git -C /opt/aws-chatbot log -1` on the instance shows exactly what is deployed. |
| `npm run build` killed during a release | Swap missing or too small. `swapon --show` on the instance; raise `swap_size_gb`, or move to `t3.small`. |
| `Invalid force-replace address` / `Invalid target` from PowerShell | PowerShell mangles unquoted `-flag=value` arguments (`-replace` and `-target` are also its own operators). Quote them: `terraform apply "-replace=aws_instance.app"`. |
| Plan wants to replace the instance | Read it carefully before agreeing — a replacement loses the certificate and the image cache. An `ami` change should never appear (it is ignored); a `user_data` change means a stop/start, not a replacement. |

## Known limits

- **Local state.** `terraform.tfstate` in this directory is the only record of
  what exists. It is gitignored; back it up before anything destructive. Moving to
  an S3 backend is a `backend "s3"` block plus `terraform init -migrate-state`.
- **The repository must stay public.** Cloning uses no credentials, which is what
  removes the artifact bucket, the S3 grant and any secret on the host. Make the
  repository private and the next redeploy fails on authentication — recoverable,
  but the fix is a deploy token read from SSM Parameter Store, not a line in
  `user_data` where it would sit in plaintext in the instance's metadata.
- **Only pushed code deploys.** There is no path from your working tree to the
  instance. That is a feature for auditability and a nuisance while iterating; the
  local stack (`docker compose up` in `deploy/`) is where uncommitted work belongs.
- **The deploy is not pinned by default.** `git_ref = "main"` means a redeploy
  ships whatever was last pushed, including someone else's push. Set `git_ref` to a
  tag or a commit SHA when a deployment needs to be reproducible — the fetch
  accepts all three.
- **IPv4 only.** The security group takes IPv4 CIDRs, and the certificate covers
  the Elastic IP. IPv6 would need its own address, rules and SAN.
- **One instance, no redundancy.** A single AZ, a single host, no load balancer:
  a stop/start is a brief outage. That is the shape the plan chose — a load
  balancer cannot present a certificate for an address it does not own.
