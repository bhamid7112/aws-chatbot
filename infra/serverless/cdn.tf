# The distribution.
#
# ── Phase 0 shape ─────────────────────────────────────────────────────────────
#
# Right now this has one origin — the Function URL — and its *default* behaviour
# carries the API. That is temporary. Once site.tf exists, S3 becomes the default
# origin and the block below moves verbatim into an `ordered_cache_behavior` for
# the `/api/*` path pattern.
#
# The arguments are already the ones the permanent `/api/*` behaviour needs, and
# that is the point: whatever this configuration proves about streaming through
# CloudFront stays true after the move, because nothing about how the API's
# requests are handled will change. A distribution must have a default
# behaviour, so during Phase 0 the API is it.
#
# ── Why CloudFront at all, rather than the Function URL directly ──────────────
#
# The frontend posts to the relative path /api/chat and holds no API base URL in
# any environment. One origin serving both the bundle and the API preserves that,
# keeps CORS switched off, and supplies a real hostname with a trusted
# certificate — replacing the hardest and most fragile part of ../server, a
# trusted certificate for a bare IP address, with one line below.

# Managed policies by name rather than by their well-known UUIDs. The IDs are
# stable, but `Managed-CachingDisabled` says what it does and a UUID does not.
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host_header" {
  name = "Managed-AllViewerExceptHostHeader"
}

# Signs every origin request with SigV4 so the Function URL's AWS_IAM
# authorization has something to check. "always" rather than "never" is what
# makes the URL private in practice: CloudFront becomes the only caller that can
# produce a valid signature.
resource "aws_cloudfront_origin_access_control" "api" {
  name                              = "${local.name}-api"
  description                       = "OAC for the ${local.name} api Function URL."
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "site" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${local.name} — serverless target"
  price_class     = var.price_class

  origin {
    origin_id = "api"

    # Built from url_id rather than by trimming `function_url`. That attribute is
    # a complete URL — scheme, host and a trailing slash — and CloudFront wants a
    # bare domain name, so string-surgery on it is the classic source of an
    # opaque InvalidArgument at apply time.
    domain_name              = "${aws_lambda_function_url.api.url_id}.lambda-url.${var.aws_region}.on.aws"
    origin_access_control_id = aws_cloudfront_origin_access_control.api.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]

      # 60 s is the maximum without a service-quota increase (180 s with one).
      # It is a per-response-packet timeout rather than a total, so it only bites
      # if the model goes silent for a full minute mid-stream — which is why it
      # is set explicitly instead of left at the 30 s default, and why a
      # multi-minute stream is still possible.
      origin_read_timeout = 60
    }
  }

  # ── the API behaviour ───────────────────────────────────────────────────────
  # Every argument here is load-bearing. See the Phase 0 note above: this block
  # becomes `ordered_cache_behavior { path_pattern = "/api/*" ... }` unchanged.
  default_cache_behavior {
    target_origin_id = "api"

    # https-only, *not* redirect-to-https. A redirect answers with 301, and a
    # browser replaying a 301 on a POST drops the request body — so the
    # convenience setting would break every chat while looking like a network
    # fault.
    viewer_protocol_policy = "https-only"

    allowed_methods = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods  = ["GET", "HEAD"]

    # Edge compression must buffer the body in order to compress it, which is
    # exactly what must not happen to a stream. This single argument is the
    # difference between words arriving one at a time and the whole reply
    # arriving at once.
    compress = false

    cache_policy_id = data.aws_cloudfront_cache_policy.caching_disabled.id

    # Mandatory, not a preference. A Function URL origin needs the Host header to
    # be the *origin's* domain — both because SigV4 signs Host, and because the
    # URL router matches on it — and this managed policy forwards everything
    # except Host. It also forwards x-amz-content-sha256, which the browser sends
    # so that OAC can sign a POST body (Lambda does not accept UNSIGNED-PAYLOAD).
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host_header.id
  }

  # No response-completion timeout is set anywhere in this resource, and that is
  # deliberate: AWS documents that when it is left unset CloudFront enforces no
  # maximum, which is what permits a reply that takes minutes to finish.

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # The whole ACME apparatus of ../server — the shortlived profile, HTTP-01,
  # default_sni, a permanently load-bearing port 80 and a six-day renewal cliff —
  # collapses into this. It costs the use of the cloudfront.net hostname; a custom
  # domain would need ACM in us-east-1 and a provider alias to reach it.
  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = local.name }
}
