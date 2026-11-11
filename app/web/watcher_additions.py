"""
WATCHER SIDE: paste these into watcher.py so the website's buttons do something.

The website only writes request files and local files. These functions make
the watcher:
  - copy data/local/<repo>/ into the clone before every deploy
  - add docker-compose.override.yml when it exists
  - skip paused repos
  - process request files (deploy / stop / start / remove / add)

Each block says where it goes. Function names match the ones you already have:
run, log, compose, deploy, check_repo, local_hash, load_config, normalize,
record, STACKS_DIR, LOCAL_DIR, CONFIG_PATH.
"""

# =====================================================================
# 1. TOP OF FILE: new import + constant
# =====================================================================

import shutil   # add next to your other imports

REQUESTS_DIR = Path(os.environ.get("REQUESTS_DIR", "data/requests"))


# =====================================================================
# 2. CONFIG SECTION: save_config (repos.yml is now written by the watcher)
#    Note: yaml.safe_dump does NOT keep comments in repos.yml.
# =====================================================================

def save_config(config: dict) -> None:
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)
    os.replace(tmp, CONFIG_PATH)


#    and in normalize(), add one more fallback:
#        repo.setdefault("paused", False)


# =====================================================================
# 3. DOCKER SECTION: replace compose() and deploy() with these
# =====================================================================

OVERRIDE = "docker-compose.override.yml"


def compose(name: str, *args: str, path: Path | None = None,
            compose_file: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    cmd = ["docker", "compose", "-p", name]
    if compose_file:
        cmd += ["-f", compose_file]
        # Compose only auto-loads an override when no -f is given, so add it ourselves
        if path is not None and (path / OVERRIDE).is_file():
            cmd += ["-f", OVERRIDE]
    return run(cmd + list(args), cwd=path, timeout=timeout)


def sync_local_files(name: str, path: Path) -> None:
    """Copy data/local/<name>/* into the clone, so compose sees .env, overrides, etc."""
    local = LOCAL_DIR / name
    if not local.is_dir():
        return
    for f in local.iterdir():
        if f.is_file():
            shutil.copy2(f, path / f.name)


def deploy(repo: dict, path: Path) -> None:
    name = repo["name"]
    log(name, "deploying...")
    sync_local_files(name, path)                      # NEW: before anything reads the files
    if repo["pull_images"]:
        pull_images(name, path, repo["compose_file"])
    compose(name, "up", "-d", "--build", "--remove-orphans",
            path=path, compose_file=repo["compose_file"], timeout=BUILD_TIMEOUT)
    log(name, "deployed ✓")

# (DEFAULT_TIMEOUT / BUILD_TIMEOUT are the timeout constants from the timeouts step.
#  If you named them differently, use your names.)


# =====================================================================
# 4. NEW SECTION "requests" (put it before main)
# =====================================================================

def find_entry(config: dict, name: str) -> dict | None:
    for entry in config.get("repos") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def handle_request(req: dict, state: dict) -> None:
    action = req.get("action")
    name = req.get("repo", "")
    config = load_config()
    config["repos"] = config.get("repos") or []
    now = datetime.now().isoformat(timespec="seconds")

    if action == "add":
        if find_entry(config, name):
            raise RuntimeError(f"{name} already exists")
        config["repos"].append(req["entry"])
        save_config(config)
        log(name, "added from web UI")
        return                                        # the normal round clones + deploys it

    entry = find_entry(config, name)
    if entry is None:
        raise RuntimeError(f"no repo named {name!r}")
    repo = normalize(entry, config.get("defaults") or {})
    path = STACKS_DIR / name

    if action in ("deploy", "start"):
        if action == "start":
            entry["paused"] = False
            save_config(config)
            repo["paused"] = False
        if (path / ".git").exists():
            deploy(repo, path)
            commit = local_hash(path)
        else:
            commit = check_repo(repo)["commit"]       # clones + deploys
        record(state, name, status="ok", error=None, commit=commit,
               last_check=now, last_deploy=now)

    elif action == "stop":
        entry["paused"] = True
        save_config(config)
        if path.exists():
            compose(name, "down", "--remove-orphans")
        log(name, "stopped (paused)")

    elif action == "remove":
        wipe = bool(req.get("wipe_volumes"))
        if path.exists():
            compose(name, "down", "--remove-orphans", *(["-v"] if wipe else []))
        config["repos"] = [e for e in config["repos"]
                           if not (isinstance(e, dict) and e.get("name") == name)]
        save_config(config)
        shutil.rmtree(path, ignore_errors=True)
        if wipe:
            shutil.rmtree(LOCAL_DIR / name, ignore_errors=True)
        state.pop(name, None)
        log(name, "removed" + (" (volumes + local files deleted)" if wipe else ""))

    else:
        raise RuntimeError(f"unknown action {action!r}")


def process_requests(state: dict) -> None:
    """Run every queued request, oldest first. Each file is deleted once handled."""
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(REQUESTS_DIR.glob("*.json")):
        req = {}
        try:
            req = json.loads(f.read_text())
            log(req.get("repo", "?"), f"request: {req.get('action')}")
            handle_request(req, state)
        except Exception as e:
            name = req.get("repo", "?") if isinstance(req, dict) else "?"
            log(name, f"request failed: {e}")
            record(state, name, status="error", error=f"request failed: {e}",
                   last_check=datetime.now().isoformat(timespec="seconds"))
        finally:
            f.unlink(missing_ok=True)
    save_state(state)


# =====================================================================
# 5. main(): three small changes
# =====================================================================
#
#  a) at the START of each round, before load_config():
#
#         process_requests(state)
#
#  b) in the per-repo loop, right after `repo = normalize(raw, defaults)`:
#
#         if repo["paused"]:
#             continue
#
#  c) so the website reacts fast: wait in short steps and wake up early
#     when a request file shows up. Replace the end-of-round wait with:
#
#         deadline = time.monotonic() + max(0, CHECK_INTERVAL - elapsed)
#         while not STOP.is_set() and time.monotonic() < deadline:
#             if any(REQUESTS_DIR.glob("*.json")):
#                 break
#             STOP.wait(2)
#
#     (a full round then runs: requests first, then the normal checks)
