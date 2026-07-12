/* Comprehensive Health workspace. */
(() => {
  let refreshTimer = null;
  let refreshing = false;
  let lastData = null;

  const E =
    window.esc ||
    window.escHtml ||
    (value =>
      String(value ?? "").replace(
        /[&<>"']/g,
        char =>
          ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
          })[char],
      ));

  const B =
    window.fmtBytes ||
    (value =>
      value == null
        ? "—"
        : `${Math.round(Number(value) / 1024 / 1024)} MB`);

  const duration = seconds => {
    if (seconds == null) return "—";

    let remaining = Math.max(0, Number(seconds));
    const days = Math.floor(remaining / 86400);
    remaining %= 86400;

    const hours = Math.floor(remaining / 3600);
    remaining %= 3600;

    const minutes = Math.floor(remaining / 60);

    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  };

  const pct = value =>
    value == null ? "—" : `${Number(value).toFixed(1)}%`;

  const tone = value => {
    if (
      value === true ||
      ["healthy", "running", "responding", "online"].includes(value)
    ) {
      return "good";
    }

    if (
      value === false ||
      [
        "unhealthy",
        "unreachable",
        "failed",
        "missing",
        "dead",
      ].includes(value)
    ) {
      return "bad";
    }

    return "warn";
  };

  const status = (label, value, text) =>
    `<span class="health-status ${tone(value)}"><span></span>${E(
      text || label,
    )}</span>`;

  const meter = (value, label) => {
    const normalized =
      value == null
        ? 0
        : Math.max(0, Math.min(100, Number(value)));

    return `
      <div class="health-meter" aria-label="${E(label)} ${pct(value)}">
        <i style="width:${normalized}%"></i>
      </div>
    `;
  };

  const metric = (label, value, sub = "") => `
    <div class="health-metric">
      <span>${E(label)}</span>
      <strong>${E(value)}</strong>
      ${sub ? `<small>${E(sub)}</small>` : ""}
    </div>
  `;

  /**
   * Create an Operations drawer entry when the global Operations API exists.
   *
   * The returned value can be null. Callers must pass it to
   * updateHealthOperation(), which safely handles unavailable APIs.
   */
  function createHealthOperation(title, detail, state = "running") {
    if (typeof window.operationNotice === "function") {
      return window.operationNotice(title, detail, state);
    }

    console.info(`[Health] ${title}: ${detail}`);
    return null;
  }

  /**
   * Update an Operations drawer entry without assuming that the Operations
   * module has loaded or that an operation entry was created.
   */
  function updateHealthOperation(operationId, patch) {
    if (
      operationId != null &&
      typeof window.updateOperation === "function"
    ) {
      window.updateOperation(operationId, patch);
      return;
    }

    const lines = Array.isArray(patch?.lines)
      ? patch.lines.join("\n")
      : "";

    if (patch?.status === "failed") {
      console.error(`[Health] ${lines || "Operation failed"}`);
    } else {
      console.info(`[Health] ${lines || "Operation completed"}`);
    }
  }

  function ensurePage() {
    if (document.getElementById("healthBack")) return;

    const panel = document.createElement("section");
    panel.id = "healthBack";
    panel.className = "workspace-panel health-page";

    panel.innerHTML = `
      <div class="workspace-view">
        <header>
          <button
            aria-label="Back"
            class="x"
            onclick="closeModal('healthBack')"
            title="Back"
          >
            <svg class="ms-icon" aria-hidden="true">
              <use href="/static/material-symbols.svg#arrow_back"></use>
            </svg>
          </button>

          <h2>Health</h2>
          <span class="sub">
            host · managed Docker · remotes · installer
          </span>

          <div class="health-head-actions">
            <span class="health-updated" id="healthUpdated">
              Not loaded
            </span>

            <label class="health-auto">
              <input
                id="healthAutoRefresh"
                type="checkbox"
                checked
                onchange="setHealthAutoRefresh(this.checked)"
              >
              Auto-refresh
            </label>

            <button
              class="btn btn-line btn-sm"
              id="healthRefreshBtn"
              onclick="refreshHealth(true)"
            >
              <svg class="ms-icon" aria-hidden="true">
                <use href="/static/material-symbols.svg#refresh"></use>
              </svg>
              <span>Refresh</span>
            </button>
          </div>
        </header>

        <div class="body health-body" id="healthContent">
          ${skeleton()}
        </div>
      </div>
    `;

    document.querySelector("main")?.appendChild(panel);
  }

  function skeleton() {
    return `
      <div class="health-grid">
        <div class="health-card health-card-wide">
          <div class="skeleton-row"></div>
          <div class="skeleton-row"></div>
        </div>

        <div class="health-card">
          <div class="skeleton-row"></div>
          <div class="skeleton-row"></div>
        </div>

        <div class="health-card">
          <div class="skeleton-row"></div>
          <div class="skeleton-row"></div>
        </div>
      </div>
    `;
  }

  function cardHeader(title, subtitle, stateValue, stateText) {
    return `
      <div class="health-card-head">
        <div>
          <h3>${E(title)}</h3>
          <p>${E(subtitle)}</p>
        </div>

        ${status("", stateValue, stateText)}
      </div>
    `;
  }

  function renderHost(host) {
    const memory = host.memory || {};
    const disk = host.disk || {};
    const load = host.load || [];

    const loadText =
      load[0] == null
        ? "—"
        : load
            .map(value =>
              value == null ? "—" : Number(value).toFixed(2),
            )
            .join(" / ");

    return `
      <section class="health-card health-card-wide">
        ${cardHeader(
          "Host system",
          `${host.hostname || "Host"} · ${host.architecture || ""}`,
          true,
          "Online",
        )}

        <div class="health-primary-grid">
          <div class="health-gauge">
            <div>
              <strong>${pct(host.cpu_pct)}</strong>
              <span>CPU</span>
            </div>

            ${meter(host.cpu_pct, "CPU usage")}

            <small>
              ${host.cpu_count || "—"} logical CPUs · load ${loadText}
            </small>
          </div>

          <div class="health-gauge">
            <div>
              <strong>${pct(memory.percent)}</strong>
              <span>Memory</span>
            </div>

            ${meter(memory.percent, "Memory usage")}

            <small>
              ${B(memory.used)} of ${B(memory.total)} ·
              ${B(memory.available)} available
            </small>
          </div>

          <div class="health-gauge">
            <div>
              <strong>${pct(disk.percent)}</strong>
              <span>Data disk</span>
            </div>

            ${meter(disk.percent, "Disk usage")}

            <small>
              ${B(disk.used)} of ${B(disk.total)} ·
              ${B(disk.free)} free
            </small>
          </div>
        </div>

        <div class="health-metrics">
          ${metric("Host uptime", duration(host.uptime_seconds))}
          ${metric("Platform", host.platform || "—")}
          ${metric(
            "Swap used",
            B(memory.swap_used),
            memory.swap_total
              ? `of ${B(memory.swap_total)}`
              : "disabled",
          )}
          ${metric("Data path", disk.path || "—")}
        </div>
      </section>
    `;
  }

  function renderDocker(docker) {
    const totals = docker.totals || {};
    const states = docker.states || {};
    const available = Boolean(docker.available);

    const rows = (docker.containers || [])
      .map(
        container => `
          <tr>
            <td>
              <button
                class="health-link"
                onclick="openHealthIntegration('${E(container.id)}')"
              >
                ${E(container.name)}
              </button>
              <small>${E(container.id)}</small>
            </td>

            <td>
              ${status("", container.status, container.status)}
            </td>

            <td>
              ${status("", container.health, container.health)}
            </td>

            <td class="mono">${pct(container.cpu_pct)}</td>
            <td class="mono">${B(container.mem_used)}</td>
            <td class="mono">${container.pids ?? "—"}</td>
            <td class="mono">${container.restarts ?? 0}</td>
          </tr>
        `,
      )
      .join("");

    return `
      <section class="health-card health-card-full">
        ${cardHeader(
          "Managed Docker containers",
          `${docker.managed_count || 0} installer-managed containers`,
          available,
          available ? "Docker available" : "Docker unavailable",
        )}

        <div class="health-metrics health-docker-summary">
          ${metric("Running", states.running || 0)}
          ${metric(
            "Stopped",
            (states.exited || 0) + (states.created || 0),
          )}
          ${metric("Healthy", docker.healthy || 0)}
          ${metric("Unhealthy", docker.unhealthy || 0)}
          ${metric("Combined CPU", pct(totals.cpu_pct))}
          ${metric("Combined memory", B(totals.mem_used))}
          ${metric("Processes", totals.pids || 0)}
          ${metric("Restarts", totals.restarts || 0)}
        </div>

        ${
          rows
            ? `
              <div class="health-table-wrap">
                <table class="health-table">
                  <thead>
                    <tr>
                      <th>Integration</th>
                      <th>Container</th>
                      <th>Health</th>
                      <th>CPU</th>
                      <th>Memory</th>
                      <th>PIDs</th>
                      <th>Restarts</th>
                    </tr>
                  </thead>
                  <tbody>${rows}</tbody>
                </table>
              </div>
            `
            : `
              <div class="health-empty">
                No managed containers are installed.
              </div>
            `
        }
      </section>
    `;
  }

  function renderRemotes(remotes) {
    const reachable = remotes.filter(remote => remote.reachable).length;

    const rows = remotes
      .map(
        remote => `
          <div class="health-remote-row">
            <div class="health-remote-main">
              <strong>${E(remote.name)}</strong>
              <small>${E(remote.address)}</small>
            </div>

            ${status(
              "",
              remote.reachable,
              remote.reachable ? "Reachable" : "Unreachable",
            )}

            <div class="health-remote-stat">
              <strong>
                ${
                  remote.reachable && remote.latency_ms != null
                    ? `${remote.latency_ms} ms`
                    : "—"
                }
              </strong>
              <span>latency</span>
            </div>

            <div class="health-remote-stat">
              <strong>${remote.drivers || 0}</strong>
              <span>drivers</span>
            </div>

            <button
              class="btn btn-line btn-sm"
              onclick="testHealthRemote('${E(remote.id)}')"
            >
              Test
            </button>
          </div>
        `,
      )
      .join("");

    const state =
      remotes.length === 0
        ? "unknown"
        : reachable === remotes.length;

    return `
      <section class="health-card health-card-full">
        ${cardHeader(
          "Remote health",
          `${reachable} of ${remotes.length} reachable`,
          state,
          remotes.length
            ? `${reachable}/${remotes.length} online`
            : "No remotes",
        )}

        <div class="health-remote-list">
          ${
            rows ||
            `
              <div class="health-empty">
                No UC remotes are configured.
              </div>
            `
          }
        </div>
      </section>
    `;
  }

  function renderInstaller(installer) {
    const healthy = installer.status === "healthy";

    return `
      <section class="health-card health-card-full">
        ${cardHeader(
          "UC External Integration Installer",
          `Build ${installer.build || "unknown"} · ${
            installer.bind || ""
          }`,
          healthy,
          installer.status || "unknown",
        )}

        <div class="health-metrics health-installer-grid">
          ${metric(
            "Service uptime",
            duration(installer.uptime_seconds),
          )}
          ${metric("Memory RSS", B(installer.memory_rss))}
          ${metric("Threads", installer.threads || 0)}
          ${metric("Active jobs", installer.active_jobs || 0)}
          ${metric("Failed jobs", installer.failed_jobs || 0)}
          ${metric("Recent errors", installer.recent_errors || 0)}
          ${metric(
            "Registry",
            installer.registry_ok
              ? `v${installer.registry_version || "?"}`
              : "Unavailable",
            installer.registry_commit || "",
          )}
          ${metric(
            "Health probe",
            installer.health_probe ? "Enabled" : "Disabled",
          )}
          ${metric(
            "Authentication",
            installer.token_required ? "Token required" : "Open",
          )}
          ${metric(
            "Service unit",
            installer.service_unit || "—",
          )}
          ${metric(
            "Data directory",
            installer.data_dir || "—",
          )}
          ${metric("API version", installer.version || "—")}
        </div>
      </section>
    `;
  }

  function render(data) {
    lastData = data;

    const content = document.getElementById("healthContent");
    if (!content) return;

    content.innerHTML = `
      <div class="health-grid">
        ${renderHost(data.host || {})}
        ${renderDocker(data.docker || {})}
        ${renderRemotes(data.remotes || [])}
        ${renderInstaller(data.installer || {})}
      </div>
    `;

    const updated = document.getElementById("healthUpdated");
    if (updated) {
      const timestamp = new Date(data.timestamp || Date.now());
      updated.textContent =
        `Updated ${timestamp.toLocaleTimeString()}`;
    }
  }

  window.refreshHealth = async (manual = false) => {
    const panel = document.getElementById("healthBack");

    if (
      refreshing ||
      !panel?.classList.contains("show")
    ) {
      return;
    }

    refreshing = true;

    const button = document.getElementById("healthRefreshBtn");

    if (button) {
      button.disabled = true;
      button.innerHTML = `
        <span class="ui-spinner" aria-hidden="true"></span>
        <span>Refreshing…</span>
      `;
    }

    try {
      const data = await api("/api/health/overview", {
        timeout: 12000,
        dedupe: false,
      });

      render(data);

      if (manual) {
        const operationId = createHealthOperation(
          "Health refreshed",
          "Host, Docker, remote, and installer statistics were updated.",
          "running",
        );

        updateHealthOperation(operationId, {
          status: "success",
          lines: ["Health information refreshed successfully."],
        });
      }
    } catch (error) {
      const message =
        error?.message ||
        "The installer did not return health information.";

      if (!lastData) {
        const content = document.getElementById("healthContent");

        if (content) {
          content.innerHTML = `
            <div class="empty">
              <h3>Health data unavailable</h3>
              <p>${E(message)}</p>
              <button
                class="btn btn-primary"
                onclick="refreshHealth(true)"
              >
                Retry
              </button>
            </div>
          `;
        }
      }

      const operationId = createHealthOperation(
        "Health refresh failed",
        message,
        "running",
      );

      updateHealthOperation(operationId, {
        status: "failed",
        lines: [message],
        retry: () => window.refreshHealth(true),
      });
    } finally {
      refreshing = false;

      if (button) {
        button.disabled = false;
        button.innerHTML = `
          <svg class="ms-icon" aria-hidden="true">
            <use href="/static/material-symbols.svg#refresh"></use>
          </svg>
          <span>Refresh</span>
        `;
      }
    }
  };

  window.setHealthAutoRefresh = enabled => {
    localStorage.setItem(
      "uc_health_auto_refresh",
      enabled ? "1" : "0",
    );

    clearInterval(refreshTimer);

    refreshTimer = enabled
      ? setInterval(() => window.refreshHealth(false), 5000)
      : null;
  };

  window.openHealth = () => {
    ensurePage();
    showWorkspacePanel("healthBack");

    if (typeof window.setHash === "function") {
      window.setHash("health");
    }

    const enabled =
      localStorage.getItem("uc_health_auto_refresh") !== "0";

    const checkbox =
      document.getElementById("healthAutoRefresh");

    if (checkbox) {
      checkbox.checked = enabled;
    }

    window.setHealthAutoRefresh(enabled);
    window.refreshHealth(true);
  };

  window.openHealthIntegration = id => {
    closeModal("healthBack");
    switchTab("installed");

    if (
      window.EXPANDED instanceof Set &&
      !window.EXPANDED.has(id)
    ) {
      toggleDetails(id);
    }

    setTimeout(() => {
      const escapedId =
        window.CSS?.escape
          ? CSS.escape(id)
          : String(id).replace(/["\\]/g, "\\$&");

      document
        .querySelector(`[data-instance-id="${escapedId}"]`)
        ?.scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
    }, 100);
  };

  window.testHealthRemote = async id => {
    const operationId = createHealthOperation(
      "Testing remote",
      id,
      "running",
    );

    try {
      const result = await api(`/api/remotes/${id}/test`, {
        method: "POST",
      });

      const latency =
        result?.latency_ms != null
          ? ` Response time: ${result.latency_ms} ms.`
          : "";

      updateHealthOperation(operationId, {
        status: "success",
        lines: [
          `Remote responded successfully.${latency}`,
        ],
      });

      await window.refreshHealth(true);
    } catch (error) {
      const message =
        error?.message || "The remote did not respond.";

      updateHealthOperation(operationId, {
        status: "failed",
        lines: [message],
        retry: () => window.testHealthRemote(id),
      });
    }
  };

  const previousCloseModal = window.closeModal;

  window.closeModal = function closeHealthModal(id) {
    if (id === "healthBack") {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }

    if (typeof previousCloseModal === "function") {
      return previousCloseModal.apply(this, arguments);
    }

    document.getElementById(id)?.classList.remove("show");
    return undefined;
  };

  ensurePage();
})();