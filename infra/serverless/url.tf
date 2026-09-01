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
resource "aws_lambda_permission" "cloudfront" {
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
