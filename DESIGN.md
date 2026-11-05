# MarkStile — Design & Handoff

> **For the implementing agent:** This document is the full context for a new project. Nothing else from the planning discussion is needed. Read it top to bottom, then start with **Milestone 0 (spikes)** in §12. The project is named **MarkStile** (stile + leaf: a stile is a set of steps for crossing a wall by its intended means; a leaf is a page).

---

## 1. Problem & idea

The user wants bookmarks kept in sync across **Firefox, Google Chrome, and Microsoft Edge**, including on Android (especially Edge for Android). Each vendor's sync service is a walled garden. Third-party tools can't write into mobile browsers' native bookmarks, and the vendor sync protocols are undocumented or locked down (Chrome Sync blocks third-party clients; Edge's server has no public API).

**The approach: real browsers as adapters.** A self-hosted Docker container runs real, non-headless desktop instances of Chrome, Edge, and Firefox. The user signs each one into its vendor account once, through a web-based VNC console. A small extension in each browser exposes the `bookmarks` WebExtension API to a Python orchestrator. The orchestrator detects changes in any browser and replicates them to the others. Each vendor's own sync then carries the changes to every other device, including phones.

```
 Phone (Edge) ⇄ Microsoft ⇄ [Edge in container] ┐
 Phone (Chrome) ⇄ Google  ⇄ [Chrome in container] ├─ extension ⇄ WebSocket ⇄ Python orchestrator ⇄ web portal
 Desktop (Firefox) ⇄ Mozilla ⇄ [Firefox in container] ┘
```

Every hop uses a supported mechanism. No vendor protocols are reverse-engineered, and no credentials are stored by the app (sign-in sessions live inside the browser profiles).

## 2. Goals & non-goals

**Goals**
- Two-way sync of bookmark trees across Chrome, Edge, and Firefox (Firefox Developer Edition; see §6.3).
- Zero required configuration beyond running the container with a data volume. All setup (portal password, browser sign-ins, first-run merge choice, folder mapping) happens in a web portal.
- Safe by default: never spread mass deletions without approval, and keep restorable history.
- Users should not depend on the maintainer for signing keys or hosted services (decentralized; everything is generated locally).

**Non-goals (for now)**
- Non-x86 architectures (Edge, and likely Chrome stable, ship x64-only Linux builds).
- Syncing anything other than bookmarks (no history, passwords, tabs).
- Firefox tags (not exposed by the WebExtension API). Document this limitation.
- Built-in HTTPS. Document that users should front it with a reverse proxy or Tailscale.
- Automating vendor sign-in. Users sign in manually via the console.

## 3. Decisions already made (do not relitigate without reason)

1. **Python orchestrator** (asyncio) is the brain. Extensions stay dumb: they read trees, apply operations, and report change events.
2. **Transport:** the orchestrator runs a local WebSocket server; each extension connects *out* to it and reconnects automatically.
3. **Change detection = snapshot diffing.** Events are only triggers. Sync runs on events (debounced) **and** on a schedule, as a safety net.
4. **No cross-browser ID mapping table.** Per-browser snapshots (with that browser's local IDs) are used to *derive* operations; operations are applied elsewhere by **path/content**, not ID. See §5 for the one extra piece of state (the canonical tree) and why.
5. **Root folders map by role**, with per-root modes `mirror` or `inbox → <destination>` (§5.6).
6. **Safety:** mass-deletion hold with approval, snapshot history with restore, explicit first-run merge choice.
7. **Web portal** handles everything user-facing, including noVNC. VNC itself is internal-only with an auto-generated password.
8. **Portal auth:** the first visitor sets the password (accepted risk: the container holds no data before sign-in). Password reset is triggered by changing an environment variable (§8.2).
9. **Firefox Developer Edition** with an unsigned, automatically provisioned extension. Revisit (e.g. self-signing alternatives) only if stability becomes a problem. Signing via a maintainer AMO key was **rejected** as too centralizing.
10. **Docker, x86-64 only.**

## 4. Container architecture

Single container, processes managed by **supervisord** (or s6; pick one), all restartable:

| Process | Purpose |
|---|---|
| `Xvnc` (TigerVNC) | X display `:1` + VNC server bound to `127.0.0.1` only, random password generated at each start and handed to the portal internally |
| `openbox` | Minimal window manager. Root menu/autostart offers only the three browsers; no general desktop |
| `google-chrome-stable` | From Google's apt repo |
| `microsoft-edge-stable` | From Microsoft's apt repo |
| `firefox-devedition` | From Mozilla's apt repo |
| `orchestrator` | Python: WebSocket server for extensions, sync engine, scheduler, web portal (FastAPI/uvicorn), noVNC WebSocket proxy, CRX update server |

**Exposed:** a single port (default `34871`) for the portal. Everything else binds to localhost.

**Browser launch notes**
- Use ordinary launches: **no** automation or remote-debugging flags (`--remote-debugging-port`, `--enable-automation`, etc.). They risk vendor sign-in blocks.
- Chrome/Edge on Linux without a keyring: use `--password-store=basic`. Verify that sign-in and sync still work (Milestone 0).
- Profiles live in the data volume (`/data/profiles/{chrome,edge,firefox}`) so sign-ins persist.
- Restart a browser automatically if it exits. If the user closes a window in the console, it comes back.
- GPU flags (e.g. `--disable-gpu`) as needed for Xvnc stability.

**Volume layout (`/data`)**
```
/data
  profiles/chrome/ profiles/edge/ profiles/firefox/
  state.db            # SQLite: settings, auth, canonical tree versions, per-browser snapshots, held changes, logs
  keys/extension.pem  # CRX signing key, generated on first start (never from the maintainer)
```

**Environment variables (all optional)**
- `PORTAL_RESET` — changing its value triggers portal password-reset mode (§8.2).
- `PORT` — portal port (default 34871).
- `SYNC_INTERVAL` — scheduled sync interval (default 5 min).
- `TZ` — for log timestamps.

## 5. Sync engine (the core; build and test this first)

### 5.1 State
- **Per-browser snapshot `S_B`**: the last-known tree of browser B, *including B's local IDs*, saved after each successful sync.
- **Canonical tree `K`**: the agreed bookmark state, browser-neutral (no IDs; just roles, folders, titles, URLs, separators, order). Versioned in SQLite for history and restore.

*Why `K` exists:* the user wanted minimal state and no mapping table. `K` keeps that promise while fixing two problems that pure op-replication has. (a) A browser whose extension was disconnected misses operations; reconciling it to `K` by content catches it up. (b) Echo loops disappear, because applying `K` to a browser and re-snapshotting produces no diff. `K` replaces a mapping table rather than adding to one.

### 5.2 Normalized tree model
```
Node = Folder{title, children[]} | Bookmark{title, url} | Separator
Root roles: toolbar, other, mobile, menu
```
- Chrome/Edge expose `toolbar` (Bookmarks bar / Favorites bar), `other` (Other bookmarks / Other favorites), `mobile` (Mobile bookmarks / Mobile favorites). Identify roots by API metadata (`folderType` where available), **not** by localized titles or hard-coded IDs.
- Firefox exposes `menu`, `toolbar`, `unfiled` (= `other`), `mobile`.
- **Projection** (`K` → a browser's view) and **lifting** (browser → `K` coordinates) handle per-browser differences:
  - Chrome/Edge have no `menu` root: project `K.menu` into a configurable folder (default `other/Bookmarks Menu`).
  - Chrome/Edge have no separators: drop them on projection and **exclude them when computing indices**, so positions stay consistent.
- **Chrome "account vs local" storage:** recent Chrome may expose separate account-synced and local-only bookmark trees. Always read and write the **account-synced** roots. Verify the API surface in Milestone 0.

### 5.3 Sync cycle
One cycle at a time (global async lock). Suppress or ignore events caused by our own writes; we re-snapshot afterward anyway.

1. **Fetch** the current tree `C_B` from each connected browser.
2. **Diff** each `C_B` against its `S_B` using B's local IDs. This yields ops in **`K` coordinates** (paths after lifting):
   - same ID, new parent or index → `move`
   - same ID, new title/url → `update`
   - ID only in `C_B` → `create` (with full subtree)
   - ID only in `S_B` → `delete`

   If a browser has no `S_B` yet, it is in first-run handling (§5.7).
3. **Safety check** (§5.5) on each browser's op set. Held ops are not applied, and that browser's `S_B` is not advanced.
4. **Merge** all browsers' ops into `K`, applying them by path:
   - `create` whose parent path is missing → create intermediate folders.
   - `update`/`move` whose target is missing → skip and log.
   - `delete` of a missing target → no-op.
   - **Edit beats delete:** if one browser deletes a node another edited or moved in this cycle, keep the node.
   - Duplicate creates (same title, url, and parent from two browsers) → dedupe.
   - Remaining conflicts (two different edits to one node) → a deterministic browser priority, configurable, defaulting to the order the ops arrived. Log every conflict.
   - Apply the root-mapping rules afterward (inbox moves, §5.6).
5. **Reconcile** each connected browser to `K` using a content tree diff (§5.4), and send the resulting ops to the extension.
6. **Verify:** re-fetch all trees and confirm each matches its projection of `K`. On mismatch, retry the reconcile once; if it still fails, log the error and surface it in the portal.
7. **Commit:** save the new `K` version (only if changed) and set `S_B = C'_B` for each verified browser.

Browsers whose extension is disconnected are skipped. They catch up automatically on a later cycle via step 5.

### 5.4 Content tree diff (`K` projection vs `C_B`)
Recursive, per folder:
- Match children by key `(type, title, url)` (folders: `(folder, title)`). Use an LCS over the keys to preserve order and minimize moves. Identical siblings match by their order among the matches.
- Unmatched in the browser → `delete`; unmatched in `K` → `create`; matched but out of order → `move`.
- Recurse into matched folders.
- Output ops reference **the browser's own IDs** (taken from `C_B`) so the extension can apply them directly.

This is pure Python and must be heavily unit- and property-tested (§11).

### 5.5 Safety
- **Mass-deletion hold:** if a browser's diff would delete more than a threshold (default: >20% of its bookmarks, or >50 items; configurable), or its tree became empty, hold that browser's ops and flag them in the portal for **Approve / Reject**. Reject means: reconcile that browser back to `K`. Typical causes of false alarms are sign-out, profile reset, and vendor sync hiccups.
- **History:** every change to `K` is stored with a timestamp and a summary (counts per op type and the browser of origin). Retention is configurable (default: 30 days plus the last 100 versions). Users can export any version as an HTML bookmarks file.
- **Restore:** set `K` to an older version, then reconcile all browsers. Requires confirmation, and bypasses the deletion hold because the user chose it explicitly.

### 5.6 Root mapping
Per root role, configurable in the portal:
- `mirror` (default): the role syncs 1:1 across browsers.
- `inbox → <destination path>`: anything that appears in this role is **moved** in `K` to the destination after the merge, leaving the root empty everywhere. The main use is `mobile` → e.g. `toolbar` or `other/From phone`, so bookmarks saved on a phone land somewhere useful without duplicates or a "mobile only" platform.
- `menu` projection target for Chrome/Edge (§5.2).

### 5.7 First run
Sync is **disabled** until the user chooses a merge strategy in the portal, after the browsers are signed in and their vendor sync has settled. The portal shows bookmark counts per browser to help.
- **Union:** `K` = merge of all browsers' trees, deduped by `(path, title, url)`.
- **Source browser:** `K` = that browser's tree. The other browsers are reconciled to it, and the portal warns how many of their items would be removed.

A browser added later, or whose `S_B` is lost, is treated as "join": its tree is union-merged into `K` (no deletions inferred), then reconciled.

### 5.8 Triggers
- **Events:** any bookmark event from an extension (`onCreated`, `onRemoved`, `onChanged`, `onMoved`, `onChildrenReordered`, import begin/end) starts a debounce timer: sync after **5 s of quiet**, forced after **30 s** maximum. Vendor sync arrives in bursts.
- **Schedule:** every `SYNC_INTERVAL` (default 5 min).
- **Manual:** the portal's "Sync now" button.
- **Startup:** a full cycle once all extensions connect, or after a timeout with whichever ones connected.

## 6. Extension

One codebase, built into two packages: a Chrome/Edge MV3 package and a Firefox package. Keep it minimal; all logic lives in Python.

### 6.1 Protocol (JSON over WebSocket, `ws://127.0.0.1:<internal port>`)
Extension → orchestrator:
- `hello {browser: "chrome"|"edge"|"firefox", extVersion, browserVersion}`
- `event {kind, ...}` — forwarded bookmark events (only to trigger syncs; no state is derived from them)
- `pong`

Orchestrator → extension (request/response with `id`):
- `getTree` → full tree from `bookmarks.getTree()`, including IDs, `folderType`/root metadata, indices
- `applyOps [ops]` → per-op results (`ok`, new IDs, or error). Ops: `create{parentId, index, title, url?}`, `update{id, title?, url?}`, `move{id, parentId, index}`, `remove{id}`, `removeTree{id}`
- `ping`

Apply ops sequentially in the extension and return per-op errors. The orchestrator decides what to do with failures.

### 6.2 Chrome / Edge (MV3)
- A service worker holds the WebSocket. To keep it alive, the orchestrator pings every ~20 s; recent Chrome versions extend service-worker lifetime while a WebSocket has activity (verify in Milestone 0). The extension reconnects with backoff on startup, on `onStartup`, and when an alarm fires.
- **Install via enterprise policy** (`--load-extension` is no longer honored by recent branded Chrome; assume Edge too):
  - On first start, generate `/data/keys/extension.pem`, pack a CRX locally, and serve it plus an update manifest XML from the orchestrator on localhost.
  - Write `ExtensionInstallForcelist` (`<extension-id>;http://127.0.0.1:<port>/ext/update.xml`) to `/etc/opt/chrome/policies/managed/` and `/etc/opt/edge/policies/managed/`. The extension ID comes from the generated key.
  - Verify that Linux Chrome and Edge accept force-installed, self-hosted CRXs from a localhost update URL (Milestone 0).
- Permissions: `bookmarks`, `alarms`. Host permission for the localhost WebSocket if required.

### 6.3 Firefox Developer Edition
- Developer Edition allows unsigned extensions when `xpinstall.signatures.required = false`. Set it via autoconfig (`mozilla.cfg` + `autoconfig.js` in the install dir) or the profile's `user.js`; verify which one works.
- Force-install with `policies.json` (in the Developer Edition install's `distribution/` directory): `ExtensionSettings` → `{ "<gecko id>": { "installation_mode": "force_installed", "install_url": "file:///opt/markstile/ext/firefox.xpi" } }`.
- The manifest needs `browser_specific_settings.gecko.id`.
- Background context: an MV2 persistent background page is simplest for a long-lived WebSocket. If you use MV3, handle event-page suspension the same way as Chrome (pings plus reconnect).
- Firefox Sync works normally in Developer Edition with a regular Mozilla account.

## 7. Display & console
- TigerVNC `Xvnc :1` on `127.0.0.1:5901`, with a random password regenerated at each container start and known only to the orchestrator.
- The portal serves the noVNC client and **proxies the VNC WebSocket through its own authenticated session** (implement a websockify-equivalent proxy in FastAPI, or run websockify on localhost behind the portal). Users never see VNC credentials.
- Openbox shows a minimal right-click menu with just the three browsers. Browsers autostart.

## 8. Web portal

FastAPI on uvicorn. A server-rendered UI (Jinja2 + htmx or similar) is enough; avoid a heavy SPA build.

### 8.1 Pages / features
- **First-run setup:** set the portal password.
- **Dashboard:** per browser — extension connected?, signed in (best effort: an indicator, or just instruct the user), last sync, bookmark count, last error. Global: last cycle, next scheduled cycle, "Sync now".
- **Console:** embedded noVNC for signing in.
- **First-run merge:** strategy choice (§5.7). Sync is disabled until it's chosen.
- **Held changes:** pending mass-deletion holds with a diff summary, plus Approve/Reject.
- **History:** versions of `K`, a diff summary per version, export as HTML, restore (with confirmation).
- **Settings:** root mapping/inbox modes, `menu` projection folder, deletion-hold thresholds, sync interval, conflict priority, retention.
- **Logs:** recent sync cycles, conflicts, errors.

### 8.2 Auth
- Password hashed with argon2 (or bcrypt) and stored in `state.db`. Session cookie: HttpOnly, SameSite=Strict, with CSRF protection on POSTs.
- **First run:** while no password exists, the first visitor sets it. This race is an accepted risk (no data exists yet).
- **Reset:** store a hash of the last-seen `PORTAL_RESET` value. If the variable is set and its value differs at startup, enter reset mode: the next portal visit prompts for a new password. *Open question:* also require a one-time token printed to the container log for resets, since by then signed-in sessions exist. Default to **yes** unless the user says otherwise; it's a small addition.
- Rate-limit login attempts.

## 9. Suggested project layout
```
markstile/
  orchestrator/
    markstile/
      engine/        # tree model, lift/project, id-diff, content-diff, merge, safety  (pure, no I/O)
      adapters/      # websocket server, browser session objects, request/response plumbing
      scheduler.py   # debounce, interval, cycle lock
      store.py       # SQLite access
      portal/        # FastAPI app, templates, auth, novnc proxy, crx update server
      main.py
    tests/
  extension/
    src/             # shared JS
    chrome/manifest.json
    firefox/manifest.json
    build.py|build.sh
  docker/
    Dockerfile
    supervisord.conf
    openbox/
    policies/        # templates, filled at startup
    entrypoint.sh    # key generation, CRX/XPI packing, policy writing, password reset check
  DESIGN.md
  README.md
```
Python 3.12+, `asyncio`, FastAPI, pydantic, `pytest`, `hypothesis`.

## 10. Security notes
- The container holds logged-in Google, Microsoft, and Mozilla sessions, so it's a valuable target. The README must say: **don't expose it to the internet**; use the LAN, Tailscale, or a reverse proxy with TLS.
- Only the portal port is exposed. VNC, the extension WebSocket, and the CRX server bind to localhost.
- The extension WebSocket should check a shared secret, generated at startup and baked into the extension build or passed via the policy-installed config, so other local processes can't impersonate a browser. Low priority, since it's a single-purpose container, but cheap.

## 11. Testing strategy
- **Engine (most important):** pure functions, no I/O.
  - Unit tests for each op type, lift/projection (separators, `menu` folder, roots), identical siblings, edit-beats-delete, inbox moves, first-run union.
  - Property tests (hypothesis): generate random trees and random edits on several simulated browsers, then run cycles. Assert **convergence** (all projections equal `K`) and **idempotence** (a second cycle produces no ops).
  - A simulated vendor-sync "burst" and an offline-browser catch-up scenario.
- **Fake adapter:** an in-memory browser that implements the extension protocol, for end-to-end orchestrator tests without real browsers.
- **Manual/integration:** real browsers in the container (Milestone 2+).

## 12. Milestones

**M0 — Spikes (do first; each answer may change the design):**
1. Chrome and Edge on Linux: does a force-installed, self-hosted CRX via `ExtensionInstallForcelist` with a `127.0.0.1` update URL install and stay enabled?
2. The Chrome bookmarks API: how are account vs local roots exposed (`folderType`, `syncing`, etc.)? Do the same fields exist in Edge?
3. MV3 service worker plus WebSocket: does a 20 s ping keep it alive indefinitely in current Chrome and Edge?
4. Firefox Developer Edition: does the autoconfig/`user.js` signature override combined with a `policies.json` force-install of an unsigned XPI work?
5. Sign-in and vendor sync for all three browsers inside Xvnc with `--password-store=basic` (Chrome/Edge) and no keyring.
6. Manual check: do Chrome and Edge **on Android** show the whole bookmark tree, including the toolbar folder? This informs the default mapping.

**M1 — Engine:** tree model, lift/project, ID diff, content diff, merge, safety, first run. Full test suite with fake adapters.

**M2 — Extension + bridge:** WebSocket protocol, both packages, and the orchestrator talking to real browsers on a dev machine (not yet containerized).

**M3 — Container:** Dockerfile, supervisord, Xvnc/openbox, browser installs, key generation, CRX/XPI provisioning, policies.

**M4 — Portal:** auth (first run plus reset), dashboard, noVNC proxy, Sync now, first-run merge flow.

**M5 — Safety UI & history:** held changes, history, export, restore.

**M6 — Mapping & polish:** root mapping UI, inbox mode, `menu` projection, settings, logs, README (including security warnings and the Firefox-tags limitation).

## 13. Open questions (ask the user if they block you)
- Should a password reset also require a console token? (Default: yes.)
- Default conflict priority between browsers?
- Default inbox behavior for `mobile`: `mirror` or `inbox → toolbar`? This depends on spike M0.6.
