# Each function's log group, created here rather than by Lambda.
#
# These resources exist to win a race. If a group does not exist when its
# function first runs, Lambda creates it — with no retention, so logs accumulate
# forever and become the largest line item on an otherwise nearly-free
# deployment. Creating them first, with retention set, and naming them
# explicitly in each function's logging_config means Lambda never gets the
# chance.
#
# It is also why the execution roles grant CreateLogStream and PutLogEvents but
# *not* CreateLogGroup (see iam.tf): a function has no legitimate reason to
# create a group, and denying it means a misconfigured log group name fails
# loudly instead of silently producing a second, retention-less group.
#
# The names are not free-form. Lambda writes to /aws/lambda/<function-name>, so
# these names and the function names are two views of one fact — which is why
# they are derived from local.functions rather than written again.

resource "aws_cloudwatch_log_group" "functions" {
  for_each = local.functions

  name              = "/aws/lambda/${each.value.name}"
  retention_in_days = var.log_retention_days

  tags = { Name = "/aws/lambda/${each.value.name}" }
}

# The api function's group predates the worker and was a singleton resource. Its
# *name* is unchanged, so this is purely an address change in state — without
# this block Terraform would destroy the group and create an identical one,
# throwing away the logs in between.
moved {
  from = aws_cloudwatch_log_group.api
  to   = aws_cloudwatch_log_group.functions["api"]
}
