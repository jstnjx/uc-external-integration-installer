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
import subprocess
import sys
import threading
import time
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


PLACEHOLDER_REPO = "https://github.com/unfoldedcircle/"


def is_installable(entry: dict[str, Any]) -> bool:
    repo = (entry.get("repository") or "").strip()
    return bool(repo) and repo != PLACEHOLDER_REPO and repo.startswith("http")


def image_from_repo(repo: str) -> str:
    """ghcr.io/<owner>/<name>:latest derived from a GitHub repo url."""
    r = repo.strip()
    r = re.sub(r"^https?://github\.com/", "", r)
    r = re.sub(r"\.git$", "", r)
    return f"ghcr.io/{r.lower()}:latest"


def owner_repo(repo: str) -> tuple[str, str]:
    r = re.sub(r"^https?://github\.com/", "", repo.strip())
    r = re.sub(r"\.git$", "", r)
    parts = r.split("/")
    return (parts[0], parts[1]) if len(parts) >= 2 else ("", r)


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


def clone_or_update(repo: str, app_dir: Path, log) -> None:
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


def _build_image(entry: dict[str, Any], job: Job) -> tuple[str, str | None]:
    """Clone + build. Returns (image_tag, entrypoint).

    Prefers the project's own Dockerfile — its author knows the correct entrypoint,
    port and dependencies. Only falls back to the generic Dockerfile (which has to
    *guess* the entrypoint) when the repo ships none. entrypoint is returned only
    for the generic path; for a repo Dockerfile the image's own CMD is used.
    """
    integration_id = entry["id"]
    repo = entry["repository"]
    app_dir = APPS_DIR / integration_id
    clone_or_update(repo, app_dir, job.log)

    tag = f"uc-local/{integration_id}:latest"
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

    record_integration({
        "id": integration_id,
        "name": entry.get("name", integration_id),
        "repository": entry.get("repository", ""),
        "image": image,
        "source": source,
        "port": port,
        "env": extra_env or {},
        "entrypoint": entrypoint or "",
        "installed_at": datetime.now(timezone.utc).isoformat(),
    })
    job.log("Done.")


def do_install(entry: dict[str, Any], port: int, extra_env: dict[str, str], job: Job):
    try:
        image = image_from_repo(entry["repository"])
        source = "ghcr"
        entrypoint = None
        if not _pull_image(image, job):
            image, entrypoint = _build_image(entry, job)
            source = "build"
        _run_container(entry, image, source, port, extra_env, entrypoint, job)
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
        _run_container(entry, image, source, port, extra_env, entrypoint, job)
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


class ConfigBody(BaseModel):
    port: int | None = None
    env: dict[str, str] = {}


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
        target=do_install, args=(entry, port, body.env, job), daemon=True
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

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("UC_INSTALLER_HOST", "0.0.0.0")
    port = int(os.environ.get("UC_INSTALLER_PORT", "8900"))
    if not TOKEN:
        print("WARNING: UC_INSTALLER_TOKEN is not set — the web UI and Docker "
              "control are open to anyone who can reach this port.")
    print(f"UC External Integration Installer -> http://{host}:{port}  (data: {DATA_DIR})")
    uvicorn.run(app, host=host, port=port)
