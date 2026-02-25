"""ClawCode Agent Runner.

Runs inside a container, receives config via stdin, outputs result to stdout.

Input protocol:
    Stdin: Full ContainerInput JSON (read until EOF)
    IPC:   Follow-up messages written as JSON files to /workspace/ipc/input/
           Files: {type:"message", text:"..."}.json -- polled and consumed
           Sentinel: /workspace/ipc/input/_close -- signals session end

Stdout protocol:
    Each result is wrapped in OUTPUT_START_MARKER / OUTPUT_END_MARKER pairs.
    Multiple results may be emitted (one per agent teams result).
    Final marker after loop ends signals completion.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
)

from ipc_tools import create_ipc_tools

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_INPUT_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
IPC_POLL_SECS = 0.5

OUTPUT_START_MARKER = "---CLAWCODE_OUTPUT_START---"
OUTPUT_END_MARKER = "---CLAWCODE_OUTPUT_END---"

SECRET_ENV_VARS = ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ContainerInput:
    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None
    secrets: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_output(output: dict) -> None:
    """Write a structured output block to stdout."""
    print(OUTPUT_START_MARKER, flush=True)
    print(json.dumps(output), flush=True)
    print(OUTPUT_END_MARKER, flush=True)


def log(message: str) -> None:
    """Log to stderr (visible to the host process)."""
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# IPC input helpers
# ---------------------------------------------------------------------------


def should_close() -> bool:
    """Check for _close sentinel."""
    if IPC_INPUT_CLOSE_SENTINEL.exists():
        try:
            IPC_INPUT_CLOSE_SENTINEL.unlink()
        except OSError:
            pass
        return True
    return False


def drain_ipc_input() -> list[str]:
    """Drain all pending IPC input messages. Returns message texts."""
    try:
        IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in IPC_INPUT_DIR.iterdir() if f.suffix == ".json")
        messages: list[str] = []
        for file_path in files:
            try:
                data = json.loads(file_path.read_text())
                file_path.unlink()
                if data.get("type") == "message" and data.get("text"):
                    messages.append(data["text"])
            except Exception as err:
                log(f"Failed to process input file {file_path.name}: {err}")
                try:
                    file_path.unlink()
                except OSError:
                    pass
        return messages
    except Exception as err:
        log(f"IPC drain error: {err}")
        return []


async def wait_for_ipc_message() -> str | None:
    """Wait for a new IPC message or _close sentinel.

    Returns the concatenated message text, or None if _close.
    """
    while True:
        if should_close():
            return None
        messages = drain_ipc_input()
        if messages:
            return "\n".join(messages)
        await asyncio.sleep(IPC_POLL_SECS)


# ---------------------------------------------------------------------------
# Transcript archiving (PreCompact hook)
# ---------------------------------------------------------------------------


@dataclass
class ParsedMessage:
    role: str  # 'user' | 'assistant'
    content: str


def parse_transcript(content: str) -> list[ParsedMessage]:
    """Parse a JSONL transcript into user/assistant messages."""
    messages: list[ParsedMessage] = []
    for line in content.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "user" and entry.get("message", {}).get("content"):
                raw = entry["message"]["content"]
                if isinstance(raw, str):
                    text = raw
                else:
                    text = "".join(c.get("text", "") for c in raw)
                if text:
                    messages.append(ParsedMessage(role="user", content=text))
            elif entry.get("type") == "assistant" and entry.get("message", {}).get(
                "content"
            ):
                text_parts = [
                    c.get("text", "")
                    for c in entry["message"]["content"]
                    if c.get("type") == "text"
                ]
                text = "".join(text_parts)
                if text:
                    messages.append(ParsedMessage(role="assistant", content=text))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return messages


def get_session_summary(session_id: str, transcript_path: str) -> str | None:
    """Get session summary from sessions-index.json."""
    project_dir = Path(transcript_path).parent
    index_path = project_dir / "sessions-index.json"

    if not index_path.exists():
        return None

    try:
        index = json.loads(index_path.read_text())
        for entry in index.get("entries", []):
            if entry.get("sessionId") == session_id and entry.get("summary"):
                return entry["summary"]
    except Exception as err:
        log(f"Failed to read sessions index: {err}")

    return None


def sanitize_filename(summary: str) -> str:
    """Sanitize summary text for use as a filename."""
    name = re.sub(r"[^a-z0-9]+", "-", summary.lower())
    return name.strip("-")[:50]


def generate_fallback_name() -> str:
    """Generate a fallback conversation filename."""
    now = datetime.now()
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def format_transcript_markdown(
    messages: list[ParsedMessage],
    title: str | None = None,
    assistant_name: str | None = None,
) -> str:
    """Format parsed messages as a Markdown transcript."""
    now = datetime.now()
    date_str = now.strftime("%b %d, %I:%M %p")

    lines: list[str] = [
        f"# {title or 'Conversation'}",
        "",
        f"Archived: {date_str}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        sender = "User" if msg.role == "user" else (assistant_name or "Assistant")
        content = msg.content[:2000] + "..." if len(msg.content) > 2000 else msg.content
        lines.append(f"**{sender}**: {content}")
        lines.append("")

    return "\n".join(lines)


def create_pre_compact_hook(assistant_name: str | None = None):
    """Create a PreCompact hook that archives transcripts before compaction."""

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        transcript_path = input_data.get("transcript_path", "")
        session_id = input_data.get("session_id", "")

        if not transcript_path or not Path(transcript_path).exists():
            log("No transcript found for archiving")
            return {}

        try:
            content = Path(transcript_path).read_text()
            messages = parse_transcript(content)

            if not messages:
                log("No messages to archive")
                return {}

            summary = get_session_summary(session_id, transcript_path)
            name = sanitize_filename(summary) if summary else generate_fallback_name()

            conversations_dir = Path("/workspace/group/conversations")
            conversations_dir.mkdir(parents=True, exist_ok=True)

            date = datetime.now().strftime("%Y-%m-%d")
            filename = f"{date}-{name}.md"
            filepath = conversations_dir / filename

            markdown = format_transcript_markdown(messages, summary, assistant_name)
            filepath.write_text(markdown)

            log(f"Archived conversation to {filepath}")
        except Exception as err:
            log(f"Failed to archive transcript: {err}")

        return {}

    return hook


def create_sanitize_bash_hook():
    """Create a PreToolUse hook that strips secret env vars from Bash commands."""

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        command = input_data.get("tool_input", {}).get("command")
        if not command:
            return {}

        unset_prefix = f"unset {' '.join(SECRET_ENV_VARS)} 2>/dev/null; "
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": {
                    **input_data.get("tool_input", {}),
                    "command": unset_prefix + command,
                },
            },
        }

    return hook


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # Read and parse stdin
    try:
        stdin_data = sys.stdin.read()
        raw = json.loads(stdin_data)
        container_input = ContainerInput(
            prompt=raw["prompt"],
            group_folder=raw["groupFolder"],
            chat_jid=raw["chatJid"],
            is_main=raw["isMain"],
            session_id=raw.get("sessionId"),
            is_scheduled_task=raw.get("isScheduledTask", False),
            assistant_name=raw.get("assistantName"),
            secrets=raw.get("secrets"),
        )
        log(f"Received input for group: {container_input.group_folder}")
    except Exception as err:
        write_output(
            {"status": "error", "result": None, "error": f"Failed to parse input: {err}"}
        )
        sys.exit(1)

    # Build SDK env: merge secrets for the SDK process only
    sdk_env: dict[str, str] = {k: v for k, v in os.environ.items()}
    for key, value in (container_input.secrets or {}).items():
        sdk_env[key] = value

    # GitHub token: set in process env so gh CLI and git can use it from Bash
    if container_input.secrets and container_input.secrets.get("GITHUB_TOKEN"):
        os.environ["GH_TOKEN"] = container_input.secrets["GITHUB_TOKEN"]
        os.environ["GITHUB_TOKEN"] = container_input.secrets["GITHUB_TOKEN"]

    # Create in-process MCP tools
    clawcode_server = create_ipc_tools(
        chat_jid=container_input.chat_jid,
        group_folder=container_input.group_folder,
        is_main=container_input.is_main,
    )

    # Prepare IPC input directory and clean stale sentinel
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        IPC_INPUT_CLOSE_SENTINEL.unlink()
    except OSError:
        pass

    # Build initial prompt
    prompt = container_input.prompt
    if container_input.is_scheduled_task:
        prompt = (
            "[SCHEDULED TASK - The following message was sent automatically "
            "and is not coming directly from the user or group.]\n\n" + prompt
        )
    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    # Load global CLAUDE.md for non-main groups
    global_claude_md_path = Path("/workspace/global/CLAUDE.md")
    system_prompt: dict[str, Any] | None = None
    if not container_input.is_main and global_claude_md_path.exists():
        global_claude_md = global_claude_md_path.read_text()
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": global_claude_md,
        }

    # Discover additional directories mounted at /workspace/extra/*
    extra_dirs: list[str] = []
    extra_base = Path("/workspace/extra")
    if extra_base.exists():
        for entry in extra_base.iterdir():
            if entry.is_dir():
                extra_dirs.append(str(entry))
    if extra_dirs:
        log(f"Additional directories: {', '.join(extra_dirs)}")

    # Build SDK options
    options = ClaudeAgentOptions(
        cwd="/workspace/group",
        add_dirs=extra_dirs if extra_dirs else [],
        resume=container_input.session_id,
        system_prompt=system_prompt,
        allowed_tools=[
            "Bash",
            "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "Task", "TaskOutput", "TaskStop",
            "TeamCreate", "TeamDelete", "SendMessage",
            "TodoWrite", "ToolSearch", "Skill",
            "NotebookEdit",
            "mcp__clawcode__*",
        ],
        env=sdk_env,
        permission_mode="bypassPermissions",
        setting_sources=["project", "user"],
        mcp_servers={"clawcode": clawcode_server},
        hooks={
            "PreCompact": [
                HookMatcher(hooks=[create_pre_compact_hook(container_input.assistant_name)])
            ],
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[create_sanitize_bash_hook()])
            ],
        },
    )

    session_id: str | None = container_input.session_id

    try:
        async with ClaudeSDKClient(options=options) as client:
            while True:
                log(f"Starting query (session: {session_id or 'new'})...")

                await client.query(prompt)

                result_count = 0
                message_count = 0

                async for message in client.receive_response():
                    message_count += 1

                    if isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            new_sid = message.data.get("session_id")
                            if new_sid:
                                session_id = new_sid
                                log(f"Session initialized: {session_id}")

                    if isinstance(message, ResultMessage):
                        result_count += 1
                        result_text = message.result
                        log(
                            f"Result #{result_count}: subtype={message.subtype}"
                            f"{f' text={result_text[:200]}' if result_text else ''}"
                        )
                        write_output(
                            {
                                "status": "success",
                                "result": result_text,
                                "newSessionId": session_id,
                            }
                        )

                log(
                    f"Query done. Messages: {message_count}, results: {result_count}"
                )

                # Check for close sentinel consumed during query
                if should_close():
                    log("Close sentinel consumed during query, exiting")
                    break

                # Emit session update so host can track it
                write_output(
                    {"status": "success", "result": None, "newSessionId": session_id}
                )

                log("Query ended, waiting for next IPC message...")

                # Wait for the next message or _close sentinel
                next_message = await wait_for_ipc_message()
                if next_message is None:
                    log("Close sentinel received, exiting")
                    break

                log(f"Got new message ({len(next_message)} chars), starting new query")
                prompt = next_message

    except Exception as err:
        error_message = str(err)
        log(f"Agent error: {error_message}")
        write_output(
            {
                "status": "error",
                "result": None,
                "newSessionId": session_id,
                "error": error_message,
            }
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
