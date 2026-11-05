// Spike background script shared by Chrome/Edge (MV3 service worker) and Firefox (MV2 background page).
const api = globalThis.browser ?? globalThis.chrome;
const WS_URL = `ws://127.0.0.1:${self.STILEAF_WS_PORT || 8765}/`;
const STARTED = Date.now();
let ws = null;
let backoff = 1000;
let connects = 0;

function detectBrowser() {
  const ua = navigator.userAgent;
  if (ua.includes("Firefox/")) return "firefox";
  if (ua.includes("Edg/")) return "edge";
  return "chrome";
}

async function browserVersion() {
  try {
    if (api.runtime.getBrowserInfo) { const i = await api.runtime.getBrowserInfo(); return `${i.name} ${i.version}`; }
  } catch (e) {}
  try {
    // Chromium UA strings are reduced to "Chrome/156.0.0.0"; full versions come from UA-CH.
    const h = await navigator.userAgentData.getHighEntropyValues(["fullVersionList"]);
    const b = h.fullVersionList.filter((x) => /Chrome|Edge|Chromium/.test(x.brand));
    if (b.length) return b.map((x) => `${x.brand} ${x.version}`).join(", ");
  } catch (e) {}
  const m = navigator.userAgent.match(/(Edg|Chrome|Firefox)\/([\d.]+)/g);
  return m ? m.join(" ") : navigator.userAgent;
}

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

async function handle(msg) {
  if (msg.type === "ping") { send({ type: "pong", id: msg.id, uptimeMs: Date.now() - STARTED, connects }); return; }
  if (msg.type === "getTree") {
    try { send({ type: "result", id: msg.id, ok: true, tree: await api.bookmarks.getTree() }); }
    catch (e) { send({ type: "result", id: msg.id, ok: false, error: String(e) }); }
    return;
  }
  if (msg.type === "create") {
    try { send({ type: "result", id: msg.id, ok: true, node: await api.bookmarks.create(msg.args) }); }
    catch (e) { send({ type: "result", id: msg.id, ok: false, error: String(e) }); }
    return;
  }
  if (msg.type === "call") {
    // Generic spike hook: bookmarks.<method>(...args)
    try { send({ type: "result", id: msg.id, ok: true, value: await api.bookmarks[msg.method](...msg.args) }); }
    catch (e) { send({ type: "result", id: msg.id, ok: false, error: String(e && e.message || e) }); }
    return;
  }
  if (msg.type === "updateCheck") {
    // Chromium only: ask the browser to poll the update URL now (normally every few hours).
    try {
      const r = await api.runtime.requestUpdateCheck();
      send({ type: "result", id: msg.id, ok: true, update: r });
    } catch (e) { send({ type: "result", id: msg.id, ok: false, error: String(e) }); }
    return;
  }
  send({ type: "result", id: msg.id, ok: false, error: "unknown " + msg.type });
}

async function connect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  try { ws = new WebSocket(WS_URL); } catch (e) { scheduleReconnect(); return; }
  ws.onopen = async () => {
    connects++; backoff = 1000;
    send({ type: "hello", browser: detectBrowser(), extVersion: api.runtime.getManifest().version,
           browserVersion: await browserVersion(), ua: navigator.userAgent,
           extId: api.runtime.id, workerStartedAt: STARTED, connects });
  };
  ws.onmessage = (ev) => { try { handle(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => { ws = null; scheduleReconnect(); };
  ws.onerror = () => {};
}

function scheduleReconnect() {
  setTimeout(connect, backoff);
  backoff = Math.min(backoff * 2, 30000);
}

for (const ev of ["onCreated", "onRemoved", "onChanged", "onMoved"]) {
  api.bookmarks[ev].addListener((id) => send({ type: "event", kind: ev, id }));
}
api.runtime.onStartup.addListener(connect);
// Apply a downloaded update immediately instead of waiting for the worker/browser to restart.
if (api.runtime.onUpdateAvailable) api.runtime.onUpdateAvailable.addListener(() => api.runtime.reload());
api.runtime.onInstalled.addListener(connect);
if (!self.STILEAF_NO_ALARM) {  // spike control builds set this to observe raw worker lifetime
  api.alarms.create("stileaf-reconnect", { periodInMinutes: 0.5 });
  api.alarms.onAlarm.addListener(connect);
}
connect();
