import base64
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
import signal
import threading
from datetime import datetime
from pathlib import Path

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken
from cryptography.fernet import Fernet, InvalidToken

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "repos.yml"))
STACKS_DIR = Path(os.environ.get("STACKS_DIR", "stacks"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
STATE_PATH = Path(os.environ.get("STATE_PATH", STACKS_DIR / ".state.json"))
# the CLI and the website drop request files here (check / deploy / stop / ...);
# the watcher wakes up, handles them, and deletes them
REQUEST_DIR = Path(os.environ.get("REQUEST_DIR", STACKS_DIR / ".requests"))
# files edited in the website (.env, overrides...), copied into the clone before each deploy
LOCAL_DIR = Path(os.environ.get("LOCAL_DIR", STACKS_DIR / ".local"))

# encrypts the .env files saved from the website (LOCAL_DIR) at rest; empty = plaintext.
# generate with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENV_ENCRYPTION_KEY = os.environ.get("ENV_ENCRYPTION_KEY", "").strip()
ENC_PREFIX = "gitops-enc:v1:"

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


OVERRIDES = ("compose.override.yaml", "compose.override.yml",
             "docker-compose.override.yaml", "docker-compose.override.yml")


def compose_cmd(name: str, path: Path | None, compose_file: str | None) -> list[str]:
    """docker compose -p <name> [-f <file> [-f <override>]]"""
    cmd = ["docker", "compose", "-p", name]
    if compose_file:
        cmd += ["-f", compose_file]
        # compose only auto-loads an override file when no -f is given, so add it ourselves
        for override in OVERRIDES:
            if path is not None and (path / override).is_file():
                cmd += ["-f", override]
                break
    return cmd


def compose(
    name,
    *args,
    path: Path | None = None,
    compose_file: str | None = None,
    timeout: float = COMPOSE_TIMEOUT,
):
    """Build and run: docker compose -p <name> [-f <file>] <args...>"""
    return run(compose_cmd(name, path, compose_file) + list(args), cwd=path, timeout=timeout)


# --- local files (.env encryption) ---
def is_secret(filename: str) -> bool:
    return filename == ".env" or filename.endswith(".env")


def _fernet() -> Fernet | None:
    if not ENV_ENCRYPTION_KEY:
        return None
    try:
        return Fernet(ENV_ENCRYPTION_KEY)
    except ValueError:
        raise RuntimeError("ENV_ENCRYPTION_KEY is not a valid Fernet key") from None


def encrypt_text(text: str) -> str:
    """Encrypt if a key is set, else leave as is."""
    f = _fernet()
    if not f:
        return text
    return ENC_PREFIX + f.encrypt(text.encode()).decode() + "\n"


def decrypt_text(text: str) -> str:
    """Plaintext passes through, so files saved before the key was set still work."""
    if not text.startswith(ENC_PREFIX):
        return text
    f = _fernet()
    if not f:
        raise RuntimeError("file is encrypted but ENV_ENCRYPTION_KEY is not set")
    try:
        return f.decrypt(text[len(ENC_PREFIX) :].strip().encode()).decode()
    except InvalidToken:
        raise RuntimeError("can't decrypt: wrong ENV_ENCRYPTION_KEY") from None


def read_local(path: Path) -> str:
    return decrypt_text(path.read_text(encoding="utf-8"))


def write_private(path: Path, text: str) -> None:
    """Atomic write, readable by the owner only."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)  # in case the .tmp was left over with other permissions
    os.replace(tmp, path)


def encrypt_local_files() -> None:
    """On start: encrypt .env files that were saved before the key was set."""
    if not _fernet() or not LOCAL_DIR.is_dir():
        return
    for f in LOCAL_DIR.glob("*/*"):
        if not f.is_file() or not is_secret(f.name):
            continue
        try:
            text = f.read_text(encoding="utf-8")
            if not text.startswith(ENC_PREFIX):
                write_private(f, encrypt_text(text))
                log(f.parent.name, f"encrypted {f.name}")
        except (OSError, UnicodeDecodeError) as e:
            log(f.parent.name, f"can't encrypt {f.name}: {e}")


def sync_local_files(name: str, path: Path) -> None:
    """Copy LOCAL_DIR/<name>/* into the clone, so compose sees .env, overrides, etc.
    Encrypted files are written decrypted: compose can only read plain .env files."""
    local = LOCAL_DIR / name
    if not local.is_dir() or not path.is_dir():
        return
    for f in local.iterdir():
        if not f.is_file():
            continue
        if is_secret(f.name):
            try:
                text = read_local(f)
            except RuntimeError as e:  # better to fail the deploy than ship ciphertext as .env
                raise RuntimeError(f"{f.name}: {e}") from None
            write_private(path / f.name, text)
        else:
            shutil.copy2(f, path / f.name)


def deploy(name, path, compose_file, pull: bool = True):
    log(name, "deploying...")
    sync_local_files(name, path)
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
        sync_local_files(name, path)
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
            stop(name, path, state.get(name, {}).get("compose_file"))
        except Exception as e:
            log(name, f"removed from config but can't stop it, will retry: {e}")
            return False
    return True


# --- self-update ---
#
# The watcher can't `docker compose up` its own stack: Docker would stop it halfway.
# So when its own repo has a new commit, it starts a one-off helper container that
# runs `git pull` + `docker compose up -d --build` in the checkout on the host.
# The helper isn't part of the stack, so it survives the restart.
SELF_UPDATE = os.environ.get("SELF_UPDATE", "").lower() in ("1", "true", "yes")
SELF_DIR = Path(os.environ.get("SELF_DIR", "/self"))  # the checkout, mounted read-only
SELF_REMOTE = os.environ.get("SELF_REMOTE", "origin")  # git remote in the checkout to update from
UPDATER = "gitops-watcher-updater"

# project:  compose project we run in (never managed as a repo, see main())
# host_dir: where the checkout is on the host;  image: our own image
# running:  commit we were started from;  tried: last commit an update was started for
SELF: dict = {}


def git_self(*args, timeout: float = 10) -> str:
    return run(["git", "-C", str(SELF_DIR), *args], timeout=timeout)


def detect_self() -> None:
    """Read our compose project and checkout path from our own container's labels."""
    try:
        info = json.loads(run(["docker", "inspect", socket.gethostname()]))[0]
    except Exception as e:
        log("self", f"can't inspect own container: {e}")
        return
    labels = info.get("Config", {}).get("Labels") or {}
    SELF["project"] = labels.get("com.docker.compose.project")
    SELF["host_dir"] = labels.get("com.docker.compose.project.working_dir")
    SELF["image"] = info.get("Config", {}).get("Image")
    if not SELF_UPDATE:
        return
    if not (SELF_DIR / ".git").exists() or not SELF["host_dir"]:
        log("self", f"self-update off: checkout not mounted at {SELF_DIR}")
        return
    try:
        SELF["running"] = git_self("rev-parse", "HEAD")
    except Exception as e:
        log("self", f"self-update off: {e}")
        return
    log("self", f"self-update on, running {SELF['running'][:7]} from {SELF['host_dir']}")
    # tell about a failed update from before the restart
    try:
        code = run(["docker", "inspect", "-f", "{{.State.ExitCode}}", UPDATER])
        if code != "0":
            log("self", f"last self-update failed (exit {code}): docker logs {UPDATER}")
    except Exception:
        pass


def check_self() -> None:
    """Start the updater if the watcher's own branch has a commit we aren't running."""
    if not SELF.get("running"):
        return

    def note(msg: str) -> None:  # log each skip reason once, not every round
        if SELF.get("note") != msg:
            log("self", msg)
            SELF["note"] = msg

    try:
        branch = git_self("rev-parse", "--abbrev-ref", "HEAD")
        remote = remote_hash(git_self("remote", "get-url", SELF_REMOTE), branch)
        local = git_self("rev-parse", "HEAD")
        dirty = git_self("status", "--porcelain", "--untracked-files=no")
    except Exception as e:
        note(f"can't check for updates: {e}")
        return
    if remote == SELF["running"] or remote == SELF.get("tried"):
        return  # up to date, or this commit already failed once
    if dirty:
        note(f"{SELF_REMOTE}/{branch} has {remote[:7]}, but the checkout has uncommitted changes: not updating")
        return
    if local != remote:
        try:  # remote is behind us = unpushed commits
            git_self("merge-base", "--is-ancestor", remote, local)
            note(f"the checkout has commits not pushed to {SELF_REMOTE}/{branch}: not updating")
            return
        except Exception:
            pass  # behind (or diverged: `pull --ff-only` will refuse)

    try:
        state = run(["docker", "inspect", "-f", "{{.State.Running}}", UPDATER])
        if state == "true":
            return  # an update is already running
        run(["docker", "rm", UPDATER])
    except RuntimeError:
        pass  # no old updater

    owner = SELF_DIR.stat()
    docker_gid = os.stat("/var/run/docker.sock").st_gid
    host_dir = SELF["host_dir"]
    script = (f"git pull --ff-only {shlex.quote(SELF_REMOTE)} {shlex.quote(branch)} && "
              f"docker compose -p {shlex.quote(SELF['project'])} up -d --build")
    try:
        run([
            "docker", "run", "-d", "--name", UPDATER,
            # as the checkout's owner, so pulled files aren't root-owned
            "--user", f"{owner.st_uid}:{owner.st_gid}", "--group-add", str(docker_gid),
            "-e", "HOME=/tmp", "-e", "GIT_TERMINAL_PROMPT=0",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{host_dir}:{host_dir}", "-w", host_dir,
            "--entrypoint", "sh", SELF["image"], "-c", script,
        ], timeout=60)
    except Exception as e:
        note(f"can't start the updater: {e}")
        return
    SELF["tried"] = remote
    SELF["note"] = None
    log("self", f"updating to {remote[:7]}, the watcher restarts in a moment "
                f"(progress: docker logs -f {UPDATER})")


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
    repo.setdefault("paused", False)
    return repo


def ensure_config() -> None:
    """Create an empty repos.yml on first start, so there's something to add repos to."""
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        "# repos the watcher deploys. Add with `cli.py add` or the website.\n"
        "defaults:\n  branch: main\n\nrepos: []\n"
    )
    log("watcher", f"created empty {CONFIG_PATH}")


# round-trip YAML keeps comments and layout when repos.yml is edited by code
rt_yaml = YAML()
rt_yaml.indent(mapping=2, sequence=4, offset=2)
rt_yaml.preserve_quotes = True


def load_config_rt():
    try:
        with CONFIG_PATH.open() as f:
            data = rt_yaml.load(f)
    except FileNotFoundError:
        data = None
    if data is None:
        data = rt_yaml.load("repos: []\n")
    if data.get("repos") is None:
        data["repos"] = rt_yaml.load("[]")
    return data


def save_config_rt(data) -> None:
    # write in place (no temp file + rename): keeps the file's owner/permissions on the
    # host, and still works if repos.yml is bind-mounted as a single file
    with CONFIG_PATH.open("w") as f:
        rt_yaml.dump(data, f)


def find_index(repos, name: str) -> int | None:
    return next(
        (i for i, r in enumerate(repos) if isinstance(r, dict) and r.get("name") == name),
        None,
    )


# ruamel stores the blank lines/comments *after* a list entry on that entry's
# last key, so they have to be moved by hand when entries are added or removed
def _trailing(entry):
    tok = entry.ca.items.get(list(entry.keys())[-1])
    return tok[2] if tok and len(tok) > 2 else None


def _set_trailing(entry, token) -> None:
    tok = entry.ca.items.setdefault(list(entry.keys())[-1], [None, None, None, None])
    tok[2] = token


def append_entry(repos, entry: dict) -> None:
    """Append with a blank line before it, like the hand-written entries."""
    if hasattr(repos, "fa"):
        repos.fa.set_block_style()  # `repos: []` would otherwise stay on one line
    if repos and hasattr(repos[-1], "ca") and _trailing(repos[-1]) is None:
        _set_trailing(repos[-1], CommentToken("\n\n", CommentMark(0), None))
    repos.append(entry)


def delete_entry(repos, idx: int) -> None:
    """Delete an entry, keeping the comment that sits above the entry after it."""
    after = _trailing(repos[idx]) if hasattr(repos[idx], "ca") else None
    if idx > 0 and hasattr(repos[idx - 1], "ca"):
        _set_trailing(repos[idx - 1], after)
    elif after is not None:
        text = "\n".join(
            l.strip().lstrip("#").strip() for l in after.value.splitlines() if l.strip()
        )
        if text:
            repos.yaml_set_start_comment(text, indent=2)
    del repos[idx]


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


# --- requests (from cli.py and the website) ---
#
# A request is a JSON file in REQUEST_DIR: {"action": ..., "repo": <name>, ...}
#   check   clear the retry backoff; the round then deploys if there's a new commit
#   deploy  like check, but redeploy even if nothing changed
#   stop    pause the repo in repos.yml and `docker compose down` it
#   start   unpause it; the round brings it back up
#   add     append {"entry": {...}} to repos.yml
#   remove  `docker compose down` (+ volumes and files if "wipe_volumes"), drop from repos.yml
ACTIONS = ("check", "deploy", "stop", "start", "add", "remove")


def queue_request(action: str, repo: str, **extra) -> None:
    req = {"action": action, "repo": repo,
           "created": datetime.now().isoformat(timespec="seconds"), **extra}
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    path = REQUEST_DIR / f"{time.time_ns()}-{action}-{repo}.json"
    # write then rename, so the watcher never reads a half-written file
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(req, indent=2))
    tmp.replace(path)


def request_check(name: str, force: bool = False) -> None:
    queue_request("deploy" if force else "check", name)


def has_requests() -> bool:
    return REQUEST_DIR.is_dir() and any(REQUEST_DIR.glob("*.json"))


def set_paused(name: str, paused: bool) -> None:
    data = load_config_rt()
    idx = find_index(data["repos"], name)
    if idx is None:
        raise RuntimeError(f"no repo named {name!r} in {CONFIG_PATH}")
    if paused:
        data["repos"][idx]["paused"] = True
    else:
        data["repos"][idx].pop("paused", None)
    save_config_rt(data)


def handle_request(req: dict, state: dict) -> None:
    action = req.get("action")
    name = str(req.get("repo") or "")
    if action not in ACTIONS:
        raise RuntimeError(f"unknown action {action!r}")
    if not NAME_RE.match(name):
        raise RuntimeError(f"invalid repo name {name!r}")
    if name == SELF.get("project") and action != "add":
        raise RuntimeError(f"{name} is the watcher's own stack, it can't {action} itself")
    log(name, f"request: {action}")

    if action == "add":
        entry = req.get("entry")
        if not isinstance(entry, dict) or entry.get("name") != name:
            raise RuntimeError("add request has no valid entry")
        config = load_config()
        normalize(entry, config.get("defaults") or {})  # raises if invalid
        data = load_config_rt()
        if find_index(data["repos"], name) is not None:
            raise RuntimeError(f"{name} is already in {CONFIG_PATH}")
        append_entry(data["repos"], entry)
        save_config_rt(data)
        log(name, "added; deploys this round" + (" (paused)" if entry.get("paused") else ""))
        return

    config = load_config()
    raw = next((r for r in config.get("repos") or []
                if isinstance(r, dict) and r.get("name") == name), None)
    if raw is None:
        raise RuntimeError(f"no repo named {name!r} in {CONFIG_PATH}")
    repo = normalize(raw, config.get("defaults") or {})
    path = STACKS_DIR / name

    if action in ("check", "deploy", "start"):
        if action == "start":
            set_paused(name, False)
        if name in state:
            record(state, name, failed_commit=None, retry_at=None)
            if action == "deploy":
                state[name].pop("commit", None)  # forces a redeploy this round

    elif action == "stop":
        set_paused(name, True)
        if path.exists():
            stop(name, path, repo["compose_file"])
        record(state, name, status="paused", error=None)

    elif action == "remove":
        wipe = bool(req.get("wipe_volumes"))
        if path.exists():
            log(name, "stopping...")
            compose(name, "down", "--remove-orphans", *(["-v"] if wipe else []),
                    path=path, compose_file=repo["compose_file"])
        data = load_config_rt()
        idx = find_index(data["repos"], name)
        if idx is not None:
            delete_entry(data["repos"], idx)
            save_config_rt(data)
        if wipe:
            shutil.rmtree(path, ignore_errors=True)
            shutil.rmtree(LOCAL_DIR / name, ignore_errors=True)
        state.pop(name, None)
        log(name, "removed" + (" (volumes and files deleted)" if wipe else ""))


def process_requests(state: dict) -> None:
    """Handle every queued request, oldest first. Each file is deleted once handled."""
    if not REQUEST_DIR.is_dir():
        return
    for f in sorted(REQUEST_DIR.glob("*.json")):
        req = None
        try:
            req = json.loads(f.read_text() or "{}")
            if not isinstance(req, dict):
                raise RuntimeError("request is not a JSON object")
            handle_request(req, state)
        except Exception as e:
            name = req.get("repo") if isinstance(req, dict) else None
            log(str(name or f.name), f"request failed: {e}")
            if name in state:
                record(state, name, status="error", error=f"request failed: {e}",
                       last_check=datetime.now().isoformat(timespec="seconds"))
        finally:
            f.unlink(missing_ok=True)


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
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    log("watcher", f"started, checking every {CHECK_INTERVAL}s")
    # only on a fresh install: if stacks are running and repos.yml went missing,
    # an empty one would make the watcher stop all of them
    if not state:
        ensure_config()

    detect_self()
    try:
        encrypt_local_files()
    except RuntimeError as e:  # bad key: deploys will report it per repo
        log("watcher", str(e))

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    while not STOP.is_set():
        round_start = time.time()
        process_requests(state)
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
            if name == SELF.get("project"):
                # deploying/stopping it would stop the watcher itself mid-way
                msg = "this is the watcher's own stack: remove it from repos.yml (it updates itself)"
                if state.get(name, {}).get("error") != msg:
                    log(name, msg)
                record(state, name, status="error", error=msg, last_check=now)
                continue
            if repo["paused"]:
                # paused by hand in repos.yml (the stop request already did this): bring it down once
                if state.get(name, {}).get("status") != "paused":
                    path = STACKS_DIR / name
                    try:
                        if path.exists():
                            stop(name, path, repo["compose_file"])
                        record(state, name, status="paused", error=None, last_check=now)
                    except Exception as e:
                        errors += 1
                        log(name, f"paused but can't stop it: {e}")
                        record(state, name, status="error", error=str(e), last_check=now)
                continue

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
            for old in [n for n in state if n not in seen and n != SELF.get("project")]:
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

        if not STOP.is_set():
            check_self()

        try:
            save_state(state)
        except OSError as e:
            log("watcher", f"can't save state: {e}")

        wait_for_next_round(max(0, CHECK_INTERVAL - elapsed))

    log("watcher", "stopped")


if __name__ == "__main__":
    main()
