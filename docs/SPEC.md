# ClawCode Specification

A GitHub AI coding agent powered by Claude, running in isolated containers. Responds to issues, pull requests, and review comments via webhooks.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Folder Structure](#folder-structure)
3. [Configuration](#configuration)
4. [Memory System](#memory-system)
5. [Session Management](#session-management)
6. [Message Flow](#message-flow)
7. [Scheduled Tasks](#scheduled-tasks)
8. [MCP Servers](#mcp-servers)
9. [Deployment](#deployment)
10. [Security Considerations](#security-considerations)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        HOST (Node.js Process)                        │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────────────┐                  ┌────────────────────┐        │
│  │  Webhook Server  │─────────────────▶│   SQLite Database  │        │
│  │  (HTTP/Express)  │◀────────────────│   (messages.db)    │        │
│  └──────────────────┘  store/query     └─────────┬──────────┘        │
│         │                                        │                   │
│         ▼                                        │                   │
│  ┌──────────────────┐                            │                   │
│  │  Event Mapper    │                            │                   │
│  │  (normalize)     │                            │                   │
│  └────────┬─────────┘                            │                   │
│           │                                      │                   │
│           ▼                                      │                   │
│  ┌──────────────────┐    ┌──────────────────┐    │                   │
│  │  Access Control  │    │  Scheduler Loop  │    │                   │
│  │  (permissions)   │    │  (checks tasks)  │    │                   │
│  └────────┬─────────┘    └────────┬─────────┘    │                   │
│           │                       │              │                   │
│           └───────────┬───────────┘              │                   │
│                       │ spawns container                             │
│                       ▼                                              │
├──────────────────────────────────────────────────────────────────────┤
│                     CONTAINER (Linux VM)                              │
├──────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    AGENT RUNNER                               │   │
│  │                                                               │   │
│  │  Working directory: /workspace/group (mounted from host)      │   │
│  │  Volume mounts:                                               │   │
│  │    • groups/{name}/ → /workspace/group                        │   │
│  │    • groups/global/ → /workspace/global/ (non-main only)      │   │
│  │    • data/sessions/{group}/.claude/ → /home/node/.claude/     │   │
│  │    • Additional dirs → /workspace/extra/*                     │   │
│  │                                                               │   │
│  │  Tools (all groups):                                          │   │
│  │    • Bash (safe - sandboxed in container!)                    │   │
│  │    • Read, Write, Edit, Glob, Grep (file operations)          │   │
│  │    • WebSearch, WebFetch (internet access)                    │   │
│  │    • agent-browser (browser automation)                       │   │
│  │    • mcp__clawcode__* (scheduler tools via IPC)               │   │
│  │                                                               │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Technology Stack

| Component | Technology | Purpose |
|-----------|------------|---------|
| Webhook Server | Node.js (HTTP) | Receive GitHub webhook events |
| GitHub Integration | Octokit + GitHub App JWT | Auth, API calls, comments, reviews |
| Message Storage | SQLite (better-sqlite3) | Store events, sessions, groups, tasks |
| Container Runtime | Containers (Linux VMs) | Isolated environments for agent execution |
| Agent | @anthropic-ai/claude-agent-sdk | Run Claude with tools and MCP servers |
| Browser Automation | agent-browser + Chromium | Web interaction and screenshots |
| Runtime | Node.js 20+ | Host process for routing and scheduling |

---

## Folder Structure

```
clawcode/
├── CLAUDE.md                      # Project context for Claude Code
├── docs/
│   ├── SPEC.md                    # This specification document
│   ├── REQUIREMENTS.md            # Architecture decisions
│   ├── SECURITY.md                # Security model
│   └── SDK_DEEP_DIVE.md           # Claude Agent SDK internals
├── README.md                      # User documentation
├── package.json                   # Node.js dependencies
├── tsconfig.json                  # TypeScript configuration
├── .gitignore
│
├── src/
│   ├── index.ts                   # Orchestrator: webhook handling, agent invocation
│   ├── webhook-server.ts          # HTTP server for GitHub webhooks
│   ├── channels/
│   │   └── github.ts              # GitHub channel: comments, reviews, PRs via Octokit
│   ├── github/
│   │   ├── auth.ts                # GitHub App JWT auth + installation token caching
│   │   ├── event-mapper.ts        # Webhook payload → normalized GitHubEvent
│   │   ├── access-control.ts      # Permission checking + rate limiting
│   │   └── app-manifest.ts        # GitHub App manifest flow
│   ├── ipc.ts                     # IPC watcher and task processing
│   ├── router.ts                  # Message formatting
│   ├── config.ts                  # Configuration constants
│   ├── types.ts                   # TypeScript interfaces
│   ├── logger.ts                  # Pino logger setup
│   ├── db.ts                      # SQLite database initialization and queries
│   ├── group-queue.ts             # Per-group queue with global concurrency limit
│   ├── mount-security.ts          # Mount allowlist validation for containers
│   ├── task-scheduler.ts          # Runs scheduled tasks when due
│   ├── container-runner.ts        # Spawns agents in containers
│   └── container-runtime.ts       # Container runtime detection and management
│
├── container/
│   ├── Dockerfile                 # Container image (runs as 'node' user)
│   ├── build.sh                   # Build script for container image
│   ├── agent-runner/              # Code that runs inside the container
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   └── src/
│   │       ├── index.ts           # Entry point (query loop, IPC polling)
│   │       └── ipc-mcp-stdio.ts   # Stdio-based MCP server for host communication
│   └── skills/
│       └── agent-browser.md       # Browser automation skill
│
├── setup/
│   ├── index.ts                   # CLI entry point for setup steps
│   ├── environment.ts             # Environment validation
│   ├── github-app.ts              # GitHub App creation via manifest flow
│   ├── container.ts               # Container runtime setup
│   ├── register.ts                # Repo registration
│   ├── mounts.ts                  # Mount configuration
│   ├── service.ts                 # Service installation (launchd/systemd)
│   ├── verify.ts                  # Installation verification
│   └── status.ts                  # Status reporting for CLI
│
├── skills/
│   ├── setup/SKILL.md             # /setup - First-time installation
│   ├── customize/SKILL.md         # /customize - Add capabilities
│   ├── debug/SKILL.md             # /debug - Container debugging
│   ├── update/SKILL.md            # /update - Pull upstream changes
│   ├── add-parallel/SKILL.md      # /add-parallel - Parallel agents
│   └── convert-to-apple-container/ # Apple Container runtime
│
├── dist/                          # Compiled JavaScript (gitignored)
│
├── groups/
│   ├── global/
│   │   └── CLAUDE.md              # Global memory (all groups read this)
│   ├── main/                      # Admin control channel
│   │   ├── CLAUDE.md              # Main channel memory
│   │   └── logs/                  # Task execution logs
│   └── {repo-name}/               # Per-repo folders (created on registration)
│       ├── CLAUDE.md              # Repo-specific memory
│       ├── logs/                  # Task logs for this repo
│       └── *.md                   # Files created by the agent
│
├── store/                         # Local data (gitignored)
│   └── messages.db                # SQLite database
│
├── data/                          # Application state (gitignored)
│   ├── sessions/                  # Per-group session data (.claude/ dirs)
│   ├── env/env                    # Copy of .env for container mounting
│   └── ipc/                       # Container IPC (messages/, tasks/)
│
├── logs/                          # Runtime logs (gitignored)
│   ├── clawcode.log               # Host stdout
│   └── clawcode.error.log         # Host stderr
│
└── launchd/
    └── com.clawcode.plist         # macOS service configuration
```

---

## Configuration

Configuration constants are in `src/config.ts`:

```typescript
export const ASSISTANT_NAME = process.env.ASSISTANT_NAME || 'ClawCode';
export const SCHEDULER_POLL_INTERVAL = 60000;
export const RECONCILIATION_INTERVAL = 60000;

// Paths are absolute (required for container mounts)
const PROJECT_ROOT = process.cwd();
export const STORE_DIR = path.resolve(PROJECT_ROOT, 'store');
export const GROUPS_DIR = path.resolve(PROJECT_ROOT, 'groups');
export const DATA_DIR = path.resolve(PROJECT_ROOT, 'data');

// Container configuration
export const CONTAINER_IMAGE = process.env.CONTAINER_IMAGE || 'clawcode-agent:latest';
export const CONTAINER_TIMEOUT = parseInt(process.env.CONTAINER_TIMEOUT || '1800000', 10); // 30min
export const IDLE_TIMEOUT = parseInt(process.env.IDLE_TIMEOUT || '1800000', 10); // 30min
export const MAX_CONCURRENT_CONTAINERS = Math.max(1, parseInt(process.env.MAX_CONCURRENT_CONTAINERS || '5', 10) || 5);

// HTTP server port for webhooks
export const PORT = parseInt(process.env.PORT || '3000', 10);
```

**Note:** Paths must be absolute for container volume mounts to work correctly.

### GitHub App Configuration

Required environment variables (stored in `.env`):

```bash
GITHUB_APP_ID=12345
GITHUB_WEBHOOK_SECRET=your-webhook-secret
GITHUB_PRIVATE_KEY_PATH=~/.config/clawcode/github-app.pem
```

### Claude Authentication

Two options for authenticating the agent:

**Option 1: Claude Subscription (OAuth token)**
```bash
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

**Option 2: Pay-per-use API Key**
```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
```

Only the authentication variables are extracted from `.env` and mounted into containers. Other environment variables are not exposed to agents.

### Container Configuration

Repos can have additional directories mounted via `containerConfig` in the SQLite `registered_groups` table (stored as JSON in the `container_config` column).

Additional mounts appear at `/workspace/extra/{containerPath}` inside the container.

**Mount syntax note:** Read-write mounts use `-v host:container`, but readonly mounts require `--mount "type=bind,source=...,target=...,readonly"` (the `:ro` suffix may not work on all runtimes).

---

## Memory System

ClawCode uses a hierarchical memory system based on CLAUDE.md files.

### Memory Hierarchy

| Level | Location | Read By | Written By | Purpose |
|-------|----------|---------|------------|---------|
| **Global** | `groups/global/CLAUDE.md` | All groups | Main only | Shared context across all repos |
| **Group** | `groups/{name}/CLAUDE.md` | That group | That group | Repo-specific context and memory |
| **Files** | `groups/{name}/*.md` | That group | That group | Notes, research, documents |

### How Memory Works

1. **Agent Context Loading**
   - Agent runs with `cwd` set to `groups/{group-name}/`
   - Claude Agent SDK with `settingSources: ['project']` automatically loads:
     - `../CLAUDE.md` (parent directory = global memory)
     - `./CLAUDE.md` (current directory = group memory)

2. **Writing Memory**
   - Agent writes to `./CLAUDE.md` for repo-specific memory
   - Main channel can write to `../CLAUDE.md` for global memory
   - Agent can create files like `notes.md`, `research.md` in the group folder

3. **Main Channel Privileges**
   - Only the "main" group can write to global memory
   - Main can manage registered repos and schedule tasks for any group
   - All groups have Bash access (safe because it runs inside container)

---

## Session Management

Sessions enable conversation continuity — Claude remembers previous interactions in the same thread.

### How Sessions Work

1. Each group has a session ID stored in SQLite (`sessions` table, keyed by `group_folder`)
2. Session ID is passed to Claude Agent SDK's `resume` option
3. Claude continues the conversation with full context
4. Session transcripts are stored as JSONL files in `data/sessions/{group}/.claude/`

---

## Message Flow

### Incoming Event Flow

```
1. GitHub sends webhook event (issue, PR, comment, review)
   │
   ▼
2. Webhook server receives and validates signature
   │
   ▼
3. Event mapper normalizes payload into GitHubEvent
   │ (filters bot messages, extracts repo/thread JIDs)
   ▼
4. Access control checks:
   ├── Is this repo registered? → No: ignore
   ├── Does sender have required permission level? → No: ignore
   └── Rate limit check → Exceeded: ignore
   │
   ▼
5. Orchestrator builds agent prompt:
   ├── Event content (issue body, comment text, PR diff)
   ├── Thread context from previous interactions
   └── Repo-specific configuration
   │
   ▼
6. Container spawned with Claude Agent SDK:
   ├── cwd: groups/{group-name}/
   ├── prompt: event context + instructions
   ├── resume: session_id (for continuity)
   └── mcpServers: clawcode (scheduler, send_message)
   │
   ▼
7. Claude processes event:
   ├── Reads CLAUDE.md files for context
   ├── Accesses repo checkout in workspace
   └── Uses tools as needed (search, browse, etc.)
   │
   ▼
8. Response posted via GitHub API:
   ├── Issue comment
   ├── PR review comment
   ├── PR review
   └── Or creates a new PR
   │
   ▼
9. Session ID saved for thread continuity
```

### Event Types Handled

| GitHub Event | Actions | Thread JID Format |
|-------------|---------|-------------------|
| `issues` | `opened`, `assigned` | `gh:owner/repo#issue:42` |
| `issue_comment` | `created` | `gh:owner/repo#issue:42` or `gh:owner/repo#pr:17` |
| `pull_request` | `opened`, `synchronize` | `gh:owner/repo#pr:17` |
| `pull_request_review` | `submitted` (with @mention) | `gh:owner/repo#pr:17` |
| `pull_request_review_comment` | `created` (with @mention or reply) | `gh:owner/repo#pr:17` |

### Per-Repo Access Control

Create `.github/clawcode.yml` in any repo:

```yaml
access:
  min_permission: triage    # minimum GitHub permission level
  allow_external: false     # whether non-collaborators can trigger
  rate_limit: 10            # max invocations per user per hour
```

Permission levels: `admin` > `maintain` > `write` > `triage` > `read` > `none`

---

## Scheduled Tasks

ClawCode has a built-in scheduler that runs tasks as full agents in their group's context.

### How Scheduling Works

1. **Group Context**: Tasks created in a group run with that group's working directory and memory
2. **Full Agent Capabilities**: Scheduled tasks have access to all tools (WebSearch, file operations, etc.)
3. **Optional Messaging**: Tasks can send messages to their group using the `send_message` tool, or complete silently
4. **Main Channel Privileges**: The main channel can schedule tasks for any group and view all tasks

### Schedule Types

| Type | Value Format | Example |
|------|--------------|---------|
| `cron` | Cron expression | `0 9 * * 1` (Mondays at 9am) |
| `interval` | Milliseconds | `3600000` (every hour) |
| `once` | ISO timestamp | `2024-12-25T09:00:00Z` |

---

## MCP Servers

### ClawCode MCP (built-in)

The `clawcode` MCP server is created dynamically per agent call with the current group's context.

**Available Tools:**
| Tool | Purpose |
|------|---------|
| `schedule_task` | Schedule a recurring or one-time task |
| `list_tasks` | Show tasks (group's tasks, or all if main) |
| `get_task` | Get task details and run history |
| `update_task` | Modify task prompt or schedule |
| `pause_task` | Pause a task |
| `resume_task` | Resume a paused task |
| `cancel_task` | Delete a task |
| `send_message` | Send a GitHub comment to the thread |

---

## Deployment

### Quick Start

```bash
git clone <your-fork-url>
cd clawcode
npm install
npm run build
./container/build.sh
npx tsx setup/index.ts --step github-app -- --webhook-url https://your-domain.com
npm start
```

### Startup Sequence

When ClawCode starts, it:
1. **Ensures container runtime is running** — Automatically starts it if needed; kills orphaned containers from previous runs
2. Initializes the SQLite database (runs migrations)
3. Loads state from SQLite (registered repos, sessions)
4. Starts the webhook HTTP server
5. Starts the scheduler loop
6. Starts the IPC watcher for container messages

### Service Management

```bash
# macOS (launchd)
launchctl load ~/Library/LaunchAgents/com.clawcode.plist
launchctl unload ~/Library/LaunchAgents/com.clawcode.plist
launchctl kickstart -k gui/$(id -u)/com.clawcode  # restart

# Linux (systemd)
systemctl --user start clawcode
systemctl --user stop clawcode
systemctl --user restart clawcode

# View logs
tail -f logs/clawcode.log
```

---

## Security Considerations

### Container Isolation

All agents run inside containers (lightweight Linux VMs), providing:
- **Filesystem isolation**: Agents can only access mounted directories
- **Safe Bash access**: Commands run inside the container, not on your host
- **Network isolation**: Can be configured per-container if needed
- **Process isolation**: Container processes can't affect the host
- **Non-root user**: Container runs as unprivileged `node` user (uid 1000)

### Prompt Injection Risk

GitHub events could contain malicious instructions attempting to manipulate Claude's behavior.

**Mitigations:**
- Container isolation limits blast radius
- Only registered repos are processed
- Permission-based access control (configurable per-repo)
- Agents can only access their group's mounted directories
- Claude's built-in safety training

**Recommendations:**
- Only install the GitHub App on trusted repos
- Set appropriate minimum permission levels
- Review scheduled tasks periodically
- Monitor logs for unusual activity

### Credential Storage

| Credential | Storage Location | Notes |
|------------|------------------|-------|
| Claude Auth | data/sessions/{group}/.claude/ | Per-group isolation |
| GitHub App Key | ~/.config/clawcode/github-app.pem | Host only, never mounted |

---

## Troubleshooting

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| No response to events | Service not running | Check service status |
| "Container exited with code 1" | Container runtime failed | Check logs |
| Session not continuing | Session ID not saved | Check SQLite: `sqlite3 store/messages.db "SELECT * FROM sessions"` |
| Permission denied | User lacks required permission level | Check `.github/clawcode.yml` |

### Log Locations

- `logs/clawcode.log` - stdout
- `logs/clawcode.error.log` - stderr
- `groups/{folder}/logs/container-*.log` - Per-container logs

### Debug Mode

```bash
npm run dev
# or
node dist/index.js
```
