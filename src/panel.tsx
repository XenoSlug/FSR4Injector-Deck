// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

// The Quick Access panel: Cyboman RDNA 2 Overrides, Official AMD Lite baseline, per-game variant selection,
// launch options, and the About page with updates.
import { useQuickAccessVisible } from "@decky/api";
import { DropdownItem, Field, Focusable, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { FC, ReactNode, useCallback, useEffect, useState } from "react";
import { AboutPage, INSTALL_HINT, UpdateController, UpdateSection } from "./about";
import {
  applyWatermark, cancelCybomanDownload, cancelDownload, checkCybomanRelease, checkForUpdates, cybomanProgress,
  disableGame, downloadCyboman, downloadDll, downloadProgress, enableGame, Game, getState, performUpdate,
  readLaunchOptions, saveLaunchOptions, scanGames, setSetting, State, StateResult, VariantId, verifyLibrary,
} from "./api";
import {
  ANTICHEAT_WARNING, copyText, CYBOMAN_ONLINE_WARNING, hasLaunchOptionsPlugin, hasRadvCswave32, isCybomanVariant,
  isLossyVariant, isNonSteam, LOSSY_TRADEOFF_WARNING, MB, offerToLaunchOptionsPlugin, onlineStatus,
  RADV_CSWAVE32_WARNING, VARIANT_BY_ID, VARIANTS, VERDICT_LABEL,
} from "./shared";
import { errorText } from "./result";
import { ActionButton, colors, confirmAction, infoRow, notify, PageHeader, SectionLink, statusPlaceholder } from "./ui";

const NAME = "FSR4 Injector";

// The QAM remounts the panel on every open; keep the last state so it renders at once and refreshes underneath.
let lastState: State | null = null;

// One updater per plugin load: a check or download keeps running while the QAM is closed or the page changes.
let updates: UpdateController | null = null;
const updater = () => (updates ??= new UpdateController({ check: checkForUpdates, download: performUpdate }, "fsr4injector"));
export const closeUpdates = () => { updates?.dispose(); updates = null; };

const failure = (text?: string): ReactNode => text ? <div style={{ color: colors.error }}>{text}</div> : null;

const badgeStyle = (border: string, bg: string, fg: string) => ({
  display: "inline-block",
  fontSize: "11px",
  lineHeight: "1.2",
  padding: "2px 6px",
  borderRadius: "4px",
  border: `1px solid ${border}`,
  backgroundColor: bg,
  color: fg,
  marginRight: "6px",
  marginTop: "4px",
});

const cybomanStageText = (stage: string) => {
  if (stage === "checking") return "Checking GitHub release…";
  if (stage === "downloading_7z") return "Downloading Cyboman (.7z)…";
  if (stage === "extracting_7z") return "Extracting .7z variants…";
  if (stage === "downloading_zip") return "Downloading Cyboman (.zip fallback)…";
  if (stage === "extracting_zip") return "Extracting .zip variants…";
  return "Downloading Cyboman RDNA 2…";
};

export const Content: FC = () => {
  const visible = useQuickAccessVisible();
  const [state, setStateRaw] = useState<State | null>(lastState);
  const [busy, setBusy] = useState<number | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState("");
  const [bulk, setBulk] = useState<{ kind: "on" | "off" | "repair"; text: string } | null>(null);
  const [error, setError] = useState("");
  const [failed, setFailed] = useState<Record<string, string>>({});
  const [liveRadv, setLiveRadv] = useState<Record<number, boolean>>({});
  const [amdExpanded, setAmdExpanded] = useState(false);
  const [cybomanChecking, setCybomanChecking] = useState(false);
  const [cybomanCheckMsg, setCybomanCheckMsg] = useState("");
  const [page, setPage] = useState<"about" | null>(null);
  const [left, setLeft] = useState(false);

  const setState = useCallback((s: State) => { lastState = s; setStateRaw(s); }, []);
  const mark = useCallback((at: string | number, text = "") => setFailed(f => ({ ...f, [at]: text })), []);

  const take = useCallback(<R extends StateResult>(r: R, title?: string, at?: string | number): R => {
    if (r.state) setState(r.state);
    if (!r.ok && title) notify(title, r.error ?? "Failed");
    if (at !== undefined) mark(at, r.ok ? "" : r.error ?? "Failed");
    return r;
  }, [setState, mark]);

  const refresh = useCallback(async () => {
    const r = await getState();
    take(r);
    setError(r.ok ? "" : r.error ?? "Failed");
  }, [take]);

  const rescan = useCallback(async (manual: boolean) => {
    setScanning(true);
    if (manual) setScanResult("");
    const r = take(await scanGames(), NAME, manual ? "rescan" : undefined);
    if (manual && r.state) {
      setScanResult(`Found ${r.state.games.length} compatible game${r.state.games.length === 1 ? "" : "s"}.`);
    }
    setScanning(false);
  }, [take]);

  const load = useCallback(async () => {
    const r = await getState();
    take(r);
    if (!r.ok) setError(r.error ?? "Failed");
    else if (!r.state?.scanned) rescan(false);
  }, [take, rescan]);
  useEffect(() => { load(); }, [load]);

  // Poll Official AMD Lite download progress
  useEffect(() => {
    if (state?.download.state !== "running") return;
    const t = setInterval(async () => {
      const r = await downloadProgress();
      if (!r.ok || !r.download || !lastState) return;
      if (r.download.state === "running") setState({ ...lastState, download: r.download, dll_ready: !!r.dll_ready });
      else refresh();
    }, 500);
    return () => clearInterval(t);
  }, [state?.download.state, setState, refresh]);

  // Poll Cyboman RDNA 2 download progress
  useEffect(() => {
    if (state?.cyboman?.download.state !== "running") return;
    const t = setInterval(async () => {
      const r = await cybomanProgress();
      if (!r.ok || !r.download || !lastState) return;
      if (r.download.state === "running") {
        setState({
          ...lastState,
          cyboman: { ...lastState.cyboman, download: r.download, ready: !!r.cyboman_ready },
        });
      } else {
        refresh();
      }
    }, 500);
    return () => clearInterval(t);
  }, [state?.cyboman?.download.state, setState, refresh]);

  const startDownload = async () => {
    if (state) setState({ ...state, download: { ...state.download, state: "running", done: 0 } });
    take(await downloadDll(), "AMD FSR4 download failed");
  };

  const startCybomanDownload = async () => {
    setCybomanCheckMsg("");
    if (state) {
      setState({
        ...state,
        cyboman: { ...state.cyboman, download: { ...state.cyboman.download, state: "running", done: 0, stage: "checking" } },
      });
    }
    take(await downloadCyboman(), "Cyboman download failed");
  };

  const checkCyboman = async () => {
    setCybomanChecking(true);
    setCybomanCheckMsg("");
    const r = take(await checkCybomanRelease(), NAME, "cyboman_check");
    if (r.ok && r.state) {
      const latest = r.state.cyboman.latest_tag;
      setCybomanCheckMsg(
        r.state.cyboman.update_available
          ? `Update available: ${latest}`
          : latest
            ? `Up to date (${latest})`
            : "Release checked."
      );
    }
    setCybomanChecking(false);
  };

  const applyVariant = async (game: Game, variant: VariantId, forcePeMismatch = false): Promise<boolean> => {
    try {
      const current = await readLaunchOptions(game.appid);
      if (hasRadvCswave32(current)) setLiveRadv(m => ({ ...m, [game.appid]: true }));
      else if (liveRadv[game.appid]) setLiveRadv(m => ({ ...m, [game.appid]: false }));

      const res = variant === "off"
        ? take(await disableGame(game.appid, current), game.name, game.appid)
        : take(await enableGame(game.appid, current, variant, forcePeMismatch), game.name, game.appid);

      if (!res.ok) {
        if (res.pe_mismatch && !forcePeMismatch) {
          const detected = res.detected_version || "unknown";
          const expected = res.expected_version || "4.1.1.2740";
          confirmAction(
            `Unexpected ${game.name} DLL version`,
            `Found amd_fidelityfx_upscaler_dx12.dll version ${detected}, expected ${expected} (OptiScaler / FSR 4.1.1). ` +
              "Swapping an incompatible version can crash the game. Swap anyway?",
            "Swap anyway",
            () => { void selectVariant(game, variant, true); },
            { destructive: true },
          );
        }
        return false;
      }
      if (res.launch_options !== undefined && res.launch_options !== current) {
        if (!take(await saveLaunchOptions(game.appid, res.launch_options), game.name, game.appid).ok && variant !== "off") {
          return false;
        }
      }
      if (res.kept?.length) notify(game.name, `Left files you changed in place: ${res.kept.join(", ")}`);
      return true;
    } catch (e) {
      notify(game.name, errorText(e));
      mark(game.appid, errorText(e));
      return false;
    }
  };

  const selectVariant = async (game: Game, variant: VariantId, forcePeMismatch = false) => {
    setBusy(game.appid);
    await applyVariant(game, variant, forcePeMismatch);
    setBusy(null);
  };

  const canCybomanNow = (s: State) => s.cyboman.ready && !!s.gpu?.model && !s.error;
  const canNativeNow = (s: State) => s.dll_ready && s.shim_ready && !!s.gpu?.model && !s.error;

  const pickBulkVariant = (s: State, g: Game): VariantId | null => {
    const cyboReady = canCybomanNow(s);
    const nativeReady = canNativeNow(s);
    const peOk = !g.upscaler_version || g.upscaler_version === s.cyboman.expected_pe_version;
    if (cyboReady && g.cyboman_eligible && peOk) return "4.1.2l-exact";
    if (nativeReady && g.native_eligible !== false) return "official-lite";
    return null;
  };

  const applyAll = async (on: boolean) => {
    if (!state) return;
    const candidates = state.games.filter(g => (on ? !g.enabled && !g.anticheat : g.enabled));
    const targets = on ? candidates.filter(g => onlineStatus(g.appid) === "offline" && pickBulkVariant(state, g)) : candidates;
    const skipped = candidates.length - targets.length;
    let ok = 0;
    setBusy(-1);
    for (const [i, g] of targets.entries()) {
      setBulk({ kind: on ? "on" : "off", text: `${i + 1}/${targets.length}` });
      const targetVariant = on ? pickBulkVariant(state, g) : "off";
      if (targetVariant && await applyVariant(g, targetVariant)) ok++;
    }
    setBulk(null);
    setBusy(null);
    const failedGames = targets.length - ok;
    const games = (n: number) => `${n} game${n === 1 ? "" : "s"}`;
    notify(NAME, (on ? `FSR4 turned on in ${games(ok)}.` : `${games(ok)} restored to how they were.`) +
      (failedGames ? ` ${games(failedGames)} could not be changed; see the messages above.` : "") +
      (skipped ? ` Skipped as online, anti-cheat, or missing OptiScaler/PE match: ${games(skipped)}.` : ""));
  };

  const repairAll = async () => {
    setBusy(-1);
    try {
      const next = take(await verifyLibrary(), NAME, "repair").state;
      if (!next) return;
      const targets = next.games.filter(g => g.enabled);
      const needsNative = targets.some(g => (g.backend ?? "native") === "native");
      const needsCyboman = targets.some(g => g.backend === "cyboman");
      if (needsNative && !next.dll_ready) {
        const damaged = "Official AMD FSR4 library is missing or damaged. Download it again before repairing.";
        notify(NAME, damaged);
        mark("repair", damaged);
        return;
      }
      if (needsCyboman && !next.cyboman.ready) {
        const damaged = "Cyboman RDNA 2 cache is missing or damaged. Download it again before repairing.";
        notify(NAME, damaged);
        mark("repair", damaged);
        return;
      }
      let ok = 0;
      for (const [i, g] of targets.entries()) {
        setBulk({ kind: "repair", text: `${i + 1}/${targets.length}` });
        const variant: VariantId = g.variant && g.variant !== "off" ? g.variant : "official-lite";
        if (await applyVariant(g, variant)) ok++;
      }
      notify(NAME, `Repaired ${ok} of ${targets.length} game${targets.length === 1 ? "" : "s"}.`);
    } finally {
      setBulk(null);
      setBusy(null);
    }
  };

  const setWatermark = async (on: boolean) => {
    setBusy(-1);
    try {
      const next = take(await setSetting("watermark", on), NAME, "watermark").state;
      for (const g of next?.games.filter(g => g.enabled && g.managed_options) ?? []) {
        const current = await readLaunchOptions(g.appid);
        const res = take(await applyWatermark(g.appid, current), g.name, g.appid);
        if (res.ok && res.launch_options !== undefined && res.launch_options !== current) {
          take(await saveLaunchOptions(g.appid, res.launch_options), g.name, g.appid);
        }
      }
    } catch (e) {
      notify(NAME, errorText(e));
      mark("watermark", errorText(e));
    }
    setBusy(null);
    refresh();
  };

  const copy = async (text: string) =>
    notify(NAME, (await copyText(text)) ? `Copied: ${text}` : "Copying is not available here.");

  const navigate = (next: "about" | null) => {
    setLeft(page === "about");
    setPage(next);
  };

  if (!state) return <>
    {statusPlaceholder({ title: NAME, label: `${NAME} unavailable`, error, onRetry: () => { setError(""); load(); } })}
    {error && <UpdateSection controller={updater()} version={__PLUGIN_VERSION__} />}
  </>;

  if (page === "about") return <Focusable onCancelActionDescription="Back" onCancelButton={e => {
    e.preventDefault();
    e.stopPropagation();
    navigate(null);
  }}>
    <PageHeader title="About" back={NAME} onBack={() => navigate(null)} focus={visible} />
    <AboutPage name={NAME} version={state.version} license="BSD-3-Clause" source="github.com/XenoSlug/FSR4Injector-Deck"
      description="Steam Deck (Van Gogh / RDNA 2) FSR 4.1.2 Variant Manager integrating Cyboman RDNA 2 overrides and Official AMD Lite."
      notes="Shim (amdxc64.dll) is LGPL-2.1+. Downloaded Cyboman builds are GPL-2.0; Official AMD download is subject to AMD's EULA."
      updates={updater()} home={state.home} install={`${INSTALL_HINT} Your games stay set up.`} />
  </Focusable>;

  const settings = state.settings ?? { auto_launch_options: true, watermark: false, session_toast: false };
  const dl = state.download;
  const cybo = state.cyboman;
  const cyboDl = cybo.download;
  const gpu = state.gpu;
  const canCyboman = canCybomanNow(state);
  const canNative = canNativeNow(state);
  const canEnableAny = canCyboman || canNative;
  const pci = gpu ? `1002:${gpu.device.toString(16).padStart(4, "0")}` : "";
  const showAmdDetails = amdExpanded || dl.state === "running" || dl.state === "error";

  const gameOptions = (g: Game) => {
    const opts = [{ data: "off" as VariantId, label: VARIANT_BY_ID.off.label }];
    const includeCybo = (g.cyboman_eligible && canCyboman) || g.backend === "cyboman" || isCybomanVariant(g.variant);
    if (includeCybo) {
      for (const v of VARIANTS) {
        if (v.cyboman) opts.push({ data: v.id, label: v.label });
      }
    }
    const includeNative = (g.native_eligible !== false && canNative) || g.variant === "official-lite";
    if (includeNative) {
      opts.push({ data: "official-lite", label: VARIANT_BY_ID["official-lite"].label });
    }
    return opts;
  };

  const gameList = (title: string, games: Game[], hint: string) => (
    <PanelSection title={title}>
      {games.length === 0 && (!state.scanned || scanning ? infoRow("Scanning your library…")
        : infoRow(`No compatible ${title.replace("Non-", "non-")} found`, hint))}
      {games.map(g => {
        const selected: VariantId = g.enabled ? (g.variant ?? "official-lite") : "off";
        const opts = gameOptions(g);
        const radvWarn = g.radv_cswave32 || !!liveRadv[g.appid];
        const isOnline = !g.anticheat && onlineStatus(g.appid) === "online";
        const peMismatch = !!g.cyboman_eligible && !!g.upscaler_version
          && g.upscaler_version !== cybo.expected_pe_version && g.backend !== "cyboman";
        return (
          <PanelSectionRow key={g.appid}>
            <DropdownItem
              label={g.name}
              layout="below"
              menuLabel={`${g.name} — FSR4 Variant`}
              rgOptions={opts}
              selectedOption={selected}
              disabled={busy !== null || state.error !== "" || !gpu?.model || (!g.enabled && opts.length <= 1)}
              onChange={opt => void selectVariant(g, opt.data as VariantId)}
              description={<>
                {(g.optiscaler_detected || radvWarn || peMismatch) && (
                  <div>
                    {g.optiscaler_detected && (
                      <span style={badgeStyle(colors.accent, colors.surface, colors.accent)}>OptiScaler detected</span>
                    )}
                    {peMismatch && (
                      <span style={badgeStyle(colors.warning, colors.warningSurface, colors.warning)}>
                        {`PE ${g.upscaler_version} ≠ ${cybo.expected_pe_version}`}
                      </span>
                    )}
                    {radvWarn && (
                      <span style={badgeStyle(colors.warning, colors.warningSurface, colors.warning)}>
                        RADV cswave32 warning
                      </span>
                    )}
                  </div>
                )}
                {g.enabled && <div>{`${VERDICT_LABEL[g.verdict]}. ${g.reason}`}</div>}
                {!g.enabled && !g.cyboman_eligible && canCyboman && (
                  <div style={{ color: colors.muted }}>
                    Requires OptiScaler / {`amd_fidelityfx_upscaler_dx12.dll`} for Cyboman overrides.
                  </div>
                )}
                {isLossyVariant(selected) && (
                  <div style={{
                    color: colors.warning,
                    backgroundColor: colors.warningSurface,
                    padding: "4px 6px",
                    borderRadius: "4px",
                    marginTop: "4px",
                  }}>
                    {LOSSY_TRADEOFF_WARNING}
                  </div>
                )}
                {radvWarn && <div style={{ color: colors.warning, marginTop: "4px" }}>{RADV_CSWAVE32_WARNING}</div>}
                {isOnline && <div style={{ color: colors.error, marginTop: "4px" }}>{CYBOMAN_ONLINE_WARNING}</div>}
                {g.anticheat && <div style={{ color: colors.error, marginTop: "4px" }}>{ANTICHEAT_WARNING}</div>}
                {failure(failed[g.appid])}
              </>}
            />
          </PanelSectionRow>
        );
      })}
    </PanelSection>
  );

  return (
    <>
      <PanelSection title="Cyboman RDNA 2 Overrides">
        {state.error && infoRow("Games cannot be changed", failure(state.error))}
        {gpu?.model && !gpu.verified && infoRow("Beta", "FSR 4 is not tested on this GPU yet. Report problems on GitHub.")}
        <PanelSectionRow>
          {!gpu ? (
            <Field label="No Steam Deck GPU found" description="FSR4 Injector requires a Steam Deck." />
          ) : gpu.model ? (
            <Field label={`${gpu.arch} GPU`}
              description={`PCI ${pci}. Supports Cyboman RDNA 2 & AMD FSR ${state.recipe.fsr} (${gpu.model_name}).`} />
          ) : (
            <Field label="GPU not supported"
              description="Only Steam Deck (Van Gogh) is supported by this edition." />
          )}
        </PanelSectionRow>

        {cyboDl.state === "running" ? (
          <>
            <PanelSectionRow>
              <Field label={cybomanStageText(cyboDl.stage)}>
                {cyboDl.total > 0 ? `${MB(cyboDl.done)} / ${MB(cyboDl.total)}` : "Preparing…"}
              </Field>
            </PanelSectionRow>
            <PanelSectionRow>
              <ActionButton onClick={async () => take(await cancelCybomanDownload())}>Cancel</ActionButton>
            </PanelSectionRow>
          </>
        ) : cybo.ready ? (
          <>
            <PanelSectionRow>
              <Field
                label={`Cyboman RDNA 2 (${cybo.tag || "cached"})`}
                description={`2 Lite variants cached (${cybo.extracted_from === "7z" ? ".7z" : ".zip"})${cybo.update_available ? ` · ${cybo.latest_tag} available` : ""}`}
              >
                Ready
              </Field>
            </PanelSectionRow>
            {cyboDl.state === "error" && infoRow("Last attempt failed", cyboDl.error)}
            {cybo.update_available ? (
              <PanelSectionRow>
                <ActionButton disabled={busy !== null} onClick={startCybomanDownload}>
                  {`Update to ${cybo.latest_tag}`}
                </ActionButton>
              </PanelSectionRow>
            ) : (
              <PanelSectionRow>
                <ActionButton
                  disabled={busy !== null || cybomanChecking}
                  onClick={checkCyboman}
                  description={failure(failed.cyboman_check) || cybomanCheckMsg || undefined}
                >
                  {cybomanChecking ? "Checking GitHub…" : "Check Cyboman release"}
                </ActionButton>
              </PanelSectionRow>
            )}
          </>
        ) : (
          <>
            <PanelSectionRow>
              <Field
                label="Cyboman RDNA 2 needed"
                description={`Fetches zgauthier2000 RDNA 2 iGPU builds (${cybo.extractor === "zip" ? ".zip ~100 MB" : `.7z ~9 MB via ${cybo.extractor}, .zip fallback`}). Single-player / offline only (GPL-2.0).`}
              />
            </PanelSectionRow>
            {cyboDl.state === "error" && infoRow("Last attempt failed", cyboDl.error)}
            <PanelSectionRow>
              <ActionButton disabled={busy !== null} onClick={startCybomanDownload}>
                Download Cyboman RDNA 2
              </ActionButton>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <PanelSection title={`Official AMD Lite (${state.recipe.fsr})`}>
        <PanelSectionRow>
          <Field
            label="Official AMD Lite"
            description={`${state.recipe.driver} · baseline for unmodified DX12 FSR 3.1+ games`}
          >
            {state.dll_ready ? "Ready" : "Not downloaded"}
          </Field>
        </PanelSectionRow>
        {!state.shim_ready && infoRow("Plugin files incomplete", "bin/amdxc64.dll is missing. Reinstall the plugin.")}
        {dl.state !== "running" && (
          <PanelSectionRow>
            <ActionButton onClick={() => setAmdExpanded(v => !v)}>
              {showAmdDetails ? "Hide Official AMD Lite setup" : "Show Official AMD Lite setup"}
            </ActionButton>
          </PanelSectionRow>
        )}
        {showAmdDetails && (
          state.dll_ready ? (
            infoRow("AMD FSR4 DLL", "Verified on disk. Pick Official AMD Lite on any compatible game below.")
          ) : dl.state === "running" ? (
            <>
              <PanelSectionRow>
                <Field label="Downloading from AMD…">{MB(dl.done)} / {MB(dl.total)}</Field>
              </PanelSectionRow>
              <PanelSectionRow>
                <ActionButton onClick={async () => take(await cancelDownload())}>Cancel</ActionButton>
              </PanelSectionRow>
            </>
          ) : (
            <>
              <PanelSectionRow>
                <Field
                  label="AMD FSR4 DLL needed"
                  description={`Fetches the unmodified FSR4 library from AMD's ${state.recipe.driver} package (~43 MB). By downloading you accept the AMD Software EULA: ${state.eula_url}`}
                />
              </PanelSectionRow>
              {dl.state === "error" && infoRow("Last attempt failed", dl.error)}
              <PanelSectionRow>
                <ActionButton disabled={busy !== null} onClick={startDownload}>Accept and download</ActionButton>
              </PanelSectionRow>
            </>
          )
        )}
      </PanelSection>

      {gameList("Steam games", state.games.filter(g => !isNonSteam(g)),
        "Install a game with OptiScaler or FSR 3.1+, then rescan your library.")}
      {gameList("Non-Steam games", state.games.filter(isNonSteam),
        "Add a Windows game with OptiScaler or FSR 3.1+ to Steam, then rescan your library.")}

      <PanelSection title="All games">
        <PanelSectionRow>
          <ActionButton disabled={busy !== null || scanning} onClick={() => rescan(true)}
            description={scanning ? undefined : failure(failed.rescan) || scanResult || undefined}>
            {scanning ? "Scanning…" : "Rescan library"}
          </ActionButton>
        </PanelSectionRow>
        {canEnableAny && state.games.some(g => !g.enabled && !g.anticheat) && (
          <PanelSectionRow>
            <ActionButton disabled={busy !== null} onClick={() => applyAll(true)}
              description="Sets up eligible offline games (4.1.2 Lite — Exact for OptiScaler 4.1.1.2740, else Official AMD Lite). Online and anti-cheat games are skipped.">
              {bulk?.kind === "on" ? `Turning on… ${bulk.text}` : "Turn on FSR4 in offline games"}
            </ActionButton>
          </PanelSectionRow>
        )}
        {state.games.some(g => g.enabled) && (
          <PanelSectionRow>
            <ActionButton disabled={busy !== null} onClick={() => applyAll(false)}
              description="Restores original DLLs and launch options for every game across both backends. Do this before uninstalling.">
              {bulk?.kind === "off" ? `Restoring… ${bulk.text}` : "Turn off FSR4 and restore all games"}
            </ActionButton>
          </PanelSectionRow>
        )}
        {state.games.some(g => g.enabled) && (
          <PanelSectionRow>
            <ActionButton disabled={busy !== null} onClick={repairAll}
              description={<>Re-checks cached DLLs and repairs configured games. Externally modified Cyboman DLLs are flagged, not overwritten.{failure(failed.repair)}</>}>
              {bulk?.kind === "repair" ? `Repairing… ${bulk.text}` : "Repair FSR4 in all games"}
            </ActionButton>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ToggleField
            label="Tell me when FSR4 runs"
            description={<>A short notification during the game once Official AMD Lite runs FSR 4.{failure(failed.session_toast)}</>}
            checked={settings.session_toast}
            disabled={busy !== null}
            onChange={async on => take(await setSetting("session_toast", on), NAME, "session_toast")}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Launch options">
        <PanelSectionRow>
          <ToggleField
            label="Add launch option automatically"
            description={<>Manages WINEDLLOVERRIDES / watermarks per game automatically. Off: leave game launch options alone.{failure(failed.auto_launch_options)}</>}
            checked={settings.auto_launch_options}
            disabled={busy !== null}
            onChange={async on => take(await setSetting("auto_launch_options", on), NAME, "auto_launch_options")}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Show FSR4 watermark"
            description={<>Adds env MLSR-WATERMARK=1 (Cyboman) or FSR_WATERMARK=1 (Official AMD Lite) to managed games.{failure(failed.watermark)}</>}
            checked={settings.auto_launch_options && settings.watermark}
            disabled={busy !== null || !settings.auto_launch_options}
            onChange={setWatermark}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ActionButton onClick={() => copy(state.copy_options.cyboman_watermark)}
            description={state.copy_options.cyboman_watermark}>
            Copy Cyboman watermark
          </ActionButton>
        </PanelSectionRow>
        <PanelSectionRow>
          <ActionButton onClick={() => copy(state.copy_options.plain)} description={state.copy_options.plain}>
            Copy AMD Lite launch options
          </ActionButton>
        </PanelSectionRow>
        <PanelSectionRow>
          <ActionButton onClick={() => copy(state.copy_options.watermark)} description={state.copy_options.watermark}>
            Copy AMD Lite + watermark
          </ActionButton>
        </PanelSectionRow>
        {hasLaunchOptionsPlugin() && (
          <PanelSectionRow>
            <ActionButton onClick={() => offerToLaunchOptionsPlugin(state.copy_options)}
              description={"Adds FSR4 Injector and watermark options to Decky Launch Options."}>
              Add to Decky Launch Options
            </ActionButton>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection>
        <SectionLink title="About" description={`Version ${state.version} · updates`} focus={visible && left}
          icon={<><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-10h.01" /></>} onOpen={() => navigate("about")} />
      </PanelSection>
    </>
  );
};
