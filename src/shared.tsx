// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

// Small helpers of the panel: labels, variants, warnings, online games, Decky Launch Options and the clipboard.
import { Game, State, VariantId } from "./api";

// Steam store categories that mean online play: MMO, Cross-Platform Multiplayer, Online PvP, Online Co-op.
// The generic Multi-player (1) and PvP (49) also cover local-only games, so they do not count.
const ONLINE_CATEGORIES = new Set([20, 27, 36, 38]);

export const onlineStatus = (appid: number): "online" | "offline" | "unknown" => {
  try {
    const categories = appStore?.GetAppOverviewByAppID(appid)?.store_category;
    if (!Array.isArray(categories) || !categories.length) return "unknown";
    return categories.some(c => ONLINE_CATEGORIES.has(c)) ? "online" : "offline";
  } catch {
    return "unknown";
  }
};

// Steam gives games added with "Add a Non-Steam Game" app ids from 2^31 up; its own app ids are far below that.
export const isNonSteam = (g: Game) => g.appid >= 2 ** 31;

export const VERDICT_LABEL: Record<Game["verdict"], string> = {
  ok: "✓ FSR4 active",
  changed: "↻ Re-check needed",
  unused: "FSR not used last session",
  failed: "⚠ FSR4 not active",
  unknown: "Not verified yet",
};

export interface VariantMeta {
  id: VariantId;
  label: string;
  summary: string;
  lossy?: boolean;
  cyboman?: boolean;
}

export const VARIANTS: VariantMeta[] = [
  { id: "off", label: "Off", summary: "Restore original game files and launch options." },
  {
    id: "4.1.2l-exact",
    label: "4.1.2 Lite — Exact (Recommended)",
    summary: "FSR 4.1.2 Lite, bit-exact to AMD Lite (~11–16% faster on RDNA 2).",
    cyboman: true,
  },
  {
    id: "4.1.2l-lossy",
    label: "4.1.2 Lite — Lossy",
    summary: "FSR 4.1.2 Lite, alternating-frame neural pass (~13–22% faster, ~2.1 ms @ 800p).",
    lossy: true,
    cyboman: true,
  },
  {
    id: "official-lite",
    label: "Official AMD Lite",
    summary: "Unmodified AMD amdxcffx64.dll + amdxc64 shim (no OptiScaler needed).",
  },
];

export const VARIANT_BY_ID = Object.fromEntries(VARIANTS.map(v => [v.id, v])) as Record<VariantId, VariantMeta>;

export const isLossyVariant = (v?: VariantId) => v === "4.1.2l-lossy";
export const isCybomanVariant = (v?: VariantId) => v === "4.1.2l-exact" || v === "4.1.2l-lossy";

export const MB = (b: number) => `${(b / 2 ** 20).toFixed(0)} MB`;

export const ANTICHEAT_WARNING =
  "Anti-cheat detected: modified Cyboman DLLs MUST ONLY be used in single-player / offline games. " +
  "Using modified or injected DLLs here can get your account banned.";

export const CYBOMAN_ONLINE_WARNING =
  "Online multiplayer detected: Cyboman builds are modified DLLs for single-player / offline games only " +
  "(skipped by bulk actions).";

export const LOSSY_TRADEOFF_WARNING =
  "4.1.2 Lite — Lossy: runs the neural pass on alternating frames and reprojects motion in between " +
  "(~2.1 ms @ 800p). May introduce uneven frame pacing or minor speckling / exposure lag.";

export const RADV_CSWAVE32_WARNING =
  "RADV_PERFTEST=cswave32 detected: adds ~1.1 ms FSR 4 overhead on SteamOS RADV. Remove cswave32 from launch options.";

/** Inspect launch options string for RADV_PERFTEST=cswave32 (comma-separated or quoted). */
export const hasRadvCswave32 = (options: string): boolean => {
  const words = (options || "").match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [];
  return words.some(w => {
    const m = /^RADV_PERFTEST=(["']?)(.*)\1$/.exec(w);
    return !!m && m[2].split(",").some(flag => flag.trim().toLowerCase() === "cswave32");
  });
};

// Decky Launch Options (github.com/Rayekkk/DeckyLaunchOptions) imports launch options other plugins offer; it asks the
// user to confirm. Stable ids make a second import update these entries instead of adding new ones.
export const hasLaunchOptionsPlugin = () =>
  typeof window !== "undefined" && !!(window as Window & { hasDeckyLaunchOptions?: boolean }).hasDeckyLaunchOptions;
export const offerToLaunchOptionsPlugin = (copy: State["copy_options"]) => {
  const [token, watermark] = copy.watermark.replace(" %command%", "").split(" ");
  const cybomanWatermark = copy.cyboman_watermark.replace(" %command%", "");
  window.dispatchEvent(new CustomEvent("dlo-add-launch-options", {
    detail: [
      { id: "fsr4-injector", group: "FSR4 Injector", name: "FSR4 Injector (Official AMD Lite)", on: token, off: "",
        enableGlobally: false },
      { id: "fsr4-injector-watermark", group: "FSR4 Injector", name: "AMD FSR4 watermark", on: watermark, off: "",
        enableGlobally: false },
      { id: "fsr4-cyboman-watermark", group: "FSR4 Injector", name: "Cyboman FSR4 watermark", on: cybomanWatermark,
        off: "", enableGlobally: false },
    ],
  }));
};

// The Steam client does not always grant the async clipboard API to plugins; fall back to a selected textarea.
export const copyText = async (text: string) => {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
};
