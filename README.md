# AWS Chatbot

A streaming chat application — React front end, FastAPI back end, Server-Sent
Events between them — deployed to a single EC2 instance behind Caddy, reachable
over genuine HTTPS **on a bare IP address with no domain name involved**.

One `terraform apply` produces the running site. One `git push` plus one SSM
command ships a new version of it.

```
┌─────────┐   https://<elastic-ip>     ┌──────────────────────────────────┐
│ browser │ ─────────────────────────► │  EC2 (Amazon Linux 2023, Docker) │
└─────────┘   one origin, one port     │                                  │
                                       │  ┌────────────────────────────┐  │
                                       │  │ caddy  :80 :443            │  │
                                       │  │  • terminates TLS          │  │
                                       │  │  • serves the React bundle │  │
                                       │  │  • /api/* ─┐               │  │
                                       │  └────────────┼───────────────┘  │
                                       │               ▼                  │
                                       │  ┌────────────────────────────┐  │
                                       │  │ api  api:8000 (unpublished)│  │
                                       │  │  uvicorn + FastAPI, SSE    │  │
                                       │  └────────────────────────────┘  │
                                       └──────────────────────────────────┘
```

Caddy is the only way in. The API publishes no host port in any environment, so
there is no route to it that bypasses the edge.

## Contents

- [Tech stack](#tech-stack)
- [How the app works](#how-the-app-works)
- [Repository layout](#repository-layout)
- [Running locally](#running-locally)
- [How SSL works](#how-ssl-works)
- [Deployment](#deployment)

## Tech stack

| Layer | Choice | Why this one |
| --- | --- | --- |
| Front end | React 19 + TypeScript, built by Vite | Plain React with hooks; no router, no state library, no CSS framework. The whole UI is one screen, and a dependency that manages nothing is a dependency that only ages. |
| Styling | Hand-written CSS with design tokens (`src/styles/tokens.css`) | Two files of tokens and one stylesheet per component. Nothing to compile, nothing to purge. |
| Back end | FastAPI on uvicorn, Python 3.12 | Native async streaming responses, which is the one thing this API does; OpenAPI comes free at `/api/docs`. |
| Validation | Pydantic v2, at the HTTP boundary only | DTOs validate *shape*. Business rules live in the use case, so no rule has two homes. |
| Transport | Server-Sent Events over `POST /api/chat` | Text flowing one way, framed, resumable to read with plain `fetch`. WebSockets would add a bidirectional protocol for a unidirectional problem. |
| Python tooling | **uv** (`uv.lock` committed), ruff, mypy `strict` | Lockfile-exact installs in every environment, including inside the image (`uv sync --locked`). |
| Containers | Docker + Compose, multi-stage builds | Two images: `api` (Python) and `web` (Caddy with the compiled bundle baked in). |
| Edge | Caddy 2.11 | Automatic HTTPS with ACME built in — and, critically, it can obtain a certificate for an IP address. |
| Host | One EC2 `t3.micro`, Amazon Linux 2023, Elastic IP | Free-tier eligible. A load balancer cannot present a certificate for an address it does not own, so the certificate lives where the address lives. |
| Model | Google **Gemma 3 27B IT** on Amazon Bedrock, via the streaming Converse API | An open-weight instruction-tuned model with a 128K context, served on-demand with no endpoint to keep warm. Bedrock means one API and no vendor SDK of its own. |
| Infrastructure | Terraform (AWS provider), local state | 17 resources: dedicated VPC, public subnet, IGW, security group, IAM instance role, scoped Bedrock invoke policy, EIP, instance. |
| Access | SSM Session Manager | No SSH, no port 22, no key pair anywhere in the configuration. |

Replies come from **Gemma 3 27B on Amazon Bedrock**, streamed token by token to
the browser. The original canned generator is still there and still supported —
`CHAT_REPLY_SOURCE=canned` runs the whole stack with no AWS account, no
credentials and no network, which is how it demos offline. Neither the domain nor
the use case knows which of the two it is talking to: see
[the reply source](#the-reply-source).

## How the app works

### One request, end to end

1. You type a message and press send. `useChat` appends it to the transcript and
   calls the injected `ChatGateway`.
2. `SseChatGateway` posts `{message, history}` to `/api/chat` — a **relative**
   path, in every environment (see [one origin](#one-origin-everywhere)).
3. Caddy matches `/api/*` and reverse-proxies to `api:8000` with
   `flush_interval -1`, so nothing is buffered on the way back.
4. FastAPI validates the body's shape, and `ChatService` validates the prompt
   **synchronously, before streaming starts** — a blank or over-long prompt is a
   normal `422`, because once a streaming response has begun its status code can
   no longer be changed.
5. The reply generator yields the reply one word at a time. Each word is framed
   as SSE and flushed.
6. The gateway reassembles frames, yields chunks, and the hook appends them to
   the in-flight assistant message. The bubble grows as the words arrive.

### The wire format

Three frame types, defined once in
[sse.py](backend/app/interfaces/sse.py) and mirrored by
[sseChatGateway.ts](frontend/src/infrastructure/sseChatGateway.ts):

```
data: {"delta": "Hi "}    a fragment of the reply — append verbatim
data: {"error": "..."}    generation failed mid-stream
data: [DONE]              terminal sentinel, always last
```

Two details carry real weight:

- **`[DONE]` is always sent, including after an error.** A client that waits for
  the sentinel can therefore never hang.
- **Failures that happen *during* streaming are reported in-band**, as an
  `error` frame, because the `200` has already gone out. Failures *before*
  streaming are ordinary HTTP status codes. Those are two different mechanisms
  for two genuinely different situations.

`EventSource` is not used: it issues a `GET` and cannot carry a request body,
and this request carries the message and its history. So the stream is read by
hand — `fetch` → `body.getReader()` → `TextDecoder` — and frames are
reassembled at a blank-line boundary.

### One origin everywhere

The front end holds **no API base URL** in any environment. It posts to
`/api/chat`, and something same-origin routes that to the API:

- **production** — Caddy serves the bundle and proxies `/api/*`
- **local dev** — Vite's dev-server proxy, whose default target is the local
  Caddy container, so a dev request takes the same path through the edge that a
  real one does

The consequence is that CORS is switched off in production
(`CHAT_CORS_ALLOW_ORIGINS=` empty → the middleware is never installed) because
there is no cross-origin request to permit.

### Clean Architecture, and it is enforced

Both halves of the application are layered the same way, with dependencies
pointing strictly inward — and in both halves the dependency rule is a **build
gate**, not a convention:

| | Back end (`backend/app/`) | Front end (`frontend/src/`) |
| --- | --- | --- |
| **domain** | `entities.py`, `ports.py`, `errors.py` — pure Python, no framework | `message.ts`, `chatGateway.ts`, `errors.ts` — no imports at all |
| **application** | `chat_service.py` — the use case; no FastAPI, no Pydantic, no SSE | `useChat.ts` — the use case as a hook; React is the one allowed import |
| **infrastructure** | `bedrock_reply_generator.py`, `canned_reply_generator.py`, `config.py` | `sseChatGateway.ts` — the only file that knows the reply arrives over HTTP |
| **interfaces / presentation** | `routes.py`, `schemas.py`, `sse.py`, `dependencies.py` | `ChatWindow`, `ChatInput`, `MessageBubble`, … |
| **main** | `main.py` — assembles, then gets out of the way | `main.tsx` — the only file naming a concrete gateway |

- `backend/tests/test_domain_isolation.py` fails if `domain` or `application`
  imports a framework.
- `frontend/scripts/check-layers.mjs` fails if any file imports across a
  boundary it may not cross. It runs inside the Docker build, so a violation
  **fails the image**, not just a local check.

The inner layers depend on **ports** (a `Protocol` in Python, an `interface` in
TypeScript), never on implementations, and the concrete choice is made in one
place per side: `dependencies.py` and `main.tsx`.

### The reply source

`ChatService` asks for a `ReplyGenerator`; only
[dependencies.py](backend/app/interfaces/dependencies.py) knows what it gets.
There are two implementations of that one-method port, and swapping between them
is a config value rather than a code change:

| `CHAT_REPLY_SOURCE` | Implementation | Needs |
| --- | --- | --- |
| `bedrock` *(default)* | [bedrock_reply_generator.py](backend/app/infrastructure/bedrock_reply_generator.py) — Gemma 3 27B over Converse streaming | AWS credentials |
| `canned` | [canned_reply_generator.py](backend/app/infrastructure/canned_reply_generator.py) — one fixed sentence, word by word | nothing at all |

Adding the model touched no file in `domain`, `application`, `routes`, the SSE
framing, or the entire front end. The wire format did not change, because the
reply was always streamed; the model simply became the thing producing the
fragments. Conversation history had been carried on the request and translated
into domain entities since the first commit, waiting for a consumer — the
Bedrock adapter is the first thing to read it.

Three things the adapter does that are worth knowing:

- **It runs boto3 off the event loop.** botocore's event stream is synchronous:
  `for event in response["stream"]` waits on a socket. Iterating that directly
  inside an `async` generator would stall the whole loop between tokens, so a
  second chat could not begin until the first finished. Each step goes through
  `anyio.to_thread` instead.
- **It repairs history rather than trusting it.** History arrives from the
  browser, and Converse rejects turns that do not alternate starting from the
  user. Rather than let a malformed history become a `ValidationException` — a
  chat that appears to be broken — the adapter drops the offending turns and
  sends slightly less context.
- **It sets `maxTokens` explicitly.** Left unset, Bedrock reserves the model's
  own maximum against the account's per-minute token quota on every call, and
  throttling arrives long before traffic would explain it.

### Credentials

The application contains no credential handling at all, and
[config.py](backend/app/infrastructure/config.py) reads no AWS variable. boto3's
default chain resolves them, so the same image runs in both places unmodified:

| Where | How | Rotation |
| --- | --- | --- |
| Local | An SSO profile mounted read-only into the api container by `docker-compose.override.yml` | `aws sso login` on the host |
| EC2 | The instance role, read from IMDS | Automatic; nothing stored |

There is **no API key anywhere**, by choice. A short-term Bedrock API key expires
with the console session (12 hours at most), and a long-term one is a static
IAM-user credential in a file on a public-facing host — in a deployment that
otherwise has no SSH key, no key pair and no stored secret of any kind. The
instance role gives short-lived credentials, scoped in
[infra/iam.tf](infra/iam.tf) to `InvokeModel` on one model ARN and nothing else,
with every call attributable in CloudTrail.

Nothing secret is ever written to `deploy/.env`. On the instance that file is
regenerated from a heredoc on every release, so hand edits there do not survive
the next deploy.

### Configuration

Every variable is optional (see
[config.py](backend/app/infrastructure/config.py)):

| Variable | Default | Notes |
| --- | --- | --- |
| `CHAT_CORS_ALLOW_ORIGINS` | the two Vite dev origins | Explicitly **empty** in production; empty means no CORS middleware at all |
| `CHAT_MAX_PROMPT_CHARS` | `4000` | Over-long prompts are rejected with `422` before streaming |
| `CHAT_REPLY_SOURCE` | `bedrock` | `canned` for a stack that needs no AWS |
| `CHAT_BEDROCK_MODEL_ID` | `google.gemma-3-27b-it` | Also scopes the IAM policy, via a Terraform variable |
| `CHAT_BEDROCK_REGION` | `us-east-1` | This model has **no** cross-region inference profile, so the region must be one that offers it |
| `CHAT_BEDROCK_MAX_OUTPUT_TOKENS` | `1024` | Model ceiling is 8192 |
| `CHAT_BEDROCK_TEMPERATURE` | `0.7` | |
| `CHAT_MAX_HISTORY_MESSAGES` | `20` | Turns resent for context. Every turn sent is re-billed as input, so this is a cost dial as much as a memory one |
| `CHAT_SYSTEM_PROMPT` | a short persona | Explicitly empty sends no system prompt at all |
| `CHAT_WORD_DELAY_SECONDS` | `0.06` | Canned generator only; Bedrock's pacing comes from the model |

## Repository layout

| Path | Holds |
| --- | --- |
| [backend/](backend/) | The API: `app/` in four layers, `tests/`, `pyproject.toml`, `uv.lock` |
| [frontend/](frontend/) | The UI: `src/` in four layers, `scripts/check-layers.mjs`, Vite config |
| [deploy/](deploy/) | `docker-compose.yml`, both Dockerfiles, `caddy/Caddyfile` — everything about running it |
| [infra/](infra/) | Terraform for the AWS environment. See [infra/README.md](infra/README.md) |

## Running locally

Requires Docker with Compose. Node and Python are needed only for the dev
server and the editor's benefit — the images build their own toolchains.

```powershell
cd deploy
docker compose up --build -d
```

`docker-compose.yml` carries local defaults for every value, so no `.env` is
required. Copy `.env.example` to `.env` only when you want to override one —
Compose auto-loads it from that directory, no `--env-file` flag needed.

Replies come from Bedrock by default, so the api container needs credentials.
Mount your own SSO profile into it:

```powershell
cd deploy
cp docker-compose.override.yml.example docker-compose.override.yml
# edit AWS_PROFILE in it to match `aws configure list-profiles`
aws sso login --profile <your-profile>
docker compose up --build -d
```

Compose loads `docker-compose.override.yml` automatically. It stays **untracked**
on purpose: the instance deploys by `git reset --hard`, so a committed override
would land there too and shadow the EC2 instance role with a profile that does
not exist on the host. Confirm it worked with:

```powershell
docker compose exec api python -c "import botocore.session as s; print(s.get_session().get_credentials().method)"
```

`sso` locally, `iam-role` on the instance. To skip all of this and run with no
AWS account at all, set `CHAT_REPLY_SOURCE=canned` in `.env` — the stack then
needs no credentials, no override file and no network.

Open <http://localhost>. `SITE_ADDRESS` defaults to `http://localhost`, and an
explicit scheme turns Caddy's automatic HTTPS **off completely** — a local stack
cannot contact an ACME server even by accident.

For a hot-reloading UI against that same stack:

```powershell
cd frontend
npm install
npm run dev                      # http://localhost:5173, /api proxied to the container
```

Useful commands:

```powershell
docker compose run --rm api-tests     # pytest, inside the image
docker compose logs -f caddy          # the edge log
docker compose down                   # stop and remove

cd backend; uv run pytest             # or run the suite on the host
cd backend; uv run ruff check .; uv run mypy .
cd frontend; npm run check            # layer rule + typecheck
```

## How SSL works

The requirement was trusted HTTPS with **no domain name** — the padlock has to
be genuine on `https://<elastic-ip>`, with no `-k` and no click-through
warning. That is unusual enough to be worth spelling out, because almost every
piece of standard advice for it is wrong.

### Certificates for an IP address

Let's Encrypt issues certificates for IP addresses, with two constraints that
shape this entire deployment:

1. **Only under the `shortlived` profile** — roughly a **six-day** lifetime,
   renewed automatically at about two thirds of it. There is no long-lived
   option.
2. **Only over the HTTP-01 challenge.** TLS-ALPN-01 cannot validate an IP
   identifier, and DNS-01 is meaningless without a name.

Caddy handles issuance and renewal itself. The whole of the configuration is
three deliberate lines in [deploy/caddy/Caddyfile](deploy/caddy/Caddyfile):

```caddyfile
{
    email {$ACME_EMAIL}
    # A browser connecting to an IP sends no SNI — SNI carries host *names*.
    # With no default there is nothing to select a certificate by, so the
    # handshake fails before any request is made.
    default_sni {$SITE_ADDRESS}
}

{$SITE_ADDRESS} {
    tls {
        issuer acme {
            profile shortlived          # the only profile that covers an IP
            disable_tlsalpn_challenge   # Caddy tries TLS-ALPN first and fails
        }
    }
    ...
}
```

- **`default_sni` is the difference between working and not.** A browser
  visiting an IP literal sends no SNI at all, because an address is not a name.
  Without a default, Caddy has nothing to pick a certificate by and the
  handshake dies before a single request — every visitor sees a TLS error rather
  than a slow site.
- **`disable_tlsalpn_challenge` forces HTTP-01.** Caddy attempts TLS-ALPN first
  by default and fails on an IP identifier
  ([caddyserver/caddy#7399](https://github.com/caddyserver/caddy/issues/7399)).
- **`profile shortlived`** is what makes issuance succeed at all for an IP.
  Verifying that it applied is simply checking that `notBefore..notAfter` spans
  about six days.

### Port 80 is load-bearing, permanently

Because HTTP-01 is the only usable challenge, **port 80 carries every renewal
for the life of the deployment** — not just the first issuance. It is open to
`0.0.0.0/0` in [infra/security.tf](infra/security.tf) and cannot be narrowed
even for a private deployment, since Let's Encrypt validates from its own
unpublished source addresses.

Closing port 80 after the site comes up looks completely harmless and takes the
site down roughly four days later. That is the single most likely way this
deployment breaks.

### Why the Elastic IP is allocated separately

The certificate is issued for an address, so the software requesting it must
know that address **before it starts** — which means the address goes into
`user_data`. An EIP owned by or read back from the instance would make
`user_data` depend on the instance and the instance depend on its `user_data`:
a dependency cycle. Allocating the address as a standalone resource breaks it,
so a single `terraform apply` produces a host that already knows which address
it will answer on.

Two safeguards follow from the same concern:

- **The deploy script waits for the association.** The instance boots with an
  auto-assigned public address; Terraform associates the EIP moments later.
  Starting Caddy inside that window means asking for a certificate naming an
  address that does not yet reach the host — validation fails, and the failure
  is charged against the account's rate limit. With a six-day certificate there
  is no spare quota for repeating that, so `user_data` polls IMDS until the
  expected address appears, and **refuses to start Caddy** if it never does.
- **Caddy's `/data` is a persisted volume.** It holds the certificate and the
  ACME account key. Without persistence every restart would re-issue from
  scratch, which with a six-day certificate and Let's Encrypt's rate limits is
  a way to lock yourself out of your own site.

### Verifying it

Run in Git Bash (both `curl` and `openssl` are on its `PATH`):

```bash
IP=$(cd infra && terraform output -raw public_ip)

# A real certificate: 200, no -k anywhere.
curl -sSI "https://$IP"

# The right certificate: issuer Let's Encrypt, SAN is this IP, and a ~6-day
# validity window — that last part proves the `shortlived` profile applied.
openssl s_client -connect "$IP:443" </dev/null 2>/dev/null |
  openssl x509 -noout -issuer -subject -ext subjectAltName -dates
```

**Then check again in four to five days.** If `notAfter` has moved forward,
renewal works and the deployment is durable. If it has not, port 80 is the
first thing to look at.

### Known limits

- **IPv4 only.** The security group takes IPv4 CIDRs and the certificate covers
  the Elastic IP; IPv6 would need its own address, rules and SAN.
- **No load balancer.** An ALB cannot present a certificate for an address it
  does not own, so TLS terminates on the instance. One host, one AZ: a
  stop/start is a brief outage.

## Deployment

Two things are deliberately separate here: **Terraform owns the environment,
and it has no part in a release.** `user_data` names a repository and a *ref*,
not a revision — so shipping code is a `git push` followed by re-running a
script on the instance, and an infrastructure change can never half-restart a
running site.

The instance clones the repository and builds both images itself. That removes
a registry, an image-push step, an artifact bucket and a second build
environment to keep in step with the Dockerfiles — and because the repository
is public, it removes every credential from the host too. The instance role can
do exactly one thing: be an SSM managed node.

Full detail, including troubleshooting, is in
[infra/README.md](infra/README.md).

### Prerequisites

- Terraform ≥ 1.9 and AWS CLI v2 on `PATH`, with a live session
  (`aws sso login --profile <name>`; verify with `aws sts get-caller-identity`)
- A **real** email address for Let's Encrypt — it is the CA's only channel to
  warn about failing renewals, which matters more than usual with a six-day
  certificate. Terraform rejects `example.com` and `.invalid` addresses.
- The code **committed and pushed** to the ref named by `git_ref` (default
  `main`). The instance clones from GitHub, not from your disk.

### First deploy

```powershell
cd infra
Copy-Item terraform.tfvars.example terraform.tfvars   # then set acme_email
terraform init
terraform validate
terraform plan          # read it: 16 to add, none destroyed
terraform apply
```

Apply returns in a couple of minutes, but the site is not up yet — the instance
is still installing Docker, building two images and requesting a certificate,
which takes roughly 5–10 minutes on a `t3.micro`. Watch it:

```powershell
terraform output -raw deploy_log_command    # copy and run the printed command
```

The log ends with `=== bootstrap finished` and a `docker compose ps` listing
both containers. Then verify TLS as [above](#verifying-it).

What first boot does, in order
([user_data.sh.tftpl](infra/templates/user_data.sh.tftpl)):

1. creates a 2 GiB swapfile — `npm run build` peaks above what a `t3.micro`'s
   1 GiB leaves free, and the OOM killer takes node mid-build
2. installs Docker, git, and the Compose and **buildx** plugins (Amazon Linux
   2023 ships the engine without either; `compose up --build` refuses to run
   with buildx older than 0.17.0)
3. writes `/usr/local/bin/aws-chatbot-deploy` — and then runs it

That split matters: the thing that starts the application is an **idempotent
command on the box**, so recovering from a failed bootstrap and shipping the
tenth release are the same one command, not a re-created instance.

### Releasing a change

```powershell
git push                                          # the release itself
terraform output -raw redeploy_command            # copy and run the printed command
```

No `terraform apply`. The instance fetches the current head of `git_ref`,
resets its working tree to it, rebuilds both images and restarts the stack, then
prunes the images the previous build left dangling. `user_data` stays
byte-identical across releases, so Terraform never replaces the instance: the
Elastic IP, the certificate and Caddy's data volume all survive.

To watch a release land, or recover from one that failed:

```bash
sudo tail -f /var/log/aws-chatbot-deploy.log
sudo /usr/local/bin/aws-chatbot-deploy          # idempotent; safe to re-run
```

### Build gates

A release cannot ship broken layering or a type error, because both image
builds fail first:

- `api.Dockerfile` — `uv sync --locked` fails on a stale `uv.lock`; a `test`
  stage runs the suite against the image itself
- `web.Dockerfile` — `npm ci` fails on a stale lockfile, then
  `npm run check:layers && npm run build` runs the dependency-rule check and
  `tsc -b` before Vite; finally `caddy validate` is run against **both**
  `SITE_ADDRESS` shapes (local `http://localhost` and a production IP literal),
  so a Caddyfile change cannot silently break the production path that can't be
  exercised locally

### Shell access

```powershell
terraform output -raw ssm_session_command
```

No SSH, no port 22, no key pair. Sessions land as `ssm-user`; use `sudo` for
Docker.

### Teardown

```powershell
cd infra
terraform destroy
```

Worth doing when the deployment is idle: an Elastic IP is free while attached
to a *running* instance and billed hourly once merely allocated, so a
stopped-but-not-destroyed deployment is the one way this costs money while doing
nothing.

### Operational notes

- **Terraform state is local.** `infra/terraform.tfstate` is the only record of
  what exists, and it is gitignored. Back it up before anything destructive;
  moving to S3 is a `backend "s3"` block plus `terraform init -migrate-state`.
- **The repository must stay public.** Cloning uses no credentials, which is
  what removes the artifact bucket and every secret from the host. Making it
  private breaks the next redeploy; the fix is a deploy token read from SSM
  Parameter Store, not a line in `user_data`.
- **Only pushed code deploys.** There is no path from your working tree to the
  instance. Uncommitted work belongs in the local Compose stack.
- **`git_ref = "main"` is not pinned.** A redeploy ships whatever was last
  pushed, including someone else's push. Set `git_ref` to a tag or a commit SHA
  when a deployment needs to be reproducible — the fetch accepts all three.
