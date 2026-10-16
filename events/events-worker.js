// flarepress events: the Worker side of the shared telemetry wire format (POST /api/events).
//
// One function, handleEvents(request, env, opts), that an app's own Worker calls for POST /api/events. It checks the
// request and writes the body to R2 as it came, under v1/<UTC day>/<batch>.jsonl, then answers 204. It never parses
// the body (the free plan allows 10 ms of CPU a request), never reads or stores the IP address, and logs nothing.
// The telemetry runner's job (tools/events_job.py) reads the bucket, checks each line and deletes what it stored.
//
//   import { handleEvents } from "./events-worker.js";
//   if (url.pathname === "/api/events") {
//     return handleEvents(request, env, { bucket: "EVENTS", origins: ["https://example.com"] });
//   }
//
// opts:
//   bucket   the R2 binding's name in wrangler.toml (required), e.g. "EVENTS"
//   origins  the Origin values a browser may send from: the site's own origin, exactly ("https://example.com")
//   key      { header, secret } for a native app, which sends no Origin: the request's `header` must equal the
//            Worker secret named `secret` (`wrangler secret put <secret>`). An empty or unset secret allows nothing.
//   now      a Date, for tests
// A request passes when its Origin is in `origins`, or when it carries the key. Neither set: every request is refused.
//
// The batch id is the X-Batch header, a UUID. navigator.sendBeacon cannot set headers, so the client's beacon puts
// it in the query instead (?batch=<uuid>); both are read, and when both are sent they must agree.
//
// Answers: 204 stored; 400 no batch id or an empty body; 403 origin or key refused; 405 not POST; 413 over 64 KB;
// 415 not text/plain; 503 R2 failed (the client keeps the batch and sends it again with the same id).
// Vendor this file into an app with `just events-vendor` in flarepress (AGENTS.md "Telemetry events").

export const EVENTS_WIRE = 1;
export const MAX_BYTES = 64 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function reply(status, extra) {
  return new Response(null, { status, headers: { "cache-control": "no-store", ...extra } });
}

// Equal strings, compared in time that does not depend on where they differ.
function same(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function allowed(request, env, opts) {
  const origin = request.headers.get("origin");
  if (origin && Array.isArray(opts.origins) && opts.origins.includes(origin)) return true;
  const key = opts.key;
  if (key && key.header && key.secret) {
    const want = env[key.secret];
    if (typeof want === "string" && want.length > 0 && same(request.headers.get(key.header) ?? "", want)) return true;
  }
  return false;
}

// The batch id from X-Batch or ?batch=, lower-cased; null when missing, malformed, or the two disagree.
export function batchOf(request) {
  const header = (request.headers.get("x-batch") ?? "").toLowerCase();
  const query = (new URL(request.url).searchParams.get("batch") ?? "").toLowerCase();
  if (header && query && header !== query) return null;
  const id = header || query;
  return UUID.test(id) ? id : null;
}

// The object's name: v1/<day the Worker got it, UTC>/<batch>.jsonl. A retry the same day overwrites itself; one
// across midnight lands under the next day, and the job keeps a batch once by its id.
export function objectKey(now, batch) {
  return `v1/${now.toISOString().slice(0, 10)}/${batch}.jsonl`;
}

// Reads at most MAX_BYTES of the body; null when it is longer, whatever Content-Length said.
async function readCapped(body) {
  if (!body) return new Uint8Array(0);
  const reader = body.getReader();
  const parts = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BYTES) {
      await reader.cancel();
      return null;
    }
    parts.push(value);
  }
  const out = new Uint8Array(size);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.byteLength;
  }
  return out;
}

export async function handleEvents(request, env, opts = {}) {
  if (request.method !== "POST") return reply(405, { allow: "POST" });
  const bucket = env[opts.bucket];
  if (!bucket) return reply(503); // the binding is missing from wrangler.toml: say so without crashing
  if (!allowed(request, env, opts)) return reply(403);

  const type = (request.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
  if (type !== "text/plain") return reply(415);
  const declared = request.headers.get("content-length");
  if (declared !== null && Number(declared) > MAX_BYTES) return reply(413);

  const batch = batchOf(request);
  if (!batch) return reply(400);

  const body = await readCapped(request.body);
  if (body === null) return reply(413);
  if (body.byteLength === 0) return reply(400);

  try {
    await bucket.put(objectKey(opts.now ?? new Date(), batch), body, {
      httpMetadata: { contentType: "application/x-ndjson" },
    });
  } catch {
    return reply(503);
  }
  return reply(204);
}
