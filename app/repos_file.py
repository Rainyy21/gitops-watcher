"""Edit repos.yml while keeping its comments and layout (used by cli.py and watcher.py)."""

import os
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "repos.yml"))

rt_yaml = YAML()
rt_yaml.indent(mapping=2, sequence=4, offset=2)
rt_yaml.preserve_quotes = True
rt_yaml.width = 4096  # don't wrap long URLs onto their own line


def load() -> dict:
    try:
        with CONFIG_PATH.open() as f:
            return rt_yaml.load(f) or {}
    except FileNotFoundError:
        return {}


def save(data) -> None:
    # an empty list is written as `[]`, and a comment kept above it would push the
    # `[]` onto its own line, which isn't valid YAML. So drop those comments.
    repo_list = data.get("repos")
    if repo_list is not None and len(repo_list) == 0:
        if hasattr(repo_list, "ca"):
            repo_list.ca.comment = None
        if hasattr(data, "ca") and "repos" in data.ca.items:
            data.ca.items["repos"][3] = None
    # write in place (no temp file + rename): repos.yml is a single-file bind
    # mount in docker, and replacing the file would detach it from the container
    with CONFIG_PATH.open("w") as f:
        rt_yaml.dump(data, f)


def repos(data) -> list:
    """The `repos:` list of a loaded config, created if missing."""
    if data.get("repos") is None:
        data["repos"] = []
    return data["repos"]


def find(repo_list, name: str) -> int | None:
    """Index of the entry called `name`, or None."""
    return next(
        (i for i, r in enumerate(repo_list) if isinstance(r, dict) and r.get("name") == name),
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


def append_entry(repo_list, entry: dict) -> None:
    """Append with a blank line before it, like the hand-written entries."""
    if repo_list and hasattr(repo_list[-1], "ca") and _trailing(repo_list[-1]) is None:
        _set_trailing(repo_list[-1], CommentToken("\n\n", CommentMark(0), None))
    repo_list.append(entry)


def delete_entry(repo_list, idx: int) -> None:
    """Delete an entry, keeping the comment that sits above the entry after it."""
    after = _trailing(repo_list[idx]) if hasattr(repo_list[idx], "ca") else None
    if idx > 0 and hasattr(repo_list[idx - 1], "ca"):
        _set_trailing(repo_list[idx - 1], after)
    elif after is not None:
        text = "\n".join(
            l.strip().lstrip("#").strip() for l in after.value.splitlines() if l.strip()
        )
        if text:
            repo_list.yaml_set_start_comment(text, indent=2)
    del repo_list[idx]


def set_key(name: str, key: str, value) -> bool:
    """Set `key: value` on one entry. Returns False if there's no such repo."""
    data = load()
    repo_list = repos(data)
    idx = find(repo_list, name)
    if idx is None:
        return False
    entry = repo_list[idx]
    if key in entry:
        entry[key] = value
    else:
        # the new key becomes the last one, so move the blank line/comment after the entry onto it
        trailing = _trailing(entry)
        _set_trailing(entry, None)
        entry[key] = value
        _set_trailing(entry, trailing)
    save(data)
    return True
