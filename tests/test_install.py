# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""Game discovery, dual-backend install/rollback safety, PE inspection, and launch option handling."""

import hashlib
import json
import os
import struct
import tempfile
import unittest
from unittest import mock

import _harness  # noqa: F401
import fsr4_dll
import fsr4_games
import fsr4_install as I


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


def _vdf_entry(index, fields):
    body = b"".join((b"\x02" + k.encode() + b"\0" + v.to_bytes(4, "little")) if isinstance(v, int)
                    else (b"\x01" + k.encode() + b"\0" + v.encode() + b"\0") for k, v in fields.items())
    return b"\x00" + str(index).encode() + b"\0" + body + b"\x00tags\0\x08\x08"


def _shortcuts_vdf(steam, user, *entries):
    _write(os.path.join(steam, "userdata", user, "config", "shortcuts.vdf"),
           b"\x00shortcuts\0" + b"".join(_vdf_entry(i, e) for i, e in enumerate(entries)) + b"\x08\x08")


class Discovery(unittest.TestCase):
    def test_finds_ffx_games_optiscaler_pe_and_radv_warnings(self):
        steam, sd = tempfile.mkdtemp(), tempfile.mkdtemp()
        _write(os.path.join(steam, "steamapps", "libraryfolders.vdf"),
               f'"libraryfolders"\n{{\n"0"\n{{\n"path" "{steam}"\n}}\n"1"\n{{\n"path" "{sd}"\n}}\n}}\n'
               .replace("\\", "\\\\").encode())
        _write(os.path.join(steam, "userdata", "1", "config", "localconfig.vdf"),
               '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
               '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"1817070"\n\t\t\t\t\t{\n'
               '\t\t\t\t\t\t"LaunchOptions"\t\t"RADV_PERFTEST=cswave32 %command%"\n'
               '\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'.encode())
        _library(steam, 1817070, "Spider-Man", "SM", ["Spider-Man.exe", "amd_fidelityfx_dx12.dll"])
        _library(sd, 1903340, "Expedition 33", "E33",
                 ["Sandfall/Binaries/Win64/SandFall.exe",
                  ("Sandfall/Binaries/Win64/amd_fidelityfx_upscaler_dx12.dll", _make_pe_with_version(4, 1, 1, 2740)),
                  "Sandfall/Binaries/Win64/OptiScaler.ini"])
        _library(sd, 4000, "No FSR", "NoFSR", ["game.exe"])
        _library(steam, 5000, "Online", "Online", ["amd_fidelityfx_dx12.dll", "EasyAntiCheat/EasyAntiCheat_x64.dll"])
        _library(steam, 5001, "Korean MMO", "KMMO", ["amd_fidelityfx_dx12.dll", "XIGNCODE/x3.xem"])
        _library(steam, 5002, "Old FPS", "OldFPS", ["amd_fidelityfx_dx12.dll", "pb/pbsvc.exe"])
        _library(steam, 1328670, "Mass Effect LE", "MELE",
                 ["amd_fidelityfx_dx12.dll", "Game/ME3/BioGame/CookedPCConsole/SFXCharacterClass_Vanguard.pcc"])
        _library(steam, 6000, "Escape", "../../outside", ["amd_fidelityfx_dx12.dll"])

        games = {g["appid"]: g for g in fsr4_games.scan(steam)}
        self.assertEqual(set(games), {1817070, 1903340, 5000, 5001, 5002, 1328670})
        self.assertTrue(games[1903340]["target_dir"].endswith(os.path.join("Binaries", "Win64")))
        self.assertTrue(games[1903340]["cyboman_eligible"])
        self.assertTrue(games[1903340]["optiscaler_detected"])
        self.assertEqual(games[1903340]["upscaler_version"], "4.1.1.2740")
        self.assertFalse(games[1817070]["cyboman_eligible"])
        self.assertTrue(games[1817070]["native_eligible"])
        self.assertTrue(games[1817070]["radv_cswave32"])
        self.assertFalse(games[1903340]["radv_cswave32"])
        self.assertTrue(all(games[a]["anticheat"] for a in (5000, 5001, 5002)))
        self.assertFalse(games[1817070]["anticheat"])
        self.assertFalse(games[1328670]["anticheat"])

    def test_finds_windows_games_added_to_steam(self):
        steam, drive = tempfile.mkdtemp(), tempfile.mkdtemp()
        _library(steam, 1817070, "Spider-Man", "SM", ["Spider-Man.exe", "amd_fidelityfx_dx12.dll"])
        for rel in ("Witcher/bin/witcher3.exe", "Witcher/bin/amd_fidelityfx_upscaler_dx12.dll", "Witcher/bin/dxgi.dll",
                    "Old/old.exe", "Online/Online.exe", "Online/Bin/amd_fidelityfx_loader_dx12.dll",
                    "Online/EasyAntiCheat/eac.dll"):
            _write(os.path.join(drive, rel), b"x")

        shortcuts_vdf = lambda user, *entries: _shortcuts_vdf(steam, user, *entries)
        exe = lambda rel: '"' + os.path.normpath(os.path.join(drive, rel)) + '"'
        shortcuts_vdf("1", {"appid": 3000000001, "AppName": "The Witcher 3", "Exe": exe("Witcher/bin/witcher3.exe"),
                            "LaunchOptions": "RADV_PERFTEST=cswave32 %command%"},
                      {"appid": 3000000002, "appname": "Old", "exe": exe("Old/old.exe")},
                      {"appid": 3000000003, "AppName": "Heroic", "Exe": '"/usr/bin/heroic"'},
                      {"appid": 3000000004, "AppName": "Same files", "Exe": exe("Witcher/bin/witcher3.exe")},
                      {"appid": 3000000005, "AppName": "Spider-Man copy",
                       "Exe": '"' + os.path.join(steam, "steamapps", "common", "SM", "Spider-Man.exe") + '"'})
        shortcuts_vdf("2", {"appid": 3000000006, "AppName": "", "Exe": exe("Online/Online.exe")})
        _write(os.path.join(steam, "userdata", "3", "config", "shortcuts.vdf"), b"\x00shortcuts\0\x00broken")

        games = {g["appid"]: g for g in fsr4_games.scan(steam)}
        self.assertEqual(set(games), {1817070, 3000000001, 3000000006})
        self.assertEqual(games[3000000001]["name"], "The Witcher 3")
        self.assertTrue(games[3000000001]["optiscaler_detected"])
        self.assertTrue(games[3000000001]["radv_cswave32"])
        self.assertEqual(games[3000000006]["name"], "Online")
        self.assertTrue(games[3000000006]["anticheat"])

    def test_a_launchers_shortcut_leaves_a_game_to_the_games_own_shortcut(self):
        steam, drive = tempfile.mkdtemp(), tempfile.mkdtemp()
        for rel in ("Ubisoft/UbisoftConnect.exe", "Ubisoft/games/ACS/ACShadows.exe",
                    "Ubisoft/games/ACS/amd_fidelityfx_dx12.dll"):
            _write(os.path.join(drive, rel), b"x")
        exe = lambda rel: os.path.normpath(os.path.join(drive, rel))
        _shortcuts_vdf(steam, "1",
                       {"appid": 3000000001, "AppName": "Ubisoft Connect", "Exe": exe("Ubisoft/UbisoftConnect.exe")},
                       {"appid": 3000000002, "AppName": "AC Shadows", "Exe": exe("Ubisoft/games/ACS/ACShadows.exe")})
        self.assertEqual([g["appid"] for g in fsr4_games.scan(steam)], [3000000002])
        self.assertEqual([g["appid"] for g in fsr4_games.scan(steam, {3000000001})], [3000000001])

    def _ubisoft_prefix(self, reg, *folders):
        steam, prefix = tempfile.mkdtemp(), os.path.join(tempfile.mkdtemp(), "pfx")
        launcher = os.path.join(prefix, "drive_c", "Program Files (x86)", "Ubisoft", "Ubisoft Game Launcher")
        _write(os.path.join(launcher, "upc.exe"), b"x")
        for folder in folders:
            _write(os.path.join(launcher, "games", folder, "amd_fidelityfx_dx12.dll"), b"x")
        _write(os.path.join(prefix, "system.reg"), reg.encode())
        return steam, prefix, '"' + os.path.join(launcher, "upc.exe") + '"'

    def test_shortcuts_sharing_a_launchers_exe_get_their_games_by_id(self):
        games = "Program Files (x86)/Ubisoft/Ubisoft Game Launcher/games"
        steam, _, upc = self._ubisoft_prefix(
            "WINE REGISTRY Version 2\n"
            r"[Software\\Wow6432Node\\Ubisoft\\Launcher\\Installs\\3923] 1700000000" "\n"
            rf'"InstallDir"="C:/{games}/Assassin\x2019s Creed Shadows/"' "\n\n"
            r"[Software\\Wow6432Node\\Microsoft\\Windows\\Uninstall\\Uplay Install 55] 1700000000" "\n"
            r'"DisplayName"="Other"' "\n"
            + '"InstallLocation"="C:\\\\' + games.replace("/", "\\\\") + '\\\\Other"\n\n'
            r"[Software\\Elsewhere\\7] 1" "\n" r'"InstallDir"="C:/not/this"' "\n",
            "Assassin\u2019s Creed Shadows", "Other")
        launch = lambda game: f'STEAM_COMPAT_DATA_PATH="/x/" %command% "uplay://launch/{game}/0"'
        _shortcuts_vdf(steam, "1",
                       {"appid": 3000000001, "AppName": "Ubisoft Connect", "Exe": upc},
                       {"appid": 3000000002, "AppName": "AC Shadows", "Exe": upc, "LaunchOptions": launch(3923)},
                       {"appid": 3000000003, "AppName": "Other", "Exe": upc, "LaunchOptions": launch(55)},
                       {"appid": 3000000004, "AppName": "Not installed", "Exe": upc, "LaunchOptions": launch(7)})
        found = {g["appid"]: os.path.basename(g["target_dir"]) for g in fsr4_games.scan(steam)}
        self.assertEqual(found, {3000000002: "Assassin\u2019s Creed Shadows", 3000000003: "Other"})
        kept = {g["appid"]: os.path.basename(g["target_dir"]) for g in fsr4_games.scan(steam, {3000000001})}
        self.assertEqual(set(kept.values()), {"Assassin\u2019s Creed Shadows", "Other"})
        self.assertIn(3000000001, kept)

    def test_a_game_id_without_a_usable_folder_is_not_listed(self):
        launch = "%command% uplay://launch/3923/0"
        for value in ("", "C:/", "C:/../../home", "no drive"):
            steam, _, upc = self._ubisoft_prefix(
                r"[Software\\Ubisoft\\Launcher\\Installs\\3923] 1" "\n" f'"InstallDir"="{value}"\n', "AC")
            _shortcuts_vdf(steam, "1", {"appid": 3000000002, "AppName": "AC", "Exe": upc, "LaunchOptions": launch})
            self.assertEqual(fsr4_games.scan(steam), [], value)
        steam, prefix, upc = self._ubisoft_prefix("", "AC")
        os.remove(os.path.join(prefix, "system.reg"))
        _shortcuts_vdf(steam, "1", {"appid": 3000000002, "AppName": "AC", "Exe": upc, "LaunchOptions": launch})
        self.assertEqual(fsr4_games.scan(steam), [])
        drive = tempfile.mkdtemp()
        _write(os.path.join(drive, "upc.exe"), b"x")
        _write(os.path.join(drive, "amd_fidelityfx_dx12.dll"), b"x")
        _shortcuts_vdf(steam, "1", {"appid": 3000000002, "AppName": "AC", "Exe": os.path.join(drive, "upc.exe"),
                                    "LaunchOptions": launch})
        self.assertEqual(fsr4_games.scan(steam), [])

    @unittest.skipIf(os.name == "nt", "a Wine drive is a symlink named z: in dosdevices")
    def test_a_game_on_another_wine_drive_is_found_through_dosdevices(self):
        library = tempfile.mkdtemp()
        _write(os.path.join(library, "games", "AC", "amd_fidelityfx_dx12.dll"), b"x")
        steam, prefix, upc = self._ubisoft_prefix(
            r"[Software\\Ubisoft\\Launcher\\Installs\\3923] 1" "\n" f'"InstallDir"="Z:{library}/games/AC/"\n')
        os.makedirs(os.path.join(prefix, "dosdevices"))
        os.symlink("/", os.path.join(prefix, "dosdevices", "z:"))
        _shortcuts_vdf(steam, "1", {"appid": 3000000002, "AppName": "AC", "Exe": upc,
                                    "LaunchOptions": "%command% uplay://launch/3923/0"})
        self.assertEqual([os.path.realpath(g["target_dir"]) for g in fsr4_games.scan(steam)],
                         [os.path.realpath(os.path.join(library, "games", "AC"))])


class LaunchOptions(unittest.TestCase):
    def test_round_trip(self):
        for original in ("", "%command% -dx12", "gamemoderun %command%", "-skipintro"):
            applied = I.with_token(original)
            self.assertTrue(applied.startswith(I.TOKEN + " "))
            self.assertIn("%command%", applied)
            self.assertEqual(I.without_token(applied, original, applied), original)

    def test_round_trip_of_a_launchers_shortcut(self):
        original = 'STEAM_COMPAT_DATA_PATH="/home/deck/compatdata/NonSteamLaunchers/" %command% "uplay://launch/3923/0"'
        applied = I.with_token(original)
        self.assertEqual(applied, f"{I.TOKEN} {original}")
        self.assertTrue(I.has_token(applied))
        self.assertEqual(I.without_token(applied, original, applied), original)

    def test_user_edit_after_enable_keeps_their_change(self):
        applied = I.with_token("%command%")
        edited = applied + " -novid"
        self.assertEqual(I.without_token(edited, "%command%", applied), "%command% -novid")

    def test_old_format_is_unchanged_without_other_overrides(self):
        for original, wm, want in (("~/.dlo/run %command%", False, f"{I.TOKEN} ~/.dlo/run %command%"),
                                   ("-novid", False, f"{I.TOKEN} %command% -novid"),
                                   ("", True, f"{I.TOKEN} {I.WATERMARK} %command%")):
            self.assertEqual(I.with_token(original, wm), want)

    def test_merges_into_another_tools_overrides_in_either_order(self):
        theirs = 'WINEDLLOVERRIDES="d3dcompiler_47=n;dxgi=n,b"'
        merged = 'WINEDLLOVERRIDES="d3dcompiler_47=n;dxgi=n,b;amdxc64=n,b"'
        applied = I.with_token(f"{theirs} %command%")
        self.assertEqual(applied, f"{merged} %command%")
        self.assertTrue(I.has_token(applied))
        self.assertEqual(I.without_token(applied, f"{theirs} %command%", applied), f"{theirs} %command%")
        later = 'WINEDLLOVERRIDES="amdxc64=n,b;d3dcompiler_47=n;dxgi=n,b" %command%'
        self.assertTrue(I.has_token(later))
        self.assertEqual(I.without_token(later, "", f"{I.TOKEN} %command%"), f"{theirs} %command%")
        self.assertEqual(I.without_token(f"{I.TOKEN} %command% -novid", "-novid", "x"), "%command% -novid")

    def test_users_own_override_stays(self):
        original = "WINEDLLOVERRIDES=dinput8=n,b %command%"
        applied = I.with_token(original)
        self.assertEqual(applied, 'WINEDLLOVERRIDES="dinput8=n,b;amdxc64=n,b" %command%')
        self.assertEqual(I.without_token(applied + " -x", original, applied), "WINEDLLOVERRIDES=dinput8=n,b %command% -x")

    def test_dll_lists_sharing_one_mode(self):
        self.assertTrue(I.has_token("WINEDLLOVERRIDES=dxgi,amdxc64=n,b %command%"))
        self.assertFalse(I.has_token("WINEDLLOVERRIDES=dxgi,amdxc64=b %command%"))
        self.assertEqual(I.with_token("WINEDLLOVERRIDES=dxgi,amdxc64=b %command%"),
                         'WINEDLLOVERRIDES="dxgi=b;amdxc64=n,b" %command%')
        self.assertEqual(I.without_token("WINEDLLOVERRIDES=dxgi,amdxc64=n,b %command%", "", "x"),
                         "WINEDLLOVERRIDES=dxgi=n,b %command%")

    def test_idempotent(self):
        once = I.with_token("%command%")
        self.assertEqual(I.with_token(once), once)


class CybomanLaunchOptions(unittest.TestCase):
    """POSIX-safe `env MLSR-WATERMARK=1` injection and removal edge cases."""

    def test_empty_and_bare_game_flags(self):
        self.assertEqual(I.with_cyboman_watermark(""), "env MLSR-WATERMARK=1 %command%")
        self.assertEqual(I.with_cyboman_watermark("", True), "env MLSR-WATERMARK=1 %command%")
        self.assertEqual(I.without_cyboman_watermark("env MLSR-WATERMARK=1 %command%", "", "x"), "")
        self.assertEqual(I.with_cyboman_watermark("-novid"), "env MLSR-WATERMARK=1 %command% -novid")
        self.assertEqual(I.without_cyboman_watermark("env MLSR-WATERMARK=1 %command% -novid", "-novid",
                                                     "env MLSR-WATERMARK=1 %command% -novid"), "-novid")

    def test_winedlloverrides_and_wrappers(self):
        wine = 'WINEDLLOVERRIDES="dxgi=n,b" %command%'
        applied_wine = I.with_cyboman_watermark(wine)
        self.assertEqual(applied_wine, 'WINEDLLOVERRIDES="dxgi=n,b" env MLSR-WATERMARK=1 %command%')
        self.assertEqual(I.without_cyboman_watermark(applied_wine + " -dx12", wine, applied_wine),
                         'WINEDLLOVERRIDES="dxgi=n,b" %command% -dx12')

        for orig in ("mangohud %command%", "gamemoderun %command% -dx12"):
            applied = I.with_cyboman_watermark(orig)
            self.assertEqual(applied, f"env MLSR-WATERMARK=1 {orig}")
            self.assertTrue(I.has_cyboman_watermark(applied))
            self.assertEqual(I.without_cyboman_watermark(applied + " -extra", orig, applied), orig + " -extra")

        # Never insert `env` after a wrapper binary even if an inner `env` exists later
        wrapped_env = "mangohud env DXVK_HUD=1 %command%"
        applied_wrapped = I.with_cyboman_watermark(wrapped_env)
        self.assertEqual(applied_wrapped, "env MLSR-WATERMARK=1 mangohud env DXVK_HUD=1 %command%")
        self.assertEqual(I.without_cyboman_watermark(applied_wrapped + " -x", wrapped_env, applied_wrapped),
                         wrapped_env + " -x")

        # Leading WINEDLLOVERRIDES followed by wrapper binary keeps env between WINEDLLOVERRIDES and wrapper
        both = 'WINEDLLOVERRIDES="dxgi=n,b" mangohud %command%'
        applied_both = I.with_cyboman_watermark(both)
        self.assertEqual(applied_both, 'WINEDLLOVERRIDES="dxgi=n,b" env MLSR-WATERMARK=1 mangohud %command%')
        self.assertEqual(I.without_cyboman_watermark(applied_both, both, applied_both), both)

    @unittest.skipUnless(os.name == "posix", "/bin/sh verification")
    def test_posix_sh_accepts_generated_launch_options(self):
        import subprocess
        import sys
        py = f'"{sys.executable}" -c "import os; print(os.environ.get(\'MLSR-WATERMARK\'), os.environ.get(\'WINEDLLOVERRIDES\'))"'
        for orig in ("", 'WINEDLLOVERRIDES="dxgi=n,b" %command%', "env DXVK_HUD=fps %command%"):
            opts = I.with_cyboman_watermark(orig).replace("%command%", py)
            res = subprocess.run(["/bin/sh", "-c", opts], capture_output=True, text=True, check=True)
            self.assertTrue(res.stdout.startswith("1 "), (opts, res.stdout))

    def test_preexisting_env_invocations_and_user_owned_watermark(self):
        pre = "env DXVK_HUD=fps %command%"
        applied = I.with_cyboman_watermark(pre)
        self.assertEqual(applied, "env DXVK_HUD=fps MLSR-WATERMARK=1 %command%")
        self.assertEqual(I.with_cyboman_watermark(applied), applied)  # idempotent
        self.assertEqual(I.without_cyboman_watermark(applied + " -x", pre, applied), "env DXVK_HUD=fps %command% -x")

        user_wm = "env MLSR-WATERMARK=1 %command%"
        self.assertEqual(I.without_cyboman_watermark(user_wm + " -x", user_wm, "other"), user_wm + " -x")

        # Repairs bare invalid MLSR-WATERMARK=1 if present
        self.assertEqual(I.with_cyboman_watermark("MLSR-WATERMARK=1 %command%"), "env MLSR-WATERMARK=1 %command%")


class InstallRollback(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.shim, self.ffx = os.path.join(d, "shim.dll"), os.path.join(d, "ffx.dll")
        _write(self.shim, b"shim")
        _write(self.ffx, b"amd")
        self.target = os.path.join(d, "game")
        _write(os.path.join(self.target, "game.exe"), b"exe")
        self.game = {"appid": 42, "target_dir": self.target}
        self.mpath = os.path.join(d, "manifest.json")
        self.m = I.Manifest(self.mpath)

    def files(self):
        return sorted(os.listdir(self.target))

    def test_amd_library_is_one_hard_link_when_the_drive_allows(self):
        I.enable(self.m, self.game, self.shim, self.ffx, "")
        self.assertTrue(os.path.samefile(self.ffx, os.path.join(self.target, I.FFX)))
        self.assertFalse(os.path.samefile(self.shim, os.path.join(self.target, I.SHIM)))
        I.disable(self.m, 42, "")
        self.assertTrue(os.path.isfile(self.ffx))

    def test_amd_library_is_copied_where_links_are_refused(self):
        with mock.patch.object(I.os, "link", side_effect=OSError(18, "Invalid cross-device link")):
            I.enable(self.m, self.game, self.shim, self.ffx, "")
        dst = os.path.join(self.target, I.FFX)
        self.assertFalse(os.path.samefile(self.ffx, dst))
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), b"amd")

    def test_enable_then_disable_restores_folder_and_options(self):
        gpu = {"device": 0x150E, "family": 150, "rev": 20}
        opts = I.enable(self.m, self.game, self.shim, self.ffx, "-novid", gpu)
        self.assertEqual(self.files(), ["amdxc64.dll", "amdxcffx64.dll", "fsr4injector.gpu", "game.exe"])
        with open(os.path.join(self.target, I.GPU)) as f:
            self.assertEqual(f.read(), "5390 150 20\n")
        self.assertEqual(opts, f"{I.TOKEN} %command% -novid")
        entry = I.Manifest(self.mpath).entries[42]
        self.assertEqual(entry["mode"], I.MODE_NATIVE)
        self.assertEqual(entry["backend"], I.BACKEND_NATIVE)
        _write(os.path.join(self.target, I.STATUS), b"{}")
        res = I.disable(I.Manifest(self.mpath), 42, opts)
        self.assertEqual(res, {"launch_options": "-novid", "kept": []})
        self.assertEqual(self.files(), ["game.exe"])

    def test_leftover_token_is_not_recorded_as_original(self):
        opts = I.enable(self.m, self.game, self.shim, self.ffx, f"{I.TOKEN} ~/.dlo/run %command%")
        self.assertEqual(I.disable(self.m, 42, opts)["launch_options"], "~/.dlo/run %command%")

    def test_never_overwrites_foreign_file(self):
        _write(os.path.join(self.target, "amdxc64.dll"), b"someone else's")
        with self.assertRaisesRegex(I.InstallError, "not ours"):
            I.enable(self.m, self.game, self.shim, self.ffx, "")
        with open(os.path.join(self.target, "amdxc64.dll"), "rb") as f:
            self.assertEqual(f.read(), b"someone else's")
        self.assertNotIn(42, self.m.entries)

    def test_disable_keeps_file_changed_since(self):
        I.enable(self.m, self.game, self.shim, self.ffx, "")
        _write(os.path.join(self.target, "amdxcffx64.dll"), b"replaced by user")
        res = I.disable(self.m, 42, "")
        self.assertEqual(res["kept"], ["amdxcffx64.dll"])
        self.assertEqual(self.files(), ["amdxcffx64.dll", "game.exe"])

    def test_reenable_updates_our_files(self):
        I.enable(self.m, self.game, self.shim, self.ffx, "")
        _write(self.shim, b"shim v2")
        I.enable(self.m, self.game, self.shim, self.ffx, I.with_token(""))
        with open(os.path.join(self.target, "amdxc64.dll"), "rb") as f:
            self.assertEqual(f.read(), b"shim v2")
        self.assertEqual(I.disable(self.m, 42, I.with_token(""))["launch_options"], "")

    def test_refresh_shim_after_plugin_update(self):
        I.enable(self.m, self.game, self.shim, self.ffx, "")
        _write(self.shim, b"shim v2")
        self.assertEqual(I.refresh_shim(self.m, self.shim), 1)
        with open(os.path.join(self.target, I.SHIM), "rb") as f:
            self.assertEqual(f.read(), b"shim v2")
        self.assertEqual(I.refresh_shim(self.m, self.shim), 0)
        _write(os.path.join(self.target, I.SHIM), b"user's own")
        _write(self.shim, b"shim v3")
        self.assertEqual(I.refresh_shim(self.m, self.shim), 0)
        self.assertEqual(I.refresh_shim(self.m, os.path.join(self.target, "missing.dll")), 0)

    def test_partial_install_is_recorded_for_rollback(self):
        def flaky(src, dst):
            raise OSError(28, "disk full", dst)
        with mock.patch.object(I, "_link_or_copy", flaky):
            with self.assertRaisesRegex(I.InstallError, "disk full") as caught:
                I.enable(self.m, self.game, self.shim, self.ffx, "")
        self.assertNotIn(self.target, str(caught.exception))
        self.assertEqual(list(I.Manifest(self.mpath).entries[42]["files"]), ["amdxc64.dll"])
        before = self.files()
        with mock.patch("dk_fs._copy", side_effect=lambda data, f: (f.write(b"half"), f.flush(),
                                                                  (_ for _ in ()).throw(OSError("full")))):
            with self.assertRaises(OSError):
                I._copy(self.ffx, os.path.join(self.target, "x.dll"))
        self.assertEqual(self.files(), before)
        with mock.patch.object(I.os, "replace", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                I._link_or_copy(self.ffx, os.path.join(self.target, "y.dll"))
        self.assertEqual(self.files(), before)
        I.disable(I.Manifest(self.mpath), 42, "")
        self.assertEqual(self.files(), ["game.exe"])

    def test_a_manifest_that_cannot_be_saved_at_the_end_is_an_install_error(self):
        with mock.patch.object(I.Manifest, "save", side_effect=[None, None, OSError(28, "No space left on device")]):
            with self.assertRaisesRegex(I.InstallError, "No space left"):
                I.enable(self.m, self.game, self.shim, self.ffx, "")

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_a_game_folder_that_is_a_link_still_works(self):
        moved = os.path.join(os.path.dirname(self.target), "other-drive")
        os.rename(self.target, moved)
        os.symlink(moved, self.target)
        I.enable(self.m, self.game, self.shim, self.ffx, "")
        self.assertEqual(sorted(os.listdir(moved)), ["amdxc64.dll", "amdxcffx64.dll", "game.exe"])
        _write(self.shim, b"shim v2")
        self.assertEqual(I.refresh_shim(self.m, self.shim), 1)
        I.disable(self.m, 42, "")
        self.assertEqual(os.listdir(moved), ["game.exe"])

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_a_symlink_in_the_game_folder_is_never_written_through(self):
        outside = os.path.join(os.path.dirname(self.target), "outside.dll")
        _write(outside, b"shim")
        os.symlink(outside, os.path.join(self.target, I.SHIM))
        with self.assertRaises(I.InstallError):
            I.enable(self.m, self.game, self.shim, self.ffx, "")
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"shim")


class CybomanSwapAndRollback(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.shim = os.path.join(d, "amdxc64.dll")
        self.ffx = os.path.join(d, "amdxcffx64.dll")
        _write(self.shim, b"shim")
        _write(self.ffx, b"amd")
        self.cache = os.path.join(d, "cyboman")
        for v in fsr4_dll.CYBOMAN_VARIANTS:
            _write(fsr4_dll.cyboman_variant_path(self.cache, v), f"cyboman-{v}".encode())
        self.target = os.path.join(d, "game")
        self.orig_bytes = _make_pe_with_version(4, 1, 1, 2740)
        self.upscaler = os.path.join(self.target, I.UPSCALER_DLL)
        _write(self.upscaler, self.orig_bytes)
        self.game = {"appid": 77, "target_dir": self.target}
        self.mpath = os.path.join(d, "manifest.json")
        self.m = I.Manifest(self.mpath)

    def test_pe_version_guard_and_force_override(self):
        _write(self.upscaler, _make_pe_with_version(4, 0, 0, 100))
        with self.assertRaises(I.PeVersionWarning) as caught:
            I.enable_variant(self.m, self.game, "4.1.2l-exact",
                             cyboman_cache_dir=self.cache, cyboman_tag="dll-2026-11-22")
        self.assertEqual((caught.exception.detected, caught.exception.expected), ("4.0.0.100", "4.1.1.2740"))
        self.assertNotIn(77, self.m.entries)
        # Explicit force_pe_mismatch succeeds cleanly
        I.enable_variant(self.m, self.game, "4.1.2l-exact",
                         cyboman_cache_dir=self.cache, cyboman_tag="dll-2026-11-22", force_pe_mismatch=True)
        with open(os.path.join(self.target, I.UPSCALER_BACKUP), "rb") as f:
            self.assertEqual(f.read(), _make_pe_with_version(4, 0, 0, 100))

    def test_backup_swap_switch_variants_and_rollback(self):
        opts = I.enable_variant(self.m, self.game, "4.1.2l-exact", launch_options="gamemoderun %command%",
                                watermark=True, cyboman_cache_dir=self.cache, cyboman_tag="dll-2026-11-22")
        self.assertEqual(opts, "env MLSR-WATERMARK=1 gamemoderun %command%")
        self.assertTrue(os.path.samefile(fsr4_dll.cyboman_variant_path(self.cache, "4.1.2l-exact"), self.upscaler))
        backup = os.path.join(self.target, I.UPSCALER_BACKUP)
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), self.orig_bytes)
        entry = I.Manifest(self.mpath).entries[77]
        self.assertEqual(entry["backend"], I.BACKEND_CYBOMAN)
        self.assertEqual(entry["backup"]["sha256"], hashlib.sha256(self.orig_bytes).hexdigest())

        # Switch to 4.1.2l-lossy preserves .fsr4deck.orig and launch_original
        opts2 = I.enable_variant(self.m, self.game, "4.1.2l-lossy", launch_options=opts,
                                 watermark=True, cyboman_cache_dir=self.cache, cyboman_tag="dll-2026-11-22")
        self.assertEqual(opts2, opts)
        with open(self.upscaler, "rb") as f:
            self.assertEqual(f.read(), b"cyboman-4.1.2l-lossy")
        self.assertEqual(I.cyboman_verdict(self.m.entries[77])[0], "ok")

        # Clean rollback restores original DLL and removes .fsr4deck.orig
        res = I.disable(I.Manifest(self.mpath), 77, opts2)
        self.assertEqual(res, {"launch_options": "gamemoderun %command%", "kept": []})
        self.assertEqual(sorted(os.listdir(self.target)), [I.UPSCALER_DLL])
        with open(self.upscaler, "rb") as f:
            self.assertEqual(f.read(), self.orig_bytes)

    def test_refuses_to_overwrite_or_delete_unrecognized_dll(self):
        I.enable_variant(self.m, self.game, "4.1.2l-exact",
                         cyboman_cache_dir=self.cache, cyboman_tag="dll-2026-11-22")
        _write(self.upscaler, b"updated-by-optiscaler")
        # Repair / re-enable must NOT silently clobber externally updated DLL
        with self.assertRaisesRegex(I.InstallError, "modified outside"):
            I.enable_variant(self.m, self.game, "4.1.2l-exact",
                             cyboman_cache_dir=self.cache, cyboman_tag="dll-2026-11-22")
        self.assertEqual(I.cyboman_verdict(self.m.entries[77])[0], "changed")
        # Disable leaves unrecognized active DLL and backup untouched
        res = I.disable(self.m, 77, "")
        self.assertEqual(sorted(res["kept"]), sorted([I.UPSCALER_DLL, I.UPSCALER_BACKUP]))
        with open(self.upscaler, "rb") as f:
            self.assertEqual(f.read(), b"updated-by-optiscaler")

    def test_switch_between_cyboman_and_official_amd_lite(self):
        orig_opts = "-novid"
        o1 = I.enable_variant(self.m, self.game, "4.1.2l-exact", launch_options=orig_opts, watermark=True,
                              cyboman_cache_dir=self.cache, cyboman_tag="dll-2026-11-22")
        self.assertEqual(o1, "env MLSR-WATERMARK=1 %command% -novid")
        # Switch Cyboman -> Official AMD Lite
        o2 = I.enable_variant(self.m, self.game, I.VARIANT_OFFICIAL_LITE, launch_options=o1, watermark=True,
                              shim_path=self.shim, ffx_path=self.ffx)
        self.assertEqual(o2, f"{I.TOKEN} {I.WATERMARK} %command% -novid")
        self.assertNotIn(I.UPSCALER_BACKUP, os.listdir(self.target))
        with open(self.upscaler, "rb") as f:
            self.assertEqual(f.read(), self.orig_bytes)
        self.assertIn(I.SHIM, os.listdir(self.target))
        # Switch Official AMD Lite -> Cyboman (exact)
        o3 = I.enable_variant(self.m, self.game, "exact", launch_options=o2, watermark=False,
                              cyboman_cache_dir=self.cache, cyboman_tag="dll-2026-11-22")
        self.assertEqual(o3, "-novid")
        self.assertNotIn(I.SHIM, os.listdir(self.target))
        self.assertNotIn(I.FFX, os.listdir(self.target))
        with open(self.upscaler, "rb") as f:
            self.assertEqual(f.read(), b"cyboman-exact")
        self.assertEqual(self.m.entries[77]["launch_original"], "-novid")


class ManifestSafety(unittest.TestCase):
    """The manifest is the only record of what is ours: never silently reset."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "manifest.json")

    def test_a_damaged_manifest_comes_back_from_its_last_good_copy(self):
        m = I.Manifest(self.path)
        m.entries[42] = {"target_dir": "/g", "files": {I.SHIM: "a" * 64}, "launch_original": ""}
        m.save()
        _write(self.path, b'{"42": {"target_dir": ')
        again = I.Manifest(self.path)
        self.assertEqual(list(again.entries), [42])
        self.assertTrue(again.recovered)
        self.assertEqual(again.problem, "")

    def test_without_a_good_copy_it_is_kept_and_nothing_is_saved_over_it(self):
        for damaged in (b"[1, 2]", b'{"42": {"files": {}}}', b"{not json"):
            _write(self.path, damaged)
            for name in os.listdir(os.path.dirname(self.path)):
                if name != "manifest.json":
                    os.remove(os.path.join(os.path.dirname(self.path), name))
            m = I.Manifest(self.path)
            self.assertIn("damaged", m.problem)
            with self.assertRaises(I.InstallError):
                m.save()
            with open(self.path, "rb") as f:
                self.assertEqual(f.read(), damaged)

    def test_an_unreadable_file_names_no_path(self):
        with mock.patch("dk_fs.load_json", side_effect=PermissionError(13, "Permission denied", self.path)):
            m = I.Manifest(self.path)
        self.assertIn("Permission denied", m.problem)
        self.assertNotIn(os.path.dirname(self.path), m.problem)

    def test_the_old_format_still_loads_and_normalizes(self):
        _write(self.path, json.dumps({"1817070": {
            "target_dir": "/g", "files": {I.SHIM: "a" * 64}, "launch_original": "", "launch_applied": None,
            "mode": "native", "enabled_at": "2026-10-25T10:00:00Z", "verified": {"steamos": "3.9"}, "missed": 1,
        }}, indent=1).encode())
        m = I.Manifest(self.path)
        self.assertEqual((m.problem, list(m.entries)), ("", [1817070]))
        self.assertEqual(I.entry_backend(m.entries[1817070]), I.BACKEND_NATIVE)
        self.assertEqual(I.entry_variant(m.entries[1817070]), I.VARIANT_OFFICIAL_LITE)


class LaunchSettings(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.shim, self.ffx = os.path.join(d, "shim.dll"), os.path.join(d, "ffx.dll")
        _write(self.shim, b"shim")
        _write(self.ffx, b"amd")
        self.target = os.path.join(d, "game")
        os.makedirs(self.target)
        self.game = {"appid": 9, "target_dir": self.target}
        self.m = I.Manifest(os.path.join(d, "manifest.json"))

    def test_watermark_rides_along_and_is_undone(self):
        opts = I.enable(self.m, self.game, self.shim, self.ffx, "~/.dlo/run %command%", watermark=True)
        self.assertEqual(opts, f"{I.TOKEN} {I.WATERMARK} ~/.dlo/run %command%")
        self.assertEqual(I.with_token(opts, True), opts)
        self.assertEqual(I.disable(self.m, 9, opts + " -novid")["launch_options"], "~/.dlo/run %command% -novid")

    def test_users_own_watermark_survives(self):
        opts = I.enable(self.m, self.game, self.shim, self.ffx, f"{I.WATERMARK} %command%", watermark=True)
        self.assertEqual(I.set_watermark(self.m, 9, opts, False), opts)
        self.assertEqual(I.disable(self.m, 9, opts + " x")["launch_options"], f"{I.WATERMARK} %command% x")

    def test_watermark_next_to_a_merged_variable(self):
        theirs = 'WINEDLLOVERRIDES="d3dcompiler_47=n;dxgi=n,b"'
        opts = I.enable(self.m, self.game, self.shim, self.ffx, f"{theirs} ~/.dlo/run %command%")
        on = I.set_watermark(self.m, 9, opts, True)
        self.assertEqual(on, f'WINEDLLOVERRIDES="d3dcompiler_47=n;dxgi=n,b;amdxc64=n,b" {I.WATERMARK} ~/.dlo/run %command%')
        off = I.set_watermark(self.m, 9, on, False)
        self.assertEqual(off, 'WINEDLLOVERRIDES="d3dcompiler_47=n;dxgi=n,b;amdxc64=n,b" ~/.dlo/run %command%')
        self.assertEqual(I.disable(self.m, 9, off)["launch_options"], f"{theirs} ~/.dlo/run %command%")

    def test_switching_the_watermark_on_set_up_games(self):
        opts = I.enable(self.m, self.game, self.shim, self.ffx, "-novid")
        on = I.set_watermark(self.m, 9, opts, True)
        self.assertEqual(on, f"{I.TOKEN} {I.WATERMARK} %command% -novid")
        off = I.set_watermark(self.m, 9, on, False)
        self.assertEqual(off, f"{I.TOKEN} %command% -novid")
        self.assertEqual(I.disable(self.m, 9, off)["launch_options"], "-novid")

    def test_hands_off_mode_never_touches_launch_options(self):
        self.assertEqual(I.enable(self.m, self.game, self.shim, self.ffx, "-novid", launch=False), "-novid")
        self.assertIn(I.SHIM, os.listdir(self.target))
        self.assertIsNone(I.set_watermark(self.m, 9, "-novid", True))
        mine = f"{I.TOKEN} %command% -novid"
        self.assertEqual(I.disable(self.m, 9, mine), {"launch_options": mine, "kept": []})
        self.assertEqual(os.listdir(self.target), [])

    def test_missing_token_in_hands_off_mode_says_to_add_it(self):
        start = 1792873295
        verdict, reason = I.session_verdict(None, start, "%command%", managed=False)
        self.assertEqual(verdict, "failed")
        self.assertIn("add WINEDLLOVERRIDES=amdxc64=n,b", reason)


class Verdict(unittest.TestCase):
    def test_states(self):
        ok = {"ffx_requested": True, "dll_loaded": True, "family": 150, "active": "INT8_LITE", "provider": "4.1.2 *"}
        self.assertEqual(I.verdict(ok)[0], "ok")
        self.assertIn("4.1.2", I.verdict(ok)[1])
        self.assertEqual(I.verdict(None)[0], "unknown")
        self.assertEqual(I.verdict({"ffx_requested": False})[0], "unused")
        self.assertEqual(I.verdict({**ok, "active": "FSR3"})[0], "failed")
        self.assertEqual(I.verdict({**ok, "dll_loaded": False, "active": None})[0], "failed")
        self.assertIn("not supported", I.verdict({**ok, "family": 0, "active": "FSR3", "gpu": "1002:15bf"})[1])
        self.assertIn("update", I.verdict({**ok, "active": "unknown"})[1])


class SessionVerdict(unittest.TestCase):
    ok = {"started": "2026-10-24T20:21:38Z", "ffx_requested": True, "dll_loaded": True, "family": 150,
          "active": "INT8_LITE"}
    start = 1792873295

    def test_status_from_this_session_is_judged_normally(self):
        self.assertEqual(I.session_verdict(self.ok, self.start, I.with_token(""))[0], "ok")

    def test_token_inside_a_merged_variable_counts(self):
        stale = {**self.ok, "started": "2026-10-24T19:00:00Z"}
        merged = 'WINEDLLOVERRIDES="d3dcompiler_47=n;amdxc64=n,b" %command%'
        self.assertEqual(I.session_verdict(stale, self.start, merged)[0], "missed")

    def test_stale_status_with_token_removed_names_the_cause(self):
        stale = {**self.ok, "started": "2026-10-24T19:00:00Z"}
        verdict, reason = I.session_verdict(stale, self.start, "~/.dlo/run %command%")
        self.assertEqual(verdict, "failed")
        self.assertIn("launch options no longer contain", reason)

    def test_missing_status_with_the_token_is_a_miss(self):
        self.assertEqual(I.session_verdict(None, self.start, I.with_token(""))[0], "missed")

    def test_without_session_start_falls_back_to_plain_verdict(self):
        self.assertEqual(I.session_verdict(self.ok, None, ""), I.verdict(self.ok))

    def test_running_model_counts_only_success_written_in_this_session(self):
        self.assertEqual(I.running_model({**self.ok, "provider": "4.1.2 *"}, self.start), "FSR 4.1.2 (INT8_LITE)")
        self.assertIsNone(I.running_model({**self.ok, "started": "2026-10-24T19:00:00Z"}, self.start))
        self.assertIsNone(I.running_model(self.ok, None))
        self.assertIsNone(I.running_model(None, self.start))
        self.assertIsNone(I.running_model({**self.ok, "active": "FSR3"}, self.start))
        self.assertIsNone(I.running_model({**self.ok, "dll_loaded": False, "active": None}, self.start))


if __name__ == "__main__":
    unittest.main()
