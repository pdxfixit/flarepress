// flarepress events: the browser side of the shared telemetry wire format. An ES module, no build step, no packages.
//
//   import { init, track } from "./events-client.js";
//   init({
//     app: "isopix",                       // §3 app name
//     version: VERSION,                    // the app's own version string
//     player: () => playerId(),            // the app's plain random player id (a string, or a function that returns one)
//     screen: () => currentScreen,         // the screen the player is on now, from events.toml's screen names
//     enabled: () => settings.telemetry,   // the app's switch, read on every call
//   });
//   track("control.use", { control: "pencil", group: "tools", layout: "rail", vw: innerWidth, vh: innerHeight, input: "touch" });
//
// Each record gets every column of the standard's §3 (app, platform "web", form, version, player, session, seq, at,
// tz, event, screen, details). session is random per page load; seq counts from 0 within it.
//
// Records queue in memory and in localStorage (one entry per page load, `events:<app>:<session>`). They are sent as
// one JSON line each, in batches of at most 64 KB, to POST /api/events (text/plain, X-Batch: a random UUID):
//   - every 5 minutes while the page is shown (fetch; the `interval` option);
//   - when the page is hidden, and on pagehide (navigator.sendBeacon, which cannot set headers: the batch id goes in
//     the query, ?batch=<uuid>, and the Worker reads either).
// A batch is dropped only after a 2xx. Until then it is kept and sent again with the same id, so the Worker overwrites
// its object and the job keeps it once. A beacon's answer cannot be read, so its batch stays queued too. A page that
// closed with records still queued is picked up by the next page load of the app, once that entry has been quiet for
// three intervals.
//
// While enabled() is false, track does nothing and nothing is sent. Turning the switch off clears every queued record
// at the next call; call clear() from the switch's handler to clear them at once.
//
// Register the app's own visibilitychange listener (the one that tracks app.background) before init(), so its record
// is in the batch the beacon sends. Telemetry never throws into the app: every failure is swallowed.
//
// Vendor this file into an app with `just events-vendor` in flarepress (AGENTS.md "Telemetry events").

export const EVENTS_WIRE = 1;
export const MAX_BYTES = 64 * 1024;

// web form from the device's shortest side (CSS px) and whether its main pointer is touch
export function formFor({ shortSide, touch }) {
  if (!touch) return "desktop";
  return shortSide < 600 ? "phone" : "tablet";
}

function bytes(s) {
  return new TextEncoder().encode(s).byteLength;
}

// A random (version 4) UUID. crypto.randomUUID exists only in secure contexts (https, localhost), so a page over
// plain http falls back to crypto.getRandomValues, and with no crypto at all to Math.random: these ids only need to
// be unique, not secret.
export function uuidV4(c = globalThis.crypto) {
  try {
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
  } catch {
    // fall through
  }
  const b = new Uint8Array(16);
  let filled = false;
  try {
    if (c && typeof c.getRandomValues === "function") {
      c.getRandomValues(b);
      filled = true;
    }
  } catch {
    filled = false;
  }
  if (!filled) for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function defaults() {
  const g = globalThis;
  let storage = null;
  try {
    storage = g.localStorage ?? null;
  } catch {
    storage = null; // blocked site data
  }
  return {
    window: g.window ?? g,
    document: g.document,
    navigator: g.navigator,
    storage,
    fetch: g.fetch?.bind(g),
    uuid: () => uuidV4(g.crypto),
    now: () => Date.now(),
    setInterval: (f, ms) => g.setInterval(f, ms),
    clearInterval: (id) => g.clearInterval(id),
  };
}

function detectForm(w, nav) {
  try {
    const sc = w.screen ?? { width: w.innerWidth, height: w.innerHeight };
    const coarse = typeof w.matchMedia === "function" ? w.matchMedia("(pointer: coarse)").matches : (nav?.maxTouchPoints ?? 0) > 0;
    return formFor({ shortSide: Math.min(sc.width, sc.height), touch: coarse });
  } catch {
    return "desktop";
  }
}

// What init returns when telemetry could not start: every call does nothing.
function inert() {
  return {
    track: () => false,
    flush: () => Promise.resolve(),
    clear: () => {},
    session: null,
    form: null,
    tick: () => Promise.resolve(),
    queued: () => ({ records: [], batches: [] }),
  };
}

// One page's telemetry. It never throws: when it cannot start, it returns an instance that does nothing.
export function createEvents(opts) {
  try {
    return start(opts);
  } catch {
    return inert();
  }
}

function start(opts) {
  const g = { ...defaults(), ...(opts.globals ?? {}) };
  const { app: appName } = opts;
  const app = String(appName);
  const version = String(opts.version);
  const endpoint = opts.endpoint ?? "/api/events";
  const interval = opts.interval ?? 300000; // 5 minutes: one Worker request per open page per 5 minutes (Ben, 2026-10-16)
  const maxRecords = opts.maxRecords ?? 5000;
  const enabled = typeof opts.enabled === "function" ? opts.enabled : () => false;
  const prefix = `events:${app}:`;
  const session = g.uuid();
  const key = prefix + session;
  const form = opts.form ?? detectForm(g.window, g.navigator);

  let seq = 0;
  let queue = []; // JSON lines not yet in a batch
  let batches = []; // [{ id, body }], oldest first; each id always carries the same body
  let written = false; // this page's entry is in storage
  let beaconed = new Set(); // batch ids beaconed since the page was last shown
  let timer = null;
  let sending = false;

  const value = (v) => {
    try {
      return typeof v === "function" ? v() : v;
    } catch {
      return null;
    }
  };

  function store(fn) {
    if (!g.storage) return undefined;
    try {
      return fn(g.storage);
    } catch {
      return undefined; // full, or blocked: memory still holds the queue
    }
  }

  function persist() {
    if (!queue.length && !batches.length) {
      store((s) => s.removeItem(key));
      written = false;
      return;
    }
    const ok = store((s) => {
      s.setItem(key, JSON.stringify({ t: g.now(), queue, batches }));
      return true;
    });
    if (ok) written = true;
  }

  // Another page load took this page's entry (it looked closed): it sends those records now, so drop ours.
  function sync() {
    if (written && store((s) => s.getItem(key)) === null) {
      queue = [];
      batches = [];
      written = false;
    }
  }

  function storedKeys() {
    return (
      store((s) => {
        const keys = [];
        for (let i = 0; i < s.length; i++) {
          const k = s.key(i);
          if (k && k.startsWith(prefix)) keys.push(k);
        }
        return keys;
      }) ?? []
    );
  }

  // Take over the entries of page loads that closed with records queued.
  function adopt() {
    const stale = g.now() - 3 * interval;
    for (const k of storedKeys()) {
      if (k === key) continue;
      const entry = store((s) => JSON.parse(s.getItem(k)));
      if (entry && typeof entry.t === "number" && entry.t > stale) continue; // still alive
      if (entry && Array.isArray(entry.batches)) {
        for (const b of entry.batches) if (b && typeof b.id === "string" && typeof b.body === "string") batches.push(b);
      }
      if (entry && Array.isArray(entry.queue)) for (const l of entry.queue) if (typeof l === "string") queue.push(l);
      store((s) => s.removeItem(k));
    }
    trim();
    persist();
  }

  function trim() {
    let over = queue.length + batches.reduce((n, b) => n + b.body.split("\n").length - 1, 0) - maxRecords;
    while (over > 0 && batches.length) {
      over -= batches.shift().body.split("\n").length - 1;
    }
    if (over > 0) queue.splice(0, over);
  }

  function clear() {
    queue = [];
    batches = [];
    beaconed = new Set();
    for (const k of storedKeys()) store((s) => s.removeItem(k));
    written = false;
  }

  function on() {
    let yes = false;
    try {
      yes = enabled() === true;
    } catch {
      yes = false;
    }
    if (!yes && (queue.length || batches.length || written)) clear();
    return yes;
  }

  // Close the loose records into batches of at most MAX_BYTES.
  function freeze() {
    while (queue.length) {
      const lines = [];
      let size = 0;
      while (queue.length && size + bytes(queue[0]) + 1 <= MAX_BYTES) {
        size += bytes(queue[0]) + 1;
        lines.push(queue.shift());
      }
      batches.push({ id: g.uuid(), body: lines.join("\n") + "\n" });
    }
  }

  function track(event, details) {
    try {
      if (!on()) return false;
      sync();
      const at = g.now();
      const record = {
        app,
        platform: "web",
        form,
        version,
        player: value(opts.player) || null,
        session,
        seq: seq++,
        at,
        tz: 0 - new Date(at).getTimezoneOffset(),
        event: String(event),
        screen: value(opts.screen) ?? null,
        details: details && typeof details === "object" && !Array.isArray(details) ? details : {},
      };
      const line = JSON.stringify(record);
      if (bytes(line) + 1 > MAX_BYTES) return false; // one record bigger than a batch cannot be sent
      queue.push(line);
      trim();
      persist();
      return true;
    } catch {
      return false;
    }
  }

  // Send what is queued. beacon: navigator.sendBeacon (the page is going away); otherwise fetch, dropping each batch
  // the Worker answers with a 2xx and stopping at the first that fails.
  async function flush({ beacon = false } = {}) {
    try {
      if (!on()) return;
      sync();
      freeze();
      persist();
      if (beacon) {
        for (const b of batches) {
          if (beaconed.has(b.id)) continue;
          const sent = g.navigator?.sendBeacon?.(`${endpoint}?batch=${b.id}`, b.body);
          if (!sent) break; // the browser's beacon quota is full: the rest wait
          beaconed.add(b.id);
        }
        return;
      }
      if (sending || !g.fetch) return;
      sending = true;
      try {
        for (const b of batches.slice()) {
          let ok = false;
          try {
            const r = await g.fetch(endpoint, {
              method: "POST",
              headers: { "content-type": "text/plain;charset=UTF-8", "x-batch": b.id },
              body: b.body,
              keepalive: true,
              credentials: "omit",
            });
            ok = r.status >= 200 && r.status < 300;
          } catch {
            ok = false;
          }
          if (!ok) break;
          batches = batches.filter((x) => x.id !== b.id);
          persist();
        }
      } finally {
        sending = false;
      }
    } catch {
      // never throw into the app
    }
  }

  function tick() {
    sync();
    if (queue.length || batches.length) persist(); // a heartbeat: this page is alive, so no other load adopts it
    return flush();
  }

  function shown() {
    if (timer === null) timer = g.setInterval(tick, interval);
    beaconed = new Set();
  }

  function hidden() {
    if (timer !== null) g.clearInterval(timer);
    timer = null;
    flush({ beacon: true });
  }

  const doc = g.document;
  if (doc && typeof doc.addEventListener === "function") {
    doc.addEventListener("visibilitychange", () => (doc.visibilityState === "hidden" ? hidden() : shown()));
  }
  if (g.window && typeof g.window.addEventListener === "function") {
    g.window.addEventListener("pagehide", () => flush({ beacon: true }));
  }
  if (on()) adopt();
  else clear(); // records an earlier page load left before the switch went off
  if (!doc || doc.visibilityState !== "hidden") shown();

  return {
    track,
    flush,
    clear,
    session,
    form,
    tick,
    queued: () => ({ records: queue.slice(), batches: batches.map((b) => ({ ...b })) }),
  };
}

let current = null;

// Start the page's telemetry (once per page load); returns the instance.
export function init(opts) {
  current = createEvents(opts);
  return current;
}

// Record one event. Does nothing before init() or while the switch is off. Returns whether it was queued.
export function track(event, details) {
  try {
    return current ? current.track(event, details) : false;
  } catch {
    return false;
  }
}

export function flush(opts) {
  try {
    return current ? current.flush(opts) : Promise.resolve();
  } catch {
    return Promise.resolve();
  }
}

// Drop every queued record, sent or not: call it when the player turns telemetry off.
export function clear() {
  try {
    if (current) current.clear();
  } catch {
    // never throw into the app
  }
}
