// Client-side routing at the edge — the CloudFront equivalent of the @spa
// matcher in deploy/caddy/Caddyfile.
//
// Caddy's rule is `not path /assets/* && not file` → rewrite to /index.html: an
// unknown path is a route for the app to resolve, not a file that is missing.
// A CloudFront function cannot express the `not file` half, because it runs
// before the origin request and has no way to ask S3 whether a key exists.
//
// So the "is this a file?" test is approximated structurally, by looking for an
// extension. That approximation is sound in one direction and deliberately
// conservative in the other:
//
//   /chat, /some/deep/path   no extension  → rewritten, the app routes it
//   /assets/index-abc123.js  under /assets → never rewritten
//   /favicon.ico             has extension → not rewritten, 404 if absent
//
// The /assets/ exclusion is not redundant with the extension test — it is the
// one that carries the safety property. Content-hashed build output lives there,
// and a miss must stay a genuine 404: answering with the HTML shell makes the
// browser execute index.html as JavaScript and fail with a syntax error that
// points nowhere near the cause. That is precisely what a redeploy does to a tab
// still fetching a chunk from the previous build, so it is a real case rather
// than a hypothetical one. Keeping both tests means a future edit to the
// extension heuristic cannot silently reintroduce that failure.
//
// This is attached to the default (S3) behaviour only. /api/* is a separate
// cache behaviour and never reaches this code, so an unknown API path keeps
// returning FastAPI's own 404 rather than the HTML shell.

var ASSETS_PREFIX = '/assets/'
var HAS_EXTENSION = /\.[^/]+$/

function handler(event) {
    var request = event.request
    var uri = request.uri

    // Serving a real file: content-hashed assets, favicon, robots.txt.
    if (uri.startsWith(ASSETS_PREFIX) || HAS_EXTENSION.test(uri)) {
        return request
    }

    // Anything else is an application route. index.html is served with
    // no-cache (see scripts/release-web.sh), so a rewritten path always
    // resolves against the current build rather than a stale shell.
    request.uri = '/index.html'
    return request
}
