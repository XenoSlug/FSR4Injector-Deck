// SPDX-License-Identifier: BSD-3-Clause
// The panel's real TSX with controlled hooks, RPC calls and timers (DeckyKit's frontend_harness.cjs).
const test = require('node:test');
const assert = require('node:assert/strict');
const { harness, all, find, findText, settle } = require('./frontend_harness.cjs');

const DLO = '~/.dlo/run %command%';
const TOKEN = 'WINEDLLOVERRIDES=amdxc64=n,b';
const game = {
  appid: 1817070,
  name: 'Spider-Man',
  target_dir: '/g',
  anticheat: false,
  enabled: false,
  backend: null,
  variant: 'off',
  release_tag: '',
  native_eligible: true,
  cyboman_eligible: true,
  optiscaler_detected: true,
  upscaler_version: '4.1.1.2740',
  radv_cswave32: false,
  verdict: 'unknown',
  reason: '',
};
const gpuDeck = { device: 0x163f, family: 144, rev: 3, arch: 'Steam Deck (Van Gogh)', model: 'INT8_LITE', model_name: 'INT8 Lite', verified: true };
const cybomanState = (over = {}) => ({
  ready: true,
  tag: 'dll-2026-11-22',
  extracted_from: '7z',
  latest_tag: '',
  update_available: false,
  extractor: '7z',
  repo: 'zgauthier2000/fsr4-shader-performance-overrides',
  variants: ['4.1.2l-exact', '4.1.2l-lossy'],
  expected_pe_version: '4.1.1.2740',
  download: { state: 'idle', done: 0, total: 0, stage: '', error: '' },
  ...over,
});
const state = (over = {}) => ({
  version: '2.0.0', home: '/home/deck', dll_ready: true, shim_ready: true, gpu: gpuDeck, error: '',
  recipe: { driver: 'Adrenalin 26.10.1', fsr: '4.1.2', dll_size: 1 }, eula_url: 'https://x',
  download: { state: 'idle', done: 0, total: 1, error: '' },
  cyboman: cybomanState(over.cyboman),
  scanned: true, games: [game],
  settings: { auto_launch_options: true, watermark: false, session_toast: false },
  copy_options: {
    plain: `${TOKEN} %command%`,
    watermark: `${TOKEN} FSR_WATERMARK=1 %command%`,
    cyboman_watermark: 'env MLSR-WATERMARK=1 %command%',
  },
  ...over,
  ...(over.cyboman ? { cyboman: cybomanState(over.cyboman) } : {}),
});
const ok = (s, extra = {}) => ({ ok: true, state: s, ...extra });

/** The panel with a Steam whose launch options read back what was last set (DLO's by default). */
function panel() {
  const options = {}, applied = [], copied = [], steam = {}, modals = [];
  const h = harness({
    ui: {
      showModal: el => { modals.push(el); return el; },
    },
    globals: {
      appStore: { GetAppOverviewByAppID: id => ({ 1817070: [2, 22], 2: [2, 28], 4: [2, 22], 5: [2, 1, 36, 38] }[id]
        ? { store_category: { 1817070: [2, 22], 2: [2, 28], 4: [2, 22], 5: [2, 1, 36, 38] }[id] } : null) },
      SteamClient: {
        Apps: {
          RegisterForAppDetails(appid, fn) {
            if (steam.stale?.[appid] !== undefined) Promise.resolve().then(() => fn({ strLaunchOptions: steam.stale[appid] }));
            Promise.resolve().then(() => fn({ strLaunchOptions: options[appid] ?? DLO }));
            return { unregister() {} };
          },
          SetAppLaunchOptions(appid, value) { applied.push([appid, value]); if (!steam.ignoreWrites) options[appid] = value; },
        },
        GameSessions: { RegisterForAppLifetimeNotifications(fn) { steam.lifetime = fn; return { unregister() {} }; } },
      },
      navigator: { clipboard: { writeText: async t => { copied.push(t); } } },
    },
  });
  return Object.assign(h, { applied, copied, steam, options, modals });
}

const pending = (h, name) => h.calls.some(c => c.name === name && !c.settled);
const text = tree => all(tree).flatMap(n => [n.props?.label, typeof n.props?.description === 'string' ? n.props.description : null,
  ...[].concat(n.props?.children ?? []).filter(c => typeof c === 'string')]).filter(Boolean).join(' | ');
const button = (tree, label) => all(tree).find(n => n.type === 'ButtonItem' && [].concat(n.props.children).includes(label));
const toggleOf = (tree, label) => all(tree).find(n => n.type === 'ToggleField' && n.props.label === label);
const dropdownOf = (tree, label) => all(tree).find(n => n.type === 'DropdownItem' && n.props.label === label);

async function open(h, s = state()) {
  h.render();
  h.respond('get_state', ok(s));
  await settle();
  return h.render();
}

test('opens with a spinner once, then renders the last state at once and refreshes underneath', async () => {
  const h = panel();
  assert.ok(find(h.render(), 'Spinner'));
  h.respond('get_state', ok(state()));
  await settle();
  assert.ok(!pending(h, 'scan_games'));
  assert.match(text(h.render()), /Spider-Man/);
  const tree = h.remount();
  assert.ok(!find(tree, 'Spinner'));
  assert.match(text(tree), /Spider-Man/);
});

test('a backend that does not answer shows Try again and keeps updates reachable', async () => {
  const h = panel();
  h.render();
  h.fail('get_state');
  await settle();
  let tree = h.render();
  assert.ok(findText(find(tree, 'Field', 'FSR4 Injector unavailable'), 'backend gone'));
  assert.ok(button(tree, 'Check for updates'));
  button(tree, 'Try again').props.onClick();
  await settle();
  assert.ok(find(h.render(), 'Spinner'));
  h.respond('get_state', ok(state()));
  await settle();
  tree = h.render();
  assert.match(text(tree), /Spider-Man/);
});

test('per-game variant selector, badges, lossy warning, and PE mismatch modal', async () => {
  const h = panel();
  const flagged = { ...game, radv_cswave32: true, upscaler_version: '4.0.0.1' };
  let tree = await open(h, state({ games: [flagged] }));
  const dd = dropdownOf(tree, 'Spider-Man');
  assert.equal(dd.props.selectedOption, 'off');
  assert.deepEqual([...dd.props.rgOptions.map(o => o.data)], ['off', '4.1.2l-exact', '4.1.2l-lossy', 'official-lite']);
  assert.deepEqual([...dd.props.rgOptions.map(o => o.label)], [
    'Off',
    '4.1.2 Lite — Exact (Recommended)',
    '4.1.2 Lite — Lossy',
    'Official AMD Lite',
  ]);
  assert.ok(findText(dd.props.description, 'OptiScaler detected'));
  assert.ok(findText(dd.props.description, 'RADV cswave32 warning'));
  assert.ok(findText(dd.props.description, 'PE 4.0.0.1'));

  // Selecting 4.1.2l-exact triggers PE mismatch modal when backend returns pe_mismatch: true
  dd.props.onChange({ data: '4.1.2l-exact', label: '4.1.2 Lite — Exact (Recommended)' });
  await settle();
  const call = h.respond('enable_game', {
    ok: false, error: 'PE mismatch', pe_mismatch: true, detected_version: '4.0.0.1', expected_version: '4.1.1.2740',
    state: state({ games: [flagged] }),
  });
  assert.deepEqual(call.args, [1817070, DLO, '4.1.2l-exact', false]);
  await settle();
  assert.equal(h.modals.length, 1);
  h.modals[0].props.onOK();
  await settle();
  const forced = h.respond('enable_game', ok(state({
    games: [{ ...flagged, enabled: true, backend: 'cyboman', variant: '4.1.2l-lossy', verdict: 'ok', reason: 'Cyboman 4.1.2l-lossy active.' }],
  }), { launch_options: DLO }));
  assert.deepEqual(forced.args, [1817070, DLO, '4.1.2l-exact', true]);
  await settle();
  tree = h.render();
  assert.ok(findText(dropdownOf(tree, 'Spider-Man').props.description, '4.1.2 Lite — Lossy'));

  // Legacy Cyboman manifest entry still exposes only the 4 choices so it can be switched or turned Off
  tree = await open(panel(), state({
    games: [{ ...game, enabled: true, backend: 'cyboman', variant: 'exact', verdict: 'ok', reason: 'Cyboman exact active.' }],
  }));
  assert.deepEqual([...dropdownOf(tree, 'Spider-Man').props.rgOptions.map(o => o.data)],
    ['off', '4.1.2l-exact', '4.1.2l-lossy', 'official-lite']);
});

test('Steam and non-Steam games are two lists with DropdownItems', async () => {
  const h = panel();
  const witcher = { ...game, appid: 3816576258, name: 'Witcher (GOG)', cyboman_eligible: false, optiscaler_detected: false };
  let tree = await open(h, state({ games: [game, witcher] }));
  const sections = all(tree).filter(n => n.type === 'PanelSection').map(n => n.props.title);
  assert.deepEqual(sections.slice(2, 5), ['Steam games', 'Non-Steam games', 'All games']);
  assert.deepEqual([...dropdownOf(tree, 'Witcher (GOG)').props.rgOptions.map(o => o.data)], ['off', 'official-lite']);

  dropdownOf(tree, 'Witcher (GOG)').props.onChange({ data: 'official-lite', label: 'Official AMD Lite' });
  await settle();
  assert.deepEqual(h.respond('enable_game', ok(state({ games: [game, { ...witcher, enabled: true, backend: 'native', variant: 'official-lite' }] }),
    { launch_options: `${TOKEN} ${DLO}` })).args, [3816576258, DLO, 'official-lite', false]);
  await settle();

  assert.ok(find(await open(panel(), state({ games: [witcher] })), 'Field', 'No compatible Steam games found'));
  assert.ok(findText(find(await open(panel(), state({ games: [game] })), 'Field', 'No compatible non-Steam games found'),
    'Add a Windows game'));
});

test('a backend still scanning says so; Rescan gives feedback', async () => {
  const h = panel();
  await open(h, state({ scanned: false, games: [] }));
  assert.match(text(h.render()), /Scanning your library/);
  h.respond('scan_games', ok(state()));
  await settle();
  button(h.render(), 'Rescan library').props.onClick();
  assert.ok(button(h.render(), 'Scanning…').props.disabled);
  h.respond('scan_games', ok(state({ games: [game, { ...game, appid: 2, name: 'E33' }] })));
  await settle();
  assert.equal(button(h.render(), 'Rescan library').props.description, 'Found 2 compatible games.');
});

test('Turn on offline games skips anti-cheat, online and unknown games; Turn off all restores every game', async () => {
  const h = panel();
  const games = [
    { ...game, enabled: true, backend: 'cyboman', variant: '4.1.2l-exact', verdict: 'ok' },
    { ...game, appid: 2, name: 'E33' },
    { ...game, appid: 3, name: 'Anti-cheat', anticheat: true },
    { ...game, appid: 4, name: 'Hitman', cyboman_eligible: false, optiscaler_detected: false },
    { ...game, appid: 5, name: 'Forza' },
    { ...game, appid: 6, name: 'No data' },
  ];
  const tree = await open(h, state({ games }));
  assert.ok(findText(dropdownOf(tree, 'Forza').props.description, 'single-player / offline games only'));
  const ac = dropdownOf(tree, 'Anti-cheat');
  assert.ok(!ac.props.disabled);
  assert.ok(all(ac.props.description).some(n => n.props?.style?.color && findText(n, 'MUST ONLY be used in single-player')));
  button(tree, 'Turn on FSR4 in offline games').props.onClick();
  await settle();
  assert.ok(button(h.render(), 'Turning on… 1/2'));
  assert.equal(h.respond('enable_game', ok(state({ games }), { launch_options: DLO })).args[2], '4.1.2l-exact');
  await settle();
  assert.equal(h.respond('enable_game', { ok: false, error: 'not ours', state: state({ games }) }).args[2], 'official-lite');
  await settle();
  assert.ok(!pending(h, 'enable_game'));

  const on = [
    { ...game, enabled: true, backend: 'cyboman', variant: '4.1.2l-exact' },
    { ...game, appid: 2, name: 'E33', enabled: true, backend: 'native', variant: 'official-lite' },
  ];
  h.remount();
  h.respond('get_state', ok(state({ games: on })));
  await settle();
  button(h.render(), 'Turn off FSR4 and restore all games').props.onClick();
  await settle();
  assert.ok(button(h.render(), 'Restoring… 1/2'));
  for (const id of [1817070, 2]) {
    assert.equal(h.respond('disable_game', ok(state(), { launch_options: '-novid', kept: [] })).args[0], id);
    await settle();
    h.respond('launch_options_saved', { ok: true });
    await settle();
  }
  assert.equal(h.toasts.at(-1).body, '2 games restored to how they were.');
});

test('Repair verifies active backend caches and re-applies enabled games', async () => {
  const h = panel();
  const games = [
    { ...game, enabled: true, backend: 'cyboman', variant: '4.1.2l-exact' },
    { ...game, appid: 2, name: 'E33' },
  ];
  button(await open(h, state({ games })), 'Repair FSR4 in all games').props.onClick();
  await settle();
  h.respond('verify_library', ok(state({ games })));
  await settle();
  assert.ok(button(h.render(), 'Repairing… 1/1'));
  assert.deepEqual(h.respond('enable_game', ok(state({ games }), { launch_options: DLO })).args, [1817070, DLO, '4.1.2l-exact', false]);
  await settle();
  assert.equal(h.toasts.at(-1).body, 'Repaired 1 of 1 game.');

  button(h.render(), 'Repair FSR4 in all games').props.onClick();
  await settle();
  h.respond('verify_library', ok(state({ cyboman: { ready: false }, games })));
  await settle();
  assert.ok(!pending(h, 'enable_game'));
  assert.match(h.toasts.at(-1).body, /Cyboman RDNA 2 cache is missing or damaged/);
});

test('Cyboman download polls progress and supports cancel', async () => {
  const h = panel();
  button(await open(h, state({ cyboman: { ready: false } })), 'Download Cyboman RDNA 2').props.onClick();
  await settle();
  assert.match(text(h.render()), /Checking GitHub release/);
  h.advance(500);
  await settle();
  assert.ok(pending(h, 'cyboman_progress'));
  h.respond('cyboman_progress', {
    ok: true,
    download: { state: 'running', done: 5 * 2 ** 20, total: 9 * 2 ** 20, stage: 'downloading_7z', error: '' },
    cyboman_ready: false,
  });
  await settle();
  assert.equal(find(h.render(), 'Field', 'Downloading Cyboman (.7z)…').props.children, '5 MB / 9 MB');
  h.respond('download_cyboman', ok(state()));
  await settle();
  const readyField = find(h.render(), 'Field', 'Cyboman RDNA 2 (dll-2026-11-22)');
  assert.equal(readyField.props.children, 'Ready');
  assert.match(readyField.props.description, /2 Lite variants cached/);
});

test('Official AMD Lite section expands on demand and polls AMD download', async () => {
  const h = panel();
  let tree = await open(h, state({ dll_ready: false }));
  assert.ok(!button(tree, 'Accept and download'), 'collapsed by default');
  button(tree, 'Show Official AMD Lite setup').props.onClick();
  tree = h.render();
  button(tree, 'Accept and download').props.onClick();
  await settle();
  assert.match(text(h.render()), /Downloading from AMD…/);
  h.advance(500);
  await settle();
  h.respond('download_progress', { ok: true, download: { state: 'running', done: 20 * 2 ** 20, total: 43 * 2 ** 20, error: '' }, dll_ready: false });
  await settle();
  assert.equal(find(h.render(), 'Field', 'Downloading from AMD…').props.children.join(''), '20 MB / 43 MB');
  h.respond('download_dll', { ok: false, error: 'HTTP 503', state: state({ dll_ready: false, download: { state: 'error', done: 0, total: 1, error: 'HTTP 503' } }) });
  await settle();
  assert.match(text(h.render()), /Last attempt failed \| HTTP 503/);
});

test('launch options: automatic option, watermark, copy buttons, Decky Launch Options', async () => {
  const h = panel();
  const games = [{ ...game, enabled: true, managed_options: true }, { ...game, appid: 2, name: 'E33', enabled: true }];
  let tree = await open(h, state({ games }));
  const sw = label => toggleOf(tree, label).props;
  sw('Show FSR4 watermark').onChange(true);
  await settle();
  assert.deepEqual(h.respond('set_setting', ok(state({ settings: { auto_launch_options: true, watermark: true, session_toast: false }, games }))).args,
    ['watermark', true]);
  await settle();
  const marked = `${TOKEN} FSR_WATERMARK=1 ${DLO}`;
  assert.deepEqual(h.respond('apply_watermark', { ok: true, launch_options: marked }).args, [1817070, DLO]);
  await settle();
  assert.deepEqual(h.respond('launch_options_saved', ok(state({ games }))).args, [1817070, marked]);
  await settle();

  button(tree, 'Copy Cyboman watermark').props.onClick();
  await settle();
  button(tree, 'Copy AMD Lite launch options').props.onClick();
  await settle();
  button(tree, 'Copy AMD Lite + watermark').props.onClick();
  await settle();
  assert.deepEqual(h.copied, ['env MLSR-WATERMARK=1 %command%', `${TOKEN} %command%`, `${TOKEN} FSR_WATERMARK=1 %command%`]);

  h.window.hasDeckyLaunchOptions = true;
  button(h.render(), 'Add to Decky Launch Options').props.onClick();
  assert.equal(h.events.at(-1).type, 'dlo-add-launch-options');
  assert.equal(h.events.at(-1).detail.length, 3);
  assert.deepEqual(h.events.at(-1).detail[2], {
    id: 'fsr4-cyboman-watermark', group: 'FSR4 Injector', name: 'Cyboman FSR4 watermark',
    on: 'env MLSR-WATERMARK=1', off: '', enableGlobally: false,
  });
});

test('About page shows dual credits and updates', async () => {
  const h = panel();
  let tree = await open(h);
  const about = all(tree).filter(n => n.type === 'Field').at(-1);
  assert.equal(about.props.label, 'About');
  about.props.onActivate();
  tree = h.render();
  assert.match(text(tree), /Version 2\.0\.0/);
  assert.match(text(tree), /zgauthier2000\/fsr4-shader-performance-overrides/);
  button(tree, 'Check for updates').props.onClick();
  await settle();
  h.respond('check_for_updates', { ok: true, current_version: '2.0.0', latest_version: '2.1.0', update_available: true, download_available: true, size: 2 * 2 ** 20 });
  await settle();
  assert.match(text(h.render()), /Update available \| Version 2\.1\.0/);
});

test('GPU line: verified Steam Deck and unsupported GPU', async () => {
  let h = panel();
  let tree = await open(h);
  assert.match(text(tree), /Steam Deck \(Van Gogh\) GPU/);
  h = panel();
  tree = await open(h, state({ gpu: { ...gpuDeck, model: null, model_name: '' } }));
  assert.match(text(tree), /GPU not supported/);
  assert.ok(dropdownOf(tree, game.name).props.disabled);
  assert.ok(!button(tree, 'Turn on FSR4 in offline games'));
});

test('plugin load: SteamOS update notice, and a warning when a session ended without FSR4', async () => {
  const h = panel();
  const plugin = h.exports.default();
  assert.equal(plugin.name, 'FSR4 Injector');
  h.respond('startup_notice', { ok: true, message: 'SteamOS was updated (a → b).' });
  await settle();
  assert.equal(h.toasts.at(-1).body, 'SteamOS was updated (a → b).');
  h.steam.lifetime({ unAppID: 1817070, bRunning: false });
  await settle();
  h.respond('session_ended', { ok: true, managed: true, verdict: 'failed', reason: 'fell back to FSR3' });
  await settle();
  assert.deepEqual([h.toasts.at(-1).title, h.toasts.at(-1).body], ['FSR4 not active', 'fell back to FSR3']);
});
