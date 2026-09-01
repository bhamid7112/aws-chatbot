# The function's HTTP front door.
#
# A Function URL, and not API Gateway, because of one requirement: the reply is
# streamed. A REST API buffers the whole response body before returning it, and
# an HTTP API has a hard 30-second integration limit with no response streaming
# at all. A Function URL with RESPONSE_STREAM is the only serverless HTTP entry
# point that can carry Server-Sent Events, which makes this choice a constraint
# rather than a preference.

resource "aws_lambda_function_url" "api" {
  function_name = aws_lambda_function.api.function_name

  # Required by Origin Access Control: OAC signs the origin request with SigV4,
  # which only means something if the URL actually checks the signature. NONE
  # would leave the function open to anyone who learns the URL, since a Function
  # URL is public DNS with no network boundary in front of it.
  authorization_type = "AWS_IAM"

  # Must agree with AWS_LWA_INVOKE_MODE in locals.tf. With BUFFERED — the
  # default — Lambda accumulates the entire reply and returns it in one piece,
  # which is indistinguishable from CloudFront buffering when debugging from the
  # outside. Both settings are written near their justification for that reason.
  invoke_mode = "RESPONSE_STREAM"

  # No cors block. CloudFront is the only permitted caller, and it serves the
  # bundle from the same origin, so the browser's requests are same-origin and
  # there is no preflight to answer. This is the same reason
  # CHAT_CORS_ALLOW_ORIGINS is empty.
}

# What allows CloudFront — and nothing else — to invoke the URL.
#
# The principal is a service, so the grant is bounded by SourceArn instead: only
# *this* distribution, not any CloudFront distribution in any account. Without
# that condition the statement would permit anyone able to create a distribution
# to point it at this function.
#
# ── Why there are two statements ──────────────────────────────────────────────
#
# A RESPONSE_STREAM function URL needs *both* lambda:InvokeFunctionUrl and
# lambda:InvokeFunction. This is specific to response streaming — a buffered URL
# works with InvokeFunctionUrl alone — and AWS states it plainly: "To enable a
# function URL for a response streaming Lambda function, you must first add a
# resource-based policy to grant lambda:InvokeFunctionUrl and
# lambda:InvokeFunction permissions."
#
# Verified the hard way. With only the first statement, every request through
# CloudFront returned 403 with Lambda's function-URL authorization message, while
# the OAC, the URL config and the policy all looked correct in the console. There
# is nothing in the error to suggest a second action is missing.
# Renames the existing statement in state instead of replacing it. Without this,
# Terraform would see an unfamiliar address, create the new permission *before*
# destroying the old one, and fail: both carry the same statement_id, and Lambda
# rejects a duplicate with ResourceConflictException.
#
# Safe to delete once every deployment has applied it — which, with one operator
# and one environment, is after the next apply.
moved {
  from = aws_lambda_permission.cloudfront
  to   = aws_lambda_permission.cloudfront_invoke_url
}

resource "aws_lambda_permission" "cloudfront_invoke_url" {
  statement_id  = "AllowCloudFrontInvokeFunctionUrl"
  action        = "lambda:InvokeFunctionUrl"
  function_name = aws_lambda_function.api.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.site.arn

  # Must match the URL's authorization_type. It scopes the permission to
  # IAM-authenticated URL invocations, so this statement cannot be reused if the
  # URL is ever switched to NONE.
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_invoke" {
  statement_id  = "AllowCloudFrontInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.site.arn

  # Deliberately *no* function_url_auth_type here, unlike the statement above.
  # That argument adds a lambda:FunctionUrlAuthType condition, and this action is
  # not necessarily evaluated with that key in the request context — a condition
  # on an absent key denies, which would leave the 403 in place and look like the
  # permission had not been added at all.
  #
  # The grant stays tightly bounded regardless: SourceArn admits exactly this one
  # distribution, so the only caller this statement enables is the CloudFront
  # distribution defined in cdn.tf.
}
