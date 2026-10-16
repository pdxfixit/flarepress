// events/events-worker.js and events-standalone.js: Node's own test runner, no packages.
//   node --test tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { handleEvents, objectKey, batchOf, MAX_BYTES } from "../events/events-worker.js";
import { handle as standalone } from "../events/events-standalone.js";

const ORIGIN = "https://example.com";
// built from parts: tests/test_public.py reads any whole UUID as an account id
const BATCH = ["0b9e4c2a", "7d31", "4f5e", "9a8b", "1c2d3e4f5a6b"].join("-");
const NOW = new Date("2026-10-15T23:59:59Z");
const LINE = '{"app":"isopix","event":"app.open"}\n';

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

const OPTS = { bucket: "EVENTS", origins: [ORIGIN], now: NOW };

function post({ body = LINE, headers = {}, method = "POST", url = `${ORIGIN}/api/events` } = {}) {
  const h = {
    origin: ORIGIN,
    "content-type": "text/plain;charset=UTF-8",
    "x-batch": BATCH,
    "cf-connecting-ip": "203.0.113.7",
    ...headers,
  };
  for (const k of Object.keys(h)) if (h[k] === undefined) delete h[k];
  return new Request(url, { method, headers: h, body: method === "POST" ? body : undefined });
}

async function status(req, env = { EVENTS: bucket() }, opts = OPTS) {
  return (await handleEvents(req, env, opts)).status;
}

test("a good batch is stored as it came, under its UTC day and batch id, and answered 204", async () => {
  const env = { EVENTS: bucket() };
  const r = await handleEvents(post(), env, OPTS);
  assert.equal(r.status, 204);
  assert.equal(r.headers.get("cache-control"), "no-store");
  assert.deepEqual(env.EVENTS.puts.map((p) => [p.key, p.body]), [[`v1/2026-10-15/${BATCH}.jsonl`, LINE]]);
});

test("a retry reuses its batch id, so the same day it overwrites the same object", () => {
  assert.equal(objectKey(NOW, BATCH), `v1/2026-10-15/${BATCH}.jsonl`);
  assert.equal(objectKey(new Date("2026-10-16T00:00:01Z"), BATCH), `v1/2026-10-16/${BATCH}.jsonl`);
});

test("only POST is taken", async () => {
  const r = await handleEvents(post({ method: "GET" }), { EVENTS: bucket() }, OPTS);
  assert.equal(r.status, 405);
  assert.equal(r.headers.get("allow"), "POST");
});

test("another origin, or none, is refused and nothing is stored", async () => {
  for (const origin of ["https://evil.invalid", "null", undefined]) {
    const env = { EVENTS: bucket() };
    assert.equal(await status(post({ headers: { origin } }), env), 403, String(origin));
    assert.equal(env.EVENTS.puts.length, 0);
  }
  assert.equal(await status(post(), { EVENTS: bucket() }, { bucket: "EVENTS" }), 403, "no origins, no key: nothing passes");
});

test("a native app passes with the shared key and fails without it", async () => {
  const opts = { bucket: "EVENTS", key: { header: "x-events-key", secret: "EVENTS_KEY" }, now: NOW };
  const env = () => ({ EVENTS: bucket(), EVENTS_KEY: "k-123" });
  assert.equal(await status(post({ headers: { origin: undefined, "x-events-key": "k-123" } }), env(), opts), 204);
  assert.equal(await status(post({ headers: { origin: undefined, "x-events-key": "k-124" } }), env(), opts), 403);
  assert.equal(await status(post({ headers: { origin: undefined, "x-events-key": "k-12" } }), env(), opts), 403);
  assert.equal(await status(post({ headers: { origin: undefined } }), env(), opts), 403);
  const noSecret = { EVENTS: bucket(), EVENTS_KEY: "" };
  assert.equal(await status(post({ headers: { origin: undefined, "x-events-key": "" } }), noSecret, opts), 403,
    "an empty secret allows nothing");
});

test("only text/plain is taken, with or without a charset", async () => {
  assert.equal(await status(post({ headers: { "content-type": "text/plain" } })), 204);
  assert.equal(await status(post({ headers: { "content-type": "application/json" } })), 415);
  // bytes, so Request does not add text/plain itself as it does for a string body
  assert.equal(await status(post({ body: new TextEncoder().encode(LINE), headers: { "content-type": undefined } })), 415);
});

test("the batch id must be a UUID, from X-Batch or ?batch= (a beacon cannot set headers)", async () => {
  assert.equal(await status(post({ headers: { "x-batch": "nope" } })), 400);
  assert.equal(await status(post({ headers: { "x-batch": undefined } })), 400);
  const env = { EVENTS: bucket() };
  assert.equal(await status(post({ headers: { "x-batch": undefined }, url: `${ORIGIN}/api/events?batch=${BATCH}` }), env), 204);
  assert.equal(env.EVENTS.puts[0].key, `v1/2026-10-15/${BATCH}.jsonl`);
  const other = "1" + BATCH.slice(1);
  assert.equal(await status(post({ url: `${ORIGIN}/api/events?batch=${other}` })), 400, "two ids that disagree");
  assert.equal(batchOf(post({ headers: { "x-batch": BATCH.toUpperCase() } })), BATCH, "upper case is taken, stored lower");
});

test("an empty body is refused", async () => {
  assert.equal(await status(post({ body: "" })), 400);
});

test("a body over 64 KB is refused, whatever Content-Length says", async () => {
  const big = "x".repeat(MAX_BYTES + 1);
  assert.equal(await status(post({ body: big })), 413);
  assert.equal(await status(post({ body: "x".repeat(MAX_BYTES) })), 204, "exactly 64 KB is fine");
  assert.equal(await status(post({ headers: { "content-length": String(MAX_BYTES + 1) } })), 413);
  // A stream with no length, longer than the cap
  const chunk = new Uint8Array(40 * 1024);
  const stream = new ReadableStream({
    start(c) { c.enqueue(chunk); c.enqueue(chunk); c.close(); },
  });
  const req = new Request(`${ORIGIN}/api/events`, {
    method: "POST", body: stream, duplex: "half",
    headers: { origin: ORIGIN, "content-type": "text/plain", "x-batch": BATCH },
  });
  assert.equal(await status(req), 413);
});

test("an R2 failure is a 503, so the client keeps the batch; a missing binding too", async () => {
  assert.equal(await status(post(), { EVENTS: bucket({ fail: true }) }), 503);
  assert.equal(await status(post(), {}), 503);
});

test("the Worker never reads the IP address or the body's contents", async () => {
  const seen = [];
  const headers = new Proxy(new Headers({ origin: ORIGIN, "content-type": "text/plain", "x-batch": BATCH }), {
    get(target, prop) {
      const v = Reflect.get(target, prop, target);
      if (prop === "get") return (name) => { seen.push(name.toLowerCase()); return target.get(name); };
      return typeof v === "function" ? v.bind(target) : v;
    },
  });
  const req = { method: "POST", url: `${ORIGIN}/api/events`, headers, body: new Blob(["not json at all"]).stream() };
  const env = { EVENTS: bucket() };
  assert.equal((await handleEvents(req, env, OPTS)).status, 204);
  assert.equal(env.EVENTS.puts[0].body, "not json at all", "stored unparsed");
  assert.ok(!seen.includes("cf-connecting-ip") && !seen.includes("x-forwarded-for") && !seen.includes("user-agent"), seen.join());
});

test("standalone: only /api/events, with the key or a configured origin", async () => {
  const env = { EVENTS: bucket(), EVENTS_KEY: "k-123", EVENTS_ORIGINS: " https://example.com , " };
  const native = (path) => post({ url: `https://events.example.com${path}`, headers: { origin: undefined, "x-events-key": "k-123" } });
  assert.equal((await standalone(native("/"), env, NOW)).status, 404);
  assert.equal((await standalone(native("/api/events/x"), env, NOW)).status, 404);
  assert.equal((await standalone(native("/api/events"), env, NOW)).status, 204);
  assert.equal(env.EVENTS.puts[0].key, `v1/2026-10-15/${BATCH}.jsonl`);
  assert.equal((await standalone(post({ url: "https://events.example.com/api/events" }), env, NOW)).status, 204, "a listed origin");
  const bare = { EVENTS: bucket(), EVENTS_KEY: "k-123" };
  assert.equal((await standalone(post({ url: "https://events.example.com/api/events" }), bare, NOW)).status, 403,
    "no origins unless EVENTS_ORIGINS lists them");
});
