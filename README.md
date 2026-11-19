<div align="center">

<img src="FSR4Injector.png" alt="FSR4 Injector" width="760">

[![Release](https://img.shields.io/github/v/release/XenoSlug/FSR4Injector-Deck?style=for-the-badge&label=release&color=C2410C&labelColor=141417)](https://github.com/XenoSlug/FSR4Injector-Deck/releases/latest)
[![GPU](https://img.shields.io/badge/GPU-Steam_Deck_(LCD_+_OLED)-0969DA?style=for-the-badge&labelColor=141417)](#requirements)
[![Requires](https://img.shields.io/badge/requires-Decky_Loader-15803D?style=for-the-badge&labelColor=141417)](https://decky.xyz)
[![License](https://img.shields.io/badge/license-BSD--3_+_LGPL--2.1-424A53?style=for-the-badge&labelColor=141417)](#license)

**AMD's official FSR 4.1.2 upscaler (Lite model) on your Steam Deck (LCD & OLED), in SteamOS Gaming Mode.**

Turn it on per game directly from the Quick Access Menu: AMD's authentic, unmodified FSR 4.1.2 library with the INT8 Lite neural model, running in DirectX 12 games that support FSR 3.1+. No OptiScaler translation layer, no patched binaries on disk.

[Features](#features) · [Requirements](#requirements) · [Deck Performance Guide](#steam-deck-performance-guide) · [Installation](#installation) · [Quick start](#quick-start) · [Troubleshooting](#troubleshooting) · [How it works](#how-it-works)

</div>

---

## What makes this Steam Deck Edition special?

This fork of [Rayekkk/FSR4Injector](https://github.com/Rayekkk/FSR4Injector) is tailored and verified for the **Valve Steam Deck**:

1. **Native Steam Deck LCD & OLED Support:**
   - Pre-configured detection for both **Steam Deck LCD (Van Gogh / Aerith `0x163F`)** and **Steam Deck OLED (Sephiroth `0x1435`)**.
   - Van Gogh (family `144`) is promoted to verified first-class status.
2. **Hardcoded Hardware Fallback Table:**
   - Both Steam Deck device IDs (`0x163F` and `0x1435`) are baked directly into the `amdxc64.dll` C driver extension shim as well as the Python backend, ensuring the AMD library always receives the correct GPU family (`144`) even if per-game `.gpu` files are deleted, unreadable, or missing.
3. **Optimized Scanner:**
   - Scans library roots (including SD cards and non-Steam shortcuts) down to depth 7 so deeply nested Unreal Engine 5 binaries are never missed.
4. **Automated CI/CD Releases:**
   - Automated MinGW-w64 cross-compilation of `amdxc64.dll` and Node.js 24 frontend bundling producing installable release ZIPs on every release tag.

---

## Features

| Feature | What you get |
|---|---|
| **One-tap setup** | Downloads AMD's authentic FSR 4.1.2 library directly from AMD's servers (~43 MB) and validates its SHA-256 |
| **Per-game switch** | Automatically scans and lists installed games with native FSR 3.1+ support (`amd_fidelityfx_*.dll`), allowing per-game toggles |
| **Clean launch options** | Automatically manages `WINEDLLOVERRIDES=amdxc64=n,b` in Steam launch options, cleanly merging with existing user overrides |
| **Verification & watermark** | AMD's real-time watermark on demand (`FSR_WATERMARK=1`), plus per-session validation in `fsr4injector.status.json` |
| **Zero risk to system** | Completely contained in user-space; leaves Proton and system libraries 100% untouched |
| **Clean rollback** | Turning a game off completely removes the DLL links and restores original launch options |

---

## Steam Deck Performance Guide

> [!IMPORTANT]
> The Steam Deck's Van Gogh APU operates on a **15W total SoC budget with 8 RDNA 2 Compute Units (CUs)**. Unlike RDNA 3/3.5, RDNA 2 lacks hardware WMMA matrix engines and executes INT8 neural upscaling via `v_dot4_i32_i8` (DP4A) instructions.
>
> Running AMD's FSR 4.1.2 Lite neural network adds approximately **1.5 ms to 2.5 ms of compute time per frame** on 8 CUs.

### How to get maximum FPS & quality on Deck:
* **Avoid "Quality" Mode (720p -> 800p):** If your game is already GPU-limited at 95%+ utilization, adding a 2 ms neural pass on 8 CUs will slightly decrease net FPS with little visible difference on an 800p screen.
* **Use "Balanced" (540p -> 800p) or "Performance" (480p -> 800p):** FSR 4 Lite's machine-learning temporal reconstruction reconstructs crisp, stable edges and thin lines from 480p/540p that match or exceed FSR 3.1 Quality at 720p. Rendering the game at 480p saves **5 ms to 8 ms of GPU rasterization time**, easily offsetting the 2 ms upscaler pass for a **net framerate boost and superior battery life**.

---

## Requirements

| Requirement | Details |
|---|---|
| **Hardware** | Steam Deck LCD (Aerith, `0x163F`) or Steam Deck OLED (Sephiroth, `0x1435`) |
| **Operating System** | SteamOS in Gaming Mode with [Decky Loader](https://decky.xyz) |
| **Compatibility Tool** | Proton 11 or Proton Experimental |
| **Games** | DirectX 12 games that natively ship `amd_fidelityfx_dx12.dll`, `amd_fidelityfx_upscaler_dx12.dll`, or `amd_fidelityfx_loader_dx12.dll` (FSR 3.1+) |
| **Network** | Internet connection once to download AMD's library (~43 MB) from `drivers.amd.com` |

*Note: Monolithic FSR 2, FSR 3.0, and pure Vulkan games without FidelityFX DX12 modular loaders are not supported by the native driver injector.*

---

## Installation

1. Install [Decky Loader](https://decky.xyz) if you have not already done so.
2. Download the latest `FSR4Injector-<version>.zip` from [Releases](https://github.com/XenoSlug/FSR4Injector-Deck/releases).
3. On your Deck (in Gaming Mode):
   - Open **Quick Access Menu (...) → Decky → Settings (gear icon)**.
   - Under **General**, toggle on **Developer mode**.
   - Navigate to the **Developer** tab, choose **Install Plugin from ZIP**, and select the downloaded ZIP file.
4. Open **FSR4 Injector** in your Decky plugin menu.

---

## Quick start

1. Open **FSR4 Injector** in the Quick Access Menu and tap **Accept and download**. This fetches AMD's authentic FSR 4.1.2 library directly from AMD's official Adrenalin driver CDN.
2. Under **Steam games** or **Non-Steam games**, turn the switch **ON** for your game (e.g., *Ghost of Tsushima*, *God of War Ragnarök*, *Spider-Man*, *Horizon Forbidden West*, *Space Marine 2*).
3. Launch the game from Steam.
4. In the game's display/graphics settings:
   - Select **AMD FSR** as the upscaler.
   - Set the upscaler quality to **Balanced** or **Performance**.
5. *(Optional)* Turn on **"Show AMD's FSR4 watermark"** in the plugin launch options to confirm the top-left HUD displays `FSR4 Lite (INT8)` in real time.

---

## How it works

AMD's official `amdxcffx64.dll` determines which FSR 4 model to execute by querying private kernel driver data (`D3DKMTQueryAdapterInfo`, `KMTQAITYPE_UMDRIVERPRIVATE`) for the GPU family and silicon stepping. Under Proton/Wine, this driver query returns nothing, forcing AMD's library to fall back to legacy FSR 3.

```
Game (D3D12 + FSR 3.1 SDK)
     │
     ▼
amd_fidelityfx_dx12.dll
     │
     ▼
amdxc64.dll (Our Open-Source Shim via WINEDLLOVERRIDES)
     │  Interceps D3DKMTQueryAdapterInfo
     │  Reports Family 144 (Van Gogh) & Revision 3
     ▼
amdxcffx64.dll (Official Signed AMD FSR 4.1.2 Library)
     │  Matches Family 144 -> Selects INT8_LITE model
     ▼
VKD3D-Proton / RADV (Vulkan VK_KHR_shader_integer_dot_product)
     │  Executes INT8 DP4A neural inference on Van Gogh CUs
     ▼
Output to Screen (Crisp Reconstruction at 800p)
```

AMD's authentic signed library remains completely untouched on disk. Nothing is modified in the OS or Proton runtime.

---

## Credits & Upstream

- [Rayekkk/FSR4Injector](https://github.com/Rayekkk/FSR4Injector) - The original project architecture, LZMA2 streaming recipe, and Decky implementation.
- **Etaash Mathamsetty** and [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom) - The original `amdxc64` D3D12 extension implementation.
- [vkd3d-proton](https://github.com/HansKristian-Work/vkd3d-proton) - D3D12 to Vulkan translation on Linux.
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) - Handheld plugin loader for SteamOS.

---

## License

Plugin code: [BSD 3-Clause](LICENSE).
Shim (`bin/amdxc64.dll` & `shim/`): [LGPL-2.1 or later](shim/COPYING.LIB).
AMD software is downloaded separately from AMD's CDN and remains under AMD's EULA. Not affiliated with AMD or Valve.
