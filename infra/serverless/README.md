# Serverless target

The same application as [../server](../server), with no server. CloudFront serves
the React bundle from S3 and forwards `/api/*` to a Lambda function that runs the
**unmodified** `uvicorn app.main:app` process.

Nothing in `backend/` knows it is on Lambda: no handler, no Mangum, no ASGI
shim, no extra dependency. That is the property the whole design is built to
keep, and it is what makes "one codebase, two deployment targets" true rather
than aspirational.

**There are two ways a reply gets to the browser here**, and only one of them
exists on the server target:

- **Streamed** — `POST /api/chat` holds one response open and writes Server-Sent
  Events into it as the model produces them. Simple, and the whole reply lives in
  a socket: drop the connection and it is gone.
- **Polled** — `POST /api/chat/jobs` hands the work to a second Lambda and
  returns an id; the browser reads the reply back in pieces as it is written to
  DynamoDB. Survives a dropped connection, is not bounded by a CDN's origin read
  timeout, and can be **cancelled**, which stops work that is being billed for.

Polled is the default where it is available. The streamed path is retained, not
retired: `?transport=sse` selects it, and the server target — which has no job
store — advertises only that one and is served by the same bundle unchanged. See
[Asynchronous replies](#asynchronous-replies).

```
┌─────────┐  https://<id>.cloudfront.net   ┌──────────────────────────────┐
│ browser │ ─────────────────────────────► │ CloudFront                   │
└─────────┘  one origin, one certificate   │                              │
                                           │  default ──► S3 (private)    │
                                           │    OAC        react bundle   │
                                           │                              │
                                           │  /api/*  ──► Function URL    │
                                           │    OAC, SigV4  RESPONSE_STREAM│
                                           └──────────────┬───────────────┘
                                                          ▼
                                           ┌──────────────────────────────┐
                                           │ api Lambda (container image)  │
                                           │  lambda-adapter (extension)   │
                                           │  └─► uvicorn + FastAPI        │
                                           └────┬────────────────────┬─────┘
                        streamed transport      │                    │  polled transport
                                                ▼                    ▼
                                    Bedrock (Converse stream)   worker Lambda
                                                                (same image)
```

Neither origin is publicly reachable. S3 blocks all public access and the
Function URL requires `AWS_IAM`, so CloudFront — signing with Origin Access
Control — is the only caller either one accepts.

| File | Holds |
| --- | --- |
| `versions.tf` | Terraform and provider constraints; why state is local and per-stack |
| `providers.tf` | Region and default tags. No credentials — those are ambient |
| `variables.tf` | Every input. All have defaults; an empty `terraform.tfvars` is valid |
| `locals.tf` | Name prefix, tags, both log group names, and each function's environment |
| `ecr.tf` | Image registry: immutable tags, lifecycle policy, explicit Lambda pull grant |
| `logs.tf` | A log group per function, created *before* it so retention is never absent |
| `alerts.tf` | Error alerting: metric filters, alarms per function, and the SNS topic they mail |
| `iam.tf` | Two execution roles: own log group, one Bedrock model, and the job table |
| `jobs.tf` | The job table and the worker's failure queue — the polled transport's state |
| `lambda.tf` | Both functions. `ignore_changes = [image_uri]` keeps Terraform out of releases |
| `url.tf` | Function URL (`RESPONSE_STREAM`) and the two permissions CloudFront needs |
| `site.tf` | Private bucket for the bundle, and the policy that makes a 404 a 404 |
| `cdn.tf` | Distribution, both OACs, the SPA function, and the two cache behaviours |
| `functions/spa-router.js` | Client-side routing at the edge |
| `outputs.tf` | The URL, and every command used after apply |

43 resources — 42 if `alert_email` is left empty. Compare 17 for the server
target. It was 25 before the polled transport, and that near-doubling is the
honest price of it: a second function, a table, a queue, three more alarms, a
second role, and every per-function resource now created twice through a
`for_each`. Nothing here is a host; one thing here — the job table — now holds
state, with a one-hour TTL and nothing that cannot be lost.

## Five decisions explain most of the configuration

- **A Function URL, not API Gateway.** The reply is streamed, and that rules
  everything else out: a REST API buffers the whole response body, and an HTTP
  API has a hard 30-second integration limit with no response streaming at all.
  A Function URL with `InvokeMode = RESPONSE_STREAM` is the only serverless HTTP
  front door that can carry SSE. This is a constraint, not a preference.
- **The Lambda Web Adapter, not a handler.** The adapter is a Lambda *extension*
  that ships the runtime interface client, so a plain `python:3.12-slim` image
  works with no AWS base image and no `awslambdaric`. It starts the image's own
  `CMD`, waits for `/api/health`, then turns each invocation into an HTTP request
  against uvicorn. This is the entire reason the backend needs no changes.
- **CloudFront in front, not the Function URL alone.** The frontend posts to the
  relative path `/api/chat` and holds no API base URL in any environment. One
  origin serving both the bundle and the API preserves that, keeps CORS switched
  off, and supplies a trusted certificate — replacing the hardest part of the
  server target, a certificate for a bare IP, with one line.
- **Terraform has no part in a release.** As on the server target, but by a
  different mechanism: `lifecycle { ignore_changes = [image_uri] }` means
  `image_tag` is read when the function is created and never again. Releases go
  through `scripts/release-*.sh`, and an infrastructure change can never ship
  code.
- **Lambda's own asynchronous invocation, not SQS, carries a job to the worker.**
  `InvocationType = "Event"` already provides a durable queue, retry on throttles
  and 5xx bounded by `maximum_event_age_in_seconds`, retry on function errors
  tunable 0–2, and an on-failure destination. Putting SQS on the request path
  would add a queue, an event source mapping, a redrive policy, a message
  envelope and a `visibility_timeout ≥ 6 × function timeout` coupling to buy
  things already present. The one genuine loss is that Lambda's queue is
  **opaque** — you cannot read it and cannot replay it — and that is recovered by
  putting a plain SQS queue back as the *failure* destination, where it is passive
  and inspectable. One queue, no coupling, replay restored.

## Prerequisites

- **Terraform ≥ 1.9**, **AWS CLI v2**, **git**, and **Docker with buildx** on
  `PATH`. Docker is new relative to the server target: the build moves from the
  instance to your workstation, because the image has to reach a registry.
- A live AWS session. Set `aws_profile` in `../shared.tfvars` and refresh it:

  ```powershell
  aws sso login --profile <name>
  aws sts get-caller-identity --profile <name>   # must return the intended account
  ```

  An expired IAM Identity Center session does not report itself as expired. The
  provider finds no usable credentials, falls through to instance metadata, and
  fails with `No valid credential sources found` plus a timeout against
  `169.254.169.254` — which reads like a network fault and is not one.
- Permissions for ECR, Lambda, IAM role/policy, S3, CloudFront, CloudWatch Logs,
  CloudWatch alarms, SNS, **DynamoDB and SQS**. Wider than the server target's,
  because a release now touches four services rather than sending one SSM
  command. The last two are for the polled transport's table and its failure
  queue.
- Bedrock access to the model in `bedrock_region`:

  ```powershell
  aws bedrock get-foundation-model --model-identifier google.gemma-3-27b-it --region us-east-2 --profile <name>
  ```

  `modelLifecycle.status` must be `ACTIVE` and `responseStreamingSupported` must
  be `true`. This model has **no** cross-region inference profile, so
  `bedrock_region` must be a region that actually offers it.
- No domain and no certificate. All of that is gone — CloudFront supplies a
  trusted one, replacing the hardest part of the server target with one line.
  An email address is still worth having, but for a different reason than there:
  not to register with a certificate authority, only to receive error alerts, and
  the deployment works without one. See [Error alerts](#error-alerts).

### Shell note

Commands are marked `powershell` or `bash`. It matters in both directions:

- **PowerShell** does not treat `\` as a line continuation, so a multi-line
  POSIX-style command becomes a pile of positional arguments. Every command
  below is a single line for that reason.
- **The release scripts are POSIX `sh`** and must run under Git Bash. Prefixing
  with `bash` works from PowerShell too.

## Variables

Shared with the server target, in **`../shared.tfvars`** (gitignored; copy
`../shared.tfvars.example`). Terraform has no cross-directory tfvars mechanism,
so the *invocation* carries the file — every command below passes
`-var-file=../shared.tfvars`, and omitting it silently falls back to defaults
that may name the wrong account.

| Variable | Default | Notes |
| --- | --- | --- |
| `aws_region` | `us-east-2` | ECR, the function and the log group. ECR must be in the function's region |
| `aws_profile` | `""` | Empty means the ambient credential chain |
| `project` | `aws-chatbot` | Name prefix. Safe to share: the two stacks create disjoint resource types |
| `bedrock_model_id` | `google.gemma-3-27b-it` | Also scopes the execution role, so the two cannot disagree |
| `bedrock_region` | `us-east-2` | Independent of `aws_region`; co-located here to avoid a hop |

This target only, in `terraform.tfvars` (optional — every value has a working
default):

| Variable | Default | Notes |
| --- | --- | --- |
| `reply_source` | `bedrock` | `canned` streams a fixed reply with no AWS call. See [Verifying streaming](#verifying-streaming) |
| `word_delay_seconds` | `0.06` | Canned generator only — and the known cadence the streaming check measures against |
| `image_tag` | `bootstrap` | Read **only** at function create. Releases update by digest afterwards |
| `lambda_architecture` | `x86_64` | Must match what the release script builds; one variable drives both |
| `memory_size_mb` | `1536` | Buys CPU, which is what a cold start needs. Init is CPU-bound |
| `timeout_seconds` | `120` | Must exceed botocore's 60 s read timeout plus init. Also a cost ceiling |
| `log_retention_days` | `14` | Set explicitly: the CloudWatch default is "never expire" |
| `price_class` | `PriceClass_100` | Cheapest class still covers North America and Europe |
| `bedrock_read_timeout_seconds` | `60` | botocore's read timeout. **Nothing can interrupt a blocked read**, so this is the real bound on a stalled reply — see [Asynchronous replies](#asynchronous-replies) |
| `worker_memory_size_mb` | `1536` | Same reasoning as `memory_size_mb`: it buys CPU for init |
| `worker_timeout_seconds` | `300` | Validated `> bedrock_read_timeout_seconds`, so Lambda's timeout is always the outer bound rather than a race |
| `worker_event_age_seconds` | `300` | How long Lambda keeps retrying delivery. Long enough to ride out a throttle burst, short enough that a dropped job is reported while somebody still cares |
| `job_retention_seconds` | `3600` | TTL on the job item. DynamoDB deletes expired items only *typically within 48 hours*, so the honest figure at rest is longer — see [Data at rest](#data-at-rest) |
| `worker_failure_retention_seconds` | `345600` | How long a failure record stays on the queue. It contains the original prompt, so shorter is better than longer |
| `alert_email` | `""` | Where error alarms mail. This stack only — **not** `../shared.tfvars`, which `../server` would warn about. Empty still creates the topic. See [Error alerts](#error-alerts) |

## First deploy

**Bootstrap is two applies**, and the reason is unavoidable: a `package_type =
"Image"` function cannot be created before its image exists, and the image cannot
be pushed before the registry exists.

```powershell
terraform -chdir=infra/serverless init
Copy-Item infra/shared.tfvars.example infra/shared.tfvars   # then set aws_profile
terraform -chdir=infra/serverless validate
```

**1. Create the registry only.**

```powershell
terraform -chdir=infra/serverless apply -var-file=../shared.tfvars -target=aws_ecr_repository.api -target=aws_ecr_lifecycle_policy.api -target=aws_ecr_repository_policy.api
```

**2. Build and push the first image**, without trying to update a function that
does not exist yet. Prints the tag — a 12-character commit SHA — which the next
step needs.

```bash
bash ./scripts/release-api.sh --skip-update
```

**3. Create everything else**, naming that tag.

```powershell
terraform -chdir=infra/serverless apply -var-file=../shared.tfvars -var="image_tag=<TAG from step 2>"
```

Expect 3–8 minutes: CloudFront distributions are slow to create. Everything else
is quick. Both functions are created here, from that one tag, and from then on
`image_uri` is ignored on both.

**Do not pass `-var` values you do not intend to keep.** Anything set on the
command line is recorded in state but not in any file, so the next apply *without
it* silently reverts to the variable's default — which shows up as an unexplained
diff, and for `alert_email` as a destroyed-and-recreated subscription that then
needs confirming again. Put the value in `terraform.tfvars` instead.

**4. Upload the bundle.** Until this runs, `/` returns S3's 404 — the
distribution exists but the bucket is empty.

```bash
bash ./scripts/release-web.sh
```

```powershell
terraform -chdir=infra/serverless output -raw site_url
```

## Verify

Run in **Git Bash** — `curl` is on its `PATH` and a JSON body survives its
quoting unchanged.

```bash
DIST=$(terraform -chdir=infra/serverless output -raw site_url)

# 1. The bundle. 200, text/html, Cache-Control: no-cache on the shell.
curl -sSI "$DIST/"

# 2. The API, through CloudFront's OAC. {"status":"ok","transports":["sse","jobs"]}
#    `transports` is what the browser reads to choose a transport, so a missing
#    "jobs" here is the answer to "why is it still using SSE".
curl -sS "$DIST/api/health"

# 3. The Function URL is NOT reachable directly. Must be 403.
curl -sS -o /dev/null -w '%{http_code}\n' "$(terraform -chdir=infra/serverless output -raw function_url)api/health"

# 4. Client-side routing: a deep path returns the app, not a 404.
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' "$DIST/some/deep/path"

# 5. A missing asset MUST be 404 — never 200 with index.html. See site.tf.
curl -sS -o /dev/null -w '%{http_code}\n' "$DIST/assets/nope.js"
```

Then open the URL in a browser and send a message. Words should appear
progressively, and **Stop** should halt the reply.

### Verifying streaming

This is the check that matters, and the one that is easy to do wrong: a buffered
response and a streamed one contain **identical bytes**. Only the *timing*
differs, so confirming the body arrived proves nothing.

```bash
BODY='{"message":"Tell me about AWS in three sentences.","history":[]}'
curl -N -sS -X POST "$DIST/api/chat" -H 'content-type: application/json' -H "x-amz-content-sha256: $(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)" -d "$BODY"
```

`-N` is not optional — without it curl buffers and the word-by-word delivery is
invisible regardless of what the server did.

For a conclusive measurement, set `reply_source = "canned"` in
`terraform.tfvars` and apply. The canned generator emits one word every
`word_delay_seconds` with no AWS call, so the expected cadence is known in
advance: frames arriving ~60 ms apart prove nothing is buffering, and frames
arriving in a single burst prove something is. A real model cannot support that
conclusion, because its own pacing is unknown and it emits large blocks rather
than words.

This was measured at **60.6 ms median** against a configured 0.06 s when the
stack was first built, which is what established that CloudFront forwards a
chunked response unbuffered.

## Asynchronous replies

The polled transport. The api function records a job, hands it to the worker and
returns immediately; the worker generates the reply and writes it out in pieces;
the browser reads those pieces back by cursor.

```
POST /api/chat/jobs ──► api Lambda ──► PutItem (pending, segments=[])
                             │
                             └──► Invoke(InvocationType=Event) ──► worker Lambda
                                     retries 2, event age 300 s        │
                                                                       │ POST /events
                                                                       ▼  (LWA pass-through)
GET /api/chat/jobs/{id}/segments/{n} ◄── api Lambda ◄── DynamoDB ◄── UpdateItem
                                                       <project>-jobs   claim
                                                       TTL 1 h          append × N
                                                                        finish
                                                                       │
                                                    on failure         ▼
                                       SQS <project>-worker-failures  Bedrock
                                                  │
                                                  ▼ depth alarm
                                       SNS <project>-alerts
```

Both functions run the **same image**. The worker differs only in its
environment: `CHAT_ROLE=worker` mounts `POST /events` and nothing else changes.
That route is reached by the Lambda Web Adapter's *pass-through* feature — a
non-HTTP invocation becomes a POST against `AWS_LWA_PASS_THROUGH_PATH`, which
defaults to `/events` — so a background worker needs no second handler, no
separate dependency and no code that knows it is on Lambda.

### The job item

`<project>-jobs`, `PAY_PER_REQUEST`, hash key `job_id`, TTL on `expires_at`:

| Attribute | Notes |
| --- | --- |
| `job_id` | Partition key, a server-generated `uuid4` hex |
| `status` | `pending` → `running` → `done` / `failed` / `cancelled` |
| `segments` | List of strings. **`len(segments)` is the cursor** — no separate counter that could drift from it |
| `error` | Only on `failed`, and only the generic domain message, never botocore's — which names the model id and the account |
| `created_at`, `deadline_at`, `expires_at` | Epoch seconds. `deadline_at` is written at *create*, not at claim |

**No prompt, no history, no lease, no attempt counter.** The request travels in
the invoke payload instead, which keeps user prompts out of the table and cuts
write cost: `UpdateItem` bills on the larger of the before/after item size even
when updating one attribute, so a history in the item would multiply the cost of
every one of ~25 flushes.

Two attribute names are **DynamoDB reserved words** — `STATUS` and `SEGMENTS`,
and `ERROR` too. Every attribute name in every expression is therefore an alias
(`#status`, `#segments`), which is a structural rule rather than a list to
remember. See the troubleshooting rows below; this cost two production bugs.

### Why `deadline_at` is written at create

Because this design admits Lambda can drop an event. If a job is never claimed
there is no claim time, so a deadline written at claim would never exist — and a
client would poll a spinner forever. Written at create, a read past
`deadline_at` reports `failed` **without writing**, which needs no reaper
function and keeps the read path free of writes. `job_deadline_seconds` is
derived, not configured: `worker_event_age_seconds + worker_timeout_seconds + 60`.

### Cancellation, and why it is the point

`DELETE /api/chat/jobs/{id}` sets `cancelled` conditioned on the job still being
`pending` or `running`. The worker learns about it the next time it flushes: the
append is conditioned on `#status = :running`, so a cancelled job **refuses the
write**, and the rejection is the news. No extra read, no polling, and the work
stops within one flush interval — measured at **270 ms** end to end.

That is the one thing this transport does that the streamed path cannot do at
all. Lambda bills the full duration and does not stop a stream when the viewer
disconnects, so on the streamed path every abandoned chat bills to completion.

The browser also cancels on `pagehide`, with `keepalive: true` so the request can
outlive the page. This matters more than the Stop button: React does not unmount
on tab close, so that path fires nothing otherwise. `navigator.sendBeacon` cannot
be used — it is POST-only.

**One worst case to know:** `anyio.to_thread.run_sync` shields its await, so a
worker blocked on a silent socket cannot be interrupted. A cancel lands only when
the current read returns, which is bounded by `bedrock_read_timeout_seconds`
(60 s) and, outside that, by `worker_timeout_seconds`. That ordering is enforced
by a variable validation.

### Verifying it

```bash
DIST=$(terraform -chdir=infra/serverless output -raw site_url)
EMPTY=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
BODY='{"message":"Say hello in five words."}'

# Both transports advertised? The browser reads this to choose one.
curl -sS "$DIST/api/health"        # {"status":"ok","transports":["sse","jobs"]}

JOB=$(curl -sS -X POST "$DIST/api/chat/jobs" -H 'content-type: application/json' -H "x-amz-content-sha256: $(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)" -d "$BODY" | python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

# The check that matters: `cursor` must CLIMB across polls.
for i in $(seq 1 20); do curl -sS "$DIST/api/chat/jobs/$JOB/segments/0"; echo; sleep 0.4; done

# Slicing from a non-zero cursor returns only what follows it.
curl -sS "$DIST/api/chat/jobs/$JOB/segments/5"

# Bodyless DELETE through OAC. Must be 204, not 403.
curl -sS -o /dev/null -w '%{http_code}\n' -X DELETE "$DIST/api/chat/jobs/$JOB" -H "x-amz-content-sha256: $EMPTY"
```

**`cursor` climbing is the whole assertion.** A single jump from 0 to the final
value means the worker buffered the entire reply and the transport bought
nothing — the same class of mistake as a buffered SSE response, and equally
invisible if you only check that the text arrived.

A cancel mid-reply should leave the job `cancelled` holding a **partial** reply,
not an empty one: the condition stops the work without discarding what was
already written.

Duplicate delivery is worth injecting once, because it is what makes retries
free:

```bash
terraform -chdir=infra/serverless output -raw worker_log_command   # copy and run it, in another shell
aws lambda invoke --function-name aws-chatbot-worker --invocation-type Event --payload "$(printf '{"job_id":"%s","request":{"message":"x"}}' "$JOB" | base64 -w0)" /dev/null --region us-east-2 --profile <name>
```

The second delivery for an already-claimed job logs `was not claimable; nothing
to do`, returns 200 and bills **~15 ms** with no model call. That is why
`maximum_retry_attempts` is 2 rather than 0: the claim condition makes a retry
harmless, and throttles and 5xx are retried for up to
`maximum_event_age_in_seconds` regardless of that setting.

### Measured latency

The polled transport is **not** a latency win, and the honest numbers matter
because the page-load health probe warms the api function for *both* transports,
so the api's cold start cancels out of the comparison:

| | First token |
| --- | --- |
| Streamed, api warmed by the probe | ≈550 ms |
| Polled, both functions warm | ≈700 ms — claim 50 + flush 400 + poll ~150 |
| Polled, cold worker | ≈3.0 s — submit 100 + worker init ~2350 + flush 400 + poll 150 |

So ≈150 ms warm, and ≈2.5 s on a session's first message. Worker `Init Duration`
is consistently **≈2 s** (1957 / 2221 / 2424 ms observed) against the api's 5.5–7 s
for the same image — unexplained, and worth re-measuring before tuning anything.
Warm polls are 7–10 ms.

What it buys instead: durability, replies longer than the 60 s origin read
timeout can carry, and cancellation. It is also a cost win only for *abandoned*
chats — about 20% more per completed one.

### Turning it off

The switch is server-side, and it is better than telling people to add
`?transport=sse` to a URL. Clear `CHAT_WORKER_FUNCTION_NAME` on the api function
and `async_replies_enabled` becomes false: `/api/health` advertises `["sse"]`
only, the job routes are not mounted, and every browser picks the streamed path
on its next page load with no client change. CloudFront caches nothing on
`/api/*`, so it takes effect immediately.

Note that it also cuts off jobs already in flight — their next poll gets a 404,
which the browser reports as an interrupted reply with the partial text kept.

The variable is currently hard-wired in `locals.tf`, so pulling it by hand with
`aws lambda update-function-configuration` drifts from Terraform and the next
apply puts it back.

Not to be confused with the *other* kill switch: reserved concurrency `0` on the
worker sends new events straight to the failure queue with no retries. That stops
spending and preserves the work for replay; it does not move anyone to the
streamed path.

### The failure queue

Events that exhaust their retries or age out land on
`<project>-worker-failures`. It is not polled, has no event source mapping and
triggers nothing — it is an archive with a depth alarm.

```bash
terraform -chdir=infra/serverless output -raw worker_failures_read_command   # copy and run it
```

A record carries `requestContext.condition` (`RetriesExhausted` or
`EventAgeExceeded`), `approximateInvokeCount`, `responseContext.functionError`
and the original `requestPayload` — which is enough to replay it with
`aws lambda invoke --invocation-type Event` once the cause is fixed.

**`sqs:SendMessage` on the worker's own execution role is load-bearing and fails
silently when missing.** Lambda delivers a failure record using the function's
execution role, not a service principal, so without that statement the
destination looks correctly configured in the console and delivers nothing. The
only evidence is the `DestinationDeliveryFailures` metric, which is why
`alerts.tf` alarms on it.

### Data at rest

Stated as a **draft position pending verification with the control owner**, not
as an implemented control:

- **Prompts and history are not written to the job table.** They travel in the
  invoke payload. Verified against a live item, which holds only `segments`,
  `status`, `created_at`, `deadline_at` and `expires_at`.
- **They do reach the failure queue**, in a failure record's `requestPayload`,
  for jobs that exhaust retries. That is the one genuinely new place a user
  prompt comes to rest, and it is why the destination is a queue in the account
  rather than the SNS alert topic — an SNS destination would put prompts in an
  operator's inbox and in SNS delivery logs.
  `worker_failure_retention_seconds` should be the shortest window that still
  allows a replay.
- **Replies are written to DynamoDB**, partial and complete, encrypted under an
  AWS-owned key. The TTL is one hour, but DynamoDB deletes expired items only
  *typically within 48 hours*, so the honest figure is up to ~49 hours.
- **Point-in-time recovery is off deliberately.** A PITR-enabled table retains
  deleted items for 35 days, which would quietly undo the retention decision.
- **A reply is not less sensitive than a prompt** — models routinely quote the
  question back. Treat the table as holding conversation content.
- No prompt or reply text is logged; log lines carry the job id only.

## Redeploy

Three kinds of change, three different commands. Knowing which is which is most
of operating this target.

### Application code (backend)

```bash
git push
bash ./scripts/release-api.sh
```

Builds the `lambda` stage, pushes to ECR under the commit SHA, then points **both
functions** at the new **digest** and waits for each to become `Active`. No
`terraform apply` — and running one would do nothing, because `image_uri` is
ignored.

The wait is not cosmetic: a container-image update goes `Pending` while Lambda
re-optimises the image, and rejects invocations until it is `Active` again.
Without the wait the script would exit successfully while the deployment is
still broken.

#### The two updates are not atomic, so the schema must be additive-only

The script loops over `function_names`, but nothing makes the two updates a
single transaction. For a moment a new api talks to an old worker. That creates a
rule worth stating before it is discovered:

> **Never rename a field of the invoke payload or the job item, and never add a
> required one.** Add optional fields, read them defensively, and remove them a
> release later.

Rename `segments` to `reply` and the new api writes one name while the old worker
writes the other: every job hangs, with no error anywhere. `WorkerEventDTO` sets
`extra="ignore"` for the same reason, so a new sender can add a field without
breaking an old receiver.

The counterpart when both functions are on one digest:

```bash
terraform -chdir=infra/serverless output -raw deployed_image_command   # copy and run it
```

Both lines must print the same digest. If the worker is behind, it is running
code the api no longer expects — the worst failure class here, because the api
half looks perfectly healthy.

### Application code (frontend)

```bash
bash ./scripts/release-web.sh
```

Builds the bundle through the same `check:layers` and `tsc -b` gates the Caddy
image uses, syncs assets with a one-year immutable cache, uploads `index.html`
last with `no-cache`, then invalidates only `/` and `/index.html`.

Assets go first and `index.html` last so the shell is never newer than the chunks
it names. The sync never uses `--delete`: the previous release's content-hashed
chunks must survive for tabs still open on the old bundle.

### Configuration or infrastructure

```powershell
terraform -chdir=infra/serverless apply -var-file=../shared.tfvars
```

Environment variables, memory, timeout, retention, price class. A change to the
function's environment is an in-place update taking seconds.

### What is deployed right now

There is no host to inspect and no `git log` to run, so the deployed revision is
an image digest:

```powershell
terraform -chdir=infra/serverless output -raw deployed_image_command   # copy and run it
```

## Logs

```powershell
terraform -chdir=infra/serverless output -raw api_log_command      # copy and run it
terraform -chdir=infra/serverless output -raw worker_log_command   # the polled transport
```

Each prints an `aws logs tail --follow` invocation. There is no SSH equivalent,
no `docker compose logs`, and no shell — better in steady state, worse the first
time init fails and there is no stack trace to reach for.

**A job's story is split across the two groups**, and following it means reading
both. The api logs `job <id> submitted` and `job <id> cancellation requested`;
the worker logs `job <id> claimed`, then one of `done`, `failed: <reason>`,
`is cancelled; stopping`, or `was not claimable; nothing to do`. The job id is in
the message text rather than a structured field, deliberately: `alerts.tf`
matches on the level token that `%(levelname)s %(name)s: %(message)s` produces,
and a new field would not change that but a new *format* would break it. There is
no log line per flush — ~25 lines per chat would make CloudWatch the largest line
item on the stack.

The `REPORT` line after each invocation carries `Init Duration` (cold start),
`Duration`, `Billed Duration` and `Max Memory Used`. Measure before tuning
`memory_size_mb`.

Current numbers on this deployment: api init **≈5.5–7 s**, warm `/api/health`
**≈10 ms**, a full canned stream **≈550 ms**, `Max Memory Used` **≈97–119 MB of
1536**. Memory is buying CPU for init, not working set.

The worker, from the **same image at the same memory size**, inits in **≈2 s**
(1957 / 2221 / 2424 ms observed) — consistently under half the api's, for reasons
nothing in the code explains. Worth re-measuring before drawing a conclusion from
it, and worth knowing because it is what makes the polled transport's cold-start
penalty ~2.5 s rather than the ~6.4 s a symmetric guess would predict. A warm
poll is 7–10 ms; a duplicate event that loses the claim race is ~15 ms.

## Error alerts

Tailing logs only helps when you already know something is wrong. `alerts.tf`
closes that gap: **seven alarms** publish to one SNS topic, which mails
`alert_email`.

```
log group per function ──► metric filter ──► log-errors alarm ──┐
        AWS/Lambda Errors ────────────► invocation-errors alarm ─┤
                                                                 ├──► SNS ──► email
        AsyncEventsDropped ──────────────────────────────► alarm ─┤
        DestinationDeliveryFailures ─────────────────────► alarm ─┤
        SQS ApproximateNumberOfMessagesVisible ──────────► alarm ─┘
```

**Two alarms per function, because neither one sees the other's failures.** Both
are created through a `for_each` over `local.functions`, so the api and the worker
cannot drift into different alerting.

| Alarm | Catches | Misses |
| --- | --- | --- |
| `<project>-<fn>-log-errors` | What the application reports: a Bedrock call the adapter could not complete, an unhandled exception in a route | Anything that kills the process before it can log |
| `<project>-<fn>-invocation-errors` | What Lambda counts: OOM kill, failed init, timeout | Application errors — the function returned a response, so Lambda calls the invocation a success |

Three more exist only for the polled transport, and each guards something that
otherwise fails **silently**:

| Alarm | Why it has to exist |
| --- | --- |
| `<project>-worker-events-dropped` | `AsyncEventsDropped > 0` is the *only* signal that Lambda's opaque queue discarded a job. Without it, the removal of SQS from the request path would be an unobservable risk rather than a measured one. |
| `<project>-worker-destination-failures` | `DestinationDeliveryFailures > 0`. A missing `sqs:SendMessage` on the worker's role, or an oversized record, loses the failure archive with no other evidence at all. |
| `<project>-worker-failures-waiting` | Queue depth `> 0`: something exhausted its retries. Period is **300 s**, not 60 — SQS publishes these metrics every five minutes, and a 60 s period would flap across the gaps. |

One thing the log-errors alarm deliberately does **not** catch: a reply the model
refused. That raises `ReplyGenerationError`, which is a `ChatError` — the job
settles as `failed` with a generic message, the invocation succeeds, and nothing
alarms. A failed *reply* is not a process defect. If replies start failing, the
evidence is the job's `error` field and an `INFO`-level `job <id> failed:` line,
not your inbox.

The first alarm exists because an application error is invisible to Lambda's own
metrics. `bedrock_reply_generator.py` catches the failure, logs it and returns a
502 to the browser — a perfectly successful invocation as far as `AWS/Lambda
Errors` is concerned.

**The log half depends on the application emitting a level with each record.**
`backend/app/infrastructure/logging.py` installs a root handler formatted
`LEVELNAME logger: message`. Without it, records fall through to Python's
`lastResort` handler, which writes the bare message with no level in it — and a
metric filter matching on text finds nothing. The alert would then stay silent
in exactly the case it exists for, which is why the coupling is asserted in
`backend/tests/test_logging.py` rather than left to a comment.

The filter pattern is an OR across four terms: `ERROR`, `CRITICAL`,
`Task timed out`, `Runtime exited with error`.

Both alarms fire on **one** breaching datapoint in a 60-second period, with
`treat_missing_data = "notBreaching"` and `ok_actions` set so recovery mails too.
That is more sensitive than the usual "2 of 5" advice, deliberately: that advice
is for rate thresholds on busy services, where one error in ten thousand
invocations is noise. Here the threshold is *zero* on low traffic, so an error is
rare, always unexpected, and worth knowing about the first time.

### Setting the address

`alert_email` goes in **`infra/serverless/terraform.tfvars`** — gitignored, and
auto-loaded with no `-var-file` flag. Create it if it does not exist; every other
value in it is optional, so a one-line file is valid:

```powershell
Set-Content infra/serverless/terraform.tfvars 'alert_email = "you@example.com"' -Encoding utf8
terraform -chdir=infra/serverless apply -var-file=../shared.tfvars
```

**Not `../shared.tfvars`.** That file is for settings *both* stacks declare, and
`../server` has no `alert_email`, so a value there makes every server-stack
command print:

```
Warning: Value for undeclared variable
The root module does not declare a variable named "alert_email" but a value was found in file "../shared.tfvars".
```

A warning, not an error — so it works, and then warns forever on the stack that
has nothing to do with alerting.

Neither file is required. `-var 'alert_email=you@example.com'` on the command
line works too, and leaving the variable unset is a supported outcome: Terraform
creates the topic and both alarms with no subscriber, and `alerts_topic_arn`
prints the command to subscribe by hand. That is the better route if the address
should not sit in a file at all.

### Verifying the filter pattern

A pattern that matches nothing fails silently — the alarm simply never fires —
so it is worth checking against real log lines rather than reading it. AWS will
evaluate it for you, and the call is read-only:

```powershell
aws logs test-metric-filter --region us-east-2 --profile <name> --filter-pattern '?ERROR ?CRITICAL ?\"Task timed out\" ?\"Runtime exited with error\"' --log-event-messages 'ERROR app.infrastructure.bedrock_reply_generator: Bedrock rejected or dropped a converse_stream call' 'START RequestId: abc Version: $LATEST' 'REPORT RequestId: abc Duration: 46.12 ms Billed Duration: 47 ms'
```

The first message must match and the other two must not. A `REPORT` line
matching would mean an alert on every successful invocation.

**The `\"` escapes are required and are not a typo.** PowerShell 5.1 strips
double quotes from an argument before handing it to a native executable — even
inside single quotes — and the C runtime then splits the pattern on spaces into
several arguments, so the CLI reports the fragments as `Unknown options`.
Escaping them keeps the pattern one argument. Under Git Bash the plain form
works and the escapes are unnecessary.

For a request with no shell quoting at all, put it in a file:

```json
{
    "filterPattern": "?ERROR ?CRITICAL ?\"Task timed out\" ?\"Runtime exited with error\"",
    "logEventMessages": [
        "ERROR app.infrastructure.bedrock_reply_generator: Bedrock rejected or dropped a converse_stream call",
        "REPORT RequestId: abc Duration: 46.12 ms Billed Duration: 47 ms"
    ]
}
```

```powershell
aws logs test-metric-filter --cli-input-json file://test-metric-filter.json --region us-east-2 --profile <name>
```

### Confirming the subscription

**SNS cannot confirm an email subscription for you.** Terraform creates it, SNS
mails a confirmation link, and until someone clicks it the endpoint sits at
`PendingConfirmation` and **delivers nothing**. An apply that succeeds is not yet
a working alert.

```powershell
terraform -chdir=infra/serverless output -raw alerts_subscription_check_command   # copy and run it
```

A `SubscriptionArn` of literally `PendingConfirmation` means the link is still
unclicked. It expires after three days, and a `terraform destroy` cannot remove
an unconfirmed subscription — only time does.

### Verifying delivery

Prove the topic, the subscription and the mail all work without waiting for a
real failure:

```powershell
terraform -chdir=infra/serverless output -raw alerts_test_command   # copy and run it
```

That overrides the alarm's state rather than faking it: CloudWatch fires the
alarm actions exactly as it would for a real breach, then re-evaluates against
the metric within a minute or two and mails the recovery as well. Two emails
means the whole path works.

Note what the email *does not* contain: the log text. A CloudWatch alarm
notification carries the alarm name, the metric and a timestamp — nothing from
the log event that tripped it. Go read the log group; the alarm description names
the `api_log_command` output for exactly that reason. Putting the error text in
the mail would take a subscription filter and a forwarder function, which is a
third Lambda to own for a deployment with one operator.

### Cost

Seven standard-resolution alarms at $0.10 plus two custom metrics at $0.30 —
roughly **$1.30/month** at list price. Metric filters themselves are free, and
the five alarms watching `AWS/Lambda` and `AWS/SQS` metrics add no metric charge
because those are published by the services.

Worth stating on a stack that is otherwise pennies: **the alerting is the largest
line item**, and it grew from ~$0.70 with the polled transport. It is still the
right trade — three of those alarms guard failures that are otherwise completely
silent — but it is the thing to cut first if this stack ever needs to be cheaper,
not the Lambdas.

## Teardown

```powershell
terraform -chdir=infra/serverless destroy -var-file=../shared.tfvars
```

Takes longer than it creates — CloudFront must be disabled before it can be
deleted, which is 15+ minutes on its own.

Both `force_delete` on the registry and `force_destroy` on the bucket are set,
so a non-empty repository and a non-empty bucket do not block the destroy.
Neither holds anything durable: images and bundles are rebuildable from any
commit.

The job table has **no deletion protection and no PITR**, deliberately, so it
does not block a destroy either. Anything in it is a reply that expires within
the hour anyway. The failure queue goes with it, including any records still on
it — read them first if you might want to replay them.

Unlike the server target, **there is no standing cost to leaving this deployed.**
The server target's Elastic IP bills hourly once merely allocated, which is the
one way it costs money while idle. Here an idle deployment is pennies of storage.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Too many command line arguments` from Terraform | PowerShell does not treat `\` as a line continuation, so a multi-line POSIX command arrives as positional arguments. Use the single-line form. |
| `Your request contains one or more invalid invalidation paths` | Git Bash rewrote `/index.html` into `C:/Program Files/Git/index.html`. `scripts/_common.sh` exports `MSYS_NO_PATHCONV=1` to prevent this; the same rewrite breaks `--log-group-name` in a hand-typed command. |
| `failed to satisfy constraint` on `logGroupName` | The same Git Bash rewrite. `export MSYS_NO_PATHCONV=1` before running `aws logs` by hand. |
| **403 on every request, including GETs**, with Lambda's function-URL auth message | A `RESPONSE_STREAM` function URL needs **both** `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`. A buffered URL works with the first alone, and nothing in the error hints at the second. Both are in `url.tf`. |
| 403 on POST only, GET fine | The `x-amz-content-sha256` header is missing. CloudFront cannot hash a body it is streaming and Lambda rejects `UNSIGNED-PAYLOAD`, so the browser supplies the digest — see `sseChatGateway.ts`. Also confirm the origin request policy is `AllViewerExceptHostHeader`, which forwards it. |
| `Stack output 'lambda_architecture' is empty or missing` | A `-target` apply prunes every output not reachable from the targeted resources. `tf_setting` in `_common.sh` falls back to reading the variable directly; if you see this, `../shared.tfvars` is probably missing. |
| `image manifest, config or layer media type … is not supported` | buildx emitted an OCI image *index* with attestations, and Lambda does not support manifest lists. `--provenance=false --sbom=false` is already in the release script; this appears if you build by hand without them. |
| `<tag> is already in the registry` | The repository is `IMMUTABLE` and tags are commit SHAs, so this commit has already been released. Commit your change, or delete the tag to rebuild it. |
| 422 on `/api/chat` | The body field is `message`, not `prompt`, and `ChatRequestDTO` sets `extra="forbid"` — so a wrong key is rejected rather than ignored. |
| Reply arrives all at once | Check in this order: `compress = false` on the `/api/*` behaviour (edge compression must buffer in order to compress), `invoke_mode = "RESPONSE_STREAM"` on the URL, and `AWS_LWA_INVOKE_MODE=response_stream` in the environment. The last two must agree, and if they disagree Lambda buffers — which looks exactly like CloudFront buffering. |
| Every invocation times out, health never passes | `AWS_LWA_PORT` must be `8000`. The adapter defaults to **8080** and the image's `CMD` tells uvicorn 8000; it is baked into `api.Dockerfile` for that reason. |
| First request after a deploy fails | The function was still `Pending` while Lambda re-optimised the image. `release-api.sh` waits for `Active`; a manual `update-function-code` does not. |
| One slow request every so often | A cold start, ≈6.4 s here. SnapStart is unavailable for container images, so the reflexive fix does not exist for this packaging. |
| Every chat replies "The assistant could not complete the reply." | The API reached the code path but Bedrock refused; the generic message is deliberate so vendor detail never reaches the browser. The real cause is in the log group. Usual suspects: the model not enabled in `bedrock_region`, or `CHAT_BEDROCK_MODEL_ID` and the IAM policy's ARN disagreeing. |
| `AccessDeniedException` on `InvokeModel` | The role names one model ARN. If `bedrock_model_id` or `bedrock_region` changed, apply to re-scope it; IAM is eventually consistent, so allow a few seconds. Check against `terraform output bedrock_model_arn`. |
| A missing asset returns 200 with `index.html` | The `s3:ListBucket` grant is missing, so S3 answers 403 rather than 404. This matters: the browser then executes HTML as JavaScript and fails with a syntax error pointing nowhere near the cause. See `site.tf`. |
| A deep path returns 404 instead of the app | The SPA function is not attached to the default behaviour, or the path contains a dot and was treated as a file. See `functions/spa-router.js`. |
| Plan wants to change `image_uri` | It should not — `lifecycle { ignore_changes = [image_uri] }` exists precisely because the API reports a digest where the configuration names a tag. If you see it, that block was removed. |
| Destroy hangs for a long time on CloudFront | Expected. It must be disabled before deletion. |
| `Unknown options: ?Task timed out, ?CRITICAL …` from `aws logs test-metric-filter` | PowerShell 5.1 strips the double quotes inside the filter pattern before handing the argument to a native exe, and the C runtime then splits it on spaces into several arguments. Escape them as `\"` even inside single quotes, or pass the whole request with `--cli-input-json`. See [Verifying the filter pattern](#verifying-the-filter-pattern). |
| No alert email ever arrives, but the alarm shows ALARM in the console | The subscription is still `PendingConfirmation` — SNS mailed a link nobody clicked, and it delivers nothing until then. Check with the `alerts_subscription_check_command` output. |
| Errors appear in the log group but the log alarm stays OK | The records carry no level, so the filter matches nothing. Confirm the line reads `ERROR app.infrastructure...: ...` and not a bare message; if it is bare, `configure_logging()` is not being called — see `backend/app/main.py`. |
| The log alarm sits in INSUFFICIENT_DATA | `default_value = "0"` is missing from the metric filter, so the metric only exists in minutes that contained an error. See `alerts.tf`. |
| Both alarms mail for the same failure | Correct, and not a bug: a timeout is both a logged `Task timed out` line and a counted Lambda error. Two mails for one cause is the accepted price of neither alarm having blind spots. |
| Destroy leaves an SNS subscription behind | An unconfirmed subscription cannot be deleted through the API. It expires on its own after three days. |
| An alert subscription exists that Terraform has never heard of | Subscribing by hand — including via the `alerts_topic_arn` output's suggested command — creates a subscription outside state. It will not appear in any plan and `destroy` leaves it attached to a topic that no longer exists. `aws sns list-subscriptions-by-topic` shows all of them; `terraform state show 'aws_sns_topic_subscription.alerts_email[0]'` shows the one that is managed. |
| **`ValidationException: Attribute name is a reserved keyword; reserved keyword: segments`** | `SEGMENTS` is a DynamoDB reserved word, as are `STATUS` and `ERROR`. Every attribute name in every expression must be an alias — `#segments`, not `segments`. This shipped: only `append` names that attribute, so the worker claimed each job, had its first flush refused, and every reply failed. The rule is enforced by the stub in `tests/test_dynamodb_job_store.py`, which rejects any unaliased name. |
| **`ValidationException: Value provided in ExpressionAttributeNames unused in expressions`** | The opposite mistake, and it also shipped: a single shared map of every alias was passed to all five writes, and DynamoDB rejects a request declaring an alias no expression mentions. Four of the five failed. Each operation must declare exactly the aliases it uses — `_STATUS_ONLY` versus `_STATUS_AND_ERROR` and so on. |
| `ValidationException` from `list_append` on the first flush | `segments` must be created as an empty list by the `PutItem`. `list_append` against a *missing* attribute is a validation error, so every job's first flush would fail. |
| **`RuntimeError: CHAT_WORKER_FUNCTION_NAME is not set, so no dispatcher can be built`** in the worker log | The worker legitimately has no worker-function name and no permission to invoke anything. If its route asks for a use case that requires a dispatcher, its dependency graph cannot be satisfied and **every event fails before reaching application code**. This shipped too: `ChatJobService` (submit/read/cancel) and `ChatJobRunner` (run) are split so the worker asks only for what it uses. |
| Jobs sit at `pending` forever, worker log empty | The event never arrived. Check the `AsyncEventsDropped` alarm and the failure queue; if the worker's reserved concurrency is 0, that is the kill switch and events go straight to the queue. The client is not stuck forever either way — a read past `deadline_at` reports `failed`. |
| Jobs sit at `pending`, worker log shows a claim failing | Something already claimed it, or it was cancelled before the worker started. A failed claim logs `was not claimable` and returns 200 so Lambda does not retry it. |
| Every job goes `failed` with "The job store could not be reached." | The generic message covers any non-conditional DynamoDB failure; the real one is in the worker log, logged with a stack trace. Usual suspects: a `ValidationException` from the rows above, or `AccessDeniedException` because `iam.tf` grants the worker `UpdateItem` **only** — no `GetItem`, by design, since every step it takes is a conditional write. |
| 403 on `DELETE /api/chat/jobs/{id}` | Send `x-amz-content-sha256` set to the SHA-256 of the empty string. In practice a bodyless request through OAC succeeds either way — CloudFront signs at the edge and there is no body to disagree about — so a 403 here points at the origin request policy not forwarding the header, or at the OAC itself. |
| No failure record ever arrives, but invocations are failing | Either `sqs:SendMessage` is missing from the **worker's own** execution role — Lambda delivers destination records using the execution role, and without it the destination silently delivers nothing — or `AWS_LWA_ERROR_STATUS_CODES` is unset. See the next row. |
| A worker crash is reported to Lambda as a success | **`AWS_LWA_ERROR_STATUS_CODES` is not set.** The adapter treats *every* response it gets back, 500 included, as a successful invocation unless told otherwise — so with it unset there is no retry, no failure record and no alarm, and the entire failure design is inert while looking configured. `locals.tf` sets `404,422,500-599` on the worker. Confirmed working: a forced 500 produced `RetriesExhausted` after exactly 3 invocations. |
| Duplicated text in the middle of a reply | The `size(#segments) = :expected` condition on each append is missing. `list_append` is not idempotent and botocore retries a call whose response was lost coming back, so an append can commit and then be re-sent — with no log line saying anything went wrong. |
| A cancel does not stop the worker for up to a minute | It is blocked on a model read. `anyio.to_thread.run_sync` shields its await, so nothing can interrupt it; `bedrock_read_timeout_seconds` is the real bound. |
| The queue-depth alarm stays in ALARM after fixing the cause | The records are still on the queue. `aws sqs purge-queue` clears it, takes up to 60 s to take effect, and the alarm needs a datapoint at its 300 s period afterwards — SQS publishes those metrics every five minutes, which is also why that alarm's period is not 60 s. |
| Health reports `transports: ["sse"]` on the serverless target | `async_replies_enabled` is false, so `CHAT_JOBS_TABLE` or `CHAT_WORKER_FUNCTION_NAME` is empty on the api function. That is also the kill switch, so check whether somebody pulled it deliberately. |
| The browser uses the streamed path when both are advertised | The bundle is older than the default flip, or the URL carries `?transport=sse`. The wire readout names the transport in use: `POST /api/chat` or `POST /api/chat/jobs`. |

## Known limits

- **Local state, per stack.** `terraform.tfstate` in this directory is the only
  record of what exists, and it is gitignored. Back it up before anything
  destructive. Separate state from `../server` is the point: destroying one
  target cannot touch the other.
- **Cold starts are real.** ≈6.4 s, and **billed** — the first request cost
  6399 ms of billed duration for 46 ms of work. Container images cannot use
  SnapStart, and provisioned concurrency (~$13/month per unit) would erase the
  cost advantage entirely.
- **Lambda bills the full duration and does not stop a stream when the viewer
  disconnects.** True of the **streamed** transport only: the frontend cancels its
  reader on abort, but the function keeps generating and billing to completion or
  timeout. This has no equivalent on the server target. `timeout_seconds` and
  `bedrock_max_output_tokens` are the bounds. The polled transport is the answer
  to this — see [Cancellation](#cancellation-and-why-it-is-the-point) — which is
  most of why it exists.
- **Lambda's asynchronous queue is opaque.** You cannot inspect its contents and
  cannot replay from it. Duplicates are handled by the claim condition and by
  `size(#segments) = :expected`; a drop surfaces to the user as a deadline-healed
  failure and to us as `AsyncEventsDropped`. Replay exists only for events that
  reached the on-failure queue.
- **Two functions can drift**, in three ways that all half-work silently: the
  release loop updating one and not the other, their environment maps
  disagreeing, or a non-additive schema change. Mitigated by `merge()` on a
  common environment, the two-function release loop and the additive-only rule —
  and each has a troubleshooting row because none of them announces itself.
- **A cancelled reply is kept, not discarded.** Whatever was flushed before the
  cancel stays in the table until its TTL and is shown to the person who stopped
  it. That is deliberate, but it does mean stopping a reply is not a way to
  un-write it.
- **The polled transport cannot be exercised in `docker compose`.** There is no
  DynamoDB there, so `?transport=jobs` would surface as a 503. Route and use-case
  behaviour are covered by pytest against fakes; the transport itself is verified
  on AWS.
- **A release is not atomic.** Bundle upload, cache invalidation and the function
  update are three independently-timed steps. Nothing coordinates them, so a
  frontend expecting a new API contract needs the API released first.
- **The build environment moved to your workstation.** Docker and buildx are now
  prerequisites where Terraform and the AWS CLI used to suffice. Building `arm64`
  on an x86 machine runs under QEMU, and `uv sync` under emulation is slow — which
  is why `lambda_architecture` defaults to `x86_64`.
- **No VPC, deliberately.** The function calls only public AWS endpoints, so
  there is no NAT gateway and no interface endpoints to pay for — **and a VPC
  would break Function URL response streaming outright.**
- **No custom domain.** `cloudfront_default_certificate` means the hostname is
  `*.cloudfront.net`. A custom domain needs ACM in **us-east-1** specifically,
  which brings back a second provider alias this stack currently does without.
- **CloudFront's origin read timeout caps at 60 s** without a quota increase
  (180 s with one). It is per-packet rather than total, so it only bites if the
  model goes silent for a full minute mid-stream.
- **No authentication, rate limiting or WAF.** Same as the server target, and
  more exposed for it: there is no instance to stop. A runaway or hostile caller
  bills per invocation.
