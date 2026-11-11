#!/usr/bin/env python3
"""
gitops-watcher web UI.

Runs as its own container. It never touches Docker: it only
  - READS   repos.yml, state.json and the clones (to show status + git files)
  - WRITES  request files (data/requests/)   -> the watcher acts on them
  - WRITES  local files   (data/local/<repo>) -> .env, overrides, etc.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

import yaml
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

# share settings and rules with the watcher (app/watcher.py, one folder up)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from watcher import (CHECK_INTERVAL, CONFIG_PATH, LOCAL_DIR,  # noqa: E402
                     REQUEST_DIR, STACKS_DIR, STATE_PATH, normalize, queue_request)

# ---------- settings ----------

TOKEN_ENVS = [t for t in os.environ.get("TOKEN_ENVS", "GITEA_TOKEN,GITHUB_TOKEN").split(",") if t]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # empty = webhooks turned off

MAX_FILE = 256 * 1024                                   # biggest file the editor will open/save
SAFE_REPO = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")   # compose project names are lowercase
SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")      # no slashes = no ../ tricks
SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
SAFE_URL = re.compile(r"^https?://[^\s@]+$")           # no "@" = no tokens pasted into URLs

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE + 64 * 1024


# ---------- reading data ----------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def load_repos() -> list[dict]:
    """repos.yml entries with defaults filled in, by the watcher's own normalize()."""
    try:
        config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    defaults = config.get("defaults") or {}
    repos = []
    for raw in config.get("repos") or []:
        try:
            repos.append(normalize(raw, defaults))
        except ValueError:
            continue  # the watcher skips (and logs) invalid entries too
    return repos


COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


def compose_file_of(repo: dict) -> str | None:
    """The compose file the watcher uses: the configured one, or what compose would auto-detect."""
    if repo["compose_file"]:
        return repo["compose_file"]
    root = STACKS_DIR / repo["name"]
    return next((n for n in COMPOSE_NAMES if (root / n).is_file()), None)


def get_repo(name: str) -> dict:
    for repo in load_repos():
        if repo["name"] == name:
            return repo
    abort(404, f"no repo named {name!r}")


def pending_requests() -> dict[str, str]:
    """{repo name: action} for requests the watcher hasn't processed yet."""
    pending = {}
    if REQUEST_DIR.is_dir():
        for f in sorted(REQUEST_DIR.glob("*.json")):
            req = load_json(f, None)
            if isinstance(req, dict) and req.get("repo"):
                pending.setdefault(req["repo"], req.get("action", "?"))
    return pending


# ---------- writing requests ----------

def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------- files ----------

def safe_path(base: Path, filename: str) -> Path:
    """Join base + filename, refusing anything that could escape base."""
    if not SAFE_FILE.match(filename) or filename in (".", ".."):
        abort(400, "bad file name")
    path = (base / filename).resolve()
    if path.parent != base.resolve():
        abort(400, "bad file name")
    return path


def is_secret(filename: str) -> bool:
    return filename == ".env" or filename.endswith(".env")


def mask(text: str) -> str:
    lines = []
    for line in text.splitlines():
        i = line.find("=")
        if i > 0 and not line.lstrip().startswith("#"):
            line = line[: i + 1] + "••••••••"
        lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def env_keys(text: str) -> set[str]:
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def missing_env(name: str) -> list[str]:
    example = STACKS_DIR / name / ".env.example"
    env = LOCAL_DIR / name / ".env"
    if not example.is_file() or not env.is_file():
        return []
    return sorted(env_keys(example.read_text()) - env_keys(env.read_text()))


def local_files(name: str) -> list[str]:
    folder = LOCAL_DIR / name
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and SAFE_FILE.match(p.name))


def git_files(repo: dict) -> list[str]:
    """Readable text files in the root of the clone (not the copied-in local ones)."""
    root = STACKS_DIR / repo["name"]
    if not root.is_dir():
        return []
    skip = set(local_files(repo["name"]))
    names = []
    for p in root.iterdir():
        if (p.is_file() and p.name not in skip and SAFE_FILE.match(p.name)
                and (not p.name.startswith(".") or p.name == ".env.example")
                and p.stat().st_size <= MAX_FILE):
            names.append(p.name)
    compose = compose_file_of(repo)
    return sorted(names, key=lambda n: (n != compose, n.lower()))


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        abort(415, "not a text file")


# ---------- CSRF (stops other websites from clicking your buttons) ----------

def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    # webhooks can't send our token; they're checked by their HMAC signature instead
    if request.method == "POST" and not request.path.startswith("/webhook/"):
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf", "")
        if not expected or not secrets.compare_digest(sent, expected):   # no session = no token = reject
            abort(400, "invalid or missing CSRF token: reload the page and try again")


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(415)
def api_errors(e):
    if request.path.startswith(("/api/", "/webhook/")):
        return jsonify(error=e.description), e.code
    return e


# ---------- template helpers ----------

@app.template_filter("ago")
def ago(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        secs = int((datetime.now() - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return iso
    if secs < 10:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return f"{secs}s ago"


@app.template_filter("pretty")
def pretty(iso: str | None) -> str:
    return iso.replace("T", " ") if iso else ""


# ---------- pages ----------

ORDER = {"error": 0, "setup": 1, "ok": 2, "new": 3, "paused": 4}


@app.get("/")
def index():
    repos = load_repos()
    state = load_json(STATE_PATH, {})
    pending = pending_requests()

    rows = []
    for repo in repos:
        name = repo["name"]
        info = state.get(name, {})
        status = "paused" if repo["paused"] else info.get("status", "new")
        if status not in ORDER:
            status = "error"
        rows.append({
            "name": name,
            "status": status,
            "commit": (info.get("commit") or "")[:7],
            "last_check": info.get("last_check"),
            "last_deploy": info.get("last_deploy"),
            "error": info.get("error") if status == "error" else None,
            "missing_env": missing_env(name),
            "busy": pending.get(name),
        })

    # repos that are queued to be added but not in repos.yml yet
    known = {r["name"] for r in rows}
    for name, action in pending.items():
        if action == "add" and name not in known:
            rows.append({"name": name, "status": "new", "commit": "", "last_check": None,
                         "last_deploy": None, "error": None, "missing_env": [], "busy": "add"})

    rows.sort(key=lambda r: (ORDER[r["status"]], r["name"]))

    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ORDER}
    checks = [r["last_check"] for r in rows if r["last_check"]]
    last_round = max(checks) if checks else None
    next_in, stale = None, False
    if last_round:
        since = (datetime.now() - datetime.fromisoformat(last_round)).total_seconds()
        next_in = max(0, int(CHECK_INTERVAL - since))
        stale = since > CHECK_INTERVAL * 2 + 60

    return render_template("index.html", rows=rows, counts=counts, total=len(rows),
                           next_in=next_in, stale=stale, last_round=last_round,
                           interval=CHECK_INTERVAL, token_envs=TOKEN_ENVS)


# ---------- actions (all become request files) ----------

@app.post("/repos/<name>/<action>")
def repo_action(name: str, action: str):
    if action not in ("deploy", "stop", "start"):
        abort(404)
    get_repo(name)
    queue_request(action, name)
    flash(f"queued: {action} {name} · runs on the watcher's next round")
    return redirect(url_for("index"))


@app.post("/repos/<name>/remove")
def remove_repo(name: str):
    get_repo(name)
    if request.form.get("confirm", "") != name:
        flash("the name didn't match, so nothing was removed", "error")
        return redirect(url_for("index"))
    wipe = request.form.get("wipe") == "on"
    queue_request("remove", name, wipe_volumes=wipe)
    flash(f"queued: remove {name}" + (" + delete its data" if wipe else ""))
    return redirect(url_for("index"))


@app.post("/repos")
def add_repo():
    f = request.form
    name = f.get("name", "").strip()
    url = f.get("url", "").strip()
    branch = f.get("branch", "").strip()              # empty = repos.yml default
    compose_file = f.get("compose_file", "").strip()  # empty = let compose find it
    token_env = f.get("token_env", "").strip()

    problems = []
    if not SAFE_REPO.match(name):
        problems.append("name: lowercase letters, numbers, - and _ only")
    elif any(r["name"] == name for r in load_repos()) or pending_requests().get(name) == "add":
        problems.append(f"a repo named {name} already exists")
    if not SAFE_URL.match(url):
        problems.append("url: must be http(s) with no username or token in it")
    if branch and not SAFE_BRANCH.match(branch):
        problems.append("branch name has invalid characters")
    if compose_file and not SAFE_FILE.match(compose_file):
        problems.append("compose file must be a plain file name")
    if token_env and token_env not in TOKEN_ENVS:
        problems.append("unknown token")
    if problems:
        for p in problems:
            flash(p, "error")
        return redirect(url_for("index"))

    entry = {"name": name, "url": url}
    if branch:
        entry["branch"] = branch
    if compose_file:
        entry["compose_file"] = compose_file
    if token_env:
        entry["token_env"] = token_env
    if f.get("pull_image") != "on":
        entry["pull_image"] = False   # the watcher's default is True
    if f.get("deploy_now") != "on":
        entry["paused"] = True

    queue_request("add", name, entry=entry)
    flash(f"queued: add {name}")
    return redirect(url_for("index"))


# ---------- file editor API (used by app.js) ----------

@app.get("/api/version")
def version():
    """A cheap fingerprint of everything the dashboard shows.
    The page polls this and reloads only when it changes."""
    def mtime(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0
    pending = pending_requests()
    raw = f"{mtime(STATE_PATH)}|{mtime(Path(CONFIG_PATH))}|{sorted(pending.items())}"
    return jsonify(version=hashlib.sha1(raw.encode()).hexdigest()[:16], busy=bool(pending))


@app.get("/api/repos/<name>/files")
def list_files(name: str):
    repo = get_repo(name)
    local = local_files(name)
    return jsonify(local=local, git=git_files(repo), compose_file=compose_file_of(repo),
                   missing_env=missing_env(name))


@app.get("/api/repos/<name>/files/<kind>/<filename>")
def read_file(name: str, kind: str, filename: str):
    repo = get_repo(name)
    if kind == "local":
        base = LOCAL_DIR / name
    elif kind == "git":
        if filename not in git_files(repo):
            abort(404, "not a viewable repo file")
        base = STACKS_DIR / name
    else:
        abort(404)
    path = safe_path(base, filename)
    if not path.is_file():
        abort(404, "file not found")
    text = read_text(path)
    secret = is_secret(filename)
    reveal = request.args.get("reveal") == "1"
    return jsonify(content=mask(text) if secret and not reveal else text,
                   secret=secret, masked=secret and not reveal)


@app.post("/api/repos/<name>/files/<filename>")
def save_file(name: str, filename: str):
    repo = get_repo(name)
    data = request.get_json(silent=True) or {}
    content = data.get("content")
    if not isinstance(content, str):
        abort(400, "missing content")
    if len(content.encode()) > MAX_FILE:
        abort(413, "file too large")
    if filename.endswith((".yml", ".yaml")):
        if "\t" in content:
            return jsonify(error="YAML can't use tabs for indentation"), 400
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            return jsonify(error=f"invalid YAML: {e}"), 400

    path = safe_path(LOCAL_DIR / name, filename)
    is_new = not path.exists()
    shadows = is_new and filename in git_files(repo)   # check BEFORE writing: git_files hides local names
    write_atomic(path, content, mode=0o600 if is_secret(filename) else 0o644)

    if data.get("redeploy"):
        queue_request("deploy", name)
    return jsonify(ok=True, missing_env=missing_env(name), shadows_git=shadows)



# ---------- webhooks (GitHub / Gitea call this on every push) ----------

def valid_signature(body: bytes) -> bool:
    """GitHub sends X-Hub-Signature-256: sha256=<hex>, Gitea sends X-Gitea-Signature: <hex>."""
    if not WEBHOOK_SECRET:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    sent = (request.headers.get("X-Hub-Signature-256", "").removeprefix("sha256=")
            or request.headers.get("X-Gitea-Signature", ""))
    return bool(sent) and hmac.compare_digest(sent, expected)


@app.post("/webhook/<name>")
def webhook(name: str):
    if not valid_signature(request.get_data()):   # before get_repo, so strangers can't probe repo names
        abort(403, "bad or missing signature")
    repo = get_repo(name)
    event = request.headers.get("X-GitHub-Event") or request.headers.get("X-Gitea-Event")
    if event == "ping":
        return jsonify(ok=True, msg="pong")
    if event != "push":
        return jsonify(ok=True, ignored=f"event {event}")
    ref = (request.get_json(silent=True) or {}).get("ref", "")
    if ref != f"refs/heads/{repo['branch']}":
        return jsonify(ok=True, ignored=f"push to {ref}")
    if repo["paused"]:
        return jsonify(ok=True, ignored="paused")
    if name not in pending_requests():            # 5 quick pushes = 1 check
        queue_request("check", name)              # deploys if the branch has a new commit
    return jsonify(ok=True, queued="check"), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
