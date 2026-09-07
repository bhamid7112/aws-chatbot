# Error alerting: one topic, one email subscription, two alarms.
#
#   log group ──> metric filter (ApiErrorCount) ──> alarm ──┐
#                                                           ├──> SNS ──> email
#           AWS/Lambda Errors ────────────────────> alarm ──┘
#
# Two alarms rather than one because they see different failures, and neither
# subsumes the other:
#
#   * The log filter catches what the *application* reports — a Bedrock call the
#     adapter could not complete, an unhandled exception in a route. Lambda
#     considers those invocations successful, because the function returned a
#     response; its Errors metric stays flat and only the log knows.
#   * The Errors metric catches what kills the process before it can write
#     anything worth matching: an OOM kill, a failed init, a timeout. There is
#     no log line to filter because there was no chance to log one.
#
# The log-filter half depends on the application emitting a level with each
# record, which it does only because backend/app/infrastructure/logging.py
# installs a handler. Left to Python's fallback the message arrives with no
# level in it, matches nothing, and this whole file goes quiet without failing.
# That coupling is asserted in backend/tests/test_logging.py rather than left as
# a comment.
#
# Cost: one custom metric and two alarms, roughly $0.70/month at current
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
#   Task timed out           the function hit timeout_seconds mid-request
#   Runtime exited with      the process died — OOM kill, or an init that raised
#
# `default_value = "0"` is the load-bearing setting. Without it the metric only
# exists in minutes that contained an error, so it is *missing* the rest of the
# time and the alarm spends its life in INSUFFICIENT_DATA, transitioning on the
# arrival and departure of data rather than on the error count. Emitting a zero
# for every non-matching event gives the alarm a continuous series to evaluate.
resource "aws_cloudwatch_log_metric_filter" "api_errors" {
  name           = "${local.name}-api-errors"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "?ERROR ?CRITICAL ?\"Task timed out\" ?\"Runtime exited with error\""

  metric_transformation {
    name          = local.error_metric_name
    namespace     = local.error_metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# Both alarms below fire on a single breaching datapoint, which departs from the
# usual advice to require 2 or 3 of 5. That advice is written for rate
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

resource "aws_cloudwatch_metric_alarm" "api_log_errors" {
  alarm_name        = "${local.name}-api-log-errors"
  alarm_description = "The api function logged an error, a timeout, or a runtime exit. Read the log group to see which: see the api_log_command output."

  namespace   = local.error_metric_namespace
  metric_name = local.error_metric_name
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
  depends_on = [aws_cloudwatch_log_metric_filter.api_errors]

  tags = { Name = "${local.name}-api-log-errors" }
}

# The dimension is what scopes this to one function rather than to every
# function in the account. Omitting it would aggregate across all of them, and
# on a shared account that reads as this deployment failing when something
# unrelated does.
resource "aws_cloudwatch_metric_alarm" "api_invocation_errors" {
  alarm_name        = "${local.name}-api-invocation-errors"
  alarm_description = "Lambda counted a failed invocation of the api function — an OOM kill, a failed init or a timeout, none of which reach the log filter."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions  = { FunctionName = aws_lambda_function.api.function_name }
  statistic   = "Sum"
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = { Name = "${local.name}-api-invocation-errors" }
}
