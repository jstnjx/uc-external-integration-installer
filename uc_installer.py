#!/usr/bin/env python3
"""
UC External Integration Installer
======================

A small self-hosted service that runs in the background on a Docker host and
exposes a web UI to browse, install, configure and manage Unfolded Circle
external integrations.

It replaces the old shell installer:
  - fetches the community registry
  - lets you pick integrations from a modern web UI
  - prefers a GHCR image, falls back to building from source
  - runs each integration as a labelled Docker container (host networking)
  - lets you start / stop / restart / reconfigure / remove them and read logs

Run directly:
    python uc_installer.py

Or via uvicorn:
    uvicorn uc_installer:app --host 0.0.0.0 --port 8900

Configuration (environment variables):
    UC_INSTALLER_HOST     bind address for the web UI      (default 0.0.0.0)
    UC_INSTALLER_PORT     bind port for the web UI         (default 8900)
    UC_INSTALLER_DATA     data directory                   (default /var/lib/uc-external-integration-installer,
                                                          falls back to ~/.local/share/...)
    UC_INSTALLER_TOKEN    optional bearer token for auth   (default: none = open)
    UC_PORT_START       first integration port           (default 8000)
    UC_REGISTRY_URL     registry JSON url
"""
from __future__ import annotations

import base64
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REGISTRY_URL = os.environ.get(
    "UC_REGISTRY_URL",
    "https://raw.githubusercontent.com/JackJPowell/uc-intg-list/refs/heads/main/registry.json",
)
PORT_START = int(os.environ.get("UC_PORT_START", "8000"))
TOKEN = os.environ.get("UC_INSTALLER_TOKEN", "").strip()

# Self-update: the installed code is a git checkout of this repo. Updating is a
# fetch + hard reset to the tip of the branch, a dependency refresh, and a
# service restart.
UPDATE_REPO = os.environ.get(
    "UC_INSTALLER_UPDATE_REPO",
    "https://github.com/jstnjx/uc-external-integration-installer",
)
UPDATE_BRANCH = os.environ.get("UC_INSTALLER_UPDATE_BRANCH", "main")
SERVICE_UNIT = os.environ.get("UC_INSTALLER_SERVICE", "uc-external-integration-installer")

# Container labels used to identify things this installer owns.
LABEL_MANAGED = "uc.installer"
LABEL_ID = "uc.integration.id"
LABEL_INSTANCE = "uc.instance.id"
LABEL_ORDINAL = "uc.instance.ordinal"
LABEL_NAME = "uc.integration.name"
LABEL_SOURCE = "uc.integration.source"  # "ghcr" or "build"
LABEL_PORT = "uc.integration.port"

ALERT_WEBHOOK = os.environ.get("UC_INSTALLER_ALERT_WEBHOOK", "").strip()

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
APP_DIR = HERE  # the installed code directory / git checkout root


def _resolve_data_dir() -> Path:
    candidate = Path(os.environ.get("UC_INSTALLER_DATA", "/var/lib/uc-external-integration-installer"))
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        # confirm writable
        probe = candidate / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return candidate
    except Exception:
        fallback = Path.home() / ".local" / "share" / "uc-external-integration-installer"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _resolve_data_dir()
APPS_DIR = DATA_DIR / "apps"
CONFIG_DIR = DATA_DIR / "config"
STATE_FILE = DATA_DIR / "state.json"
EVENTS_FILE = DATA_DIR / "events.jsonl"
REGISTRY_CACHE = DATA_DIR / "registry.json"
for d in (APPS_DIR, CONFIG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Serializes source builds/installs (the "install queue") so concurrent installs
# don't clobber the shared clone/build directories.
_install_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

_events_lock = threading.Lock()


def record_event(kind: str, instance_id: str | None, message: str) -> None:
    """Append an event to the log (kept for the UI activity feed + alerts)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,               # install | remove | register | update | state | error | alert
        "instance_id": instance_id,
        "message": message,
    }
    try:
        with _events_lock:
            with EVENTS_FILE.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
            # trim to the last ~1000 lines occasionally
            if EVENTS_FILE.stat().st_size > 500_000:
                lines = EVENTS_FILE.read_text().splitlines()[-1000:]
                EVENTS_FILE.write_text("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass
    category = _KIND_TO_CATEGORY.get(kind)
    if category:
        try:
            notify(category, message)
        except Exception:  # noqa: BLE001
            pass


def load_events(limit: int = 200) -> list[dict[str, Any]]:
    if not EVENTS_FILE.exists():
        return []
    try:
        lines = EVENTS_FILE.read_text().splitlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:  # noqa: BLE001
                continue
        out.reverse()  # newest first
        return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Notifications (GUI-configurable webhook + per-category toggles)
# ---------------------------------------------------------------------------

NOTIFY_CATEGORIES = ["install", "update", "register", "health", "remove", "maintenance", "error"]
# event kind -> notification category (kinds not listed never notify, e.g. lifecycle)
_KIND_TO_CATEGORY = {
    "install": "install", "update": "update", "register": "register",
    "alert": "health", "remove": "remove", "state": "maintenance", "error": "error",
}
DEFAULT_ALERT_EVENTS = {c: c in ("update", "health", "error") for c in NOTIFY_CATEGORIES}
ALERTS_FILE = DATA_DIR / "alerts.json"
_alerts_lock = threading.Lock()
_alerts_cache: dict[str, Any] | None = None


def load_alert_settings() -> dict[str, Any]:
    global _alerts_cache
    if _alerts_cache is not None:
        return _alerts_cache
    data: dict[str, Any] = {"webhook": "", "events": dict(DEFAULT_ALERT_EVENTS)}
    try:
        if ALERTS_FILE.exists():
            saved = json.loads(ALERTS_FILE.read_text())
            if isinstance(saved.get("webhook"), str):
                data["webhook"] = saved["webhook"].strip()
            ev = saved.get("events") or {}
            for c in NOTIFY_CATEGORIES:
                if c in ev:
                    data["events"][c] = bool(ev[c])
    except Exception:  # noqa: BLE001
        pass
    if not data["webhook"] and ALERT_WEBHOOK:
        data["webhook"] = ALERT_WEBHOOK  # environment fallback
    _alerts_cache = data
    return data


def save_alert_settings(webhook: str, events: dict[str, Any]) -> dict[str, Any]:
    global _alerts_cache
    clean = {
        "webhook": (webhook or "").strip(),
        "events": {c: bool(events.get(c, False)) for c in NOTIFY_CATEGORIES},
    }
    with _alerts_lock:
        ALERTS_FILE.write_text(json.dumps(clean, indent=2))
    _alerts_cache = clean
    return clean


def _fire_webhook(message: str, title: str = "UC External Integration Installer") -> bool:
    url = load_alert_settings()["webhook"]
    if not url:
        return False
    low = url.lower()
    try:
        if "discord.com/api/webhooks" in low or "discordapp.com/api/webhooks" in low:
            # Discord requires a "content" field (max 2000 chars); 204 on success.
            r = httpx.post(url, json={"content": f"**{title}**\n{message}"[:1900]}, timeout=10.0)
        elif "ntfy" in low:
            r = httpx.post(url, content=message.encode("utf-8"),
                           headers={"Title": title, "Tags": "gear"}, timeout=10.0)
        else:
            # generic: include the field names Slack/Discord/most webhooks look for
            r = httpx.post(url, json={"title": title, "text": message,
                                      "message": message, "content": message}, timeout=10.0)
        return getattr(r, "status_code", 200) < 400
    except Exception:  # noqa: BLE001
        return False


def notify(category: str, message: str) -> None:
    s = load_alert_settings()
    if not s["webhook"] or not s["events"].get(category, False):
        return
    threading.Thread(target=_fire_webhook, args=(message,), daemon=True).start()


# ---------------------------------------------------------------------------
# General settings (UI-configurable; first-run flag)
# ---------------------------------------------------------------------------

SETTINGS_FILE = DATA_DIR / "settings.json"
_settings_lock = threading.Lock()
_settings_cache: dict[str, Any] | None = None

_STR_SETTINGS = ("registry_url", "update_repo", "update_branch", "update_service", "token")


def _env_health_probe_default() -> bool:
    return os.environ.get("UC_INSTALLER_HEALTH_PROBE", "1") != "0"


def _settings_defaults() -> dict[str, Any]:
    return {
        "setup_complete": False,
        "port_start": PORT_START,
        "registry_url": REGISTRY_URL,
        "update_repo": UPDATE_REPO,
        "update_branch": UPDATE_BRANCH,
        "update_service": SERVICE_UNIT,
        "health_probe": _env_health_probe_default(),
        "token": TOKEN,
    }


def load_settings() -> dict[str, Any]:
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    data = _settings_defaults()
    try:
        if SETTINGS_FILE.exists():
            saved = json.loads(SETTINGS_FILE.read_text())
            for k in _STR_SETTINGS:
                if isinstance(saved.get(k), str) and saved.get(k) != "":
                    data[k] = saved[k]
            if saved.get("port_start") not in (None, ""):
                data["port_start"] = saved["port_start"]
            if "health_probe" in saved:
                data["health_probe"] = bool(saved["health_probe"])
            # token may legitimately be cleared back to empty
            if "token" in saved and isinstance(saved["token"], str):
                data["token"] = saved["token"]
            data["setup_complete"] = bool(saved.get("setup_complete", False))
    except Exception:  # noqa: BLE001
        pass
    try:
        data["port_start"] = int(data["port_start"])
    except (TypeError, ValueError):
        data["port_start"] = PORT_START
    _settings_cache = data
    return data


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    global _settings_cache
    cur = dict(load_settings())
    for k in _STR_SETTINGS:
        if k in patch and patch[k] is not None:
            cur[k] = str(patch[k]).strip()
    if patch.get("port_start") is not None:
        cur["port_start"] = patch["port_start"]
    if "health_probe" in patch and patch["health_probe"] is not None:
        cur["health_probe"] = bool(patch["health_probe"])
    if "setup_complete" in patch and patch["setup_complete"] is not None:
        cur["setup_complete"] = bool(patch["setup_complete"])
    try:
        cur["port_start"] = int(cur["port_start"])
    except (TypeError, ValueError):
        cur["port_start"] = PORT_START
    cur["setup_complete"] = bool(cur.get("setup_complete", False))
    with _settings_lock:
        SETTINGS_FILE.write_text(json.dumps(cur, indent=2))
    _settings_cache = cur
    return cur


def get_port_start() -> int:
    return int(load_settings().get("port_start") or PORT_START)


def registry_url_val() -> str:
    return load_settings().get("registry_url") or REGISTRY_URL


def update_repo() -> str:
    return load_settings().get("update_repo") or UPDATE_REPO


def update_branch() -> str:
    return load_settings().get("update_branch") or UPDATE_BRANCH


def service_unit_val() -> str:
    return load_settings().get("update_service") or SERVICE_UNIT


def health_probe_on() -> bool:
    return bool(load_settings().get("health_probe", True))


def token_val() -> str:
    return (load_settings().get("token") or TOKEN or "").strip()


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"integrations": {}}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def record_integration(record: dict[str, Any]) -> None:
    with _state_lock:
        state = load_state()
        state["integrations"][record["id"]] = record
        save_state(state)


def forget_integration(integration_id: str) -> None:
    with _state_lock:
        state = load_state()
        state["integrations"].pop(integration_id, None)
        save_state(state)


# ---------------------------------------------------------------------------
# Remotes: multi-remote registry with saved credentials
# ---------------------------------------------------------------------------
#
# Each remote talks the Unfolded Circle Core-API. Registering an external
# integration is POST /api/intg/drivers with the driver metadata and its
# ws:// url, authenticated as web-configurator:<PIN> (HTTP Basic) or with an
# API key (Bearer). Credentials are stored on disk — see the README security note.

REMOTES_FILE = DATA_DIR / "remotes.json"
_remotes_lock = threading.Lock()


def load_remotes() -> dict[str, Any]:
    if REMOTES_FILE.exists():
        try:
            return json.loads(REMOTES_FILE.read_text())
        except Exception:
            pass
    return {"remotes": {}, "active": None}


def save_remotes(data: dict[str, Any]) -> None:
    tmp = REMOTES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(REMOTES_FILE)
    try:
        os.chmod(REMOTES_FILE, 0o600)  # credentials live here
    except OSError:
        pass


def _mask(remote: dict[str, Any]) -> dict[str, Any]:
    """Public view of a remote — never leak the PIN or API key."""
    return {
        "id": remote["id"],
        "name": remote.get("name"),
        "scheme": remote.get("scheme", "http"),
        "host": remote.get("host"),
        "port": remote.get("port"),
        "username": remote.get("username", "web-configurator"),
        "has_pin": bool(remote.get("pin")),
        "has_api_key": bool(remote.get("api_key")),
        "verify_tls": remote.get("verify_tls", False),
        "advertise_ip": remote.get("advertise_ip", ""),
    }


def parse_address(addr: str) -> tuple[str, str, int]:
    addr = (addr or "").strip()
    if not re.match(r"^https?://", addr):
        addr = "http://" + addr
    u = urllib.parse.urlparse(addr)
    scheme = u.scheme or "http"
    host = u.hostname or ""
    port = u.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def detect_host_ip(target_host: str) -> str | None:
    """Local IP on the interface that routes to the remote (for driver_url)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect((target_host, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def remote_request(remote: dict[str, Any], method: str, path: str,
                   json_body: Any = None, timeout: float = 15.0) -> httpx.Response:
    base = f"{remote.get('scheme', 'http')}://{remote['host']}:{remote['port']}/api"
    headers = {}
    auth = None
    if remote.get("api_key"):
        headers["Authorization"] = f"Bearer {remote['api_key']}"
    else:
        auth = httpx.BasicAuth(
            remote.get("username") or "web-configurator", remote.get("pin", "")
        )
    return httpx.request(
        method, base + path, json=json_body, auth=auth, headers=headers,
        timeout=timeout, verify=remote.get("verify_tls", False),
    )


def remote_all_drivers(remote: dict[str, Any], driver_type: str | None = None,
                       timeout: float = 10.0) -> list[dict[str, Any]]:
    """Fetch the remote's full driver list.

    GET /intg/drivers defaults to limit=10, so an unpaginated call only returns
    the first 10 (which are the pre-installed LOCAL drivers) and hides EXTERNAL
    drivers we register. Page through with the max limit until we've collected
    Pagination-Count items.
    """
    out: list[dict[str, Any]] = []
    page = 1
    while page <= 30:  # hard safety cap
        path = f"/intg/drivers?limit=100&page={page}"
        if driver_type:
            path += f"&driver_type={driver_type}"
        r = remote_request(remote, "GET", path, timeout=timeout)
        if r.status_code >= 400:
            break
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            break
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        try:
            total = int(r.headers.get("Pagination-Count", len(out)))
        except (TypeError, ValueError):
            total = len(out)
        if len(out) >= total:
            break
        page += 1
    return out


def build_driver_payload(entry: dict[str, Any], rec: dict[str, Any],
                         advertise_ip: str, port: int) -> dict[str, Any]:
    # instance-specific driver_id/name so multiple instances register distinctly
    driver_id = rec.get("driver_id") or entry.get("driver_id") or rec.get("id") or entry.get("id")
    name = rec.get("label") or entry.get("name") or rec.get("name") or driver_id
    payload: dict[str, Any] = {
        "driver_id": driver_id,
        "name": {"en": name},
        "version": rec.get("version") or entry.get("version") or "1.0.0",
        "driver_url": f"ws://{advertise_ip}:{port}",
    }
    if entry.get("description"):
        payload["description"] = {"en": entry["description"]}
    if entry.get("author"):
        payload["developer"] = {"name": entry["author"]}
    return payload


# ---------------------------------------------------------------------------
# Registry client
# ---------------------------------------------------------------------------

_registry_cache: dict[str, Any] | None = None
_registry_lock = threading.Lock()


def fetch_registry(force: bool = False) -> dict[str, Any]:
    global _registry_cache
    with _registry_lock:
        if _registry_cache is not None and not force:
            return _registry_cache
        try:
            resp = httpx.get(registry_url_val(), timeout=20.0, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
            REGISTRY_CACHE.write_text(json.dumps(data))
        except Exception:
            if REGISTRY_CACHE.exists():
                data = json.loads(REGISTRY_CACHE.read_text())
            else:
                raise
        _registry_cache = data
        return data


def find_integration(integration_id: str) -> dict[str, Any] | None:
    data = fetch_registry()
    for it in data.get("integrations", []):
        if it.get("id") == integration_id:
            return it
    return None


# The registry is served from a GitHub repo; show which commit of that repo we're
# looking at. Resolved from the raw URL and cached; refreshed in the background so
# it never adds latency to /api/health (which gets pinged during self-updates).
_reg_commit = {"sha": None, "ts": 0.0, "refreshing": False}
_reg_commit_lock = threading.Lock()


def _registry_repo_ref() -> tuple[str, str, str] | None:
    m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/(.+)", registry_url_val())
    if not m:
        return None
    owner, repo, rest = m.group(1), m.group(2), m.group(3)
    parts = rest.split("/")
    branch = parts[2] if parts[:2] == ["refs", "heads"] and len(parts) > 2 else parts[0]
    return owner, repo, branch


def _refresh_registry_commit() -> None:
    try:
        ref = _registry_repo_ref()
        if ref:
            owner, repo, branch = ref
            resp = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}",
                headers={"User-Agent": "uc-external-integration-installer",
                         "Accept": "application/vnd.github+json"},
                timeout=10.0, follow_redirects=True,
            )
            resp.raise_for_status()
            _reg_commit["sha"] = resp.json()["sha"][:7]
    except Exception:
        pass  # keep last known value
    finally:
        _reg_commit["ts"] = time.time()
        _reg_commit["refreshing"] = False


def registry_commit() -> str | None:
    """Return the cached registry-repo commit; kick off an async refresh if stale."""
    with _reg_commit_lock:
        stale = time.time() - _reg_commit["ts"] > 900
        if stale and not _reg_commit["refreshing"]:
            _reg_commit["refreshing"] = True
            threading.Thread(target=_refresh_registry_commit, daemon=True).start()
    return _reg_commit["sha"]


PLACEHOLDER_REPO = "https://github.com/unfoldedcircle/"


def is_installable(entry: dict[str, Any]) -> bool:
    # Official (first-party) integrations are meant to run on the remote itself,
    # not as external containers — never installable here.
    if entry.get("official"):
        return False
    repo = (entry.get("repository") or "").strip()
    return bool(repo) and repo != PLACEHOLDER_REPO and repo.startswith("http")


def image_from_repo(repo: str, tag: str = "latest") -> str:
    """ghcr.io/<owner>/<name>:<tag> derived from a GitHub repo url."""
    r = repo.strip()
    r = re.sub(r"^https?://github\.com/", "", r)
    r = re.sub(r"\.git$", "", r)
    return f"ghcr.io/{r.lower()}:{tag or 'latest'}"


def owner_repo(repo: str) -> tuple[str, str]:
    r = re.sub(r"^https?://github\.com/", "", repo.strip())
    r = re.sub(r"\.git$", "", r)
    parts = r.split("/")
    return (parts[0], parts[1]) if len(parts) >= 2 else ("", r)


# ---- integration versions (GitHub releases / tags) -------------------------

_versions_cache: dict[str, dict[str, Any]] = {}
_versions_lock = threading.Lock()
_VERSIONS_TTL = 3600  # 1 hour — GitHub's unauthenticated API is rate-limited


def _gh(url: str) -> Any:
    resp = httpx.get(
        url,
        headers={"User-Agent": "uc-external-integration-installer",
                 "Accept": "application/vnd.github+json"},
        timeout=12.0, follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_repo_versions(owner: str, repo: str) -> list[dict[str, Any]]:
    """Newest-first list of {tag, published_at, prerelease}. Releases, else tags."""
    try:
        releases = _gh(f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=30")
        out = [
            {"tag": r["tag_name"], "published_at": r.get("published_at"),
             "prerelease": r.get("prerelease", False)}
            for r in releases if r.get("tag_name")
        ]
        if out:
            return out
    except Exception:
        pass
    try:
        tags = _gh(f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=30")
        return [{"tag": t["name"], "published_at": None, "prerelease": False}
                for t in tags if t.get("name")]
    except Exception:
        return []


def repo_versions(repo: str) -> list[dict[str, Any]]:
    owner, name = owner_repo(repo)
    if not owner or not name:
        return []
    key = f"{owner}/{name}"
    now = time.time()
    with _versions_lock:
        cached = _versions_cache.get(key)
        if cached and now - cached["ts"] < _VERSIONS_TTL:
            return cached["items"]
    items = _fetch_repo_versions(owner, name)
    with _versions_lock:
        _versions_cache[key] = {"ts": now, "items": items}
    return items


def _vkey(tag: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def version_gt(a: str, b: str) -> bool:
    return _vkey(a) > _vkey(b)


def compute_update(rec: dict[str, Any]) -> dict[str, Any]:
    """Given an installed record, determine if a newer version exists."""
    repo = rec.get("repository") or ""
    installed = rec.get("version") or "latest"
    versions = repo_versions(repo)
    stable = [v for v in versions if not v.get("prerelease")] or versions
    latest_tag = stable[0]["tag"] if stable else None

    available = False
    if latest_tag and installed not in ("latest", "", None):
        # A specific version is pinned — flag if a newer release exists.
        # "latest" installs rebuild to newest on demand, so they never "need" an update.
        available = version_gt(latest_tag, installed)
    return {
        "installed_version": installed,
        "latest_version": latest_tag,
        "update_available": available,
    }


# ---------------------------------------------------------------------------
# Docker layer (lazy — the service still starts without a running daemon)
# ---------------------------------------------------------------------------

_docker_client = None
_docker_lock = threading.Lock()


def get_docker():
    global _docker_client
    with _docker_lock:
        if _docker_client is None:
            import docker  # imported lazily so the app starts without docker

            _docker_client = docker.from_env()
        return _docker_client


def docker_available() -> bool:
    try:
        get_docker().ping()
        return True
    except Exception:
        return False


def _container_for(integration_id: str):
    import docker

    try:
        return get_docker().containers.get(integration_id)
    except docker.errors.NotFound:
        return None


def base_environment(port: int, entrypoint: str | None) -> dict[str, str]:
    env = {
        "UC_CONFIG_HOME": "/config",
        "UC_INTEGRATION_INTERFACE": "0.0.0.0",
        "UC_INTEGRATION_HTTP_PORT": str(port),
        "UC_DISABLE_MDNS_PUBLISH": "false",
        "PYTHONUNBUFFERED": "1",
    }
    if entrypoint:
        # Only set for the generic source build; makes package-relative imports
        # resolve regardless of where the entrypoint file lives in the tree.
        env["UC_ENTRYPOINT"] = entrypoint
        env["PYTHONPATH"] = "/app"
    return env


def _host_port_bound(port: int) -> bool:
    """True if something on the host is already bound to the TCP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _port_free(port: int, exclude: str | None = None) -> bool:
    """Free = not claimed by another managed instance and not bound on the host."""
    for iid, rec in load_state()["integrations"].items():
        if iid != exclude and str(rec.get("port")) == str(port):
            return False
    return not _host_port_bound(port)


def next_free_port(exclude: str | None = None) -> int:
    port = get_port_start()
    while not _port_free(port, exclude=exclude):
        port += 1
    return port


def resolve_port(requested: int | None, instance_id: str | None = None) -> int:
    """Validate a requested port or auto-assign a free one."""
    if requested:
        if not _port_free(int(requested), exclude=instance_id):
            raise HTTPException(409, f"Port {requested} is already in use — pick another or leave it blank to auto-assign.")
        return int(requested)
    return next_free_port(exclude=instance_id)


# ---- instances -------------------------------------------------------------


def next_instance_id(integration_id: str) -> tuple[str, int]:
    """(instance_id, ordinal) for a NEW instance. First instance reuses the
    integration id (backward compatible); extras get an -iN suffix."""
    state = load_state()["integrations"]
    if integration_id not in state:
        return integration_id, 1
    n = 2
    while f"{integration_id}-i{n}" in state:
        n += 1
    return f"{integration_id}-i{n}", n


def instance_driver_id(base: str, ordinal: int) -> str:
    return base if ordinal <= 1 else f"{base}_{ordinal}"


def instance_label(name: str, ordinal: int) -> str:
    return name if ordinal <= 1 else f"{name} #{ordinal}"


def integration_instances(integration_id: str) -> list[str]:
    return [iid for iid, rec in load_state()["integrations"].items()
            if rec.get("integration_id", iid) == integration_id]


def reconcile_state() -> None:
    """On startup, adopt managed containers missing from state (e.g. after a
    manual docker action) so the UI reflects reality."""
    try:
        client = get_docker()
        containers = client.containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=managed"})
    except Exception:  # noqa: BLE001
        return
    state = load_state()
    changed = False
    for c in containers:
        instance_id = c.name
        if instance_id in state["integrations"]:
            continue
        labels = c.attrs.get("Config", {}).get("Labels", {}) or {}
        integration_id = labels.get(LABEL_ID, instance_id)
        try:
            ordinal = int(labels.get(LABEL_ORDINAL, "1"))
        except ValueError:
            ordinal = 1
        try:
            port = int(labels.get(LABEL_PORT, "0"))
        except ValueError:
            port = 0
        name = labels.get(LABEL_NAME, integration_id)
        image = (c.image.tags or [""])[0] if c.image else ""
        state["integrations"][instance_id] = {
            "instance_id": instance_id, "id": instance_id, "integration_id": integration_id,
            "instance": ordinal, "name": name, "label": instance_label(name, ordinal),
            "driver_id": instance_driver_id(
                (find_integration(integration_id) or {}).get("driver_id") or integration_id, ordinal),
            "repository": (find_integration(integration_id) or {}).get("repository", ""),
            "image": image, "source": labels.get(LABEL_SOURCE, "unknown"), "port": port,
            "env": {}, "entrypoint": "", "version": "latest", "stack": None,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "adopted": True,
        }
        changed = True
        record_event("state", instance_id, f"Adopted orphaned container '{instance_id}'")
    if changed:
        save_state(state)


# ---- source build helpers --------------------------------------------------

GENERIC_DOCKERFILE = """FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git gcc libc6-dev libffi-dev libssl-dev \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
RUN if [ -f pyproject.toml ]; then pip install .; fi

COPY docker-entry.external.sh /usr/local/bin/docker-entry.external.sh
RUN chmod +x /usr/local/bin/docker-entry.external.sh

CMD ["/usr/local/bin/docker-entry.external.sh"]
"""

GENERIC_ENTRY = """#!/usr/bin/env sh
set -eu
cd /app
export PYTHONPATH="/app:${PYTHONPATH:-}"

# Run a python file the right way: if it sits inside a package (its directory has
# an __init__.py), import it as a module so relative imports work; otherwise run
# the file directly.
run_py() {
  f="$1"
  dir="$(dirname "$f")"
  if [ "$dir" != "." ] && [ -f "$dir/__init__.py" ]; then
    mod="$(echo "${f%.py}" | sed 's#/#.#g')"
    echo "Launching module $mod"
    exec python -m "$mod"
  fi
  echo "Launching script $f"
  exec python "$f"
}

if [ -n "${UC_ENTRYPOINT:-}" ] && [ -f "$UC_ENTRYPOINT" ]; then run_py "$UC_ENTRYPOINT"; fi
if [ -f main.py ]; then run_py main.py; fi
if [ -f driver.py ]; then run_py driver.py; fi
FOUND="$(find . -maxdepth 3 -type f -name '*.py' | grep -E '/(main|driver|integration|intg).*\\.py$' | head -n1 || true)"
if [ -n "$FOUND" ]; then run_py "${FOUND#./}"; fi
echo "No Python entrypoint found. Set UC_ENTRYPOINT."
exit 1
"""


def detect_entrypoint(app_dir: Path) -> str:
    for name in ("main.py", "driver.py"):
        if (app_dir / name).exists():
            return name
    candidates = sorted(app_dir.glob("**/*.py"))
    pat = re.compile(r"(main|driver|integration|intg).*\.py$")
    for c in candidates:
        rel = c.relative_to(app_dir)
        if len(rel.parts) <= 3 and pat.search(rel.name):
            return str(rel)
    return ""


# --- multi-language source builds -------------------------------------------
# Integrations aren't all Python — they're written in Node/TypeScript, C#,
# Rust, Go, etc. When the repo ships no Dockerfile, detect the stack from its
# files and generate an appropriate build+run recipe. Each image reads the same
# UC_* env vars at runtime (passed by the container), so no per-language env is
# needed — only the build toolchain and the start command differ.

NODE_DOCKERFILE = r"""FROM node:20-slim
RUN apt-get update && apt-get install -y --no-install-recommends git python3 make g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN if [ -f package-lock.json ]; then npm ci || npm install; else npm install; fi
RUN node -e "const s=require('./package.json').scripts||{};process.exit(s.build?0:1)" && npm run build || true
CMD ["sh","/app/uc-entry.node.sh"]
"""

NODE_ENTRY = r"""#!/bin/sh
set -e
cd /app
if node -e "const s=require('./package.json').scripts||{};process.exit(s.start?0:1)" 2>/dev/null; then
  echo "Starting via: npm start"; exec npm start
fi
MAIN=$(node -e "try{const p=require('./package.json');let m=p.main;if(p.bin&&typeof p.bin==='object'){m=Object.values(p.bin)[0]||m}else if(typeof p.bin==='string'){m=p.bin}console.log(m||'')}catch(e){}")
if [ -n "$MAIN" ] && [ -f "$MAIN" ]; then echo "Starting $MAIN"; exec node "$MAIN"; fi
for f in dist/index.js dist/driver.js dist/main.js build/index.js index.js driver.js src/index.js src/driver.js; do
  [ -f "$f" ] && { echo "Starting $f"; exec node "$f"; }
done
echo "No Node entrypoint found (no start script, package.json main, or dist/index.js)."; exit 1
"""

DOTNET_DOCKERFILE = r"""FROM mcr.microsoft.com/dotnet/sdk:__SDK_TAG__
WORKDIR /src
COPY . /src
RUN PROJ=$(ls *.sln 2>/dev/null | head -1); if [ -z "$PROJ" ]; then PROJ=$(find . -name '*.csproj' | head -1); fi; echo "Publishing $PROJ"; dotnet publish "$PROJ" -c Release --no-self-contained -p:PublishSingleFile=false -p:PublishAot=false -p:PublishReadyToRun=false -p:PublishTrimmed=false -o /out
CMD ["sh","/src/uc-entry.dotnet.sh"]
"""

DOTNET_ENTRY = r"""#!/bin/sh
set -e
cd /out
# framework-dependent publish: <name>.runtimeconfig.json + <name>.dll (arch-portable)
RC=$(ls *.runtimeconfig.json 2>/dev/null | head -1 || true)
if [ -n "$RC" ]; then
  NAME=$(basename "$RC" .runtimeconfig.json)
  if [ -f "$NAME.dll" ]; then echo "Starting: dotnet $NAME.dll"; exec dotnet "$NAME.dll"; fi
  if [ -x "./$NAME" ]; then echo "Starting: ./$NAME"; exec "./$NAME"; fi
fi
# self-contained / single-file / native: run the app's native executable
for f in *; do
  [ -f "$f" ] && [ -x "$f" ] || continue
  case "$f" in
    createdump|apphost|*.dll|*.json|*.pdb|*.so|*.a|*.sh|*.map|web.config) continue ;;
  esac
  echo "Starting: ./$f"; exec "./$f"
done
echo "No runnable .NET entry found in /out. Contents:"; ls -la
exit 1
"""

RUST_DOCKERFILE = r"""FROM rust:slim
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN cargo build --release
CMD ["sh","-c","BIN=$(find target/release -maxdepth 1 -type f -executable ! -name '*.d' | head -1); echo \"Starting $BIN\"; exec \"$BIN\""]
"""

GO_DOCKERFILE = r"""FROM golang:alpine AS build
RUN apk add --no-cache git
WORKDIR /src
COPY . /src
RUN go build -o /out/driver . || go build -o /out/driver ./... || sh -c 'D=$(dirname $(grep -rl "func main" --include=*.go . | head -1)); go build -o /out/driver "./$D"'
FROM alpine
RUN apk add --no-cache ca-certificates
COPY --from=build /out/driver /usr/local/bin/driver
CMD ["/usr/local/bin/driver"]
"""


def _dotnet_sdk_tag(app_dir: Path) -> str:
    """Pick a .NET SDK image tag that can build this project.

    Reads the target framework(s) from every .csproj and any global.json SDK pin,
    then uses the highest version — an SDK can always build older frameworks, so
    matching the newest requirement is both necessary and sufficient. Defaults to
    a recent LTS when nothing is found.
    """
    versions: list[tuple[int, int]] = []

    gj = app_dir / "global.json"
    if gj.exists():
        try:
            v = (json.loads(gj.read_text()).get("sdk", {}) or {}).get("version", "") or ""
            m = re.match(r"(\d+)\.(\d+)", v)
            if m:
                versions.append((int(m.group(1)), int(m.group(2))))
        except Exception:  # noqa: BLE001
            pass

    for cs in app_dir.glob("**/*.csproj"):
        try:
            text = cs.read_text(errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        # matches <TargetFramework>net10.0</TargetFramework> and TargetFrameworks lists
        for maj, minr in re.findall(r"net(\d+)\.(\d+)", text):
            versions.append((int(maj), int(minr)))

    if not versions:
        return "8.0"
    maj, minr = max(versions)
    return f"{maj}.{minr}"


def detect_stack(app_dir: Path) -> str:
    """Identify the integration's language/runtime from repo files."""
    def exists(*names: str) -> bool:
        return any((app_dir / n).exists() for n in names)

    def glob(pattern: str) -> bool:
        return next(iter(app_dir.glob(pattern)), None) is not None

    if glob("*.sln") or glob("*.csproj") or glob("**/*.csproj"):
        return "dotnet"
    if exists("Cargo.toml"):
        return "rust"
    if exists("go.mod"):
        return "go"
    if exists("package.json"):
        return "node"
    if exists("requirements.txt", "pyproject.toml", "setup.py") or glob("**/*.py"):
        return "python"
    return "unknown"


def clone_or_update(repo: str, app_dir: Path, log, ref: str | None = None) -> None:
    pinned = ref and ref != "latest"
    if pinned:
        # fresh checkout of a specific tag/branch
        shutil.rmtree(app_dir, ignore_errors=True)
        log(f"Cloning {repo} at {ref} ...")
        app_dir.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", ref, repo, str(app_dir)]
        )
        if r.returncode != 0:  # ref may be a non-branch/tag; clone then checkout
            subprocess.run(["git", "clone", "--depth", "1", repo, str(app_dir)], check=True)
            subprocess.run(["git", "-C", str(app_dir), "fetch", "--depth", "1", "origin", ref], check=False)
            subprocess.run(["git", "-C", str(app_dir), "checkout", ref], check=True)
        return
    if (app_dir / ".git").exists():
        log(f"Updating source in {app_dir.name} ...")
        subprocess.run(["git", "-C", str(app_dir), "pull", "--ff-only"], check=False)
    else:
        log(f"Cloning {repo} ...")
        app_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", repo, str(app_dir)], check=True)


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------


class Job:
    def __init__(self, kind: str, integration_id: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.integration_id = integration_id
        self.status = "running"  # running | success | error
        self.lines: list[str] = []
        self.created = time.time()
        self.updated = time.time()
        self._lock = threading.Lock()

    def log(self, line: str) -> None:
        with self._lock:
            self.lines.append(line.rstrip("\n"))
            self.lines = self.lines[-500:]
            self.updated = time.time()

    def finish(self, status: str, line: str | None = None) -> None:
        if line:
            self.log(line)
        self.status = status
        self.updated = time.time()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "integration_id": self.integration_id,
                "status": self.status,
                "lines": list(self.lines),
                "created": self.created,
                "updated": self.updated,
            }


JOBS: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def new_job(kind: str, integration_id: str) -> Job:
    job = Job(kind, integration_id)
    with _jobs_lock:
        JOBS[job.id] = job
        # keep the map from growing unbounded
        if len(JOBS) > 100:
            oldest = sorted(JOBS.values(), key=lambda j: j.created)[:-100]
            for j in oldest:
                JOBS.pop(j.id, None)
    return job


# ---- install / recreate work (runs in a worker thread) ---------------------


def _pull_image(image: str, job: Job) -> bool:
    import docker

    client = get_docker()
    job.log(f"Looking for image {image} ...")
    try:
        last = ""
        for chunk in client.api.pull(*image.rsplit(":", 1), stream=True, decode=True):
            status = chunk.get("status")
            if status and status != last:
                job.log(status)
                last = status
        job.log(f"Pulled {image}")
        return True
    except (docker.errors.APIError, docker.errors.NotFound) as exc:
        job.log(f"No prebuilt image ({exc}). Will build from source.")
        return False


def _find_repo_dockerfile(app_dir: Path) -> str | None:
    """Return a repo-relative path to the project's own Dockerfile, if any."""
    for candidate in ("Dockerfile", "docker/Dockerfile", "Dockerfile.prod"):
        if (app_dir / candidate).exists():
            return candidate
    return None


def _docker_build(client, app_dir: Path, dockerfile: str, tag: str, job: Job) -> None:
    for chunk in client.api.build(
        path=str(app_dir), dockerfile=dockerfile, tag=tag, rm=True, pull=True, decode=True,
    ):
        if "stream" in chunk:
            text = chunk["stream"].strip()
            if text:
                job.log(text)
        elif "error" in chunk:
            raise RuntimeError(chunk["error"])


def _nixpacks_available() -> bool:
    return shutil.which("nixpacks") is not None


def _build_with_nixpacks(app_dir: Path, tag: str, job: Job) -> None:
    """Universal source build. Nixpacks auto-detects the language (Node, Python,
    Go, Rust, .NET, Java, PHP, Ruby, Deno, ...) and produces a runnable image."""
    proc = subprocess.Popen(
        ["nixpacks", "build", str(app_dir), "--name", tag],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            job.log(line)
    if proc.wait() != 0:
        raise RuntimeError(f"nixpacks exited with code {proc.returncode}")


def _prepare_stack_build(app_dir: Path, stack: str, job: Job) -> tuple[str | None, str | None]:
    """Write a tuned Dockerfile for a known stack. Returns (dockerfile, entrypoint)
    or (None, None) if the stack isn't one we generate a Dockerfile for."""
    if stack == "python":
        (app_dir / "Dockerfile.external").write_text(GENERIC_DOCKERFILE)
        (app_dir / "docker-entry.external.sh").write_text(GENERIC_ENTRY)
        entrypoint = detect_entrypoint(app_dir)
        job.log(f"Detected entrypoint: {entrypoint or '(auto at runtime)'}")
        return "Dockerfile.external", entrypoint
    if stack == "node":
        (app_dir / "Dockerfile.external").write_text(NODE_DOCKERFILE)
        (app_dir / "uc-entry.node.sh").write_text(NODE_ENTRY)
        return "Dockerfile.external", None
    if stack == "dotnet":
        sdk = _dotnet_sdk_tag(app_dir)
        job.log(f"Using .NET SDK image mcr.microsoft.com/dotnet/sdk:{sdk}")
        (app_dir / "Dockerfile.external").write_text(DOTNET_DOCKERFILE.replace("__SDK_TAG__", sdk))
        (app_dir / "uc-entry.dotnet.sh").write_text(DOTNET_ENTRY)
        return "Dockerfile.external", None
    if stack == "rust":
        (app_dir / "Dockerfile.external").write_text(RUST_DOCKERFILE)
        return "Dockerfile.external", None
    if stack == "go":
        (app_dir / "Dockerfile.external").write_text(GO_DOCKERFILE)
        return "Dockerfile.external", None
    return None, None


def _build_image(entry: dict[str, Any], job: Job,
                 version: str = "latest") -> tuple[str, str | None, str]:
    """Clone + build from source. Returns (image_tag, entrypoint, stack).

    Order: the project's own Dockerfile → a tuned build for a known language →
    Nixpacks (universal, any language) as the catch-all and as a fallback when a
    tuned build fails. This lets *any* integration build from source when no
    prebuilt image is available, as long as Nixpacks is installed for the long tail.
    """
    integration_id = entry["id"]
    repo = entry["repository"]
    app_dir = APPS_DIR / integration_id
    clone_or_update(repo, app_dir, job.log, ref=version)

    safe = re.sub(r"[^a-z0-9_.-]", "-", (version or "latest").lower())
    tag = f"uc-local/{integration_id}:{safe}"
    client = get_docker()

    # 1) the project's own Dockerfile — the author knows the correct build.
    repo_dockerfile = _find_repo_dockerfile(app_dir)
    if repo_dockerfile:
        job.log(f"Using the project's own {repo_dockerfile}. Building {tag} ...")
        _docker_build(client, app_dir, repo_dockerfile, tag, job)
        job.log(f"Built {tag}")
        return tag, None, "dockerfile"

    # 2) tuned build for a known language (UC integrations are mostly these).
    stack = detect_stack(app_dir)
    dockerfile, entrypoint = _prepare_stack_build(app_dir, stack, job)
    if dockerfile is not None:
        job.log(f"Detected a {stack} project. Building {tag} ...")
        try:
            _docker_build(client, app_dir, dockerfile, tag, job)
            job.log(f"Built {tag}")
            return tag, entrypoint, stack
        except Exception as exc:  # noqa: BLE001
            job.log(f"{stack} build failed: {exc}")
            if not _nixpacks_available():
                raise
            job.log("Retrying with Nixpacks ...")

    # 3) Nixpacks — universal dynamic build for anything else (or a failed tuned build).
    if _nixpacks_available():
        job.log("Building automatically with Nixpacks ...")
        _build_with_nixpacks(app_dir, tag, job)
        job.log(f"Built {tag} with Nixpacks")
        return tag, None, "nixpacks"

    raise RuntimeError(
        "Couldn't build this integration from source. No Dockerfile was found and "
        f"the language ({stack}) has no built-in recipe. Install Nixpacks on the host "
        "for automatic builds of any language (Node, Python, Go, Rust, .NET, Java, PHP, "
        "Ruby, ...), or add a Dockerfile to the repository."
    )


def _run_container(
    entry: dict[str, Any], instance_id: str, ordinal: int, image: str, source: str,
    port: int, extra_env: dict[str, str], entrypoint: str | None, job: Job,
    version: str = "latest", stack: str | None = None, platform: str | None = None,
):
    integration_id = entry["id"]
    cfg = CONFIG_DIR / instance_id
    cfg.mkdir(parents=True, exist_ok=True)

    env = base_environment(port, entrypoint)
    env.update(extra_env or {})

    existing = _container_for(instance_id)
    if existing is not None:
        job.log("Removing previous container ...")
        existing.remove(force=True)

    base_driver = entry.get("driver_id") or integration_id
    driver_id = instance_driver_id(base_driver, ordinal)
    name = entry.get("name", integration_id)
    label = instance_label(name, ordinal)

    run_kwargs: dict[str, Any] = {}
    if platform:
        run_kwargs["platform"] = platform

    job.log(f"Starting container '{instance_id}' on port {port} ...")
    get_docker().containers.run(
        image,
        name=instance_id,
        detach=True,
        network_mode="host",
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
        environment=env,
        volumes={str(cfg): {"bind": "/config", "mode": "rw"}},
        labels={
            LABEL_MANAGED: "managed",
            LABEL_ID: integration_id,
            LABEL_INSTANCE: instance_id,
            LABEL_ORDINAL: str(ordinal),
            LABEL_NAME: name,
            LABEL_SOURCE: source,
            LABEL_PORT: str(port),
        },
        **run_kwargs,
    )

    prev = load_state()["integrations"].get(instance_id, {})
    record_integration({
        "instance_id": instance_id,
        "id": instance_id,
        "integration_id": integration_id,
        "instance": ordinal,
        "name": name,
        "label": label,
        "driver_id": driver_id,
        "repository": entry.get("repository", ""),
        "image": image,
        "source": source,
        "port": port,
        "env": extra_env or {},
        "entrypoint": entrypoint or "",
        "version": version or "latest",
        "stack": stack if stack is not None else prev.get("stack"),
        "auto_update": prev.get("auto_update", False),
        "installed_at": prev.get("installed_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    record_event("install", instance_id, f"{label} started on port {port} ({source})")
    job.log("Done.")


def do_install(entry: dict[str, Any], instance_id: str, ordinal: int, port: int,
               extra_env: dict[str, str], job: Job, version: str = "latest",
               reuse: bool = False):
    try:
        if _install_lock.locked():
            job.log("Another install is in progress — queued, waiting ...")
        with _install_lock:
            version = version or "latest"

            # Additional instances of an already-installed integration reuse the
            # sibling's resolved image/stack — no re-pull or rebuild, same stack.
            if reuse:
                sib = next((r for iid, r in load_state()["integrations"].items()
                            if iid != instance_id
                            and r.get("integration_id") == entry["id"]
                            and r.get("version", "latest") == version
                            and r.get("image")), None)
                if sib:
                    job.log(f"Reusing image {sib['image']} from {sib.get('label', sib['id'])} "
                            f"({sib.get('source')})")
                    _run_container(entry, instance_id, ordinal, sib["image"], sib.get("source", "ghcr"),
                                   port, extra_env, sib.get("entrypoint") or None, job, version,
                                   sib.get("stack"))
                    job.finish("success")
                    return

            image = image_from_repo(entry["repository"], version)
            source = "ghcr"
            entrypoint = None
            stack = None
            if not _pull_image(image, job):
                image, entrypoint, stack = _build_image(entry, job, version)
                source = "build"
            _run_container(entry, instance_id, ordinal, image, source, port,
                           extra_env, entrypoint, job, version, stack)
        job.finish("success")
    except Exception as exc:  # noqa: BLE001
        record_event("error", instance_id, f"install failed: {exc}")
        job.finish("error", f"ERROR: {exc}")


def do_reconfigure(rec: dict[str, Any], port: int, extra_env: dict[str, str], job: Job):
    """Re-run an existing instance with new port/env, reusing its resolved image."""
    try:
        integration_id = rec.get("integration_id", rec["id"])
        entry = find_integration(integration_id) or {
            "id": integration_id, "name": rec.get("name", integration_id),
            "repository": rec.get("repository", ""), "driver_id": rec.get("driver_id"),
        }
        image = rec.get("image") or image_from_repo(rec.get("repository", ""))
        source = rec.get("source", "ghcr")
        entrypoint = rec.get("entrypoint") or None
        version = rec.get("version", "latest")
        _run_container(entry, rec["instance_id"], rec.get("instance", 1), image, source,
                       port, extra_env, entrypoint, job, version, rec.get("stack"))
        job.finish("success")
    except Exception as exc:  # noqa: BLE001
        job.finish("error", f"ERROR: {exc}")


def do_rebuild(rec: dict[str, Any], job: Job, version: str | None = None):
    """Force a fresh pull/build of an existing instance."""
    integration_id = rec.get("integration_id", rec["id"])
    entry = find_integration(integration_id)
    if entry is None:
        job.finish("error", "ERROR: integration is no longer in the registry")
        return
    do_install(entry, rec["instance_id"], rec.get("instance", 1), int(rec["port"]),
               rec.get("env", {}), job, version or rec.get("version", "latest"))


# ---- install from an Unfolded Circle release archive (.tar.gz) --------------

ARCHIVE_DOCKERFILE = """FROM debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libicu-dev libssl3 && rm -rf /var/lib/apt/lists/* || true
WORKDIR /app
COPY . /app
RUN chmod +x /app/bin/driver 2>/dev/null || true
CMD ["/app/bin/driver"]
"""

_ELF_MACHINES = {0x3E: "x86_64", 0xB7: "aarch64", 0x28: "arm", 0x08: "mips"}
_ARCH_PLATFORM = {"x86_64": "linux/amd64", "aarch64": "linux/arm64", "arm": "linux/arm/v7"}


def _elf_arch(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            head = f.read(20)
        if head[:4] != b"\x7fELF":
            return None
        return _ELF_MACHINES.get(int.from_bytes(head[18:20], "little"))
    except Exception:  # noqa: BLE001
        return None


def _host_platform() -> str:
    return _ARCH_PLATFORM.get(platform.machine().lower().replace("arm64", "aarch64"), "linux/amd64")


def _safe_extract(tar: tarfile.TarFile, dest: Path, only: str | None = None) -> None:
    dest = dest.resolve()
    for m in tar.getmembers():
        if only and m.name != only and not m.name.startswith(only.rstrip("/") + "/"):
            continue
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest)):
            continue  # skip path traversal
        tar.extract(m, dest)


def do_install_archive(data: bytes, filename: str, job: Job):
    try:
        with _install_lock:
            job.log(f"Reading {filename} ({len(data)//1024} KB) ...")
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
                dj_member = next((m for m in tar.getmembers() if m.name.rstrip("/").endswith("driver.json")), None)
                if dj_member is None:
                    raise RuntimeError("no driver.json in archive — not a UC integration release")
                dj = json.loads(tar.extractfile(dj_member).read().decode("utf-8"))

            driver_id = dj.get("driver_id") or "archive-driver"
            name = dj.get("name")
            if isinstance(name, dict):
                name = name.get("en") or next(iter(name.values()), driver_id)
            name = name or driver_id
            ver = dj.get("version", "latest")
            slug = re.sub(r"[^a-z0-9_.-]", "-", driver_id.lower())
            instance_id = f"archive-{slug}"
            app_dir = APPS_DIR / instance_id
            shutil.rmtree(app_dir, ignore_errors=True)
            app_dir.mkdir(parents=True)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
                _safe_extract(tar, app_dir)

            binpath = app_dir / "bin" / "driver"
            if not binpath.exists():
                binpath = next((p for p in app_dir.glob("**/driver") if p.is_file()), None)
            arch = _elf_arch(binpath) if binpath else None
            build_platform = _ARCH_PLATFORM.get(arch or "")
            host_pf = _host_platform()
            job.log(f"driver_id={driver_id} name={name} version={ver} binary_arch={arch} host={host_pf}")
            if build_platform and build_platform != host_pf:
                raise RuntimeError(
                    f"This release is built for {arch}, but this host is {host_pf}. "
                    "Release archives must run on a matching (native) architecture — under "
                    "QEMU emulation the integration's mDNS sockets fail (OSError 92, "
                    "'Protocol not available') and it crash-loops. Install this on an "
                    "ARM64 host, or use a source/GHCR install instead."
                )
            plat = None

            (app_dir / "Dockerfile.external").write_text(ARCHIVE_DOCKERFILE)
            tag = f"uc-local/{instance_id}:{re.sub(r'[^a-z0-9_.-]','-',str(ver).lower())}"
            job.log(f"Building {tag} ...")
            client = get_docker()
            build_kwargs = {"platform": plat} if plat else {}
            for chunk in client.api.build(path=str(app_dir), dockerfile="Dockerfile.external",
                                          tag=tag, rm=True, pull=True, decode=True, **build_kwargs):
                if "stream" in chunk:
                    t = chunk["stream"].strip()
                    if t:
                        job.log(t)
                elif "error" in chunk:
                    raise RuntimeError(chunk["error"])

            entry = {"id": instance_id, "name": name, "repository": "", "driver_id": driver_id}
            port = next_free_port()
            _run_container(entry, instance_id, 1, tag, "archive", port, {}, None, job,
                           str(ver), "archive", platform=plat)
        job.finish("success")
    except Exception as exc:  # noqa: BLE001
        record_event("error", None, f"archive install failed: {exc}")
        job.finish("error", f"ERROR: {exc}")


# ---------------------------------------------------------------------------
# Self-update (pull this installer's own code from GitHub)
# ---------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(APP_DIR), *args], capture_output=True, text=True
    )


def _is_git_repo() -> bool:
    return (APP_DIR / ".git").exists()


def _owner_repo_from_url(url: str) -> str:
    r = re.sub(r"^https?://github\.com/", "", url.strip())
    return re.sub(r"\.git$", "", r)


def current_commit() -> dict[str, Any]:
    if not _is_git_repo():
        return {"sha": None, "short": None, "date": None, "subject": None}
    sha = _git("rev-parse", "HEAD")
    if sha.returncode != 0:
        return {"sha": None, "short": None, "date": None, "subject": None}
    full = sha.stdout.strip()
    meta = _git("log", "-1", "--format=%cI%n%s")
    date, subject = (meta.stdout.strip().split("\n", 1) + ["", ""])[:2]
    return {"sha": full, "short": full[:7], "date": date, "subject": subject}


def latest_commit() -> dict[str, Any]:
    api = f"https://api.github.com/repos/{_owner_repo_from_url(update_repo())}/commits/{update_branch()}"
    resp = httpx.get(
        api,
        headers={
            "User-Agent": "uc-external-integration-installer",
            "Accept": "application/vnd.github+json",
        },
        timeout=15.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    j = resp.json()
    return {
        "sha": j["sha"],
        "short": j["sha"][:7],
        "date": j.get("commit", {}).get("committer", {}).get("date"),
        "subject": (j.get("commit", {}).get("message", "") or "").splitlines()[0],
    }


def _can_restart_service() -> bool:
    # systemd sets INVOCATION_ID for units it starts; systemd-run lets us restart
    # ourselves from a separate cgroup that survives the restart.
    return bool(os.environ.get("INVOCATION_ID")) and shutil.which("systemd-run") is not None


def _run_git_logged(job: Job, *args: str) -> None:
    job.log("$ git " + " ".join(args))
    r = _git(*args)
    for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
        if line.strip():
            job.log(line)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({r.returncode})")


def do_update(job: Job, ref: str | None = None) -> None:
    target = (ref or update_branch()).strip() or update_branch()
    try:
        job.log(f"Repository: {update_repo()}  (build: {target})")
        job.log(f"Install directory: {APP_DIR}")

        if not _is_git_repo():
            job.log("Not a git checkout yet — attaching the repository in place ...")
            _run_git_logged(job, "init", "-q")
            if _git("remote", "add", "origin", update_repo()).returncode != 0:
                _run_git_logged(job, "remote", "set-url", "origin", update_repo())

        _run_git_logged(job, "fetch", "--depth", "1", "origin", target)
        _run_git_logged(job, "reset", "--hard", "FETCH_HEAD")

        cur = current_commit()
        job.log(f"Now at {cur['short']} — {cur['subject']}")

        req = APP_DIR / "requirements.txt"
        if req.exists():
            job.log("Refreshing Python dependencies ...")
            p = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req)],
                capture_output=True, text=True,
            )
            for line in (p.stdout + p.stderr).splitlines():
                if line.strip():
                    job.log(line)
            if p.returncode != 0:
                raise RuntimeError("pip install failed")

        if _can_restart_service():
            job.finish("success", f"Update complete — restarting {service_unit_val()} now.")
            time.sleep(2)  # let the UI fetch the final log before we go down
            subprocess.Popen(
                ["systemd-run", "--no-block", "--collect",
                 "systemctl", "restart", service_unit_val()],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            job.finish(
                "success",
                f"Update complete. Restart to apply: systemctl restart {service_unit_val()} "
                "(or restart the process).",
            )
    except Exception as exc:  # noqa: BLE001
        job.finish("error", f"ERROR: {exc}")


class InstallBody(BaseModel):
    port: int | None = None
    env: dict[str, str] = {}
    version: str | None = None


class ConfigBody(BaseModel):
    port: int | None = None
    env: dict[str, str] = {}


class RebuildBody(BaseModel):
    version: str | None = None


class RemoteBody(BaseModel):
    name: str
    address: str                      # host, host:port, or full http(s):// url
    username: str | None = "web-configurator"
    pin: str | None = None
    api_key: str | None = None
    verify_tls: bool = False
    advertise_ip: str | None = None


class RegisterBody(BaseModel):
    integration_id: str
    advertise_ip: str | None = None


class ActiveRemoteBody(BaseModel):
    id: str | None = None


class UpdatePolicyBody(BaseModel):
    mode: str = "off"  # off | notify | stable | prerelease | scheduled
    delay_days: int = 0
    maintenance_window: str = ""


class SettingsImportBody(BaseModel):
    payload: dict[str, Any]



# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def require_token(authorization: str | None = Header(default=None),
                  token: str | None = Query(default=None)) -> None:
    tok = token_val()
    if not tok:
        return
    supplied = None
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    supplied = supplied or token
    if supplied != tok:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="UC External Integration Installer", version="1.0.0")


@app.get("/api/health")
def health() -> dict[str, Any]:
    reg_ok, version, count = True, None, 0
    try:
        data = fetch_registry()
        version = data.get("version")
        count = len(data.get("integrations", []))
    except Exception:
        reg_ok = False
    return {
        "docker": docker_available(),
        "registry_ok": reg_ok,
        "registry_version": version,
        "integration_count": count,
        "token_required": bool(token_val()),
        "port_start": get_port_start(),
        "data_dir": str(DATA_DIR),
        "build": current_commit().get("short"),
        "registry_commit": registry_commit(),
        "nixpacks": _nixpacks_available(),
        "host_arch": platform.machine(),
        "bind_host": os.environ.get("UC_INSTALLER_HOST", "0.0.0.0"),
        "bind_port": int(os.environ.get("UC_INSTALLER_PORT", "8900")),
        "data_dir": str(DATA_DIR),
        "setup_complete": load_settings()["setup_complete"],
        "archive_supported": platform.machine().lower() in ("aarch64", "arm64"),
    }


@app.get("/api/update/status", dependencies=[Depends(require_token)])
def api_update_status() -> dict[str, Any]:
    cur = current_commit()
    info: dict[str, Any] = {
        "repo": update_repo(),
        "branch": update_branch(),
        "service": service_unit_val(),
        "is_git": _is_git_repo(),
        "service_restartable": _can_restart_service(),
        "current": cur,
    }
    try:
        latest = latest_commit()
        info["latest"] = latest
        info["update_available"] = (cur["sha"] != latest["sha"]) if cur["sha"] else True
        info["error"] = None
    except Exception as exc:  # noqa: BLE001
        info["latest"] = None
        info["update_available"] = False
        info["error"] = f"Could not reach GitHub: {exc}"
    return info


@app.get("/api/update/builds", dependencies=[Depends(require_token)])
def api_update_builds() -> dict[str, Any]:
    """List the branches and tags/releases of the installer repo the user can
    switch to."""
    owner_repo = _owner_repo_from_url(update_repo())
    headers = {"User-Agent": "uc-external-integration-installer",
               "Accept": "application/vnd.github+json"}
    refs: list[dict[str, str]] = []
    error = None
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as cl:
            b = cl.get(f"https://api.github.com/repos/{owner_repo}/branches?per_page=100")
            if b.status_code < 400:
                for br in b.json():
                    refs.append({"type": "branch", "name": br.get("name", "")})
            t = cl.get(f"https://api.github.com/repos/{owner_repo}/tags?per_page=100")
            if t.status_code < 400:
                for tg in t.json():
                    refs.append({"type": "tag", "name": tg.get("name", "")})
    except Exception as exc:  # noqa: BLE001
        error = f"Could not reach GitHub: {exc}"
    refs = [r for r in refs if r["name"]]
    return {"repo": update_repo(), "configured_branch": update_branch(),
            "current": current_commit(), "refs": refs, "error": error}


class UpdateBody(BaseModel):
    ref: str | None = None


@app.post("/api/update/apply", dependencies=[Depends(require_token)])
def api_update_apply(body: UpdateBody | None = None) -> dict[str, str]:
    if not shutil.which("git"):
        raise HTTPException(500, "git is not installed on the host")
    ref = (body.ref if body else None)
    job = new_job("update", "self")
    threading.Thread(target=do_update, args=(job, ref), daemon=True).start()
    return {"job_id": job.id}


def _installed_ids() -> set[str]:
    return set(load_state()["integrations"].keys())


@app.get("/api/registry", dependencies=[Depends(require_token)])
def api_registry(refresh: bool = False) -> dict[str, Any]:
    data = fetch_registry(force=refresh)
    installed = _installed_ids()
    cats = {c["id"]: c for c in data.get("categories", [])}
    devs = {d["name"]: d for d in data.get("developers", [])}
    items = []
    for it in data.get("integrations", []):
        items.append({
            "id": it.get("id"),
            "name": it.get("name"),
            "author": it.get("author"),
            "description": it.get("description", ""),
            "repository": it.get("repository", ""),
            "categories": it.get("categories", []),
            "features": it.get("features", []),
            "official": it.get("official", False),
            "custom": it.get("custom", False),
            "installable": is_installable(it),
            "installed": it.get("id") in installed,
            "image_candidate": image_from_repo(it["repository"]) if is_installable(it) else None,
        })
    return {
        "version": data.get("version"),
        "last_updated": data.get("last_updated"),
        "categories": list(cats.values()),
        "developers": devs,
        "integrations": items,
    }


@app.get("/api/installed", dependencies=[Depends(require_token)])
def api_installed() -> list[dict[str, Any]]:
    state = load_state()
    result = []
    for integration_id, rec in state["integrations"].items():
        status, health_status = "unknown", None
        restart_count, last_error, exit_code = 0, "", None
        c = _container_for(integration_id)
        if c is not None:
            status = c.status
            state = c.attrs.get("State", {}) or {}
            health_status = (state.get("Health", {}) or {}).get("Status")
            restart_count = c.attrs.get("RestartCount", 0)
            last_error = state.get("Error", "") or ""
            exit_code = state.get("ExitCode")
        else:
            status = "missing"
        result.append({
            **rec,
            "status": status,
            "health": health_status,
            "restart_count": restart_count,
            "exit_code": exit_code,
            "last_error": last_error,
        })
    result.sort(key=lambda r: r.get("name", "").lower())
    return result


@app.post("/api/integrations/{integration_id}/install", dependencies=[Depends(require_token)])
def api_install(integration_id: str, body: InstallBody) -> dict[str, str]:
    """Install (or replace) the default instance of an integration."""
    entry = find_integration(integration_id)
    if entry is None:
        raise HTTPException(404, "Unknown integration")
    if not is_installable(entry):
        raise HTTPException(400, "This integration has no installable repository")
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    port = resolve_port(body.port, instance_id=integration_id)
    job = new_job("install", integration_id)
    threading.Thread(
        target=do_install,
        args=(entry, integration_id, 1, port, body.env, job, body.version or "latest"),
        daemon=True,
    ).start()
    return {"job_id": job.id}


@app.post("/api/integrations/{integration_id}/add-instance", dependencies=[Depends(require_token)])
def api_add_instance(integration_id: str, body: InstallBody) -> dict[str, str]:
    """Spin up an additional, independent instance of an integration."""
    entry = find_integration(integration_id)
    if entry is None:
        raise HTTPException(404, "Unknown integration")
    if not is_installable(entry):
        raise HTTPException(400, "This integration has no installable repository")
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    instance_id, ordinal = next_instance_id(integration_id)
    port = resolve_port(body.port, instance_id=instance_id)
    job = new_job("install", instance_id)
    threading.Thread(
        target=do_install,
        args=(entry, instance_id, ordinal, port, body.env, job, body.version or "latest", True),
        daemon=True,
    ).start()
    return {"job_id": job.id, "instance_id": instance_id}


@app.post("/api/instances/{instance_id}/config", dependencies=[Depends(require_token)])
def api_config(instance_id: str, body: ConfigBody) -> dict[str, str]:
    rec = load_state()["integrations"].get(instance_id)
    if rec is None:
        raise HTTPException(404, "Instance is not installed")
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    port = resolve_port(body.port or int(rec.get("port") or 0), instance_id=instance_id)
    job = new_job("reconfigure", instance_id)
    threading.Thread(target=do_reconfigure, args=(rec, port, body.env, job), daemon=True).start()
    return {"job_id": job.id}


@app.post("/api/instances/{instance_id}/rebuild", dependencies=[Depends(require_token)])
def api_rebuild(instance_id: str, body: RebuildBody) -> dict[str, str]:
    rec = load_state()["integrations"].get(instance_id)
    if rec is None:
        raise HTTPException(404, "Instance is not installed")
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    job = new_job("rebuild", instance_id)
    threading.Thread(target=do_rebuild, args=(rec, job, body.version), daemon=True).start()
    return {"job_id": job.id}


class AutoUpdateBody(BaseModel):
    enabled: bool


@app.post("/api/instances/{instance_id}/auto-update", dependencies=[Depends(require_token)])
def api_auto_update(instance_id: str, body: AutoUpdateBody) -> dict[str, Any]:
    state = load_state()
    rec = state["integrations"].get(instance_id)
    if rec is None:
        raise HTTPException(404, "Instance is not installed")
    rec["auto_update"] = bool(body.enabled)
    save_state(state)
    record_event("lifecycle", instance_id,
                 f"auto-update {'enabled' if body.enabled else 'disabled'}")
    return {"auto_update": rec["auto_update"]}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
def api_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job")
    return job.to_dict()


def _lifecycle(instance_id: str, action: str) -> dict[str, str]:
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    c = _container_for(instance_id)
    if c is None:
        raise HTTPException(404, "Container not found")
    getattr(c, action)()
    record_event("lifecycle", instance_id, f"{action} requested")
    return {"status": "ok", "action": action}


@app.post("/api/instances/{instance_id}/start", dependencies=[Depends(require_token)])
def api_start(instance_id: str):
    return _lifecycle(instance_id, "start")


@app.post("/api/instances/{instance_id}/stop", dependencies=[Depends(require_token)])
def api_stop(instance_id: str):
    return _lifecycle(instance_id, "stop")


@app.post("/api/instances/{instance_id}/restart", dependencies=[Depends(require_token)])
def api_restart(instance_id: str):
    return _lifecycle(instance_id, "restart")


@app.delete("/api/instances/{instance_id}", dependencies=[Depends(require_token)])
def api_remove(instance_id: str, purge: bool = False):
    rec = load_state()["integrations"].get(instance_id, {})
    integration_id = rec.get("integration_id", instance_id)
    if docker_available():
        c = _container_for(instance_id)
        if c is not None:
            c.remove(force=True)
    forget_integration(instance_id)
    if purge:
        shutil.rmtree(CONFIG_DIR / instance_id, ignore_errors=True)
        # only drop the shared source clone when no sibling instances remain
        if not integration_instances(integration_id):
            shutil.rmtree(APPS_DIR / integration_id, ignore_errors=True)
    record_event("remove", instance_id, f"removed{' (purged config)' if purge else ''}")
    return {"status": "removed", "purged": purge}


@app.get("/api/instances/{instance_id}/logs", dependencies=[Depends(require_token)])
def api_logs(instance_id: str, tail: int = 300):
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    c = _container_for(instance_id)
    if c is None:
        raise HTTPException(404, "Container not found")
    logs = c.logs(tail=tail, timestamps=False).decode("utf-8", "replace")
    return {"logs": logs}


@app.get("/api/instances/{instance_id}/logs/stream", dependencies=[Depends(require_token)])
def api_logs_stream(instance_id: str, tail: int = 200):
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    c = _container_for(instance_id)
    if c is None:
        raise HTTPException(404, "Container not found")

    def gen():
        try:
            for line in c.logs(stream=True, follow=True, tail=tail):
                text = line.decode("utf-8", "replace").rstrip("\n")
                yield f"data: {text}\n\n"
        except Exception:  # noqa: BLE001
            return

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---- versions & update detection -------------------------------------------


@app.get("/api/integrations/{integration_id}/versions", dependencies=[Depends(require_token)])
def api_versions(integration_id: str) -> dict[str, Any]:
    entry = find_integration(integration_id)
    if entry is None:
        raise HTTPException(404, "Unknown integration")
    versions = repo_versions(entry.get("repository", ""))
    rec = load_state()["integrations"].get(integration_id)
    return {
        "current": (rec or {}).get("version"),
        "installed": rec is not None,
        # "latest" always offered (moving tag); then the discovered releases/tags
        "versions": [{"tag": "latest", "published_at": None, "prerelease": False}] + versions,
    }


@app.get("/api/updates", dependencies=[Depends(require_token)])
def api_updates() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for integration_id, rec in load_state()["integrations"].items():
        try:
            out[integration_id] = compute_update(rec)
        except Exception:  # noqa: BLE001
            out[integration_id] = {"update_available": False, "latest_version": None,
                                   "installed_version": rec.get("version")}
    return out


# ---- live health / usage stats ---------------------------------------------

_stats_cache: dict[str, Any] = {"ts": 0.0, "data": {}, "refreshing": False}
_stats_lock = threading.Lock()


def _cpu_percent(st: dict[str, Any]) -> float:
    cpu = st.get("cpu_stats", {}) or {}
    pre = st.get("precpu_stats", {}) or {}
    cu = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
    pu = (pre.get("cpu_usage") or {}).get("total_usage", 0)
    su = cpu.get("system_cpu_usage", 0) or 0
    ps = pre.get("system_cpu_usage", 0) or 0
    online = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    cd, sd = cu - pu, su - ps
    if cd > 0 and sd > 0:
        return round(cd / sd * online * 100.0, 1)
    return 0.0


def _mem_usage(st: dict[str, Any]) -> tuple[int, int]:
    m = st.get("memory_stats", {}) or {}
    usage = m.get("usage", 0) or 0
    sub = m.get("stats", {}) or {}
    cache = sub.get("inactive_file", sub.get("cache", 0)) or 0
    return max(usage - cache, 0), (m.get("limit", 0) or 0)


def _compute_stats(container) -> dict[str, Any]:
    """Two streamed samples give an accurate CPU delta; the 2nd frame already
    carries precpu_stats from the 1st."""
    try:
        gen = container.stats(stream=True, decode=True)
        next(gen)          # seed frame
        st = next(gen)     # has precpu populated
        try:
            gen.close()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        return {}
    used, limit = _mem_usage(st)
    return {
        "cpu_pct": _cpu_percent(st),
        "mem_used": used,
        "mem_limit": limit,
        "mem_pct": round(used / limit * 100.0, 1) if limit else None,
        "pids": (st.get("pids_stats") or {}).get("current"),
    }


HEALTH_PROBE_ENABLED = os.environ.get("UC_INSTALLER_HEALTH_PROBE", "1") != "0"
_HEALTH_TTL = 30.0
_health_cache: dict[str, tuple[float, str | None]] = {}
_health_lock = threading.Lock()


def _probe_port(port: Any, host: str = "127.0.0.1", timeout: float = 1.5) -> bool | None:
    """Liveness probe that performs a proper WebSocket opening handshake.

    UC integrations run a WebSocket server (ucapi). A bare TCP connect-and-close
    makes that server log a noisy "opening handshake failed / did not receive a
    valid HTTP request" traceback, so we send a real Upgrade request (and a clean
    close frame) — the integration sees a normal client, not malformed input.
    Returns True if the port accepts the connection, False if refused, None if the
    port is unknown."""
    try:
        p = int(port)
    except (TypeError, ValueError):
        return None
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET / HTTP/1.1\r\n"
        f"Host: {host}:{p}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode()
    try:
        with socket.create_connection((host, p), timeout=timeout) as s:
            s.settimeout(timeout)
            try:
                s.sendall(req)
                s.recv(256)                                # let the server send its 101
                s.sendall(b"\x88\x80\x00\x00\x00\x00")     # masked empty close frame
            except OSError:
                pass  # port was open; a clean close is best-effort
            return True
    except OSError:
        return False


def _probe_health(port: Any) -> str | None:
    if not health_probe_on():
        return None
    keyp = str(port)
    now = time.time()
    with _health_lock:
        hit = _health_cache.get(keyp)
        if hit and now - hit[0] < _HEALTH_TTL:
            return hit[1]
    ok = _probe_port(port)
    val = None if ok is None else ("responding" if ok else "unreachable")
    with _health_lock:
        _health_cache[keyp] = (now, val)
    return val


def _refresh_stats() -> None:
    try:
        data: dict[str, Any] = {}
        running = []
        for iid, rec in load_state()["integrations"].items():
            c = _container_for(iid)
            if c is None:
                data[iid] = {"status": "missing"}
                continue
            state = c.attrs.get("State", {}) or {}
            docker_health = (state.get("Health") or {}).get("Status")
            data[iid] = {
                "status": c.status,
                "health": docker_health,
                "started_at": state.get("StartedAt"),
                "restart_count": c.attrs.get("RestartCount", 0),
            }
            if c.status == "running":
                running.append((iid, c, rec.get("port"), docker_health))
        if running:
            import concurrent.futures as cf

            def _work(item):
                iid, c, port, docker_health = item
                s = _compute_stats(c)
                # No Docker HEALTHCHECK on these images — derive health from an
                # application-level probe of the integration's WebSocket port.
                if not docker_health:
                    h = _probe_health(port)
                    if h:
                        s["health"] = h
                return iid, s

            with cf.ThreadPoolExecutor(max_workers=min(8, len(running))) as ex:
                futs = [ex.submit(_work, it) for it in running]
                for fut in cf.as_completed(futs, timeout=12):
                    try:
                        iid, s = fut.result()
                        data[iid].update(s)
                    except Exception:  # noqa: BLE001
                        pass
        with _stats_lock:
            _stats_cache["data"] = data
            _stats_cache["ts"] = time.time()
    finally:
        with _stats_lock:
            _stats_cache["refreshing"] = False


@app.get("/api/stats", dependencies=[Depends(require_token)])
def api_stats() -> dict[str, Any]:
    """Cached per-integration health/usage. Refreshed in the background so the
    request never blocks on Docker stats sampling."""
    if not docker_available():
        return {}
    with _stats_lock:
        stale = time.time() - _stats_cache["ts"] > 4
        if stale and not _stats_cache["refreshing"]:
            _stats_cache["refreshing"] = True
            threading.Thread(target=_refresh_stats, daemon=True).start()
    return _stats_cache["data"]


# ---- remotes ---------------------------------------------------------------

# Short cache of each remote's registered driver list, so building the
# registration map doesn't hammer the remotes on every page load.
_remote_drivers_cache: dict[str, dict[str, Any]] = {}
_remote_drivers_lock = threading.Lock()
_REMOTE_DRIVERS_TTL = 30


def _remote_drivers(remote: dict[str, Any]) -> list[dict[str, Any]]:
    rid = remote["id"]
    now = time.time()
    with _remote_drivers_lock:
        cached = _remote_drivers_cache.get(rid)
        if cached and now - cached["ts"] < _REMOTE_DRIVERS_TTL:
            return cached["items"]
    items: list[dict[str, Any]] = []
    try:
        items = remote_all_drivers(remote, timeout=8.0)
    except Exception:  # noqa: BLE001
        items = []
    with _remote_drivers_lock:
        _remote_drivers_cache[rid] = {"ts": now, "items": items}
    return items


def _driver_url_port(url: str) -> str | None:
    m = re.search(r":(\d+)(?:/|$)", url or "")
    return m.group(1) if m else None


def _driver_is_ours(d: dict[str, Any], rec: dict[str, Any]) -> bool:
    """True only if this remote driver is OUR external instance — not the remote's
    own built-in (LOCAL) or uploaded (CUSTOM) driver that shares the driver_id."""
    if not isinstance(d, dict) or d.get("driver_id") != rec.get("driver_id"):
        return False
    url = d.get("driver_url") or ""
    recport = str(rec.get("port") or "")
    if url:
        # our registration set driver_url = ws://<host>:<our-port>; require an exact
        # port match so a CUSTOM driver uploaded onto the remote isn't mistaken for ours
        return bool(recport) and _driver_url_port(url) == recport
    # no url in the listing → only trust an explicitly EXTERNAL type (never CUSTOM/LOCAL)
    return (d.get("driver_type") or "").upper() == "EXTERNAL"


@app.get("/api/registrations", dependencies=[Depends(require_token)])
def api_registrations() -> dict[str, list[dict[str, str]]]:
    """Map of installed instance id -> the remotes it's registered on."""
    remotes = load_remotes()["remotes"]
    state = load_state()["integrations"]
    result: dict[str, list[dict[str, str]]] = {iid: [] for iid in state}
    for rid, remote in remotes.items():
        drivers = [d for d in _remote_drivers(remote) if isinstance(d, dict)]
        for iid, rec in state.items():
            if any(_driver_is_ours(d, rec) for d in drivers):
                result[iid].append({"remote_id": rid, "remote_name": remote.get("name", rid)})
    return result


def _get_remote_or_404(rid: str) -> dict[str, Any]:
    remote = load_remotes()["remotes"].get(rid)
    if remote is None:
        raise HTTPException(404, "Unknown remote")
    return remote


@app.get("/api/remotes", dependencies=[Depends(require_token)])
def api_remotes_list() -> dict[str, Any]:
    data = load_remotes()
    return {
        "remotes": [_mask(r) for r in data["remotes"].values()],
        "active": data.get("active"),
    }


@app.post("/api/remotes", dependencies=[Depends(require_token)])
def api_remotes_create(body: RemoteBody) -> dict[str, Any]:
    scheme, host, port = parse_address(body.address)
    if not host:
        raise HTTPException(400, "Could not parse the remote address")
    rid = uuid.uuid4().hex[:8]
    remote = {
        "id": rid, "name": body.name, "scheme": scheme, "host": host, "port": port,
        "username": body.username or "web-configurator",
        "pin": body.pin or "", "api_key": body.api_key or "",
        "verify_tls": body.verify_tls, "advertise_ip": body.advertise_ip or "",
    }
    with _remotes_lock:
        data = load_remotes()
        data["remotes"][rid] = remote
        if not data.get("active"):
            data["active"] = rid
        save_remotes(data)
    return _mask(remote)


@app.put("/api/remotes/{rid}", dependencies=[Depends(require_token)])
def api_remotes_update(rid: str, body: RemoteBody) -> dict[str, Any]:
    with _remotes_lock:
        data = load_remotes()
        remote = data["remotes"].get(rid)
        if remote is None:
            raise HTTPException(404, "Unknown remote")
        scheme, host, port = parse_address(body.address)
        remote.update({
            "name": body.name, "scheme": scheme, "host": host, "port": port,
            "username": body.username or "web-configurator",
            "verify_tls": body.verify_tls, "advertise_ip": body.advertise_ip or "",
        })
        # only overwrite secrets when a new value is supplied
        if body.pin:
            remote["pin"] = body.pin
        if body.api_key is not None:
            remote["api_key"] = body.api_key
        save_remotes(data)
        return _mask(remote)


@app.delete("/api/remotes/{rid}", dependencies=[Depends(require_token)])
def api_remotes_delete(rid: str) -> dict[str, str]:
    with _remotes_lock:
        data = load_remotes()
        data["remotes"].pop(rid, None)
        if data.get("active") == rid:
            data["active"] = next(iter(data["remotes"]), None)
        save_remotes(data)
    return {"status": "removed"}


@app.post("/api/remotes/active", dependencies=[Depends(require_token)])
def api_remotes_active(body: ActiveRemoteBody) -> dict[str, Any]:
    with _remotes_lock:
        data = load_remotes()
        if body.id and body.id not in data["remotes"]:
            raise HTTPException(404, "Unknown remote")
        data["active"] = body.id
        save_remotes(data)
    return {"active": body.id}


@app.post("/api/remotes/{rid}/test", dependencies=[Depends(require_token)])
def api_remotes_test(rid: str) -> dict[str, Any]:
    remote = _get_remote_or_404(rid)
    try:
        r = remote_request(remote, "GET", "/intg/drivers?limit=1", timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach remote: {exc}")
    if r.status_code == 401:
        raise HTTPException(401, "Authentication failed — check the PIN or API key")
    if r.status_code >= 400:
        raise HTTPException(502, f"Remote returned {r.status_code}")
    try:
        total = int(r.headers.get("Pagination-Count"))
    except (TypeError, ValueError):
        total = None
    return {"ok": True, "driver_count": total}


@app.get("/api/remotes/{rid}/drivers", dependencies=[Depends(require_token)])
def api_remotes_drivers(rid: str) -> Any:
    remote = _get_remote_or_404(rid)
    try:
        return remote_all_drivers(remote)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Remote error: {exc}")


@app.post("/api/remotes/{rid}/register", dependencies=[Depends(require_token)])
def api_remotes_register(rid: str, body: RegisterBody) -> dict[str, Any]:
    remote = _get_remote_or_404(rid)
    rec = load_state()["integrations"].get(body.integration_id)
    if rec is None:
        raise HTTPException(404, "Instance is not installed")
    entry = find_integration(rec.get("integration_id", body.integration_id)) or {}
    port = int(rec.get("port"))
    advertise = body.advertise_ip or remote.get("advertise_ip") or detect_host_ip(remote["host"])
    if not advertise:
        raise HTTPException(
            400, "Could not determine this host's IP — set an advertise IP on the remote"
        )
    payload = build_driver_payload(entry, rec, advertise, port)
    post_status: int | None = None
    post_text = ""
    post_error: str | None = None
    try:
        r = remote_request(remote, "POST", "/intg/drivers", json_body=payload, timeout=20.0)
        post_status = r.status_code
        post_text = (r.text or "")[:300]
    except Exception as exc:  # noqa: BLE001
        post_error = str(exc)

    with _remote_drivers_lock:
        _remote_drivers_cache.pop(rid, None)

    # The remote's POST status is unreliable — it can return 500 yet still register
    # the driver. Verify by polling and treat a confirmed-present driver as success.
    confirmed, state = False, None
    try:
        for _ in range(6):
            time.sleep(1.0)
            for d in remote_all_drivers(remote):
                if isinstance(d, dict) and d.get("driver_id") == payload["driver_id"]:
                    confirmed = True
                    state = d.get("driver_state") or d.get("state")
                    break
            if confirmed and state in ("CONNECTED", "IDLE", None):
                break
    except Exception:  # noqa: BLE001
        pass

    # 409 = already registered, which for our purposes is "it's there".
    succeeded = confirmed or post_status in (200, 201, 204, 409)
    if not succeeded:
        if post_error:
            raise HTTPException(502, f"Could not reach remote: {post_error}")
        if post_status == 401:
            raise HTTPException(401, "Authentication failed — check the PIN or API key")
        raise HTTPException(502, f"Remote returned {post_status}: {post_text}")

    record_event("register", body.integration_id,
                 f"registered {payload['driver_id']} on {remote.get('name', rid)}"
                 + (f" ({state})" if state else ""))
    return {"ok": True, "driver_id": payload["driver_id"], "driver_url": payload["driver_url"],
            "confirmed": confirmed, "driver_state": state, "remote_status": post_status}


@app.delete("/api/remotes/{rid}/drivers/{driver_id}", dependencies=[Depends(require_token)])
def api_remotes_unregister(rid: str, driver_id: str) -> dict[str, str]:
    remote = _get_remote_or_404(rid)
    try:
        r = remote_request(remote, "DELETE", f"/intg/drivers/{driver_id}", timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach remote: {exc}")
    if r.status_code in (200, 204):
        with _remote_drivers_lock:
            _remote_drivers_cache.pop(rid, None)
        record_event("register", None, f"unregistered {driver_id} from {remote.get('name', rid)}")
        return {"status": "unregistered", "driver_id": driver_id}
    raise HTTPException(502, f"Remote returned {r.status_code}")


# ---- diagnostics, settings portability, registration preflight --------------


def _tcp_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


@app.get("/api/remotes/{rid}/registration-preflight/{instance_id}", dependencies=[Depends(require_token)])
def api_registration_preflight(rid: str, instance_id: str) -> dict[str, Any]:
    remote = _get_remote_or_404(rid)
    rec = load_state()["integrations"].get(instance_id)
    if not rec:
        raise HTTPException(404, "Instance is not installed")
    entry = find_integration(rec.get("integration_id", instance_id)) or {}
    advertise = remote.get("advertise_ip") or detect_host_ip(remote["host"])
    payload = build_driver_payload(entry, rec, advertise or "0.0.0.0", int(rec.get("port") or 0))
    issues: list[dict[str, Any]] = []
    reachable = True
    drivers: list[dict[str, Any]] = []
    try:
        drivers = remote_all_drivers(remote)
    except Exception as exc:
        reachable = False
        issues.append({"code": "remote_unreachable", "severity": "error", "message": f"Remote unreachable: {exc}"})
    if rec.get("status") not in (None, "running"):
        issues.append({"code": "integration_stopped", "severity": "error", "message": "Integration is not running"})
    if not advertise:
        issues.append({"code": "advertise_ip_missing", "severity": "error", "message": "Advertise IP could not be determined"})
    if advertise and not _tcp_reachable(advertise, int(rec.get("port") or 0)):
        issues.append({"code": "port_unreachable", "severity": "warning", "message": f"Integration port {rec.get('port')} is not reachable on {advertise}"})
    same_id = next((d for d in drivers if d.get("driver_id") == payload["driver_id"]), None)
    same_url = next((d for d in drivers if d.get("driver_url") == payload["driver_url"]), None)
    if same_id:
        issues.append({"code": "driver_id_exists", "severity": "warning", "message": "Driver ID is already registered", "driver": same_id})
    if same_url and not same_id:
        issues.append({"code": "driver_url_exists", "severity": "warning", "message": "The same driver URL is already registered", "driver": same_url})
    return {"ok": reachable and not any(i["severity"] == "error" for i in issues), "reachable": reachable,
            "driver_id": payload["driver_id"], "driver_url": payload["driver_url"], "issues": issues}


@app.get("/api/diagnostics", dependencies=[Depends(require_token)])
def api_diagnostics() -> dict[str, Any]:
    docker_version = None
    disk = shutil.disk_usage(DATA_DIR)
    try:
        docker_version = get_docker().version().get("Version")
    except Exception:
        pass
    remotes = load_remotes()
    remote_summary = []
    for rid, r in remotes.get("remotes", {}).items():
        ok = False
        try:
            ok = remote_request(r, "GET", "/intg/drivers?limit=1", timeout=2.0).status_code < 400
        except Exception:
            pass
        remote_summary.append({"id": rid, "name": r.get("name"), "reachable": ok})
    recent_errors = [e for e in load_events(200) if e.get("kind") == "error"][:20]
    return {
        "installer_version": app.version,
        "python_version": platform.python_version(),
        "docker_version": docker_version,
        "data_dir": str(DATA_DIR),
        "service_unit": service_unit_val(),
        "registry_url": registry_url_val(),
        "registry_commit": registry_commit(),
        "active_jobs": sum(1 for j in JOBS.values() if j.status == "running"),
        "installed_integrations": len(load_state().get("integrations", {})),
        "remotes": remote_summary,
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "recent_errors": recent_errors,
    }


@app.get("/api/settings/export", dependencies=[Depends(require_token)])
def api_settings_export() -> dict[str, Any]:
    remotes = load_remotes()
    safe_remotes = []
    for r in remotes.get("remotes", {}).values():
        m = _mask(r)
        m.pop("has_pin", None); m.pop("has_api_key", None)
        safe_remotes.append(m)
    return {"version": 1, "settings": {k:v for k,v in load_settings().items() if k != "token"},
            "alerts": {"events": load_alert_settings().get("events", {})},
            "remotes": safe_remotes}


@app.post("/api/settings/import", dependencies=[Depends(require_token)])
def api_settings_import(body: SettingsImportBody) -> dict[str, Any]:
    payload = body.payload or {}
    settings = payload.get("settings") or {}
    allowed = {k:v for k,v in settings.items() if k in {"port_start","registry_url","update_repo","update_branch","update_service","health_probe"}}
    save_settings(allowed)
    alerts = payload.get("alerts") or {}
    if isinstance(alerts.get("events"), dict):
        cur = load_alert_settings(); save_alert_settings(cur.get("webhook", ""), alerts["events"])
    record_event("state", None, "settings imported")
    return {"ok": True, "imported": list(allowed)}


@app.post("/api/service/restart", dependencies=[Depends(require_token)])
def api_service_restart() -> dict[str, Any]:
    if not _can_restart_service():
        raise HTTPException(409, f"Service restart is not available; restart {service_unit_val()} manually")
    record_event("state", None, f"installer service restart requested: {service_unit_val()}")
    subprocess.Popen(["systemd-run", "--no-block", "--collect", "systemctl", "restart", service_unit_val()],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "service": service_unit_val()}


@app.put("/api/instances/{instance_id}/update-policy", dependencies=[Depends(require_token)])
def api_update_policy(instance_id: str, body: UpdatePolicyBody) -> dict[str, Any]:
    if body.mode not in {"off", "notify", "stable", "prerelease", "scheduled"}:
        raise HTTPException(400, "Invalid update policy")
    with _state_lock:
        state = load_state(); rec = state.get("integrations", {}).get(instance_id)
        if not rec: raise HTTPException(404, "Instance is not installed")
        rec["update_policy"] = {"mode": body.mode, "delay_days": max(0, int(body.delay_days)),
                                "maintenance_window": body.maintenance_window.strip()}
        rec["auto_update"] = body.mode in {"stable", "prerelease", "scheduled"}
        save_state(state)
    record_event("state", instance_id, f"update policy changed to {body.mode}")
    return rec["update_policy"]


# ---- events, backup, maintenance -------------------------------------------


@app.get("/api/events", dependencies=[Depends(require_token)])
def api_events(limit: int = 100) -> dict[str, Any]:
    return {"events": load_events(limit)}


@app.get("/api/backup", dependencies=[Depends(require_token)])
def api_backup():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if STATE_FILE.exists():
            tar.add(str(STATE_FILE), arcname="state.json")
        if CONFIG_DIR.exists():
            tar.add(str(CONFIG_DIR), arcname="config")
    buf.seek(0)
    fn = "uc-installer-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".tar.gz"
    return StreamingResponse(
        buf, media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@app.post("/api/restore", dependencies=[Depends(require_token)])
async def api_restore(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            _safe_extract(tar, DATA_DIR, only="state.json")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            _safe_extract(tar, DATA_DIR, only="config")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Invalid backup archive: {exc}")
    record_event("state", None, "restored config + state from backup")
    return {"status": "restored", "note": "Rebuild instances to recreate their containers."}


@app.get("/api/instances/{instance_id}/backup", dependencies=[Depends(require_token)])
def api_instance_backup(instance_id: str):
    rec = load_state()["integrations"].get(instance_id)
    if rec is None:
        raise HTTPException(404, "Instance is not installed")
    cfg = CONFIG_DIR / instance_id
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = json.dumps(rec, indent=2).encode()
        ti = tarfile.TarInfo(f"{instance_id}/instance.json"); ti.size = len(meta)
        tar.addfile(ti, io.BytesIO(meta))
        if cfg.exists():
            tar.add(str(cfg), arcname=f"{instance_id}/config")
    buf.seek(0)
    fn = f"{instance_id}-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".tar.gz"
    return StreamingResponse(
        buf, media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@app.post("/api/instances/{instance_id}/restore", dependencies=[Depends(require_token)])
async def api_instance_restore(instance_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    if instance_id not in load_state()["integrations"]:
        raise HTTPException(404, "Instance is not installed")
    data = await file.read()
    dest = CONFIG_DIR / instance_id
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            members = [m for m in tar.getmembers() if "config/" in m.name or m.name.endswith("/config")]
            base = dest.resolve()
            for m in members:
                # strip everything up to and including the "config/" segment
                rel = m.name.split("config/", 1)[1] if "config/" in m.name else ""
                if not rel:
                    continue
                target = (dest / rel).resolve()
                if not str(target).startswith(str(base)):
                    continue
                m.name = rel
                tar.extract(m, dest)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Invalid backup archive: {exc}")
    record_event("state", instance_id, "restored instance config from backup")
    return {"status": "restored", "note": "Restart or rebuild the instance to apply."}



class ReconcileOptions(BaseModel):
    adopt: bool = True
    remove_stale: bool = False
    restart_stopped: bool = False


class PruneOptions(BaseModel):
    unused_local: bool = True
    dangling: bool = True
    build_cache: bool = False

@app.post("/api/maintenance/prune", dependencies=[Depends(require_token)])
def api_prune(options: PruneOptions | None = None) -> dict[str, Any]:
    options = options or PruneOptions()
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    client = get_docker()
    in_use = {rec.get("image") for rec in load_state()["integrations"].values()}
    removed: list[str] = []
    reclaimed = 0
    if options.unused_local:
        try:
            for img in client.images.list():
                tags = img.tags or []
                local = [t for t in tags if t.startswith("uc-local/")]
                if local and not any(t in in_use for t in tags):
                    try:
                        client.images.remove(img.id, force=True)
                        removed.extend(local)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
    if options.dangling:
        try:
            reclaimed += int((client.images.prune(filters={"dangling": True}) or {}).get("SpaceReclaimed", 0))
        except Exception:  # noqa: BLE001
            pass
    if options.build_cache:
        try:
            reclaimed += int((client.api.prune_builds() or {}).get("SpaceReclaimed", 0))
        except Exception:  # noqa: BLE001
            pass
    record_event("state", None, f"pruned {len(removed)} build image(s), reclaimed {reclaimed} bytes")
    return {"removed": removed, "space_reclaimed": reclaimed}


@app.post("/api/maintenance/reconcile", dependencies=[Depends(require_token)])
def api_reconcile(options: ReconcileOptions | None = None) -> dict[str, Any]:
    options = options or ReconcileOptions()
    before = set(load_state()["integrations"])
    if options.adopt:
        reconcile_state()
    state = load_state()
    adopted = len(set(state["integrations"]) - before)
    stale_removed = 0
    started = 0
    try:
        client = get_docker()
        existing = {c.name: c for c in client.containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=managed"})}
        if options.remove_stale:
            for iid in list(state["integrations"]):
                if iid not in existing:
                    state["integrations"].pop(iid, None)
                    stale_removed += 1
            if stale_removed:
                save_state(state)
        if options.restart_stopped:
            for iid, container in existing.items():
                if iid in state["integrations"] and container.status != "running":
                    try:
                        container.start()
                        started += 1
                    except Exception:  # noqa: BLE001
                        pass
    except Exception:  # noqa: BLE001
        pass
    record_event("state", None, f"reconciled containers: {adopted} adopted, {stale_removed} stale removed, {started} started")
    return {"status": "ok", "adopted": adopted, "stale_removed": stale_removed, "started": started}


@app.get("/api/installer/logs", dependencies=[Depends(require_token)])
def api_installer_logs(lines: int = 500) -> dict[str, str]:
    """The installer's own service logs (journalctl)."""
    unit = service_unit_val()
    if not shutil.which("journalctl"):
        return {"logs": "journalctl is not available on this host — the installer's "
                        "logs live wherever its process output is captured "
                        f"(e.g. `docker logs`, or the console if run manually).",
                "source": "none"}
    try:
        p = subprocess.run(
            ["journalctl", "-u", unit, "--no-pager", "-n", str(max(1, min(lines, 5000)))],
            capture_output=True, text=True, timeout=15,
        )
        out = (p.stdout or "") + (p.stderr or "")
        if not out.strip():
            out = f"(no journal entries for unit '{unit}')"
        return {"logs": out, "source": "journalctl", "unit": unit}
    except Exception as exc:  # noqa: BLE001
        return {"logs": f"Could not read the journal: {exc}", "source": "error"}


@app.get("/api/installer/logs/stream", dependencies=[Depends(require_token)])
def api_installer_logs_stream(lines: int = 200):
    unit = service_unit_val()
    if not shutil.which("journalctl"):
        def unavailable():
            yield "data: journalctl is not available on this host\n\n"
        return StreamingResponse(unavailable(), media_type="text/event-stream")

    def gen():
        proc = subprocess.Popen(
            ["journalctl", "-u", unit, "--no-pager", "-n", str(max(1, min(lines, 2000))), "-f"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                yield f"data: {line.rstrip()}\n\n"
        except Exception:  # noqa: BLE001
            return
        finally:
            proc.terminate()

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/install-archive", dependencies=[Depends(require_token)])
async def api_install_archive(file: UploadFile = File(...)) -> dict[str, str]:
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    data = await file.read()
    job = new_job("archive-install", file.filename or "archive")
    threading.Thread(target=do_install_archive, args=(data, file.filename or "archive.tar.gz", job),
                     daemon=True).start()
    return {"job_id": job.id}


# ---- notification settings + monitor ---------------------------------------

_alert_state: dict[str, str] = {}
_notified_updates: set[str] = set()
_autoupdate_inflight: set[str] = set()


def _auto_update_instance(iid: str, target: str) -> None:
    try:
        rec = load_state()["integrations"].get(iid)
        if rec is None:
            return
        record_event("update", iid,
                     f"auto-updating {rec.get('label', iid)} to {target}")
        job = new_job("auto-update", iid)
        do_rebuild(rec, job, version=target)
    except Exception as exc:  # noqa: BLE001
        record_event("error", iid, f"auto-update failed: {exc}")
    finally:
        _autoupdate_inflight.discard(iid)


class AlertSettingsBody(BaseModel):
    webhook: str = ""
    events: dict[str, bool] = {}


class SetupBody(BaseModel):
    port_start: int | None = None
    registry_url: str | None = None
    update_repo: str | None = None
    update_branch: str | None = None
    update_service: str | None = None
    health_probe: bool | None = None
    token: str | None = None
    webhook: str | None = None
    events: dict[str, bool] | None = None
    complete: bool = True


@app.get("/api/setup", dependencies=[Depends(require_token)])
def api_get_setup() -> dict[str, Any]:
    s = load_settings()
    a = load_alert_settings()
    return {
        "setup_complete": s["setup_complete"],
        "port_start": s["port_start"],
        "registry_url": s["registry_url"],
        "update_repo": s["update_repo"],
        "update_branch": s["update_branch"],
        "update_service": s["update_service"],
        "health_probe": s["health_probe"],
        "token": s.get("token", ""),
        "webhook": a["webhook"],
        "events": a["events"],
        "categories": NOTIFY_CATEGORIES,
        # read-only, set at install time (env / systemd unit)
        "bind_host": os.environ.get("UC_INSTALLER_HOST", "0.0.0.0"),
        "bind_port": int(os.environ.get("UC_INSTALLER_PORT", "8900")),
        "data_dir": str(DATA_DIR),
    }


@app.post("/api/setup", dependencies=[Depends(require_token)])
def api_post_setup(body: SetupBody) -> dict[str, Any]:
    patch: dict[str, Any] = {"setup_complete": bool(body.complete)}
    if body.port_start:
        patch["port_start"] = int(body.port_start)
    for field in ("registry_url", "update_repo", "update_branch", "update_service", "token"):
        val = getattr(body, field)
        if val is not None:
            patch[field] = val.strip()
    if body.health_probe is not None:
        patch["health_probe"] = bool(body.health_probe)
    s = save_settings(patch)
    if body.webhook is not None or body.events is not None:
        cur = load_alert_settings()
        save_alert_settings(
            body.webhook if body.webhook is not None else cur["webhook"],
            body.events if body.events is not None else cur["events"],
        )
    record_event("state", None, "installer settings changed")
    return {"setup_complete": s["setup_complete"], "port_start": s["port_start"]}


@app.get("/api/settings/alerts", dependencies=[Depends(require_token)])
def api_get_alerts() -> dict[str, Any]:
    s = load_alert_settings()
    return {"webhook": s["webhook"], "events": s["events"], "categories": NOTIFY_CATEGORIES}


@app.put("/api/settings/alerts", dependencies=[Depends(require_token)])
def api_put_alerts(body: AlertSettingsBody) -> dict[str, Any]:
    result = save_alert_settings(body.webhook, body.events)
    record_event("state", None, "notification settings changed")
    return result


@app.post("/api/settings/alerts/test", dependencies=[Depends(require_token)])
def api_test_alert() -> dict[str, str]:
    if not load_alert_settings()["webhook"]:
        raise HTTPException(400, "Set and save a webhook URL first")
    if not _fire_webhook("Test notification from UC External Integration Installer."):
        raise HTTPException(502, "Failed to send — check the webhook URL")
    return {"status": "sent"}


def _alert_monitor() -> None:
    tick = 0
    while True:
        try:
            state = load_state()["integrations"]
            # health transitions every 60s
            for iid, rec in state.items():
                c = _container_for(iid)
                if c is None:
                    continue
                cur = (_probe_health(rec.get("port")) or "running") if c.status == "running" else c.status
                prev = _alert_state.get(iid)
                bad = cur in ("unreachable", "exited", "dead")
                was_bad = prev in ("unreachable", "exited", "dead")
                if bad and not was_bad and prev is not None:
                    record_event("alert", iid, f"{rec.get('label', iid)} is {cur}")
                _alert_state[iid] = cur
            # update-available checks ~hourly
            if tick % 60 == 0:
                for iid, rec in state.items():
                    try:
                        upd = compute_update(rec)
                    except Exception:  # noqa: BLE001
                        continue
                    if upd.get("update_available"):
                        target = upd.get("latest_version")
                        if rec.get("auto_update") and target and iid not in _autoupdate_inflight:
                            _autoupdate_inflight.add(iid)
                            threading.Thread(target=_auto_update_instance,
                                             args=(iid, target), daemon=True).start()
                        elif not rec.get("auto_update") and iid not in _notified_updates:
                            _notified_updates.add(iid)
                            record_event("update", iid,
                                         f"update available for {rec.get('label', iid)}: "
                                         f"{target} (installed {upd.get('installed_version')})")
                    else:
                        _notified_updates.discard(iid)
        except Exception:  # noqa: BLE001
            pass
        tick += 1
        time.sleep(60)


# ---- static frontend -------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse({"detail": "UI not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

registry_commit()  # warm the registry-commit cache in the background

# Adopt any orphaned managed containers into state on startup.
try:
    reconcile_state()
except Exception:  # noqa: BLE001
    pass

# Health-transition + update-available monitor (records events; notifies when a
# webhook is configured via the GUI or UC_INSTALLER_ALERT_WEBHOOK).
threading.Thread(target=_alert_monitor, daemon=True).start()

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("UC_INSTALLER_HOST", "0.0.0.0")
    port = int(os.environ.get("UC_INSTALLER_PORT", "8900"))
    if not token_val():
        print("WARNING: no access token is set — the web UI and Docker "
              "control are open to anyone who can reach this port.")
    print(f"UC External Integration Installer -> http://{host}:{port}  (data: {DATA_DIR})")
    uvicorn.run(app, host=host, port=port)
