# ClawCode Debug Checklist

## Known Issues (2026-02-08)

### 1. [FIXED] Resume branches from stale tree position
When agent teams spawns subagent CLI processes, they write to the same session JSONL. On subsequent `query()` resumes, the CLI reads the JSONL but may pick a stale branch tip (from before the subagent activity), causing the agent's response to land on a branch the host never receives a `result` for. **Fix**: pass `resumeSessionAt` with the last assistant message UUID to explicitly anchor each resume.

### 2. IDLE_TIMEOUT == CONTAINER_TIMEOUT (both 30 min)
Both timers fire at the same time, so containers always exit via hard SIGKILL (code 137) instead of graceful `_close` sentinel shutdown. The idle timeout should be shorter (e.g., 5 min) so containers wind down between messages, while container timeout stays at 30 min as a safety net for stuck agents.

### 3. Cursor advanced before agent succeeds
`processGroupMessages` advances `lastAgentTimestamp` before the agent runs. If the container times out, retries find no messages (cursor already past them). Messages are permanently lost on timeout.

## Quick Status Check

```bash
# 1. Is the service running?
launchctl list | grep clawcode  # macOS
systemctl --user status clawcode  # Linux

# 2. Any running containers?
container ls --format '{{.Names}} {{.Status}}' 2>/dev/null | grep clawcode

# 3. Any stopped/orphaned containers?
container ls -a --format '{{.Names}} {{.Status}}' 2>/dev/null | grep clawcode

# 4. Recent errors in service log?
grep -E 'ERROR|WARN' logs/clawcode.log | tail -20

# 5. Is the webhook server running?
grep -E 'Webhook server|listening' logs/clawcode.log | tail -5

# 6. Are repos registered?
grep -E 'groupCount|registered' logs/clawcode.log | tail -3
```

## Session Transcript Branching

```bash
# Check for concurrent CLI processes in session debug logs
ls -la data/sessions/<group>/.claude/debug/

# Count unique SDK processes that handled messages
# Each .txt file = one CLI subprocess. Multiple = concurrent queries.

# Check parentUuid branching in transcript
python3 -c "
import json, sys
lines = open('data/sessions/<group>/.claude/projects/-workspace-group/<session>.jsonl').read().strip().split('\n')
for i, line in enumerate(lines):
  try:
    d = json.loads(line)
    if d.get('type') == 'user' and d.get('message'):
      parent = d.get('parentUuid', 'ROOT')[:8]
      content = str(d['message'].get('content', ''))[:60]
      print(f'L{i+1} parent={parent} {content}')
  except: pass
"
```

## Container Timeout Investigation

```bash
# Check for recent timeouts
grep -E 'Container timeout|timed out' logs/clawcode.log | tail -10

# Check container log files for the timed-out container
ls -lt groups/*/logs/container-*.log | head -10

# Read the most recent container log (replace path)
cat groups/<group>/logs/container-<timestamp>.log

# Check if retries were scheduled and what happened
grep -E 'Scheduling retry|retry|Max retries' logs/clawcode.log | tail -10
```

## Agent Not Responding

```bash
# Check if webhook events are being received
grep -E 'webhook|event' logs/clawcode.log | tail -10

# Check if events are being processed (container spawned)
grep -E 'Processing|Spawning container' logs/clawcode.log | tail -10

# Check the queue state — any active containers?
grep -E 'Starting container|Container active|concurrency limit' logs/clawcode.log | tail -10
```

## Container Mount Issues

```bash
# Check mount validation logs (shows on container spawn)
grep -E 'Mount validated|Mount.*REJECTED|mount' logs/clawcode.log | tail -10

# Verify the mount allowlist is readable
cat ~/.config/clawcode/mount-allowlist.json

# Check group's container_config in DB
sqlite3 store/messages.db "SELECT name, container_config FROM registered_groups;"

# Test-run a container to check mounts (dry run)
container run -i --rm --entrypoint ls clawcode-agent:latest /workspace/extra/
```

## GitHub App Auth Issues

```bash
# Check for authentication errors
grep -E 'auth\|token\|JWT\|401\|403' logs/clawcode.log | tail -10

# Verify GitHub App credentials exist
test -f .env && grep -c GITHUB_APP_ID .env
test -f ~/.config/clawcode/github-app.pem && echo "PEM exists" || echo "PEM missing"
```

## Service Management

```bash
# macOS (launchd)
launchctl kickstart -k gui/$(id -u)/com.clawcode  # restart
tail -f logs/clawcode.log                          # view live logs
launchctl bootout gui/$(id -u)/com.clawcode        # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.clawcode.plist  # start

# Linux (systemd)
systemctl --user restart clawcode
journalctl --user -u clawcode -f                   # view live logs

# Rebuild after code changes
npm run build && launchctl kickstart -k gui/$(id -u)/com.clawcode
```
