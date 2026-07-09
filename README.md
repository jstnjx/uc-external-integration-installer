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

Remote credentials are saved in `UC_INSTALLER_DATA/remotes.json` (file mode `600`,
plaintext). Keep the data directory private; prefer a per-remote PIN/API key you
can revoke over reusing sensitive credentials.

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

Source builds clone the repo to `UC_INSTALLER_DATA/apps/<id>`, add a generic
`Dockerfile.external`, detect the Python entrypoint, and build `uc-local/<id>:latest`.

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
`GET/POST /api/remotes` · `PUT/DELETE /api/remotes/{id}` ·
`POST /api/remotes/{id}/test` · `POST /api/remotes/{id}/register` ·
`GET /api/remotes/{id}/drivers` · `DELETE /api/remotes/{id}/drivers/{driver_id}`

If a token is configured, send `Authorization: Bearer <token>`.
