# CodeClaw

GitHub AI coding agent. See [README.md](README.md) for setup. See [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) for architecture decisions.

## Quick Context

Single Python process (FastAPI + uvicorn) that receives GitHub webhooks, routes events to Claude Agent SDK running in containers (Linux VMs). Each repo gets isolated filesystem and memory. Agents respond via the GitHub API (comments, reviews, PRs).

## Key Files

| File | Purpose |
|------|---------|
| `codeclaw/main.py` | Orchestrator: webhook handling, repo checkout, agent invocation |
| `codeclaw/webhook_server.py` | FastAPI server for GitHub webhooks |
| `codeclaw/channels/github.py` | GitHub channel: post comments, reviews, PRs via httpx |
| `codeclaw/github/auth.py` | GitHub App JWT auth + installation token caching |
| `codeclaw/github/event_mapper.py` | Webhook payload → normalized messages |
| `codeclaw/github/access_control.py` | Permission checking + rate limiting |
| `codeclaw/ipc.py` | IPC watcher and task processing |
| `codeclaw/router.py` | Message formatting and XML escaping |
| `codeclaw/config.py` | Paths, intervals, container config |
| `codeclaw/container_runner.py` | Spawns agent containers with mounts |
| `codeclaw/task_scheduler.py` | Runs scheduled tasks |
| `codeclaw/db.py` | SQLite operations |
| `codeclaw/models.py` | Pydantic models (RegisteredGroup, NewMessage, etc.) |
| `container/agent_runner/main.py` | Container-side agent entry point (Python SDK) |
| `container/agent_runner/ipc_tools.py` | In-process MCP tools for agents |
| `groups/{name}/CLAUDE.md` | Per-group memory (isolated) |
| `container/skills/agent-browser.md` | Browser automation tool (available to all agents via Bash) |

## Skills

| Skill | When to Use |
|-------|-------------|
| `/debug` | Container issues, logs, troubleshooting |
| `/update` | Pull upstream changes, merge with customizations, run migrations |

## Development

Run commands directly—don't tell the user to run them.

```bash
python -m codeclaw.main   # Run the server
pytest                     # Run tests
ruff check codeclaw/       # Lint
./container/build.sh       # Rebuild agent container
```

Service management:
```bash
# macOS (launchd)
launchctl load ~/Library/LaunchAgents/com.codeclaw.plist
launchctl unload ~/Library/LaunchAgents/com.codeclaw.plist
launchctl kickstart -k gui/$(id -u)/com.codeclaw  # restart

# Linux (systemd)
systemctl --user start codeclaw
systemctl --user stop codeclaw
systemctl --user restart codeclaw
```

## Pre-commit

Always run these before committing and fix any errors:

```bash
ruff check codeclaw/             # Lint (use --fix for auto-fixable issues)
python -m pytest                 # Tests
```

## Container Build Cache

The container buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild, prune the builder then re-run `./container/build.sh`.
