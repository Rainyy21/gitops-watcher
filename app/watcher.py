import base64
import json
import os
import re
import shutil
import subprocess
import time
import signal
import threading
from datetime import datetime
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "repos.yml"))
STACKS_DIR = Path(os.environ.get("STACKS_DIR", "stacks"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
STATE_PATH = Path(os.environ.get("STATE_PATH", STACKS_DIR / ".state.json"))
# `cli.py check` drops a file here to make the watcher check a repo right away
REQUEST_DIR = Path(os.environ.get("REQUEST_DIR", STACKS_DIR / ".requests"))

# seconds before a command is killed
GIT_TIMEOUT = int(os.environ.get("GIT_TIMEOUT", "300"))  # clone / fetch
DEPLOY_TIMEOUT = int(os.environ.get("DEPLOY_TIMEOUT", "1800"))  # build / pull
COMPOSE_TIMEOUT = 120  # up / down / restart

# after a failed deploy, wait CHECK_INTERVAL, then 2x, 4x... up to this before retrying the same commit
MAX_BACKOFF = int(os.environ.get("MAX_BACKOFF", "3600"))

# docker compose project names: lowercase letters, digits, '-' and '_'
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# fail instead of waiting for a username/password prompt
os.environ["GIT_TERMINAL_PROMPT"] = "0"

STOP = threading.Event()


# --- helpers ---
def log(name: str, msg: str) -> None:
    timestamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    print(f"[{timestamp}] [{name}] {msg}", flush=True)


def handle_stop(signum, frame) -> None:
    log("watcher", "stop requested, finishing current repo...")
    STOP.set()


def run(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: float = 10,
    env: dict | None = None,
) -> str:
    if env:
        env = {**os.environ, **env}
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        msg = f"{' '.join(cmd)} timed out after {timeout}s"
        raise RuntimeError(redact(msg)) from None
    if result.returncode != 0:
        msg = f"{' '.join(cmd)} failed:\n{result.stderr.strip()}"
        raise RuntimeError(redact(msg))
    return result.stdout.strip()


def redact(text: str) -> str:
    return re.sub(r"https://[^@/\s]+@", "https://***@", text)


# --- git ---


def git_auth_env(repo: dict) -> dict | None:
    """Env vars that make git send the token as an auth header.

    Keeps the token out of the command line (visible in `ps`) and out of .git/config.
    """
    token_env = repo.get("token_env")
    if not token_env:
        return None
    token = os.environ.get(token_env)
    if not token:
        raise RuntimeError(f"env var {token_env} is not set")
    if not repo["url"].startswith("https://"):
        raise RuntimeError("token auth only works with https:// URLs")

    # same scheme GitHub Actions uses; Gitea accepts it too
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def remote_hash(url: str, branch: str, env: dict | None = None) -> str:
    out = run(["git", "ls-remote", url, f"refs/heads/{branch}"], env=env)
    if not out:
        raise RuntimeError(f"branch '{branch}' not found")

    return out.split()[0]


def local_hash(path: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=path)


def clone(url: str, path: Path, branch: str, env: dict | None = None) -> None:
    run(
        ["git", "clone", "--branch", branch, "--depth", "1", url, str(path)],
        timeout=GIT_TIMEOUT,
        env=env,
    )


def update(path: Path, branch: str, env: dict | None = None) -> None:
    # explicit refspec so origin/<branch> exists even if the branch changed since the clone
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    run(
        ["git", "fetch", "--depth", "1", "origin", refspec],
        cwd=path,
        timeout=GIT_TIMEOUT,
        env=env,
    )
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=path)


# --- docker ---


def compose(
    name,
    *args,
    path: Path | None = None,
    compose_file: str | None = None,
    timeout: float = COMPOSE_TIMEOUT,
):
    """Build and run: docker compose -p <name> [-f <file>] <args...>"""
    cmd = ["docker", "compose", "-p", name]
    if compose_file:
        cmd += ["-f", compose_file]
    return run(cmd + list(args), cwd=path, timeout=timeout)


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
        timeout=DEPLOY_TIMEOUT,
    )
    log(name, "deployed ✓")


def stop(name, path, compose_file):
    log(name, "stopping...")
    compose(name, "down", path=path, compose_file=compose_file)
    log(name, "stopped")


def pull_image(name, path, compose_file):
    return compose(
        name,
        "pull",
        "--ignore-buildable",
        path=path,
        compose_file=compose_file,
        timeout=DEPLOY_TIMEOUT,
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


class DeployFailed(Exception):
    """Syncing or deploying a specific commit failed; retried with backoff."""

    def __init__(self, commit: str, cause: Exception):
        super().__init__(str(cause))
        self.commit = commit


def check_repo(repo, state: dict) -> dict:
    """Sync one repo. Returns {"commit", "deployed", "skipped"}; errors propagate to the caller."""
    name = repo["name"]
    path = STACKS_DIR / name
    url = repo["url"]
    branch = repo["branch"]
    compose_file = repo["compose_file"]
    current = state.get(name, {})

    env = git_auth_env(repo)
    remote = remote_hash(url, branch, env)

    if current.get("failed_commit") == remote and time.time() < current.get(
        "retry_at", 0
    ):
        return {"commit": current.get("commit"), "deployed": False, "skipped": True}

    cloned = (path / ".git").exists()
    if cloned:
        # keep origin in sync with the config; also strips tokens left in old clones' URLs
        run(["git", "remote", "set-url", "origin", url], cwd=path)

    if current.get("commit") == remote and cloned:
        # no new commit: refresh images / restart stopped containers.
        # `up -d` only recreates containers whose image or config changed.
        if repo["pull_image"]:
            pull_image(name, path, compose_file)
        compose(
            name, "up", "-d", "--remove-orphans", path=path, compose_file=compose_file
        )
        return {"commit": remote, "deployed": False, "skipped": False}

    try:
        if not cloned:
            log(name, "not cloned yet, cloning...")
            shutil.rmtree(path, ignore_errors=True)
            clone(url, path, branch, env)
        else:
            local = local_hash(path)
            if local != remote:
                log(name, f"update found {local[:7]} -> {remote[:7]}")
                update(path, branch, env)

        deploy(name, path, compose_file, repo["pull_image"])
    except Exception as e:
        raise DeployFailed(remote, e) from e
    return {"commit": local_hash(path), "deployed": True, "skipped": False}


def remove_stack(name: str, state: dict) -> bool:
    """Stop a stack that was removed from the config. Returns True once it's gone.

    The stack's folder is left on disk, since it may hold bind-mounted data.
    """
    path = STACKS_DIR / name
    if path.exists():
        try:
            stop(name, path, state[name].get("compose_file"))
        except Exception as e:
            log(name, f"removed from config but can't stop it, will retry: {e}")
            return False
    return True


# --- config ---
def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


def normalize(raw, defaults: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"repo entry is not a mapping: {raw!r}")
    repo = {**defaults, **raw}

    for key in ("name", "url"):
        if not repo.get(key):
            raise ValueError(f"repo entry missing '{key}': {raw}")
    if not NAME_RE.match(str(repo["name"])):
        raise ValueError(
            f"invalid name '{repo['name']}': use lowercase letters, digits, '-' and '_'"
        )

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
    # in memory only; main() saves once per loop
    state.setdefault(name, {}).update(fields)


# --- check requests (from cli.py) ---
def request_check(name: str, force: bool = False) -> None:
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    (REQUEST_DIR / f"{name}.json").write_text(json.dumps({"force": force}))


def has_requests() -> bool:
    return REQUEST_DIR.is_dir() and any(REQUEST_DIR.glob("*.json"))


def apply_requests(state: dict) -> None:
    """Clear backoff for requested repos (and the deployed commit, if forced)."""
    if not REQUEST_DIR.is_dir():
        return
    for f in REQUEST_DIR.glob("*.json"):
        name = f.stem
        try:
            force = json.loads(f.read_text() or "{}").get("force", False)
        except (json.JSONDecodeError, OSError):
            force = False
        f.unlink(missing_ok=True)
        log(name, "check requested" + (" (forced redeploy)" if force else ""))
        if name in state:
            record(state, name, failed_commit=None, retry_at=None)
            if force:
                state[name].pop("commit", None)


def wait_for_next_round(seconds: float) -> None:
    """Sleep until the next round, waking early on stop or a check request."""
    deadline = time.time() + seconds
    while not STOP.is_set() and time.time() < deadline:
        if has_requests():
            return
        STOP.wait(min(1, max(0, deadline - time.time())))


# --- main loop ---
def main() -> None:
    STACKS_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    log("watcher", f"started, checking every {CHECK_INTERVAL}s")

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    while not STOP.is_set():
        round_start = time.time()
        apply_requests(state)
        checked = deployed = errors = 0
        try:
            config = load_config()
        except Exception as e:
            log("watcher", f"can't read config: {e}")
            wait_for_next_round(CHECK_INTERVAL)
            continue
        defaults = config.get("defaults") or {}
        entries = config.get("repos") or []

        seen: set[str] = set()
        config_ok = True
        for i, raw in enumerate(entries):
            if STOP.is_set():  # don't start another repo
                break

            label = (
                raw.get("name")
                if isinstance(raw, dict) and raw.get("name")
                else f"repos[{i}]"
            )
            try:
                repo = normalize(raw, defaults)
            except ValueError as e:
                log(str(label), f"invalid entry, skipped: {e}")
                config_ok = False
                continue
            name = repo["name"]
            if name in seen:
                log(name, f"duplicate name, repos[{i}] skipped")
                config_ok = False
                continue
            seen.add(name)

            now = datetime.now().isoformat(timespec="seconds")
            checked += 1
            try:
                result = check_repo(repo, state)
                record(state, name, last_check=now)
                if result["skipped"]:
                    continue
                record(
                    state,
                    name,
                    status="ok",
                    error=None,
                    commit=result["commit"],
                    compose_file=repo["compose_file"],
                    failed_commit=None,
                    failures=0,
                    retry_at=None,
                )
                if result["deployed"]:
                    record(state, name, last_deploy=now)
                    deployed += 1
            except DeployFailed as e:
                errors += 1
                prev = state.get(name, {})
                failures = (
                    prev.get("failures", 0) + 1
                    if prev.get("failed_commit") == e.commit
                    else 1
                )
                delay = min(CHECK_INTERVAL * 2 ** (failures - 1), MAX_BACKOFF)
                log(
                    name,
                    f"deploy of {e.commit[:7]} failed ({failures}x), retrying in {delay}s: {e}",
                )
                record(
                    state,
                    name,
                    last_check=now,
                    status="error",
                    error=str(e),
                    failed_commit=e.commit,
                    failures=failures,
                    retry_at=time.time() + delay,
                )
            except Exception as e:
                errors += 1
                log(name, f"error: {e}")
                record(state, name, last_check=now, status="error", error=str(e))

        # stop stacks removed from the config. Skipped when the config has a bad
        # entry (a typo can't take a stack down) or when stopping mid-loop
        # (`seen` would be incomplete).
        if config_ok and not STOP.is_set():
            for old in [n for n in state if n not in seen]:
                if remove_stack(old, state):
                    log(old, "removed from config, stack stopped")
                    del state[old]
        elif not config_ok:
            removed = [n for n in state if n not in seen]
            if removed:
                log(
                    "watcher",
                    f"config has invalid entries, not stopping: {', '.join(removed)}",
                )

        elapsed = time.time() - round_start
        if deployed or errors:
            log(
                "watcher",
                f"round done: {checked} repos, {deployed} deployed, "
                f"{errors} errors ({elapsed:.1f}s)",
            )

        try:
            save_state(state)
        except OSError as e:
            log("watcher", f"can't save state: {e}")

        wait_for_next_round(max(0, CHECK_INTERVAL - elapsed))

    log("watcher", "stopped")


if __name__ == "__main__":
    main()
