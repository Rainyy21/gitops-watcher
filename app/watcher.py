import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "repos.yml"))
STACKS_DIR = Path(os.environ.get("STACKS_DIR", "stacks"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))


# --- helpers ---
def log(name: str, msg: str) -> None:
    timestamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    print(f"[{timestamp}] [{name}] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


# --- git ---


def remote_hash(url: str, branch: str) -> str:
    out = run(["git", "ls-remote", url, f"refs/heads/{branch}"])
    if not out:
        raise RuntimeError(f"branch '{branch}' not found")

    return out.split()[0]


def local_hash(path: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=path)


def clone(url: str, path: Path, branch: str) -> None:
    run(["git", "clone", "--branch", branch, "--depth", "1", url, str(path)])


def update(path: Path, branch: str) -> None:
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


def deploy(name, path, compose_file):
    log(name, "deploying...")
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

# commit hash last deployed per repo, so a failed deploy is retried next check
deployed: dict[str, str] = {}


def check_repo(repo):
    name = repo["name"]
    path = STACKS_DIR / name
    url = repo["url"]
    branch = repo.get("branch", "main")
    compose_file = repo.get("compose_file")

    try:
        remote = remote_hash(url, branch)
        if deployed.get(name) == remote:
            return

        if not (path / ".git").exists():
            log(name, "not cloned yet, cloning...")
            shutil.rmtree(path, ignore_errors=True)
            clone(url, path, branch)
        else:
            local = local_hash(path)
            if local != remote:
                log(name, f"update found {local[:7]} -> {remote[:7]}")
                update(path, branch)

        deploy(name, path, compose_file)
        deployed[name] = local_hash(path)
    except Exception as e:
        log(name, f"error: {e}")


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
    return repo


# --- main loop ---
def main() -> None:
    STACKS_DIR.mkdir(parents=True, exist_ok=True)
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
                check_repo(repo)
            except Exception as e:
                log(name, f"error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
