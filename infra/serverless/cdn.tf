# The distribution — the single origin the browser talks to.
#
# Two origins behind one hostname, split by path: the bundle from S3, /api/* from
# the Function URL. That is what lets the frontend keep posting to the relative
# path /api/chat and hold no API base URL in any environment, which is the same
# property deploy/caddy/Caddyfile provides on the EC2 target. It also means the
# browser's requests are same-origin, so CORS stays switched off entirely.
#
# ── History worth keeping ─────────────────────────────────────────────────────
#
# The /api/* behaviour below was verified before the S3 half existed, as the
# distribution's *default* behaviour, with CHAT_REPLY_SOURCE=canned. It measured
# a 60.6 ms median gap between SSE frames against a configured 0.06 s cadence —
# so CloudFront forwards a chunked response to the viewer as it arrives rather
# than buffering it. Its arguments are unchanged since that measurement; only its
# position moved from `default_cache_behavior` to an ordered one. Treat every
# argument in it as load-bearing and measured, not chosen.

# Managed policies by name rather than by their well-known UUIDs. The IDs are
# stable, but `Managed-CachingDisabled` says what it does and a UUID does not.
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host_header" {
  name = "Managed-AllViewerExceptHostHeader"
}

data "aws_cloudfront_response_headers_policy" "security_headers" {
  name = "Managed-SecurityHeadersPolicy"
}

# Two OACs, because the signing rules differ by origin type and one cannot serve
# both. Both sign every request, which is what makes each origin private: S3
# blocks all public access, and the Function URL requires AWS_IAM.
resource "aws_cloudfront_origin_access_control" "api" {
  name                              = "${local.name}-api"
  description                       = "OAC for the ${local.name} api Function URL."
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${local.name}-site"
  description                       = "OAC for the ${local.name} bundle bucket."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Client-side routing, at the edge. See functions/spa-router.js for why the rule
# is expressed as it is and why /assets/ is excluded explicitly.
resource "aws_cloudfront_function" "spa_router" {
  name    = "${local.name}-spa-router"
  runtime = "cloudfront-js-2.0"
  comment = "Rewrites application routes to /index.html, leaving real files alone."
  publish = true
  code    = file("${path.module}/functions/spa-router.js")
}

resource "aws_cloudfront_distribution" "site" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${local.name} — serverless target"
  price_class     = var.price_class

  # So a request for / returns the app rather than S3's bucket listing denial.
  # The SPA function handles every other route-shaped path.
  default_root_object = "index.html"

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

  origin {
    origin_id = "site"

    # The *regional* domain name. The global form (bucket.s3.amazonaws.com) can
    # answer a fresh bucket with a 307 redirect to the regional endpoint, which
    # CloudFront caches and which then breaks OAC signing.
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id

    # No s3_origin_config and no origin_access_identity: those belong to the
    # legacy OAI mechanism, and setting either alongside an OAC is how a
    # distribution ends up authenticating two ways and succeeding at neither.
  }

  # ── the bundle ──────────────────────────────────────────────────────────────
  default_cache_behavior {
    target_origin_id = "site"

    # Safe here, unlike on the API behaviour: a static asset is a GET, so there
    # is no request body for a 301 to discard.
    viewer_protocol_policy = "redirect-to-https"

    allowed_methods = ["GET", "HEAD", "OPTIONS"]
    cached_methods  = ["GET", "HEAD"]

    # The bundle is what compression is for. release-web.sh gives assets a
    # one-year immutable cache and index.html no-cache, so the cache policy's TTLs
    # defer to those headers rather than fighting them.
    compress        = true
    cache_policy_id = data.aws_cloudfront_cache_policy.caching_optimized.id

    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security_headers.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_router.arn
    }
  }

  # ── the API ─────────────────────────────────────────────────────────────────
  # Every argument here is load-bearing and was verified by measurement; see the
  # note at the top of this file. One matcher covers /api/chat, /api/health and
  # /api/docs, exactly as the Caddyfile's `handle /api/*` does.
  ordered_cache_behavior {
    path_pattern     = "/api/*"
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
    # so that OAC can sign a POST body (Lambda does not accept UNSIGNED-PAYLOAD);
    # without that header a POST is rejected with 403, which was confirmed by
    # removing it.
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host_header.id

    # No function_association. The SPA rewrite must not touch API paths, or an
    # unknown one would return the HTML shell instead of FastAPI's 404.
  }

  # No custom_error_response anywhere in this resource, and that is a decision
  # rather than an omission. It is distribution-level rather than per-behaviour,
  # so mapping 404 to /index.html would also rewrite the API's errors — and it is
  # unnecessary, because the SPA function already resolves route-shaped paths and
  # the bucket policy's ListBucket grant makes a genuine asset miss a real 404.

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
