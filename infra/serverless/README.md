# Serverless target

The same application as [../server](../server), with no server. CloudFront serves
the React bundle from S3 and forwards `/api/*` to a Lambda function that runs the
**unmodified** `uvicorn app.main:app` process — and streams Server-Sent Events
end to end.

Nothing in `backend/` knows it is on Lambda: no handler, no Mangum, no ASGI
shim, no extra dependency. That is the property the whole design is built to
keep, and it is what makes "one codebase, two deployment targets" true rather
than aspirational.

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
                                           │ Lambda (container image)      │
                                           │  lambda-adapter (extension)   │
                                           │  └─► uvicorn + FastAPI, SSE   │
                                           └──────────────┬───────────────┘
                                                          ▼
                                                    Bedrock (Converse stream)
```

Neither origin is publicly reachable. S3 blocks all public access and the
Function URL requires `AWS_IAM`, so CloudFront — signing with Origin Access
Control — is the only caller either one accepts.

| File | Holds |
| --- | --- |
| `versions.tf` | Terraform and provider constraints; why state is local and per-stack |
| `providers.tf` | Region and default tags. No credentials — those are ambient |
| `variables.tf` | Every input. All have defaults; an empty `terraform.tfvars` is valid |
| `locals.tf` | Name prefix, tags, log group name, and the function's environment |
| `ecr.tf` | Image registry: immutable tags, lifecycle policy, explicit Lambda pull grant |
| `logs.tf` | The log group, created *before* the function so retention is never absent |
| `alerts.tf` | Error alerting: metric filter, two alarms, and the SNS topic they mail |
| `iam.tf` | Execution role: own log group, plus invoke on one Bedrock model |
| `lambda.tf` | The function. `ignore_changes = [image_uri]` keeps Terraform out of releases |
| `url.tf` | Function URL (`RESPONSE_STREAM`) and the two permissions CloudFront needs |
| `site.tf` | Private bucket for the bundle, and the policy that makes a 404 a 404 |
| `cdn.tf` | Distribution, both OACs, the SPA function, and the two cache behaviours |
| `functions/spa-router.js` | Client-side routing at the edge |
| `outputs.tf` | The URL, and every command used after apply |

25 resources — 24 if `alert_email` is left empty. Compare 17 for the server
target: the count is similar, but the *shape* is not. Nothing here is a host,
and nothing here holds state.

## Four decisions explain most of the configuration

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
  CloudWatch alarms and SNS. Wider than the server target's, because a release
  now touches four services rather than sending one SSM command.
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
is quick.

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

# 2. The API, through CloudFront's OAC. {"status":"ok"}
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

## Redeploy

Three kinds of change, three different commands. Knowing which is which is most
of operating this target.

### Application code (backend)

```bash
git push
bash ./scripts/release-api.sh
```

Builds the `lambda` stage, pushes to ECR under the commit SHA, then points the
function at the new **digest** and waits for it to become `Active`. No
`terraform apply` — and running one would do nothing, because `image_uri` is
ignored.

The wait is not cosmetic: a container-image update goes `Pending` while Lambda
re-optimises the image, and rejects invocations until it is `Active` again.
Without the wait the script would exit successfully while the deployment is
still broken.

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
terraform -chdir=infra/serverless output -raw api_log_command   # copy and run it
```

That prints an `aws logs tail --follow` invocation. There is no SSH equivalent,
no `docker compose logs`, and no shell — better in steady state, worse the first
time init fails and there is no stack trace to reach for.

The `REPORT` line after each invocation carries `Init Duration` (cold start),
`Duration`, `Billed Duration` and `Max Memory Used`. Measure before tuning
`memory_size_mb`.

Current numbers on this deployment: init **≈6.4 s**, warm `/api/health` **≈10 ms**,
a full canned stream **≈550 ms**, `Max Memory Used` **≈97 MB of 1536**. Memory is
buying CPU for init, not working set.

## Error alerts

Tailing logs only helps when you already know something is wrong. `alerts.tf`
closes that gap: two alarms publish to one SNS topic, which mails
`alert_email`.

```
log group ──► metric filter (ApiErrorCount) ──► alarm ──┐
                                                        ├──► SNS ──► email
        AWS/Lambda Errors ────────────────────► alarm ──┘
```

**Two alarms, because neither one sees the other's failures.**

| Alarm | Catches | Misses |
| --- | --- | --- |
| `<project>-api-log-errors` | What the application reports: a Bedrock call the adapter could not complete, an unhandled exception in a route | Anything that kills the process before it can log |
| `<project>-api-invocation-errors` | What Lambda counts: OOM kill, failed init, timeout | Application errors — the function returned a response, so Lambda calls the invocation a success |

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
second Lambda to own for a deployment with one operator.

### Cost

One custom metric and two alarms, roughly **$0.70/month**. Metric filters
themselves are free. Worth stating on a stack that is otherwise pennies: the
alerting is the line item.

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
  disconnects.** The frontend cancels its reader on abort, so abandoned chats are
  routine, and each bills to completion or timeout. This has no equivalent on the
  server target. `timeout_seconds` and `bedrock_max_output_tokens` are the bounds.
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
