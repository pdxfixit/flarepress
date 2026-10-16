// flarepress events: a Worker that does nothing but take event batches, for an app with no site (a watch app).
//
// It answers POST /api/events through events-worker.js and 404s every other path. A native app sends no Origin, so
// each request must carry the shared key: the X-Events-Key header must equal the Worker secret EVENTS_KEY
// (`wrangler secret put EVENTS_KEY`). The key is a constant inside the app, so it keeps out casual traffic, not a
// determined sender; the job checks every line. EVENTS_ORIGINS (a var, comma-separated) also lets those browser
// origins post, and is empty by default.
//
// Its wrangler.toml (in the app's repo, AGENTS.md "Telemetry events"):
//   main = "../telemetry/events-standalone.js"     # beside events-worker.js
//   [[r2_buckets]]
//   binding = "EVENTS"
//   bucket_name = "<app>-events"
// Vendor it with `just events-vendor --standalone DIR` in flarepress, which writes both files there.

import { handleEvents } from "./events-worker.js";

export function standaloneOpts(env) {
  const origins = String(env.EVENTS_ORIGINS ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return { bucket: "EVENTS", origins, key: { header: "x-events-key", secret: "EVENTS_KEY" } };
}

export async function handle(request, env, now) {
  const url = new URL(request.url);
  if (url.pathname !== "/api/events") {
    return new Response(null, { status: 404, headers: { "cache-control": "no-store" } });
  }
  return handleEvents(request, env, { ...standaloneOpts(env), now });
}

export default {
  fetch: (request, env) => handle(request, env),
};
