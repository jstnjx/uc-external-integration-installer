# UC External Integration Installer

A self-hosted background service with a web UI to **browse, install, configure and
manage Unfolded Circle external integrations** on a Docker host. It replaces the
old shell installer with a persistent service you control from a browser.

For each integration it prefers a prebuilt **GHCR image** and falls back to
**building from source**, then runs it as a labelled Docker container on the host
network — exactly the model the shell script used, but manageable and re-entrant.

## What it does

- Fetches the community registry and lists integrations by **name and developer**,
  with search and category filtering.
- **Install** with one click (optionally set a port and environment variables).
- **Manage** running integrations: start, stop, restart, view live logs, remove.
- **Reconfigure** ports and environment variables and recreate the container.
- Runs in the background via **systemd** and survives reboots.

## Quick start (systemd)

```bash
git clone https://github.com/jstnjx/uc-external-integration-installer.git && cd uc-external-integration-installer && chmod +x install.sh && sudo ./install.sh
```

This clones the repo, builds a virtualenv, installs the systemd service, and
starts it. Then open `http://<host-ip>:8900`.

## Run manually (no systemd)

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python uc_installer.py      # serves on 0.0.0.0:8900
```

## Configuration

Set via environment (or `systemctl edit uc-external-integration-installer`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `UC_INSTALLER_HOST` | `0.0.0.0` | Web UI bind address |
| `UC_INSTALLER_PORT` | `8900` | Web UI port |
| `UC_INSTALLER_DATA` | `/var/lib/uc-external-integration-installer` | State, config, cloned source |
| `UC_INSTALLER_TOKEN` | *(empty)* | Bearer token; if set, the UI asks for it |
| `UC_PORT_START` | `8000` | First integration port; auto-increments |
| `UC_REGISTRY_URL` | community registry | Override the registry source |
| `UC_INSTALLER_UPDATE_REPO` | this GitHub repo | Source repo for self-update |
| `UC_INSTALLER_UPDATE_BRANCH` | `main` | Branch the updater tracks |
| `UC_INSTALLER_SERVICE` | `uc-external-integration-installer` | systemd unit restarted after an update |
| `UC_INSTALLER_ALERT_WEBHOOK` | *(empty)* | If set, POSTs an alert when an instance goes unreachable/exits (ntfy URL or generic JSON webhook) |
| `UC_INSTALLER_HEALTH_PROBE` | `1` | Set to `0` to disable the periodic WebSocket health probe (health then shows only container state) |

## Updating

The installed code is a git checkout of the source repo, so the service can
update itself. The web UI shows the current build in the header; when the tracked
branch is ahead, the indicator turns amber and reads **update available**. Opening
it shows the installed vs latest commit and an **Update & restart** button, which:

1. fetches the branch and hard-resets the install directory to its tip,
2. refreshes Python dependencies in the venv,
3. restarts the systemd service (via a transient `systemd-run` unit so the restart
   survives the service going down). Running integration containers are unaffected.

The updater also works on an install that wasn't originally a git checkout — the
first update attaches the repository in place. You can also update from the API
(`POST /api/update/apply`) or just re-run `install.sh`.

If the service isn't running under systemd (e.g. launched by hand), the update is
applied but you restart the process yourself; the UI tells you so.

## Registering with a remote

The installer can register an installed integration directly with one or more UC
remotes over the Core-API, so you don't have to rely on mDNS discovery.

Add remotes from the ⚙ button next to the remote selector in the header. Each
remote needs its address (IP or hostname) and the **web-configurator PIN** (used as
HTTP Basic `web-configurator:PIN`); an API key can be used instead. Use **Test** to
verify the connection, then pick the active remote from the header dropdown.

On any installed integration, **Register** posts its driver to the active remote
(`POST /api/intg/drivers`) with `driver_url = ws://<this-host>:<port>`. The host IP
is auto-detected toward the remote, or you can set an explicit advertise IP per
remote. The integration should be running so the remote can connect to it. The
**Drivers** button on a remote lists what's registered and lets you unregister.

Each installed row shows **which remotes it's registered on** (queried live from the
remotes, briefly cached), its installed **version**, and an amber **update ▸ vX**
badge when a newer release exists. Running rows also show live **CPU, memory,
uptime and health**, and a **Details** panel expands to the full record (image,
driver id, build stack, install/update timestamps, repository) plus live usage
(CPU %, memory used/limit, processes, uptime, health, restart count). Stats are
sampled from Docker in the background and cached, so the UI never blocks on them.
Since these images don't ship a Docker `HEALTHCHECK`, **health** is derived from a
periodic probe of the integration's WebSocket port: `responding` if it completes a
WebSocket handshake, `unreachable` if the container is running but not serving. The
probe performs a real handshake (not a bare TCP connect) so it doesn't spam the
integration's log, is cached (~30s), and can be turned off with
`UC_INSTALLER_HEALTH_PROBE=0`. A real Docker healthcheck is used instead when an
image defines one.

Remote credentials are saved in `UC_INSTALLER_DATA/remotes.json` (file mode `600`,
plaintext). Keep the data directory private; prefer a per-remote PIN/API key you
can revoke over reusing sensitive credentials.

## Instances, activity & maintenance

- **Multiple instances.** Install an integration once for the default instance, then
  use **+ Instance** (on its Browse card) to run additional independent copies — each
  gets its own port, `/config`, and a distinct `driver_id` (`base`, `base_2`, …) so
  they register separately on a remote. Rows are labelled `Name`, `Name #2`, …
- **Install from a release archive** *(ARM64 hosts only)*. On an ARM64 host, "Install
  from file" accepts an Unfolded Circle `.tar.gz` release: it reads `driver.json`,
  detects the binary's architecture, and runs it in a container. The button is hidden
  on non-ARM64 hosts and the API refuses cross-arch archives, because these releases
  are native ARM64 binaries — under emulation their mDNS sockets fail (`OSError 92`)
  and they crash-loop. On x86 hosts, use a source/GHCR install instead.
- **Activity log** (≣) records installs, registrations, removals, adoptions and alerts.
- **Maintenance** (⛭): download a **backup** of all config + state, **restore** it,
  **reconcile** (adopt managed containers missing from the list after a reboot or
  manual `docker` action), and **prune** build images no longer used by any instance.
- **Port-conflict guard**: installs/reconfigures refuse a port already used by another
  instance or bound on the host, and auto-assign a free one otherwise.
- **Registration confirmation**: after registering, the installer polls the remote
  until the driver actually appears/connects and reports the result.
- **Live logs**: the Logs dialog can **Follow** (server-sent events stream) and
  **Download** the full log. Installs are **serialized** so concurrent builds don't clash.
- **Notifications**: configure a webhook in **Maintenance → Notifications** (ntfy URL
  or any JSON webhook) and tick which events to be notified about — installs, updates
  available, registrations, health alerts, removals, backup/maintenance, and errors.
  A **Send test** button verifies it. `UC_INSTALLER_ALERT_WEBHOOK` still works as a
  default if you'd rather set it via environment.

## Security

This service controls Docker, which is effectively root on the host. **No token is
set by default**, so anyone who can reach the port can install and run containers.
On any shared or exposed host, set `UC_INSTALLER_TOKEN` and, ideally, keep the port
on a trusted network or behind a reverse proxy with TLS.

## How integrations run

Each integration becomes one container:

- name = the integration id, labelled `uc.installer=managed`
- `network_mode: host`, `restart: unless-stopped`
- config persisted at `UC_INSTALLER_DATA/config/<id>` mounted to `/config`
- base env: `UC_CONFIG_HOME`, `UC_INTEGRATION_INTERFACE`, `UC_INTEGRATION_HTTP_PORT`,
  `UC_DISABLE_MDNS_PUBLISH`, `PYTHONUNBUFFERED` (plus your overrides)

Installing resolves an image in this order: pull a prebuilt GHCR image; else build
from source. Source builds clone the repo to `UC_INSTALLER_DATA/apps/<id>` and:

1. use the repo's own `Dockerfile` if it ships one;
2. else use a tuned build for a known language (below);
3. else — or if a tuned build fails — build with **Nixpacks**, a universal builder
   that auto-detects the language (Node, Python, Go, Rust, .NET, Java, PHP, Ruby,
   Deno, and more) and produces a runnable image. Install it with `install.sh` (or
   `curl -fsSL https://nixpacks.com/install.sh | sudo bash`).

Tuned language builds:

| Detected from | Stack | Build & start |
| --- | --- | --- |
| `package.json` | Node / TypeScript | `npm ci` + `npm run build` (if present), start via `npm start` / main / `dist/index.js` |
| `*.csproj` / `*.sln` | .NET / C# | SDK image auto-matched to the project's target framework, `dotnet publish -c Release`, run the DLL |
| `Cargo.toml` | Rust | `cargo build --release`, run the produced binary |
| `go.mod` | Go | `go build`, run the binary |
| `requirements.txt` / `pyproject.toml` / `*.py` | Python | install deps, run the detected entrypoint |

With Nixpacks installed, an integration in any language can be built from source
when no prebuilt image exists. Without it, the five tuned stacks are still covered;
anything else fails with a clear message. The build method/stack is shown on the
installed row.

Four first-party entries in the registry have no public repo; the UI shows them but
marks them as not installable here.

## Files

```text
uc-external-integration-installer/
├── uc_installer.py                    # FastAPI service + Docker control + jobs
├── static/index.html                # web UI (single file, no build step)
├── requirements.txt
├── install.sh                       # venv + systemd bootstrap
├── uc-external-integration-installer.service   # systemd unit
└── README.md
```

## API (for automation)

`GET /api/health` · `GET /api/registry` · `GET /api/installed` ·
`POST /api/integrations/{id}/install` · `POST /api/integrations/{id}/config` ·
`POST /api/integrations/{id}/{start|stop|restart}` ·
`DELETE /api/integrations/{id}?purge=bool` ·
`GET /api/integrations/{id}/logs?tail=N` · `GET /api/jobs/{job_id}` ·
`GET /api/update/status` · `POST /api/update/apply` ·
`GET /api/integrations/{id}/versions` · `GET /api/updates` · `GET /api/registrations` ·
`GET /api/stats` ·
`POST /api/integrations/{id}/add-instance` ·
`POST /api/instances/{iid}/{start|stop|restart|config|rebuild}` ·
`DELETE /api/instances/{iid}` · `GET /api/instances/{iid}/logs[/stream]` ·
`GET /api/events` · `GET /api/backup` · `POST /api/restore` · `POST /api/install-archive` ·
`POST /api/maintenance/{prune|reconcile}` ·
`GET/PUT /api/settings/alerts` · `POST /api/settings/alerts/test` ·
`GET/POST /api/remotes` · `PUT/DELETE /api/remotes/{id}` ·
`POST /api/remotes/{id}/test` · `POST /api/remotes/{id}/register` ·
`GET /api/remotes/{id}/drivers` · `DELETE /api/remotes/{id}/drivers/{driver_id}`

If a token is configured, send `Authorization: Bearer <token>`.
