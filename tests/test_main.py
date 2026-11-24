# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""The RPC layer end to end with a fake Steam library: dual-backend variants, downloads, sessions, and rollback."""

import asyncio
import json
import os
import struct
import tempfile
import time
import unittest
from unittest import mock

import _harness  # noqa: F401
import decky
import fsr4_dll
import fsr4_install as I
import main

GPU = {"device": 0x163F, "family": 144, "rev": 3, "arch": "Steam Deck (Van Gogh)",
       "model": "INT8_LITE", "model_name": "INT8 Lite", "verified": True}
APPID = 1817070


def _write(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _library(root, appid, name, installdir, files):
    _write(os.path.join(root, "steamapps", f"appmanifest_{appid}.acf"),
           f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"installdir"\t\t"{installdir}"\n}}\n'
           .encode())
    for rel in files:
        if isinstance(rel, tuple):
            _write(os.path.join(root, "steamapps", "common", installdir, rel[0]), rel[1])
        else:
            _write(os.path.join(root, "steamapps", "common", installdir, rel), b"x")


def _make_pe_with_version(major, minor, build, rev):
    buf = bytearray(0x400)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x40)
    buf[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", buf, 0x44 + 2, 1)
    struct.pack_into("<H", buf, 0x44 + 16, 0xF0)
    opt = 0x44 + 20
    struct.pack_into("<H", buf, opt, 0x20B)
    struct.pack_into("<I", buf, opt + 108, 16)
    struct.pack_into("<II", buf, opt + 112 + 16, 0x1000, 0x200)
    sec = opt + 0xF0
    struct.pack_into("<IIII", buf, sec + 8, 0x200, 0x1000, 0x200, 0x200)
    rsrc = 0x200
    struct.pack_into("<HH", buf, rsrc + 14, 1, 0)
    struct.pack_into("<II", buf, rsrc + 16, 16, 0x80000018)
    struct.pack_into("<HH", buf, rsrc + 0x18 + 14, 1, 0)
    struct.pack_into("<II", buf, rsrc + 0x18 + 16, 1, 0x80000030)
    struct.pack_into("<HH", buf, rsrc + 0x30 + 14, 1, 0)
    struct.pack_into("<II", buf, rsrc + 0x30 + 16, 0x0409, 0x48)
    struct.pack_into("<II", buf, rsrc + 0x48, 0x1060, 52)
    struct.pack_into("<IIII", buf, rsrc + 0x60, 0xFEEF04BD, 0x00010000,
                     (major << 16) | minor, (build << 16) | rev)
    return bytes(buf)


class PluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = tempfile.mkdtemp()
        for name, sub in (("DECKY_PLUGIN_SETTINGS_DIR", "settings"), ("DECKY_PLUGIN_RUNTIME_DIR", "runtime"),
                          ("DECKY_USER_HOME", "home")):
            self.enterContext(mock.patch.object(decky, name, os.path.join(base, sub)))
        self.steam = os.path.join(decky.DECKY_USER_HOME, ".local", "share", "Steam")
        _library(self.steam, APPID, "Spider-Man", "SM",
                 ["Spider-Man.exe", "amd_fidelityfx_dx12.dll",
                  ("amd_fidelityfx_upscaler_dx12.dll", _make_pe_with_version(4, 1, 1, 2740)),
                  "OptiScaler.ini"])
        self.target = os.path.join(self.steam, "steamapps", "common", "SM")
        self.enterContext(mock.patch.object(main, "SHIM_PATH", os.path.join(base, "amdxc64.dll")))
        _write(main.SHIM_PATH, b"shim")
        self.enterContext(mock.patch.object(main.fsr4_gpu, "detect", return_value=GPU))
        self.p = await self.start()
        _write(main._ffx_path(), b"amd")
        self.p.dll_ok = True
        self._seed_cyboman(self.p)

    def _seed_cyboman(self, plugin, tag="dll-2026-11-22"):
        cdir = main._cyboman_dir()
        variants = {}
        for v in fsr4_dll.CYBOMAN_VARIANTS:
            p = fsr4_dll.cyboman_variant_path(cdir, v)
            data = f"cyboman-{v}".encode()
            _write(p, data)
            variants[v] = {"sha256": main.dk_fs.sha256_file(p), "size": len(data), "pe_version": "4.1.2.0"}
        main.dk_fs.write_json(os.path.join(cdir, fsr4_dll.CYBOMAN_META),
                              {"release_tag": tag, "extracted_from": "zip", "variants": variants})
        plugin.cyboman = fsr4_dll.cyboman_status(cdir)

    async def start(self):
        p = main.Plugin()
        await p._main()
        await p._scan_task
        return p

    async def asyncTearDown(self):
        await self.p._unload()

    def status(self, active="INT8_LITE"):
        _write(os.path.join(self.target, I.STATUS), json.dumps(
            {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ffx_requested": True,
             "dll_loaded": True, "family": 144, "active": active}).encode())


class Contract(PluginTest):
    async def test_state_comes_in_the_d2_shape(self):
        res = await self.p.get_state()
        self.assertTrue(res["ok"])
        self.assertEqual([g["appid"] for g in res["state"]["games"]], [APPID])
        self.assertTrue(res["state"]["games"][0]["optiscaler_detected"])
        self.assertTrue(res["state"]["cyboman"]["ready"])
        self.assertEqual(res["state"]["error"], "")

    async def test_an_unexpected_error_is_a_failure_not_an_exception(self):
        with mock.patch.object(main.fsr4_games, "scan", side_effect=RuntimeError("/home/deck/secret")):
            with self.assertLogs(decky.logger, "ERROR"):
                res = await self.p.scan_games()
        self.assertFalse(res["ok"])
        self.assertNotIn("/home/deck", res["error"])

    async def test_a_failed_start_still_answers(self):
        main._ready.clear()
        self.addCleanup(main._ready.set)
        with mock.patch.object(main.fsr4_dll, "is_valid", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await main.Plugin()._main()
        self.assertTrue(main._ready.is_set())


class Settings(PluginTest):
    async def test_settings_are_validated_saved_and_used_by_the_next_start(self):
        self.assertFalse((await self.p.set_setting("watermark", "yes"))["ok"])
        self.assertFalse((await self.p.set_setting("nonsense", True))["ok"])
        res = await self.p.set_setting("watermark", True)
        want = {"auto_launch_options": True, "watermark": True, "session_toast": False}
        self.assertEqual(res["state"]["settings"], want)
        self.assertEqual((await self.start()).settings, want)

    async def test_a_setting_that_cannot_be_saved_changes_nothing(self):
        full = OSError(28, "No space left on device", self.p.settings_path)
        with mock.patch.object(main.dk_fs, "write_json", side_effect=full):
            with self.assertLogs(decky.logger, "ERROR"):
                res = await self.p.set_setting("watermark", True)
        self.assertFalse(res["ok"])
        self.assertIn("No space left", res["error"])
        self.assertNotIn(self.p.settings_path, res["error"])
        self.assertFalse(self.p.settings["watermark"])

    async def test_two_quick_changes_both_stick(self):
        await asyncio.gather(self.p.set_setting("watermark", True), self.p.set_setting("auto_launch_options", False))
        want = {"auto_launch_options": False, "watermark": True, "session_toast": False}
        self.assertEqual(self.p.settings, want)
        self.assertEqual((await self.start()).settings, want)

    async def test_a_damaged_settings_file_gives_the_defaults(self):
        _write(self.p.settings_path, b"{nope")
        self.assertEqual((await self.start()).settings, main.DEFAULT_SETTINGS)


class Download(PluginTest):
    async def test_a_download_failure_is_reported_and_can_be_retried(self):
        with mock.patch.object(main.fsr4_dll, "fetch", side_effect=fsr4_dll.FetchError("HTTP 503")):
            with self.assertLogs(decky.logger, "ERROR"):
                res = await self.p.download_dll()
        self.assertEqual((res["ok"], res["error"], res["state"]["download"]["state"]), (False, "HTTP 503", "error"))

    async def test_an_unexpected_error_never_leaves_the_download_running(self):
        import http.client
        with mock.patch.object(main.fsr4_dll, "fetch", side_effect=http.client.IncompleteRead(b"")):
            with self.assertLogs(decky.logger, "ERROR"):
                res = await self.p.download_dll()
        self.assertFalse(res["ok"])
        self.assertEqual(self.p.download["state"], "error")
        with mock.patch.object(main.fsr4_dll, "fetch") as fetch:
            self.assertTrue((await self.p.download_dll())["ok"])
        fetch.assert_called_once()

    async def test_progress_is_polled_without_reading_files(self):
        def fetch(dest, progress, cancelled):
            progress(10, 100)
        with mock.patch.object(main.fsr4_dll, "fetch", side_effect=fetch):
            await self.p.download_dll()
        with mock.patch.object(main.fsr4_install, "read_status", side_effect=AssertionError("file read")), \
                mock.patch.object(main.os.path, "isfile", side_effect=AssertionError("file read")):
            res = await self.p.download_progress()
        self.assertEqual((res["ok"], res["download"]["done"], res["download"]["total"]), (True, 10, 100))

    async def test_cancel_and_verify(self):
        self.assertTrue((await self.p.cancel_download())["ok"])
        self.assertTrue(self.p._cancel)
        _write(main._ffx_path(), b"damaged")
        self.assertFalse((await self.p.verify_library())["state"]["dll_ready"])

    async def test_cyboman_release_check_download_progress_and_verify(self):
        rel = {"tag": "dll-2026-12-01", "html_url": "https://x", "published_at": "",
               "assets": {"7z": {"name": "a.7z", "size": 50}, "zip": None, "sha256sums": None}}
        with mock.patch.object(main.fsr4_dll, "check_cyboman_release", return_value=rel):
            res = await self.p.check_cyboman_release()
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"]["cyboman"]["latest_tag"], "dll-2026-12-01")
        self.assertTrue(res["state"]["cyboman"]["update_available"])

        def fake_fetch(cache_dir, release, *, progress, cancelled, on_stage):
            on_stage("downloading_7z", {"size": 50})
            progress(25, 50)
            self._seed_cyboman(self.p, tag="dll-2026-12-01")

        with mock.patch.object(main.fsr4_dll, "fetch_cyboman", side_effect=fake_fetch):
            dl = await self.p.download_cyboman()
        self.assertTrue(dl["ok"])
        self.assertEqual(dl["state"]["cyboman"]["tag"], "dll-2026-12-01")
        prog = await self.p.cyboman_progress()
        self.assertEqual((prog["ok"], prog["download"]["done"], prog["cyboman_ready"]), (True, 25, True))
        self.assertTrue((await self.p.cancel_cyboman_download())["ok"])
        self.assertTrue(self.p._cyboman_cancel)

        # Corrupt one Cyboman cached file -> verify_library catches it
        os.remove(fsr4_dll.cyboman_variant_path(main._cyboman_dir(), "4.1.2l-lossy"))
        self.assertFalse((await self.p.verify_library())["state"]["cyboman"]["ready"])


class Games(PluginTest):
    async def test_enable_needs_the_dll_and_a_gpu_model(self):
        self.p.dll_ok = False
        self.assertIn("Download", (await self.p.enable_game(APPID, "", I.VARIANT_OFFICIAL_LITE))["error"])
        self.p.dll_ok = True
        self.p.cyboman = {"ready": False, "tag": "", "extracted_from": "", "variants": {}}
        self.assertIn("Download", (await self.p.enable_game(APPID, "", "4.1.2l-exact"))["error"])
        self.p.gpu = {**GPU, "family": 148, "model": None}
        self.assertIn("requires a Steam Deck", (await self.p.enable_game(APPID, ""))["error"])
        self.assertIn("rescan", (await self.p.enable_game(1, ""))["error"])

    async def test_enable_and_disable_restore_folder_and_options(self):
        res = await self.p.enable_game(APPID, "-novid", I.VARIANT_OFFICIAL_LITE)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["launch_options"], f"{I.TOKEN} %command% -novid")
        self.assertTrue(res["state"]["games"][0]["enabled"])
        self.assertTrue((await self.p.launch_options_saved(APPID, res["launch_options"]))["ok"])
        res = await self.p.disable_game(APPID, res["launch_options"])
        self.assertEqual((res["ok"], res["launch_options"], res["kept"]), (True, "-novid", []))
        self.assertEqual(sorted(os.listdir(self.target)),
                         ["OptiScaler.ini", "Spider-Man.exe", "amd_fidelityfx_dx12.dll", "amd_fidelityfx_upscaler_dx12.dll"])
        self.assertFalse(res["state"]["games"][0]["enabled"])

    async def test_cyboman_enable_pe_warning_watermark_and_switch(self):
        res = await self.p.enable_game(APPID, "gamemoderun %command%", "4.1.2l-exact")
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"]["games"][0]["variant"], "4.1.2l-exact")
        self.assertEqual(res["state"]["games"][0]["backend"], "cyboman")

        await self.p.set_setting("watermark", True)
        wm = await self.p.apply_watermark(APPID, res["launch_options"])
        self.assertEqual(wm["launch_options"], "env MLSR-WATERMARK=1 gamemoderun %command%")

        # Switch Cyboman -> Official AMD Lite cleanly
        sw = await self.p.enable_game(APPID, wm["launch_options"], I.VARIANT_OFFICIAL_LITE)
        self.assertTrue(sw["ok"])
        self.assertEqual(sw["launch_options"], f"{I.TOKEN} {I.WATERMARK} gamemoderun %command%")

        # Disable restores original launch options
        off = await self.p.disable_game(APPID, sw["launch_options"])
        self.assertEqual(off["launch_options"], "gamemoderun %command%")

        # PE version mismatch triggers pe_mismatch=True unless forced
        _write(os.path.join(self.target, I.UPSCALER_DLL), _make_pe_with_version(4, 0, 0, 1))
        with self.assertLogs(decky.logger, "WARNING"):
            warn = await self.p.enable_game(APPID, "", "4.1.2l-exact", False)
        self.assertFalse(warn["ok"])
        self.assertTrue(warn["pe_mismatch"])
        self.assertEqual(warn["detected_version"], "4.0.0.1")
        forced = await self.p.enable_game(APPID, "", "4.1.2l-exact", True)
        self.assertTrue(forced["ok"])

    async def test_desktop_variants_rejected_and_legacy_manifest_rolls_back(self):
        for rejected in ("exact", "lossy"):
            res = await self.p.enable_game(APPID, "", rejected)
            self.assertFalse(res["ok"])
            self.assertIn("Unknown FSR4 variant", res["error"])

        orig_path = os.path.join(self.target, I.UPSCALER_DLL)
        with open(orig_path, "rb") as f:
            orig_bytes = f.read()
        legacy_bytes = b"legacy-exact-dll"
        _write(os.path.join(self.target, I.UPSCALER_BACKUP), orig_bytes)
        _write(orig_path, legacy_bytes)
        _write(self.p.manifest.path, json.dumps({
            str(APPID): {
                "target_dir": self.target,
                "backend": I.BACKEND_CYBOMAN,
                "variant": "exact",
                "release_tag": "dll-2026-11-20",
                "files": {I.UPSCALER_DLL: main.dk_fs.sha256_file(orig_path)},
                "backup": {
                    "file": I.UPSCALER_BACKUP,
                    "target": I.UPSCALER_DLL,
                    "sha256": main.dk_fs.sha256_file(os.path.join(self.target, I.UPSCALER_BACKUP)),
                    "pe_version": "4.1.1.2740",
                },
                "launch_original": "-novid",
                "launch_applied": "env MLSR-WATERMARK=1 %command% -novid",
            }
        }).encode())
        p = await self.start()
        g = (await p.get_state())["state"]["games"][0]
        self.assertTrue(g["enabled"])
        self.assertEqual((g["backend"], g["variant"], g["verdict"]), ("cyboman", "exact", "ok"))
        off = await p.disable_game(APPID, "env MLSR-WATERMARK=1 %command% -novid")
        self.assertEqual((off["ok"], off["launch_options"], off["kept"]), (True, "-novid", []))
        with open(orig_path, "rb") as f:
            self.assertEqual(f.read(), orig_bytes)
        self.assertFalse(os.path.exists(os.path.join(self.target, I.UPSCALER_BACKUP)))

    async def test_launch_options_steam_did_not_keep_are_recorded_and_reported(self):
        res = await self.p.enable_game(APPID, "-novid")
        with self.assertLogs(decky.logger, "WARNING"):
            saved = await self.p.launch_options_saved(APPID, "-novid")
        self.assertFalse(saved["ok"])
        self.assertIn("did not keep", saved["error"])
        self.assertEqual(self.p.manifest.entries[APPID]["launch_applied"], "-novid")
        p = await self.start()
        self.assertEqual((await p.disable_game(APPID, "-novid"))["launch_options"], "-novid")
        self.assertEqual(res["launch_options"], f"{I.TOKEN} %command% -novid")

    async def test_launch_options_steam_reported_empty_are_still_ours(self):
        res = await self.p.enable_game(APPID, "")
        with self.assertLogs(decky.logger, "WARNING"):
            self.assertFalse((await self.p.launch_options_saved(APPID, ""))["ok"])
        self.assertTrue((await self.p.get_state())["state"]["games"][0]["managed_options"])
        self.assertEqual((await self.p.disable_game(APPID, res["launch_options"]))["launch_options"], "")

    async def test_hands_off_mode_never_touches_launch_options(self):
        await self.p.set_setting("auto_launch_options", False)
        res = await self.p.enable_game(APPID, "-novid")
        self.assertEqual(res["launch_options"], "-novid")
        self.assertTrue((await self.p.launch_options_saved(APPID, "anything"))["ok"])
        self.assertFalse(res["state"]["games"][0]["managed_options"])
        self.assertFalse((await self.p.apply_watermark(APPID, "-novid"))["ok"])

    async def test_the_watermark_follows_the_setting(self):
        await self.p.set_setting("watermark", True)
        res = await self.p.enable_game(APPID, "")
        self.assertEqual(res["launch_options"], f"{I.TOKEN} {I.WATERMARK} %command%")
        await self.p.set_setting("watermark", False)
        res = await self.p.apply_watermark(APPID, res["launch_options"])
        self.assertEqual((res["ok"], res["launch_options"]), (True, f"{I.TOKEN} %command%"))

    async def test_a_plugin_update_keeps_v1_games_set_up(self):
        # Write a v1-format manifest without backend/variant keys
        _write(self.p.manifest.path, json.dumps({
            str(APPID): {
                "target_dir": self.target,
                "files": {I.SHIM: "a" * 64},
                "launch_original": "",
                "launch_applied": None,
                "mode": "native",
            }
        }).encode())
        p = await self.start()
        g = (await p.get_state())["state"]["games"][0]
        self.assertTrue(g["enabled"])
        self.assertEqual(g["backend"], "native")
        self.assertEqual(g["variant"], "official-lite")

    async def test_a_damaged_manifest_blocks_changes_and_says_why(self):
        await self.p.enable_game(APPID, "")
        path = self.p.manifest.path
        for name in os.listdir(os.path.dirname(path)):
            if name.startswith("manifest.json."):
                os.remove(os.path.join(os.path.dirname(path), name))
        _write(path, b"{damaged")
        with self.assertLogs(decky.logger, "ERROR"):
            p = await self.start()
        state = (await p.get_state())["state"]
        self.assertIn("damaged", state["error"])
        p.dll_ok = True
        for res in (await p.enable_game(APPID, ""), await p.disable_game(APPID, "")):
            self.assertFalse(res["ok"])
            self.assertIn("damaged", res["error"])
        self.assertIn(I.SHIM, os.listdir(self.target))


class Sessions(PluginTest):
    async def test_a_session_is_judged_when_the_game_exits(self):
        res = await self.p.enable_game(APPID, "")
        self.assertTrue((await self.p.session_started(APPID))["managed"])
        self.status("FSR3")
        with self.assertLogs(decky.logger, "WARNING"):
            ended = await self.p.session_ended(APPID, res["launch_options"])
        self.assertEqual((ended["ok"], ended["verdict"]), (True, "failed"))
        self.assertEqual((await self.p.session_ended(1, ""))["managed"], False)

    async def test_cyboman_session_does_not_expect_shim_status_file(self):
        await self.p.set_setting("session_toast", True)
        await self.p.enable_game(APPID, "", "4.1.2l-exact")
        started = await self.p.session_started(APPID)
        self.assertEqual((started["managed"], started["watch"]), (True, False))
        self.assertIsNone((await self.p.session_check(APPID))["running"])
        ended = await self.p.session_ended(APPID, "")
        self.assertEqual((ended["managed"], ended["verdict"]), (True, "ok"))

    async def test_a_missed_session_warns_only_the_second_time_in_a_row(self):
        res = await self.p.enable_game(APPID, "")
        for expected in ("missed", "failed", "missed"):
            await self.p.session_started(APPID)
            self.p.sessions[APPID] = time.time() + 60
            with self.assertNoLogs(decky.logger, "WARNING") if expected != "failed" else self.assertLogs(decky.logger):
                ended = await self.p.session_ended(APPID, res["launch_options"])
            self.assertEqual(ended["verdict"], expected)
            if expected == "failed":
                self.assertIn("twice in a row", ended["reason"])
                await self.p.session_started(APPID)
                self.status()
                self.assertEqual((await self.p.session_ended(APPID, res["launch_options"]))["verdict"], "ok")

    async def test_a_watched_session_reports_fsr4_once_it_runs(self):
        await self.p.enable_game(APPID, "")
        self.assertFalse((await self.p.session_started(APPID))["watch"])
        await self.p.set_setting("session_toast", True)
        self.assertTrue((await self.p.session_started(APPID))["watch"])
        self.assertFalse((await self.p.session_started(1))["watch"])
        self.assertIsNone((await self.p.session_check(APPID))["running"])
        self.status()
        self.assertEqual((await self.p.session_check(APPID))["running"], "FSR 4 (INT8_LITE)")
        self.assertIsNone((await self.p.session_check(1))["running"])

    async def test_steamos_update_notice_once(self):
        await self.p.enable_game(APPID, "")
        with mock.patch.object(main.fsr4_env, "steamos_build", return_value="3.9.5 (1)"):
            self.assertEqual((await self.p.startup_notice())["message"], "")
        with mock.patch.object(main.fsr4_env, "steamos_build", return_value="3.9.6 (2)"):
            self.assertIn("3.9.5 (1) → 3.9.6 (2)", (await self.p.startup_notice())["message"])
            self.assertEqual((await self.p.startup_notice())["message"], "")

    async def test_the_notice_survives_a_disk_that_cannot_save_it(self):
        await self.p.enable_game(APPID, "")
        with mock.patch.object(main.fsr4_env, "steamos_build", return_value="3.9.5 (1)"):
            await self.p.startup_notice()
        with mock.patch.object(main.fsr4_env, "steamos_build", return_value="3.9.6 (2)"), \
                mock.patch.object(main.dk_fs, "write_json", side_effect=OSError(28, "No space left on device")):
            with self.assertLogs(decky.logger, "WARNING"):
                res = await self.p.startup_notice()
        self.assertIn("3.9.6 (2)", res["message"])


class Updates(PluginTest):
    async def test_updates_go_through_the_shared_updater(self):
        with mock.patch.object(self.p.updates, "check", return_value={"ok": True, "update_available": False}) as c:
            self.assertTrue((await self.p.check_for_updates())["ok"])
        c.assert_called_once_with(True)
        self.assertEqual((self.p.updates.repo, self.p.updates.folder, self.p.updates.plugin_name),
                         ("XenoSlug/FSR4Injector-Deck", "FSR4Injector", "FSR4 Injector"))
        self.assertIn("bin/amdxc64.dll", self.p.updates.required)
        with mock.patch.object(self.p.updates, "download", return_value={"ok": False, "error": "x"}) as d:
            self.assertFalse((await self.p.perform_update("9.9.9"))["ok"])
        d.assert_called_once_with("9.9.9")


if __name__ == "__main__":
    unittest.main()
