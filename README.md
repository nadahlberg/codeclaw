# ClawCode

A GitHub AI coding agent that responds to issues and pull requests using Claude in isolated containers.

## How It Works

```
GitHub webhook → ClawCode → Container (Claude Agent SDK) → GitHub API response
```

When someone @mentions your bot in an issue or PR, ClawCode:

1. Receives the webhook event
2. Checks permissions (configurable per-repo via `.github/clawcode.yml`)
3. Clones the repo into an isolated container
4. Runs Claude Agent SDK with full access to the codebase
5. Posts comments, reviews, or creates PRs via the GitHub API

Agents run in Linux containers with filesystem isolation. They can only see the repo checkout and explicitly mounted directories.

## Quick Start

```bash
git clone <your-fork-url>
cd clawcode
pip install -e ".[dev]"
./container/build.sh
```

### Create a GitHub App

Before starting the server, create a GitHub App. Pass the public URL where GitHub will send webhooks (e.g. an ngrok tunnel or your server's domain):

```bash
python -m setup.github_app --webhook-url https://your-domain.com
```

This opens your browser to create a GitHub App via the manifest flow, exchanges the OAuth code, and saves the credentials to `.env` and `~/.config/clawcode/github-app.pem` automatically. Install the app on the repos you want to monitor when prompted.

### Start the server

```bash
clawcode
# or
python -m clawcode.main
```

## Deploy to Fly.io

```bash
fly launch
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
fly secrets set GITHUB_APP_ID=...
fly secrets set GITHUB_WEBHOOK_SECRET=...
# Store your GitHub App private key
fly secrets set GITHUB_PRIVATE_KEY="$(cat github-app.pem)"
fly deploy
```

## Self-Host

Requirements:
- Python 3.12+
- Docker (for spawning agent containers)
- Node.js 20+ (for Claude Code CLI, used inside containers)

Set `ANTHROPIC_API_KEY` in your environment, then run the setup to create a GitHub App:

```bash
python -m setup.github_app --webhook-url https://your-public-url.com
```

This writes `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, and `GITHUB_PRIVATE_KEY_PATH` to `.env` automatically. If you prefer to configure manually, set these environment variables yourself and store the private key at `~/.config/clawcode/github-app.pem`.

## Per-Repo Configuration

Create `.github/clawcode.yml` in any repo where your GitHub App is installed:

```yaml
access:
  min_permission: triage    # minimum GitHub permission level to trigger the bot
  allow_external: false     # whether non-collaborators can trigger it
  rate_limit: 10            # max invocations per user per hour
```

Permission levels: `admin` > `maintain` > `write` > `triage` > `read` > `none`

## Development

```bash
pip install -e ".[dev]"     # Install with dev dependencies
python -m clawcode.main     # Run the server
pytest                      # Run tests
ruff check clawcode/ tests/ # Lint
./container/build.sh        # Rebuild agent container
```

## Architecture

Single Python process (FastAPI + uvicorn). Webhook-driven (no polling). Agents execute in isolated Linux containers with filesystem isolation.

Key files:
- `clawcode/main.py` — Orchestrator: webhook handling, repo checkout, agent invocation
- `clawcode/webhook_server.py` — FastAPI server for GitHub webhooks
- `clawcode/channels/github.py` — GitHub channel: comments, reviews, PRs via httpx
- `clawcode/github/auth.py` — GitHub App JWT auth + installation token caching
- `clawcode/github/event_mapper.py` — Webhook payload normalization
- `clawcode/github/access_control.py` — Permission checking + rate limiting
- `clawcode/container_runner.py` — Spawns agent containers with repo mounts
- `clawcode/ipc.py` — IPC watcher for structured GitHub responses
- `clawcode/task_scheduler.py` — Scheduled tasks
- `clawcode/db.py` — SQLite (messages, groups, processed events)
- `container/agent_runner/main.py` — In-container agent runner (Python SDK)
- `container/agent_runner/ipc_tools.py` — In-process MCP tools for agents

## License

MIT
