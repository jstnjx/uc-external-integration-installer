
const $ = (id) => document.getElementById(id);
let REGISTRY = { integrations: [], categories: [] };
let INSTALLED = [];
let UPDATES = {};   // integration_id -> {update_available, latest_version, installed_version}
let REGS = {};      // integration_id -> [{remote_id, remote_name}]
let STATS = {};     // integration_id -> {cpu_pct, mem_used, mem_limit, pids, started_at, health, status}
const EXPANDED = new Set();  // ids whose details panel is open
let TAB = 'browse';
let cfgMode = 'install';   // install | version | config
let cfgTarget = null;
let cfgIntegration = null;  // parent integration id (for version lookups)
let installedTimer = null;
let logTarget = null;

function token() { return localStorage.getItem('uc_token') || ''; }
function headers(extra) {
  const h = Object.assign({ 'Content-Type': 'application/json' }, extra || {});
  if (token()) h['Authorization'] = 'Bearer ' + token();
  return h;
}
async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: headers() }, opts || {}));
  if (res.status === 401) { $('lockbar').classList.add('show'); throw new Error('Unauthorized'); }
  if (!res.ok) { let d = ''; try { d = (await res.json()).detail; } catch(e){} throw new Error(d || ('HTTP ' + res.status)); }
  return res.status === 204 ? null : res.json();
}

function toast(msg, kind) {
  const t = document.createElement('div');
  t.className = 'toast ' + (kind || '');
  t.textContent = msg;
  $('toasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 300); }, 3200);
}

let dialogResolver = null;
let dialogPreviousFocus = null;

function closeAppDialog(result) {
  const back = $('dialogBack');
  if (!back.classList.contains('show')) return;
  back.classList.remove('show');
  document.removeEventListener('keydown', dialogKeydown);
  const resolve = dialogResolver;
  dialogResolver = null;
  if (dialogPreviousFocus && typeof dialogPreviousFocus.focus === 'function') dialogPreviousFocus.focus();
  dialogPreviousFocus = null;
  if (resolve) resolve(result);
}
function dialogBackdropClick(e) { if (e.target === $('dialogBack')) closeAppDialog({ confirmed: false, value: null, checked: false }); }
function dialogKeydown(e) {
  if (e.key === 'Escape') { e.preventDefault(); closeAppDialog({ confirmed: false, value: null, checked: false }); }
  if (e.key === 'Enter' && e.target !== $('dialogCancel')) { e.preventDefault(); $('dialogConfirm').click(); }
}
function openAppDialog(options) {
  const o = Object.assign({
    type: 'confirm', title: 'Confirm', message: '', detail: '', confirmText: 'Confirm', cancelText: 'Cancel',
    danger: false, icon: '?', promptLabel: 'Value', promptValue: '', checkboxLabel: '', checkboxChecked: false,
  }, options || {});
  if (dialogResolver) closeAppDialog({ confirmed: false, value: null, checked: false });
  dialogPreviousFocus = document.activeElement;
  $('dialogTitle').textContent = o.title;
  $('dialogMessage').textContent = o.message;
  $('dialogDetail').textContent = o.detail || '';
  $('dialogDetail').style.display = o.detail ? '' : 'none';
  $('dialogIcon').textContent = o.icon || (o.danger ? '!' : '?');
  $('appDialog').classList.toggle('danger', !!o.danger);
  $('dialogConfirm').classList.toggle('danger', !!o.danger);
  $('dialogConfirm').textContent = o.confirmText;
  $('dialogCancel').textContent = o.cancelText;
  $('dialogCancel').style.display = o.type === 'alert' ? 'none' : '';
  const prompt = o.type === 'prompt';
  $('dialogPromptField').style.display = prompt ? '' : 'none';
  $('dialogPromptLabel').textContent = o.promptLabel;
  $('dialogPromptInput').value = o.promptValue == null ? '' : String(o.promptValue);
  const hasCheck = !!o.checkboxLabel;
  $('dialogCheckWrap').style.display = hasCheck ? '' : 'none';
  $('dialogCheckLabel').textContent = o.checkboxLabel || '';
  $('dialogCheck').checked = !!o.checkboxChecked;
  $('dialogBack').classList.add('show');
  document.addEventListener('keydown', dialogKeydown);
  return new Promise(resolve => {
    dialogResolver = resolve;
    $('dialogCancel').onclick = () => closeAppDialog({ confirmed: false, value: null, checked: $('dialogCheck').checked });
    $('dialogConfirm').onclick = () => closeAppDialog({
      confirmed: true,
      value: prompt ? $('dialogPromptInput').value : null,
      checked: hasCheck ? $('dialogCheck').checked : false,
    });
    requestAnimationFrame(() => (prompt ? $('dialogPromptInput') : $('dialogConfirm')).focus());
  });
}
async function uiAlert(options) {
  await openAppDialog(Object.assign({ type: 'alert', title: 'Information', confirmText: 'OK', icon: 'i' }, typeof options === 'string' ? { message: options } : options));
}
async function uiConfirm(options) {
  const result = await openAppDialog(Object.assign({ type: 'confirm' }, typeof options === 'string' ? { message: options } : options));
  return result;
}
async function uiPrompt(options) {
  const result = await openAppDialog(Object.assign({ type: 'prompt', confirmText: 'Save' }, typeof options === 'string' ? { message: options } : options));
  return result.confirmed ? result.value : null;
}

function saveToken() {
  const v = $('tokenInput').value.trim();
  if (!v) return;
  localStorage.setItem('uc_token', v);
  $('lockbar').classList.remove('show');
  init();
}

// ---- health / status ----
async function loadHealth() {
  try {
    const h = await (await fetch('/api/health')).json();
    $('ledDocker').className = 'led ' + (h.docker ? 'on' : 'err');
    $('ledReg').className = 'led ' + (h.registry_ok ? 'on' : 'err');
    const rc = h.registry_commit ? (' · ' + h.registry_commit) : '';
    $('regInfo').textContent = h.registry_ok ? ('registry v' + (h.registry_version || '?') + rc) : 'registry down';
    if (h.token_required) { $('lockStat').style.display = 'flex'; if (!token()) $('lockbar').classList.add('show'); }
    $('brandSub').textContent = 'external integrations · port ' + h.port_start + '+';
    if ($('archiveBtn')) $('archiveBtn').style.display = h.archive_supported ? '' : 'none';
    return h;
  } catch (e) { return {}; }
}

// ---- browse ----
async function loadRegistry(refresh) {
  REGISTRY = await api('/api/registry' + (refresh ? '?refresh=true' : ''));
  const sel = $('catFilter');
  sel.innerHTML = '<option value="">All categories</option>';
  (REGISTRY.categories || []).forEach(c => {
    const o = document.createElement('option'); o.value = c.id; o.textContent = c.name; sel.appendChild(o);
  });
  $('browseCount').textContent = REGISTRY.integrations.length;
  renderBrowse();
}
async function refreshRegistry() { toast('Refreshing registry…'); await loadRegistry(true); await loadInstalled(); toast('Registry updated', 'ok'); }

function installedIds() { return new Set(INSTALLED.map(i => i.id)); }

function renderBrowse() {
  const q = $('searchInput').value.trim().toLowerCase();
  const cat = $('catFilter').value;
  const inst = installedIds();
  const items = REGISTRY.integrations.filter(it => {
    if (cat && !(it.categories || []).includes(cat)) return false;
    if (!q) return true;
    return (it.name || '').toLowerCase().includes(q) || (it.author || '').toLowerCase().includes(q)
        || (it.description || '').toLowerCase().includes(q);
  });
  const grid = $('browseGrid');
  grid.innerHTML = '';
  if (!items.length) { grid.innerHTML = '<div class="empty"><h3>Nothing matches</h3><p>Try a different search or category.</p></div>'; return; }
  items.forEach(it => {
    const isInstalled = inst.has(it.id);
    const card = document.createElement('div');
    card.className = 'card' + (it.installable ? '' : ' disabled');
    const badges = [];
    if (isInstalled) badges.push('<span class="badge installed">installed</span>');
    else if (it.official) badges.push('<span class="badge official">official</span>');
    else badges.push('<span class="badge">' + (it.custom ? 'community' : 'custom') + '</span>');
    const chips = (it.features || []).slice(0, 4).map(f => '<span class="chip">' + esc(f) + '</span>').join('');
    let action;
    if (!it.installable) action = '<span class="hint" style="margin:0">' +
      (it.official ? 'Official — runs on the remote, not here' : 'Bundled first-party — not installable here') + '</span>';
    else if (isInstalled) action = '<button class="btn btn-line btn-sm" onclick="addInstance(\'' + it.id + '\')">+ Instance</button>' +
      '<button class="btn btn-line btn-sm" onclick="switchTab(\'installed\')">Manage</button>';
    else action = '<button class="btn btn-primary btn-sm" onclick="openInstall(\'' + it.id + '\')">Install</button>';
    card.innerHTML =
      '<div class="card-head"><div style="flex:1">' +
        '<p class="card-title">' + esc(it.name) + '</p>' +
        '<div class="card-dev">' + esc(it.author || 'unknown') + '</div>' +
      '</div>' + badges.join('') + '</div>' +
      '<p class="card-desc">' + esc(it.description || '') + '</p>' +
      (chips ? '<div class="chips">' + chips + '</div>' : '') +
      '<div class="card-foot"><div class="spacer"></div>' + action + '</div>';
    grid.appendChild(card);
  });
}

// ---- installed ----
async function loadInstalled() {
  try {
    INSTALLED = await api('/api/installed');
    $('installedCount').textContent = INSTALLED.length;
    renderInstalled();
    if (TAB === 'browse') renderBrowse();
  } catch (e) {}
}
async function loadUpdates() {
  try { UPDATES = await api('/api/updates'); updateOverview(); renderInstalled(); } catch (e) {}
}
async function loadRegistrations() {
  try { REGS = await api('/api/registrations'); updateOverview(); renderInstalled(); } catch (e) {}
}
function statusLed(s) {
  if (s === 'running') return '<span class="led on running-pulse"></span>';
  if (s === 'restarting') return '<span class="led warn"></span>';
  if (s === 'missing' || s === 'exited' || s === 'dead') return '<span class="led err"></span>';
  return '<span class="led off"></span>';
}
function fmtVer(v) {
  v = (v || 'latest').toString();
  if (v.toLowerCase() === 'latest') return 'latest';
  return /^v\d/i.test(v) ? v : 'v' + v;
}
function fmtBytes(n) {
  if (n == null) return '—';
  const u = ['B','KB','MB','GB','TB']; let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
}
function fmtUptime(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime(); if (!t || t < 0) return '—';
  let s = Math.max(0, (Date.now() - t) / 1000);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}
function healthColor(h) {
  if (h === 'healthy' || h === 'responding') return 'var(--emerald)';
  if (h === 'unhealthy' || h === 'unreachable') return 'var(--rose)';
  return 'var(--amber)';   // starting / other
}
function detailsHtml(it) {
  const s = STATS[it.id] || {};
  const link = it.repository
    ? '<a href="' + esc(it.repository) + '" target="_blank" rel="noopener">' + esc(it.repository.replace('https://github.com/','')) + '</a>'
    : '—';
  const wsUrl = 'ws://' + location.hostname + ':' + it.port;
  const memStr = s.mem_used != null
    ? fmtBytes(s.mem_used) + (s.mem_limit ? ' / ' + fmtBytes(s.mem_limit) : '') + (s.mem_pct != null ? ' (' + s.mem_pct + '%)' : '')
    : '—';
  const tiles = [
    ['CPU', s.cpu_pct != null ? s.cpu_pct + '<span class="u">%</span>' : '—'],
    ['Memory', s.mem_used != null ? fmtBytes(s.mem_used) : '—'],
    ['Uptime', fmtUptime(s.started_at)],
    ['Health', s.health ? '<span style="color:' + healthColor(s.health) + '">' + esc(s.health) + '</span>' : '—'],
  ].map(([k, v]) => '<div class="tile"><div class="tile-v">' + v + '</div><div class="tile-k">' + k + '</div></div>').join('');
  const meta = [
    ['Version', esc(fmtVer(it.version))],
    ['Build', esc((it.source || '—') + (it.stack && it.stack !== 'dockerfile' ? ' · ' + it.stack : ''))],
    ['Driver id', esc(it.driver_id || '—')],
    ['Port', esc(String(it.port))],
    ['Memory', memStr],
    ['Processes', s.pids != null ? esc(String(s.pids)) : '—'],
    ['Restarts', esc(String(it.restart_count || 0))],
    ['Image', esc(it.image || '—')],
    ['Installed', it.installed_at ? new Date(it.installed_at).toLocaleString() : '—'],
    ['Updated', it.updated_at ? new Date(it.updated_at).toLocaleString() : '—'],
    ['Repository', link],
    ['Add URL', '<span class="mono">' + esc(wsUrl) + '</span> <button class="mini" onclick="copyText(\'' + wsUrl + '\')">copy</button>'],
  ];
  const kv = arr => arr.map(([k, v]) => '<div class="kv"><span class="k2">' + k + '</span><span class="v2">' + v + '</span></div>').join('');
  return '<div class="tiles">' + tiles + '</div>' +
         '<div class="det-grid">' + kv(meta) + '</div>' +
         '<div class="details-management">' +
           '<div class="details-management-head"><h4>Management</h4><span class="hint" style="margin:0">Actions for this integration instance</span></div>' +
           '<div class="details-auto"><div><strong>Automatic updates</strong><span>Install newer released versions automatically.</span></div><label class="auto-toggle"><input type="checkbox" ' + (it.auto_update ? 'checked' : '') + ' onchange="setAutoUpdate(\'' + it.id + '\',this.checked)"/> Auto-update</label></div>' +
           '<div class="details-actions-grid">' +
             '<button class="btn btn-line btn-sm" onclick="openLogs(\'' + it.id + '\')">View logs</button>' +
             '<button class="btn btn-line btn-sm" onclick="openVersion(\'' + it.id + '\')">Change version</button>' +
             '<button class="btn btn-line btn-sm" onclick="openConfig(\'' + it.id + '\')">Configure</button>' +
             '<button class="btn btn-line btn-sm" onclick="rebuild(\'' + it.id + '\')">Rebuild</button>' +
             '<button class="btn btn-line btn-sm" onclick="backupInstance(\'' + it.id + '\')">Backup config</button>' +
             '<button class="btn btn-line btn-sm" onclick="restoreInstance(\'' + it.id + '\')">Restore config…</button>' +
             '<button class="btn btn-danger btn-sm" onclick="removeIntegration(\'' + it.id + '\')">Remove integration</button>' +
           '</div>' +
         '</div>';
}
function copyText(t) { if (navigator.clipboard) navigator.clipboard.writeText(t); toast('Copied', 'ok'); }
function backupInstance(id) {
  window.location.href = '/api/instances/' + id + '/backup' + (token() ? '?token=' + encodeURIComponent(token()) : '');
}
let restoreTarget = null;
function restoreInstance(id) { restoreTarget = id; $('instRestoreFile').click(); }
async function doInstanceRestore(file) {
  if (!file || !restoreTarget) return;
  const fd = new FormData(); fd.append('file', file);
  const h = {}; if (token()) h['Authorization'] = 'Bearer ' + token();
  try {
    const r = await fetch('/api/instances/' + restoreTarget + '/restore', { method: 'POST', headers: h, body: fd });
    const j = await r.json(); if (!r.ok) throw new Error(j.detail || 'failed');
    toast('Restored — restart the instance to apply', 'ok');
  } catch (e) { toast('Restore failed: ' + e.message, 'bad'); }
  $('instRestoreFile').value = '';
}
function closeMenus() { document.querySelectorAll('.menu.show').forEach(m => m.classList.remove('show')); }
function toggleMenu(e, id) {
  e.stopPropagation();
  const m = $('menu-' + id); const open = m.classList.contains('show');
  closeMenus(); if (!open) m.classList.add('show');
}
document.addEventListener('click', closeMenus);
function toggleDetails(id) {
  if (EXPANDED.has(id)) EXPANDED.delete(id); else { EXPANDED.add(id); loadStats(); }
  renderInstalled();
}

function updateOverview() {
  const running = INSTALLED.filter(i => i.status === 'running').length;
  const updates = Object.values(UPDATES || {}).filter(u => u && u.update_available).length;
  const registered = Object.values(REGS || {}).filter(v => Array.isArray(v) && v.length).length;
  if ($('overviewInstalled')) $('overviewInstalled').textContent = INSTALLED.length;
  if ($('overviewRunning')) $('overviewRunning').textContent = running;
  if ($('overviewUpdates')) $('overviewUpdates').textContent = updates;
  if ($('overviewRegistered')) $('overviewRegistered').textContent = registered;
}
function filteredInstalled() {
  const q = $('installedSearch') ? $('installedSearch').value.trim().toLowerCase() : '';
  const f = $('statusFilter') ? $('statusFilter').value : '';
  return INSTALLED.filter(it => {
    if (q && ![it.label,it.name,it.id,it.source,it.driver_id].some(v => String(v || '').toLowerCase().includes(q))) return false;
    if (!f) return true;
    if (f === 'running') return it.status === 'running';
    if (f === 'stopped') return !['running','restarting'].includes(it.status);
    if (f === 'attention') return ['missing','exited','dead','restarting'].includes(it.status) || (it.restart_count || 0) >= 3;
    if (f === 'updates') return !!(UPDATES[it.id] && UPDATES[it.id].update_available);
    return true;
  });
}
function renderInstalled() {
  const box = $('installedRows');
  box.innerHTML = '';
  box.className = 'rows installed-list';
  updateOverview();
  const visible = filteredInstalled();
  if (!visible.length) {
    box.innerHTML = '<div class="empty"><h3>' + (INSTALLED.length ? 'No matching instances' : 'No integrations installed') + '</h3><p>' + (INSTALLED.length ? 'Adjust the search or state filter.' : 'Head to Browse and install one to get started.') + '</p></div>';
    return;
  }
  visible.forEach(it => {
    const running = it.status === 'running';
    const rc = it.restart_count || 0;
    const looping = (it.status === 'restarting' || it.status === 'exited') && rc >= 3;
    const upd = UPDATES[it.id] || {};
    const regs = REGS[it.id] || [];
    const st = STATS[it.id] || {};
    const expanded = EXPANDED.has(it.id);
    const health = st.health || (running ? 'starting' : 'inactive');
    const stateClass = ['missing','exited','dead'].includes(it.status) ? 'error' : (it.status === 'restarting' || looping ? 'warning' : (running ? 'running' : ''));
    const row = document.createElement('article');
    row.className = 'row integration-row';
    const primaryAction = running
      ? '<button class="btn btn-line btn-sm" onclick="lifecycle(\'' + it.id + '\',\'stop\')">Stop</button>'
      : '<button class="btn btn-primary btn-sm" onclick="lifecycle(\'' + it.id + '\',\'start\')">Start</button>';
    const registration = regs.length ? esc(regs.map(r => r.remote_name).join(', ')) : 'Not registered';
    const updateBadge = upd.update_available ? '<span class="upd-badge" onclick="openVersion(\'' + it.id + '\')">update ▸ ' + esc(fmtVer(upd.latest_version || '')) + '</span>' : '';
    row.innerHTML =
      '<div class="integration-row-main">' +
        '<div class="integration-row-state ' + stateClass + '"></div>' +
        '<div class="integration-row-content">' +
          '<div class="integration-row-head"><span class="integration-row-title">' + esc(it.label || it.name) + '</span>' + updateBadge + (it.auto_update ? '<span class="auto-badge">⟳ auto-update</span>' : '') + '</div>' +
          '<div class="integration-row-sub">' + esc(it.id) + (it.driver_id ? ' · ' + esc(it.driver_id) : '') + '</div>' +
          '<div class="integration-row-meta">' +
            '<span>' + statusLed(it.status) + ' <strong>' + esc(it.status) + '</strong>' + (rc ? ' · ' + rc + ' restarts' : '') + '</span>' +
            '<span>health <strong style="color:' + healthColor(health) + '">' + esc(health) + '</strong></span>' +
            '<span>port <strong>' + esc(String(it.port)) + '</strong></span>' +
            '<span>version <strong>' + esc(fmtVer(it.version || 'latest')) + '</strong></span>' +
            '<span>cpu <strong>' + (st.cpu_pct != null ? esc(String(st.cpu_pct)) + '%' : '—') + '</strong></span>' +
            '<span>memory <strong>' + (st.mem_used != null ? fmtBytes(st.mem_used) : '—') + '</strong></span>' +
            '<span>remote <strong>' + registration + '</strong></span>' +
          '</div>' +
          (looping ? '<div class="integration-row-alert">Crash loop detected. Open logs to inspect the failure.</div>' : '') +
        '</div>' +
        '<div class="integration-row-actions">' + primaryAction +
          '<button class="btn btn-line btn-sm" onclick="openLogs(\'' + it.id + '\')">Logs</button>' +
          '<button class="btn btn-line btn-sm" onclick="registerIntegration(\'' + it.id + '\')">Register</button>' +
          '<button class="btn btn-line btn-sm' + (expanded ? ' det-open' : '') + '" onclick="toggleDetails(\'' + it.id + '\')">' + (expanded ? 'Hide details' : 'Details') + '</button>' +
          '<div class="menu-wrap"><button class="btn btn-line btn-sm" onclick="toggleMenu(event,\'' + it.id + '\')">More ▾</button><div class="menu" id="menu-' + it.id + '">' +
            '<button onclick="lifecycle(\'' + it.id + '\',\'restart\')">Restart</button><button onclick="openVersion(\'' + it.id + '\')">Change version</button><button onclick="openConfig(\'' + it.id + '\')">Configure</button><button onclick="rebuild(\'' + it.id + '\')">Rebuild</button><button onclick="toggleAutoUpdate(\'' + it.id + '\')">Auto-update: ' + (it.auto_update ? 'on' : 'off') + '</button><div class="menu-sep"></div><button onclick="backupInstance(\'' + it.id + '\')">Backup config</button><button onclick="restoreInstance(\'' + it.id + '\')">Restore config…</button><div class="menu-sep"></div><button class="danger" onclick="removeIntegration(\'' + it.id + '\')">Remove</button></div></div>' +
        '</div>' +
      '</div>' + (expanded ? '<div class="details">' + detailsHtml(it) + '</div>' : '');
    box.appendChild(row);
  });
}

async function loadStats() {
  try { STATS = await api('/api/stats'); renderInstalled(); } catch (e) {}
}

async function lifecycle(id, action) {
  try { await api('/api/instances/' + id + '/' + action, { method: 'POST' });
    toast(action + ' ' + id, 'ok'); setTimeout(loadInstalled, 400);
  } catch (e) { toast(e.message, 'bad'); }
}
async function rebuild(id) {
  const it = INSTALLED.find(x => x.id === id); if (!it) return;
  const decision = await uiConfirm({ title: 'Rebuild integration', message: 'Rebuild ' + (it.label || id) + '?', detail: 'The source or image will be refreshed and the container recreated.', confirmText: 'Rebuild', icon: '↻' });
  if (!decision.confirmed) return;
  try {
    const { job_id } = await api('/api/instances/' + id + '/rebuild', { method: 'POST', body: '{}' });
    followJob(job_id, 'Rebuilding ' + id);
  } catch (e) { toast(e.message, 'bad'); }
}
async function toggleAutoUpdate(id) {
  const it = INSTALLED.find(x => x.id === id); if (!it) return;
  const next = !it.auto_update;
  try {
    await api('/api/instances/' + id + '/auto-update', { method: 'POST', body: JSON.stringify({ enabled: next }) });
    toast('Auto-update ' + (next ? 'enabled' : 'disabled') + (next ? ' — updates apply automatically when released' : ''), 'ok');
    loadInstalled();
  } catch (e) { toast(e.message, 'bad'); }
}
async function addInstance(integrationId) {
  const it = REGISTRY.integrations.find(x => x.id === integrationId);
  const decision = await uiConfirm({ title: 'Add integration instance', message: 'Start another instance of ' + (it ? it.name : integrationId) + '?', detail: 'The instance receives its own port, configuration, and driver ID.', confirmText: 'Add instance', icon: '+' });
  if (!decision.confirmed) return;
  try {
    const { job_id, instance_id } = await api('/api/integrations/' + integrationId + '/add-instance', { method: 'POST', body: '{}' });
    followJob(job_id, 'Adding instance ' + instance_id);
  } catch (e) { toast(e.message, 'bad'); }
}
async function removeIntegration(id) {
  const it = INSTALLED.find(x => x.id === id);
  const decision = await uiConfirm({
    title: 'Remove integration',
    message: 'Remove ' + (it ? it.label : id) + '?',
    detail: 'The container will be deleted. Saved configuration is kept unless you select the option below.',
    confirmText: 'Remove', danger: true, icon: '!',
    checkboxLabel: 'Also delete saved configuration' + (it && it.instance > 1 ? '' : ' and cloned source'),
  });
  if (!decision.confirmed) return;
  const purge = decision.checked;
  try { await api('/api/instances/' + id + '?purge=' + purge, { method: 'DELETE' });
    toast('Removed ' + id, 'ok'); loadInstalled(); loadRegistrations();
  } catch (e) { toast(e.message, 'bad'); }
}

// ---- config / install modal ----
function envRows() {
  return Array.from(document.querySelectorAll('#envList .env-row')).reduce((acc, r) => {
    const k = r.querySelector('.k').value.trim(); const v = r.querySelector('.v').value;
    if (k) acc[k] = v; return acc;
  }, {});
}
function addEnvRow(k, v) {
  const row = document.createElement('div');
  row.className = 'env-row';
  row.innerHTML = '<input class="k" placeholder="KEY" value="' + esc(k || '') + '">' +
                  '<input class="v" placeholder="value" value="' + esc(v || '') + '">' +
                  '<button class="btn btn-line btn-sm" onclick="this.parentElement.remove()">✕</button>';
  $('envList').appendChild(row);
}
async function loadVersionOptions(integrationId, selected) {
  const sel = $('cfgVersion');
  sel.innerHTML = '<option>loading…</option>';
  try {
    const data = await api('/api/integrations/' + integrationId + '/versions');
    const seen = new Set();
    sel.innerHTML = '';
    (data.versions || []).forEach(v => {
      if (seen.has(v.tag)) return; seen.add(v.tag);
      const label = v.tag + (v.prerelease ? ' (pre-release)' : '');
      const o = document.createElement('option');
      o.value = v.tag; o.textContent = label;
      if (v.tag === (selected || 'latest')) o.selected = true;
      sel.appendChild(o);
    });
    if (!sel.options.length) sel.innerHTML = '<option value="latest">latest</option>';
  } catch (e) { sel.innerHTML = '<option value="latest">latest</option>'; }
}
function _cfgFields(version, port, env) {
  $('cfgVersionField').style.display = version ? 'block' : 'none';
  $('cfgPortField').style.display = port ? 'block' : 'none';
  $('cfgEnvField').style.display = env ? 'block' : 'none';
}
function openInstall(id) {
  const it = REGISTRY.integrations.find(x => x.id === id); if (!it) return;
  cfgMode = 'install'; cfgTarget = id; cfgIntegration = id;
  $('cfgTitle').textContent = 'Install ' + it.name;
  $('cfgSub').textContent = id;
  $('cfgPort').value = ''; $('envList').innerHTML = '';
  _cfgFields(true, true, true);
  $('cfgSubmit').textContent = 'Install';
  showWorkspacePanel('cfgBack');
  loadVersionOptions(id, 'latest');
}
function openVersion(id) {
  const it = INSTALLED.find(x => x.id === id); if (!it) return;
  cfgMode = 'version'; cfgTarget = id; cfgIntegration = it.integration_id || id;
  $('cfgTitle').textContent = 'Change version — ' + (it.label || it.name);
  $('cfgSub').textContent = id;
  _cfgFields(true, false, false);
  $('cfgSubmit').textContent = 'Install version';
  showWorkspacePanel('cfgBack');
  loadVersionOptions(cfgIntegration, it.version || 'latest');
}
function openConfig(id) {
  const it = INSTALLED.find(x => x.id === id); if (!it) return;
  cfgMode = 'config'; cfgTarget = id; cfgIntegration = it.integration_id || id;
  $('cfgTitle').textContent = 'Configure ' + (it.label || it.name);
  $('cfgSub').textContent = id;
  $('cfgPort').value = it.port || ''; $('envList').innerHTML = '';
  Object.entries(it.env || {}).forEach(([k, v]) => addEnvRow(k, v));
  _cfgFields(false, true, true);
  $('cfgSubmit').textContent = 'Apply & restart';
  showWorkspacePanel('cfgBack');
}
async function submitConfig() {
  let path, body = {};
  if (cfgMode === 'install') {
    body = { env: envRows(), version: $('cfgVersion').value || 'latest' };
    const p = $('cfgPort').value.trim(); if (p) body.port = parseInt(p, 10);
    path = '/api/integrations/' + cfgTarget + '/install';
  } else if (cfgMode === 'version') {
    body = { version: $('cfgVersion').value || 'latest' };
    path = '/api/instances/' + cfgTarget + '/rebuild';
  } else {
    body = { env: envRows() };
    const p = $('cfgPort').value.trim(); if (p) body.port = parseInt(p, 10);
    path = '/api/instances/' + cfgTarget + '/config';
  }
  const verb = cfgMode === 'install' ? 'Installing ' : cfgMode === 'version' ? 'Switching ' : 'Reconfiguring ';
  try {
    const { job_id } = await api(path, { method: 'POST', body: JSON.stringify(body) });
    closeModal('cfgBack');
    followJob(job_id, verb + cfgTarget);
  } catch (e) { toast(e.message, 'bad'); }
}

// ---- job follower ----
function followJob(jobId, title) {
  $('jobTitle').textContent = title;
  $('jobSub').innerHTML = '<span class="spin"></span>';
  $('jobClose').style.display = 'none';
  $('jobDone').style.display = 'none';
  $('jobConsole').innerHTML = '';
  showWorkspacePanel('jobBack');
  const poll = async () => {
    let job;
    try { job = await api('/api/jobs/' + jobId); } catch (e) { $('jobConsole').textContent = e.message; return; }
    const c = $('jobConsole');
    c.innerHTML = job.lines.map(l => /^ERROR/.test(l) ? '<span class="err">' + esc(l) + '</span>' : esc(l)).join('\n');
    c.scrollTop = c.scrollHeight;
    if (job.status === 'running') { setTimeout(poll, 900); return; }
    $('jobSub').innerHTML = job.status === 'success' ? '✓ complete' : '✕ failed';
    $('jobClose').style.display = 'block';
    $('jobDone').style.display = 'block';
    if (job.status === 'success') { toast(title.split(' ')[0] + ' complete', 'ok'); loadInstalled(); loadUpdates(); loadRegistrations(); }
    else toast('Job failed — see log', 'bad');
  };
  poll();
}
function closeJob() { closeModal('jobBack'); loadInstalled(); }

// ---- logs ----
let logInstaller = false;
let LOG_LINES = [];
function scrollLogsToLatest(instant = false) {
  const c = $('logConsole');
  if (!c) return;
  const previous = c.style.scrollBehavior;
  if (instant) c.style.scrollBehavior = 'auto';
  requestAnimationFrame(() => {
    c.scrollTop = c.scrollHeight;
    if (instant) requestAnimationFrame(() => { c.style.scrollBehavior = previous; });
  });
}
async function openLogs(id) {
  if (logSource) { logSource.close(); logSource = null; }
  $('logFollowBtn').textContent = 'Follow'; $('logFollowBtn').classList.remove('det-open');
  logInstaller = false;
  logTarget = id;
  document.querySelector('#logBack h2').textContent = 'Logs';
  $('logSub').textContent = id;
  clearLogFilters();
  $('logConsole').classList.remove('log-view'); $('logConsole').textContent = 'Loading…';
  showWorkspacePanel('logBack');
  refreshLogs();
}
async function openInstallerLogs() {
  if (logSource) { logSource.close(); logSource = null; }
  $('logFollowBtn').textContent = 'Follow'; $('logFollowBtn').classList.remove('det-open');
  logInstaller = true; logTarget = null;
  document.querySelector('#logBack h2').textContent = 'Installer logs';
  $('logSub').textContent = 'systemd journal';
  clearLogFilters();
  $('logConsole').classList.remove('log-view'); $('logConsole').textContent = 'Loading…';
  showWorkspacePanel('logBack');
  refreshLogs();
}
function classifyLogLine(line) {
  const value = String(line || '');
  if (/\b(error|fatal|panic|failed|failure|exception|traceback)\b/i.test(value)) return ['error', 'error'];
  if (/\b(warn|warning|deprecated)\b/i.test(value)) return ['warn', 'warn'];
  if (/\b(debug|trace)\b/i.test(value)) return ['debug', 'debug'];
  if (/\b(success|successful|started|ready|healthy|complete[d]?)\b/i.test(value)) return ['success', 'ok'];
  if (/\b(info|notice)\b/i.test(value)) return ['info', 'info'];
  return ['', ''];
}
function splitLogLine(line) {
  const patterns = [
    /^(\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?)\s+(.*)$/,
    /^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(.*)$/,
    /^(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(.*)$/
  ];
  for (const pattern of patterns) {
    const match = String(line || '').match(pattern);
    if (match) return { time: match[1], message: match[2] };
  }
  return { time: '', message: String(line || '') };
}
function parseLogTimestamp(raw) {
  if (!raw) return null;
  let ts = Date.parse(raw);
  if (!Number.isNaN(ts)) return ts;
  const m = raw.match(/^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})$/);
  if (m) { const months={Jan:0,Feb:1,Mar:2,Apr:3,May:4,Jun:5,Jul:6,Aug:7,Sep:8,Oct:9,Nov:10,Dec:11}; const now=new Date(); const d=new Date(now.getFullYear(),months[m[1]],Number(m[2]),Number(m[3]),Number(m[4]),Number(m[5])); if(d.getTime()>now.getTime()+86400000)d.setFullYear(d.getFullYear()-1); return d.getTime(); }
  const t = raw.match(/^(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?$/);
  if (t) { const n=new Date(); n.setHours(Number(t[1]),Number(t[2]),Number(t[3]),0); return n.getTime(); }
  return null;
}
function logLineHtml(line) {
  const parsed = splitLogLine(line);
  const [levelClass, levelLabel] = classifyLogLine(parsed.message);
  const timestamp=parseLogTimestamp(parsed.time);
  return '<div class="log-line' + (levelClass ? ' level-' + levelClass : '') + '" data-level="' + (levelClass || 'other') + '" data-ts="' + (timestamp||'') + '" data-search="' + esc(String(line || '').toLowerCase()) + '">' +
    '<span class="log-time">' + esc(parsed.time) + '</span>' +
    '<span class="log-message">' + (levelLabel ? '<span class="log-level">' + levelLabel + '</span>' : '') + esc(parsed.message || ' ') + '</span></div>';
}
function renderLogs(logs) {
  const c = $('logConsole');
  c.classList.add('log-view');
  LOG_LINES = String(logs || '').replace(/\r/g, '').split('\n').filter((line, i, arr) => line || arr.length === 1);
  if (!logs || (LOG_LINES.length === 1 && !LOG_LINES[0])) {
    c.innerHTML = '<div class="log-empty">No log output yet</div>';
    updateLogSummary(0, 0);
    return;
  }
  c.innerHTML = LOG_LINES.map(logLineHtml).join('');
  applyLogFilters();
}
function appendLogLine(line) {
  const c = $('logConsole');
  c.classList.add('log-view');
  const empty = c.querySelector('.log-empty');
  if (empty) empty.remove();
  LOG_LINES.push(String(line || ''));
  c.insertAdjacentHTML('beforeend', logLineHtml(line));
  applyLogFilters();
}
function applyLogFilters() {
  const c = $('logConsole'); if (!c) return;
  const query = ($('logSearch')?.value || '').trim().toLowerCase();
  const level = $('logLevelFilter')?.value || '';
  let visible = 0, total = 0;
  c.querySelectorAll('.log-line').forEach(row => {
    total++;
    const levelMatch = !level || row.dataset.level === level;
    const queryMatch = !query || (row.dataset.search || '').includes(query);
    const show = levelMatch && queryMatch;
    row.classList.toggle('filtered-out', !show);
    if (show) visible++;
  });
  updateLogSummary(visible, total);
}
function updateLogSummary(visible, total) {
  if ($('logSummary')) $('logSummary').textContent = total ? (visible === total ? total + ' lines' : visible + ' of ' + total + ' lines') : '';
}
function clearLogFilters() {
  if ($('logSearch')) $('logSearch').value = '';
  if ($('logLevelFilter')) $('logLevelFilter').value = '';
  applyLogFilters();
}
async function refreshLogs() {
  const c = $('logConsole');
  try {
    let logs;
    if (logInstaller) {
      logs = (await api('/api/installer/logs?lines=800')).logs;
    } else {
      if (!logTarget) return;
      logs = (await api('/api/instances/' + logTarget + '/logs?tail=400')).logs;
    }
    renderLogs(logs);
    scrollLogsToLatest(true);
  } catch (e) { renderLogs('ERROR ' + e.message); }
}

// ---- misc ----
// ---- software update ----
let UPD = null;

async function loadUpdateStatus() {
  try {
    UPD = await api('/api/update/status');
    renderUpdIndicator();
  } catch (e) { /* token gate or offline */ }
}
function renderUpdIndicator() {
  if (!UPD) return;
  const cur = (UPD.current && UPD.current.short) || '—';
  $('updInfo').textContent = UPD.update_available ? 'update available' : ('build ' + cur);
  $('updBtn').classList.toggle('available', !!UPD.update_available);
}
async function checkUpdate() {
  $('updInfo').textContent = 'checking…';
  await loadUpdateStatus();
  populateUpdate();
  toast(UPD && UPD.update_available ? 'Update available' : 'Up to date', 'ok');
}
function commitLine(c) {
  if (!c || !c.short) return '<span class="mono">unknown</span>';
  const when = c.date ? new Date(c.date).toLocaleString() : '';
  return '<span class="mono">' + esc(c.short) + '</span> — ' + esc(c.subject || '') +
         (when ? '<div class="hint" style="margin-top:2px">' + esc(when) + '</div>' : '');
}
function populateUpdate() {
  $('updRepo').textContent = UPD ? (UPD.repo.replace('https://github.com/', '') + ' · ' + UPD.branch) : '';
  const b = $('updBodyContent');
  if (!UPD) { b.innerHTML = '<p class="hint">Status unavailable.</p>'; return; }
  let html = '';
  if (UPD.error) html += '<p style="color:var(--red)">' + esc(UPD.error) + '</p>';
  html += '<div class="field"><label>Installed</label>' + commitLine(UPD.current) + '</div>';
  html += '<div class="field"><label>Latest on ' + esc(UPD.branch) + '</label>' +
          (UPD.latest ? commitLine(UPD.latest) : '<span class="hint">could not fetch</span>') + '</div>';
  if (UPD.update_available) html += '<p style="color:var(--amber);margin:0">An update is available.</p>';
  else if (!UPD.error) html += '<p class="hint" style="margin:0">You are on the latest version.</p>';
  if (!UPD.service_restartable)
    html += '<p class="hint">The service can\'t restart itself here — after updating, run ' +
            '<span class="mono">systemctl restart ' + esc(UPD.service) + '</span> manually.</p>';
  b.innerHTML = html;
  $('updApplyBtn').textContent = UPD.update_available ? 'Update & restart' : 'Reinstall latest';
}
function renderApplyLabel() {
  const ref = $('updRef') && $('updRef').value;
  const cur = (UPD && UPD.current && UPD.current.short) || '';
  const btn = $('updApplyBtn');
  if (!btn) return;
  btn.textContent = (UPD && UPD.update_available && ref === UPD.branch) ? 'Update & restart'
    : ('Install ' + (ref || 'selected build'));
}
async function loadBuilds() {
  const sel = $('updRef'); if (!sel) return;
  sel.innerHTML = '<option>loading…</option>';
  try {
    const data = await api('/api/update/builds');
    const branches = (data.refs || []).filter(r => r.type === 'branch');
    const tags = (data.refs || []).filter(r => r.type === 'tag');
    let html = '';
    if (branches.length) html += '<optgroup label="Branches">' +
      branches.map(r => '<option value="' + esc(r.name) + '"' + (r.name === data.configured_branch ? ' selected' : '') + '>' + esc(r.name) + '</option>').join('') + '</optgroup>';
    if (tags.length) html += '<optgroup label="Releases / tags">' +
      tags.map(r => '<option value="' + esc(r.name) + '">' + esc(r.name) + '</option>').join('') + '</optgroup>';
    sel.innerHTML = html || ('<option value="' + esc(data.configured_branch || 'main') + '">' + esc(data.configured_branch || 'main') + '</option>');
  } catch (e) {
    sel.innerHTML = '<option value="' + esc((UPD && UPD.branch) || 'main') + '">' + esc((UPD && UPD.branch) || 'main') + '</option>';
  }
  renderApplyLabel();
}
function openUpdate() {
  showWorkspacePanel('updBack');
  populateUpdate();
  loadBuilds();
  if (!UPD) checkUpdate();
}
async function applyUpdate() {
  const ref = ($('updRef') && $('updRef').value) || '';
  const decision = await uiConfirm({ title: 'Update installer', message: 'Install build "' + (ref || 'default') + '"?', detail: 'The installer service will restart. Active integrations will keep running.', confirmText: 'Update & restart', icon: '↑' });
  if (!decision.confirmed) return;
  try {
    const { job_id } = await api('/api/update/apply', { method: 'POST', body: JSON.stringify({ ref: ref || null }) });
    closeModal('updBack');
    followUpdate(job_id);
  } catch (e) { toast(e.message, 'bad'); }
}
function followUpdate(jobId) {
  $('jobTitle').textContent = 'Updating installer';
  $('jobSub').innerHTML = '<span class="spin"></span>';
  $('jobClose').style.display = 'none';
  $('jobDone').style.display = 'none';
  $('jobConsole').innerHTML = '';
  showWorkspacePanel('jobBack');
  const poll = async () => {
    let job;
    try { job = await api('/api/jobs/' + jobId); }
    catch (e) { awaitRestart(); return; }   // service may already be going down
    const c = $('jobConsole');
    c.innerHTML = job.lines.map(l => /^ERROR/.test(l) ? '<span class="err">' + esc(l) + '</span>' : esc(l)).join('\n');
    c.scrollTop = c.scrollHeight;
    if (job.status === 'running') { setTimeout(poll, 900); return; }
    if (job.status === 'success') {
      $('jobSub').innerHTML = '✓ updated';
      if (UPD && UPD.service_restartable) awaitRestart();
      else { $('jobClose').style.display = 'block'; $('jobDone').style.display = 'block'; toast('Update applied', 'ok'); }
    } else {
      $('jobSub').innerHTML = '✕ failed';
      $('jobClose').style.display = 'block';
      $('jobDone').style.display = 'block';
      toast('Update failed — see log', 'bad');
    }
  };
  poll();
}
function awaitRestart() {
  $('jobSub').innerHTML = '<span class="spin"></span> service restarting…';
  const line = document.createElement('div');
  $('jobConsole').appendChild(line);
  let tries = 0;
  const ping = async () => {
    tries++;
    try {
      const r = await fetch('/api/health', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch (e) { /* still down */ }
    if (tries > 60) { $('jobSub').innerHTML = 'restart taking a while — reload manually'; $('jobClose').style.display = 'block'; return; }
    setTimeout(ping, 2000);
  };
  setTimeout(ping, 2500);
}

// ---- remotes ----
let REMOTES = { remotes: [], active: null };

async function loadRemotes() {
  try {
    REMOTES = await api('/api/remotes');
    renderRemoteSelector();
    if ($('remBack').classList.contains('show')) renderRemoteList();
  } catch (e) { /* token gate */ }
}
function activeRemote() { return (REMOTES.remotes || []).find(r => r.id === REMOTES.active) || null; }
function renderRemoteSelector() {
  const sel = $('remoteSelect');
  const list = REMOTES.remotes || [];
  if (!list.length) { sel.innerHTML = '<option value="">No remotes</option>'; return; }
  sel.innerHTML = list.map(r => '<option value="' + r.id + '"' + (r.id === REMOTES.active ? ' selected' : '') +
    '>' + esc(r.name) + '</option>').join('');
}
async function setActiveRemote(id) {
  try { await api('/api/remotes/active', { method: 'POST', body: JSON.stringify({ id: id || null }) });
    REMOTES.active = id || null;
  } catch (e) { toast(e.message, 'bad'); }
}
function openRemotes() { showWorkspacePanel('remBack'); resetRemoteForm(); closeRemoteForm(); loadRemotes(); }
function openRemoteForm() { resetRemoteForm(); $('remoteEditor').classList.add('show'); $('remName').focus(); }
function closeRemoteForm() { $('remoteEditor').classList.remove('show'); resetRemoteForm(); }
function renderRemoteList() {
  const box = $('remList');
  const list = REMOTES.remotes || [];
  if (!list.length) { box.innerHTML = '<div class="empty" style="padding:48px 20px"><h3>No remotes configured</h3><p>Add a remote to register and manage external integrations.</p></div>'; return; }
  box.innerHTML = list.map(r => {
    const active = r.id === REMOTES.active;
    return '<div class="rem-row' + (active ? ' active' : '') + '" id="rem-' + r.id + '">' +
      '<div><div class="rem-name">' + esc(r.name) + (active ? ' <span class="active-tag">active</span>' : '') + '</div>' +
        '<div class="rem-addr">' + esc(r.scheme + '://' + r.host + ':' + r.port) +
          (r.has_api_key ? ' · api key' : (r.has_pin ? ' · pin' : ' · no auth')) + '</div></div>' +
      '<div class="rem-actions">' +
        (active ? '' : '<button class="btn btn-line btn-sm" onclick="setActiveRemote(\'' + r.id + '\').then(loadRemotes)">Set active</button>') +
        '<button class="btn btn-line btn-sm" onclick="testRemote(\'' + r.id + '\')">Test</button>' +
        '<button class="btn btn-line btn-sm" onclick="viewDrivers(\'' + r.id + '\')">Drivers</button>' +
        '<button class="btn btn-line btn-sm" onclick="editRemote(\'' + r.id + '\')">Edit</button>' +
        '<button class="btn btn-danger btn-sm" onclick="deleteRemote(\'' + r.id + '\')">Delete</button>' +
      '</div><div class="drivers-list" id="drv-' + r.id + '" style="display:none"></div></div>';
  }).join('');
}
async function testRemote(rid) {
  toast('Testing connection…');
  try { const r = await api('/api/remotes/' + rid + '/test', { method: 'POST' });
    toast('Connected — ' + (r.driver_count != null ? r.driver_count + ' drivers registered' : 'ok'), 'ok');
  } catch (e) { toast('Test failed: ' + e.message, 'bad'); }
}
async function viewDrivers(rid) {
  const box = $('drv-' + rid);
  if (box.style.display === 'block') { box.style.display = 'none'; return; }
  box.style.display = 'block';
  box.innerHTML = '<span class="hint">Loading…</span>';
  try {
    let drivers = await api('/api/remotes/' + rid + '/drivers');
    if (!Array.isArray(drivers) || !drivers.length) { box.innerHTML = '<span class="hint">No drivers on this remote.</span>'; return; }
    const rank = { EXTERNAL: 0, CUSTOM: 1, LOCAL: 2 };
    drivers = drivers.slice().sort((a, b) =>
      (rank[a.driver_type] ?? 3) - (rank[b.driver_type] ?? 3) ||
      String((a.name && (a.name.en || Object.values(a.name)[0])) || a.driver_id).localeCompare(
        String((b.name && (b.name.en || Object.values(b.name)[0])) || b.driver_id)));
    const ext = drivers.filter(d => d.driver_type === 'EXTERNAL').length;
    const head = '<div class="hint" style="margin:0 0 6px">' + drivers.length + ' drivers · ' +
      ext + ' external' + '</div>';
    box.innerHTML = head + drivers.map(d => {
      const id = d.driver_id || d.id || '?';
      const nm = (d.name && (d.name.en || Object.values(d.name)[0])) || id;
      const t = d.driver_type || '?';
      const cls = t === 'EXTERNAL' ? 'dt-ext' : t === 'CUSTOM' ? 'dt-cust' : 'dt-local';
      const st = d.state ? ' · ' + esc(d.state.toLowerCase()) : '';
      const canRemove = t === 'EXTERNAL' || t === 'CUSTOM';
      const btn = canRemove
        ? '<button class="btn btn-danger btn-sm" style="margin-left:auto" onclick="unregisterDriver(\'' + rid + '\',\'' + esc(id) + '\')">Unregister</button>'
        : '<span class="dt-badge dt-local" style="margin-left:auto">bundled</span>';
      return '<div class="driver-item"><span class="dt-badge ' + cls + '">' + esc(t.toLowerCase()) + '</span>' +
        '<span>' + esc(nm) + ' <span style="color:var(--muted-2)">(' + esc(id) + ')' + st + '</span></span>' + btn + '</div>';
    }).join('');
  } catch (e) { box.innerHTML = '<span class="hint" style="color:var(--red)">' + esc(e.message) + '</span>'; }
}
async function unregisterDriver(rid, driverId) {
  const decision = await uiConfirm({ title: 'Unregister driver', message: 'Unregister ' + driverId + ' from this remote?', confirmText: 'Unregister', danger: true, icon: '!' });
  if (!decision.confirmed) return;
  try { await api('/api/remotes/' + rid + '/drivers/' + encodeURIComponent(driverId), { method: 'DELETE' });
    toast('Unregistered ' + driverId, 'ok'); viewDrivers(rid); viewDrivers(rid);
    setTimeout(loadRegistrations, 500);
  } catch (e) { toast(e.message, 'bad'); }
}
function editRemote(rid) {
  const r = (REMOTES.remotes || []).find(x => x.id === rid); if (!r) return;
  $('remEditId').value = r.id;
  $('remName').value = r.name || '';
  $('remAddr').value = r.scheme + '://' + r.host + ':' + r.port;
  $('remPin').value = '';
  $('remApiKey').value = '';
  $('remAdv').value = r.advertise_ip || '';
  $('remTls').checked = !!r.verify_tls;
  $('remFormTitle').textContent = 'Edit ' + r.name;
  $('remoteEditor').classList.add('show');
  $('remoteEditor').scrollIntoView({ behavior: 'smooth', block: 'start' });
  $('remSaveBtn').textContent = 'Save changes';
  $('remCancelEdit').style.display = 'inline-block';
}
function resetRemoteForm() {
  $('remEditId').value = '';
  ['remName','remAddr','remPin','remApiKey','remAdv'].forEach(id => $(id).value = '');
  $('remTls').checked = false;
  $('remFormTitle').textContent = 'Add a remote';
  $('remSaveBtn').textContent = 'Add remote';
  $('remCancelEdit').style.display = 'none';
}
async function saveRemote() {
  const body = {
    name: $('remName').value.trim(),
    address: $('remAddr').value.trim(),
    pin: $('remPin').value,
    api_key: $('remApiKey').value,
    advertise_ip: $('remAdv').value.trim(),
    verify_tls: $('remTls').checked,
  };
  if (!body.name || !body.address) { toast('Name and address are required', 'bad'); return; }
  const editId = $('remEditId').value;
  try {
    if (editId) await api('/api/remotes/' + editId, { method: 'PUT', body: JSON.stringify(body) });
    else await api('/api/remotes', { method: 'POST', body: JSON.stringify(body) });
    toast('Remote saved', 'ok');
    closeRemoteForm();
    await loadRemotes();
  } catch (e) { toast(e.message, 'bad'); }
}
async function deleteRemote(rid) {
  const decision = await uiConfirm({ title: 'Delete remote', message: 'Delete this remote from the installer?', detail: 'Integrations already registered on the remote will remain there.', confirmText: 'Delete remote', danger: true, icon: '!' });
  if (!decision.confirmed) return;
  try { await api('/api/remotes/' + rid, { method: 'DELETE' }); toast('Remote deleted', 'ok'); await loadRemotes(); }
  catch (e) { toast(e.message, 'bad'); }
}
async function registerIntegration(id, remoteId) {
  const targetRemote = remoteId || REMOTES.active;
  if (!targetRemote) { toast('Add and select a remote first', 'bad'); openRemotes(); return; }
  const r = (REMOTES.remotes || []).find(x => x.id === targetRemote) || activeRemote();
  const it = INSTALLED.find(x => x.id === id);
  if (it && it.status !== 'running') {
    const decision = await uiConfirm({ title: 'Register stopped integration', message: 'This integration is not running.', detail: 'The remote may fail to connect until the integration is started.', confirmText: 'Register anyway', icon: '!' });
    if (!decision.confirmed) return;
  }
  toast('Registering on ' + (r ? r.name : 'remote') + '…');
  try {
    const res = await api('/api/remotes/' + targetRemote + '/register', {
      method: 'POST', body: JSON.stringify({ integration_id: id }),
    });
    if (res.confirmed) {
      toast('Registered ' + res.driver_id + (res.driver_state ? ' — ' + res.driver_state : ' — confirmed'), 'ok');
    } else {
      toast('Sent ' + res.driver_id + ', but the remote hasn\'t confirmed it yet — check the remote', 'bad');
    }
    setTimeout(loadRegistrations, 500);
  } catch (e) { toast('Register failed: ' + e.message, 'bad'); }
}

// ---- activity / maintenance / archive / streaming logs ----
function openActivity() { showWorkspacePanel('actBack'); loadActivity(); }
async function loadActivity() {
  try {
    const { events } = await api('/api/events?limit=150');
    const box = $('actList');
    if (!events.length) { box.innerHTML = '<p class="hint">No activity yet.</p>'; return; }
    const col = { error: 'var(--rose)', alert: 'var(--rose)', register: 'var(--emerald)', install: 'var(--emerald)', remove: 'var(--muted)', state: 'var(--amber)' };
    box.innerHTML = events.map(e => {
      const when = new Date(e.ts).toLocaleString();
      return '<div class="kv"><span class="k2" style="color:' + (col[e.kind] || 'var(--muted)') + ';min-width:70px">' + esc(e.kind) + '</span>' +
        '<span class="v2" style="text-align:left;flex:1">' + esc(e.message) +
        (e.instance_id ? ' <span class="mono" style="color:var(--muted-2)">(' + esc(e.instance_id) + ')</span>' : '') + '</span>' +
        '<span class="k2">' + esc(when) + '</span></div>';
    }).join('');
  } catch (e) { $('actList').innerHTML = '<p class="hint">' + esc(e.message) + '</p>'; }
}
function openMaint() { showWorkspacePanel('maintBack'); loadMainSettings(); loadAlertSettings(); }

async function loadSettingsBranches(selected) {
  const sel = $('settingsBranch');
  if (!sel) return;
  sel.innerHTML = '<option>loading…</option>';
  try {
    const data = await api('/api/update/builds');
    const refs = data.refs || [];
    const names = new Set(refs.map(r => r.name));
    let html = refs.map(r => '<option value="' + esc(r.name) + '"' + (r.name === selected ? ' selected' : '') + '>' + esc(r.name) + (r.type === 'tag' ? ' · tag' : '') + '</option>').join('');
    if (selected && !names.has(selected)) html = '<option value="' + esc(selected) + '" selected>' + esc(selected) + ' (current)</option>' + html;
    sel.innerHTML = html || '<option value="' + esc(selected || 'main') + '">' + esc(selected || 'main') + '</option>';
  } catch (e) { sel.innerHTML = '<option value="' + esc(selected || 'main') + '">' + esc(selected || 'main') + '</option>'; }
}
async function loadMainSettings() {
  try {
    const s = await api('/api/setup');
    $('settingsPort').value = s.port_start || 8000;
    $('settingsRegistry').value = s.registry_url || '';
    $('settingsRepo').value = s.update_repo || '';
    $('settingsService').value = s.update_service || '';
    $('settingsProbe').checked = s.health_probe !== false;
    $('settingsToken').value = s.token || '';
    $('settingsRuntime').innerHTML = [
      ['Bind address', (s.bind_host || '0.0.0.0') + ':' + (s.bind_port || 8900)],
      ['Data directory', s.data_dir || '—']
    ].map(([k,v]) => '<div class="kv"><span class="k2">' + k + '</span><span class="v2 mono">' + esc(v) + '</span></div>').join('');
    loadSettingsBranches(s.update_branch || 'main');
  } catch (e) { toast('Could not load settings: ' + e.message, 'bad'); }
}
async function saveMainSettings(options = {}) {
  const body = { complete: true };
  const port = parseInt($('settingsPort').value, 10); if (port) body.port_start = port;
  body.registry_url = $('settingsRegistry').value.trim();
  body.update_repo = $('settingsRepo').value.trim();
  body.update_branch = ($('settingsBranch').value || '').trim();
  body.update_service = $('settingsService').value.trim();
  body.health_probe = $('settingsProbe').checked;
  body.token = $('settingsToken').value.trim();
  try {
    await api('/api/setup', { method:'POST', body:JSON.stringify(body) });
    if (body.token) localStorage.setItem('uc_token', body.token); else localStorage.removeItem('uc_token');
    if (!options.silent) toast('Settings saved', 'ok');
    await loadHealth();
  } catch (e) { toast('Could not save settings: ' + e.message, 'bad'); }
}
const ALERT_LABELS = { install: 'Installs', update: 'Updates available', register: 'Registrations', health: 'Health alerts (unreachable/exited)', remove: 'Removals', maintenance: 'Backup & maintenance', error: 'Errors' };
async function loadAlertSettings() {
  try {
    const s = await api('/api/settings/alerts');
    $('alertWebhook').value = s.webhook || '';
    $('alertEvents').innerHTML = (s.categories || []).map(cat =>
      '<label class="checkline"><input type="checkbox" data-cat="' + cat + '"' + (s.events[cat] ? ' checked' : '') + '> ' +
      esc(ALERT_LABELS[cat] || cat) + '</label>').join('');
  } catch (e) {}
}
async function saveAlertSettings(options = {}) {
  const events = {};
  $('alertEvents').querySelectorAll('input[type=checkbox]').forEach(cb => { events[cb.dataset.cat] = cb.checked; });
  try {
    await api('/api/settings/alerts', { method: 'PUT', body: JSON.stringify({ webhook: $('alertWebhook').value.trim(), events }) });
    if (!options.silent) toast('Notifications saved', 'ok');
  } catch (e) { toast(e.message, 'bad'); throw e; }
}
async function testAlert(saveFirst = true) {
  try {
    if (saveFirst) await saveAlertSettings();
    await api('/api/settings/alerts/test', { method: 'POST' });
    toast('Test notification sent', 'ok');
  } catch (e) { toast('Test failed: ' + e.message, 'bad'); }
}
function downloadBackup() {
  window.location.href = '/api/backup' + (token() ? '?token=' + encodeURIComponent(token()) : '');
}
async function restoreBackup(file) {
  if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  const h = {}; if (token()) h['Authorization'] = 'Bearer ' + token();
  try {
    const r = await fetch('/api/restore', { method: 'POST', headers: h, body: fd });
    const j = await r.json(); if (!r.ok) throw new Error(j.detail || 'failed');
    toast('Restored — rebuild instances to recreate their containers', 'ok'); loadInstalled();
  } catch (e) { toast('Restore failed: ' + e.message, 'bad'); }
  $('restoreFile').value = '';
}
async function doReconcile() {
  try { await api('/api/maintenance/reconcile', { method: 'POST' }); toast('Reconciled', 'ok'); loadInstalled(); }
  catch (e) { toast(e.message, 'bad'); }
}
async function doPrune() {
  try { const r = await api('/api/maintenance/prune', { method: 'POST' }); toast('Pruned ' + ((r.removed || []).length) + ' image(s)', 'ok'); }
  catch (e) { toast(e.message, 'bad'); }
}
async function installArchive(file) {
  if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  const h = {}; if (token()) h['Authorization'] = 'Bearer ' + token();
  try {
    const r = await fetch('/api/install-archive', { method: 'POST', headers: h, body: fd });
    const j = await r.json(); if (!r.ok) throw new Error(j.detail || 'failed');
    switchTab('installed'); followJob(j.job_id, 'Installing ' + file.name);
  } catch (e) { toast('Archive install failed: ' + e.message, 'bad'); }
  $('archiveFile').value = '';
}
let logSource = null;
function toggleFollow() {
  if (logSource) { logSource.close(); logSource = null; $('logFollowBtn').textContent = 'Follow'; $('logFollowBtn').classList.remove('det-open'); return; }
  const t = token() ? ('?token=' + encodeURIComponent(token())) : '';
  const url = logInstaller ? ('/api/installer/logs/stream' + t) : ('/api/instances/' + logTarget + '/logs/stream' + t);
  logSource = new EventSource(url);
  const c = $('logConsole');
  logSource.onmessage = (e) => { appendLogLine(e.data); scrollLogsToLatest(); };
  logSource.onerror = () => {};
  $('logFollowBtn').textContent = 'Following…'; $('logFollowBtn').classList.add('det-open');
}
async function downloadLogs() {
  try {
    let logs, name;
    if (logInstaller) { logs = (await api('/api/installer/logs?lines=5000')).logs; name = 'uc-installer'; }
    else { logs = (await api('/api/instances/' + logTarget + '/logs?tail=5000')).logs; name = logTarget; }
    const blob = new Blob([logs || ''], { type: 'text/plain' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = name + '.log'; a.click(); URL.revokeObjectURL(a.href);
  } catch (e) { toast(e.message, 'bad'); }
}
function closeLogs() {
  if (logSource) { logSource.close(); logSource = null; }
  logInstaller = false;
  $('logFollowBtn').textContent = 'Follow'; $('logFollowBtn').classList.remove('det-open');
  closeModal('logBack');
}

function switchTab(t) {
  hideAllWorkspacePanels();
  $('dashboardShell').style.display = 'block';
  TAB = t;
  $('browse').style.display = t === 'browse' ? 'block' : 'none';
  $('installed').style.display = t === 'installed' ? 'block' : 'none';
  $('tabBrowseBtn').classList.toggle('active', t === 'browse');
  $('tabInstalledBtn').classList.toggle('active', t === 'installed');
  if ($('workspaceHint')) $('workspaceHint').textContent = t === 'installed' ? 'Operate containers, versions, registration, and runtime health.' : 'Find integrations from the configured registry.';
  if (t === 'installed') { loadInstalled(); loadStats(); }
  if (t === 'browse') renderBrowse();
}
function hideAllWorkspacePanels() {
  document.querySelectorAll('.workspace-panel.show').forEach(p => p.classList.remove('show'));
}
function showWorkspacePanel(id) {
  closeMenus();
  hideAllWorkspacePanels();
  $('dashboardShell').style.display = 'none';
  const panel = $(id);
  if (panel) panel.classList.add('show');
  window.scrollTo({ top: 0, behavior: 'instant' });
}
function hideWorkspacePanel(id) {
  const panel = $(id);
  if (panel) panel.classList.remove('show');
  $('dashboardShell').style.display = 'block';
  window.scrollTo({ top: 0, behavior: 'instant' });
}
function closeModal(id) { hideWorkspacePanel(id); }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

document.addEventListener('keydown', e => { if (e.key === 'Escape') { const p = document.querySelector('.workspace-panel.show'); if (p && !(p.id === 'setupBack' && $('setupCancel').style.display === 'none')) closeModal(p.id); } });

async function loadSetupBranches(selected) {
  const sel = $('setupBranch');
  sel.innerHTML = '<option>loading…</option>';
  const fallback = () => {
    const opts = new Set([selected || 'main', 'main']);
    sel.innerHTML = Array.from(opts).map(n => '<option value="' + esc(n) + '"' + (n === (selected || 'main') ? ' selected' : '') + '>' + esc(n) + '</option>').join('');
  };
  try {
    const data = await api('/api/update/builds');
    const branches = (data.refs || []).filter(r => r.type === 'branch');
    const tags = (data.refs || []).filter(r => r.type === 'tag');
    if (!branches.length && !tags.length) return fallback();
    let html = '';
    const names = new Set();
    if (branches.length) html += '<optgroup label="Branches">' + branches.map(r => { names.add(r.name); return '<option value="' + esc(r.name) + '"' + (r.name === selected ? ' selected' : '') + '>' + esc(r.name) + '</option>'; }).join('') + '</optgroup>';
    if (tags.length) html += '<optgroup label="Releases / tags">' + tags.map(r => { names.add(r.name); return '<option value="' + esc(r.name) + '"' + (r.name === selected ? ' selected' : '') + '>' + esc(r.name) + '</option>'; }).join('') + '</optgroup>';
    if (selected && !names.has(selected)) html = '<option value="' + esc(selected) + '" selected>' + esc(selected) + ' (current)</option>' + html;
    sel.innerHTML = html;
  } catch (e) { fallback(); }
}
async function openSetup() {
  try {
    const s = await api('/api/setup');
    $('setupPort').value = s.port_start || 8000;
    $('setupRegistry').value = s.registry_url || '';
    $('setupRepo').value = s.update_repo || '';
    $('setupService').value = s.update_service || '';
    $('setupProbe').checked = s.health_probe !== false;
    $('setupToken').value = s.token || '';
    $('setupWebhook').value = s.webhook || '';
    $('setupEvents').innerHTML = (s.categories || []).map(cat =>
      '<label class="checkline"><input type="checkbox" data-cat="' + cat + '"' + (s.events && s.events[cat] ? ' checked' : '') + '> ' +
      esc(ALERT_LABELS[cat] || cat) + '</label>').join('');
    $('setupRuntime').innerHTML = [
      ['Bind address', (s.bind_host || '0.0.0.0') + ':' + (s.bind_port || 8900)],
      ['Data directory', s.data_dir || '—'],
    ].map(([k, v]) => '<div class="kv"><span class="k2">' + k + '</span><span class="v2 mono">' + esc(v) + '</span></div>').join('');
    loadSetupBranches(s.update_branch || 'main');
    const done = s.setup_complete === true;
    $('setupTitle').textContent = done ? 'Settings' : 'Welcome — first-time setup';
    $('setupFinish').textContent = done ? 'Save settings' : 'Finish setup';
    $('setupCancel').style.display = done ? '' : 'none';
  } catch (e) {}
  showWorkspacePanel('setupBack');
}
async function finishSetup() {
  const body = { complete: true };
  const p = parseInt($('setupPort').value, 10); if (p) body.port_start = p;
  body.registry_url = $('setupRegistry').value.trim();
  body.update_repo = $('setupRepo').value.trim();
  body.update_branch = ($('setupBranch').value || '').trim();
  body.update_service = $('setupService').value.trim();
  body.health_probe = $('setupProbe').checked;
  const tok = $('setupToken').value.trim();
  body.token = tok;
  body.webhook = $('setupWebhook').value.trim();
  const events = {};
  $('setupEvents').querySelectorAll('input[type=checkbox]').forEach(cb => { events[cb.dataset.cat] = cb.checked; });
  body.events = events;
  try {
    await api('/api/setup', { method: 'POST', body: JSON.stringify(body) });
    // remember the token we just set so the reload isn't locked out
    if (tok) localStorage.setItem('uc_token', tok); else localStorage.removeItem('uc_token');
    hideWorkspacePanel('setupBack');
    location.reload();
  } catch (e) { toast('Could not save setup: ' + e.message, 'bad'); }
}
async function init() {
  const h = await loadHealth();
  if (h.token_required && !token()) return;   // wait for unlock
  if (h.setup_complete === false) { openSetup(); return; }  // first-run wizard
  try { await loadRegistry(false); } catch (e) {}
  await loadInstalled();
  loadUpdateStatus();
  loadRemotes();
  loadUpdates();
  loadRegistrations();
  loadStats();
  if (installedTimer) clearInterval(installedTimer);
  installedTimer = setInterval(() => {
    if (!document.querySelector('.workspace-panel.show') && !document.querySelector('.menu.show')) { loadInstalled(); loadRegistrations(); loadStats(); }
  }, 6000);
}

// ---- management enhancement layer ----
const SELECTED_INSTALLED = new Set();
const OPERATIONS = new Map();
const REMOTE_TELEMETRY = {};
let LOG_PAUSED = false;
let LOG_AT_LATEST = true;
let LOG_PENDING = [];
let FORM_BASELINES = new Map();

function saveInstalledView() {
  const ids=['installedSearch','statusFilter','healthFilter','registrationFilter','updateFilter','installedSort'];
  const data={}; ids.forEach(id=>{if($(id)) data[id]=$(id).value;});
  localStorage.setItem('uc_installed_view',JSON.stringify(data));
}
function restoreInstalledView() {
  try { const d=JSON.parse(localStorage.getItem('uc_installed_view')||'{}'); Object.entries(d).forEach(([k,v])=>{if($(k)) $(k).value=v;}); } catch(e){}
}
function selectedInstalledIds(){ return Array.from(SELECTED_INSTALLED).filter(id=>INSTALLED.some(x=>x.id===id)); }
function updateBulkBar(){ const n=selectedInstalledIds().length, visible=filteredInstalled(), all=visible.length&&visible.every(it=>SELECTED_INSTALLED.has(it.id)); $('bulkBar')?.classList.toggle('show',n>0); if($('bulkCount')) $('bulkCount').textContent=n+' selected'; if($('selectVisibleBtn')) $('selectVisibleBtn').textContent=all?'Clear visible':'Select visible'; }
function toggleInstalledSelection(id, checked){ checked?SELECTED_INSTALLED.add(id):SELECTED_INSTALLED.delete(id); updateBulkBar(); }
function toggleSelectAllInstalled(checked){ filteredInstalled().forEach(it=>checked?SELECTED_INSTALLED.add(it.id):SELECTED_INSTALLED.delete(it.id)); renderInstalled(); }
function toggleVisibleInstalledSelection(){ const visible=filteredInstalled(); const all=visible.length&&visible.every(it=>SELECTED_INSTALLED.has(it.id)); visible.forEach(it=>all?SELECTED_INSTALLED.delete(it.id):SELECTED_INSTALLED.add(it.id)); renderInstalled(); }
async function bulkLifecycle(action){ const ids=selectedInstalledIds(); if(!ids.length)return; const d=await uiConfirm({title:action[0].toUpperCase()+action.slice(1)+' integrations',message:action+' '+ids.length+' selected integrations?',confirmText:action[0].toUpperCase()+action.slice(1)}); if(!d.confirmed)return; await Promise.allSettled(ids.map(id=>api('/api/instances/'+id+'/'+action,{method:'POST'}))); toast(action+' requested for '+ids.length+' integrations','ok'); setTimeout(loadInstalled,500); }
async function bulkAutoUpdate(enabled){ const ids=selectedInstalledIds(); if(!ids.length)return; await Promise.allSettled(ids.map(id=>api('/api/instances/'+id+'/auto-update',{method:'POST',body:JSON.stringify({enabled})}))); toast('Auto-update '+(enabled?'enabled':'disabled')+' for '+ids.length+' integrations','ok'); loadInstalled(); }
async function bulkRegister(){ const ids=selectedInstalledIds(); for(const id of ids){ try{await registerIntegration(id);}catch(e){} } }
async function bulkRemove(){ const ids=selectedInstalledIds(); if(!ids.length)return; const d=await uiConfirm({title:'Remove integrations',message:'Remove '+ids.length+' selected integrations?',detail:ids.join('\n'),confirmText:'Remove all',danger:true,checkboxLabel:'Also delete saved configuration and cloned source'}); if(!d.confirmed)return; await Promise.allSettled(ids.map(id=>api('/api/instances/'+id+'?purge='+d.checked,{method:'DELETE'}))); SELECTED_INSTALLED.clear(); toast('Removed selected integrations','ok'); loadInstalled(); loadRegistrations(); }

function filteredInstalled(){
  const q=($('installedSearch')?.value||'').trim().toLowerCase(), sf=$('statusFilter')?.value||'', hf=$('healthFilter')?.value||'', rf=$('registrationFilter')?.value||'', uf=$('updateFilter')?.value||'', sort=$('installedSort')?.value||'name';
  let out=INSTALLED.filter(it=>{
    const st=STATS[it.id]||{}, regs=REGS[it.id]||[], upd=UPDATES[it.id]||{};
    const hay=[it.label,it.name,it.id,it.source,it.driver_id,it.repository,it.port].join(' ').toLowerCase();
    if(q&&!hay.includes(q))return false;
    if(sf==='running'&&it.status!=='running')return false;
    if(sf==='stopped'&&['running','restarting'].includes(it.status))return false;
    if(sf==='attention'&&!(['missing','exited','dead','restarting'].includes(it.status)||(it.restart_count||0)>=3||['unhealthy','unreachable'].includes(st.health||it.health)))return false;
    const health=(st.health||it.health||'unknown').toLowerCase();
    if(hf==='healthy'&&!['healthy','responding'].includes(health))return false;
    if(hf==='unhealthy'&&!['unhealthy','unreachable'].includes(health))return false;
    if(hf==='unknown'&&health!=='unknown'&&health!=='starting'&&health!=='inactive')return false;
    if(rf==='registered'&&!regs.length)return false; if(rf==='unregistered'&&regs.length)return false;
    if(uf==='available'&&!upd.update_available)return false; if(uf==='current'&&upd.update_available)return false; if(uf==='auto'&&!it.auto_update)return false;
    return true;
  });
  const val=it=>{const st=STATS[it.id]||{}; if(sort==='status')return String(it.status); if(sort==='cpu')return -(st.cpu_pct||0); if(sort==='memory')return -(st.mem_used||0); if(sort==='uptime')return new Date(st.started_at||0).getTime(); if(sort==='port')return Number(it.port||0); if(sort==='updated')return -new Date(it.updated_at||it.installed_at||0).getTime(); return String(it.label||it.name||'').toLowerCase();};
  return out.sort((a,b)=>{const av=val(a),bv=val(b); return typeof av==='number'?av-bv:String(av).localeCompare(String(bv));});
}
function statusTone(v){ return ['running','healthy','responding','registered','current'].includes(v)?'good':['missing','exited','dead','unhealthy','unreachable'].includes(v)?'bad':'warn'; }
function handleInstalledRowClick(event,id){
  if(event.target.closest('button,a,input,select,label,.menu,.menu-wrap')) return;
  toggleInstalledSelection(id,!SELECTED_INSTALLED.has(id));
}
function handleInstalledRowKey(event,id){
  if(event.key!=='Enter'&&event.key!==' ') return;
  if(event.target!==event.currentTarget) return;
  event.preventDefault();
  toggleInstalledSelection(id,!SELECTED_INSTALLED.has(id));
}
function stateRailClass(status){
  if(status==='running') return 'running';
  if(['missing','exited','dead'].includes(status)) return 'error';
  return 'warning';
}
function remoteRegisterMenuHtml(it){
  const remotes=(REMOTES.remotes||[]);
  if(!remotes.length) return '<div class="menu-label">No remotes configured</div><button onclick="openRemotes()">Add a remote</button>';
  return '<div class="menu-label">Register on remote</div>' + remotes.map(r =>
    '<button onclick="registerIntegration(\''+it.id+'\',\''+r.id+'\')"><span>'+esc(r.name||r.host||r.id)+'</span><small>'+esc((r.host||'')+(r.port?':'+r.port:''))+'</small></button>'
  ).join('');
}
function toggleRegisterMenu(event,id){
  event.stopPropagation();
  const menu=$('reg-menu-'+id), open=menu?.classList.contains('show');
  closeMenus();
  if(menu&&!open) menu.classList.add('show');
}
function renderInstalled(){
  const box=$('installedRows'); box.innerHTML=''; box.className='rows installed-list'; updateOverview(); const visible=filteredInstalled();
  if(!visible.length){ box.innerHTML='<div class="empty"><h3>'+(INSTALLED.length?'No matching instances':'No integrations installed')+'</h3><p>'+(INSTALLED.length?'Adjust the active filters.':'Browse the registry to install your first integration.')+'</p>'+(INSTALLED.length?'':'<button class="btn btn-primary" onclick="switchTab(\'browse\')">Browse integrations</button>')+'</div>'; updateBulkBar(); return; }
  visible.forEach(it=>{ const running=it.status==='running', rc=it.restart_count||0, upd=UPDATES[it.id]||{}, regs=REGS[it.id]||[], st=STATS[it.id]||{}, health=st.health||it.health||(running?'starting':'unknown'), expanded=EXPANDED.has(it.id), selected=SELECTED_INSTALLED.has(it.id), looping=(it.status==='restarting'||it.status==='exited')&&rc>=3;
    const row=document.createElement('article'); row.className='row integration-row'+(selected?' selected':''); row.tabIndex=0; row.setAttribute('aria-selected',selected?'true':'false'); row.setAttribute('role','option'); row.onclick=e=>handleInstalledRowClick(e,it.id); row.onkeydown=e=>handleInstalledRowKey(e,it.id);
    row.innerHTML='<div class="integration-row-main"><div class="integration-row-state '+stateRailClass(it.status)+'" title="Container: '+esc(it.status)+'"></div><div class="integration-row-content"><div class="integration-row-head"><span class="integration-row-title">'+esc(it.label||it.name)+'</span><span class="integration-title-actions">'+
      '<button class="row-icon-btn" title="Stop integration" aria-label="Stop '+esc(it.label||it.name)+'" '+(!running?'disabled':'')+' onclick="lifecycle(\''+it.id+'\',\'stop\')">■</button>'+
      '<button class="row-icon-btn" title="Restart integration" aria-label="Restart '+esc(it.label||it.name)+'" onclick="lifecycle(\''+it.id+'\',\'restart\')">↻</button>'+
      '<span class="menu-wrap"><button class="row-icon-btn register" title="Register on a remote" aria-label="Register '+esc(it.label||it.name)+' on a remote" onclick="toggleRegisterMenu(event,\''+it.id+'\')">+</button><div class="menu register-menu" id="reg-menu-'+it.id+'">'+remoteRegisterMenuHtml(it)+'</div></span></span>'+
      (upd.update_available?'<span class="upd-badge" onclick="openVersion(\''+it.id+'\')">update ▸ '+esc(fmtVer(upd.latest_version||''))+'</span>':'')+'</div><div class="integration-row-sub">'+esc(it.id)+(it.driver_id?' · '+esc(it.driver_id):'')+'</div>'+
      '<div class="state-group"><span class="state-pill '+statusTone(it.status)+'">container · '+esc(it.status)+'</span><span class="state-pill '+statusTone(health)+'">health · '+esc(health)+'</span><span class="state-pill '+(regs.length?'good':'warn')+'">remote · '+(regs.length?esc(regs.map(r=>r.remote_name).join(', ')):'unregistered')+'</span><span class="state-pill '+(upd.update_available?'warn':'good')+'">version · '+(upd.update_available?'update available':'current')+'</span></div>'+
      '<div class="integration-row-meta"><span>port <strong>'+esc(String(it.port))+'</strong></span><span>version <strong>'+esc(fmtVer(it.version||'latest'))+'</strong></span><span>cpu <strong>'+(st.cpu_pct!=null?esc(String(st.cpu_pct))+'%':'—')+'</strong></span><span>memory <strong>'+(st.mem_used!=null?fmtBytes(st.mem_used):'—')+'</strong></span><span>uptime <strong>'+fmtUptime(st.started_at)+'</strong></span></div>'+(looping?'<div class="integration-row-alert">Crash loop detected. Open details to inspect logs and management options.</div>':'')+'</div>'+
      '<button class="integration-row-chevron'+(expanded?' open':'')+'" title="'+(expanded?'Hide':'Show')+' details" aria-label="'+(expanded?'Hide':'Show')+' details for '+esc(it.label||it.name)+'" onclick="toggleDetails(\''+it.id+'\')"><span>›</span></button></div>'+(expanded?'<div class="details">'+detailsHtml(it)+'</div>':''); box.appendChild(row);
  }); updateBulkBar();
}
async function setAutoUpdate(id,enabled){ try{await api('/api/instances/'+id+'/auto-update',{method:'POST',body:JSON.stringify({enabled})}); const it=INSTALLED.find(x=>x.id===id); if(it)it.auto_update=enabled; toast('Auto-update '+(enabled?'enabled':'disabled'),'ok');}catch(e){toast(e.message,'bad');loadInstalled();} }

function routeForPanel(id){ return {remBack:'remotes',logBack:logInstaller?'installer-logs':'logs/'+(logTarget||''),actBack:'activity',maintBack:'settings',updBack:'update',cfgBack:'configure/'+(cfgTarget||''),jobBack:'operations',setupBack:'setup'}[id]||'installed'; }
function setHash(route,replace=false){ const h='#/'+route; if(location.hash!==h) history[replace?'replaceState':'pushState']({},'',h); }
const _switchTab=switchTab; switchTab=function(t){ _switchTab(t); setHash(t); };
const _showWorkspacePanel=showWorkspacePanel; showWorkspacePanel=function(id){ _showWorkspacePanel(id); setHash(routeForPanel(id)); };
const _hideWorkspacePanel=hideWorkspacePanel; hideWorkspacePanel=function(id){ _hideWorkspacePanel(id); setHash(TAB||'browse'); };
function applyRoute(){ const r=(location.hash||'#/browse').replace(/^#\/?/,''); if(r==='browse'||r==='installed'){_switchTab(r);return;} if(r==='remotes'){openRemotes();return;} if(r==='activity'){openActivity();return;} if(r==='settings'){openMaint();return;} if(r==='installer-logs'){openInstallerLogs();return;} if(r.startsWith('logs/')){openLogs(decodeURIComponent(r.slice(5)));return;} }
window.addEventListener('popstate',applyRoute);

const OPERATION_TTL = { success: 12000, failed: 30000 };
const operationCleanupTimers = new Map();
function scheduleOperationCleanup(id,status){
  if(operationCleanupTimers.has(id)) clearTimeout(operationCleanupTimers.get(id));
  const ttl=OPERATION_TTL[status]; if(!ttl) return;
  operationCleanupTimers.set(id,setTimeout(()=>{
    OPERATIONS.delete(id); operationCleanupTimers.delete(id); renderOperations();
  },ttl));
}
function addOperation(id,title){
  if(operationCleanupTimers.has(id)){clearTimeout(operationCleanupTimers.get(id));operationCleanupTimers.delete(id);}
  OPERATIONS.set(id,{id,title,status:'running',lines:[],progress:5}); renderOperations();
}
function updateOperation(id,patch){
  const op=OPERATIONS.get(id)||{id,title:id,status:'running',lines:[],progress:5};
  Object.assign(op,patch); OPERATIONS.set(id,op);
  if(op.status==='success'||op.status==='failed') scheduleOperationCleanup(id,op.status);
  renderOperations();
}
function updateFloatingOffsets(){
  const drawer=$('operationDrawer'), toasts=$('toasts'); if(!toasts)return;
  const visible=drawer&&!drawer.classList.contains('hidden');
  const height=visible?drawer.getBoundingClientRect().height:0;
  document.documentElement.style.setProperty('--toast-bottom',(visible?height+36:24)+'px');
}
function renderOperations(){
  const ops=Array.from(OPERATIONS.values()).slice(-8).reverse(), active=ops.filter(o=>o.status==='running').length;
  const drawer=$('operationDrawer');
  if(drawer) drawer.classList.toggle('hidden',ops.length===0);
  if($('operationSummary')) $('operationSummary').textContent=active?active+' active':(ops.length?ops.length+' recent':'No active operations');
  if($('operationList')) $('operationList').innerHTML=ops.length?ops.map(o=>'<div class="operation-item"><div class="operation-row"><span>'+esc(o.title)+'</span><span>'+esc(o.status)+'</span></div><div class="operation-meta">'+esc((o.lines||[]).slice(-1)[0]||'Queued')+'</div><div class="operation-progress"><span style="width:'+(o.status==='success'||o.status==='failed'?100:Math.min(95,o.progress||10))+'%"></span></div></div>').join(''):'';
  requestAnimationFrame(updateFloatingOffsets);
}
function toggleOperationDrawer(){
  const drawer=$('operationDrawer'); if(!drawer)return;
  const open=drawer.classList.toggle('open');
  const button=drawer.querySelector('.operation-head'); if(button)button.setAttribute('aria-expanded',String(open));
  if($('operationChevron')) $('operationChevron').textContent='⌄';
  requestAnimationFrame(updateFloatingOffsets);
}
function followJob(jobId,title){
  addOperation(jobId,title);
  const poll=async()=>{let job;try{job=await api('/api/jobs/'+jobId);}catch(e){updateOperation(jobId,{status:'failed',lines:[e.message]});return;}
    updateOperation(jobId,{status:job.status,lines:job.lines||[],progress:Math.min(90,10+(job.lines||[]).length*3)});
    if(job.status==='running'){setTimeout(poll,900);return;}
    if(job.status==='success'){toast(title.split(' ')[0]+' complete','ok');loadInstalled();loadUpdates();loadRegistrations();}
    else toast('Job failed — open Operations for details','bad');
  };
  poll(); const drawer=$('operationDrawer'); if(drawer){drawer.classList.remove('hidden');drawer.classList.add('open');drawer.querySelector('.operation-head')?.setAttribute('aria-expanded','true');requestAnimationFrame(updateFloatingOffsets);}
}
function scrollToTop(){ window.scrollTo({top:0,behavior:'smooth'}); }
function updateBackToTop(){ $('backToTop')?.classList.toggle('show',window.scrollY>520); }
window.addEventListener('scroll',updateBackToTop,{passive:true});
window.addEventListener('resize',updateFloatingOffsets);
window.addEventListener('load',()=>{updateBackToTop();updateFloatingOffsets();});

function toggleLogPause(){ LOG_PAUSED=!LOG_PAUSED; if($('logPauseBtn'))$('logPauseBtn').textContent=LOG_PAUSED?'Resume':'Pause'; if(!LOG_PAUSED&&LOG_PENDING.length){LOG_PENDING.splice(0).forEach(appendLogLine);applyLogFilters();} }
function jumpToLatestLogs(){ LOG_AT_LATEST=true; scrollLogsToLatest(true); }
function toggleLogWrap(){ $('logConsole')?.classList.toggle('no-wrap',!$('logWrap')?.checked); }
function toggleLogTimestamps(){ $('logConsole')?.classList.toggle('hide-time',!$('logTimestamps')?.checked); }
function clearVisibleLogs(){ $('logConsole')?.querySelectorAll('.log-line:not(.filtered-out)').forEach(x=>x.remove()); applyLogFilters(); }
async function copyVisibleLogs(){ const text=Array.from($('logConsole')?.querySelectorAll('.log-line:not(.filtered-out)')||[]).map(x=>x.textContent).join('\n'); await navigator.clipboard?.writeText(text); toast('Visible logs copied','ok'); }
const _appendLogLine=appendLogLine; appendLogLine=function(line){ if(LOG_PAUSED){LOG_PENDING.push(line);return;} _appendLogLine(line); if(LOG_AT_LATEST)scrollLogsToLatest(); };
const _applyLogFilters=applyLogFilters; applyLogFilters=function(){ const c=$('logConsole'); if(!c)return; const query=($('logSearch')?.value||'').trim(), level=$('logLevelFilter')?.value||'', mins=Number($('logTimeFilter')?.value||0), regex=$('logRegex')?.checked; let rx=null; if(regex&&query){try{rx=new RegExp(query,'i');}catch(e){}} let visible=0,total=0; const cutoff=Date.now()-mins*60000; c.querySelectorAll('.log-line').forEach(row=>{total++; const text=row.dataset.search||'', lv=!level||row.dataset.level===level; let qm=!query||(rx?rx.test(text):text.includes(query.toLowerCase())); let tm=true;if(mins){const ts=Number(row.dataset.ts||0);tm=!!ts&&ts>=cutoff;} const show=lv&&qm&&tm;row.classList.toggle('filtered-out',!show);row.classList.toggle('match',show&&!!query);if(show)visible++;});updateLogSummary(visible,total); };
const _downloadLogs=downloadLogs; downloadLogs=async function(){ const rows=Array.from($('logConsole')?.querySelectorAll('.log-line:not(.filtered-out)')||[]); if(rows.length){const blob=new Blob([rows.map(x=>x.textContent).join('\n')],{type:'text/plain'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=(logInstaller?'uc-installer':logTarget)+'-filtered.log';a.click();URL.revokeObjectURL(a.href);return;} return _downloadLogs(); };
$('logConsole')?.addEventListener('scroll',()=>{const c=$('logConsole');LOG_AT_LATEST=(c.scrollHeight-c.scrollTop-c.clientHeight)<32;});

const _testRemote=testRemote; testRemote=async function(rid){ const started=Date.now(); try{const r=await api('/api/remotes/'+rid+'/test',{method:'POST'});REMOTE_TELEMETRY[rid]={ok:true,last:new Date(),latency:Date.now()-started,drivers:r.driver_count};toast('Connected in '+(Date.now()-started)+' ms','ok');renderRemoteList();}catch(e){REMOTE_TELEMETRY[rid]={ok:false,last:new Date(),error:e.message};toast('Test failed: '+e.message,'bad');renderRemoteList();} };
const _renderRemoteList=renderRemoteList; renderRemoteList=function(){ _renderRemoteList(); (REMOTES.remotes||[]).forEach(r=>{const row=$('rem-'+r.id),t=REMOTE_TELEMETRY[r.id];if(!row)return;const div=document.createElement('div');div.className='rem-telemetry';div.innerHTML=t?(t.ok?'<span style="color:var(--emerald)">reachable</span><span>'+t.latency+' ms</span><span>'+(t.drivers??'—')+' drivers</span><span>tested '+t.last.toLocaleTimeString()+'</span>':'<span style="color:var(--rose)">unreachable</span><span>'+esc(t.error||'failed')+'</span>'):'<span>connection not tested</span>';row.querySelector('.rem-actions')?.before(div);}); };

function captureFormBaseline(rootId){ const root=$(rootId); if(!root)return; const data={}; root.querySelectorAll('input,select,textarea').forEach((el,i)=>data[el.id||i]=el.type==='checkbox'?el.checked:el.value); FORM_BASELINES.set(rootId,JSON.stringify(data)); }
function formDirty(rootId){ const root=$(rootId);if(!root||!FORM_BASELINES.has(rootId))return false;const data={};root.querySelectorAll('input,select,textarea').forEach((el,i)=>data[el.id||i]=el.type==='checkbox'?el.checked:el.value);return JSON.stringify(data)!==FORM_BASELINES.get(rootId);}
const _saveMainSettings=saveMainSettings;
window.addEventListener('beforeunload',e=>{if(formDirty('maintBack')||formDirty('cfgBack')||formDirty('remBack')){e.preventDefault();e.returnValue='';}});

restoreInstalledView();
setTimeout(()=>{if(location.hash)applyRoute();else setHash('browse',true);},0);

init();
