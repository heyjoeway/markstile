<p align="center"><img src="orchestrator/markstile/portal/static/icon-256.png" alt="MarkStile logo: a pixel-art stone stile" width="128" height="128"></p>

# MarkStile

*Bookmark + stile*: a way over the walls between browser vendors' bookmark sync.

Two-way bookmark sync across **Google Chrome, Microsoft Edge, Brave, Vivaldi, Opera and Firefox**, phones included, without reverse-engineering any vendor protocol.

Each vendor's sync service is a walled garden, so MarkStile doesn't try to talk to them. A self-hosted container runs real desktop copies of the browsers instead. You sign each one into its vendor account once, through a web console. A tiny extension in each browser exposes its bookmarks to an orchestrator, which keeps their trees in agreement. Each vendor's own sync then carries the changes to your other devices.

```
 Phone (Edge)      ⇄ Microsoft ⇄ [Edge in container]    ┐
 Phone (Chrome)    ⇄ Google    ⇄ [Chrome in container]  ├─ extension ⇄ WebSocket ⇄ orchestrator ⇄ web portal
 Phone (Brave)     ⇄ Brave     ⇄ [Brave in container]   │
 Phone (Vivaldi)   ⇄ Vivaldi   ⇄ [Vivaldi in container] │
 Phone (Opera)     ⇄ Opera     ⇄ [Opera in container]   │
 Desktop (Firefox) ⇄ Mozilla   ⇄ [Firefox in container] ┘
```

MarkStile stores no credentials. Sign-in sessions live inside the browser profiles on your data volume.

> [!WARNING]
> **Do not expose MarkStile to the internet.** The container holds signed-in Google, Microsoft and Mozilla sessions. Reach the portal over your LAN, over [Tailscale](https://tailscale.com/), or through a reverse proxy that terminates TLS. MarkStile does not do HTTPS itself.

## Quick start

Requirements: Docker on **x86-64** (the Chromium-based browsers ship x86-64-only Linux builds), about 2 GB of RAM, and a few GB of disk.

```bash
docker compose up -d --build
```

Or without Compose:

```bash
docker build -f docker/Dockerfile -t markstile .
docker run -d --name markstile -p 8080:8080 --shm-size=1g --stop-timeout 30 -v markstile-data:/data markstile
```

Then open `http://<host>:8080` and:

1. **Set the portal password.** Whoever opens the portal first sets it, so do this right away. The container holds no data before you sign in.
2. **Choose your browsers.** Only the ones you tick are started and synced. You can change this later on the **Browsers** page.
3. **Console:** sign in to each browser with its vendor account, and turn on bookmark sync. Switch between the browser windows with the taskbar at the bottom of the console. Ctrl+V (Cmd+V on a Mac) pastes text from your computer. Firefox shows its own welcome and terms dialog on the first start; MarkStile leaves accepting it to you. Brave has no account: on another device open Brave's **Settings → Sync**, add a new device to show its sync code words, then in the console's Brave choose **I have a Sync Code** and paste them. Vivaldi opens its own setup wizard on the first start; click through it.
4. **Wait** until each browser's bookmarks have finished downloading. The dashboard shows live counts.
5. **First run:** choose how to combine the browsers.
   - **Union** keeps everything from every browser, de-duplicated by folder path, title and URL.
   - **Use one browser as the source**: the others are made to match it. The page shows how many bookmarks each would lose.

   Nothing syncs until you choose.

**Adding a browser later** starts it and puts it on the dashboard as *waiting to join*. Sign in to it in the console first, then click **Start syncing**: its bookmarks are merged with the others and nothing is deleted. **Removing a browser** closes it, stops syncing it and erases its profile in the container (bookmarks, sign-in and settings there), after you confirm. Your other browsers and your vendor account keep their bookmarks. Adding it back later starts a fresh profile that you sign in to again.

After that, changes in any browser, or on any device that syncs to one, reach all the others. A sync runs about 5 seconds after bookmark activity settles (30 seconds at most during a long burst), every 5 minutes as a safety net, and when you click **Sync now**.

### Prebuilt image

GitHub Actions publishes the image to `ghcr.io/heyjoeway/markstile`, tagged `latest` and with the build date (for example `2026.11.05.42`). A new image is built after every change to `main`, and once a day if any browser or the Ubuntu base image has a new release; days without one publish nothing. Use it instead of building:

```bash
docker run -d --name markstile -p 8080:8080 --shm-size=1g --stop-timeout 30 -v markstile-data:/data ghcr.io/heyjoeway/markstile:latest
```

### TrueNAS SCALE

MarkStile isn't in the TrueNAS app catalog, so it installs as a custom app (TrueNAS SCALE 24.10 or later):

1. **Create a dataset** for its data, for example `POOL/apps/markstile` (**Datasets → Add Dataset**, preset *Generic*). The container makes itself the owner (UID 1000) on its first start.
2. Go to **Apps → Discover Apps**, open the **⋮** menu and choose **Install via YAML**. Name the app `markstile`.
3. Paste [`deploy/truenas.yaml`](deploy/truenas.yaml), with your dataset path and the newest image tag filled in, and click **Save**.
4. Open `http://<truenas-address>:8080` and continue with step 1 of the quick start above.

If port 8080 is taken on your NAS, change the number on the left of `8080:8080`. To update, edit the app, change the image tag to a newer one, and save.

## How it works

- **Canonical tree `K`.** The orchestrator keeps one browser-neutral bookmark tree (roles, folders, titles, URLs, separators, order). Each version is stored in SQLite.
- **Change detection by snapshot diffing.** After each successful sync, MarkStile saves every browser's tree with that browser's own IDs. On the next sync it diffs each browser's current tree against its snapshot to find creates, deletes, edits and moves. Bookmark events only trigger syncs; no state is derived from them.
- **Applied by content, not ID.** Changes are merged into `K` by matching content, and then each browser is *reconciled* to `K` with a minimal content diff (creates, moves, renames, removals). No cross-browser ID mapping table exists. A browser that was offline simply catches up, and echo loops can't happen: applying `K` and re-reading gives no diff.
- **Conflicts.** An edit beats a delete. Identical bookmarks created in two browsers at once are de-duplicated. Two different edits to the same item are settled by a configurable browser priority (default: the order the changes arrived), and every conflict is logged.

### Root folders

| `K` role | Chrome / Edge / Brave / Vivaldi / Opera | Firefox |
|---|---|---|
| `toolbar` | Bookmarks bar / Favorites bar | Bookmarks Toolbar |
| `other` | Other bookmarks / Other favorites | Other Bookmarks |
| `mobile` | Mobile bookmarks / Mobile favorites | Mobile Bookmarks |
| `menu` | a folder, default `Other bookmarks/Bookmarks Menu` | Bookmarks Menu |

Each root is either **mirror** (synced 1:1) or an **inbox** with a destination such as `toolbar` or `other/From phone`. Anything that appears in an inbox root is moved to the destination everywhere. By default `toolbar`, `other` and `menu` mirror, and `mobile` is an inbox to `other/From phone`: bookmarks you save on a phone land in a folder every browser shows (including the phone, after vendor sync). Existing Mobile bookmarks are moved there on the first sync. You can switch `mobile` to mirror in Settings.

Roots are identified by API metadata (`folderType`, Firefox's fixed root IDs), never by their localized titles.

### Trimmed-down browsers

The browsers in the container exist only to sync bookmarks, so managed policies (written in `provision.py`) turn off everything else that costs CPU, memory or network:

- **Browsers start on a blank page, and new tabs are blank** in Chrome, Edge, Brave and Vivaldi: no news, weather, backgrounds, shortcuts, sponsored tiles or stories. Opera starts on a blank page too, but its new tabs always open its Speed Dial; no policy or setting changes that.
- **Off:** search suggestions, translation, spell check, network prediction, casting, shopping and rewards features, Copilot/AI Mode/Leo and other AI features, sidebars, promotions, surveys, telemetry and studies. In Brave also: Rewards, Wallet, VPN, News, Talk, Tor windows and its usage pings. In Vivaldi also: its VPN.
- **No password, address or card storage.**
- **Vendor sync carries bookmarks only (the Chromium-based browsers).** Passwords, history, tabs, extensions, settings and other data types are switched off for the container. This is per device, so your other devices keep syncing them as before.
- **Opera's and Vivaldi's own extras stay.** Opera's Speed Dial wallpaper, messenger sidebar and AI button, and Vivaldi's panels, have no policies. Turn them off in each browser's settings if you like. Opera's sponsored content can only be turned off in Opera itself, because Opera signs those settings and resets them if anything else edits them: in `opera://settings`, search for *promoted* and turn off **Show Promoted Suggestions**, then on the Speed Dial page open **Easy setup** (the ☰ button at the top right) and turn off **Show Speed Dial suggestions**.
- **Firefox sync options are left alone.** Turning a Firefox Sync engine off is recorded on your account and can switch it off on your other devices too, so leave the "Choose what to sync" options as they are when signing in to Firefox in the container. Firefox's policy-set preferences are never uploaded to your account.

### Vendor default bookmarks

Vivaldi fills a new profile with sponsored bookmarks (Speed Dial folders and partner links), and Firefox adds a "Mozilla Firefox" folder to its Bookmarks Menu. The first time MarkStile meets one of these browsers, it checks whether the profile holds nothing but the vendor's own links. If so, it removes them before anything syncs. Vivaldi puts them in its Trash. This happens once per browser. Anything that arrives later, for example from your Vivaldi account after you sign in, is kept. Chrome, Edge, Brave and Opera start with no bookmarks.

### Browser restarts

Browsers slowly gather memory when they run for days, so MarkStile restarts them once a day by default (Settings → Browsers; 0 turns it off). A restart waits until nobody has used the console for 5 minutes, and runs between sync cycles, one browser at a time. The extensions reconnect on their own, and anything that changed meanwhile syncs right after.

## Safety

- **Mass-deletion hold.** If a browser deletes more than 20% of its bookmarks or more than 50 of them, or its tree becomes empty, its changes are held and nothing is spread. The dashboard offers **Reject** (restore that browser from `K`) or **Approve** (delete everywhere). Common false alarms are sign-outs, profile resets and vendor-sync hiccups. Both thresholds are configurable.
- **History.** Every change to `K` is kept with a summary: changes per browser, conflicts, and items moved out of inboxes. By default versions are kept for 30 days, plus always the last 100. Any version can be exported as a standard bookmarks HTML file, or restored after confirmation. A restore reconciles every browser to that version and bypasses the deletion hold.

## Configuration

Everything is configured in the portal. Environment variables are optional:

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Portal port (the only exposed port). |
| `MARKSTILE_BROWSERS` | — | Comma-separated browsers to run and sync (`chrome`, `edge`, `brave`, `vivaldi`, `opera`, `firefox`), to skip the portal's browser choice on a fresh install. Ignored once a choice is saved. |
| `SYNC_INTERVAL` | `5m` | Initial scheduled sync interval (`300`, `90s`, `5m`, `1h`). The portal setting overrides it afterwards. |
| `PORTAL_RESET` | — | Change its value to reset the portal password (see below). |
| `PORTAL_RESET_TOKEN` | `true` | Require the one-time token from the container log for a reset. |
| `TZ` | `UTC` | Time zone for log timestamps. |
| `SECURE_COOKIES` | `false` | Set to `true` when serving the portal over HTTPS via a proxy. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` headers. |

### Changing the portal password

**Settings → Password** changes it if you know the current one. Every other portal session is logged out; yours stays logged in. If you've forgotten it, reset it as described below.

### Resetting the portal password

Set `PORTAL_RESET` to any new value and restart the container. The container log prints a one-time token:

```
markstile: PORTAL_RESET changed: portal password reset is enabled. One-time reset token: 3F9A1C07
```

The next visit to the portal asks for that token and a new password. Existing portal sessions are signed out. Setting the same value again later does nothing; change it again to trigger another reset.

## Limitations

- **Firefox tags and keywords are not synced.** The WebExtension bookmarks API does not expose them.
- **Separators** exist only in Firefox. The Chromium-based browsers can't show them; MarkStile keeps them in `K` and in Firefox.
- **Firefox Developer Edition** is used because it can run an unsigned extension. Your Firefox account syncs with it like any other Firefox.
- **Only bookmarks** are synced (no history, passwords or tabs).
- **x86-64 only.**
- **Sign-in is manual**, through the console. MarkStile never automates vendor sign-in, and the browsers run without automation or remote-debugging flags.
- **Chrome's local-only bookmarks.** Recent Chrome can keep separate account and local-only bookmark trees. MarkStile reads and writes the account-synced roots.
- **Vivaldi's top bookmark folder is its bookmark-bar root**, so folders you keep there (including Speed Dial folders) appear on the other browsers' bookmarks bars.
- **Chromium-based browsers hide an empty Mobile bookmarks folder**, and while it's hidden their bookmarks API can neither read nor write it. If you switch `mobile` to `mirror`, mobile bookmarks from other browsers reach a Chromium browser only once its own Mobile folder has something in it. The default inbox mode avoids this.
- **The menu folder in the Chromium-based browsers** (`Bookmarks Menu` by default) is structural. If you delete or rename it, it is recreated.

## Troubleshooting

- **A browser's card says "not connected".** Its extension connects within a few seconds of the browser starting. If a browser window was closed it comes back automatically. On a container's very first start, Chrome sometimes holds back the extension's settings until it restarts; MarkStile notices and restarts that browser once by itself (shown in **Logs**). Otherwise check **Logs** and `docker logs`.
- **Keyboard shortcuts in the console** such as Ctrl+D may be caught by your own browser before they reach the remote one. Use the browser's menus or toolbar buttons instead.
- **The console clipboard is one-way.** Ctrl+V pastes text from your computer into the console; copying from the console back to your computer isn't supported.
- **Extension updates.** After you upgrade the image, the extension updates itself: Firefox installs the new version when it starts, and the Chromium-based browsers check for it each time their extension connects (at most every 10 minutes).

## Security notes

- Only the portal port is exposed. VNC (`127.0.0.1:5901`), the extension WebSocket and the extension update server (`127.0.0.1:8765`) bind to localhost inside the container.
- The VNC password is regenerated on every start. The portal proxies VNC through its own authenticated session, so you never see or need it.
- Each extension authenticates to the orchestrator with a shared secret. The secret is regenerated on every start and delivered through managed browser policy (`3rdparty` extension configuration).
- The Chromium extension is packed and signed at first start with a key generated in your volume (`/data/keys/extension.pem`). Nothing depends on a maintainer's key or a hosted service.
- The portal password is hashed with Argon2. Sessions are server-side, cookies are `HttpOnly` and `SameSite=Strict`, every form is CSRF-protected, and login attempts are rate-limited.
- The Chromium-based browsers keep their sandbox when the container runtime allows user namespaces. Otherwise they fall back to `--no-sandbox` and say so in the container log.

## Data volume

```
/data
  profiles/<browser>/                                browser profiles (sign-ins persist here)
  home/                                              the browsers' $HOME
  state.db                                           settings, auth, versions of K, snapshots, holds, logs
  keys/extension.pem                                 CRX signing key, generated on first start
```

Back up the volume to keep both your sign-ins and your history.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e 'orchestrator[test]'
cd orchestrator && ../.venv/bin/pytest                         # unit, property and portal tests
HYPOTHESIS_PROFILE=stress ../.venv/bin/pytest tests/test_properties.py
../.venv/bin/markstile demo                                       # portal on 127.0.0.1:8080 with three fake browsers
python3 extension/build.py build/extension                      # unpacked Chrome extension + firefox.xpi
```

Project layout:

```
orchestrator/markstile/
  engine/      pure sync engine: tree model, lift/project, matching, content diff (reconcile), merge, safety, cycle planning
  adapters/    WebSocket bridge to the extensions (+ CRX update server), fake in-memory browser
  sync.py      fetch → plan → apply → verify → commit
  scheduler.py debounce, interval, manual and startup triggers
  store.py     SQLite
  portal/      FastAPI app, templates, auth, noVNC proxy
  crx.py, provision.py   key generation, CRX3 packing, browser policies
extension/     shared background script; Chromium MV3 and Firefox MV2 manifests; build.py
docker/        Dockerfile, entrypoint, supervisord, openbox
```

`DESIGN.md` has the full design and the reasoning behind it.

## License

[MIT](LICENSE)
