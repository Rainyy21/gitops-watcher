import os
import subprocess
import time
from datetime import datetime

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from ruamel.yaml import YAML
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken

from watcher import (
    CONFIG_PATH,
    NAME_RE,
    STACKS_DIR,
    load_config,
    load_state,
    normalize,
    redact,
    request_check,
)

app = typer.Typer(help="control the gitops-watcher.")
console = Console()


def ago(iso: str | None) -> str:
    """'2026-11-11T09:30:00' -> '09:30:00 (5m ago)', or '-' if missing."""
    if not iso:
        return "-"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    secs = int((datetime.now() - then).total_seconds())
    if secs < 60:
        rel = f"{secs}s"
    elif secs < 3600:
        rel = f"{secs // 60}m"
    elif secs < 86400:
        rel = f"{secs // 3600}h"
    else:
        rel = f"{secs // 86400}d"
    return f"{then:%m-%d %H:%M:%S} ({rel} ago)"


def fail(msg: str) -> None:
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(code=1)


def find_repo(name: str, quiet: bool = False) -> dict | None:
    """The normalized repo entry for `name` from repos.yml.

    Exits with an error if it's missing or invalid, or returns None when quiet.
    """
    try:
        config = load_config()
    except Exception as e:
        fail(f"can't read {CONFIG_PATH}: {e}")
    defaults = config.get("defaults") or {}
    for raw in config.get("repos") or []:
        if isinstance(raw, dict) and raw.get("name") == name:
            try:
                return normalize(raw, defaults)
            except ValueError as e:
                if quiet:
                    return None
                fail(f"entry for '{name}' is invalid: {e}")
    if quiet:
        return None
    fail(f"no repo named '{name}' in {CONFIG_PATH}")


# round-trip YAML keeps comments and layout when add/remove edit repos.yml
rt_yaml = YAML()
rt_yaml.indent(mapping=2, sequence=4, offset=2)
rt_yaml.preserve_quotes = True


def load_config_rt():
    try:
        with CONFIG_PATH.open() as f:
            return rt_yaml.load(f) or {}
    except FileNotFoundError:
        return {}


def save_config_rt(data) -> None:
    # write in place (no temp file + rename): repos.yml is a single-file bind
    # mount in docker, and replacing the file would detach it from the container
    with CONFIG_PATH.open("w") as f:
        rt_yaml.dump(data, f)


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
    if repos and _trailing(repos[-1]) is None:
        _set_trailing(repos[-1], CommentToken("\n\n", CommentMark(0), None))
    repos.append(entry)


def delete_entry(repos, idx: int) -> None:
    """Delete an entry, keeping the comment that sits above the entry after it."""
    after = _trailing(repos[idx])
    if idx > 0:
        _set_trailing(repos[idx - 1], after)
    elif after is not None:
        text = "\n".join(
            l.strip().lstrip("#").strip() for l in after.value.splitlines() if l.strip()
        )
        if text:
            repos.yaml_set_start_comment(text, indent=2)
    del repos[idx]


def config_names() -> list[str] | None:
    """Repo names from repos.yml, or None if it can't be read."""
    try:
        config = load_config()
    except Exception:
        return None
    defaults = config.get("defaults") or {}
    names = []
    for raw in config.get("repos") or []:
        try:
            names.append(normalize(raw, defaults)["name"])
        except ValueError:
            pass
    return names


@app.command()
def status():
    """Show the status of every watched repo."""
    state = load_state()
    names = config_names()

    # repos in the config but not checked yet show up as "pending"
    for name in names or []:
        state.setdefault(name, {"status": "pending"})
    if not state:
        console.print("[yellow]No data yet. Has the watcher run a round?[/yellow]")
        return

    table = Table(title="gitops-watcher")
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Commit")
    table.add_column("Last check")
    table.add_column("Last deploy")
    table.add_column("Error", max_width=60)

    failing = 0
    for name, info in sorted(state.items()):
        status = info.get("status") or "unknown"
        if status == "ok":
            status_cell = "[green]ok[/green]"
        elif status == "error":
            failing += 1
            status_cell = "[red]error[/red]"
            retry_at = info.get("retry_at")
            if retry_at and retry_at > time.time():
                mins = int((retry_at - time.time()) // 60) + 1
                status_cell += f"\n[dim]retry in {mins}m ({info.get('failures', 1)}x)[/dim]"
        else:
            status_cell = f"[yellow]{escape(status)}[/yellow]"
        if names is not None and name not in names:
            status_cell += "\n[dim]not in config[/dim]"

        # commit: first 7 chars, or "-" if missing
        commit = (info.get("commit") or "-")[:7]

        # times: show them, or "-" if missing
        last_check = ago(info.get("last_check"))
        last_deploy = ago(info.get("last_deploy"))

        # error: last non-empty line, or "" if none. The first line is always
        # "<command> failed:"; the actual cause is at the end of the output.
        # escaped so [brackets] in git/docker output aren't read as rich markup
        lines = [l for l in (info.get("error") or "").splitlines() if l.strip()]
        error = escape(lines[-1]) if lines else ""

        table.add_row(name, status_cell, commit, last_check, last_deploy, error)

    console.print(table)
    if names is None:
        console.print("[yellow]couldn't read repos.yml; showing saved state only[/yellow]")

    # non-zero exit when something is failing, so scripts/cron can alert on it
    raise typer.Exit(code=1 if failing else 0)


@app.command()
def list_repos():
    """List repos from repos.yml with their settings."""
    try:
        config = load_config()
    except Exception as e:
        fail(f"can't read {CONFIG_PATH}: {e}")
    defaults = config.get("defaults") or {}
    entries = config.get("repos") or []
    if not entries:
        console.print(f"[yellow]no repos in {CONFIG_PATH}[/yellow]")
        return

    table = Table(title=str(CONFIG_PATH))
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("URL")
    table.add_column("Branch")
    table.add_column("Compose file")
    table.add_column("Pull")
    table.add_column("Token")

    invalid = 0
    for i, raw in enumerate(entries):
        try:
            repo = normalize(raw, defaults)
        except ValueError as e:
            invalid += 1
            label = raw.get("name") if isinstance(raw, dict) and raw.get("name") else f"repos[{i}]"
            table.add_row(escape(str(label)), f"[red]invalid: {escape(str(e))}[/red]", "", "", "", "")
            continue

        token_env = repo.get("token_env")
        if not token_env:
            token = "[dim]none[/dim]"
        elif os.environ.get(token_env):
            token = f"{token_env} [green]set[/green]"
        else:
            token = f"{token_env} [red]not set[/red]"

        table.add_row(
            repo["name"],
            escape(redact(repo["url"])),
            repo["branch"],
            repo["compose_file"] or "[dim]auto[/dim]",
            "yes" if repo["pull_image"] else "no",
            token,
        )

    console.print(table)
    if invalid:
        raise typer.Exit(code=1)


@app.command()
def logs(
    name: str,
    tail: int = typer.Option(50, help="Number of lines per container."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep streaming new lines."),
):
    """Show recent container logs for a stack."""
    path = STACKS_DIR / name
    if not path.is_dir():
        fail(f"no stack folder at {path}; has '{name}' been deployed?")

    # prefer the current config; fall back to the saved state for stacks
    # that were removed from repos.yml but are still running
    repo = find_repo(name, quiet=True)
    if repo:
        compose_file = repo["compose_file"]
    else:
        compose_file = load_state().get(name, {}).get("compose_file")

    cmd = ["docker", "compose", "-p", name]
    if compose_file:
        cmd += ["-f", compose_file]
    cmd += ["logs", "--tail", str(tail)]
    if follow:
        cmd.append("--follow")
    # not captured: output streams straight to the terminal
    try:
        code = subprocess.run(cmd, cwd=path).returncode
    except KeyboardInterrupt:
        code = 0
    raise typer.Exit(code=code)


@app.command()
def check(
    name: str,
    force: bool = typer.Option(False, "--force", help="Redeploy even if nothing changed."),
    wait: bool = typer.Option(False, "--wait", help="Wait for the result."),
):
    """Ask the watcher to check a repo now."""
    find_repo(name)  # exits if it isn't in repos.yml
    before = load_state().get(name, {}).get("last_check")
    request_check(name, force)
    console.print(f"requested a check of [cyan]{name}[/cyan]" + (" (forced redeploy)" if force else ""))
    console.print("[dim]clears any retry backoff; the watcher picks it up within a second[/dim]")
    if not wait:
        return

    with console.status("waiting for the watcher..."):
        try:
            while True:
                time.sleep(1)
                info = load_state().get(name, {})
                if info.get("last_check") != before:
                    break
        except KeyboardInterrupt:
            fail("stopped waiting (the check still happens)")

    if info.get("status") == "ok":
        console.print(f"[green]ok[/green] at {(info.get('commit') or '-')[:7]}")
    else:
        lines = [l for l in (info.get("error") or "").splitlines() if l.strip()]
        fail(f"error: {lines[-1] if lines else 'unknown'}")


@app.command()
def add(
    name: str,
    url: str,
    branch: str = typer.Option(None, help="Defaults to the branch in repos.yml defaults, or main."),
    token_env: str = typer.Option(None, help="Name of the env var holding the access token."),
    compose_file: str = typer.Option(None, help="Defaults to repos.yml defaults, or auto-detect."),
):
    """Add a repo to repos.yml."""
    if not NAME_RE.match(name):
        fail(f"invalid name '{name}': use lowercase letters, digits, '-' and '_'")
    if token_env and not url.startswith("https://"):
        fail("token auth only works with https:// URLs")

    data = load_config_rt()
    repos = data.get("repos")
    if repos is None:
        repos = data["repos"] = []
    if any(isinstance(r, dict) and r.get("name") == name for r in repos):
        fail(f"'{name}' is already in {CONFIG_PATH}")

    # only write what was given, so the entry inherits from `defaults`
    entry = {"name": name, "url": url}
    if branch:
        entry["branch"] = branch
    if token_env:
        entry["token_env"] = token_env
    if compose_file:
        entry["compose_file"] = compose_file
    append_entry(repos, entry)
    save_config_rt(data)

    console.print(f"added [cyan]{name}[/cyan] to {CONFIG_PATH}; the watcher deploys it on its next round")
    if token_env and not os.environ.get(token_env):
        console.print(f"[yellow]note: {token_env} isn't set here. Add it to .env, then recreate the container (docker compose up -d) so the watcher sees it[/yellow]")


@app.command()
def remove(
    name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
):
    """Remove a repo from repos.yml."""
    data = load_config_rt()
    repos = data.get("repos") or []
    idx = next(
        (i for i, r in enumerate(repos) if isinstance(r, dict) and r.get("name") == name),
        None,
    )
    if idx is None:
        fail(f"no repo named '{name}' in {CONFIG_PATH}")

    console.print(
        f"this removes [cyan]{name}[/cyan] from {CONFIG_PATH}. On its next round the watcher "
        f"runs `docker compose down` for it.\n[dim]its folder and volumes are kept[/dim]"
    )
    if not yes and not typer.confirm("continue?"):
        raise typer.Exit(code=1)

    delete_entry(repos, idx)
    save_config_rt(data)
    console.print(f"removed [cyan]{name}[/cyan]")


if __name__ == "__main__":
    app()
