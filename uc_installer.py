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

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
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
LABEL_NAME = "uc.integration.name"
LABEL_SOURCE = "uc.integration.source"  # "ghcr" or "build"
LABEL_PORT = "uc.integration.port"

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
REGISTRY_CACHE = DATA_DIR / "registry.json"
for d in (APPS_DIR, CONFIG_DIR):
    d.mkdir(parents=True, exist_ok=True)


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
    driver_id = (entry.get("driver_id") or rec.get("id") or entry.get("id"))
    name = entry.get("name") or rec.get("name") or driver_id
    payload: dict[str, Any] = {
        "driver_id": driver_id,
        "name": {"en": name},
        "version": entry.get("version") or "1.0.0",
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
            resp = httpx.get(REGISTRY_URL, timeout=20.0, follow_redirects=True)
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
    m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/(.+)", REGISTRY_URL)
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
    latest_pub = stable[0].get("published_at") if stable else None

    available = False
    if latest_tag:
        if installed in ("latest", "", None):
            # tracking a moving tag: flag if a release landed after we installed
            inst_at = rec.get("installed_at")
            if latest_pub and inst_at:
                available = latest_pub > inst_at
        else:
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


def next_free_port() -> int:
    used = set()
    state = load_state()
    for rec in state["integrations"].values():
        try:
            used.add(int(rec.get("port")))
        except (TypeError, ValueError):
            continue
    port = PORT_START
    while port in used:
        port += 1
    return port


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


def _build_image(entry: dict[str, Any], job: Job, version: str = "latest") -> tuple[str, str | None]:
    """Clone + build. Returns (image_tag, entrypoint).

    Prefers the project's own Dockerfile — its author knows the correct entrypoint,
    port and dependencies. Only falls back to the generic Dockerfile (which has to
    *guess* the entrypoint) when the repo ships none. entrypoint is returned only
    for the generic path; for a repo Dockerfile the image's own CMD is used.
    """
    integration_id = entry["id"]
    repo = entry["repository"]
    app_dir = APPS_DIR / integration_id
    clone_or_update(repo, app_dir, job.log, ref=version)

    safe = re.sub(r"[^a-z0-9_.-]", "-", (version or "latest").lower())
    tag = f"uc-local/{integration_id}:{safe}"
    client = get_docker()

    repo_dockerfile = _find_repo_dockerfile(app_dir)
    if repo_dockerfile:
        dockerfile = repo_dockerfile
        entrypoint: str | None = None  # use the image's own CMD/ENTRYPOINT
        job.log(f"Using the project's own {repo_dockerfile}")
    else:
        (app_dir / "Dockerfile.external").write_text(GENERIC_DOCKERFILE)
        (app_dir / "docker-entry.external.sh").write_text(GENERIC_ENTRY)
        dockerfile = "Dockerfile.external"
        entrypoint = detect_entrypoint(app_dir)
        job.log("No Dockerfile in repo — using generic build. "
                f"Detected entrypoint: {entrypoint or '(auto at runtime)'}")

    job.log(f"Building {tag} ...")
    for chunk in client.api.build(
        path=str(app_dir),
        dockerfile=dockerfile,
        tag=tag,
        rm=True,
        pull=True,
        decode=True,
    ):
        if "stream" in chunk:
            text = chunk["stream"].strip()
            if text:
                job.log(text)
        elif "error" in chunk:
            raise RuntimeError(chunk["error"])
    job.log(f"Built {tag}")
    return tag, entrypoint


def _run_container(
    entry: dict[str, Any], image: str, source: str, port: int,
    extra_env: dict[str, str], entrypoint: str | None, job: Job,
    version: str = "latest",
):
    integration_id = entry["id"]
    cfg = CONFIG_DIR / integration_id
    cfg.mkdir(parents=True, exist_ok=True)

    env = base_environment(port, entrypoint)
    env.update(extra_env or {})

    # remove any previous container with the same name
    existing = _container_for(integration_id)
    if existing is not None:
        job.log("Removing previous container ...")
        existing.remove(force=True)

    job.log(f"Starting container '{integration_id}' on port {port} ...")
    get_docker().containers.run(
        image,
        name=integration_id,
        detach=True,
        network_mode="host",
        # on-failure (not unless-stopped) so a container that can't start settles
        # into "exited" and is visibly broken, instead of masquerading as a
        # permanent "restarting" crash loop.
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
        environment=env,
        volumes={str(cfg): {"bind": "/config", "mode": "rw"}},
        labels={
            LABEL_MANAGED: "managed",
            LABEL_ID: integration_id,
            LABEL_NAME: entry.get("name", integration_id),
            LABEL_SOURCE: source,
            LABEL_PORT: str(port),
        },
    )

    prev = load_state()["integrations"].get(integration_id, {})
    record_integration({
        "id": integration_id,
        "name": entry.get("name", integration_id),
        "driver_id": entry.get("driver_id") or integration_id,
        "repository": entry.get("repository", ""),
        "image": image,
        "source": source,
        "port": port,
        "env": extra_env or {},
        "entrypoint": entrypoint or "",
        "version": version or "latest",
        "installed_at": prev.get("installed_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    job.log("Done.")


def do_install(entry: dict[str, Any], port: int, extra_env: dict[str, str],
               job: Job, version: str = "latest"):
    try:
        version = version or "latest"
        image = image_from_repo(entry["repository"], version)
        source = "ghcr"
        entrypoint = None
        if not _pull_image(image, job):
            image, entrypoint = _build_image(entry, job, version)
            source = "build"
        _run_container(entry, image, source, port, extra_env, entrypoint, job, version)
        job.finish("success")
    except Exception as exc:  # noqa: BLE001
        job.finish("error", f"ERROR: {exc}")


def do_reconfigure(entry: dict[str, Any], port: int, extra_env: dict[str, str], job: Job):
    """Re-run with new port/env, reusing the already-resolved image."""
    try:
        state = load_state()
        rec = state["integrations"].get(entry["id"], {})
        image = rec.get("image") or image_from_repo(entry["repository"])
        source = rec.get("source", "ghcr")
        entrypoint = rec.get("entrypoint") or None
        version = rec.get("version", "latest")
        _run_container(entry, image, source, port, extra_env, entrypoint, job, version)
        job.finish("success")
    except Exception as exc:  # noqa: BLE001
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
    api = f"https://api.github.com/repos/{_owner_repo_from_url(UPDATE_REPO)}/commits/{UPDATE_BRANCH}"
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


def do_update(job: Job) -> None:
    try:
        job.log(f"Repository: {UPDATE_REPO}  (branch {UPDATE_BRANCH})")
        job.log(f"Install directory: {APP_DIR}")

        if not _is_git_repo():
            job.log("Not a git checkout yet — attaching the repository in place ...")
            _run_git_logged(job, "init", "-q")
            if _git("remote", "add", "origin", UPDATE_REPO).returncode != 0:
                _run_git_logged(job, "remote", "set-url", "origin", UPDATE_REPO)

        _run_git_logged(job, "fetch", "--depth", "1", "origin", UPDATE_BRANCH)
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
            job.finish("success", f"Update complete — restarting {SERVICE_UNIT} now.")
            time.sleep(2)  # let the UI fetch the final log before we go down
            subprocess.Popen(
                ["systemd-run", "--no-block", "--collect",
                 "systemctl", "restart", SERVICE_UNIT],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            job.finish(
                "success",
                f"Update complete. Restart to apply: systemctl restart {SERVICE_UNIT} "
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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def require_token(authorization: str | None = Header(default=None),
                  token: str | None = Query(default=None)) -> None:
    if not TOKEN:
        return
    supplied = None
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    supplied = supplied or token
    if supplied != TOKEN:
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
        "token_required": bool(TOKEN),
        "port_start": PORT_START,
        "data_dir": str(DATA_DIR),
        "build": current_commit().get("short"),
        "registry_commit": registry_commit(),
    }


@app.get("/api/update/status", dependencies=[Depends(require_token)])
def api_update_status() -> dict[str, Any]:
    cur = current_commit()
    info: dict[str, Any] = {
        "repo": UPDATE_REPO,
        "branch": UPDATE_BRANCH,
        "service": SERVICE_UNIT,
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


@app.post("/api/update/apply", dependencies=[Depends(require_token)])
def api_update_apply() -> dict[str, str]:
    if not shutil.which("git"):
        raise HTTPException(500, "git is not installed on the host")
    job = new_job("update", "self")
    threading.Thread(target=do_update, args=(job,), daemon=True).start()
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
    entry = find_integration(integration_id)
    if entry is None:
        raise HTTPException(404, "Unknown integration")
    if not is_installable(entry):
        raise HTTPException(400, "This integration has no installable repository")
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    port = body.port or next_free_port()
    job = new_job("install", integration_id)
    threading.Thread(
        target=do_install, args=(entry, port, body.env, job, body.version or "latest"), daemon=True
    ).start()
    return {"job_id": job.id}


@app.post("/api/integrations/{integration_id}/config", dependencies=[Depends(require_token)])
def api_config(integration_id: str, body: ConfigBody) -> dict[str, str]:
    entry = find_integration(integration_id)
    if entry is None:
        raise HTTPException(404, "Unknown integration")
    rec = load_state()["integrations"].get(integration_id)
    if rec is None:
        raise HTTPException(404, "Integration is not installed")
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    port = body.port or int(rec.get("port") or next_free_port())
    job = new_job("reconfigure", integration_id)
    threading.Thread(
        target=do_reconfigure, args=(entry, port, body.env, job), daemon=True
    ).start()
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
def api_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job")
    return job.to_dict()


def _lifecycle(integration_id: str, action: str) -> dict[str, str]:
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    c = _container_for(integration_id)
    if c is None:
        raise HTTPException(404, "Container not found")
    getattr(c, action)()
    return {"status": "ok", "action": action}


@app.post("/api/integrations/{integration_id}/start", dependencies=[Depends(require_token)])
def api_start(integration_id: str):
    return _lifecycle(integration_id, "start")


@app.post("/api/integrations/{integration_id}/stop", dependencies=[Depends(require_token)])
def api_stop(integration_id: str):
    return _lifecycle(integration_id, "stop")


@app.post("/api/integrations/{integration_id}/restart", dependencies=[Depends(require_token)])
def api_restart(integration_id: str):
    return _lifecycle(integration_id, "restart")


@app.delete("/api/integrations/{integration_id}", dependencies=[Depends(require_token)])
def api_remove(integration_id: str, purge: bool = False):
    if docker_available():
        c = _container_for(integration_id)
        if c is not None:
            c.remove(force=True)
    forget_integration(integration_id)
    if purge:
        shutil.rmtree(CONFIG_DIR / integration_id, ignore_errors=True)
        shutil.rmtree(APPS_DIR / integration_id, ignore_errors=True)
    return {"status": "removed", "purged": purge}


@app.get("/api/integrations/{integration_id}/logs", dependencies=[Depends(require_token)])
def api_logs(integration_id: str, tail: int = 300):
    if not docker_available():
        raise HTTPException(503, "Docker is not available")
    c = _container_for(integration_id)
    if c is None:
        raise HTTPException(404, "Container not found")
    logs = c.logs(tail=tail, timestamps=False).decode("utf-8", "replace")
    return {"logs": logs}


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


@app.get("/api/registrations", dependencies=[Depends(require_token)])
def api_registrations() -> dict[str, list[dict[str, str]]]:
    """Map of installed integration id -> the remotes it's registered on."""
    remotes = load_remotes()["remotes"]
    state = load_state()["integrations"]
    result: dict[str, list[dict[str, str]]] = {iid: [] for iid in state}
    for rid, remote in remotes.items():
        present = {
            d.get("driver_id") for d in _remote_drivers(remote) if isinstance(d, dict)
        }
        for iid, rec in state.items():
            driver_id = rec.get("driver_id") or (find_integration(iid) or {}).get("driver_id") or iid
            if driver_id in present:
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
        raise HTTPException(404, "Integration is not installed")
    entry = find_integration(body.integration_id) or {}
    port = int(rec.get("port"))
    advertise = body.advertise_ip or remote.get("advertise_ip") or detect_host_ip(remote["host"])
    if not advertise:
        raise HTTPException(
            400, "Could not determine this host's IP — set an advertise IP on the remote"
        )
    payload = build_driver_payload(entry, rec, advertise, port)
    try:
        r = remote_request(remote, "POST", "/intg/drivers", json_body=payload, timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach remote: {exc}")
    if r.status_code in (200, 201):
        with _remote_drivers_lock:
            _remote_drivers_cache.pop(rid, None)
        return {"ok": True, "driver_id": payload["driver_id"], "driver_url": payload["driver_url"]}
    if r.status_code == 401:
        raise HTTPException(401, "Authentication failed — check the PIN or API key")
    if r.status_code == 409:
        raise HTTPException(409, f"Driver '{payload['driver_id']}' is already registered on this remote")
    raise HTTPException(502, f"Remote returned {r.status_code}: {r.text[:300]}")


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
        return {"status": "unregistered", "driver_id": driver_id}
    raise HTTPException(502, f"Remote returned {r.status_code}")


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

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("UC_INSTALLER_HOST", "0.0.0.0")
    port = int(os.environ.get("UC_INSTALLER_PORT", "8900"))
    if not TOKEN:
        print("WARNING: UC_INSTALLER_TOKEN is not set — the web UI and Docker "
              "control are open to anyone who can reach this port.")
    print(f"UC External Integration Installer -> http://{host}:{port}  (data: {DATA_DIR})")
    uvicorn.run(app, host=host, port=port)
