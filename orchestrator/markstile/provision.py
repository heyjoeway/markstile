"""Container-start provisioning (run as root by the entrypoint, before the
browsers start): signing key, CRX + update manifest, a fresh extension shared
secret, and managed policies for each browser."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
from pathlib import Path

from . import crx

FIREFOX_EXT_ID = "markstile@markstile.local"
# IDs the extension had under earlier project names; blocking them removes old copies.
FIREFOX_OLD_EXT_IDS = ("stileaf@stileaf.local", "stilite@stilite.local")

# The container only syncs bookmarks: turn off new-tab content, suggestions,
# telemetry, AI features, password/autofill storage and every other sync type.
# Checked in chrome://policy and edge://policy: no errors (both mark the two
# Autofill policies deprecated but still apply them).
CHROMIUM_EXTRA_POLICIES = {
    "RestoreOnStartup": 1,
    "NewTabPageLocation": "about:blank",
    "TranslateEnabled": False,
    "SpellcheckEnabled": False,
    "SearchSuggestEnabled": False,
    "AlternateErrorPagesEnabled": False,
    "NetworkPredictionOptions": 2,
    "EnableMediaRouter": False,
    "PasswordManagerEnabled": False,
    "AutofillAddressEnabled": False,
    "AutofillCreditCardEnabled": False,
    "PaymentMethodQueryEnabled": False,
    "HardwareAccelerationModeEnabled": False,
    "HighEfficiencyModeEnabled": True,
    "UserFeedbackAllowed": False,
}
CHROME_EXTRA_POLICIES = {  # unknown to Edge on Linux; Brave, Vivaldi and Opera know them all
    "DefaultBrowserSettingEnabled": False,
    "MetricsReportingEnabled": False,
    "BackgroundModeEnabled": False,
    "NTPCustomBackgroundEnabled": False,
    "PromotionsEnabled": False,
    "SyncTypesListDisabled": [
        "autofill", "extensions", "preferences", "passwords", "productComparison",
        "readingList", "savedTabGroups", "tabs", "themes", "typedUrls", "cookies",
    ],
    "NTPCardsVisible": False,
    "ShoppingListEnabled": False,
    "UrlKeyedAnonymizedDataCollectionEnabled": False,
    "FeedbackSurveysEnabled": False,
    "BrowserLabsEnabled": False,
    # GenAiDefaultSettings only applies from cloud management: set each feature
    "AIModeSettings": 1,
    "HelpMeWriteSettings": 2,
    "HistorySearchSettings": 2,
    "TabCompareSettings": 2,
    "CreateThemesSettings": 2,
    "DevToolsGenAiSettings": 2,
    "AutofillPredictionSettings": 2,
}
EDGE_EXTRA_POLICIES = {
    "HideFirstRunExperience": True,
    "AutoImportAtFirstRun": 4,
    "ShowRecommendationsEnabled": False,
    "SyncTypesListDisabled": [
        "settings", "passwords", "addressesAndMore", "extensions", "history", "openTabs",
        "edgeWallet", "collections", "apps",
    ],
    "NewTabPageContentEnabled": False,
    "NewTabPageQuickLinksEnabled": False,
    "NewTabPageHideDefaultTopSites": True,
    "NewTabPageAllowedBackgroundTypes": 3,
    "PersonalizationReportingEnabled": False,
    "HubsSidebarEnabled": False,
    "Microsoft365CopilotChatIconEnabled": False,
    "CopilotPageContext": False,
    "ComposeInlineEnabled": False,
    "EdgeShoppingAssistantEnabled": False,
    "ShowMicrosoftRewards": False,
    "ShowAcrobatSubscriptionButton": False,
    "EdgeEDropEnabled": False,
    "EdgeWorkspacesEnabled": False,
    "QuickSearchShowMiniMenu": False,
    "VisualSearchEnabled": False,
    "MicrosoftEditorProofingEnabled": False,
    "ResolveNavigationErrorsUseWebService": False,
    "TabServicesEnabled": False,
    "SleepingTabsEnabled": True,
    "EfficiencyModeEnabled": True,
}
# Brave's own features.  Brave Sync honours SyncTypesListDisabled like Chrome
# (it is the same per-device setting).
BRAVE_EXTRA_POLICIES = {
    **CHROME_EXTRA_POLICIES,
    "BraveRewardsDisabled": True,
    "BraveWalletDisabled": True,
    "BraveVPNDisabled": True,
    "BraveAIChatEnabled": False,
    "BraveNewsDisabled": True,
    "BraveTalkDisabled": True,
    "TorDisabled": True,
    "BraveP3AEnabled": False,
    "BraveStatsPingEnabled": False,
    "BraveWebDiscoveryEnabled": False,
    "BraveSpeedreaderEnabled": False,
    "BraveWaybackMachineEnabled": False,
}
# Vivaldi's and Opera's own extras (Opera's Speed Dial, sidebar and AI) have no
# policies; Opera has no policy page either, but applies the Chromium ones.
# Both open their start page when they start (Vivaldi even for "open the new tab
# page"), so they start on about:blank instead.  Vivaldi's new tabs are set by
# markstile.prefs; Opera's always show its Speed Dial.
BLANK_STARTUP = {"RestoreOnStartup": 4, "RestoreOnStartupURLs": ["about:blank"]}
VIVALDI_EXTRA_POLICIES = {**CHROME_EXTRA_POLICIES, **BLANK_STARTUP, "VivaldiVPNEnabled": False}
OPERA_EXTRA_POLICIES = {**CHROME_EXTRA_POLICIES, **BLANK_STARTUP}
# Managed policy directory of each Chromium browser, relative to /etc.
CHROMIUM_POLICY_DIRS = {
    "chrome": ("opt/chrome", CHROME_EXTRA_POLICIES),
    "edge": ("opt/edge", EDGE_EXTRA_POLICIES),
    "brave": ("brave", BRAVE_EXTRA_POLICIES),
    "vivaldi": ("vivaldi", VIVALDI_EXTRA_POLICIES),
    "opera": ("opt/opera", OPERA_EXTRA_POLICIES),
}
# Firefox sync engines are left alone: declining one is recorded account-wide
# and would turn it off on the user's other devices too.  Policies only set
# default/locked prefs, which Firefox Sync never uploads.
FIREFOX_EXTRA_POLICIES = {
    "DisableAppUpdate": True,
    "DontCheckDefaultBrowser": True,
    "DisableTelemetry": True,
    "DisableFirefoxStudies": True,
    "DisableFeedbackCommands": True,
    "DisablePocket": True,
    "OverrideFirstRunPage": "",
    "OverridePostUpdatePage": "",
    "UserMessaging": {
        "WhatsNew": False, "ExtensionRecommendations": False, "FeatureRecommendations": False,
        "UrlbarInterventions": False, "SkipOnboarding": True, "MoreFromMozilla": False,
        "FirefoxLabs": False, "Locked": True,
    },
    "NewTabPage": False,
    "Homepage": {"URL": "about:blank", "StartPage": "previous-session"},
    "FirefoxHome": {
        "Search": True, "Weather": False, "TopSites": False, "SponsoredTopSites": False,
        "Highlights": False, "Pocket": False, "Stories": False, "SponsoredPocket": False,
        "SponsoredStories": False, "Widgets": {"Enabled": False}, "Locked": True,
    },
    "FirefoxSuggest": {"WebSuggestions": False, "SponsoredSuggestions": False, "ImproveSuggest": False, "Locked": True},
    "SearchSuggestEnabled": False,
    "GenerativeAI": {"Enabled": False, "Locked": True},
    "TranslateEnabled": False,
    "OfferToSaveLogins": False,
    "PasswordManagerEnabled": False,
    "AutofillAddressEnabled": False,
    "AutofillCreditCardEnabled": False,
    "DisableFormHistory": True,
    "NetworkPrediction": False,
    "HardwareAcceleration": False,
}

AUTOCONFIG_JS = """// MarkStile: load mozilla.cfg (allows the unsigned, policy-installed extension)
pref("general.config.filename", "mozilla.cfg");
pref("general.config.obscure_value", 0);
pref("general.config.sandbox_enabled", false);
"""
MOZILLA_CFG = """// MarkStile autoconfig (first line must be a comment)
lockPref("xpinstall.signatures.required", false);
lockPref("extensions.autoDisableScopes", 0);
lockPref("app.update.enabled", false);
defaultPref("browser.sessionstore.resume_from_crash", false);
"""
USER_JS = """// MarkStile (also set via autoconfig; kept here as a fallback)
user_pref("xpinstall.signatures.required", false);
user_pref("extensions.autoDisableScopes", 0);
"""


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o644)


def provision(
    data: Path, run: Path, ext_src: Path, ws_port: int, firefox_dir: Path, etc: Path = Path("/etc"),
    owner: str | None = None,
) -> dict:
    run.mkdir(parents=True, exist_ok=True)
    (data / "keys").mkdir(parents=True, exist_ok=True)
    key = crx.load_or_create_key(data / "keys" / "extension.pem")
    ext_id = crx.extension_id(crx.public_der(key))
    manifest = json.loads((ext_src / "chrome" / "manifest.json").read_text())
    version = manifest["version"]
    base = f"http://127.0.0.1:{ws_port}/ext"

    out = run / "ext"
    out.mkdir(parents=True, exist_ok=True)
    # The forcelist URL is only used for the first install; later update checks
    # follow the manifest's update_url (otherwise they go to the Web Store).
    staging = out / "chrome-src"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(ext_src / "chrome", staging)
    manifest["update_url"] = f"{base}/update.xml"
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "markstile.crx").write_bytes(crx.pack_crx3(staging, key))
    shutil.rmtree(staging)
    (out / "update.xml").write_text(crx.update_manifest(ext_id, version, f"{base}/markstile.crx"))

    secret = secrets.token_urlsafe(32)
    (run / "ext.secret").write_text(secret)
    (run / "ext.secret").chmod(0o600)

    def ext_config(browser: str) -> dict:
        return {"wsPort": ws_port, "secret": secret, "browser": browser}

    for browser, (d, extra) in CHROMIUM_POLICY_DIRS.items():
        write_json(
            etc / d / "policies" / "managed" / "markstile.json",
            {
                "ExtensionInstallForcelist": [f"{ext_id};{base}/update.xml"],
                "3rdparty": {"extensions": {ext_id: ext_config(browser)}},
                **CHROMIUM_EXTRA_POLICIES,
                **extra,
            },
        )

    xpi = ext_src / "firefox.xpi"
    if firefox_dir.exists():
        write_json(
            firefox_dir / "distribution" / "policies.json",
            {
                "policies": {
                    "ExtensionSettings": {
                        FIREFOX_EXT_ID: {"installation_mode": "force_installed", "install_url": xpi.as_uri()},
                        **{old: {"installation_mode": "blocked"} for old in FIREFOX_OLD_EXT_IDS},
                    },
                    "3rdparty": {"Extensions": {FIREFOX_EXT_ID: ext_config("firefox")}},
                    **FIREFOX_EXTRA_POLICIES,
                }
            },
        )
        (firefox_dir / "defaults" / "pref").mkdir(parents=True, exist_ok=True)
        (firefox_dir / "defaults" / "pref" / "autoconfig.js").write_text(AUTOCONFIG_JS)
        (firefox_dir / "mozilla.cfg").write_text(MOZILLA_CFG)
    ff_profile = data / "profiles" / "firefox"
    ff_profile.mkdir(parents=True, exist_ok=True)
    (ff_profile / "user.js").write_text(USER_JS)

    if owner:
        for p in [run, data / "keys", data / "profiles"]:
            for f in [p, *p.rglob("*")]:
                try:
                    shutil.chown(f, owner, owner)
                except (LookupError, PermissionError, FileNotFoundError):
                    pass
    return {"extension_id": ext_id, "version": version}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="markstile provision")
    ap.add_argument("--data", default=os.environ.get("MARKSTILE_DATA", "/data"))
    ap.add_argument("--run", default=os.environ.get("MARKSTILE_RUN", "/run/markstile"))
    ap.add_argument("--ext", default=os.environ.get("MARKSTILE_EXT", "/opt/markstile/ext"))
    ap.add_argument("--ws-port", type=int, default=int(os.environ.get("MARKSTILE_WS_PORT", "8765")))
    ap.add_argument("--firefox-dir", default=os.environ.get("FIREFOX_DIR", "/opt/firefox-devedition"))
    ap.add_argument("--etc", default="/etc")
    ap.add_argument("--owner", default=os.environ.get("MARKSTILE_USER"))
    a = ap.parse_args(argv)
    info = provision(Path(a.data), Path(a.run), Path(a.ext), a.ws_port, Path(a.firefox_dir), Path(a.etc), a.owner)
    print(f"markstile: extension id {info['extension_id']} (v{info['version']})")


if __name__ == "__main__":
    main()
