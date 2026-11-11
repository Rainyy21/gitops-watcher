# gitops-watcher

A small GitOps deployer for a single Docker host. It watches a list of Git
repositories and, when a branch gets a new commit, pulls it and runs
`docker compose up -d --build` for that repo. Removing a repo from the config
stops its stack.

No Kubernetes, no agents, no CI runners: one container with access to the
Docker socket, a YAML file listing your repos, and a CLI to see what's going on.

## How it works

Every `CHECK_INTERVAL` seconds the watcher goes through `repos.yml` and, for each repo:

1. Runs `git ls-remote` to get the latest commit on the branch (cheap, no clone).
2. **New commit:** clones the repo into `STACKS_DIR/<name>` (shallow), or fetches
   and hard-resets an existing clone, then runs
   `docker compose -p <name> up -d --build --remove-orphans`.
3. **No new commit:** runs `docker compose up -d` anyway, so containers that were
   stopped or crashed come back. If `pull_image` is on, it pulls newer images first.
4. **Deploy failed:** retries the same commit with exponential backoff
   (`CHECK_INTERVAL`, 2x, 4x … up to `MAX_BACKOFF`). A new commit is tried right away.

Repos that disappear from `repos.yml` get `docker compose down`. Their folder and
volumes are left on disk, since they may hold data. If the config has an invalid
entry, nothing is stopped that round, so a typo can't take a stack down.

State (last commit, last check, errors, backoff) is kept in `STACKS_DIR/.state.json`.

On `docker stop` the watcher finishes the repo it is working on before exiting
(the compose file gives it a 5 minute grace period).

## Quick start

```sh
git clone <this repo> gitops-watcher
cd gitops-watcher

cp .env.example .env        # fill in the tokens you need
$EDITOR repos.yml           # see below

docker compose up -d --build
docker logs -f gitops-watcher
```

Clones go to `/opt/gitops/<name>` on the host. That path is mounted at the same
location inside the container, so relative bind mounts in your stacks'
compose files (`./data:/data`) resolve to the right place on the host.

## Configuring repos

`repos.yml` sits next to `docker-compose.yml` (it's git-ignored). Example:

```yaml
defaults:
  branch: main
  compose_file: docker-compose.yml
  pull_image: false

repos:
  # private repo on Gitea
  - name: test-app
    url: https://gitea.example.com/me/test-app.git
    token_env: GITEA_TOKEN

  # public repo, all defaults
  - name: public-app
    url: https://github.com/me/public-app.git

  # private repo on GitHub, different branch and compose file
  - name: private-app
    url: https://github.com/me/private-app.git
    token_env: GITHUB_TOKEN
    branch: production
    compose_file: compose.yaml
```

| Key            | Required | Default            | Meaning |
|----------------|----------|--------------------|---------|
| `name`         | yes      |                    | Folder name and compose project name. Lowercase letters, digits, `-` and `_`. |
| `url`          | yes      |                    | Git URL. Use `https://` if you need a token. |
| `branch`       | no       | `main`             | Branch to deploy. |
| `compose_file` | no       | auto-detect        | Compose file in the repo root. Unset lets `docker compose` find it. |
| `pull_image`   | no       | `true`             | Run `docker compose pull --ignore-buildable` before each deploy and each check. |
| `token_env`    | no       |                    | Name of the env var holding an access token for this repo. |
| `paused`       | no       | `false`            | Leave the stack alone (set by `gw stop` / the web UI's stop button). |

Anything under `defaults` applies to every repo unless the repo sets it itself.
The file is re-read every round, so edits take effect without a restart.

### Private repos

Put the token in `.env` and point the repo at it with `token_env`:

```sh
# .env
GITEA_TOKEN=...
GITHUB_TOKEN=...
```

The token is sent as an HTTP auth header through git's environment config. It
never appears in the command line, in `.git/config` or in logs. Don't put tokens
in the URL itself. After changing `.env`, recreate the container
(`docker compose up -d`) so the watcher picks up the new value.

## CLI

`cli.py` talks to the watcher through its state file and config, so run it inside
the container:

```sh
alias gw='docker exec -it gitops-watcher python cli.py'
```

| Command | What it does |
|---------|--------------|
| `gw status` | Table of every repo: status, commit, last check, last deploy, last error line. Exits `1` if any repo is failing, so it works in cron/monitoring. |
| `gw list-repos` | Repos from `repos.yml` with their settings and whether their token is set. |
| `gw check <name>` | Check a repo now instead of waiting for the next round. Clears any retry backoff. |
| `gw check <name> --force` | Redeploy even if the commit hasn't changed. |
| `gw check <name> --wait` | Wait and print the result. |
| `gw logs <name> [-f] [--tail N]` | Container logs for a stack. |
| `gw add <name> <url> [--branch B] [--token-env T] [--compose-file F]` | Add a repo to `repos.yml`. Only the options you give are written, the rest come from `defaults`. |
| `gw remove <name> [-y]` | Remove a repo from `repos.yml`. The watcher stops the stack on its next round. |
| `gw stop <name>` | Stop a stack and mark it `paused`, so the watcher leaves it down. |
| `gw start <name>` | Unpause a stack and deploy it. |

`add` and `remove` keep the comments and layout of `repos.yml`.

## Settings

Set these in the `environment:` block of `docker-compose.yml`.

| Variable | Default (compose file) | Meaning |
|----------|------------------------|---------|
| `CHECK_INTERVAL` | `30` | Seconds between rounds. |
| `STACKS_DIR` | `/opt/gitops` | Where repos are cloned. |
| `CONFIG_PATH` | `/config/repos.yml` | Path to the repo list. |
| `STATE_PATH` | `$STACKS_DIR/.state.json` | Where state is saved. |
| `REQUEST_DIR` | `$STACKS_DIR/.requests` | Where the CLI and the web UI drop requests for the watcher. |
| `LOCAL_DIR` | `$STACKS_DIR/.local` | Per-repo files that aren't in git (`.env`, overrides), copied into the clone before each deploy. |
| `GIT_TIMEOUT` | `300` | Seconds before a clone/fetch is killed. |
| `DEPLOY_TIMEOUT` | `1800` | Seconds before a pull/build/up is killed. |
| `MAX_BACKOFF` | `3600` | Longest wait between retries of a failing commit. |

## Running without Docker

For development:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cd app
CONFIG_PATH=../repos.yml STACKS_DIR=../stacks python watcher.py
```

Needs `git` and `docker compose` on the PATH.

## Web UI

`docker compose up -d` also starts `gitops-web`, a dashboard on port `8088`:

- status of every repo, and buttons to deploy, stop/start (pause) and remove
- an "add repo" form
- a file view per repo: read the repo's files, and edit local files such as
  `.env` or `docker-compose.override.yml`. They're kept in `LOCAL_DIR/<name>`
  and copied into the clone before every deploy, so they survive updates.
  `.env` values are masked until you click to reveal them.
- `POST /webhook/<name>` for GitHub/Gitea push webhooks, so a push is deployed
  right away instead of on the next round

The website has no Docker socket and no git tokens. It can only read the state
and write request files, which the watcher checks and acts on. Set these in
`.env` (see `.env.example`):

```sh
WEB_SECRET_KEY=...    # openssl rand -hex 32; keeps open pages working across restarts
WEBHOOK_SECRET=...    # openssl rand -hex 32; webhooks are off while it's empty
```

To add a webhook, point it at `http://<host>:8088/webhook/<name>` with content
type `application/json`, the `push` event, and `WEBHOOK_SECRET` as the secret.

**The website has no login**, and anyone who can use it can deploy any repo, which
means root on this host. Keep it on your LAN or behind a reverse proxy with access
control.

## Security notes

- The watcher container has the Docker socket, which means root on the host.
  Only watch repos you trust: anyone who can push to a watched branch can run
  anything on this machine.
- Tokens should be read-only (repo contents only).
