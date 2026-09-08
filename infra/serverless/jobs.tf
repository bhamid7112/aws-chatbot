# Where an asynchronous reply lives while it is being written, and where a job
# that failed outright is archived.
#
#   POST /api/chat/jobs ──> table (pending) ──> worker claims it ──> appends
#          GET .../segments/N <──────────────────┘
#
# The table holds *generated text only*. The prompt and the conversation history
# travel to the worker in the invocation payload and are never written here, for
# two reasons that happen to point the same way: DynamoDB charges write capacity
# on the whole item on every write, so a long history in the item would multiply
# the cost of every flush by its own size; and a prompt not written to a database
# is a prompt with no retention question attached.

resource "aws_dynamodb_table" "jobs" {
  name         = "${local.name}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  # Only the key is declared. DynamoDB is schemaless beyond its keys, so
  # `status`, `segments`, `deadline_at` and the rest exist without being named
  # here — and naming them would suggest a schema this table does not enforce.
  #
  # There is no sort key and no index. Every access is by job id: the client
  # holds the one it was given, and the worker was told which one to run. A
  # secondary index would only enable queries nothing performs.

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Encryption is left at the default, and that is the cheaper *and* the correct
  # choice rather than a compromise. DynamoDB encrypts every table at rest with
  # an AWS-owned key at no charge; setting server_side_encryption here would
  # switch it to a billed AWS-managed KMS key, adding a monthly cost and a
  # per-request charge for no change in who can read the data.

  # Off, and it must stay off. Point-in-time recovery retains deleted items for
  # 35 days, which would quietly overturn the retention decision that
  # job_retention_seconds documents: replies would survive five weeks in
  # backups after expiring in an hour.
  point_in_time_recovery {
    enabled = false
  }

  # Explicitly false so `terraform destroy` keeps working, as it does for every
  # other resource in this stack. A job's contents are worthless an hour after
  # it finishes; there is nothing here worth protecting a table for.
  deletion_protection_enabled = false

  tags = { Name = "${local.name}-jobs" }
}

# Where Lambda writes a job it could not deliver or could not run.
#
# This is the queue that was *removed* from the request path and put back on the
# failure path. Handing work to Lambda's own event queue gives us durable
# buffering, backoff on throttles, bounded retries on errors and a failure
# destination for free — so there is nothing for a queue to do in front of the
# worker. But Lambda's queue is opaque: it cannot be inspected and it cannot be
# replayed. This queue is where that capability comes back.
#
# Nothing consumes it. There is no event source mapping, no visibility timeout to
# size against a function, and no consumer to keep up. It is an archive with a
# depth alarm on it (see alerts.tf), and `aws sqs receive-message` is how a
# failed job is read.
#
# An SNS topic was the obvious alternative and is the wrong one. AWS documents a
# 256 KB ceiling on an SNS message, and a failure record carries the original
# payload plus metadata — so a long conversation would make the record
# undeliverable, losing the report for exactly the jobs most worth reporting.
# It would also mail the prompt to whoever receives the alerts.
resource "aws_sqs_queue" "worker_failures" {
  name = "${local.name}-worker-failures"

  message_retention_seconds = var.worker_failure_retention_seconds

  # SSE-SQS rather than a KMS key: encryption at rest at no cost and with no
  # per-request charge. These records contain prompts, so it is not optional.
  sqs_managed_sse_enabled = true

  tags = { Name = "${local.name}-worker-failures" }
}
