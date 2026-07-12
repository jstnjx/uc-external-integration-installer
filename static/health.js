/* Comprehensive Health workspace. */
(() => {
  let refreshTimer = null;
  let refreshing = false;
  let lastData = null;

  const E = window.esc || window.escHtml || (value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
  const B = window.fmtBytes || (n => n == null ? '—' : `${Math.round(Number(n) / 1024 / 1024)} MB`);
  const duration = seconds => {
    if (seconds == null) return '—';
    let s = Math.max(0, Number(seconds));
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600); s %= 3600;
    const m = Math.floor(s / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };
  const pct = value => value == null ? '—' : `${Number(value).toFixed(1)}%`;
  const tone = value => value === true || ['healthy','running','responding','online'].includes(value) ? 'good' : value === false || ['unhealthy','unreachable','failed','missing','dead'].includes(value) ? 'bad' : 'warn';
  const status = (label, value, text) => `<span class="health-status ${tone(value)}"><span></span>${E(text || label)}</span>`;
  const meter = (value, label) => {
    const n = value == null ? 0 : Math.max(0, Math.min(100, Number(value)));
    return `<div class="health-meter" aria-label="${E(label)} ${pct(value)}"><i style="width:${n}%"></i></div>`;
  };
  const metric = (label, value, sub = '') => `<div class="health-metric"><span>${E(label)}</span><strong>${E(value)}</strong>${sub ? `<small>${E(sub)}</small>` : ''}</div>`;

  function ensurePage() {
    if (document.getElementById('healthBack')) return;
    const panel = document.createElement('section');
    panel.id = 'healthBack';
    panel.className = 'workspace-panel health-page';
    panel.innerHTML = `
      <div class="workspace-view">
        <header>
          <button aria-label="Back" class="x" onclick="closeModal('healthBack')" title="Back"><span class="material-symbols-outlined" aria-hidden="true">arrow_back</span></button>
          <h2>Health</h2><span class="sub">host · managed Docker · remotes · installer</span>
          <div class="health-head-actions">
            <span class="health-updated" id="healthUpdated">Not loaded</span>
            <label class="health-auto"><input id="healthAutoRefresh" type="checkbox" checked onchange="setHealthAutoRefresh(this.checked)"> Auto-refresh</label>
            <button class="btn btn-line btn-sm" id="healthRefreshBtn" onclick="refreshHealth(true)"><span class="material-symbols-outlined" aria-hidden="true">refresh</span><span>Refresh</span></button>
          </div>
        </header>
        <div class="body health-body" id="healthContent">${skeleton()}</div>
      </div>`;
    document.querySelector('main').appendChild(panel);
  }

  function skeleton() {
    return `<div class="health-grid"><div class="health-card health-card-wide"><div class="skeleton-row"></div><div class="skeleton-row"></div></div><div class="health-card"><div class="skeleton-row"></div><div class="skeleton-row"></div></div><div class="health-card"><div class="skeleton-row"></div><div class="skeleton-row"></div></div></div>`;
  }

  function cardHeader(title, subtitle, stateValue, stateText) {
    return `<div class="health-card-head"><div><h3>${E(title)}</h3><p>${E(subtitle)}</p></div>${status('', stateValue, stateText)}</div>`;
  }

  function renderHost(host) {
    const mem = host.memory || {}, disk = host.disk || {}, load = host.load || [];
    return `<section class="health-card health-card-wide">
      ${cardHeader('Host system', `${host.hostname || 'Host'} · ${host.architecture || ''}`, true, 'Online')}
      <div class="health-primary-grid">
        <div class="health-gauge"><div><strong>${pct(host.cpu_pct)}</strong><span>CPU</span></div>${meter(host.cpu_pct, 'CPU usage')}<small>${host.cpu_count || '—'} logical CPUs · load ${load[0] == null ? '—' : load.map(v => v == null ? '—' : Number(v).toFixed(2)).join(' / ')}</small></div>
        <div class="health-gauge"><div><strong>${pct(mem.percent)}</strong><span>Memory</span></div>${meter(mem.percent, 'Memory usage')}<small>${B(mem.used)} of ${B(mem.total)} · ${B(mem.available)} available</small></div>
        <div class="health-gauge"><div><strong>${pct(disk.percent)}</strong><span>Data disk</span></div>${meter(disk.percent, 'Disk usage')}<small>${B(disk.used)} of ${B(disk.total)} · ${B(disk.free)} free</small></div>
      </div>
      <div class="health-metrics">${metric('Host uptime', duration(host.uptime_seconds))}${metric('Platform', host.platform || '—')}${metric('Swap used', B(mem.swap_used), mem.swap_total ? `of ${B(mem.swap_total)}` : 'disabled')}${metric('Data path', disk.path || '—')}</div>
    </section>`;
  }

  function renderDocker(docker) {
    const totals = docker.totals || {}, states = docker.states || {};
    const available = !!docker.available;
    const rows = (docker.containers || []).map(c => `<tr>
      <td><button class="health-link" onclick="openHealthIntegration('${E(c.id)}')">${E(c.name)}</button><small>${E(c.id)}</small></td>
      <td>${status('', c.status, c.status)}</td><td>${status('', c.health, c.health)}</td>
      <td class="mono">${pct(c.cpu_pct)}</td><td class="mono">${B(c.mem_used)}</td><td class="mono">${c.pids || '—'}</td><td class="mono">${c.restarts || 0}</td>
    </tr>`).join('');
    return `<section class="health-card health-card-full">
      ${cardHeader('Managed Docker containers', `${docker.managed_count || 0} installer-managed containers`, available, available ? 'Docker available' : 'Docker unavailable')}
      <div class="health-metrics health-docker-summary">
        ${metric('Running', states.running || 0)}${metric('Stopped', (states.exited || 0) + (states.created || 0))}${metric('Healthy', docker.healthy || 0)}${metric('Unhealthy', docker.unhealthy || 0)}${metric('Combined CPU', pct(totals.cpu_pct))}${metric('Combined memory', B(totals.mem_used))}${metric('Processes', totals.pids || 0)}${metric('Restarts', totals.restarts || 0)}
      </div>
      ${rows ? `<div class="health-table-wrap"><table class="health-table"><thead><tr><th>Integration</th><th>Container</th><th>Health</th><th>CPU</th><th>Memory</th><th>PIDs</th><th>Restarts</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="health-empty">No managed containers are installed.</div>`}
    </section>`;
  }

  function renderRemotes(remotes) {
    const reachable = remotes.filter(r => r.reachable).length;
    const rows = remotes.map(r => `<div class="health-remote-row">
      <div class="health-remote-main"><strong>${E(r.name)}</strong><small>${E(r.address)}</small></div>
      ${status('', r.reachable, r.reachable ? 'Reachable' : 'Unreachable')}
      <div class="health-remote-stat"><strong>${r.reachable ? `${r.latency_ms} ms` : '—'}</strong><span>latency</span></div>
      <div class="health-remote-stat"><strong>${r.drivers || 0}</strong><span>drivers</span></div>
      <button class="btn btn-line btn-sm" onclick="testHealthRemote('${E(r.id)}')">Test</button>
    </div>`).join('');
    return `<section class="health-card health-card-full">
      ${cardHeader('Remote health', `${reachable} of ${remotes.length} reachable`, remotes.length === 0 ? 'unknown' : reachable === remotes.length, remotes.length ? `${reachable}/${remotes.length} online` : 'No remotes')}
      <div class="health-remote-list">${rows || '<div class="health-empty">No UC remotes are configured.</div>'}</div>
    </section>`;
  }

  function renderInstaller(installer) {
    const ok = installer.status === 'healthy';
    return `<section class="health-card health-card-full">
      ${cardHeader('UC External Integration Installer', `Build ${installer.build || 'unknown'} · ${installer.bind || ''}`, ok, installer.status || 'unknown')}
      <div class="health-metrics health-installer-grid">
        ${metric('Service uptime', duration(installer.uptime_seconds))}${metric('Memory RSS', B(installer.memory_rss))}${metric('Threads', installer.threads || 0)}${metric('Active jobs', installer.active_jobs || 0)}${metric('Failed jobs', installer.failed_jobs || 0)}${metric('Recent errors', installer.recent_errors || 0)}${metric('Registry', installer.registry_ok ? `v${installer.registry_version || '?'}` : 'Unavailable', installer.registry_commit || '')}${metric('Health probe', installer.health_probe ? 'Enabled' : 'Disabled')}${metric('Authentication', installer.token_required ? 'Token required' : 'Open')}${metric('Service unit', installer.service_unit || '—')}${metric('Data directory', installer.data_dir || '—')}${metric('API version', installer.version || '—')}
      </div>
    </section>`;
  }

  function render(data) {
    lastData = data;
    document.getElementById('healthContent').innerHTML = `<div class="health-grid">${renderHost(data.host || {})}${renderDocker(data.docker || {})}${renderRemotes(data.remotes || [])}${renderInstaller(data.installer || {})}</div>`;
    const stamp = new Date(data.timestamp || Date.now());
    document.getElementById('healthUpdated').textContent = `Updated ${stamp.toLocaleTimeString()}`;
  }

  window.refreshHealth = async (manual = false) => {
    const panel = document.getElementById('healthBack');
    if (refreshing || !panel?.classList.contains('show')) return;
    refreshing = true;
    const button = document.getElementById('healthRefreshBtn');
    if (button) { button.disabled = true; button.innerHTML = '<span class="ui-spinner" aria-hidden="true"></span><span>Refreshing…</span>'; }
    try {
      const data = await api('/api/health/overview', {timeout: 12000, dedupe: false});
      render(data);
    } catch (error) {
      if (!lastData) document.getElementById('healthContent').innerHTML = `<div class="empty"><h3>Health data unavailable</h3><p>${E(error.message || 'The installer did not return health information.')}</p><button class="btn btn-primary" onclick="refreshHealth(true)">Retry</button></div>`;
      if (typeof window.operationNotice === 'function') window.operationNotice('Health refresh failed', error.message || 'Could not load health information.', 'failed', {retry: () => refreshHealth(true)});
    } finally {
      refreshing = false;
      if (button) { button.disabled = false; button.innerHTML = '<span class="material-symbols-outlined" aria-hidden="true">refresh</span><span>Refresh</span>'; }
    }
  };

  window.setHealthAutoRefresh = enabled => {
    localStorage.setItem('uc_health_auto_refresh', enabled ? '1' : '0');
    clearInterval(refreshTimer);
    refreshTimer = enabled ? setInterval(() => refreshHealth(false), 5000) : null;
  };
  window.openHealth = () => {
    ensurePage();
    showWorkspacePanel('healthBack');
    setHash?.('health');
    const enabled = localStorage.getItem('uc_health_auto_refresh') !== '0';
    document.getElementById('healthAutoRefresh').checked = enabled;
    setHealthAutoRefresh(enabled);
    refreshHealth(true);
  };
  window.openHealthIntegration = id => {
    closeModal('healthBack'); switchTab('installed');
    if (!EXPANDED.has(id)) toggleDetails(id);
    setTimeout(() => document.querySelector(`[data-instance-id="${CSS.escape(id)}"]`)?.scrollIntoView({behavior:'smooth', block:'center'}), 100);
  };
  window.testHealthRemote = async id => {
    const op = operationNotice(`Testing remote`, id, 'running');
    try { await api(`/api/remotes/${id}/test`, {method:'POST'}); updateOperation(op, {status:'success', lines:['Remote responded successfully.']}); refreshHealth(true); }
    catch (error) { updateOperation(op, {status:'failed', lines:[error.message], retry:() => testHealthRemote(id)}); }
  };

  const oldClose = window.closeModal;
  window.closeModal = function(id) {
    if (id === 'healthBack') { clearInterval(refreshTimer); refreshTimer = null; }
    return oldClose.apply(this, arguments);
  };
  ensurePage();
})();
