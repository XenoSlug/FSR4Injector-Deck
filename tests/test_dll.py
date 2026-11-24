# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""Recipe-driven AMD DLL fetch and Cyboman RDNA 2 archive download/extraction tests."""

import hashlib
import io
import json
import lzma
import os
import random
import stat
import struct
import subprocess
import tempfile
import unittest
import unittest.mock
import zipfile

import _harness  # noqa: F401
import dk_net
import fsr4_dll


def _package(dll, before, start_offset, junk):
    """Remote file bytes and a recipe pointing at a 'reset chunk' that begins after junk bytes."""
    stream = before + dll + os.urandom(4096)
    packed = lzma.compress(stream, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_X86, "start_offset": start_offset}, {"id": lzma.FILTER_LZMA2, "dict_size": 2**20}])
    remote = os.urandom(junk) + packed
    recipe = {"url": "https://drivers.amd.com/x.exe", "referer": "https://www.amd.com/", "packed_offset": junk,
              "packed_size": len(packed), "packed_sha256": hashlib.sha256(packed).hexdigest(),
              "unpacked_offset": start_offset, "dict_size": 2**20, "dll_offset": len(before), "dll_size": len(dll),
              "sha256": hashlib.sha256(dll).hexdigest()}
    return remote, recipe


def _spool(remote, seen=None):
    """dk_net.spool serving the asked range of remote, with its size and hash checks."""
    def spool(url, folder, limit, sha256=None, **options):
        if seen is not None:
            seen.update(options, url=url, folder=folder, limit=limit, sha256=sha256)
        first, last = options["byte_range"]
        body = remote[first:last + 1]
        if options.get("cancel") and options["cancel"]():
            raise dk_net.NetError("cancelled")
        if options.get("progress"):
            options["progress"](len(body))
        if hashlib.sha256(body).hexdigest() != sha256:
            raise dk_net.Mismatch("not the expected file (SHA-256 differs)")
        return io.BytesIO(body)
    return spool


def _make_pe_with_version(major, minor, build, rev):
    """Minimal valid PE32+ image containing an RT_VERSION VS_FIXEDFILEINFO resource."""
    buf = bytearray(0x400)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x40)
    buf[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", buf, 0x44 + 2, 1)      # NumberOfSections
    struct.pack_into("<H", buf, 0x44 + 16, 0xF0)  # SizeOfOptionalHeader
    opt = 0x44 + 20
    struct.pack_into("<H", buf, opt, 0x20B)       # PE32+
    struct.pack_into("<I", buf, opt + 108, 16)    # NumberOfRvaAndSizes
    struct.pack_into("<II", buf, opt + 112 + 16, 0x1000, 0x200)  # RT_RESOURCE dir
    sec = opt + 0xF0
    struct.pack_into("<IIII", buf, sec + 8, 0x200, 0x1000, 0x200, 0x200)
    rsrc = 0x200
    struct.pack_into("<HH", buf, rsrc + 14, 1, 0)
    struct.pack_into("<II", buf, rsrc + 16, 16, 0x80000018)  # RT_VERSION
    struct.pack_into("<HH", buf, rsrc + 0x18 + 14, 1, 0)
    struct.pack_into("<II", buf, rsrc + 0x18 + 16, 1, 0x80000030)
    struct.pack_into("<HH", buf, rsrc + 0x30 + 14, 1, 0)
    struct.pack_into("<II", buf, rsrc + 0x30 + 16, 0x0409, 0x48)
    struct.pack_into("<II", buf, rsrc + 0x48, 0x1060, 52)
    struct.pack_into("<IIII", buf, rsrc + 0x60, 0xFEEF04BD, 0x00010000,
                     (major << 16) | minor, (build << 16) | rev)
    return bytes(buf)


def _cyboman_zip(variants_data, prefix="igpu-rdna2", extra=()):
    """Build a synthetic Cyboman ZIP bytes payload."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for variant, data in variants_data.items():
            member = f"{prefix}/{variant}/{fsr4_dll.CYBOMAN_DLL_NAME}" if prefix else f"{variant}/{fsr4_dll.CYBOMAN_DLL_NAME}"
            zf.writestr(member, data)
        for member, data in extra:
            zf.writestr(member, data)
    return out.getvalue()


class Fetch(unittest.TestCase):
    def setUp(self):
        rnd = random.Random(7)
        self.dll = bytes(rnd.choice((0xE8, 0xE9, 0x00, 0xFF, rnd.randrange(256))) for _ in range(300_000))
        self.before = bytes(rnd.randrange(256) for _ in range(150_000))
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, "amdxcffx64.dll")

    def test_extracts_exact_dll_from_exactly_the_pinned_range(self):
        remote, recipe = _package(self.dll, self.before, 1610612736, 5000)
        seen, progress = {}, []
        fsr4_dll.fetch(self.dest, recipe, progress=lambda d, t: progress.append((d, t)), spool=_spool(remote, seen))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.dll)
        self.assertEqual(progress[-1], (recipe["packed_size"], recipe["packed_size"]))
        self.assertTrue(fsr4_dll.is_valid(self.dest, recipe))
        self.assertEqual(seen["byte_range"], (5000, 5000 + recipe["packed_size"] - 1))
        self.assertEqual((seen["limit"], seen["sha256"]), (recipe["packed_size"], recipe["packed_sha256"]))
        self.assertEqual(seen["hosts"], {"drivers.amd.com"})
        self.assertEqual(seen["headers"]["Referer"], "https://www.amd.com/")
        self.assertEqual(os.path.abspath(seen["folder"]), os.path.abspath(self.dir))
        self.assertEqual(os.listdir(self.dir), ["amdxcffx64.dll"])

    def test_the_real_recipe_is_complete(self):
        keys = {"url", "referer", "packed_offset", "packed_size", "packed_sha256", "unpacked_offset", "dict_size",
                "dll_offset", "dll_size", "sha256"}
        for recipe in fsr4_dll.RECIPES:
            self.assertLessEqual(keys, set(recipe))
            self.assertLessEqual(recipe["packed_size"], dk_net.MAX_BODY)
            self.assertEqual(dk_net.check_url(recipe["url"], fsr4_dll.HOSTS), recipe["url"])

    def test_wrong_bcj_offset_fails_checksum_and_leaves_nothing(self):
        remote, recipe = _package(self.dll, self.before, 123457, 0)
        recipe["unpacked_offset"] = 0
        with self.assertRaisesRegex(fsr4_dll.FetchError, "checksum"):
            fsr4_dll.fetch(self.dest, recipe, spool=_spool(remote))
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_range_too_short_for_the_dll(self):
        remote, recipe = _package(self.dll, self.before, 0, 0)
        recipe["packed_size"] = 1000
        recipe["packed_sha256"] = hashlib.sha256(remote[:1000]).hexdigest()
        with self.assertRaisesRegex(fsr4_dll.FetchError, "ended"):
            fsr4_dll.fetch(self.dest, recipe, spool=_spool(remote))
        self.assertEqual(os.listdir(self.dir), [])

    def test_changed_package_and_network_errors_are_fetch_errors(self):
        remote, recipe = _package(self.dll, self.before, 0, 0)
        with self.assertRaisesRegex(fsr4_dll.FetchError, "AMD changed the package"):
            fsr4_dll.fetch(self.dest, recipe, spool=_spool(b"x" + remote))
        for error in (dk_net.NetError("the server did not send the requested part of the file"),
                      dk_net.NetError("the connection dropped before the end")):
            def spool(*a, **k):
                raise error
            with self.assertRaisesRegex(fsr4_dll.FetchError, str(error)):
                fsr4_dll.fetch(self.dest, recipe, spool=spool)
        self.assertEqual(os.listdir(self.dir), [])

    def test_cancel(self):
        remote, recipe = _package(self.dll, self.before, 0, 0)
        with self.assertRaisesRegex(fsr4_dll.FetchError, "cancelled"):
            fsr4_dll.fetch(self.dest, recipe, spool=_spool(remote), cancelled=lambda: True)

    def test_a_full_disk_names_no_path(self):
        remote, recipe = _package(self.dll, self.before, 0, 0)
        full = OSError(28, "No space left on device", self.dest)
        with unittest.mock.patch("dk_fs.write_atomic", side_effect=full):
            with self.assertRaisesRegex(fsr4_dll.FetchError, "No space left") as caught:
                fsr4_dll.fetch(self.dest, recipe, spool=_spool(remote))
        self.assertNotIn(self.dir, str(caught.exception))

    def test_existing_file_validation(self):
        self.assertFalse(fsr4_dll.is_valid(self.dest))
        with open(self.dest, "wb") as f:
            f.write(b"x" * 10)
        self.assertFalse(fsr4_dll.is_valid(self.dest))


class CybomanFetch(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.tag = "dll-2026-11-22"
        self.variants = {
            "4.1.2l-exact": _make_pe_with_version(4, 1, 2, 10) + b"412le",
            "4.1.2l-lossy": _make_pe_with_version(4, 1, 2, 11) + b"412ll",
            "exact": _make_pe_with_version(4, 1, 2, 12) + b"exact",
            "lossy": _make_pe_with_version(4, 1, 2, 13) + b"lossy",
        }
        self.zip_bytes = _cyboman_zip(self.variants)
        self.sevenz_bytes = b"7z-fake-archive"
        self.sums_lines = [
            f"{hashlib.sha256(self.zip_bytes).hexdigest()}  fsr4.1.2-igpu-cyboman-rdna2-2026-11-22.zip",
            f"{hashlib.sha256(self.sevenz_bytes).hexdigest()}  fsr4.1.2-igpu-cyboman-rdna2-2026-11-22.7z",
        ]
        for v, data in self.variants.items():
            self.sums_lines.append(f"{hashlib.sha256(data).hexdigest()}  igpu-rdna2/{v}/{fsr4_dll.CYBOMAN_DLL_NAME}")
        self.sums_bytes = ("\n".join(self.sums_lines) + "\n").encode()

    def _release_json(self, *, draft=False, prerelease=False, include_7z=True, include_zip=True,
                      include_sums=True, html_url=None):
        base = fsr4_dll.CYBOMAN_RELEASE_BASE
        assets = []
        if include_7z:
            n = "fsr4.1.2-igpu-cyboman-rdna2-2026-11-22.7z"
            assets.append({"name": n, "state": "uploaded", "size": len(self.sevenz_bytes),
                           "digest": f"sha256:{hashlib.sha256(self.sevenz_bytes).hexdigest()}",
                           "browser_download_url": f"{base}/download/{self.tag}/{n}"})
        if include_zip:
            n = "fsr4.1.2-igpu-cyboman-rdna2-2026-11-22.zip"
            assets.append({"name": n, "state": "uploaded", "size": len(self.zip_bytes),
                           "digest": f"sha256:{hashlib.sha256(self.zip_bytes).hexdigest()}",
                           "browser_download_url": f"{base}/download/{self.tag}/{n}"})
        if include_sums:
            assets.append({"name": "SHA256SUMS", "state": "uploaded", "size": len(self.sums_bytes),
                           "digest": f"sha256:{hashlib.sha256(self.sums_bytes).hexdigest()}",
                           "browser_download_url": f"{base}/download/{self.tag}/SHA256SUMS"})
        return {
            "tag_name": self.tag,
            "draft": draft,
            "prerelease": prerelease,
            "html_url": html_url or f"{base}/tag/{self.tag}",
            "assets": assets,
        }

    def _fake_get(self, metadata):
        def get(url, limit, sha256=None, **kw):
            if url == fsr4_dll.CYBOMAN_API_URL:
                return json.dumps(metadata).encode()
            if url.endswith("/SHA256SUMS"):
                if sha256 and hashlib.sha256(self.sums_bytes).hexdigest() != sha256:
                    raise dk_net.Mismatch("bad sums")
                return self.sums_bytes
            raise AssertionError(f"unexpected get({url})")
        return get

    def _fake_spool(self, seen_urls=None):
        def spool(url, folder, limit, sha256=None, **kw):
            if seen_urls is not None:
                seen_urls.append(url)
            body = self.sevenz_bytes if url.endswith(".7z") else self.zip_bytes
            if sha256 and hashlib.sha256(body).hexdigest() != sha256:
                raise dk_net.Mismatch("not expected")
            if kw.get("progress"):
                kw["progress"](len(body))
            return io.BytesIO(body)
        return spool

    def test_pe_version_reader(self):
        p = os.path.join(self.cache, "test.dll")
        with open(p, "wb") as f:
            f.write(_make_pe_with_version(4, 1, 1, 2740))
        self.assertEqual(fsr4_dll.read_pe_version(p), "4.1.1.2740")
        with open(p, "wb") as f:
            f.write(b"not a pe")
        self.assertIsNone(fsr4_dll.read_pe_version(p))

    def test_release_validation(self):
        meta = self._release_json()
        rel = fsr4_dll.check_cyboman_release(get=self._fake_get(meta))
        self.assertEqual(rel["tag"], self.tag)
        self.assertIsNotNone(rel["assets"]["7z"])
        self.assertIsNotNone(rel["assets"]["zip"])
        for bad in (
            self._release_json(draft=True),
            self._release_json(prerelease=True),
            self._release_json(html_url="https://github.com/evil/repo/releases/tag/x"),
            self._release_json(include_7z=False, include_zip=False),
        ):
            with self.assertRaises(fsr4_dll.FetchError):
                fsr4_dll.check_cyboman_release(get=self._fake_get(bad))

    def test_extractor_preference_order(self):
        self.assertEqual(fsr4_dll.available_7z_extractor(which=lambda n: f"/bin/{n}" if n == "7zz" else "/bin/bsdtar"),
                         ("7z", "/bin/7zz"))
        self.assertEqual(fsr4_dll.available_7z_extractor(which=lambda n: "/bin/7z" if n == "7z" else None),
                         ("7z", "/bin/7z"))
        self.assertEqual(fsr4_dll.available_7z_extractor(which=lambda n: "/bin/bsdtar" if n == "bsdtar" else None),
                         ("bsdtar", "/bin/bsdtar"))
        self.assertIsNone(fsr4_dll.available_7z_extractor(which=lambda _n: None))

    def test_zip_fallback_when_no_7z_extractor_and_cache_validation(self):
        # Seed stale exact/ and lossy/ dirs from earlier versions to verify cache pruning
        for stale in ("exact", "lossy"):
            stale_dll = os.path.join(self.cache, stale, fsr4_dll.CYBOMAN_DLL_NAME)
            os.makedirs(os.path.dirname(stale_dll), exist_ok=True)
            with open(stale_dll, "wb") as f:
                f.write(b"stale-" + stale.encode())

        urls = []
        rel = fsr4_dll.check_cyboman_release(get=self._fake_get(self._release_json()))
        meta = fsr4_dll.fetch_cyboman(
            self.cache, rel, get=self._fake_get(self._release_json()),
            spool=self._fake_spool(urls), which=lambda _n: None,
        )
        self.assertEqual(fsr4_dll.CYBOMAN_VARIANTS, ("4.1.2l-exact", "4.1.2l-lossy"))
        self.assertTrue(urls[0].endswith(".zip"))
        self.assertEqual(meta["extracted_from"], "zip")
        status = fsr4_dll.cyboman_status(self.cache)
        self.assertTrue(status["ready"])
        self.assertEqual(status["tag"], self.tag)
        self.assertEqual(set(status["variants"]), {"4.1.2l-exact", "4.1.2l-lossy"})
        for v in ("4.1.2l-exact", "4.1.2l-lossy"):
            with open(fsr4_dll.cyboman_variant_path(self.cache, v), "rb") as f:
                self.assertEqual(f.read(), self.variants[v])
        for skipped in ("exact", "lossy"):
            self.assertFalse(os.path.lexists(os.path.join(self.cache, skipped)))
            with self.assertRaises(ValueError):
                fsr4_dll.cyboman_variant_path(self.cache, skipped)
        # Corrupting one cached variant invalidates status
        with open(fsr4_dll.cyboman_variant_path(self.cache, "4.1.2l-lossy"), "wb") as f:
            f.write(b"corrupt")
        self.assertFalse(fsr4_dll.cyboman_status(self.cache)["ready"])

    def test_7z_extraction_and_runtime_fallback_to_zip_on_7z_error(self):
        # 1) Working 7z extractor path (archive lists all 4; only the 2 Lite variants are requested/extracted)
        requested_7z = []

        def ok_run(argv, timeout, **kw):
            if argv[1] == "l":
                lines = ["----------"]
                for v in ("4.1.2l-exact", "4.1.2l-lossy", "exact", "lossy"):
                    lines.append(f"Path = igpu-rdna2/{v}/{fsr4_dll.CYBOMAN_DLL_NAME}\nFolder = -\nAttributes = A\n")
                return subprocess.CompletedProcess(argv, 0, stdout="\n".join(lines), stderr="")
            requested_7z.extend(argv[argv.index("--") + 1:])
            stage = next(a[2:] for a in argv if a.startswith("-o"))
            for rel_member in argv[argv.index("--") + 1:]:
                variant = rel_member.split("/")[-2]
                p = os.path.join(stage, rel_member)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(self.variants[variant])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        urls = []
        rel = fsr4_dll.check_cyboman_release(get=self._fake_get(self._release_json()))
        meta = fsr4_dll.fetch_cyboman(
            self.cache, rel, get=self._fake_get(self._release_json()),
            spool=self._fake_spool(urls), which=lambda n: "/usr/bin/7z" if n == "7z" else None, run=ok_run,
        )
        self.assertEqual(meta["extracted_from"], "7z")
        self.assertEqual(set(requested_7z), {
            f"igpu-rdna2/4.1.2l-exact/{fsr4_dll.CYBOMAN_DLL_NAME}",
            f"igpu-rdna2/4.1.2l-lossy/{fsr4_dll.CYBOMAN_DLL_NAME}",
        })
        self.assertFalse(os.path.lexists(os.path.join(self.cache, "exact")))
        self.assertFalse(os.path.lexists(os.path.join(self.cache, "lossy")))
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].endswith(".7z"))

        # 2) Runtime failure in bsdtar/7z automatically falls back to .zip
        cache2 = tempfile.mkdtemp()
        urls2 = []

        def broken_run(argv, timeout, **kw):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unsupported 7z filter")

        meta2 = fsr4_dll.fetch_cyboman(
            cache2, rel, get=self._fake_get(self._release_json()),
            spool=self._fake_spool(urls2), which=lambda n: "/usr/bin/bsdtar" if n == "bsdtar" else None,
            run=broken_run,
        )
        self.assertEqual(meta2["extracted_from"], "zip")
        self.assertTrue(urls2[0].endswith(".7z") and urls2[1].endswith(".zip"))
        self.assertTrue(fsr4_dll.cyboman_status(cache2)["ready"])
        self.assertFalse(os.path.lexists(os.path.join(cache2, "exact")))
        self.assertFalse(os.path.lexists(os.path.join(cache2, "lossy")))

    def test_archive_path_resilience_and_security_guards(self):
        # Archive without leading igpu-rdna2/ prefix (custom wrapper dir) still works and skips exact/lossy
        alt_zip = _cyboman_zip(self.variants, prefix="custom-root")
        self.zip_bytes = alt_zip
        rel_meta = self._release_json(include_7z=False, include_sums=False)
        rel = fsr4_dll.check_cyboman_release(get=self._fake_get(rel_meta))
        meta = fsr4_dll.fetch_cyboman(
            self.cache, rel, get=self._fake_get(rel_meta),
            spool=self._fake_spool(), which=lambda _n: None,
        )
        self.assertTrue(fsr4_dll.cyboman_status(self.cache)["ready"])
        self.assertEqual(set(meta["variants"]), {"4.1.2l-exact", "4.1.2l-lossy"})
        self.assertEqual(meta["variants"]["4.1.2l-exact"]["sha256"],
                         hashlib.sha256(self.variants["4.1.2l-exact"]).hexdigest())
        self.assertFalse(os.path.lexists(os.path.join(self.cache, "exact")))

        # Ambiguous duplicate variant entries are refused
        dup_zip = _cyboman_zip(self.variants, prefix="igpu-rdna2",
                               extra=[(f"rx6000/4.1.2l-exact/{fsr4_dll.CYBOMAN_DLL_NAME}", b"other")])
        with self.assertRaisesRegex(fsr4_dll.FetchError, "ambiguous"):
            fsr4_dll._extract_zip_variants(io.BytesIO(dup_zip), self.cache, {}, self.tag, "x.zip", "0" * 64)

        # Traversal & symlink entries in ZIP are refused (even under skipped dirs)
        bad_zip = _cyboman_zip(self.variants, extra=[("../evil.dll", b"x")])
        with self.assertRaisesRegex(fsr4_dll.FetchError, "unsafe"):
            fsr4_dll._extract_zip_variants(io.BytesIO(bad_zip), self.cache, {}, self.tag, "x.zip", "0" * 64)

        for sym_member in (f"igpu-rdna2/4.1.2l-exact/{fsr4_dll.CYBOMAN_DLL_NAME}",
                           f"igpu-rdna2/exact/{fsr4_dll.CYBOMAN_DLL_NAME}"):
            sym_out = io.BytesIO()
            with zipfile.ZipFile(sym_out, "w") as zf:
                info = zipfile.ZipInfo(sym_member)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, b"/etc/passwd")
            with self.assertRaisesRegex(fsr4_dll.FetchError, "symlink"):
                fsr4_dll._extract_zip_variants(io.BytesIO(sym_out.getvalue()), self.cache, {}, self.tag, "x.zip", "0" * 64)

        # SHA256SUMS DLL mismatch is refused
        bad_sums = {f"igpu-rdna2/4.1.2l-exact/{fsr4_dll.CYBOMAN_DLL_NAME}": "0" * 64}
        with self.assertRaisesRegex(fsr4_dll.FetchError, "checksum mismatch"):
            fsr4_dll._extract_zip_variants(io.BytesIO(alt_zip), self.cache, bad_sums, self.tag, "x.zip", "0" * 64)


if __name__ == "__main__":
    unittest.main()
