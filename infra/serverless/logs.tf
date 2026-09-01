# The function's log group, created here rather than by Lambda.
#
# This resource exists to win a race. If the group does not exist when the
# function first runs, Lambda creates it — with no retention, so logs accumulate
# forever and become the largest line item on an otherwise nearly-free
# deployment. Creating it first, with retention set, and naming it explicitly in
# the function's logging_config means Lambda never gets the chance.
#
# It is also why the execution role grants CreateLogStream and PutLogEvents but
# *not* CreateLogGroup (see iam.tf): the function has no legitimate reason to
# create a group, and denying it means a misconfigured log group name fails
# loudly instead of silently producing a second, retention-less group.
#
# The name is not free-form. Lambda writes to /aws/lambda/<function-name>, so
# local.log_group_name and the function's name are two views of one fact.

resource "aws_cloudwatch_log_group" "api" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days

  tags = { Name = local.log_group_name }
}
