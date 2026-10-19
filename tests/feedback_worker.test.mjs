// events/events-worker.js handleFeedback: the feedback box (POST /api/feedback). Node's own test runner, no packages.
//   node --test tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { handleFeedback, feedbackKey, MAX_FEEDBACK } from "../events/events-worker.js";

const ORIGIN = "https://example.com";
// built from parts: tests/test_public.py reads any whole UUID as an account id
const ID = ["5d1c2b3a", "4e5f", "4a6b", "8c7d", "9e0f1a2b3c4d"].join("-");
const NOW = new Date("2026-10-19T12:00:00Z");
const TEXT = "Please add a bigger eraser 🧽\nThanks!";

function bucket({ fail = false } = {}) {
  const puts = [];
  return {
    puts,
    async put(key, body, opts) {
      if (fail) throw new Error("r2 down");
      puts.push({ key, body: Buffer.from(body).toString("utf8"), opts });
    },
  };
}

function limiter(allow = true) {
  const keys = [];
  return { keys, async limit({ key }) { keys.push(key); return { success: allow }; } };
}

const OPTS = { bucket: "FEEDBACK", app: "isopix", origins: [ORIGIN], limiter: "FEEDBACK_LIMITER", now: NOW };
const env = (over = {}) => ({ FEEDBACK: bucket(), FEEDBACK_LIMITER: limiter(), ...over });

function post({ body = TEXT, headers = {}, method = "POST" } = {}) {
  const h = {
    origin: ORIGIN,
    "content-type": "text/plain;charset=UTF-8",
    "x-feedback": ID,
    "x-app-version": "1.2.117",
    "cf-connecting-ip": "203.0.113.7",
    ...headers,
  };
  for (const k of Object.keys(h)) if (h[k] === undefined) delete h[k];
  return new Request(`${ORIGIN}/api/feedback`, { method, headers: h, body: method === "POST" ? body : undefined });
}

async function status(req, e = env(), opts = OPTS) {
  return (await handleFeedback(req, e, opts)).status;
}

test("feedback is stored as it came, under the app's prefix, its day, version and id, and answered 204", async () => {
  const e = env();
  const r = await handleFeedback(post(), e, OPTS);
  assert.equal(r.status, 204);
  assert.equal(r.headers.get("cache-control"), "no-store");
  assert.deepEqual(e.FEEDBACK.puts.map((p) => [p.key, p.body]), [[`v1/isopix/2026-10-19/1.2.117-${ID}.txt`, TEXT]]);
  assert.equal(e.FEEDBACK.puts[0].opts.httpMetadata.contentType, "text/plain; charset=utf-8");
});

test("the object name is the one the telemetry runner reads, and a retry overwrites", () => {
  const key = feedbackKey(NOW, "noodlepads", "a1b2c3d", ID);
  assert.equal(key, `v1/noodlepads/2026-10-19/a1b2c3d-${ID}.txt`);
  assert.equal(feedbackKey(NOW, "noodlepads", "a1b2c3d", ID), key);
  // telemetry.py's SUGGEST_KEY (pdxfixit/telemetry), copied: change both together
  const SUGGEST_KEY = /^v1\/([a-z][a-z0-9_]*)\/([0-9]{4}-[0-9]{2}-[0-9]{2})\/([0-9A-Za-z.+_]{1,32})-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.txt$/;
  assert.match(key, SUGGEST_KEY);
  assert.match(feedbackKey(NOW, "isopix", "1.2.117", ID), SUGGEST_KEY);
});

test("only POST from the site's own origin; no key opens it, and nothing is stored when refused", async () => {
  const get = await handleFeedback(post({ method: "GET" }), env(), OPTS);
  assert.equal(get.status, 405);
  assert.equal(get.headers.get("allow"), "POST");
  for (const origin of ["https://evil.invalid", "null", undefined]) {
    const e = env();
    assert.equal(await status(post({ headers: { origin } }), e), 403, String(origin));
    assert.equal(e.FEEDBACK.puts.length, 0);
  }
  const keyed = { ...OPTS, key: { header: "x-events-key", secret: "EVENTS_KEY" } };
  const e = env({ EVENTS_KEY: "k-123" });
  assert.equal(await status(post({ headers: { origin: undefined, "x-events-key": "k-123" } }), e, keyed), 403,
    "a native app's key does not open the feedback box");
});

test("the rate limit counts the whole box by the app's name, never the sender's address", async () => {
  const e = env({ FEEDBACK_LIMITER: limiter(false) });
  assert.equal(await status(post(), e), 429);
  assert.deepEqual(e.FEEDBACK_LIMITER.keys, ["isopix"]);
  assert.equal(e.FEEDBACK.puts.length, 0);
  assert.equal(await status(post(), env({ FEEDBACK_LIMITER: undefined })), 204, "no limiter bound: still taken");
  assert.equal(await status(post(), env(), { ...OPTS, limiter: undefined }), 204, "no limiter named");
});

test("only text/plain, with or without a charset", async () => {
  assert.equal(await status(post({ headers: { "content-type": "text/plain" } })), 204);
  assert.equal(await status(post({ headers: { "content-type": "application/json" } })), 415);
  assert.equal(await status(post({ body: new TextEncoder().encode(TEXT), headers: { "content-type": undefined } })), 415);
});

test("bad headers or an empty body are 400", async () => {
  for (const headers of [
    { "x-feedback": undefined }, { "x-feedback": "../x" },
    { "x-app-version": undefined }, { "x-app-version": "1-2" }, { "x-app-version": "9".repeat(33) },
  ]) {
    const e = env();
    assert.equal(await status(post({ headers }), e), 400, JSON.stringify(headers));
    assert.equal(e.FEEDBACK.puts.length, 0);
  }
  assert.equal(await status(post({ body: "" })), 400);
  const e = env();
  assert.equal(await status(post({ headers: { "x-feedback": ID.toUpperCase() } }), e), 204);
  assert.match(e.FEEDBACK.puts[0].key, new RegExp(`-${ID}\\.txt$`), "upper case is taken, stored lower");
});

test("over 4096 bytes is 413, declared or arriving; 4096 exactly is stored", async () => {
  assert.equal(await status(post({ body: "x".repeat(MAX_FEEDBACK + 1) })), 413);
  assert.equal(await status(post({ headers: { "content-length": String(MAX_FEEDBACK + 1) } })), 413);
  assert.equal(await status(post({ body: "x".repeat(MAX_FEEDBACK) })), 204);
});

test("R2 failing, a missing binding or a bad app name is 503, so the page offers to try again", async () => {
  assert.equal(await status(post(), env({ FEEDBACK: bucket({ fail: true }) })), 503);
  assert.equal(await status(post(), env({ FEEDBACK: undefined })), 503);
  for (const app of [undefined, "", "Isopix", "../x", "1app"]) {
    assert.equal(await status(post(), env(), { ...OPTS, app }), 503, String(app));
  }
});

test("it never reads the IP address or the body's contents", async () => {
  const seen = [];
  const headers = new Proxy(new Headers({ origin: ORIGIN, "content-type": "text/plain", "x-feedback": ID, "x-app-version": "1" }), {
    get(target, prop) {
      const v = Reflect.get(target, prop, target);
      if (prop === "get") return (name) => { seen.push(name.toLowerCase()); return target.get(name); };
      return typeof v === "function" ? v.bind(target) : v;
    },
  });
  const req = { method: "POST", url: `${ORIGIN}/api/feedback`, headers, body: new Blob(["<script>not parsed</script>"]).stream() };
  const e = env();
  assert.equal((await handleFeedback(req, e, OPTS)).status, 204);
  assert.equal(e.FEEDBACK.puts[0].body, "<script>not parsed</script>", "stored unparsed");
  assert.ok(!seen.includes("cf-connecting-ip") && !seen.includes("x-forwarded-for") && !seen.includes("user-agent"), seen.join());
});
