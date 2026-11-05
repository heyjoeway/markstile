// MarkStile extension: a thin bridge between the browser's bookmarks API and the
// MarkStile orchestrator. It reads trees, applies operations, and forwards
// bookmark events. All sync logic lives in the orchestrator.
"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const IS_FIREFOX = typeof globalThis.browser !== "undefined" && !!api.runtime.getBrowserInfo;
const DEFAULTS = { wsPort: 8765, secret: "", browser: "" };

let ws = null;
let connecting = false;
let backoff = 1000;
let reconnectTimer = null;

async function config() {
  let managed = {};
  try {
    managed = (await api.storage.managed.get(null)) || {};
  } catch (e) {
    // no managed storage (developer install): use defaults
  }
  const cfg = { ...DEFAULTS, ...managed };
  if (!cfg.browser) cfg.browser = await detectBrowser();
  return cfg;
}

async function detectBrowser() {
  if (IS_FIREFOX) return "firefox";
  if (navigator.brave) return "brave";
  const brands = navigator.userAgentData?.brands?.map((b) => b.brand) ?? [];
  if (brands.some((b) => /Opera/i.test(b)) || /\bOPR\//.test(navigator.userAgent)) return "opera";
  if (brands.some((b) => /Vivaldi/i.test(b)) || /\bVivaldi\//.test(navigator.userAgent)) return "vivaldi";
  if (brands.some((b) => /Edge/i.test(b)) || /\bEdg\//.test(navigator.userAgent)) return "edge";
  return "chrome";
}

async function browserVersion() {
  if (IS_FIREFOX) {
    const info = await api.runtime.getBrowserInfo();
    return `${info.name} ${info.version}`;
  }
  // Chromium reduces the UA string to "Chrome/NNN.0.0.0"; ask for the full list.
  try {
    const hv = await navigator.userAgentData.getHighEntropyValues(["fullVersionList"]);
    const list = hv.fullVersionList.filter((b) => !/Not.?A.?Brand|Chromium/i.test(b.brand));
    if (list.length) return list.map((b) => `${b.brand} ${b.version}`).join(", ");
  } catch (e) {}
  return navigator.userAgent;
}

// Random per browser session: storage.session survives service-worker restarts
// but not browser restarts, so the orchestrator can tell the two apart.
async function bootId() {
  try {
    const { bootId } = await api.storage.session.get("bootId");
    if (bootId) return bootId;
    const id = crypto.randomUUID();
    await api.storage.session.set({ bootId: id });
    return id;
  } catch (e) {
    return "";
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, backoff);
  backoff = Math.min(backoff * 2, 30000);
}

async function connect() {
  if (connecting || (ws && ws.readyState <= 1)) return;
  connecting = true;
  try {
    const cfg = await config();
    const sock = new WebSocket(`ws://127.0.0.1:${cfg.wsPort}`);
    ws = sock;
    sock.onopen = async () => {
      backoff = 1000;
      checkForUpdate();
      sock.send(JSON.stringify({
        type: "hello",
        browser: cfg.browser,
        extVersion: api.runtime.getManifest().version,
        browserVersion: await browserVersion(),
        bootId: await bootId(),
        secret: cfg.secret,
      }));
    };
    sock.onmessage = (ev) => handle(sock, ev.data);
    sock.onclose = () => {
      if (ws === sock) ws = null;
      scheduleReconnect();
    };
    sock.onerror = () => {};
  } catch (e) {
    scheduleReconnect();
  } finally {
    connecting = false;
  }
}

async function handle(sock, data) {
  let msg;
  try {
    msg = JSON.parse(data);
  } catch (e) {
    return;
  }
  if (msg.type === "ping") {
    sock.send('{"type":"pong"}');
    return;
  }
  if (msg.type !== "request") return;
  try {
    let result;
    if (msg.method === "getTree") result = await api.bookmarks.getTree();
    else if (msg.method === "applyOps") result = await applyOps(msg.params.ops);
    else if (msg.method === "ping") result = "pong";
    else throw new Error(`unknown method ${msg.method}`);
    sock.send(JSON.stringify({ type: "response", id: msg.id, result }));
  } catch (e) {
    sock.send(JSON.stringify({ type: "response", id: msg.id, error: String(e?.message ?? e) }));
  }
}

// -- operations ---------------------------------------------------------------

async function getOne(id) {
  const [node] = await api.bookmarks.get(id);
  return node;
}

// `index` in a move op is the node's final index in the destination folder.
// Chrome's move() takes the index *before* removal for same-parent moves
// downward; verify afterwards and correct if a browser behaves otherwise.
async function move(id, parentId, index) {
  const before = await getOne(id);
  let idx = index;
  if (!IS_FIREFOX && before.parentId === parentId && before.index < index) idx = index + 1;
  await api.bookmarks.move(id, { parentId, index: idx });
  const after = await getOne(id);
  if (after.parentId === parentId && after.index !== index) {
    const alt = after.index < index ? idx + 1 : idx - 1;
    await api.bookmarks.move(id, { parentId, index: Math.max(0, alt) });
  }
}

async function exists(id) {
  try {
    await api.bookmarks.get(id);
    return true;
  } catch (e) {
    return false;
  }
}

// Vivaldi reports success for removing a Speed Dial folder but keeps it; moving
// it into Vivaldi's Trash root (where its removals go anyway) does work.
async function removeNode(id, tree) {
  if (tree) await api.bookmarks.removeTree(id);
  else await api.bookmarks.remove(id);
  if (!(await exists(id))) return;
  const [root] = await api.bookmarks.getTree();
  const trash = (root.children ?? []).find((c) => c.trash === true);
  if (!trash) throw new Error("the browser kept the removed item");
  await api.bookmarks.move(id, { parentId: trash.id });
}

async function applyOps(ops) {
  const refs = new Map();
  const res = (x) => (x && refs.has(x) ? refs.get(x) : x);
  const results = [];
  for (const op of ops) {
    try {
      if (op.op === "create") {
        const details = { parentId: res(op.parentId), index: op.index };
        if (op.type === "separator") {
          if (!IS_FIREFOX) throw new Error("separators unsupported");
          details.type = "separator";
        } else {
          details.title = op.title ?? "";
          if (op.type === "bookmark") details.url = op.url;
          if (IS_FIREFOX) details.type = op.type;
        }
        const node = await api.bookmarks.create(details);
        if (op.ref) refs.set(op.ref, node.id);
        results.push({ ok: true, id: node.id });
      } else if (op.op === "update") {
        const changes = {};
        if ("title" in op) changes.title = op.title;
        if ("url" in op) changes.url = op.url;
        await api.bookmarks.update(res(op.id), changes);
        results.push({ ok: true });
      } else if (op.op === "move") {
        await move(res(op.id), res(op.parentId), op.index);
        results.push({ ok: true });
      } else if (op.op === "remove" || op.op === "removeTree") {
        await removeNode(res(op.id), op.op === "removeTree");
        results.push({ ok: true });
      } else {
        results.push({ ok: false, error: `unknown op ${op.op}` });
      }
    } catch (e) {
      results.push({ ok: false, error: String(e?.message ?? e) });
    }
  }
  return results;
}

// -- events -------------------------------------------------------------------

const EVENTS = [
  "onCreated", "onRemoved", "onChanged", "onMoved",
  "onChildrenReordered", "onImportBegan", "onImportEnded",
];
for (const name of EVENTS) {
  const ev = api.bookmarks[name];
  if (!ev) continue;
  ev.addListener(() => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "event", kind: name }));
  });
}

// -- self-hosted updates (Chrome/Edge) ------------------------------------------
// The orchestrator serves a new CRX after an image upgrade; check on every
// connect instead of waiting hours for the browser's periodic check.
let lastUpdateCheck = 0;
function checkForUpdate() {
  if (IS_FIREFOX || !api.runtime.requestUpdateCheck) return;
  if (Date.now() - lastUpdateCheck < 10 * 60 * 1000) return;
  lastUpdateCheck = Date.now();
  api.runtime.requestUpdateCheck().catch(() => {});
}
api.runtime.onUpdateAvailable?.addListener(() => api.runtime.reload());

// -- lifecycle ----------------------------------------------------------------

// Managed policy (port, secret) can arrive after startup: reconnect with it.
api.storage.onChanged?.addListener((changes, area) => {
  if (area !== "managed") return;
  backoff = 1000;
  if (ws) ws.close();
  else connect();
});

api.runtime.onStartup?.addListener(connect);
api.runtime.onInstalled?.addListener(connect);
if (api.alarms) {
  api.alarms.create("markstile-reconnect", { periodInMinutes: 0.5 });
  api.alarms.onAlarm.addListener(() => connect());
}
connect();
