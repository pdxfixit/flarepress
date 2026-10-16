// events/events-client.js against small fakes of the page: Node's own test runner, no packages.
//   node --test tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createEvents, formFor, init, track, flush, clear, MAX_BYTES } from "../events/events-client.js";

class FakeStorage {
  constructor() { this.m = new Map(); this.full = false; }
  get length() { return this.m.size; }
  key(i) { return [...this.m.keys()][i] ?? null; }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { if (this.full) throw new Error("QuotaExceededError"); this.m.set(k, String(v)); }
  removeItem(k) { this.m.delete(k); }
}

class Target {
  constructor() { this.l = {}; }
  addEventListener(t, f) { (this.l[t] ??= []).push(f); }
  fire(t) { for (const f of this.l[t] ?? []) f(); }
}

// A page: document, window, navigator, storage, a fetch that answers `status`, a clock and timers.
let pages = 0;

function page({ storage = new FakeStorage(), status = 204, at = Date.UTC(2026, 9, 15, 12), beaconOk = true } = {}) {
  const n = ++pages;
  const p = {
    storage,
    status,
    t: at,
    ids: 0,
    fetches: [],
    beacons: [],
    timers: new Map(),
    document: Object.assign(new Target(), { visibilityState: "visible" }),
    window: Object.assign(new Target(), { screen: { width: 390, height: 844 }, matchMedia: () => ({ matches: true }) }),
  };
  p.navigator = { sendBeacon: (url, body) => { if (!beaconOk) return false; p.beacons.push({ url, body }); return true; } };
  p.globals = {
    window: p.window,
    document: p.document,
    navigator: p.navigator,
    storage,
    fetch: async (url, init) => {
      p.fetches.push({ url, ...init });
      if (p.status === "throw") throw new TypeError("offline");
      return { status: p.status };
    },
    uuid: () => `${String(n).padStart(8, "0")}-0000-4000-8000-${String(++p.ids).padStart(12, "0")}`,
    now: () => p.t,
    setInterval: (f, ms) => { const id = p.timers.size + 1; p.timers.set(id, { f, ms }); return id; },
    clearInterval: (id) => p.timers.delete(id),
  };
  p.hide = () => { p.document.visibilityState = "hidden"; p.document.fire("visibilitychange"); };
  p.show = () => { p.document.visibilityState = "visible"; p.document.fire("visibilitychange"); };
  return p;
}

function events(p, over = {}) {
  let on = over.on ?? true;
  const e = createEvents({
    app: "isopix", version: "1.2.3", player: () => "p-1", screen: () => "canvas",
    enabled: () => on, globals: p.globals, ...over,
  });
  e.turn = (v) => { on = v; };
  return e;
}

const lines = (body) => body.trim().split("\n").map((l) => JSON.parse(l));

test("a record carries every column of the standard, in order", async () => {
  const p = page();
  const e = events(p);
  e.track("control.use", { control: "pencil" });
  p.t += 5;
  e.track("screen.view", {});
  await e.flush();
  assert.equal(p.fetches.length, 1);
  const [a, b] = lines(p.fetches[0].body);
  assert.deepEqual(Object.keys(a), ["app", "platform", "form", "version", "player", "session", "seq", "at", "tz",
    "event", "screen", "details"]);
  assert.deepEqual({ ...a, session: "s", tz: 0 }, {
    app: "isopix", platform: "web", form: "phone", version: "1.2.3", player: "p-1", session: "s", seq: 0,
    at: Date.UTC(2026, 9, 15, 12), tz: 0, event: "control.use", screen: "canvas", details: { control: "pencil" },
  });
  assert.equal(a.tz, 0 - new Date(a.at).getTimezoneOffset());
  assert.equal(a.session, e.session);
  assert.equal(b.seq, 1);
  assert.equal(b.at, a.at + 5);
});

test("the request is POST text/plain with the batch id in X-Batch, without cookies", async () => {
  const p = page();
  const e = events(p);
  e.track("app.open", {});
  await e.flush();
  const f = p.fetches[0];
  assert.equal(f.url, "/api/events");
  assert.equal(f.method, "POST");
  assert.equal(f.headers["content-type"], "text/plain;charset=UTF-8");
  assert.match(f.headers["x-batch"], /^[0-9a-f-]{36}$/);
  assert.equal(f.credentials, "omit");
  assert.ok(f.body.endsWith("\n"));
});

test("form comes from the shortest side and touch", () => {
  assert.equal(formFor({ shortSide: 390, touch: true }), "phone");
  assert.equal(formFor({ shortSide: 820, touch: true }), "tablet");
  assert.equal(formFor({ shortSide: 390, touch: false }), "desktop");
  const p = page();
  p.window.matchMedia = () => ({ matches: false });
  assert.equal(events(p).form, "desktop");
  p.window.matchMedia = () => ({ matches: true });
  p.window.screen = { width: 1366, height: 1024 };
  assert.equal(events(p).form, "tablet");
});

test("a batch is dropped only after a 2xx; a retry reuses its id and body", async () => {
  const p = page({ status: 503 });
  const e = events(p);
  e.track("app.open", {});
  await e.flush();
  p.status = "throw";
  await e.flush();
  p.status = 204;
  await e.flush();
  assert.equal(p.fetches.length, 3);
  assert.equal(new Set(p.fetches.map((f) => f.headers["x-batch"])).size, 1, "one id for every try");
  assert.equal(new Set(p.fetches.map((f) => f.body)).size, 1, "and one body");
  assert.deepEqual(e.queued().batches, []);
  await e.flush();
  assert.equal(p.fetches.length, 3, "nothing left to send");
});

test("records made while a batch waits go in a new batch, never into the waiting one", async () => {
  const p = page({ status: 500 });
  const e = events(p);
  e.track("a", {});
  await e.flush();
  e.track("b", {});
  p.status = 204;
  await e.flush();
  const sent = p.fetches.slice(1).map((f) => [f.headers["x-batch"], lines(f.body).map((r) => r.event)]);
  assert.equal(sent.length, 2);
  assert.equal(sent[0][0], p.fetches[0].headers["x-batch"]);
  assert.deepEqual(sent.map((s) => s[1]), [["a"], ["b"]]);
});

test("batches stay within 64 KB, and a record over 64 KB is dropped", async () => {
  const p = page();
  const e = events(p);
  const pad = "x".repeat(1000);
  for (let i = 0; i < 150; i++) e.track("content.start", { pad });
  assert.equal(e.track("content.start", { pad: "y".repeat(MAX_BYTES) }), false);
  await e.flush();
  assert.ok(p.fetches.length >= 3);
  for (const f of p.fetches) assert.ok(new TextEncoder().encode(f.body).byteLength <= MAX_BYTES);
  assert.equal(p.fetches.reduce((n, f) => n + lines(f.body).length, 0), 150);
});

test("every 60 s while shown; on hidden and pagehide by beacon, with the id in the query", async () => {
  const p = page();
  const e = events(p);
  assert.deepEqual([...p.timers.values()].map((t) => t.ms), [60000]);
  e.track("app.open", {});
  await [...p.timers.values()][0].f();
  assert.equal(p.fetches.length, 1, "the timer sends");
  e.track("app.background", {});
  p.hide();
  assert.equal(p.timers.size, 0, "no timer while hidden");
  assert.equal(p.beacons.length, 1);
  const [b] = p.beacons;
  const id = e.queued().batches[0].id;
  assert.equal(b.url, `/api/events?batch=${id}`);
  assert.deepEqual(lines(b.body).map((r) => r.event), ["app.background"]);
  p.window.fire("pagehide");
  assert.equal(p.beacons.length, 1, "a batch is beaconed once while hidden");
  assert.equal(e.queued().batches.length, 1, "a beacon's answer is unknown, so the batch stays");
  p.show();
  assert.equal(p.timers.size, 1);
  await e.flush();
  assert.equal(p.fetches.at(-1).headers["x-batch"], id, "sent again with the same id");
  assert.deepEqual(e.queued().batches, []);
});

test("a full beacon quota leaves the rest for later", () => {
  const p = page({ beaconOk: false });
  const e = events(p);
  e.track("app.open", {});
  p.hide();
  assert.equal(p.beacons.length, 0);
  assert.equal(e.queued().batches.length, 1);
});

test("the switch: off sends nothing and records nothing; turning it off clears the queue", async () => {
  const p = page();
  const e = events(p);
  e.track("a", {});
  assert.ok(p.storage.length > 0, "queued in localStorage");
  e.turn(false);
  assert.equal(e.track("b", {}), false);
  assert.deepEqual(e.queued(), { records: [], batches: [] });
  assert.equal(p.storage.length, 0, "and cleared from localStorage");
  await e.flush();
  p.hide();
  assert.equal(p.fetches.length + p.beacons.length, 0);
  e.turn(true);
  e.track("c", {});
  e.clear();
  assert.deepEqual(e.queued(), { records: [], batches: [] });
  assert.equal(p.storage.length, 0);
});

test("a page that starts with the switch off clears what an earlier load left", () => {
  const p = page();
  events(p).track("a", {});
  assert.equal(p.storage.length, 1);
  events(p, { on: false });
  assert.equal(p.storage.length, 0);
});

test("records survive a closed page: the next load picks them up once the entry is quiet", async () => {
  const storage = new FakeStorage();
  const p1 = page({ storage });
  const first = events(p1);
  first.track("a", {});
  first.track("b", {});
  // the page closes without sending. A load straight away leaves the entry alone (it may be another live tab).
  const p2 = page({ storage, at: p1.t + 1000 });
  events(p2);
  assert.equal(storage.length, 1);
  // three intervals later it is taken over
  const p3 = page({ storage, at: p1.t + 3 * 60000 + 1 });
  const third = events(p3);
  third.track("c", {});
  await third.flush();
  const got = p3.fetches.flatMap((f) => lines(f.body)).map((r) => [r.session === first.session, r.event]);
  assert.deepEqual(got, [[true, "a"], [true, "b"], [false, "c"]]);
  assert.equal(storage.length, 0);
});

test("a tab whose entry another load took over drops its copy instead of sending it twice", async () => {
  const storage = new FakeStorage();
  const p1 = page({ storage });
  const first = events(p1);
  first.track("a", {});
  const p2 = page({ storage, at: p1.t + 4 * 60000 });
  events(p2);
  await first.flush();
  assert.equal(p1.fetches.length, 0);
  assert.deepEqual(first.queued(), { records: [], batches: [] });
});

test("a live tab's heartbeat keeps its entry its own", () => {
  const storage = new FakeStorage();
  const p1 = page({ storage, status: 500 });
  const first = events(p1);
  first.track("a", {});
  p1.t += 2 * 60000;
  first.tick();
  const p2 = page({ storage, at: p1.t + 2 * 60000 });
  const second = events(p2);
  assert.deepEqual(second.queued(), { records: [], batches: [] });
});

test("blocked or full storage: the queue lives in memory and nothing throws", async () => {
  const p = page({ storage: null });
  p.globals.storage = null;
  const e = events(p);
  e.track("a", {});
  await e.flush();
  assert.equal(p.fetches.length, 1);
  const full = new FakeStorage();
  full.full = true;
  const q = page({ storage: full });
  const f = events(q);
  assert.equal(f.track("a", {}), true);
  assert.equal(f.queued().records.length, 1);
});

test("a throwing getter or bad details never reach the app", () => {
  const p = page();
  const e = events(p, { screen: () => { throw new Error("boom"); }, player: () => "" });
  assert.equal(e.track("a", [1, 2]), true);
  const r = JSON.parse(e.queued().records[0]);
  assert.equal(r.screen, null);
  assert.equal(r.player, null);
  assert.deepEqual(r.details, {});
  const bad = events(p, { enabled: () => { throw new Error("no"); } });
  assert.equal(bad.track("a", {}), false);
});

test("the queue is capped, oldest first", () => {
  const p = page();
  const e = events(p, { maxRecords: 3 });
  for (const n of ["a", "b", "c", "d"]) e.track(n, {});
  assert.deepEqual(e.queued().records.map((l) => JSON.parse(l).event), ["b", "c", "d"]);
});

test("the module's own init and track", async () => {
  assert.equal(track("before-init", {}), false);
  await flush();
  clear();
  const p = page();
  init({ app: "noodlepads", version: "9", player: "p-2", screen: () => null, enabled: () => true, globals: p.globals });
  assert.equal(track("app.open", { first_run: true }), true);
  await flush();
  const [r] = lines(p.fetches[0].body);
  assert.equal(r["app"], "noodlepads");
  assert.equal(r.player, "p-2");
  track("x", {});
  clear();
  await flush();
  assert.equal(p.fetches.length, 1);
});
