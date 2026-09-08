# Error alerting: one topic, one email subscription, and alarms covering five
# distinct ways this deployment can fail.
#
#   per function:
#     log group ──> metric filter ──> log-errors alarm ──────┐
#     AWS/Lambda Errors ───────────> invocation alarm ───────┤
#   worker only:                                             ├──> SNS ──> email
#     AWS/Lambda AsyncEventsDropped ──────────> alarm ───────┤
#     AWS/Lambda DestinationDeliveryFailures ──> alarm ──────┤
#     SQS depth on the failure queue ──────────> alarm ──────┘
#
# The per-function alarms are iterated over local.functions rather than written
# twice, and that is the point of the map: an alarm someone forgot to copy for
# the worker would leave the half of the system nobody watches — the half that
# runs with no client connected — failing in silence. Adding a third function
# cannot forget its alarms.
#
# The five kinds, and why none subsumes another:
#
#   * The log filter catches what the *application* reports — a Bedrock call it
#     could not complete, an unhandled exception. Lambda considers such an
#     invocation successful, because the function returned a response; its
#     Errors metric stays flat and only the log knows.
#   * The Errors metric catches what kills the process before it can write
#     anything worth matching: an OOM kill, a failed init, a timeout. There is
#     no log line to filter because there was no chance to log one.
#   * AsyncEventsDropped catches a job Lambda threw away without running it.
#     Nothing in the application ever sees such a job, and the client is told
#     only that it passed its deadline. This is the sole signal that the event
#     queue — which this stack relies on in place of a queue of its own — is
#     losing work, and without it that reliance would be unobservable.
#   * DestinationDeliveryFailures catches the alerting failing. A failure record
#     too large for its destination, or a missing sqs:SendMessage grant, means
#     the archive is empty exactly when it matters, with nothing else to say so.
#   * Queue depth catches jobs that failed and *were* archived, which is the one
#     of the five that means "go and read the record".
#
# The log-filter half depends on the application emitting a level with each
# record, which it does only because backend/app/infrastructure/logging.py
# installs a handler. Left to Python's fallback the message arrives with no
# level in it, matches nothing, and this whole file goes quiet without failing.
# That coupling is asserted in backend/tests/test_logging.py rather than left as
# a comment.
#
# The worker's log filter has a second dependency, and it is a configuration
# one: the adapter must be told which status codes mean failure, or a crashing
# worker is reported to Lambda as a success and neither its Errors alarm nor its
# retries nor this queue ever see it. See AWS_LWA_ERROR_STATUS_CODES in
# locals.tf.
#
# Cost: two custom metrics and seven alarms, roughly $2/month at current
# CloudWatch prices. Metric filters themselves are free. Worth stating on a
# deployment that is otherwise near-zero — the alerting is the line item.

resource "aws_sns_topic" "alerts" {
  name         = "${local.name}-alerts"
  display_name = "${local.name} alerts"

  tags = { Name = "${local.name}-alerts" }
}

# No aws_sns_topic_policy, and the absence is deliberate rather than an
# oversight. A CloudWatch alarm publishing to a topic in its own account needs
# no policy statement — the permission is implicit — and the default policy SNS
# attaches already restricts Publish to principals in this account. Writing an
# explicit policy here could only narrow it further, at the cost of also locking
# the operator out of `aws sns publish` for testing. Nothing is gained.

# Email subscriptions cannot be completed by an API call: SNS mails a
# confirmation link and the endpoint stays "PendingConfirmation", delivering
# nothing, until someone clicks it. Terraform creates the subscription and then
# has no way to finish the job, so this resource is honest about being half of a
# manual step — see README.md.
#
# Counted rather than always created so `alert_email = ""` still yields a working
# topic with alarms wired to it, for the case where the address should not appear
# in a tfvars file at all and is subscribed by hand instead.
resource "aws_sns_topic_subscription" "alerts_email" {
  count = var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# What counts as an error in the log.
#
# `?` makes each term an alternative, so this is an OR across four patterns and
# not an AND across four:
#
#   ERROR, CRITICAL          the application's own records, now that they carry
#                            a level, plus Lambda's own "[ERROR]" platform lines
#   Task timed out           the function hit its timeout mid-request
#   Runtime exited with      the process died — OOM kill, or an init that raised
#
# `default_value = "0"` is the load-bearing setting. Without it the metric only
# exists in minutes that contained an error, so it is *missing* the rest of the
# time and the alarm spends its life in INSUFFICIENT_DATA, transitioning on the
# arrival and departure of data rather than on the error count. Emitting a zero
# for every non-matching event gives the alarm a continuous series to evaluate.
resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each = local.functions

  name           = "${local.name}-${each.key}-errors"
  log_group_name = aws_cloudwatch_log_group.functions[each.key].name
  pattern        = "?ERROR ?CRITICAL ?\"Task timed out\" ?\"Runtime exited with error\""

  metric_transformation {
    name          = each.value.metric_name
    namespace     = local.error_metric_namespace[each.key]
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

moved {
  from = aws_cloudwatch_log_metric_filter.api_errors
  to   = aws_cloudwatch_log_metric_filter.errors["api"]
}

# Every alarm below fires on a single breaching datapoint, which departs from
# the usual advice to require 2 or 3 of 5. That advice is written for rate
# thresholds on busy services, where one error among ten thousand invocations is
# noise to be smoothed away. Here the threshold is *zero* on a low-traffic
# deployment: an error is rare, always unexpected, and worth an email the first
# time it happens. Smoothing would only add minutes of delay.
#
# `treat_missing_data = "notBreaching"` for the same reason it always should be
# on an error count: no data means no errors, which is the OK state, not an
# unknown one. Left at the default of "missing" these alarms would report
# INSUFFICIENT_DATA through every idle period.
#
# `ok_actions` is set so recovery also mails. An alert with no all-clear leaves
# the reader unable to distinguish "resolved" from "still broken, no new errors".

resource "aws_cloudwatch_metric_alarm" "log_errors" {
  for_each = local.functions

  alarm_name        = "${local.name}-${each.key}-log-errors"
  alarm_description = "The ${each.key} function logged an error, a timeout, or a runtime exit. Read its log group to see which: see the ${each.key}_log_command output."

  namespace   = local.error_metric_namespace[each.key]
  metric_name = each.value.metric_name
  statistic   = "Sum"
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  # The alarm names its metric through locals rather than by referring to the
  # filter's attributes, because a metric is identified by name and namespace and
  # not by the resource that publishes it — so the two resources must agree on
  # strings, which is why those strings live in locals.tf and not here.
  #
  # That leaves no implicit dependency for Terraform to infer, hence the explicit
  # one. Nothing breaks without it — an alarm may name a metric that has no data
  # yet, and with notBreaching it simply reads OK — but ordering the filter first
  # means the alarm is never briefly watching a metric nothing publishes.
  depends_on = [aws_cloudwatch_log_metric_filter.errors]

  tags = { Name = "${local.name}-${each.key}-log-errors" }
}

moved {
  from = aws_cloudwatch_metric_alarm.api_log_errors
  to   = aws_cloudwatch_metric_alarm.log_errors["api"]
}

# The dimension is what scopes these to one function rather than to every
# function in the account. Omitting it would aggregate across all of them, and
# on a shared account that reads as this deployment failing when something
# unrelated does.
resource "aws_cloudwatch_metric_alarm" "invocation_errors" {
  for_each = local.functions

  alarm_name        = "${local.name}-${each.key}-invocation-errors"
  alarm_description = "Lambda counted a failed invocation of the ${each.key} function — an OOM kill, a failed init or a timeout, none of which reach the log filter."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions  = { FunctionName = each.value.name }
  statistic   = "Sum"
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = { Name = "${local.name}-${each.key}-invocation-errors" }
}

moved {
  from = aws_cloudwatch_metric_alarm.api_invocation_errors
  to   = aws_cloudwatch_metric_alarm.invocation_errors["api"]
}

# ── the asynchronous path's own failures ──────────────────────────────────────

# A job Lambda accepted and then threw away.
#
# This alarm is the price of not running a queue. Lambda's event queue retries a
# throttled delivery with backoff and then, past worker_event_age_seconds,
# discards the event — and a discarded event never reaches the application, so
# nothing logs it and no invocation fails. The client learns only that its job
# passed its deadline, which looks identical to a worker that crashed.
#
# Without this metric, "Lambda's queue can silently drop work" would be a stated
# risk with no way to tell whether it is happening. With it, the risk is
# observable, and a run of these is the signal to raise worker_event_age_seconds
# or to put a real queue back in front.
resource "aws_cloudwatch_metric_alarm" "worker_events_dropped" {
  alarm_name        = "${local.name}-worker-events-dropped"
  alarm_description = "Lambda discarded a job without running it — retries exhausted or the event aged out. The client saw a deadline failure and nothing was logged. Consider raising worker_event_age_seconds."

  namespace   = "AWS/Lambda"
  metric_name = "AsyncEventsDropped"
  dimensions  = { FunctionName = local.functions.worker.name }
  statistic   = "Sum"
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = { Name = "${local.name}-worker-events-dropped" }
}

# The alerting itself failing.
#
# A failure record is written by Lambda using the worker's execution role, and
# both of the ways that can fail are silent: a missing sqs:SendMessage grant, or
# a record too large for its destination. Either leaves the archive empty at
# precisely the moment it should hold something, with this metric as the only
# evidence. It is a small alarm guarding a large assumption.
resource "aws_cloudwatch_metric_alarm" "worker_destination_failures" {
  alarm_name        = "${local.name}-worker-destination-failures"
  alarm_description = "Lambda could not write a failed job to the failure queue — most likely the worker role is missing sqs:SendMessage, or the record exceeded the destination's size limit. Failed jobs are being lost."

  namespace   = "AWS/Lambda"
  metric_name = "DestinationDeliveryFailures"
  dimensions  = { FunctionName = local.functions.worker.name }
  statistic   = "Sum"
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = { Name = "${local.name}-worker-destination-failures" }
}

# A failed job waiting to be read.
#
# Nothing consumes the failure queue, so anything on it stays until it is read
# by hand or expires. Depth is therefore a backlog of failures nobody has looked
# at, and unlike the alarms above it does not clear on its own — which is the
# intended behaviour: the all-clear should mean "someone dealt with it".
resource "aws_cloudwatch_metric_alarm" "worker_failures_waiting" {
  alarm_name        = "${local.name}-worker-failures-waiting"
  alarm_description = "A job failed and its record is on the failure queue. Read it with the worker_failures_read_command output; it holds the request that failed, so it is also the fastest way to reproduce one."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = aws_sqs_queue.worker_failures.name }
  statistic   = "Maximum"

  # Five minutes rather than one. SQS publishes these metrics at five-minute
  # intervals, so a 60-second period would be empty four minutes in five and the
  # alarm would flap between OK and its threshold on the gaps rather than on the
  # queue.
  period = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = { Name = "${local.name}-worker-failures-waiting" }
}
