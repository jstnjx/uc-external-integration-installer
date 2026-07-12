/* Installed integration management, registration state, details grouping, and safe bulk actions. */
(() => {
  const pending = AppStore.state.registrationPending;

  const REMOTE_AVAILABILITY = new Map();
  const REMOTE_AVAILABILITY_TTL = 15000;

  EXPANDED.clear();
  (AppStore.state.expanded || new Set()).forEach(id => EXPANDED.add(id));

  function selectionMarker(selected) {
    return selected
      ? '<span class="selection-marker on" aria-hidden="true">' +
          msIcon('check') +
        '</span>'
      : '';
  }

  function versionState(it, upd) {
    const version = String(it.version || 'latest');

    if (upd.update_available) {
      return {
        text: 'Update ' + fmtVer(upd.latest_version || ''),
        cls: 'warn',
        click: "openVersion('" + it.id + "')",
      };
    }

    if (/alpha|beta|rc|pre/i.test(version)) {
      return {
        text: 'Pre-release',
        cls: 'warn',
      };
    }

    if (version === 'latest') {
      return {
        text: 'latest',
        cls: 'good',
      };
    }

    if (version) {
      return {
        text: 'Pinned ' + fmtVer(version),
        cls: 'neutral',
      };
    }

    return {
      text: 'Unknown',
      cls: 'warn',
    };
  }

  function lifecycleButtons(it) {
    const name = esc(it.label || it.name);
    const state = it.status;

    if (state === 'running') {
      return (
        '<button class="row-icon-btn" title="Stop" aria-label="Stop ' +
          name +
          '" onclick="lifecycle(\'' +
          it.id +
          '\',\'stop\')">' +
          msIcon('stop') +
        '</button>' +
        '<button class="row-icon-btn" title="Restart" aria-label="Restart ' +
          name +
          '" onclick="lifecycle(\'' +
          it.id +
          '\',\'restart\')">' +
          msIcon('restart_alt') +
        '</button>'
      );
    }

    if (state === 'restarting') {
      return (
        '<button class="row-icon-btn" disabled title="Restarting">' +
          '<span class="spin"></span>' +
        '</button>'
      );
    }

    if (state === 'missing') {
      return (
        '<button class="row-icon-btn register" ' +
          'title="Rebuild missing integration" ' +
          'aria-label="Rebuild ' +
          name +
          '" ' +
          'onclick="rebuild(\'' +
          it.id +
          '\')">' +
          msIcon('restart_alt') +
        '</button>'
      );
    }

    return (
      '<button class="row-icon-btn register" title="Start" aria-label="Start ' +
        name +
        '" onclick="lifecycle(\'' +
        it.id +
        '\',\'start\')">' +
        msIcon('play_arrow') +
      '</button>'
    );
  }

  function registrationFor(it, remote) {
    return (REGS[it.id] || []).find(
      registration => registration.remote_id === remote.id
    );
  }

  function remoteAddress(remote) {
    const scheme = remote.scheme || 'http';
    const host = remote.host || '';
    const port = remote.port ? ':' + remote.port : '';

    return host ? scheme + '://' + host + port : '';
  }

  async function checkRemoteAvailability(remote, force = false) {
    const cached = REMOTE_AVAILABILITY.get(remote.id);

    if (
      !force &&
      cached &&
      Date.now() - cached.checkedAt < REMOTE_AVAILABILITY_TTL
    ) {
      return cached;
    }

    REMOTE_AVAILABILITY.set(remote.id, {
      available: null,
      checking: true,
      checkedAt: Date.now(),
      message: '',
    });

    try {
      const result = await api(
        '/api/remotes/' + encodeURIComponent(remote.id) + '/test',
        {
          method: 'POST',
          timeout: 8000,
          dedupe: false,
        }
      );

      const state = {
        available: true,
        checking: false,
        checkedAt: Date.now(),
        message: result?.message || '',
        latencyMs: result?.latency_ms ?? null,
      };

      REMOTE_AVAILABILITY.set(remote.id, state);
      return state;
    } catch (error) {
      const state = {
        available: false,
        checking: false,
        checkedAt: Date.now(),
        message: error?.message || 'Remote is unreachable.',
        latencyMs: null,
      };

      REMOTE_AVAILABILITY.set(remote.id, state);
      return state;
    }
  }

  function remoteState(remote, registered, busy) {
    if (busy) {
      return {
        text: 'Working…',
        cls: 'checking',
        disabled: true,
      };
    }

    const availability = REMOTE_AVAILABILITY.get(remote.id);

    if (!availability || availability.checking) {
      return {
        text: 'Checking…',
        cls: 'checking',
        disabled: true,
      };
    }

    if (availability.available === false) {
      return {
        text: 'Unavailable',
        cls: 'unavailable',
        disabled: true,
      };
    }

    if (registered) {
      return {
        text: 'Registered',
        cls: 'registered',
        disabled: false,
      };
    }

    return {
      text: 'Available',
      cls: 'available',
      disabled: false,
    };
  }

  window.remoteRegisterMenuHtml = function remoteRegisterMenuHtml(it) {
    const remotes = REMOTES.remotes || [];

    if (!remotes.length) {
      return (
        '<div class="menu-label">No remotes configured</div>' +
        '<button type="button" onclick="openRemotes()">Add a remote</button>'
      );
    }

    return (
      '<div class="menu-label">Remote registrations</div>' +
      remotes
        .map(remote => {
          const registered = Boolean(registrationFor(it, remote));
          const busy = pending.has(it.id + ':' + remote.id);
          const state = remoteState(remote, registered, busy);
          const address = remoteAddress(remote);

          const action = registered
            ? "unregisterIntegrationFromRemote('" +
              it.id +
              "','" +
              remote.id +
              "')"
            : "registerIntegration('" +
              it.id +
              "','" +
              remote.id +
              "')";

          return (
            '<button type="button" ' +
              'class="remote-register-option' +
                (state.disabled ? ' is-disabled' : '') +
              '" ' +
              (state.disabled ? 'disabled ' : '') +
              'onclick="' +
                (state.disabled ? '' : action) +
              '" ' +
              'title="' +
                esc(
                  state.cls === 'unavailable'
                    ? REMOTE_AVAILABILITY.get(remote.id)?.message ||
                        'Remote is unavailable'
                    : ''
                ) +
              '">' +

              '<span class="remote-register-copy">' +
                '<strong>' +
                  esc(remote.name || remote.host || remote.id) +
                '</strong>' +
                (address
                  ? '<small>' + esc(address) + '</small>'
                  : '') +
              '</span>' +

              '<span class="remote-state-pill ' +
                state.cls +
              '">' +
                esc(state.text) +
              '</span>' +
            '</button>'
          );
        })
        .join('')
    );
  };

  window.toggleRegisterMenu = async function toggleRegisterMenu(event, id) {
    event.stopPropagation();

    const menu = $('reg-menu-' + id);
    const wasOpen = menu?.classList.contains('show');

    closeMenus();

    if (!menu || wasOpen) {
      return;
    }

    const integration = INSTALLED.find(item => item.id === id);

    if (!integration) {
      return;
    }

    /*
     * Render the initial checking state immediately so the dropdown opens
     * without waiting for all remote requests to finish.
     */
    (REMOTES.remotes || []).forEach(remote => {
      const cached = REMOTE_AVAILABILITY.get(remote.id);

      if (
        !cached ||
        Date.now() - cached.checkedAt >= REMOTE_AVAILABILITY_TTL
      ) {
        REMOTE_AVAILABILITY.set(remote.id, {
          available: null,
          checking: true,
          checkedAt: Date.now(),
          message: '',
        });
      }
    });

    menu.innerHTML = remoteRegisterMenuHtml(integration);
    menu.classList.add('show');

    const results = await Promise.allSettled(
      (REMOTES.remotes || []).map(remote =>
        checkRemoteAvailability(remote, true)
      )
    );

    /*
     * Avoid reopening or rewriting a menu the user has already closed while
     * the remote tests were running.
     */
    if (!menu.classList.contains('show')) {
      return;
    }

    menu.innerHTML = remoteRegisterMenuHtml(integration);

    const firstEnabled = menu.querySelector(
      '.remote-register-option:not(:disabled)'
    );

    firstEnabled?.focus();

    return results;
  };

  window.unregisterIntegrationFromRemote =
    async function unregisterIntegrationFromRemote(id, remoteId) {
      const integration = INSTALLED.find(item => item.id === id);

      if (!integration) {
        return;
      }

      const key = id + ':' + remoteId;
      pending.add(key);
      renderInstalled();

      try {
        await api(
          '/api/remotes/' +
            encodeURIComponent(remoteId) +
            '/drivers/' +
            encodeURIComponent(integration.driver_id || id),
          {
            method: 'DELETE',
          }
        );

        toast('Unregistered from remote', 'ok');
        await loadRegistrations();
      } catch (error) {
        toast(
          'Unregister failed: ' +
            (error?.message || 'Unknown error'),
          'bad'
        );
      } finally {
        pending.delete(key);
        renderInstalled();
      }
    };

  const previousRegisterIntegration = window.registerIntegration;

  window.registerIntegration = async function registerIntegration(
    id,
    remoteId
  ) {
    const targetRemoteId =
      remoteId || (REMOTES.remotes || [])[0]?.id || '';

    if (!targetRemoteId) {
      openRemotes();
      return;
    }

    const cached = REMOTE_AVAILABILITY.get(targetRemoteId);

    if (cached?.available === false) {
      toast('The selected remote is unavailable.', 'bad');
      return;
    }

    const key = id + ':' + targetRemoteId;
    pending.add(key);
    renderInstalled();

    try {
      return await previousRegisterIntegration(id, targetRemoteId);
    } finally {
      pending.delete(key);
      renderInstalled();
    }
  };

  window.toggleDetails = function toggleDetails(id) {
    const wasOpen = EXPANDED.has(id);

    EXPANDED.clear();

    if (!wasOpen) {
      EXPANDED.add(id);
    }

    AppStore.state.expanded = new Set(EXPANDED);
    AppStore.persist();
    renderInstalled();

    if (EXPANDED.has(id)) {
      setTimeout(() => {
        $('details-' + id)?.focus();
      }, 0);
    }
  };

  window.detailsHtml = function detailsHtml(it) {
    const stats = STATS[it.id] || {};
    const update = UPDATES[it.id] || {};

    const repository = it.repository
      ? '<a href="' +
        esc(it.repository) +
        '" target="_blank" rel="noopener">' +
        esc(it.repository.replace('https://github.com/', '')) +
        '</a>'
      : '—';

    return (
      '<div class="details-sections" id="details-' +
        it.id +
        '" tabindex="-1">' +

        '<section>' +
          '<h4>Runtime</h4>' +
          '<div class="tiles">' +

            '<div class="tile">' +
              '<div class="tile-v">' +
                (stats.cpu_pct ?? '—') +
                (stats.cpu_pct != null
                  ? '<span class="u">%</span>'
                  : '') +
              '</div>' +
              '<div class="tile-k">CPU</div>' +
            '</div>' +

            '<div class="tile">' +
              '<div class="tile-v">' +
                (stats.mem_used != null
                  ? fmtBytes(stats.mem_used)
                  : '—') +
              '</div>' +
              '<div class="tile-k">Memory</div>' +
            '</div>' +

            '<div class="tile">' +
              '<div class="tile-v">' +
                fmtUptime(stats.started_at) +
              '</div>' +
              '<div class="tile-k">Uptime</div>' +
            '</div>' +

            '<div class="tile">' +
              '<div class="tile-v">' +
                esc(stats.health || 'unknown') +
              '</div>' +
              '<div class="tile-k">Health</div>' +
            '</div>' +

            '<div class="tile">' +
              '<div class="tile-v">' +
                esc(String(it.restart_count || 0)) +
              '</div>' +
              '<div class="tile-k">Restarts</div>' +
            '</div>' +

          '</div>' +
        '</section>' +

        '<section>' +
          '<h4>Configuration</h4>' +
          '<div class="det-grid">' +

            '<div class="kv">' +
              '<span class="k2">Port</span>' +
              '<span class="v2">' +
                esc(String(it.port)) +
              '</span>' +
            '</div>' +

            '<div class="kv">' +
              '<span class="k2">Version</span>' +
              '<span class="v2">' +
                esc(fmtVer(it.version || 'latest')) +
              '</span>' +
            '</div>' +

            '<div class="kv">' +
              '<span class="k2">Source</span>' +
              '<span class="v2">' +
                esc(it.source || '—') +
              '</span>' +
            '</div>' +

            '<div class="kv">' +
              '<span class="k2">Driver ID</span>' +
              '<span class="v2">' +
                esc(it.driver_id || '—') +
              '</span>' +
            '</div>' +

            '<div class="kv">' +
              '<span class="k2">Repository</span>' +
              '<span class="v2">' +
                repository +
              '</span>' +
            '</div>' +

          '</div>' +
        '</section>' +

        '<section>' +
          '<h4>Management</h4>' +

          '<div class="management-grid">' +
            '<button class="btn btn-line" onclick="openLogs(\'' +
              it.id +
              '\')">Logs</button>' +

            '<button class="btn btn-line" onclick="openConfig(\'' +
              it.id +
              '\')">Configure</button>' +

            '<button class="btn btn-line" onclick="openVersion(\'' +
              it.id +
              '\')">Change version</button>' +

            '<label class="auto-control">' +
              '<input type="checkbox" ' +
                (it.auto_update ? 'checked' : '') +
                ' onchange="setAutoUpdate(\'' +
                it.id +
                '\',this.checked)">' +
              ' Auto-update' +
            '</label>' +

            '<button class="btn btn-line" onclick="backupInstance(\'' +
              it.id +
              '\')">Backup</button>' +

            '<button class="btn btn-line" onclick="restoreInstance(\'' +
              it.id +
              '\')">Restore</button>' +

            '<button class="btn btn-line" onclick="rebuild(\'' +
              it.id +
              '\')">Rebuild</button>' +

            '<button class="btn btn-danger" onclick="removeIntegration(\'' +
              it.id +
              '\')">Remove</button>' +
          '</div>' +
        '</section>' +

      '</div>'
    );
  };

  const previousToggleInstalledSelection =
    window.toggleInstalledSelection;

  window.toggleInstalledSelection = function toggleInstalledSelection(
    id,
    checked
  ) {
    previousToggleInstalledSelection(id, checked);
    renderInstalled();
  };

  window.renderInstalled = function renderInstalled() {
    const box = $('installedRows');

    box.innerHTML = '';
    box.className = 'rows installed-list';

    updateOverview();

    const visible = filteredInstalled();

    if (!visible.length) {
      box.innerHTML =
        '<div class="empty">' +
          '<h3>' +
            (INSTALLED.length
              ? 'No matching instances'
              : 'No integrations installed') +
          '</h3>' +

          '<p>' +
            (INSTALLED.length
              ? 'Adjust the active filters.'
              : 'Browse the registry to install your first integration.') +
          '</p>' +

          (INSTALLED.length
            ? ''
            : '<button class="btn btn-primary" ' +
              'onclick="switchTab(\'browse\')">' +
              'Browse integrations' +
              '</button>') +
        '</div>';

      updateBulkBar();
      return;
    }

    visible.forEach(it => {
      const restartCount = it.restart_count || 0;
      const update = UPDATES[it.id] || {};
      const registrations = REGS[it.id] || [];
      const stats = STATS[it.id] || {};

      const health =
        stats.health ||
        it.health ||
        (it.status === 'running' ? 'starting' : 'unknown');

      const expanded = EXPANDED.has(it.id);
      const selected = SELECTED_INSTALLED.has(it.id);

      const crashLooping =
        (it.status === 'restarting' || it.status === 'exited') &&
        restartCount >= 3;

      const version = versionState(it, update);

      const row = document.createElement('article');

      row.className =
        'row integration-row' +
        (selected ? ' selected' : '');

      row.tabIndex = 0;
      row.dataset.instanceId = it.id;
      row.setAttribute('aria-selected', String(selected));
      row.setAttribute('role', 'option');

      row.onclick = event =>
        handleInstalledRowClick(event, it.id);

      row.onkeydown = event =>
        handleInstalledRowKey(event, it.id);

      row.innerHTML =
        '<div class="integration-row-main">' +

          '<div class="integration-row-state ' +
            stateRailClass(it.status) +
          '"></div>' +

          '<div class="integration-row-content">' +

            '<div class="integration-row-head">' +

              '<span class="integration-row-title">' +
                esc(it.label || it.name) +
              '</span>' +

              '<button class="version-state ' +
                version.cls +
                '" ' +
                (version.click
                  ? 'onclick="' + version.click + '"'
                  : '') +
              '>' +
                esc(version.text) +
              '</button>' +

              '<span class="integration-title-actions">' +
                lifecycleButtons(it) +

                '<span class="menu-wrap">' +
                  '<button class="row-icon-btn register" ' +
                    'title="Manage remote registrations" ' +
                    'aria-label="Manage remote registrations for ' +
                      esc(it.label || it.name) +
                    '" ' +
                    'onclick="toggleRegisterMenu(event,\'' +
                      it.id +
                    '\')">' +
                    msIcon('add') +
                  '</button>' +

                  '<div class="menu register-menu" ' +
                    'id="reg-menu-' +
                    it.id +
                  '">' +
                    remoteRegisterMenuHtml(it) +
                  '</div>' +
                '</span>' +
              '</span>' +

              selectionMarker(selected) +
            '</div>' +

            '<div class="integration-row-sub">' +
              esc(it.id) +
              (it.driver_id
                ? ' · ' + esc(it.driver_id)
                : '') +
            '</div>' +

            '<div class="state-group">' +
              '<span class="state-pill ' +
                (registrations.length ? 'good' : 'warn') +
              '">' +
                (registrations.length
                  ? esc(
                      registrations.length +
                      ' registered'
                    )
                  : 'unregistered') +
              '</span>' +
            '</div>' +

            '<div class="integration-row-meta">' +
              '<span>port <strong>' +
                esc(String(it.port)) +
              '</strong></span>' +

              '<span>cpu <strong>' +
                (stats.cpu_pct != null
                  ? stats.cpu_pct + '%'
                  : '—') +
              '</strong></span>' +

              '<span>memory <strong>' +
                (stats.mem_used != null
                  ? fmtBytes(stats.mem_used)
                  : '—') +
              '</strong></span>' +

              '<span>uptime <strong>' +
                fmtUptime(stats.started_at) +
              '</strong></span>' +
            '</div>' +

            (crashLooping
              ? '<div class="crash-warning">' +
                  '<div>' +
                    '<strong>Crash loop detected</strong>' +
                    '<span>' +
                      restartCount +
                      ' restart attempts' +
                    '</span>' +
                  '</div>' +

                  '<button class="btn btn-danger btn-sm" ' +
                    'onclick="openLogs(\'' +
                      it.id +
                    '\')">' +
                    'Open logs' +
                  '</button>' +
                '</div>'
              : '') +
          '</div>' +

          '<button class="integration-row-chevron' +
            (expanded ? ' open' : '') +
            '" ' +
            'title="' +
              (expanded ? 'Hide' : 'Show') +
              ' details" ' +
            'aria-label="' +
              (expanded ? 'Hide' : 'Show') +
              ' details for ' +
              esc(it.label || it.name) +
            '" ' +
            'onclick="toggleDetails(\'' +
              it.id +
            '\')">' +
            '<span>' +
              msIcon('chevron_right') +
            '</span>' +
          '</button>' +
        '</div>' +

        (expanded
          ? '<div class="details">' +
              detailsHtml(it) +
            '</div>'
          : '');

      box.appendChild(row);
    });

    updateBulkBar();
  };

  const previousUpdateBulkBar = window.updateBulkBar;

  window.updateBulkBar = function updateBulkBar() {
    previousUpdateBulkBar();

    const count = selectedInstalledIds().length;

    if ($('bulkCount')) {
      $('bulkCount').textContent =
        count +
        ' selected integration' +
        (count === 1 ? '' : 's');
    }
  };

  function eligible(action, ids) {
    const rows = ids
      .map(id => INSTALLED.find(item => item.id === id))
      .filter(Boolean);

    const run = [];
    const skip = [];

    for (const integration of rows) {
      const valid =
        action === 'restart'
          ? integration.status === 'running'
          : action === 'start'
            ? integration.status !== 'running'
            : action === 'stop'
              ? integration.status === 'running'
              : true;

      (valid ? run : skip).push(integration);
    }

    return {
      run,
      skip,
    };
  }

  window.bulkLifecycle = async function bulkLifecycle(action) {
    const ids = selectedInstalledIds();

    if (!ids.length) {
      return;
    }

    const { run, skip } = eligible(action, ids);

    const decision = await uiConfirm({
      title:
        action[0].toUpperCase() +
        action.slice(1) +
        ' ' +
        ids.length +
        ' integrations',

      message:
        run.length +
        ' will ' +
        action +
        '.',

      detail: skip.length
        ? skip.length +
          ' will be skipped because their state is not eligible.'
        : 'All selected integrations are eligible.',

      confirmText:
        action[0].toUpperCase() +
        action.slice(1),

      danger: action === 'stop',
    });

    if (!decision.confirmed || !run.length) {
      return;
    }

    await Promise.allSettled(
      run.map(integration =>
        api(
          '/api/instances/' +
            integration.id +
            '/' +
            action,
          {
            method: 'POST',
          }
        )
      )
    );

    toast(
      action +
        ' requested for ' +
        run.length +
        ' integrations',
      'ok'
    );

    setTimeout(loadInstalled, 500);
  };

  setTimeout(() => renderInstalled(), 0);
})();