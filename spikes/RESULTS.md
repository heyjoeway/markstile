# Stileaf M0 feasibility spikes: results

Date: 2026-11-03. Host: Ubuntu 26.04 x86-64, no root. All browsers ran headless, without a window
on the user's desktop. Raw evidence is in `spikes/evidence/`.

| # | Spike | Verdict |
|---|-------|---------|
| 1 | Chrome/Edge force-install of a self-hosted CRX from `http://127.0.0.1` | **Works.** Both browsers, headless, installed within about 1 s of startup and stayed enabled across restarts. Self-hosted *updates* need `update_url` in the manifest (see below). |
| 2 | Chrome/Edge bookmarks tree fields | **Answered.** `folderType` and `syncing` exist on both. Managed roots carry `unmodifiable: "managed"`. No account roots appear when not signed in. |
| 3 | MV3 service worker + WebSocket with 20 s pings | **Works.** Chrome and Edge each kept **one worker and one connection for 32 min** with 0 disconnects. Controls show the pings are what keep the worker alive. |
| 4 | Firefox Developer Edition: unsigned XPI via `policies.json` | **Works with either method.** (a) autoconfig and (b) profile `user.js` each work on their own. With neither, nothing installs. |
| 5 | Sign-in / vendor sync | **Skipped.** It needs interactive accounts. |
| 6 | Android tree check | **Skipped.** It needs the user. |

## Versions tested

| Browser | Version | Source |
|---|---|---|
| Google Chrome stable | 156.0.8078.79 | `https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb` (`dpkg -x`) |
| Microsoft Edge stable | 156.0.4314.58 (Chromium 156.0.8078.80) | `https://packages.microsoft.com/repos/edge/pool/main/m/microsoft-edge-stable/microsoft-edge-stable_156.0.4314.58-1_amd64.deb` (newest in the repo `Packages` index; the index is not sorted, so sort by version) |
| Firefox Developer Edition | 160.0b4 (reports `Firefox 160.0` via `runtime.getBrowserInfo`) | `https://download.mozilla.org/?product=firefox-devedition-latest-ssl&os=linux64&lang=en-US` (tar.xz) |
| Firefox release (control) | 159.0.1 (`/usr/bin/firefox`) | system |
| Python `websockets` | 17.4 | project venv |

## Files in `spikes/`

| File | Purpose |
|---|---|
| `crx3.py` | **CRX3 packer** that needs only the stdlib plus the `openssl` CLI: `genkey`, `id`, `pack`, `update-xml`. It is ready to reuse in production. |
| `build.sh [OUT]` | Builds both packages: key (once), CRX, `update.xml`, XPI. Env: `WS_PORT`, `HTTP_PORT`, `NO_ALARM=1`. |
| `ext/common/bg.js`, `ext/chrome/*`, `ext/firefox/*` | Test extension: hello, getTree, create, ping/pong, updateCheck, and forwarded bookmark events. |
| `spike_server.py` | Static HTTP (update.xml, crx, xpi; sends `Cache-Control: no-store`) plus a WebSocket server. It pings, saves `getTree`, and runs scripted requests. Uses the venv. |
| `run_chromium.sh` | Runs Chrome/Edge with a managed policy at `/etc/opt/{chrome,edge}/policies/managed/` without root (namespace and overlay trick below). |
| `run_firefox.sh MODE ...` | Writes `distribution/policies.json` plus the autoconfig or `user.js` override, then runs Firefox Developer Edition headless. |
| `run_soak.sh`, `run_bare_controls.sh` | Spike 3 soak and controls. |
| `test_chromium_update.sh` | On-demand CRX update test. |
| `show_tree.py` | Prints a tree dump one node per line. |
| `script-*.json` | Request scripts for `spike_server.py --script`. |

## Running policies without root (dev machines only)

`/etc/opt` did not exist, and a mount point cannot be created without root. Mounting **overlayfs over all of `/etc`** inside `unshare -rm` works: `upperdir` holds `opt/chrome/policies/managed/stileaf.json`.

Chrome refuses to start as (namespace) uid 0 with its sandbox: `Running as root without --no-sandbox is not supported`. The fix is to exec the browser through a **nested** `unshare -U --map-user=$UID --map-group=$GID`. The nested namespace keeps the mount namespace, and the normal Chromium sandbox then works. `HOME`/`XDG_*` are redirected because crashpad otherwise creates `~/.config/google-chrome/Crash Reports`.

In the container this is unnecessary, since root writes the real `/etc/opt/...`.

---

## Spike 1: force-installed self-hosted CRX (Chrome and Edge)

**Verdict: works on both, headless (`--headless=new`).** Policy files were placed at `/etc/opt/chrome/policies/managed/stileaf.json` and `/etc/opt/edge/policies/managed/stileaf.json`:

```json
{ "ExtensionInstallForcelist": ["amfgkafnmffofhmicmeddphgmdglambl;http://127.0.0.1:8780/update.xml"] }
```

No `ExtensionInstallSources`, `ExtensionAllowedTypes` or domain-join was needed on Linux, and plain `http://` on 127.0.0.1 was accepted.

### Evidence

From `evidence/chrome-first-install.events.log`. About 1 s after start, Chrome fetched:

```
GET /update.xml?os=linux&arch=x64&prod=chromecrx&prodchannel=&prodversion=156.0.8078.79&lang=en-US&acceptformat=crx3,puff&x=id%3Damfgkafnmffofhmicmeddphgmdglambl%26v%3D0.0.0.0%26installsource%3Dnotfromwebstore%26installedby%3Dpolicy%26uc
GET /stileaf.crx
```

The service worker then sent `hello` (extId `amfgkafnmffofhmicmeddphgmdglambl`). Edge behaved the same (`prod=chromiumcrx`).

Further details:
- Chrome logged `force_installed_metrics.cc: All forced extensions seem to be installed`.
- Profile `Preferences`: `extensions.settings.<id>.location = 7` (EXTERNAL_POLICY_DOWNLOAD), `disable_reasons: []`, `from_webstore: false`.
- After several restarts of the same profile (soak profiles), the extension connected again every time.

**Launch flags used:**

```
--headless=new --user-data-dir=<dir> --no-first-run --no-default-browser-check --password-store=basic --disable-gpu
```

Optional: `--remote-debugging-port`, `--enable-logging=stderr`.

The task asked to check whether headless honors policy at all. Headless honored policy fully: forcelist, ManagedBookmarks and ManagedFavorites all took effect. No Xvfb was available. A headful run was not done because it was not needed. The container will run headful under Xvnc, which should only be easier.

### CRX3 packing details (`crx3.py`)

The file layout is:

```
"Cr24" | uint32le 3 | uint32le header_len | CrxFileHeader | zip
```

`CrxFileHeader` is hand-encoded protobuf:
- field 2, `sha256_with_rsa`: `AsymmetricKeyProof { 1: public_key = DER SubjectPublicKeyInfo, 2: signature }`
- field 10000, `signed_header_data`: serialized `SignedData { 1: crx_id }`, where `crx_id = sha256(SPKI DER)[:16]`

Field tags as varints: `0x12` for field 2 and `0x82 0xF1 0x04` for field 10000, wire type 2.

Signature: RSA PKCS#1 v1.5 with SHA-256, computed with `openssl dgst -sha256 -sign key.pem` over:

```
b"CRX3 SignedData\x00" | uint32le(len(signed_header_data)) | signed_header_data | zip_bytes
```

- **Extension ID** = hex of `crx_id`, with digits mapped `0-f` to `a-p`.
- **Key:** `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048` (PKCS#8 PEM).
- **Zip:** deterministic, with sorted entries and a fixed date.

Chrome 156 and Edge 156 both accepted the result. Neither `chrome --pack-extension` nor the `cryptography` package is needed (the venv does not have `cryptography`).

Update manifest (gupdate XML, served at `/update.xml` with any query string):

```xml
<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
  <app appid='<id>'><updatecheck codebase='http://127.0.0.1:8780/stileaf.crx' version='0.0.3' /></app>
</gupdate>
```

The CRX was served as `application/x-chrome-extension` via GET.

### Surprise: self-hosted updates need `update_url` in the manifest

- The forcelist URL is used for the **initial install only**.
- With no `update_url` in `manifest.json`, the updater (`UpdateService`) sent later checks to the **Chrome Web Store** (`update.googleapis.com/service/update2/json`, seen in a NetLog). Our server never saw a request.
- In that state, `runtime.requestUpdateCheck()` returned `no_update`. Neither a restart nor changing the forcelist URL (`...update.xml?v=0.0.4`) triggered an update.

With `"update_url": "http://127.0.0.1:<port>/update.xml"` in the manifest, the on-demand update works on **both Chrome and Edge** (`evidence/update-{chrome,edge}.events.log`):
1. The orchestrator serves a new version.
2. The extension calls `chrome.runtime.requestUpdateCheck()`.
3. Chrome GETs `update.xml` and then the CRX.
4. `runtime.onUpdateAvailable` fires, and the handler calls `runtime.reload()`.
5. A new `hello` arrives with the new version. Steps 2 to 5 took under 1 s.

### Surprise: serve the update manifest with `Cache-Control: no-store`

Python's `SimpleHTTPRequestHandler` sends `Last-Modified`. This can let Chromium heuristically cache `update.xml`. Send `Cache-Control: no-store`.

### Surprise: the Chromium UA string is reduced

The UA string reads `Chrome/156.0.0.0` / `Edg/156.0.0.0`. For `hello.browserVersion`, use `navigator.userAgentData.getHighEntropyValues(["fullVersionList"])`. That gives "Google Chrome 156.0.8078.79" and "Microsoft Edge 156.0.4314.58".

### Permissions

`"permissions": ["bookmarks", "alarms"]` is enough. **No `host_permissions` were needed** for the service worker to open `ws://127.0.0.1:<port>/`.

## Spike 2: bookmarks tree fields (Chrome / Edge / Firefox)

These profiles were fresh and **not signed in**. JSON dumps are in `evidence/tree-*.json`.

### Chrome 156 and Edge 156

The fields are the same in both. Every node has `id`, `title`, `dateAdded`, `syncing` (boolean, on the root and every node). Non-root nodes also have `parentId` and `index`. Folders have `dateGroupModified` once modified, and bookmarks have `url`.

| Root | id | `folderType` | Chrome title | Edge title | Notes |
|---|---|---|---|---|---|
| (tree root) | `"0"` | none | `""` | `""` | Has `syncing: false` and no `folderType`. |
| bar | `"1"` | `"bookmarks-bar"` | Bookmarks bar | Favorites bar | |
| other | `"2"` | `"other"` | Other bookmarks | Other favorites | |
| mobile | `"3"` | `"mobile"` | Mobile bookmarks | (Mobile favorites) | **Hidden from `getTree()` while empty.** It appeared (index 2) once it had a child. Seeded via the on-disk `Bookmarks` file; Chrome tested. |
| managed | **varies** (`4`, then `101` on a later start; `6` in Edge) | `"managed"` | ManagedBookmarks `toplevel_name` | ManagedFavorites `toplevel_name` | It and its children have `unmodifiable: "managed"`. **Its IDs are reassigned on each start.** Edge needs the policy name `ManagedFavorites`; `ManagedBookmarks` is ignored by Edge. |

Further observations:
- `syncing` is `false` everywhere when not signed in.
- **No account roots** (duplicate bar/other/mobile with `syncing: true`) are exposed when signed out. Only one set of local roots appears.
- What appears after sign-in with account bookmark storage could not be tested (spike 5). Expect a second set of roots with the same `folderType` and `syncing: true`. Design for **several roots per `folderType`**.
- Edge's on-disk file also has a hidden `workspaces_v2` root (id 5), which the API never exposes. This is why Edge's node IDs start higher.
- `bookmarks.create` without `parentId` lands in `other` on both browsers. Creating in the bar (`"1"`) and in other (`"2"`) works.

### Firefox Developer Edition 160

There is **no `folderType`, `syncing` or `unmodifiable`**. Every node has `type` (`"folder"` / `"bookmark"` / `"separator"`). Roots are identified by fixed GUID-style IDs:

| Root | id | title | index under root |
|---|---|---|---|
| (tree root) | `root________` | `""` | (none) |
| menu | `menu________` | Bookmarks Menu | 0 |
| toolbar | `toolbar_____` | Bookmarks Toolbar | 1 |
| other | `unfiled_____` | Other Bookmarks | **3** |
| mobile | `mobile______` | Mobile Bookmarks | 4 (always present, even when empty) |

- Index 2 is skipped. It is the hidden `tags________` root.
- Node IDs are 12-character GUIDs.
- Creating in `mobile______` works. `create` without `parentId` goes to `unfiled_____`.
- **First-run race:** the first `getTree()` (right after `hello`) showed empty roots. About 1.5 s later Firefox created its default bookmarks: a "Mozilla Firefox" folder with 4 links in the menu. These arrived as `onCreated` events.

## Spike 3: MV3 service worker lifetime with a WebSocket

The server sent an application-level `{"type":"ping"}` every 20 s, and the extension answered with `pong`. Library-level WebSocket pings were disabled. All runs were headless and used fresh profiles.

| Run | Duration | Result |
|---|---|---|
| **Chrome, pings every 20 s + 30 s alarm** (production config) | 32 min | 95 pongs, max gap 21 s, **0 disconnects**, `connects: 1`, worker uptime 1902 s (one worker lifetime) |
| **Edge, same config** | 32 min | Same: 95 pongs, max gap 21 s, 0 disconnects, uptime 1900 s |
| Chrome + Edge, **pings, no alarm** | 15 min | 0 disconnects, one worker each (uptime 880 s) |
| Chrome, **no pings, no alarm** | 15 min | **Disconnected at exactly 30 s** (idle worker killed). It never came back because nothing woke it. |
| Chrome, **alarm only (30 s), no pings** | 32 min | Worker killed and restarted 3 times (at 19 min, 20 min and 28.5 min; new `workerStartedAt` each time). The alarm reconnected within about 1 s each time. |
| Firefox Developer Edition, MV2 persistent background page, pings | 12 min | 0 disconnects. Note: this run used the `autoconfig` method. |

Evidence: `soak-chrome-edge-32min.events.log`, `control-*.events.log`, `soak-firefox-12min.events.log`.

The 20 s WebSocket traffic resets the 30 s idle timer, and the worker stays alive. Keep the `alarms` reconnect anyway (period 30 s works; Chrome's minimum is 30 s). Without pings the worker is still restarted periodically, and the alarm recovers it.

## Spike 4: Firefox Developer Edition, unsigned XPI force-install

**Verdict: works.** Each override method works on its own.

`<install>/distribution/policies.json`:

```json
{ "policies": { "ExtensionSettings": { "spike@stileaf.local": {
    "installation_mode": "force_installed",
    "install_url": "file:///path/to/stileaf.xpi" } } } }
```

Manifest: MV2, `"background": {"scripts": [...], "persistent": true}`, `"permissions": ["bookmarks", "alarms"]`, `"browser_specific_settings": {"gecko": {"id": "spike@stileaf.local", "strict_min_version": "115.0"}}`. The XPI is a plain zip of the files with no signature. No host permission was needed for `ws://127.0.0.1`.

| Signature override | Result |
|---|---|
| none (control) | Not installed. No connection, and the add-on is absent from `extensions.json`. |
| (b) profile `user.js`: `user_pref("xpinstall.signatures.required", false);` | **Works.** hello, getTree and creates all succeeded. |
| (a) autoconfig only | **Works.** Same evidence. |
| Release Firefox 159 + `/etc/firefox/policies/policies.json` + `user.js` (control) | Not installed. This is expected: release builds ignore the pref. |

The autoconfig files are:
- `<install>/defaults/pref/autoconfig.js`:
  ```js
  pref("general.config.filename", "mozilla.cfg");
  pref("general.config.obscure_value", 0);
  ```
- `<install>/mozilla.cfg` (the first line must be a comment):
  ```js
  // mozilla.cfg
  lockPref("xpinstall.signatures.required", false);
  ```

Installed add-on state (`extensions.json`): `active: true`, `signedState: 0` (unsigned), `location: "app-profile"`, `installTelemetryInfo.source: "enterprise-policy"`. It survived restarts.

**Updates:**
- With a new XPI version at the **same** `install_url`, Firefox started the old version, then installed the new one during the same startup. Two hellos were seen: 0.0.2, then 0.0.3.
- A **new** `install_url` path installs the new version directly.

Launch: `firefox --headless --no-remote --profile <dir>`. The env var `MOZ_CRASHREPORTER_DISABLE=1` is set by `run_firefox.sh`, but runs without it were not tried.

## Spikes 5 and 6

Not run. Sign-in and vendor sync need real accounts and interactive login (spike 5). The Android check needs the user (spike 6). The Chrome profile does already contain an `EncryptedBookmarks2` file next to `Bookmarks` without sign-in. This is irrelevant to the API, but don't edit `Bookmarks` on disk; editing worked here, but it is fragile.

---

## Recommended design changes

1. **§6.2 Chrome/Edge manifest:** add `"update_url": "http://127.0.0.1:<port>/ext/update.xml"` to the Chromium manifest. Without it, upgrades never arrive from the self-hosted URL.
2. **§6.1 protocol:** add `updateCheck`. The extension calls `runtime.requestUpdateCheck()` and then `runtime.reload()` on `onUpdateAvailable`. Use it after the orchestrator ships a new CRX version, and compare `hello.extVersion` against the expected version. (Firefox: change the `install_url` path or restart.)
3. **Serve `update.xml` and the CRX with `Cache-Control: no-store`.**
4. **Keepalive confirmed:** a 20 s app-level ping keeps one worker alive for at least 32 min. Keep the 30 s alarm plus backoff reconnect, because the worker can still be recycled. No `host_permissions` are needed.
5. **§5.6 root mapping:**
   - Chromium roots: map by `folderType` (`bookmarks-bar`, `other`, `mobile`; ignore `managed`), not by ID or title. Titles differ ("Favorites bar" in Edge) and are localized.
   - Expect a missing `mobile` root (hidden while empty). An empty or hidden mobile root cannot be written to through the API (see the follow-up spike at the end).
   - Expect possibly several roots with the same `folderType` and `syncing: true|false` once signed in. Treat `syncing` as part of the root key.
   - Firefox: map by fixed IDs (`toolbar_____`, `menu________`, `unfiled_____`, `mobile______`). The `menu` root has no Chromium equivalent, which fits the planned `menu` projection.
6. **Exclude `folderType: "managed"` / `unmodifiable` nodes** from sync. Their IDs change on every browser start.
7. **Firefox first run:** default bookmarks appear about 1.5 s after the extension starts. The startup sync should wait for bookmark events to go quiet, or re-fetch `getTree`, before the first-run merge. Otherwise it will see an empty tree and then a burst of creates.
8. **`hello.browserVersion`:** get it from UA-CH `fullVersionList` on Chromium, and from `runtime.getBrowserInfo()` on Firefox.
9. **Firefox override:** either method works. Prefer **autoconfig** (`lockPref`) because it is profile-independent, locked, and lives next to `distribution/policies.json` in the install dir the container already controls. Adding `user.js` as well is harmless.
10. **Dev and test tooling:** to test policies without root, run the overlay over `/etc` plus the nested user namespace (`run_chromium.sh`). Pass `HOME`/`XDG_*` to keep crashpad out of the real home.

---

## Follow-up spike: the hidden (empty) mobile root in Chrome / Edge

The runs used Chrome 156.0.8078.79 and Edge 156.0.4314.58, both headless with fresh profiles, the same force-installed extension and a generic `{"type":"call","method":...,"args":[...]}` hook.

Scripts and evidence:
- Script: `test_mobile_root.sh chrome|edge BIN WORK [SCRIPT] [fresh|seed|reuse] [TAG]`.
- Request scripts: `script-mobile-root.json` (empty root), `script-mobile-seeded.json` (root visible because a child was seeded into the on-disk `Bookmarks` file), and `script-mobile-restart.json`.
- Evidence: `evidence/mobile-root-{chrome,edge}-{fresh,seed,restart}.json`.

**Chrome and Edge behaved identically in every step.**

| Question | Result |
|---|---|
| (1) `create({parentId:"3", ...})` while the root is empty/hidden | **Fails**: `Can't find parent bookmark for id.` The root does not appear in `getTree`. Ways to make it visible: something outside the API adds a child, i.e. vendor sync from a phone, or editing the on-disk file. |
| (2) `get("3")`, `getChildren("3")` or `getSubTree("3")` while hidden | **All fail**: `Can't find bookmark for id.` A hidden root does not exist as far as the API is concerned. |
| (3) Removing the last child of a visible mobile root | **It disappears immediately** from `getTree` in the same session. `get("3")` and `create` into it then fail too. An **`onRemoved` event fires for id `"3"`** (the root itself). It stays hidden after a restart. |
| (4) `move(id, {parentId:"3"})` | **Fails** while hidden (`Can't find parent bookmark for id.`). While it is visible, `create` into it works (step 1 of the seeded run). `move` into a *visible* mobile root was not run explicitly, but it is expected to behave like `create`. |

While the root is visible, `get("3")` returns `{id:"3", parentId:"0", folderType:"mobile", index:2}`. The title is "Mobile bookmarks" in Chrome and "Mobile favorites" in Edge.

Side finding: Chromium writes the `Bookmarks` file about 2.5 s after the last change. If the browser is killed sooner, recent API changes are lost on the next start. This first caused a false "visible after restart" result here. The production container should stop browsers gracefully. The orchestrator should not assume an applied op survives a hard kill, so it should re-read state after a browser restart.

### Design implications
- **Stileaf cannot write into an empty Chromium mobile root.** Chromium mobile roots can be targets only while non-empty. When a mapping targets an empty or missing `mobile` root, the engine must either:
  - treat it as read-only and absent, and hold or redirect the ops (e.g. into a "Mobile" subfolder of `other`), or
  - keep a placeholder child there, which is impossible to create from the API anyway.

  This matters for the §13 `mobile` default. `mirror` into Chromium's mobile root only works while it already has content. `inbox → toolbar`/`other` is the safe default.
- Deleting the last mobile bookmark through Stileaf makes the root vanish mid-cycle, with an `onRemoved` for `"3"`. The adapter must not treat that as "root deleted". Model the root as present-but-empty. Treat later ops targeting it as failing, not as a conflict.
- Firefox is unaffected: `mobile______` is always present and writable (spike 2).
