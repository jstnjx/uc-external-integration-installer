# UC External Integration Installer

A self-hosted web application for installing, configuring and managing **Unfolded Circle external integrations**

The installer provides a modern web interface for browsing the community registry, managing integration instances, registering drivers on one or more UC remotes, monitoring system health, viewing logs, and maintaining the installer itself.

---

## Features

- Browse and search the community integration registry
- One-click installation using prebuilt GHCR images or automatic source builds
- Multiple instances per integration
- Live integration management
  - Start
  - Stop
  - Restart
  - Rebuild
  - Configure
  - Remove
- Per-integration update policies
- Driver registration on multiple UC remotes
- Live Docker runtime statistics
- Health monitoring
  - Host system
  - Docker containers
  - UC remotes
  - Installer service
- Integrated logs
  - Installer logs
  - Integration logs
  - Filtering
  - Live streaming
  - Saved views
- Activity history
- Persistent operations drawer
- Backup & restore
- Docker reconciliation & cleanup
- Diagnostics page
- Discord, ntfy, Slack and generic webhook notifications
- Automatic installer updates
- Fully responsive interface

---

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/jstnjx/uc-external-integration-installer/main/install.sh | sudo bash
```

Once installed, open

```
http://<host-ip>:8900
```

and complete the first-time setup wizard.

---

## Updating

The installer can update itself directly from the web interface.

Alternatively, simply rerun the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/jstnjx/uc-external-integration-installer/main/install.sh | sudo bash
```

Installed integrations remain untouched.

---

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/jstnjx/uc-external-integration-installer/main/uninstall.sh | sudo CONFIRM=1 bash
```

---

## Web Interface

### Installed

Manage installed integrations.

- Runtime statistics
- Version management
- Driver registration
- Bulk actions
- Configuration
- Logs
- Health information

### Browse

Browse and install integrations from the community registry.

### Health

View live information about:

- Host system
- Managed Docker containers
- UC remotes
- Installer service

### Remotes

Manage one or more Unfolded Circle remotes.

- Connection testing
- Driver management
- Registration management
- Remote health

### Logs

View installer and integration logs with:

- Live streaming
- Filtering
- Search
- Saved views

### Activity

History of installer events including:

- Installs
- Updates
- Registrations
- Maintenance
- Configuration changes

### Settings

Configure:

- Registry
- Updates
- Notifications
- Authentication
- Docker maintenance
- Backup & restore
- Runtime behaviour

---

## Notifications

Supports:

- Discord
- ntfy
- Slack
- Generic webhooks

Notifications include rich information about installs, updates, registrations, health alerts, maintenance operations and failures.

---

## Health Monitoring

The installer continuously monitors:

- Docker containers
- Integration runtime
- Host resources
- Remote connectivity
- Installer service

---

## Security

Optional bearer-token authentication can be enabled.

For internet-facing deployments, it is recommended to place the installer behind a reverse proxy with HTTPS.

---

## Project Structure

```
uc-external-integration-installer/
├── uc_installer.py
├── install.sh
├── uninstall.sh
├── requirements.txt
├── static/
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   ├── api.js
│   ├── installed.js
│   ├── remotes.js
│   ├── logs.js
│   ├── settings.js
│   ├── operations.js
│   ├── navigation.js
│   ├── dialogs.js
│   ├── health.js
│   └── favicon.svg
└── README.md
```

---

## REST API

The installer exposes a REST API used by the web interface and for automation.

If authentication is enabled, include:

```http
Authorization: Bearer <token>
```

### Health & Diagnostics

| Method | Endpoint | Description |
|---------|----------|-------------|
| GET | `/api/health` | Basic installer health |
| GET | `/api/health/overview` | Complete system health overview |
| GET | `/api/diagnostics` | Full diagnostics report |
| GET | `/api/stats` | Runtime statistics |

---

### Registry

| Method | Endpoint |
|---------|----------|
| GET | `/api/registry` |
| GET | `/api/updates` |

---

### Installed Integrations

| Method | Endpoint |
|---------|----------|
| GET | `/api/installed` |
| POST | `/api/integrations/{id}/install` |
| POST | `/api/integrations/{id}/config` |
| POST | `/api/integrations/{id}/add-instance` |
| GET | `/api/integrations/{id}/versions` |
| GET | `/api/integrations/{id}/logs` |
| GET | `/api/integrations/{id}/logs/stream` |
| POST | `/api/integrations/{id}/start` |
| POST | `/api/integrations/{id}/stop` |
| POST | `/api/integrations/{id}/restart` |
| DELETE | `/api/integrations/{id}` |

---

### Integration Instances

| Method | Endpoint |
|---------|----------|
| POST | `/api/instances/{id}/start` |
| POST | `/api/instances/{id}/stop` |
| POST | `/api/instances/{id}/restart` |
| POST | `/api/instances/{id}/config` |
| POST | `/api/instances/{id}/rebuild` |
| POST | `/api/instances/{id}/auto-update` |
| GET | `/api/instances/{id}/logs` |
| GET | `/api/instances/{id}/logs/stream` |
| DELETE | `/api/instances/{id}` |

---

### Remotes

| Method | Endpoint |
|---------|----------|
| GET | `/api/remotes` |
| POST | `/api/remotes` |
| PUT | `/api/remotes/{id}` |
| DELETE | `/api/remotes/{id}` |
| POST | `/api/remotes/{id}/test` |
| GET | `/api/remotes/{id}/drivers` |
| DELETE | `/api/remotes/{id}/drivers/{driver_id}` |
| POST | `/api/remotes/{id}/register` |
| POST | `/api/remotes/{id}/register/preflight` |

---

### Registration

| Method | Endpoint |
|---------|----------|
| GET | `/api/registrations` |

---

### Logs

| Method | Endpoint |
|---------|----------|
| GET | `/api/installer/logs` |
| GET | `/api/installer/logs/stream` |

---

### Activity & Operations

| Method | Endpoint |
|---------|----------|
| GET | `/api/events` |
| GET | `/api/jobs/{job_id}` |
| GET | `/api/operations` |

---

### Maintenance

| Method | Endpoint |
|---------|----------|
| POST | `/api/maintenance/reconcile` |
| POST | `/api/maintenance/prune` |

---

### Backup & Restore

| Method | Endpoint |
|---------|----------|
| GET | `/api/backup` |
| POST | `/api/restore` |

---

### Settings

| Method | Endpoint |
|---------|----------|
| GET | `/api/settings` |
| PUT | `/api/settings` |
| GET | `/api/settings/alerts` |
| PUT | `/api/settings/alerts` |
| POST | `/api/settings/alerts/test` |
| GET | `/api/settings/export` |
| POST | `/api/settings/import` |

---

### Updates

| Method | Endpoint |
|---------|----------|
| GET | `/api/update/status` |
| POST | `/api/update/apply` |
| POST | `/api/update/restart` |

---

### Archive Installation

| Method | Endpoint |
|---------|----------|
| POST | `/api/install-archive` |

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.