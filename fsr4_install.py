# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""Installing, switching, and safely rolling back FSR 4 backends per game.

Two isolated backends share a single per-game manifest:
  * Backend A (Cyboman RDNA 2 Overrides, primary for Steam Deck): swaps amd_fidelityfx_upscaler_dx12.dll
    after backing up the original DLL to amd_fidelityfx_upscaler_dx12.dll.fsr4deck.orig with SHA-256 verification
    and PE version inspection (expected 4.1.1.2740). Optional watermark uses `env MLSR-WATERMARK=1`.
  * Backend B (Official AMD Lite): adds amdxc64.dll + amdxcffx64.dll + fsr4injector.gpu alongside the game's
    FidelityFX DLL and merges amdxc64=n,b (+ optional FSR_WATERMARK=1) into WINEDLLOVERRIDES.

Rules that make rollback safe:
  * every file we place or swap has its SHA-256 recorded in manifest.json (protected by dk_fs .last-good);
  * we never delete or overwrite an unrecognized file (hash mismatch leaves files in place and reports kept);
  * switching between backends rolls back the previous backend cleanly before applying the new one while
    preserving the game's pre-plugin launch_original.
"""

import calendar
import hashlib
import json
import os
import re
import secrets
import time

import dk_fs
import fsr4_dll
from dk_fs import sha256_file

SHIM = "amdxc64.dll"
FFX = "amdxcffx64.dll"
GPU = "fsr4injector.gpu"  # the GPU's real family/revision for the shim
STATUS = "fsr4injector.status.json"
UPSCALER_DLL = "amd_fidelityfx_upscaler_dx12.dll"
BACKUP_SUFFIX = ".fsr4deck.orig"
UPSCALER_BACKUP = UPSCALER_DLL + BACKUP_SUFFIX

BACKEND_NATIVE = "native"
BACKEND_CYBOMAN = "cyboman"
MODE_NATIVE = "native"  # backwards-compatible alias
VARIANT_OFFICIAL_LITE = "official-lite"
CYBOMAN_VARIANTS = fsr4_dll.CYBOMAN_VARIANTS
ALL_VARIANTS = (*CYBOMAN_VARIANTS, VARIANT_OFFICIAL_LITE)

# Required for Official AMD Lite: without it Wine loads its builtin amdxc64 from system32 and ignores ours.
TOKEN = "WINEDLLOVERRIDES=amdxc64=n,b"
ENTRY = "amdxc64=n,b"  # our entry inside WINEDLLOVERRIDES
_OVERRIDES = re.compile(r"""^WINEDLLOVERRIDES=(["']?)(.*)\1$""")
_SHELL_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*=.*$")
WATERMARK = "FSR_WATERMARK=1"  # the shim turns it into AMD's own on-screen FSR 4 watermark
CYBOMAN_WATERMARK = "MLSR-WATERMARK=1"  # hyphenated env var requires `env` in /bin/sh
CYBOMAN_WATERMARK_CMD = f"env {CYBOMAN_WATERMARK}"


def launch_prefix(watermark=False, backend=BACKEND_NATIVE):
    """What goes in front of %command%; also what the panel's copy buttons copy."""
    if backend == BACKEND_CYBOMAN:
        return CYBOMAN_WATERMARK_CMD if watermark else ""
    return f"{TOKEN} {WATERMARK}" if watermark else TOKEN


class InstallError(Exception):
    pass


class PeVersionWarning(InstallError):
    """Raised when amd_fidelityfx_upscaler_dx12.dll PE version differs from 4.1.1.2740 and not forced."""

    def __init__(self, detected, expected=fsr4_dll.CYBOMAN_EXPECTED_PE_VERSION):
        self.detected = detected
        self.expected = expected
        found = detected or "unknown"
        super().__init__(
            f"{UPSCALER_DLL} is version {found} (expected {expected} from OptiScaler / FSR 4.1.1). "
            "Confirm to swap anyway, or keep the original file."
        )


def entry_backend(entry):
    """Normalized backend ('native' | 'cyboman') for a manifest entry (v1 entries default to 'native')."""
    return entry.get("backend") or BACKEND_NATIVE


def entry_variant(entry):
    """Normalized variant id for a manifest entry (v1 entries default to 'official-lite')."""
    backend = entry_backend(entry)
    default = VARIANT_OFFICIAL_LITE if backend == BACKEND_NATIVE else fsr4_dll.CYBOMAN_DEFAULT_VARIANT
    return entry.get("variant") or default


def _check_manifest(data):
    """dk_fs's validate hook: accepts both v1 and v2 entries."""
    dk_fs.require_object(data)
    for appid, e in data.items():
        if (not appid.isdigit() or not isinstance(e, dict) or not isinstance(e.get("target_dir"), str)
                or not isinstance(e.get("files"), dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in e["files"].items())
                or not isinstance(e.get("launch_original", ""), str)
                or not isinstance(e.get("launch_applied") or "", str)):
            raise ValueError(f"entry {appid!r} is not a game set up by this plugin")
        backend = e.get("backend")
        if backend is not None and backend not in (BACKEND_NATIVE, BACKEND_CYBOMAN):
            raise ValueError(f"entry {appid!r} has unknown backend {backend!r}")
        backup = e.get("backup")
        if backup is not None:
            if (not isinstance(backup, dict) or not isinstance(backup.get("file"), str)
                    or not isinstance(backup.get("target"), str) or not isinstance(backup.get("sha256"), str)):
                raise ValueError(f"entry {appid!r} has invalid backup metadata")


class Manifest:
    """appid -> {target_dir, backend, variant, files: {name: sha256}, backup?, launch_original, launch_applied, ...}

    It is the only record of what the plugin put into game folders. A damaged file comes back from its last-good
    copy; when that is impossible (or the file cannot be read) `problem` says why and nothing is saved over it, so
    the record is never lost to a later change."""

    def __init__(self, path):
        self.path, self.entries, self.problem, self.recovered = path, {}, "", ""
        try:
            loaded = dk_fs.load_json(path, {}, validate=_check_manifest)
        except OSError as e:
            self.problem = f"The list of games set up could not be read ({dk_fs.shown(e)})."
            return
        if loaded.status == "lost":
            self.problem = ("The list of games set up is damaged, so no game can be changed. It is kept as it is; "
                            "the Decky log has the details.")
        self.recovered = loaded.detail if loaded.status in ("lost", "recovered") else ""
        self.entries = {int(k): v for k, v in loaded.value.items()}

    def save(self):
        if self.problem:
            raise InstallError(self.problem)
        dk_fs.write_json(self.path, {str(k): v for k, v in self.entries.items()})


def _split_words(options):
    """Split launch options on unquoted whitespace while keeping original quotes inside each word intact."""
    words, buf, quote, escape = [], [], None, False
    for ch in options or "":
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escape = True
            continue
        if ch in ('"', "'"):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            buf.append(ch)
            continue
        if ch.isspace() and quote is None:
            if buf:
                words.append("".join(buf))
                buf.clear()
            continue
        buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def _override_entries(word):
    """The entries of a WINEDLLOVERRIDES=... launch option word, or None for any other word."""
    match = _OVERRIDES.match(word)
    return [e for e in match.group(2).split(";") if e] if match else None


def _without_amdxc64(entries):
    """Entries with amdxc64 taken out; in a list sharing one mode ('dxgi,amdxc64=n,b') the others keep it."""
    kept = []
    for entry in entries:
        dlls, sep, mode = entry.partition("=")
        rest = [d for d in dlls.split(",") if d and d.lower() != "amdxc64"]
        if rest:
            kept.append(",".join(rest) + sep + mode)
    return kept


def _overrides_word(entries):
    value = ";".join(entries)
    return f'WINEDLLOVERRIDES="{value}"' if ";" in value else f"WINEDLLOVERRIDES={value}"


def _without_ours(words):
    """Launch option words with our amdxc64 entry taken out of WINEDLLOVERRIDES (dropped once it is empty)."""
    out = []
    for word in words:
        entries = _override_entries(word)
        if entries is not None:
            kept = _without_amdxc64(entries)
            if not kept:
                continue
            word = _overrides_word(kept)
        out.append(word)
    return out


def has_token(options):
    """Whether the launch options load our amdxc64: amdxc64=n,b in WINEDLLOVERRIDES, alone or in a DLL list."""
    for word in _split_words(options):
        for entry in _override_entries(word) or []:
            dlls, _, mode = entry.partition("=")
            if mode == "n,b" and "amdxc64" in (d.lower() for d in dlls.split(",")):
                return True
    return False


def with_token(options, watermark=False):
    """Launch options with amdxc64=n,b merged into WINEDLLOVERRIDES (and the watermark next to it).

    Without other overrides this is our token in front, as it always was; %command% is added when the options had
    none (they were the game's arguments).
    """
    words = [w for w in _split_words(options) if not (watermark and w == WATERMARK)]
    for i, word in enumerate(words):
        entries = _override_entries(word)
        if entries is not None:
            words[i] = _overrides_word(_without_amdxc64(entries) + [ENTRY])
            if watermark:
                words.insert(i + 1, WATERMARK)
            break
    else:
        if "%command%" not in words:
            words = ["%command%"] + words
        words = launch_prefix(watermark, BACKEND_NATIVE).split() + words
    if "%command%" not in words:
        words.append("%command%")
    return " ".join(words)


def without_token(current, original, applied):
    """Undo with_token: the original when untouched since, else just our entry and watermark taken out (other
    overrides and a watermark the user already had stay)."""
    current = (current or "").strip()
    if current == applied:
        return original
    users_watermark = WATERMARK in _split_words(original)
    words = [w for w in _without_ours(_split_words(current)) if w != WATERMARK or users_watermark]
    if words == ["%command%"] and "%command%" not in original:
        return original
    return " ".join(words)


def _is_env_cmd(word):
    return word in ("env", "/usr/bin/env")


def has_cyboman_watermark(options):
    """Whether launch options carry MLSR-WATERMARK=1 inside a POSIX-safe `env` prefix."""
    words = _split_words(options)
    cmd_idx = words.index("%command%") if "%command%" in words else len(words)
    i = 0
    while i < cmd_idx:
        if _is_env_cmd(words[i]):
            j = i + 1
            while j < cmd_idx and _ENV_ASSIGN.match(words[j]):
                if words[j] == CYBOMAN_WATERMARK:
                    return True
                j += 1
        i += 1
    return False


def with_cyboman_watermark(options, on=True):
    """Inject or remove `env MLSR-WATERMARK=1` cleanly for /bin/sh before %command%."""
    if not on:
        return without_cyboman_watermark(options, (options or "").strip(), None, force_remove=True)
    words = _split_words(options)
    if "%command%" not in words:
        words = ["%command%"] + words
    cmd_idx = words.index("%command%")
    # Drop any broken bare MLSR-WATERMARK=... not inside an env command before %command%
    cleaned = []
    in_env = False
    for idx, w in enumerate(words):
        if idx < cmd_idx:
            if _is_env_cmd(w):
                in_env = True
                cleaned.append(w)
                continue
            if in_env and _ENV_ASSIGN.match(w):
                cleaned.append(w)
                continue
            in_env = False
            if w.startswith("MLSR-WATERMARK="):
                continue
        cleaned.append(w)
    words = cleaned
    cmd_idx = words.index("%command%")

    if has_cyboman_watermark(" ".join(words)):
        return " ".join(words)

    pos = 0
    while pos < cmd_idx and _SHELL_ASSIGN.match(words[pos]):
        pos += 1
    if pos < cmd_idx and _is_env_cmd(words[pos]):
        j = pos + 1
        while j < cmd_idx and _ENV_ASSIGN.match(words[j]):
            if words[j].startswith("MLSR-WATERMARK="):
                words[j] = CYBOMAN_WATERMARK
                return " ".join(words)
            j += 1
        words.insert(j, CYBOMAN_WATERMARK)
        return " ".join(words)
    words[pos:pos] = ["env", CYBOMAN_WATERMARK]
    return " ".join(words)


def without_cyboman_watermark(current, original, applied, *, force_remove=False):
    """Undo with_cyboman_watermark while preserving pre-existing `env` vars and user-owned MLSR-WATERMARK=1."""
    current = (current or "").strip()
    original = (original or "").strip()
    if applied is not None and current == applied:
        return original
    if not force_remove and has_cyboman_watermark(original):
        return current
    words = _split_words(current)
    cmd_idx = words.index("%command%") if "%command%" in words else len(words)
    out = []
    i = 0
    while i < len(words):
        if i < cmd_idx and _is_env_cmd(words[i]):
            env_word = words[i]
            assigns = []
            j = i + 1
            while j < cmd_idx and _ENV_ASSIGN.match(words[j]):
                if words[j] != CYBOMAN_WATERMARK:
                    assigns.append(words[j])
                j += 1
            if assigns:
                out.append(env_word)
                out.extend(assigns)
            elif not force_remove and _is_env_cmd_bare_in(original):
                out.append(env_word)
            i = j
            continue
        if i < cmd_idx and words[i] == CYBOMAN_WATERMARK:
            i += 1
            continue
        out.append(words[i])
        i += 1
    if not force_remove and out == ["%command%"] and not original:
        return ""
    return " ".join(out)


def _is_env_cmd_bare_in(original):
    words = _split_words(original)
    for i, w in enumerate(words):
        if _is_env_cmd(w) and (i + 1 >= len(words) or not _ENV_ASSIGN.match(words[i + 1])):
            return True
    return False


# Game files: never written through a symlink (dk_fs's no_links), and a failed copy (a full SD card) leaves neither
# half a DLL nor its temp file. The game folder itself may be a link (a game moved to another drive), so it is
# resolved first; no_links alone would refuse it.

def _real(dst):
    return os.path.join(os.path.realpath(os.path.dirname(dst)), os.path.basename(dst))


def _copy(src, dst):
    with open(src, "rb") as f:
        dk_fs.write_atomic(_real(dst), f, no_links=True)


def _link_or_copy(src, dst):
    """A hard link to src where the file system allows it; a copy on another drive (SD card), FAT/exFAT, or where
    links are refused. Never follows a symlink at dst."""
    real_dst = _real(dst)
    if os.path.islink(real_dst):
        raise dk_fs.UnsafePath(f"{os.path.basename(dst)} is a symbolic link")
    tmp = f"{real_dst}.{secrets.token_hex(6)}.fsr4i-tmp"
    try:
        os.link(src, tmp)
    except OSError:
        return _copy(src, dst)
    try:
        os.replace(tmp, real_dst)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write(dst, data):
    dk_fs.write_atomic(_real(dst), data, no_links=True)


def gpu_file_content(gpu):
    return f"{gpu['device']} {gpu['family']} {gpu['rev']}\n".encode()


def _strip_all_plugin_launch_options(options):
    """Strip any plugin-managed native tokens or Cyboman watermark tokens from raw launch_options."""
    stripped = " ".join(_without_ours(_split_words(options)))
    return without_cyboman_watermark(stripped, stripped, None, force_remove=True)


def _enable_native(manifest, game, shim_path, ffx_path, launch_options, gpu=None, launch=True, watermark=False,
                   *, original_override=None):
    appid, target = game["appid"], game["target_dir"]
    sources = {SHIM: shim_path, FFX: ffx_path}
    want = {SHIM: sha256_file(shim_path), FFX: sha256_file(ffx_path)}
    if gpu:
        want[GPU] = hashlib.sha256(gpu_file_content(gpu)).hexdigest()
    known = manifest.entries.get(appid, {}).get("files", {})

    for name in want:
        path = os.path.join(target, name)
        if os.path.exists(path) and sha256_file(path) not in (want[name], known.get(name)):
            raise InstallError(f"{name} already exists in the game folder and is not ours; leaving it alone")

    original = (original_override if original_override is not None
                else " ".join(_without_ours(_split_words(launch_options))))
    entry = manifest.entries.get(appid) or {"target_dir": target, "files": {}, "launch_original": original}
    entry["backend"] = BACKEND_NATIVE
    entry["variant"] = VARIANT_OFFICIAL_LITE
    entry["mode"] = MODE_NATIVE
    new_options = with_token(launch_options, watermark) if launch else None
    try:
        for name in want:
            dst = os.path.join(target, name)
            if name == GPU:
                _write(dst, gpu_file_content(gpu))
            else:
                (_link_or_copy if name == FFX else _copy)(sources[name], dst)
            entry["files"][name] = want[name]
            manifest.entries[appid] = entry  # recorded as soon as a file exists, so a crash can still be undone
            manifest.save()
        if launch:
            entry["launch_applied"] = new_options
        entry["enabled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        manifest.save()
    except dk_fs.UnsafePath as e:
        raise InstallError("a file the plugin writes is a link in the game folder; leaving it alone") from e
    except OSError as e:
        raise InstallError(f"could not write to the game folder ({dk_fs.shown(e)})") from e
    return new_options if launch else (launch_options or "").strip()


def enable(manifest, game, shim_path, ffx_path, launch_options, gpu=None, launch=True, watermark=False):
    """Install Official AMD Lite into game["target_dir"]; returns the launch options to apply."""
    return enable_variant(
        manifest, game, VARIANT_OFFICIAL_LITE,
        launch_options=launch_options, launch=launch, watermark=watermark,
        shim_path=shim_path, ffx_path=ffx_path, gpu=gpu,
    )


def _preflight_cyboman(manifest, appid, target, src_dll, force_pe_mismatch):
    """Validate target directory, backup state, and PE version before modifying anything."""
    dst_dll = os.path.join(target, UPSCALER_DLL)
    backup_dll = os.path.join(target, UPSCALER_BACKUP)
    real_dst = _real(dst_dll)
    real_backup = _real(backup_dll)

    if os.path.islink(real_dst) or os.path.islink(real_backup):
        raise InstallError("a file the plugin writes is a link in the game folder; leaving it alone")
    if not os.path.isfile(real_dst):
        raise InstallError(
            f"{UPSCALER_DLL} was not found in the game folder. OptiScaler or FidelityFX SDK 2.x is required "
            "for Cyboman overrides."
        )

    want_sha = sha256_file(src_dll)
    entry = manifest.entries.get(appid)
    if entry and entry_backend(entry) == BACKEND_CYBOMAN:
        backup = entry.get("backup")
        if not backup or not os.path.isfile(real_backup):
            raise InstallError(f"Original backup {UPSCALER_BACKUP} is missing; cannot safely swap.")
        if sha256_file(real_backup) != backup["sha256"]:
            raise InstallError(f"Original backup {UPSCALER_BACKUP} no longer matches its recorded checksum.")
        active_sha = sha256_file(real_dst)
        recorded_sha = entry.get("files", {}).get(UPSCALER_DLL)
        pending_sha = entry.get("pending_sha")
        if active_sha not in (recorded_sha, pending_sha):
            raise InstallError(
                f"{UPSCALER_DLL} was modified outside FSR4 Injector (for example by a game or OptiScaler update); "
                "leaving it alone."
            )
        return want_sha, None

    if os.path.lexists(real_backup):
        raise InstallError(f"{UPSCALER_BACKUP} already exists in the game folder and is not ours; leaving it alone.")

    pe_version = fsr4_dll.read_pe_version(real_dst)
    if pe_version != fsr4_dll.CYBOMAN_EXPECTED_PE_VERSION and not force_pe_mismatch:
        raise PeVersionWarning(pe_version, fsr4_dll.CYBOMAN_EXPECTED_PE_VERSION)
    return want_sha, (sha256_file(real_dst), pe_version or "")


def _enable_cyboman(manifest, game, variant, src_dll, cyboman_tag, launch_options, launch, watermark,
                    force_pe_mismatch, *, original_override=None):
    appid, target = game["appid"], game["target_dir"]
    want_sha, fresh_info = _preflight_cyboman(manifest, appid, target, src_dll, force_pe_mismatch)
    dst_dll = os.path.join(target, UPSCALER_DLL)
    backup_dll = os.path.join(target, UPSCALER_BACKUP)

    existing = manifest.entries.get(appid)
    if existing and entry_backend(existing) == BACKEND_CYBOMAN:
        entry = existing
    else:
        original = (original_override if original_override is not None
                    else without_cyboman_watermark(launch_options, launch_options, None, force_remove=True))
        orig_sha, pe_version = fresh_info
        try:
            _copy(dst_dll, backup_dll)
            if sha256_file(_real(backup_dll)) != orig_sha:
                raise InstallError(f"Backup verification failed for {UPSCALER_BACKUP}.")
        except dk_fs.UnsafePath as e:
            raise InstallError("a file the plugin writes is a link in the game folder; leaving it alone") from e
        except OSError as e:
            raise InstallError(f"could not back up {UPSCALER_DLL} ({dk_fs.shown(e)})") from e
        entry = {
            "target_dir": target,
            "backend": BACKEND_CYBOMAN,
            "variant": variant,
            "release_tag": cyboman_tag,
            "files": {},
            "backup": {
                "file": UPSCALER_BACKUP,
                "target": UPSCALER_DLL,
                "sha256": orig_sha,
                "pe_version": pe_version,
            },
            "launch_original": original,
        }
        manifest.entries[appid] = entry
        manifest.save()

    if not launch:
        new_options = None
    elif watermark:
        new_options = with_cyboman_watermark(launch_options, True)
    elif existing and entry_backend(existing) == BACKEND_CYBOMAN:
        new_options = without_cyboman_watermark(launch_options, existing.get("launch_original", ""), None)
    else:
        new_options = (launch_options or "").strip()
    entry["pending_sha"] = want_sha
    manifest.entries[appid] = entry
    manifest.save()
    try:
        _link_or_copy(src_dll, dst_dll)
        entry["backend"] = BACKEND_CYBOMAN
        entry["variant"] = variant
        entry["release_tag"] = cyboman_tag
        entry["files"] = {UPSCALER_DLL: want_sha}
        entry.pop("pending_sha", None)
        if launch:
            entry["launch_applied"] = new_options
        entry["enabled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        manifest.save()
    except dk_fs.UnsafePath as e:
        raise InstallError("a file the plugin writes is a link in the game folder; leaving it alone") from e
    except OSError as e:
        raise InstallError(f"could not write {UPSCALER_DLL} ({dk_fs.shown(e)})") from e
    return new_options if launch else (launch_options or "").strip()


def enable_variant(manifest, game, variant, *, launch_options="", launch=True, watermark=False,
                   shim_path=None, ffx_path=None, gpu=None, cyboman_cache_dir=None, cyboman_tag="",
                   force_pe_mismatch=False):
    """Enable or switch FSR 4 backend/variant for one game; safely rolls back any different active backend first."""
    if variant not in ALL_VARIANTS:
        raise InstallError(f"Unknown FSR4 variant: {variant}")
    appid, target = game["appid"], game["target_dir"]
    target_backend = BACKEND_NATIVE if variant == VARIANT_OFFICIAL_LITE else BACKEND_CYBOMAN

    # Resolve source path upfront before rolling back an existing different backend
    src_dll = None
    if target_backend == BACKEND_CYBOMAN:
        if not cyboman_cache_dir:
            raise InstallError("Download the Cyboman RDNA 2 overrides first.")
        src_dll = fsr4_dll.cyboman_variant_path(cyboman_cache_dir, variant)
        if not os.path.isfile(src_dll):
            raise InstallError("Download the Cyboman RDNA 2 overrides first.")
    else:
        if not (shim_path and ffx_path and os.path.isfile(shim_path) and os.path.isfile(ffx_path)):
            raise InstallError("Download the AMD FSR4 DLL first.")

    prev = manifest.entries.get(appid)
    original_override = None
    if prev and entry_backend(prev) != target_backend:
        # Preflight target preconditions BEFORE rolling back the active backend
        if target_backend == BACKEND_CYBOMAN:
            _preflight_cyboman(manifest, appid, target, src_dll, force_pe_mismatch)
        else:
            want_native = {SHIM: sha256_file(shim_path), FFX: sha256_file(ffx_path)}
            for name, digest in want_native.items():
                path = os.path.join(target, name)
                if os.path.exists(path) and sha256_file(path) != digest:
                    raise InstallError(f"{name} already exists in the game folder and is not ours; leaving it alone")

        saved_original = prev.get("launch_original", "")
        rolled = disable(manifest, appid, launch_options)
        if rolled["kept"]:
            raise InstallError(
                f"Could not cleanly roll back previous backend ({', '.join(rolled['kept'])} changed); leaving files alone."
            )
        launch_options = rolled["launch_options"]
        if prev.get("launch_applied") is not None and launch_options == saved_original:
            original_override = saved_original
        else:
            original_override = _strip_all_plugin_launch_options(launch_options)

    if target_backend == BACKEND_NATIVE:
        return _enable_native(
            manifest, game, shim_path, ffx_path, launch_options,
            gpu=gpu, launch=launch, watermark=watermark, original_override=original_override,
        )
    return _enable_cyboman(
        manifest, game, variant, src_dll, cyboman_tag, launch_options,
        launch, watermark, force_pe_mismatch, original_override=original_override,
    )


def set_watermark(manifest, appid, current, on):
    """Add or take out the appropriate watermark in a game whose launch options we manage; None when we do not."""
    entry = manifest.entries.get(appid)
    if not entry or entry.get("launch_applied") is None:  # None: the user manages them ("" is ours, emptied)
        return None
    if entry_backend(entry) == BACKEND_CYBOMAN:
        if on:
            new = with_cyboman_watermark(current, True)
        else:
            new = without_cyboman_watermark(current, entry.get("launch_original", ""), None)
    else:
        if on:
            new = with_token(current, True)
        else:
            users_watermark = WATERMARK in _split_words(entry.get("launch_original", ""))
            new = " ".join(w for w in _split_words(current) if w != WATERMARK or users_watermark)
    entry["launch_applied"] = new
    manifest.save()
    return new


def refresh_shim(manifest, shim_path):
    """After a plugin update, bring Official AMD Lite games' amdxc64.dll up to the bundled build."""
    try:
        want = sha256_file(shim_path)
    except OSError:
        return 0
    updated = 0
    for entry in manifest.entries.values():
        if entry_backend(entry) != BACKEND_NATIVE:
            continue
        had = entry["files"].get(SHIM)
        path = os.path.join(entry["target_dir"], SHIM)
        try:
            if not had or had == want or sha256_file(path) != had:
                continue
            _copy(shim_path, path)
        except OSError:
            continue
        entry["files"][SHIM] = want
        updated += 1
    if updated:
        manifest.save()
    return updated


def _disable_cyboman(entry, launch_options):
    """Safely restore amd_fidelityfx_upscaler_dx12.dll from .fsr4deck.orig; never delete unrecognized DLLs."""
    target = entry["target_dir"]
    backup = entry.get("backup") or {}
    backup_name = backup.get("file", UPSCALER_BACKUP)
    backup_sha = backup.get("sha256", "")
    active_name = backup.get("target", UPSCALER_DLL)
    expected_variant_sha = entry.get("files", {}).get(active_name, "")

    active_path = os.path.join(target, active_name)
    backup_path = os.path.join(target, backup_name)
    kept = []

    try:
        real_active = _real(active_path)
        real_backup = _real(backup_path)
        if os.path.islink(real_active) or os.path.islink(real_backup):
            if os.path.lexists(real_active):
                kept.append(active_name)
            if os.path.lexists(real_backup):
                kept.append(backup_name)
        else:
            active_exists = os.path.exists(real_active)
            backup_exists = os.path.exists(real_backup)
            active_matches_ours = active_exists and expected_variant_sha and sha256_file(real_active) == expected_variant_sha
            backup_matches_orig = backup_exists and backup_sha and sha256_file(real_backup) == backup_sha

            if (not active_exists or active_matches_ours) and backup_matches_orig:
                _copy(real_backup, active_path)
                if sha256_file(_real(active_path)) == backup_sha:
                    os.remove(real_backup)
                else:
                    kept.extend([active_name, backup_name])
            else:
                if active_exists and not active_matches_ours:
                    kept.append(active_name)
                elif active_exists and not backup_matches_orig:
                    kept.append(active_name)
                if backup_exists:
                    kept.append(backup_name)
    except OSError:
        if os.path.exists(active_path) and active_name not in kept:
            kept.append(active_name)
        if os.path.exists(backup_path) and backup_name not in kept:
            kept.append(backup_name)

    if entry.get("launch_applied") is not None:
        restored = without_cyboman_watermark(
            launch_options, entry.get("launch_original", ""), entry["launch_applied"]
        )
    else:
        restored = (launch_options or "").strip()
    return restored, kept


def disable(manifest, appid, launch_options):
    """Remove what we installed and restore backups/launch options. Returns {launch_options, kept}."""
    entry = manifest.entries.get(appid)
    if not entry:
        return {"launch_options": (launch_options or "").strip(), "kept": []}

    if entry_backend(entry) == BACKEND_CYBOMAN:
        restored, kept = _disable_cyboman(entry, launch_options)
    else:
        kept = []
        for name, digest in entry["files"].items():
            path = os.path.join(entry["target_dir"], name)
            try:
                if not os.path.exists(path):
                    continue
                if sha256_file(path) == digest:
                    os.remove(path)
                else:
                    kept.append(name)
            except OSError:
                kept.append(name)
        for name in (STATUS, STATUS + ".tmp"):  # written by the shim at runtime
            try:
                os.remove(os.path.join(entry["target_dir"], name))
            except OSError:
                pass
        if entry.get("launch_applied") is not None:
            restored = without_token(launch_options, entry.get("launch_original", ""), entry["launch_applied"])
        else:
            restored = (launch_options or "").strip()

    del manifest.entries[appid]
    manifest.save()
    return {"launch_options": restored, "kept": kept}


def cyboman_verdict(entry):
    """(ok | changed, human-readable reason) for a Cyboman-managed game by verifying active + backup hashes."""
    target = entry["target_dir"]
    variant = entry_variant(entry)
    tag = entry.get("release_tag", "")
    tag_note = f" ({tag})" if tag else ""
    active_path = os.path.join(target, UPSCALER_DLL)
    backup = entry.get("backup") or {}
    backup_path = os.path.join(target, backup.get("file", UPSCALER_BACKUP))
    expected_sha = entry.get("files", {}).get(UPSCALER_DLL)
    try:
        if not os.path.isfile(active_path) or sha256_file(active_path) != expected_sha:
            return "changed", f"{UPSCALER_DLL} was modified or missing since {variant} was installed."
        if not os.path.isfile(backup_path) or sha256_file(backup_path) != backup.get("sha256"):
            return "changed", f"Original backup {UPSCALER_BACKUP} is missing or modified."
    except OSError:
        return "changed", f"Could not verify {UPSCALER_DLL} in the game folder."
    return "ok", f"Cyboman {variant}{tag_note} active."


def read_status(manifest, appid):
    """Last session status written by the shim, or None."""
    entry = manifest.entries.get(appid)
    if not entry or entry_backend(entry) != BACKEND_NATIVE:
        return None
    try:
        with open(os.path.join(entry["target_dir"], STATUS), encoding="utf-8", errors="replace") as f:
            status = json.load(f)
    except (OSError, ValueError, RecursionError):
        return None
    return status if isinstance(status, dict) else None


def _epoch(iso):
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _written_since(status, session_start):
    started = _epoch((status or {}).get("started"))
    return started is not None and started >= session_start - 5  # shim clock has 1 s resolution


def running_model(status, session_start):
    """'FSR <version> (<model>)' once FSR 4 runs in the current session, else None."""
    if not session_start or not _written_since(status, session_start) or verdict(status)[0] != "ok":
        return None
    return _model(status)


def _model(status):
    return f"FSR {str(status.get('provider') or '').strip(' *') or '4'} ({status.get('active')})"


def session_verdict(status, session_start, launch_options, managed=True):
    """verdict() for a session that just ended (Official AMD Lite backend)."""
    if session_start and not _written_since(status, session_start):
        if not has_token(launch_options):
            if not managed:
                return "failed", (f"FSR4 Injector was not loaded: add {TOKEN} in front of %command% in the game's "
                                  "launch options (Copy launch options in the plugin).")
            return "failed", (f"FSR4 Injector was not loaded: the game's launch options no longer contain {TOKEN} "
                              "(another tool may have changed them). Toggle the game off and on to repair.")
        return "missed", ("FSR4 Injector's DLL was not loaded in this session (the game did not start DirectX 12, "
                          "for example it ran in DirectX 11 mode or was closed in its launcher).")
    return verdict(status)


def verdict(status):
    """(ok | unused | failed | unknown, human-readable reason) for a session status."""
    if not status:
        return "unknown", "Start the game and use FSR once."
    if not status.get("ffx_requested"):
        return "unused", "Pick AMD FSR as the upscaler in the game's graphics settings."
    active = status.get("active")
    if active in ("INT8_LITE", "INT8", "INT8_NAVI2", "FP8"):
        return "ok", f"{_model(status)} ran in the last session."
    if not status.get("dll_loaded"):
        return "failed", "The AMD FSR4 DLL did not load."
    if not status.get("family"):
        return "failed", f"GPU {status.get('gpu', '?')} is not supported by this plugin version."
    if active == "unknown":
        return "failed", "This AMD DLL version is not recognised; the plugin needs an update."
    if active == "FSR3":
        return "failed", "The AMD DLL refused FSR4 and fell back to FSR3 (likely after a SteamOS/Proton update)."
    return "unknown", "FSR was requested but the result is not known yet."
