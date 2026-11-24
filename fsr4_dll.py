# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""Fetching FSR 4 DLLs (Official AMD amdxcffx64.dll and Cyboman RDNA 2 overrides) without redistributing binaries.

Official AMD path:
The DLL ships only inside AMD's Adrenalin installer, a 1.7 GB 7z SFX compressed as a few solid LZMA2+BCJ blocks.
7-Zip's multithreaded LZMA2 resets its dictionary every so often, so decoding can start at a reset chunk shortly
before the DLL. A recipe pins that chunk's offsets, the exact packed range that holds the DLL and the SHA-256 of
both (made with tools/find_recipe.py); dk_net fetches that range (~43 MiB) beside the destination, it is decoded in
one streaming pass, and only the verified DLL is kept (published by dk_fs).

Cyboman RDNA 2 Overrides path (zgauthier2000/fsr4-shader-performance-overrides):
Fetches the latest GitHub Release metadata, selects the Steam Deck / RDNA 2 iGPU archive (.7z when 7zz/7z/bsdtar
is available, otherwise falling back to .zip via Python's built-in zipfile, including automatic runtime fallback if
.7z extraction fails), extracts only the Steam Deck Lite variants (4.1.2l-exact, 4.1.2l-lossy), verifies archive
and per-DLL SHA-256 checksums, and caches them locally.
"""

import hashlib
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import tempfile
import zipfile

import dk_fs
import dk_net
import dk_process
from dk_fs import sha256_file

RECIPES = [
    {
        "driver": "Adrenalin 26.10.1",
        "fsr": "4.1.2",
        "file_version": "2.4.0.4370",
        "url": "https://drivers.amd.com/drivers/whql-amd-software-adrenalin-edition-26.10.1-win11-c.exe",
        "referer": "https://www.amd.com/",
        "packed_offset": 857273233,  # LZMA2 dictionary-reset chunk, absolute file offset
        "packed_size": 44843008,  # bytes from that chunk until the DLL is decoded completely
        "packed_sha256": "820aeb5ad7e077e5a380551d9e604d0cae413c08c3576fa9002dfedf33b84be7",
        "unpacked_offset": 1610612736,  # position of that chunk in the block's decoded stream (BCJ needs it)
        "dict_size": 2**24,  # the 7z block's LZMA2 dictionary
        "dll_offset": 124861144,  # DLL start, counted from the chunk
        "dll_size": 85684104,
        "sha256": "c33a287f3048bf02311cae430b6a977500b94ca7b315cd7bff751e6b03e483e3",
    },
]
RECIPE = RECIPES[0]
EULA_URL = "https://www.amd.com/en/legal/eula/amd-software-eula.html"
HOSTS = frozenset({"drivers.amd.com"})
DEADLINE = 1800  # 43 MiB still arrive at 25 kB/s
CHUNK = 1 << 20
USER_AGENT = "Mozilla/5.0 (FSR4Injector)"  # AMD's CDN is known to answer this one


class FetchError(Exception):
    pass


def is_valid(path, recipe=RECIPE):
    try:
        return os.path.getsize(path) == recipe["dll_size"] and sha256_file(path) == recipe["sha256"]
    except OSError:
        return False


class _Decoded:
    """The DLL decoded from the packed range, as a file object for dk_fs.write_atomic. It reaches its end only with
    the pinned SHA-256; anything else raises, so write_atomic never publishes it."""

    def __init__(self, packed, recipe, lzma):
        self.packed, self.recipe, self.lzma_error = packed, recipe, lzma.LZMAError
        self.decoder = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_X86, "start_offset": recipe["unpacked_offset"] % 2**32},
            {"id": lzma.FILTER_LZMA2, "dict_size": recipe["dict_size"]},
        ])
        self.skip, self.got, self.digest = recipe["dll_offset"], 0, hashlib.sha256()

    def read(self, _size=-1):
        size = self.recipe["dll_size"]
        while self.got < size:
            chunk = self.packed.read(CHUNK)
            if not chunk:
                raise FetchError("download ended before the DLL was complete")
            try:
                data = self.decoder.decompress(chunk)
            except self.lzma_error as e:
                raise FetchError(f"AMD package does not match the recipe ({e})") from e
            if self.skip:
                cut = min(self.skip, len(data))
                self.skip -= cut
                data = data[cut:]
            data = data[:size - self.got]
            if data:
                self.got += len(data)
                self.digest.update(data)
                return data
        if self.digest.hexdigest() != self.recipe["sha256"]:
            raise FetchError("checksum mismatch: AMD changed the package, a new recipe is needed")
        return b""


def fetch(dest, recipe=RECIPE, progress=None, cancelled=None, spool=dk_net.spool):
    """Download, decode and verify the DLL into dest. Leaves nothing behind on failure.

    progress(done_bytes, total_bytes) follows the download from AMD; cancelled() stops it.
    """
    try:
        import lzma  # imported here so a Python without it still loads the plugin and can say why
    except ImportError as e:
        raise FetchError("this Decky Loader's Python has no lzma module; the DLL cannot be decoded") from e
    first, size = recipe["packed_offset"], recipe["packed_size"]
    try:
        packed = spool(recipe["url"], os.path.dirname(os.path.abspath(dest)), size, recipe["packed_sha256"],
                       hosts=HOSTS, headers={"User-Agent": USER_AGENT, "Referer": recipe["referer"]},
                       byte_range=(first, first + size - 1), deadline=DEADLINE, cancel=cancelled,
                       progress=(lambda done: progress(done, size)) if progress else None)
    except dk_net.Mismatch as e:
        raise FetchError("checksum mismatch: AMD changed the package, a new recipe is needed") from e
    except dk_net.NetError as e:
        raise FetchError(str(e)) from e
    with packed:
        try:
            dk_fs.write_atomic(dest, _Decoded(packed, recipe, lzma), 0o644)
        except OSError as e:
            raise FetchError(f"the DLL could not be saved ({dk_fs.shown(e)})") from e


# ---- PE FileVersion inspection --------------------------------------------------------------------------------

_VS_FIXEDFILEINFO_SIG = 0xFEEF04BD


def read_pe_version(path):
    """Return the PE FileVersion 'A.B.C.D' from RT_VERSION, or None when absent/unreadable/not a PE."""
    try:
        with open(path, "rb") as f:
            dos = f.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return None
            e_lfanew = struct.unpack_from("<I", dos, 0x3C)[0]
            if e_lfanew > 65536:
                return None
            f.seek(e_lfanew)
            pe_hdr = f.read(24)
            if len(pe_hdr) < 24 or pe_hdr[:4] != b"PE\0\0":
                return None
            num_sections = struct.unpack_from("<H", pe_hdr, 6)[0]
            opt_size = struct.unpack_from("<H", pe_hdr, 20)[0]
            if opt_size < 96 or opt_size > 4096 or num_sections == 0 or num_sections > 96:
                return None
            opt = f.read(opt_size)
            if len(opt) < opt_size:
                return None
            magic = struct.unpack_from("<H", opt, 0)[0]
            if magic == 0x10B:  # PE32
                num_rva_off, dd_off = 92, 96
            elif magic == 0x20B:  # PE32+
                num_rva_off, dd_off = 108, 112
            else:
                return None
            if opt_size < dd_off + 24:
                return None
            num_dirs = struct.unpack_from("<I", opt, num_rva_off)[0]
            if num_dirs <= 2:
                return None
            res_rva, res_size = struct.unpack_from("<II", opt, dd_off + 16)
            if not res_rva or not res_size:
                return None
            sec_raw = f.read(num_sections * 40)
            if len(sec_raw) < num_sections * 40:
                return None
            sections = [struct.unpack_from("<IIII", sec_raw, i * 40 + 8) for i in range(num_sections)]

            def rva_to_off(rva):
                for vsize, va, raw_size, raw_ptr in sections:
                    span = max(vsize, raw_size)
                    if va <= rva < va + span and (rva - va) < raw_size:
                        return raw_ptr + (rva - va)
                return None

            def read_dir(dir_rva):
                off = rva_to_off(dir_rva)
                if off is None:
                    return []
                f.seek(off)
                hdr = f.read(16)
                if len(hdr) < 16:
                    return []
                named, numbered = struct.unpack_from("<HH", hdr, 12)
                count = named + numbered
                if count > 512:
                    return []
                raw = f.read(count * 8)
                if len(raw) < count * 8:
                    return []
                return [struct.unpack_from("<II", raw, i * 8) for i in range(count)]

            for type_id, type_off in read_dir(res_rva):
                if type_id != 16 or not (type_off & 0x80000000):  # RT_VERSION
                    continue
                for _, name_off in read_dir(res_rva + (type_off & 0x7FFFFFFF)):
                    if not (name_off & 0x80000000):
                        continue
                    for _, lang_off in read_dir(res_rva + (name_off & 0x7FFFFFFF)):
                        if lang_off & 0x80000000:
                            continue
                        data_entry_off = rva_to_off(res_rva + lang_off)
                        if data_entry_off is None:
                            continue
                        f.seek(data_entry_off)
                        entry = f.read(16)
                        if len(entry) < 16:
                            continue
                        data_rva, data_size = struct.unpack_from("<II", entry, 0)
                        data_off = rva_to_off(data_rva)
                        if data_off is None or data_size < 52:
                            continue
                        f.seek(data_off)
                        blob = f.read(min(data_size, 4096))
                        for i in range(0, len(blob) - 52 + 1, 4):
                            sig, _, ms, ls = struct.unpack_from("<IIII", blob, i)
                            if sig == _VS_FIXEDFILEINFO_SIG:
                                return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (OSError, struct.error, ValueError):
        return None
    return None


# ---- Cyboman RDNA 2 Overrides (zgauthier2000/fsr4-shader-performance-overrides) -----------------------------

CYBOMAN_REPO = "zgauthier2000/fsr4-shader-performance-overrides"
CYBOMAN_API_URL = f"https://api.github.com/repos/{CYBOMAN_REPO}/releases/latest"
CYBOMAN_RELEASE_BASE = f"https://github.com/{CYBOMAN_REPO}/releases"
CYBOMAN_API_HOSTS = frozenset({"api.github.com"})
CYBOMAN_ASSET_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
CYBOMAN_REDIRECT_HOSTS = CYBOMAN_ASSET_HOSTS - {"github.com"}
CYBOMAN_VARIANTS = ("4.1.2l-exact", "4.1.2l-lossy")
CYBOMAN_PRUNED_VARIANTS = ("exact", "lossy")
CYBOMAN_DEFAULT_VARIANT = "4.1.2l-exact"
CYBOMAN_DLL_NAME = "amd_fidelityfx_upscaler_dx12.dll"
CYBOMAN_EXPECTED_PE_VERSION = "4.1.1.2740"
CYBOMAN_ASSET_PREFIX = "fsr4.1.2-igpu-cyboman-rdna2-"
CYBOMAN_MAX_ARCHIVE = 250 * 1024 * 1024
CYBOMAN_MAX_EXTRACTED = 128 * 1024 * 1024
CYBOMAN_MAX_MEMBERS = 512
CYBOMAN_META = "meta.json"
CYBOMAN_ARCHIVE_DEADLINE = 1800
_DIGEST_RE = re.compile(r"sha256:([0-9a-fA-F]{64})")
_SUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")


def _parse_sha256sums(text):
    """Parse sha256sum lines into {normalized_rel_path: lower_hex_sha256}."""
    out = {}
    for line in (text or "").splitlines():
        m = _SUM_LINE_RE.match(line.strip())
        if not m:
            continue
        rel = m.group(2).replace("\\", "/").lstrip("./")
        out[rel] = m.group(1).lower()
    return out


def _validate_release_asset(asset, tag):
    """Validate one GitHub release asset dict; return sanitized info dict or None."""
    if not isinstance(asset, dict):
        return None
    name, url = asset.get("name"), asset.get("browser_download_url")
    size, state = asset.get("size"), asset.get("state")
    if (not isinstance(name, str) or not name or "/" in name or "\\" in name
            or state != "uploaded"
            or url != f"{CYBOMAN_RELEASE_BASE}/download/{tag}/{name}"
            or type(size) is not int or not 0 < size <= CYBOMAN_MAX_ARCHIVE):
        return None
    digest = asset.get("digest")
    sha256 = None
    if digest is not None:
        m = _DIGEST_RE.fullmatch(digest) if isinstance(digest, str) else None
        if not m:
            return None
        sha256 = m.group(1).lower()
    return {"name": name, "url": url, "size": size, "sha256": sha256}


def check_cyboman_release(get=dk_net.get, cancel=None):
    """Fetch and verify latest Cyboman RDNA 2 release metadata from GitHub."""
    try:
        raw = get(CYBOMAN_API_URL, 1 << 20, hosts=CYBOMAN_API_HOSTS, redirects=False,
                  headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                  deadline=20, cancel=cancel)
    except dk_net.NetError as e:
        if e.status == 404:
            raise FetchError("No published Cyboman release was found on GitHub.") from e
        if e.status in (403, 429):
            raise FetchError("GitHub's API rate limit was reached. Try again later.") from e
        raise FetchError(f"GitHub could not check Cyboman releases ({e}).") from e
    try:
        data = dk_fs.parse_json(raw, dk_fs.require_object)
    except ValueError as e:
        raise FetchError("GitHub returned unreadable Cyboman release metadata.") from e
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag or "/" in tag or "\\" in tag or ".." in tag:
        raise FetchError("GitHub returned no valid Cyboman release tag.")
    if data.get("draft") is not False or data.get("prerelease") is not False:
        raise FetchError("Only published stable Cyboman releases are supported.")
    if data.get("html_url") != f"{CYBOMAN_RELEASE_BASE}/tag/{tag}":
        raise FetchError("Release metadata does not belong to the Cyboman repository.")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise FetchError("GitHub returned unreadable Cyboman release assets.")

    found = {"7z": [], "zip": [], "sha256sums": []}
    for raw_asset in assets:
        if not isinstance(raw_asset, dict):
            continue
        name = raw_asset.get("name")
        if not isinstance(name, str):
            continue
        if name.startswith(CYBOMAN_ASSET_PREFIX) and name.endswith(".7z"):
            found["7z"].append(raw_asset)
        elif name.startswith(CYBOMAN_ASSET_PREFIX) and name.endswith(".zip"):
            found["zip"].append(raw_asset)
        elif name == "SHA256SUMS":
            found["sha256sums"].append(raw_asset)

    picked = {}
    for kind in ("7z", "zip", "sha256sums"):
        if len(found[kind]) == 1:
            info = _validate_release_asset(found[kind][0], tag)
            picked[kind] = info
        else:
            picked[kind] = None

    if not picked["7z"] and not picked["zip"]:
        raise FetchError("The latest Cyboman release has no RDNA 2 iGPU archive yet.")
    return {
        "tag": tag,
        "html_url": data["html_url"],
        "published_at": data.get("published_at") if isinstance(data.get("published_at"), str) else "",
        "assets": picked,
    }


def available_7z_extractor(which=shutil.which):
    """Return ('7z', path) or ('bsdtar', path) if a .7z-capable extractor exists on PATH, else None."""
    for name in ("7zz", "7z"):
        path = which(name)
        if path:
            return "7z", path
    path = which("bsdtar")
    if path:
        return "bsdtar", path
    return None


def _safe_member_parts(raw_name):
    """Validate an archive member path; return normalized POSIX path or raise FetchError."""
    if (not isinstance(raw_name, str) or not raw_name or len(raw_name) > 1024
            or "\\" in raw_name or ":" in raw_name or raw_name.startswith("/")
            or any(ord(c) < 32 or ord(c) == 127 for c in raw_name)):
        raise FetchError("archive contains an unsafe entry path")
    trimmed = raw_name.rstrip("/")
    parts = trimmed.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise FetchError("archive contains an unsafe entry path")
    return trimmed, parts


def _match_variant_members(member_names):
    """Match <variant>/amd_fidelityfx_upscaler_dx12.dll suffixes safely without hardcoding top-level folder."""
    matches = {v: [] for v in CYBOMAN_VARIANTS}
    for raw_name in member_names:
        if raw_name.endswith("/"):
            _safe_member_parts(raw_name)
            continue
        norm, parts = _safe_member_parts(raw_name)
        if len(parts) >= 2 and parts[-1].lower() == CYBOMAN_DLL_NAME and parts[-2].lower() in matches:
            matches[parts[-2].lower()].append(norm)
    selected = {}
    for variant in CYBOMAN_VARIANTS:
        hits = matches[variant]
        if len(hits) > 1:
            raise FetchError(f"archive has ambiguous entries for variant {variant}")
        if not hits:
            raise FetchError(f"archive is missing {variant}/{CYBOMAN_DLL_NAME}")
        selected[variant] = hits[0]
    return selected


def _expected_variant_hash(sums, variant, member_path):
    """Look up SHA-256 for a matched variant from SHA256SUMS if available."""
    if not sums:
        return None
    if member_path in sums:
        return sums[member_path]
    igpu_key = f"igpu-rdna2/{variant}/{CYBOMAN_DLL_NAME}"
    if igpu_key in sums:
        return sums[igpu_key]
    return None


def _parse_7z_slt(output):
    """Parse `7z l -slt` output into [(path, is_dir, is_symlink, encrypted)]."""
    sep = output.find("----------")
    if sep < 0:
        raise FetchError("unreadable 7z archive listing")
    body = output[sep:]
    entries = []
    cur = {}
    for line in body.splitlines() + [""]:
        stripped = line.strip()
        if not stripped:
            if cur and "Path" in cur:
                path = cur["Path"]
                is_dir = cur.get("Folder") == "+" or "D" in cur.get("Attributes", "")
                mode = cur.get("Mode", "")
                attrs = cur.get("Attributes", "")
                is_link = mode.startswith("l") or "L" in attrs or "Symbolic Link" in cur.get("Type", "")
                encrypted = cur.get("Encrypted") == "+"
                entries.append((path, is_dir, is_link, encrypted))
            cur = {}
            continue
        if " = " in line:
            k, _, v = line.partition(" = ")
            cur[k.strip()] = v.strip()
    return entries


def _safe_regular_in_stage(stage, rel_path):
    """Verify every component from stage down to rel_path is real (no symlinks) and return file size + sha256."""
    cur = stage
    parts = rel_path.split("/")
    for part in parts[:-1]:
        cur = os.path.join(cur, part)
        st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise FetchError("extracted archive contains a non-directory or symlink")
    target = os.path.join(cur, parts[-1])
    st = os.lstat(target)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise FetchError("extracted DLL is not a regular file")
    if not 0 < st.st_size <= CYBOMAN_MAX_EXTRACTED:
        raise FetchError("extracted DLL size is out of bounds")
    return target, st.st_size, sha256_file(target)


def _prune_stale_cyboman_variants(cache_dir):
    """Remove stale full-desktop variant directories (exact/, lossy/) from cache_dir without following symlinks."""
    for stale in CYBOMAN_PRUNED_VARIANTS:
        stale_path = os.path.join(cache_dir, stale)
        try:
            if os.path.islink(stale_path):
                os.unlink(stale_path)
            elif os.path.isdir(stale_path):
                shutil.rmtree(stale_path, ignore_errors=True)
            elif os.path.lexists(stale_path):
                os.remove(stale_path)
        except OSError:
            pass


def _publish_staged_variants(cache_dir, staged, tag, extracted_from, asset_name, asset_sha256):
    """Atomically publish verified Lite variant DLLs and meta.json into cache_dir, pruning stale desktop dirs."""
    variants_meta = {}
    for variant in CYBOMAN_VARIANTS:
        src_path, size, digest = staged[variant]
        dest_path = os.path.join(cache_dir, variant, CYBOMAN_DLL_NAME)
        with open(src_path, "rb") as f:
            dk_fs.write_atomic(dest_path, f, 0o644)
        variants_meta[variant] = {
            "sha256": digest,
            "size": size,
            "pe_version": read_pe_version(dest_path) or "",
        }
    meta = {
        "release_tag": tag,
        "extracted_from": extracted_from,
        "asset_name": asset_name,
        "asset_sha256": asset_sha256,
        "variants": variants_meta,
    }
    dk_fs.write_json(os.path.join(cache_dir, CYBOMAN_META), meta)
    _prune_stale_cyboman_variants(cache_dir)
    return meta


def _extract_zip_variants(archive_file, cache_dir, sums, tag, asset_name, asset_sha256, cancelled=None):
    """Safely extract and verify the Cyboman Lite variants from a ZipFile-readable binary file."""
    try:
        with zipfile.ZipFile(archive_file) as zf:
            members = zf.infolist()
            if not members or len(members) > CYBOMAN_MAX_MEMBERS:
                raise FetchError("ZIP archive is empty or has too many entries")
            seen = set()
            by_name = {}
            for m in members:
                if m.orig_filename != m.filename:
                    raise FetchError("ZIP archive has mismatched filename")
                norm, _ = _safe_member_parts(m.filename)
                folded = norm.casefold() + ("/" if m.is_dir() else "")
                if folded in seen:
                    raise FetchError("ZIP archive has duplicate entries")
                seen.add(folded)
                if m.is_dir():
                    continue
                if m.flag_bits & 1:
                    raise FetchError("ZIP archive contains encrypted entries")
                if m.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise FetchError("ZIP archive uses an unsupported compression method")
                mode = m.external_attr >> 16
                if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                    raise FetchError("ZIP archive contains a symlink or special file")
                if not 0 < m.file_size <= CYBOMAN_MAX_EXTRACTED:
                    raise FetchError("ZIP entry size is out of bounds")
                by_name[norm] = m

            selected = _match_variant_members(by_name)
            stage = tempfile.mkdtemp(prefix=".cyboman-zip-", dir=cache_dir)
            try:
                staged = {}
                for variant, member_path in selected.items():
                    info = by_name[member_path]
                    out_path = os.path.join(stage, f"{variant}.dll")
                    digest = hashlib.sha256()
                    total = 0
                    with zf.open(info) as src, open(out_path, "wb") as dst:
                        while chunk := src.read(1 << 16):
                            if cancelled and cancelled():
                                raise FetchError("cancelled")
                            total += len(chunk)
                            if total > info.file_size:
                                raise FetchError("ZIP entry exceeded declared size")
                            digest.update(chunk)
                            dst.write(chunk)
                    if total != info.file_size:
                        raise FetchError("ZIP entry was truncated")
                    got_sha = digest.hexdigest()
                    want_sha = _expected_variant_hash(sums, variant, member_path)
                    if want_sha and got_sha != want_sha:
                        raise FetchError(f"checksum mismatch for {variant}/{CYBOMAN_DLL_NAME}")
                    staged[variant] = (out_path, total, got_sha)
                return _publish_staged_variants(cache_dir, staged, tag, "zip", asset_name, asset_sha256)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError) as e:
        if isinstance(e, FetchError):
            raise
        raise FetchError(f"Cyboman ZIP extraction failed ({dk_fs.shown(e)})") from e


def _extract_7z_variants(archive_path, extractor, cache_dir, sums, tag, asset_name, asset_sha256,
                         cancelled=None, run=dk_process.run):
    """Safely inspect, extract, and verify the Cyboman Lite variants from a .7z archive."""
    kind, bin_path = extractor
    if cancelled and cancelled():
        raise FetchError("cancelled")
    try:
        if kind == "7z":
            listed = run([bin_path, "l", "-slt", archive_path], timeout=60, capture_output=True)
            if listed.returncode != 0:
                raise FetchError("7z could not list the Cyboman archive")
            entries = _parse_7z_slt(listed.stdout)
            if not entries or len(entries) > CYBOMAN_MAX_MEMBERS:
                raise FetchError("7z archive is empty or has too many entries")
            member_names = []
            for path, is_dir, is_link, encrypted in entries:
                if is_link or encrypted:
                    raise FetchError("7z archive contains symlinks or encrypted entries")
                _safe_member_parts(path)
                if not is_dir:
                    member_names.append(path)
            selected = _match_variant_members(member_names)
            extract_cmd = lambda stage: [bin_path, "x", "-y", f"-o{stage}", archive_path, "--", *selected.values()]
        else:  # bsdtar
            listed = run([bin_path, "-tf", archive_path], timeout=60, capture_output=True)
            if listed.returncode != 0:
                raise FetchError("bsdtar could not read the .7z archive")
            raw_names = [line.rstrip("\r\n") for line in listed.stdout.splitlines() if line.strip()]
            if not raw_names or len(raw_names) > CYBOMAN_MAX_MEMBERS:
                raise FetchError("7z archive is empty or has too many entries")
            selected = _match_variant_members(raw_names)
            extract_cmd = lambda stage: [bin_path, "-xf", archive_path, "-C", stage,
                                         "--no-same-owner", "--no-same-permissions", "--", *selected.values()]
    except (OSError, subprocess.SubprocessError) as e:
        raise FetchError(f"7z inspection failed ({dk_fs.shown(e)})") from e

    if cancelled and cancelled():
        raise FetchError("cancelled")
    stage = tempfile.mkdtemp(prefix=".cyboman-7z-", dir=cache_dir)
    try:
        try:
            extracted = run(extract_cmd(stage), timeout=180, capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            raise FetchError(f"7z extraction failed ({dk_fs.shown(e)})") from e
        if extracted.returncode != 0:
            raise FetchError("7z extraction failed with non-zero status")
        staged = {}
        for variant, member_path in selected.items():
            path, size, got_sha = _safe_regular_in_stage(stage, member_path)
            want_sha = _expected_variant_hash(sums, variant, member_path)
            if want_sha and got_sha != want_sha:
                raise FetchError(f"checksum mismatch for {variant}/{CYBOMAN_DLL_NAME}")
            staged[variant] = (path, size, got_sha)
        return _publish_staged_variants(cache_dir, staged, tag, "7z", asset_name, asset_sha256)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def cyboman_variant_path(cache_dir, variant):
    """Path to a cached Cyboman variant DLL inside cache_dir."""
    if variant not in CYBOMAN_VARIANTS:
        raise ValueError(f"unknown Cyboman variant: {variant}")
    return os.path.join(cache_dir, variant, CYBOMAN_DLL_NAME)


def cyboman_status(cache_dir):
    """Validate cached Cyboman variant DLLs against meta.json. Returns {ready, tag, extracted_from, variants}."""
    empty = {"ready": False, "tag": "", "extracted_from": "", "variants": {}}
    try:
        meta = dk_fs.read_json(os.path.join(cache_dir, CYBOMAN_META), {}, validate=dk_fs.require_object)
    except OSError:
        return empty
    tag = meta.get("release_tag")
    variants = meta.get("variants")
    if not isinstance(tag, str) or not tag or not isinstance(variants, dict):
        return empty
    for variant in CYBOMAN_VARIANTS:
        info = variants.get(variant)
        if not isinstance(info, dict) or not isinstance(info.get("sha256"), str) or type(info.get("size")) is not int:
            return empty
        path = cyboman_variant_path(cache_dir, variant)
        try:
            if os.path.islink(path) or os.path.getsize(path) != info["size"] or sha256_file(path) != info["sha256"]:
                return empty
        except OSError:
            return empty
    return {
        "ready": True,
        "tag": tag,
        "extracted_from": meta.get("extracted_from") if isinstance(meta.get("extracted_from"), str) else "",
        "variants": {variant: variants[variant] for variant in CYBOMAN_VARIANTS},
    }


def _resolve_archive_sha256(asset, sums):
    """Require at least one SHA-256 (asset.digest or SHA256SUMS) and ensure they agree when both exist."""
    api_sha = asset.get("sha256")
    sums_sha = sums.get(asset["name"]) if sums else None
    if api_sha and sums_sha and api_sha != sums_sha:
        raise FetchError(f"checksum mismatch between GitHub metadata and SHA256SUMS for {asset['name']}")
    sha = api_sha or sums_sha
    if not sha:
        raise FetchError(f"no trusted SHA-256 digest is available for {asset['name']}")
    return sha


def fetch_cyboman(cache_dir, release=None, progress=None, cancelled=None, *, on_stage=None,
                  get=dk_net.get, spool=dk_net.spool, which=shutil.which, run=dk_process.run):
    """Download, verify, and cache the Cyboman RDNA 2 Lite variants into cache_dir.

    Uses the smaller .7z asset when 7zz/7z/bsdtar is available, and automatically falls back to the .zip asset
    if no 7z extractor is present or if .7z extraction fails at runtime.
    """
    os.makedirs(cache_dir, exist_ok=True)
    release = release or check_cyboman_release(get=get, cancel=cancelled)
    tag = release["tag"]
    assets = release["assets"]

    sums = {}
    if assets.get("sha256sums"):
        s_asset = assets["sha256sums"]
        try:
            raw_sums = get(s_asset["url"], s_asset["size"], sha256=s_asset.get("sha256"),
                           hosts=CYBOMAN_ASSET_HOSTS, redirects=CYBOMAN_REDIRECT_HOSTS,
                           deadline=60, cancel=cancelled)
            sums = _parse_sha256sums(raw_sums.decode("utf-8", errors="replace"))
        except dk_net.NetError as e:
            raise FetchError(f"Cyboman SHA256SUMS could not be downloaded ({e}).") from e

    extractor = available_7z_extractor(which=which)
    asset_7z = assets.get("7z")
    asset_zip = assets.get("zip")

    def _download_zip():
        if not asset_zip:
            raise FetchError("No Cyboman .zip fallback archive is available in this release.")
        zip_sha = _resolve_archive_sha256(asset_zip, sums)
        total = asset_zip["size"]
        if on_stage:
            on_stage("downloading_zip", asset_zip)
        if progress:
            progress(0, total)
        try:
            spooled = spool(asset_zip["url"], cache_dir, total, zip_sha,
                            hosts=CYBOMAN_ASSET_HOSTS, redirects=CYBOMAN_REDIRECT_HOSTS,
                            deadline=CYBOMAN_ARCHIVE_DEADLINE, cancel=cancelled,
                            progress=(lambda done: progress(done, total)) if progress else None)
        except dk_net.Mismatch as e:
            raise FetchError("checksum mismatch: Cyboman ZIP archive did not match its SHA-256") from e
        except dk_net.NetError as e:
            raise FetchError(str(e)) from e
        with spooled:
            if on_stage:
                on_stage("extracting_zip", asset_zip)
            return _extract_zip_variants(spooled, cache_dir, sums, tag, asset_zip["name"], zip_sha,
                                         cancelled=cancelled)

    if extractor and asset_7z:
        sha_7z = _resolve_archive_sha256(asset_7z, sums)
        total = asset_7z["size"]
        if on_stage:
            on_stage("downloading_7z", asset_7z)
        if progress:
            progress(0, total)
        try:
            spooled = spool(asset_7z["url"], cache_dir, total, sha_7z,
                            hosts=CYBOMAN_ASSET_HOSTS, redirects=CYBOMAN_REDIRECT_HOSTS,
                            deadline=CYBOMAN_ARCHIVE_DEADLINE, cancel=cancelled,
                            progress=(lambda done: progress(done, total)) if progress else None)
        except dk_net.Mismatch as e:
            raise FetchError("checksum mismatch: Cyboman 7z archive did not match its SHA-256") from e
        except dk_net.NetError as e:
            if cancelled and cancelled() or not asset_zip:
                raise FetchError(str(e)) from e
            # If .7z download itself failed on CDN and .zip is available, try .zip fallback
            return _download_zip()

        tmp_7z = os.path.join(cache_dir, f".cyboman-{secrets.token_hex(6)}.7z")
        try:
            with spooled:
                dk_fs.write_atomic(tmp_7z, spooled, 0o600, no_links=True)
            if on_stage:
                on_stage("extracting_7z", asset_7z)
            return _extract_7z_variants(tmp_7z, extractor, cache_dir, sums, tag, asset_7z["name"], sha_7z,
                                        cancelled=cancelled, run=run)
        except FetchError as e:
            if cancelled and cancelled():
                raise
            # Runtime .7z extractor failure (e.g. bsdtar lacking 7z/BCJ filter) -> fall back to .zip
            if asset_zip and "checksum mismatch" not in str(e):
                return _download_zip()
            raise
        except OSError as e:
            if cancelled and cancelled():
                raise FetchError("cancelled") from e
            if asset_zip:
                return _download_zip()
            raise FetchError(f"Cyboman 7z archive could not be staged ({dk_fs.shown(e)})") from e
        finally:
            try:
                os.remove(tmp_7z)
            except OSError:
                pass

    return _download_zip()
