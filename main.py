# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""FSR4Injector (Deck) - Steam Deck (Van Gogh / RDNA 2) FSR 4.1.2 Variant Manager under Proton.

Supports two isolated injection backends under one per-game manifest:
  * Cyboman RDNA 2 Overrides (zgauthier2000/fsr4-shader-performance-overrides): swaps
    amd_fidelityfx_upscaler_dx12.dll (with .fsr4deck.orig SHA-256 backup + PE 4.1.1.2740 guard) across
    the 4.1.2l-exact and 4.1.2l-lossy Steam Deck Lite variants.
  * Official AMD Lite (baseline): pairs amdxcffx64.dll (fetched from AMD) with bin/amdxc64.dll (LGPL shim)
    and WINEDLLOVERRIDES=amdxc64=n,b.

Every RPC answers {ok, error?, state?} (DeckyKit D2, dk_rpc).
"""

import asyncio
import json
import os
import sys
import time

import decky

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import dk_fs  # noqa: E402
import dk_rpc  # noqa: E402
import dk_update  # noqa: E402
import fsr4_dll  # noqa: E402
import fsr4_env  # noqa: E402
import fsr4_games  # noqa: E402
import fsr4_gpu  # noqa: E402
import fsr4_install  # noqa: E402

REPOSITORY = "XenoSlug/FSR4Injector-Deck"
SHIM_PATH = os.path.join(PLUGIN_DIR, "bin", fsr4_install.SHIM)
try:
    with open(os.path.join(PLUGIN_DIR, "plugin.json"), encoding="utf-8") as _f:
        VERSION = json.load(_f)["version"]
except (OSError, ValueError, KeyError):
    VERSION = "0.0.0"

DEFAULT_SETTINGS = {"auto_launch_options": True, "watermark": False, "session_toast": False}
_ready = asyncio.Event()
rpc = dk_rpc.boundary(decky.logger, _ready)


def _data_dir():
    return getattr(decky, "DECKY_PLUGIN_RUNTIME_DIR", decky.DECKY_PLUGIN_SETTINGS_DIR)


def _ffx_path():
    return os.path.join(_data_dir(), fsr4_install.FFX)


def _cyboman_dir():
    return os.path.join(_data_dir(), "cyboman")


def _home():
    return getattr(decky, "DECKY_USER_HOME", os.path.expanduser("~"))


def gpu_unsupported(gpu):
    if not gpu:
        return "No Steam Deck GPU was found, or its details could not be read."
    return (f"FSR4 Injector (Deck Edition) requires a Steam Deck (detected PCI 1002:{gpu['device']:04x}, "
            f"family {gpu['family']}, revision {gpu['rev']}).")


def _load_settings(path):
    """The switches; a damaged or unreadable file gives the defaults (it is replaced on the next change)."""
    try:
        saved = dk_fs.read_json(path, {}, validate=dk_fs.require_object)
    except OSError as e:
        decky.logger.warning(f"settings could not be read ({dk_fs.shown(e)}); using defaults")
        saved = {}
    return {k: saved[k] if isinstance(saved.get(k), bool) else v for k, v in DEFAULT_SETTINGS.items()}


def _steam_root():
    return os.path.join(_home(), ".local", "share", "Steam")


class Plugin:
    async def _main(self):
        try:
            self.manifest = fsr4_install.Manifest(os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "manifest.json"))
            self.settings_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
            self.settings = dict(DEFAULT_SETTINGS)
            self.games = []
            self.download = {"state": "idle", "done": 0, "total": fsr4_dll.RECIPE["packed_size"], "error": ""}
            self.cyboman_download = {"state": "idle", "done": 0, "total": 0, "stage": "", "error": ""}
            self.cyboman = {"ready": False, "tag": "", "extracted_from": "", "variants": {}}
            self.cyboman_release = None
            self.cyboman_extractor = None
            self.dll_ok, self.gpu = False, None
            self.lock = asyncio.Lock()
            self._cancel = False
            self._cyboman_cancel = False
            self.sessions = {}  # appid -> session start (epoch s)
            self.scanned = False
            self._scan_task = None
            self.updates = dk_update.Updates(
                repo=REPOSITORY, folder="FSR4Injector", plugin_name="FSR4 Injector", package_name="fsr4injector",
                current=VERSION, home=_home(), required=(*dk_update.REQUIRED, "bin/amdxc64.dll"),
                max_expanded=64 << 20, max_members=512, logger=decky.logger)
            if self.manifest.problem:
                decky.logger.error(f"manifest: {self.manifest.problem} {self.manifest.recovered}")
            elif self.manifest.recovered:
                decky.logger.warning(f"manifest: {self.manifest.recovered}")
            os.makedirs(_data_dir(), exist_ok=True)
            self.settings = await asyncio.to_thread(_load_settings, self.settings_path)
            self.dll_ok = await asyncio.to_thread(fsr4_dll.is_valid, _ffx_path())
            self.cyboman = await asyncio.to_thread(fsr4_dll.cyboman_status, _cyboman_dir())
            self.cyboman_extractor = await asyncio.to_thread(fsr4_dll.available_7z_extractor)
            self.gpu = await asyncio.to_thread(fsr4_gpu.detect)
            decky.logger.info(f"GPU: {self.gpu}")
            refreshed = await asyncio.to_thread(fsr4_install.refresh_shim, self.manifest, SHIM_PATH)
            if refreshed:
                decky.logger.info(f"updated amdxc64.dll in {refreshed} configured game(s)")
            self._scan_task = asyncio.create_task(self.scan_games())
        finally:
            _ready.set()

    def _game_view(self, g):
        appid = g["appid"]
        entry = self.manifest.entries.get(appid)
        backend = fsr4_install.entry_backend(entry) if entry else None
        variant = fsr4_install.entry_variant(entry) if entry else "off"
        if entry and backend == fsr4_install.BACKEND_CYBOMAN:
            verdict, reason = fsr4_install.cyboman_verdict(entry)
        else:
            status = fsr4_install.read_status(self.manifest, appid)
            verdict, reason = fsr4_install.verdict(status)
            if entry and verdict == "ok":
                changed = fsr4_env.changes(entry.get("verified"))
                if changed:
                    verdict, reason = "changed", (
                        f"Updated since FSR4 was last confirmed: {', '.join(changed)}. Play once to re-check."
                    )
        return {
            **g,
            "native_eligible": g.get("native_eligible", True),
            "cyboman_eligible": g.get("cyboman_eligible", False),
            "optiscaler_detected": g.get("optiscaler_detected", False),
            "upscaler_version": g.get("upscaler_version"),
            "radv_cswave32": g.get("radv_cswave32", False),
            "enabled": entry is not None,
            "backend": backend,
            "variant": variant,
            "release_tag": entry.get("release_tag", "") if entry else "",
            "verdict": verdict,
            "reason": reason,
            "managed_options": bool(entry) and entry.get("launch_applied") is not None,
        }

    def _state_now(self):
        entries = dict(self.manifest.entries)
        known = {g["appid"] for g in self.games}
        enabled_missing = [
            {
                "appid": a,
                "name": f"App {a}",
                "target_dir": e["target_dir"],
                "anticheat": False,
                "native_eligible": fsr4_install.entry_backend(e) == fsr4_install.BACKEND_NATIVE,
                "cyboman_eligible": fsr4_install.entry_backend(e) == fsr4_install.BACKEND_CYBOMAN,
                "optiscaler_detected": False,
                "upscaler_version": (e.get("backup") or {}).get("pe_version") or None,
                "radv_cswave32": False,
            }
            for a, e in entries.items() if a not in known
        ]
        latest_tag = self.cyboman_release["tag"] if self.cyboman_release else ""
        update_available = bool(latest_tag and self.cyboman["ready"] and latest_tag != self.cyboman["tag"])
        extractor_kind = self.cyboman_extractor[0] if self.cyboman_extractor else "zip"
        return {
            "version": VERSION,
            "home": _home(),
            "dll_ready": self.dll_ok,
            "shim_ready": os.path.isfile(SHIM_PATH),
            "gpu": self.gpu,
            "recipe": {k: fsr4_dll.RECIPE[k] for k in ("driver", "fsr", "dll_size")},
            "eula_url": fsr4_dll.EULA_URL,
            "download": dict(self.download),
            "cyboman": {
                "ready": self.cyboman["ready"],
                "tag": self.cyboman["tag"],
                "extracted_from": self.cyboman["extracted_from"],
                "latest_tag": latest_tag,
                "update_available": update_available,
                "extractor": extractor_kind,
                "repo": fsr4_dll.CYBOMAN_REPO,
                "variants": list(fsr4_dll.CYBOMAN_VARIANTS),
                "expected_pe_version": fsr4_dll.CYBOMAN_EXPECTED_PE_VERSION,
                "download": dict(self.cyboman_download),
            },
            "settings": dict(self.settings),
            "copy_options": {
                "plain": f"{fsr4_install.launch_prefix(False)} %command%",
                "watermark": f"{fsr4_install.launch_prefix(True)} %command%",
                "cyboman_watermark": f"{fsr4_install.launch_prefix(True, fsr4_install.BACKEND_CYBOMAN)} %command%",
            },
            "scanned": self.scanned,
            "error": self.manifest.problem,
            "games": [self._game_view(g) for g in self.games + enabled_missing],
        }

    async def _state(self):
        return await asyncio.to_thread(self._state_now)

    @rpc
    async def get_state(self) -> dict:
        return dk_rpc.ok(await self._state())

    @rpc
    async def set_setting(self, key: str, value: bool) -> dict:
        if key not in DEFAULT_SETTINGS or not isinstance(value, bool):
            return dk_rpc.fail("Unknown setting.", await self._state())
        async with self.lock:
            new = {**self.settings, key: value}
            try:
                await asyncio.to_thread(dk_fs.write_json, self.settings_path, new)
            except OSError as e:
                decky.logger.error(f"settings not saved: {e}")
                return dk_rpc.fail(f"The setting could not be saved ({dk_fs.shown(e)}).", await self._state())
            self.settings = new
        decky.logger.info(f"setting {key} = {value}")
        return dk_rpc.ok(await self._state())

    @rpc
    async def apply_watermark(self, appid: int, launch_options: str) -> dict:
        """The watermark setting for one game whose launch options the plugin manages."""
        async with self.lock:
            new = await asyncio.to_thread(
                fsr4_install.set_watermark, self.manifest, appid, launch_options, self.settings["watermark"]
            )
        if new is None:
            return dk_rpc.fail(
                "FSR4 Injector does not manage this game's launch options.", launch_options=launch_options
            )
        decky.logger.info(f"watermark {'on' if self.settings['watermark'] else 'off'} for {appid}")
        return dk_rpc.ok(launch_options=new)

    @rpc
    async def check_for_updates(self) -> dict:
        return await asyncio.to_thread(self.updates.check, True)

    @rpc
    async def perform_update(self, expected_version: str) -> dict:
        return await asyncio.to_thread(self.updates.download, expected_version)

    @rpc
    async def scan_games(self) -> dict:
        self.games = await asyncio.to_thread(fsr4_games.scan, _steam_root(), set(self.manifest.entries))
        self.scanned = True
        return dk_rpc.ok(await self._state())

    @rpc
    async def startup_notice(self) -> dict:
        """One-time message after a SteamOS update, when Official AMD Lite games are set up; '' otherwise."""
        path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "env.json")
        now = await asyncio.to_thread(fsr4_env.steamos_build)
        try:
            saved = await asyncio.to_thread(dk_fs.read_json, path, {}, validate=dk_fs.require_object)
        except OSError:
            saved = {}
        before = saved.get("steamos") if isinstance(saved.get("steamos"), str) else ""
        message = ""
        has_native = any(fsr4_install.entry_backend(e) == fsr4_install.BACKEND_NATIVE
                         for e in self.manifest.entries.values())
        if before and now and before != now and has_native:
            message = (f"SteamOS was updated ({before} → {now}). FSR4 is re-checked the next time you play "
                       "each game; you'll be told if it stopped working.")
        if now and now != before:
            try:
                await asyncio.to_thread(dk_fs.write_json, path, {"steamos": now})
            except OSError as e:
                decky.logger.warning(f"env.json not saved: {e}")
        return dk_rpc.ok(message=message)

    @rpc
    async def download_dll(self) -> dict:
        if self.download["state"] == "running":
            return dk_rpc.fail("The download is already running.", await self._state())
        self.download.update(state="running", done=0, total=fsr4_dll.RECIPE["packed_size"], error="")
        self._cancel = False
        decky.logger.info("AMD FSR4 DLL download started")

        def progress(done, total):
            self.download.update(done=done, total=total)

        try:
            await asyncio.to_thread(fsr4_dll.fetch, _ffx_path(), progress=progress, cancelled=lambda: self._cancel)
            self.dll_ok = True
            self.download["state"] = "done"
            decky.logger.info("AMD FSR4 DLL downloaded and verified")
        except fsr4_dll.FetchError as e:
            decky.logger.error(f"DLL download failed: {e}")
            self.download.update(state="error", error=str(e))
        finally:
            if self.download["state"] == "running":
                self.download.update(state="error", error="Unexpected error. Details are in the Decky log.")
        if self.download["state"] == "error":
            return dk_rpc.fail(self.download["error"], await self._state())
        return dk_rpc.ok(await self._state())

    @rpc
    async def download_progress(self) -> dict:
        """What the panel polls during the AMD download: no file is read."""
        return dk_rpc.ok(download=dict(self.download), dll_ready=self.dll_ok)

    @rpc
    async def cancel_download(self) -> dict:
        self._cancel = True
        decky.logger.info("AMD download cancel requested")
        return dk_rpc.ok(await self._state())

    @rpc
    async def check_cyboman_release(self) -> dict:
        """Fetch latest Cyboman RDNA 2 release metadata from GitHub."""
        try:
            self.cyboman_release = await asyncio.to_thread(fsr4_dll.check_cyboman_release)
        except fsr4_dll.FetchError as e:
            return dk_rpc.fail(str(e), await self._state())
        return dk_rpc.ok(await self._state())

    @rpc
    async def download_cyboman(self) -> dict:
        """Download, verify, and cache Cyboman RDNA 2 variants (.7z with automatic .zip fallback)."""
        if self.cyboman_download["state"] == "running":
            return dk_rpc.fail("The Cyboman download is already running.", await self._state())
        self.cyboman_download.update(state="running", done=0, total=0, stage="checking", error="")
        self._cyboman_cancel = False
        decky.logger.info("Cyboman RDNA 2 download started")

        def progress(done, total):
            self.cyboman_download.update(done=done, total=total)

        def on_stage(stage, asset):
            self.cyboman_download["stage"] = stage
            if isinstance(asset, dict) and isinstance(asset.get("size"), int):
                self.cyboman_download["total"] = asset["size"]

        try:
            release = self.cyboman_release
            if not release:
                release = await asyncio.to_thread(
                    fsr4_dll.check_cyboman_release, cancel=lambda: self._cyboman_cancel
                )
                self.cyboman_release = release
            await asyncio.to_thread(
                fsr4_dll.fetch_cyboman, _cyboman_dir(), release,
                progress=progress, cancelled=lambda: self._cyboman_cancel, on_stage=on_stage,
            )
            self.cyboman = await asyncio.to_thread(fsr4_dll.cyboman_status, _cyboman_dir())
            self.cyboman_download["state"] = "done"
            self.cyboman_download["stage"] = ""
            decky.logger.info(
                f"Cyboman RDNA 2 cached ({self.cyboman['tag']}, via {self.cyboman['extracted_from']})"
            )
        except fsr4_dll.FetchError as e:
            decky.logger.error(f"Cyboman download failed: {e}")
            self.cyboman_download.update(state="error", stage="", error=str(e))
        finally:
            if self.cyboman_download["state"] == "running":
                self.cyboman_download.update(
                    state="error", stage="", error="Unexpected error. Details are in the Decky log."
                )
        if self.cyboman_download["state"] == "error":
            return dk_rpc.fail(self.cyboman_download["error"], await self._state())
        return dk_rpc.ok(await self._state())

    @rpc
    async def cyboman_progress(self) -> dict:
        """What the panel polls during the Cyboman download: no file is read."""
        return dk_rpc.ok(download=dict(self.cyboman_download), cyboman_ready=self.cyboman["ready"])

    @rpc
    async def cancel_cyboman_download(self) -> dict:
        self._cyboman_cancel = True
        decky.logger.info("Cyboman download cancel requested")
        return dk_rpc.ok(await self._state())

    @rpc
    async def verify_library(self) -> dict:
        """Re-check both AMD's library and Cyboman's cached variants on disk."""
        self.dll_ok = await asyncio.to_thread(fsr4_dll.is_valid, _ffx_path())
        self.cyboman = await asyncio.to_thread(fsr4_dll.cyboman_status, _cyboman_dir())
        return dk_rpc.ok(await self._state())

    @rpc
    async def enable_game(self, appid: int, launch_options: str,
                          variant: str = fsr4_install.VARIANT_OFFICIAL_LITE,
                          force_pe_mismatch: bool = False) -> dict:
        async with self.lock:
            if self.manifest.problem:
                return dk_rpc.fail(self.manifest.problem, await self._state())
            game = next((g for g in self.games if g["appid"] == appid), None)
            if not game:
                return dk_rpc.fail("Game not found; rescan the library.")
            if variant not in fsr4_install.ALL_VARIANTS:
                return dk_rpc.fail(f"Unknown FSR4 variant: {variant}", await self._state())
            if not (self.gpu and self.gpu["model"]):
                return dk_rpc.fail(gpu_unsupported(self.gpu))
            if variant == fsr4_install.VARIANT_OFFICIAL_LITE:
                if not (self.dll_ok and os.path.isfile(SHIM_PATH)):
                    return dk_rpc.fail("Download the AMD FSR4 DLL first.")
            else:
                if not self.cyboman["ready"]:
                    return dk_rpc.fail("Download the Cyboman RDNA 2 overrides first.")
            try:
                options = await asyncio.to_thread(
                    fsr4_install.enable_variant,
                    self.manifest, game, variant,
                    launch_options=launch_options,
                    launch=self.settings["auto_launch_options"],
                    watermark=self.settings["watermark"],
                    shim_path=SHIM_PATH,
                    ffx_path=_ffx_path(),
                    gpu=self.gpu,
                    cyboman_cache_dir=_cyboman_dir(),
                    cyboman_tag=self.cyboman["tag"],
                    force_pe_mismatch=force_pe_mismatch,
                )
            except fsr4_install.PeVersionWarning as e:
                decky.logger.warning(f"enable {appid} ({variant}) PE mismatch: {e}")
                return dk_rpc.fail(
                    str(e), await self._state(),
                    pe_mismatch=True, detected_version=e.detected or "", expected_version=e.expected,
                )
            except fsr4_install.InstallError as e:
                decky.logger.warning(f"enable {appid} ({variant}): {e}")
                return dk_rpc.fail(str(e), await self._state())
            decky.logger.info(f"enabled FSR4 ({variant}) for {appid} in {game['target_dir']}")
            return dk_rpc.ok(await self._state(), launch_options=options)

    @rpc
    async def disable_game(self, appid: int, launch_options: str) -> dict:
        async with self.lock:
            if self.manifest.problem:
                return dk_rpc.fail(self.manifest.problem, await self._state())
            result = await asyncio.to_thread(fsr4_install.disable, self.manifest, appid, launch_options)
            decky.logger.info(f"disabled FSR4 for {appid}, kept={result['kept']}")
            return dk_rpc.ok(await self._state(), **result)

    @rpc
    async def launch_options_saved(self, appid: int, saved: str) -> dict:
        """Record what Steam actually kept so turning FSR4 off later restores original options exactly."""
        async with self.lock:
            entry = self.manifest.entries.get(appid)
            saved = (saved or "").strip()
            if not entry or entry.get("launch_applied") is None or saved == entry["launch_applied"]:
                return dk_rpc.ok()
            decky.logger.warning(f"launch options of {appid}: Steam kept {saved!r}, not {entry['launch_applied']!r}")
            entry["launch_applied"] = saved
            await asyncio.to_thread(self.manifest.save)
        return dk_rpc.fail(
            "Steam did not keep the new launch options, so FSR4 may not load. Turn the game off and "
            "on again, or add the launch option by hand.", await self._state()
        )

    @rpc
    async def session_started(self, appid: int) -> dict:
        entry = self.manifest.entries.get(appid)
        managed = entry is not None
        is_native = bool(entry) and fsr4_install.entry_backend(entry) == fsr4_install.BACKEND_NATIVE
        if is_native:
            self.sessions[appid] = time.time()
        return dk_rpc.ok(managed=managed, watch=is_native and self.settings["session_toast"])

    @rpc
    async def session_check(self, appid: int) -> dict:
        """Polled by the frontend during a watched Official AMD Lite session."""
        entry = self.manifest.entries.get(appid)
        if not entry or fsr4_install.entry_backend(entry) != fsr4_install.BACKEND_NATIVE:
            return dk_rpc.ok(running=None)
        status = await asyncio.to_thread(fsr4_install.read_status, self.manifest, appid)
        return dk_rpc.ok(running=fsr4_install.running_model(status, self.sessions.get(appid)))

    @rpc
    async def session_ended(self, appid: int, launch_options: str = "") -> dict:
        """Called by the frontend when a game exits; checks Official AMD Lite status."""
        entry = self.manifest.entries.get(appid)
        if not entry:
            return dk_rpc.ok(managed=False)
        if fsr4_install.entry_backend(entry) == fsr4_install.BACKEND_CYBOMAN:
            verdict, reason = await asyncio.to_thread(fsr4_install.cyboman_verdict, entry)
            return dk_rpc.ok(managed=True, verdict=verdict, reason=reason)
        status = await asyncio.to_thread(fsr4_install.read_status, self.manifest, appid)
        verdict, reason = fsr4_install.session_verdict(
            status, self.sessions.pop(appid, None), launch_options, entry.get("launch_applied") is not None
        )
        async with self.lock:
            entry["missed"] = entry.get("missed", 0) + 1 if verdict == "missed" else 0
            if verdict == "missed" and entry["missed"] >= 2:
                verdict, reason = "failed", reason + " It happened twice in a row."
            if verdict == "ok":
                entry["verified"] = await asyncio.to_thread(fsr4_env.fingerprint, status)
            await asyncio.to_thread(self.manifest.save)
        if verdict == "failed":
            decky.logger.warning(f"FSR4 not active for {appid}: {reason} status={status}")
        return dk_rpc.ok(managed=True, verdict=verdict, reason=reason)

    async def _unload(self):
        if not hasattr(self, "updates"):
            return
        self._cancel = True
        self._cyboman_cancel = True
        self.updates.close()
        if self._scan_task:
            self._scan_task.cancel()

    async def _uninstall(self):
        entries = getattr(getattr(self, "manifest", None), "entries", {})
        decky.logger.info(f"uninstall/update: leaving {len(entries)} game(s) configured")
