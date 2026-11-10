import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "repos.yml"))
STACKS_DIR = Path(os.environ.get("STACKS_DIR", "stacks"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
STATE_PATH = Path(os.environ.get("STATE_PATH", STACKS_DIR / ".state.json"))


# --- helpers ---
def log(name: str, msg: str) -> None:
    timestamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    print(f"[{timestamp}] [{name}] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        msg = f"{' '.join(cmd)} failed:\n{result.stderr.strip()}"
        raise RuntimeError(redact(msg))
    return result.stdout.strip()


def redact(text: str) -> str:
    return re.sub(r"https://[^@/\s]+@", "https://***@", text)


# --- git ---


def auth_url(repo: dict) -> str | None:
    url = repo["url"]
    token_env = repo.get("token_env")
    if not token_env:
        return None
    token = os.environ.get(token_env)
    if not token:
        raise RuntimeError(f"env var {token_env} is not set")
    if not url.startswith("https://"):
        raise RuntimeError("token auth only works with https:// URLs")

    return url.replace("https://", f"https://{token}@", 1)


def remote_hash(url: str, branch: str) -> str:
    out = run(["git", "ls-remote", url, f"refs/heads/{branch}"])
    if not out:
        raise RuntimeError(f"branch '{branch}' not found")

    return out.split()[0]


def local_hash(path: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=path)


def clone(url: str, path: Path, branch: str) -> None:
    run(["git", "clone", "--branch", branch, "--depth", "1", url, str(path)])


def update(url: str, path: Path, branch: str) -> None:
    # keep origin in sync with the config, e.g. after a token rotation
    run(["git", "remote", "set-url", "origin", url], cwd=path)
    # explicit refspec so origin/<branch> exists even if the branch changed since the clone
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    run(["git", "fetch", "--depth", "1", "origin", refspec], cwd=path)
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=path)


# --- docker ---


def compose(name, *args, path: Path | None = None, compose_file: str | None = None):
    """Build and run: docker compose -p <name> [-f <file>] <args...>"""
    cmd = ["docker", "compose", "-p", name]
    if compose_file:
        cmd += ["-f", compose_file]
    return run(cmd + list(args), cwd=path)


def deploy(name, path, compose_file, pull: bool = True):
    log(name, "deploying...")
    if pull:
        pull_image(name, path, compose_file)
    compose(
        name,
        "up",
        "-d",
        "--build",
        "--remove-orphans",
        path=path,
        compose_file=compose_file,
    )
    log(name, "deployed ✓")


def stop(name, path, compose_file):
    log(name, "stopping...")
    compose(name, "down", path=path, compose_file=compose_file)
    log(name, "stopped")


def pull_image(name, path, compose_file):
    return compose(
        name, "pull", "--ignore-buildable", path=path, compose_file=compose_file
    )


def status(name, path, compose_file):
    out = compose(
        name, "ps", "--all", "--format", "json", path=path, compose_file=compose_file
    )
    if not out:
        return []
    if out.startswith("["):
        return json.loads(out)
    return [json.loads(line) for line in out.splitlines()]


def logs(name, path, compose_file, tail: int = 50):
    return compose(
        name, "logs", "--tail", str(tail), path=path, compose_file=compose_file
    )


def restart(name, path, compose_file):
    compose(name, "restart", path=path, compose_file=compose_file)
    log(name, "restarted")


# -- core logic ---


def check_repo(repo, state: dict):
    name = repo["name"]
    path = STACKS_DIR / name
    branch = repo["branch"]
    compose_file = repo["compose_file"]
    current = state.get(name, {})

    try:
        url = auth_url(repo) or repo["url"]
        remote = remote_hash(url, branch)

        if current.get("commit") == remote and (path / ".git").exists():
            # no new commit: refresh images / restart stopped containers.
            # `up -d` only recreates containers whose image or config changed.
            if repo["pull_image"]:
                pull_image(name, path, compose_file)
            compose(name, "up", "-d", "--remove-orphans", path=path, compose_file=compose_file)
            if current.get("status") != "ok":
                record(state, name, status="ok", error=None)
            return

        if not (path / ".git").exists():
            log(name, "not cloned yet, cloning...")
            shutil.rmtree(path, ignore_errors=True)
            clone(url, path, branch)
        else:
            local = local_hash(path)
            if local != remote:
                log(name, f"update found {local[:7]} -> {remote[:7]}")
                update(url, path, branch)

        deploy(name, path, compose_file, repo["pull_image"])
        record(
            state,
            name,
            commit=local_hash(path),
            deployed_at=f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            status="ok",
            error=None,
        )
    except Exception as e:
        log(name, f"error: {e}")
        record(state, name, status="error", error=str(e))


# --- config ---
def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


def normalize(raw: dict, defaults: dict) -> dict:
    repo = {**defaults, **raw}

    for key in ("name", "url"):
        if not repo.get(key):
            raise ValueError(f"repo entry missing '{key}': {raw}")

    repo.setdefault("branch", "main")
    repo.setdefault("compose_file", None)
    repo.setdefault("pull_image", True)
    return repo


# --- state ---
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log("watcher", f"state file unreadable, starting fresh: {e}")
        return {}


def save_state(state: dict) -> None:
    # write to a temp file then rename, so a crash mid-write can't corrupt it
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def record(state: dict, name: str, **fields) -> None:
    state.setdefault(name, {}).update(fields)
    try:
        save_state(state)
    except OSError as e:
        log("watcher", f"can't save state: {e}")


# --- main loop ---
def main() -> None:
    STACKS_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    log("watcher", f"started, checking every {CHECK_INTERVAL}s")

    while True:
        try:
            config = load_config()
        except Exception as e:
            log("watcher", f"can't read config: {e}")
            time.sleep(CHECK_INTERVAL)
            continue
        defaults = config.get("defaults") or {}
        for raw in config.get("repos") or []:
            name = raw.get("name", "?")
            try:
                repo = normalize(raw, defaults)
                check_repo(repo, state)
            except Exception as e:
                log(name, f"error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
