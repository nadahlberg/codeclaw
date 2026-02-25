"""In-process MCP tools for ClawCode.

Replaces the stdio MCP server (ipc-mcp-stdio.ts) with in-process tools
using the Python SDK's @tool decorator and create_sdk_mcp_server().
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from croniter import croniter

IPC_DIR = Path("/workspace/ipc")
MESSAGES_DIR = IPC_DIR / "messages"
TASKS_DIR = IPC_DIR / "tasks"


def _write_ipc_file(dir_path: Path, data: dict) -> str:
    """Atomically write a JSON IPC file. Returns the filename."""
    dir_path.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time() * 1000)}-{os.urandom(3).hex()}.json"
    filepath = dir_path / filename
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)
    return filename


def create_ipc_tools(chat_jid: str, group_folder: str, is_main: bool) -> Any:
    """Create in-process MCP tools bound to the current container context.

    Returns an McpSdkServerConfig for use with ClaudeAgentOptions.mcp_servers.
    """

    @tool(
        "send_message",
        "Send a message to the user or group immediately while you're still running. "
        "Use this for progress updates or to send multiple messages. You can call this "
        "multiple times. Note: when running as a scheduled task, your final output is "
        "NOT sent to the user — use this tool if you need to communicate with the user or group.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The message text to send"},
                "sender": {
                    "type": "string",
                    "description": 'Your role/identity name (e.g. "Researcher").',
                },
            },
            "required": ["text"],
        },
    )
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        data = {
            "type": "message",
            "chatJid": chat_jid,
            "text": args["text"],
            "sender": args.get("sender"),
            "groupFolder": group_folder,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_ipc_file(MESSAGES_DIR, data)
        return {"content": [{"type": "text", "text": "Message sent."}]}

    @tool(
        "schedule_task",
        (
            "Schedule a recurring or one-time task. The task will run as a full agent "
            "with access to all tools.\n\n"
            "CONTEXT MODE - Choose based on task type:\n"
            '- "group": Task runs in the group\'s conversation context, with access to '
            "chat history. Use for tasks that need context about ongoing discussions.\n"
            '- "isolated": Task runs in a fresh session with no conversation history. '
            "Use for independent tasks.\n\n"
            "SCHEDULE VALUE FORMAT (all times are LOCAL timezone):\n"
            '- cron: Standard cron expression (e.g., "0 9 * * *" for daily at 9am)\n'
            '- interval: Milliseconds between runs (e.g., "300000" for 5 minutes)\n'
            '- once: Local time WITHOUT "Z" suffix (e.g., "2026-02-01T15:30:00")'
        ),
        {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What the agent should do when the task runs.",
                },
                "schedule_type": {
                    "type": "string",
                    "enum": ["cron", "interval", "once"],
                    "description": "cron=recurring at specific times, interval=recurring every N ms, once=run once",
                },
                "schedule_value": {
                    "type": "string",
                    "description": 'cron: "*/5 * * * *" | interval: "300000" | once: "2026-02-01T15:30:00"',
                },
                "context_mode": {
                    "type": "string",
                    "enum": ["group", "isolated"],
                    "default": "group",
                    "description": "group=runs with chat history, isolated=fresh session",
                },
                "target_group_jid": {
                    "type": "string",
                    "description": "(Main group only) JID of the group to schedule the task for.",
                },
            },
            "required": ["prompt", "schedule_type", "schedule_value"],
        },
    )
    async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
        schedule_type = args["schedule_type"]
        schedule_value = args["schedule_value"]

        # Validate schedule_value
        if schedule_type == "cron":
            try:
                croniter(schedule_value)
            except (ValueError, KeyError):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f'Invalid cron: "{schedule_value}". Use format like "0 9 * * *".',
                        }
                    ],
                    "isError": True,
                }
        elif schedule_type == "interval":
            try:
                ms = int(schedule_value)
                if ms <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f'Invalid interval: "{schedule_value}". Must be positive milliseconds.',
                        }
                    ],
                    "isError": True,
                }
        elif schedule_type == "once":
            import re

            if re.search(r"[Zz]$", schedule_value) or re.search(
                r"[+-]\d{2}:\d{2}$", schedule_value
            ):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f'Timestamp must be local time without timezone suffix. Got "{schedule_value}".',
                        }
                    ],
                    "isError": True,
                }

        target_jid = (
            args.get("target_group_jid", chat_jid)
            if is_main
            else chat_jid
        )

        data = {
            "type": "schedule_task",
            "prompt": args["prompt"],
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "context_mode": args.get("context_mode", "group"),
            "targetJid": target_jid,
            "createdBy": group_folder,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        filename = _write_ipc_file(TASKS_DIR, data)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Task scheduled ({filename}): {schedule_type} - {schedule_value}",
                }
            ]
        }

    @tool(
        "list_tasks",
        "List all scheduled tasks. From main: shows all tasks. From other groups: shows only that group's tasks.",
        {
            "type": "object",
            "properties": {},
        },
    )
    async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        tasks_file = IPC_DIR / "current_tasks.json"

        try:
            if not tasks_file.exists():
                return {
                    "content": [
                        {"type": "text", "text": "No scheduled tasks found."}
                    ]
                }

            all_tasks = json.loads(tasks_file.read_text())
            tasks = (
                all_tasks
                if is_main
                else [t for t in all_tasks if t.get("groupFolder") == group_folder]
            )

            if not tasks:
                return {
                    "content": [
                        {"type": "text", "text": "No scheduled tasks found."}
                    ]
                }

            formatted = "\n".join(
                f"- [{t['id']}] {t['prompt'][:50]}... "
                f"({t['schedule_type']}: {t['schedule_value']}) - "
                f"{t['status']}, next: {t.get('next_run', 'N/A')}"
                for t in tasks
            )
            return {
                "content": [
                    {"type": "text", "text": f"Scheduled tasks:\n{formatted}"}
                ]
            }
        except Exception as err:
            return {
                "content": [
                    {"type": "text", "text": f"Error reading tasks: {err}"}
                ]
            }

    @tool(
        "pause_task",
        "Pause a scheduled task. It will not run until resumed.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to pause"},
            },
            "required": ["task_id"],
        },
    )
    async def pause_task(args: dict[str, Any]) -> dict[str, Any]:
        data = {
            "type": "pause_task",
            "taskId": args["task_id"],
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_ipc_file(TASKS_DIR, data)
        return {
            "content": [
                {"type": "text", "text": f"Task {args['task_id']} pause requested."}
            ]
        }

    @tool(
        "resume_task",
        "Resume a paused task.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to resume"},
            },
            "required": ["task_id"],
        },
    )
    async def resume_task(args: dict[str, Any]) -> dict[str, Any]:
        data = {
            "type": "resume_task",
            "taskId": args["task_id"],
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_ipc_file(TASKS_DIR, data)
        return {
            "content": [
                {"type": "text", "text": f"Task {args['task_id']} resume requested."}
            ]
        }

    @tool(
        "cancel_task",
        "Cancel and delete a scheduled task.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to cancel"},
            },
            "required": ["task_id"],
        },
    )
    async def cancel_task(args: dict[str, Any]) -> dict[str, Any]:
        data = {
            "type": "cancel_task",
            "taskId": args["task_id"],
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_ipc_file(TASKS_DIR, data)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Task {args['task_id']} cancellation requested.",
                }
            ]
        }

    @tool(
        "register_group",
        "Register a new repository or group so the agent can respond to events there. Main group only.",
        {
            "type": "object",
            "properties": {
                "jid": {
                    "type": "string",
                    "description": 'The group JID (e.g., "gh:owner/repo")',
                },
                "name": {
                    "type": "string",
                    "description": "Display name for the group",
                },
                "folder": {
                    "type": "string",
                    "description": 'Folder name for group files (e.g., "owner--repo-name")',
                },
                "trigger": {
                    "type": "string",
                    "description": 'Trigger pattern (e.g., "@bot-name")',
                },
            },
            "required": ["jid", "name", "folder", "trigger"],
        },
    )
    async def register_group(args: dict[str, Any]) -> dict[str, Any]:
        if not is_main:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Only the main group can register new groups.",
                    }
                ],
                "isError": True,
            }

        data = {
            "type": "register_group",
            "jid": args["jid"],
            "name": args["name"],
            "folder": args["folder"],
            "trigger": args["trigger"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_ipc_file(TASKS_DIR, data)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f'Group "{args["name"]}" registered. It will start receiving events immediately.',
                }
            ]
        }

    # --- GitHub-specific tools ---

    @tool(
        "github_comment",
        "Post a comment on a GitHub issue or pull request. Defaults to the current thread.",
        {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The comment body (Markdown supported)",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "Issue/PR number. Defaults to current thread.",
                },
            },
            "required": ["text"],
        },
    )
    async def github_comment(args: dict[str, Any]) -> dict[str, Any]:
        target_jid = chat_jid
        if args.get("issue_number"):
            target_jid = chat_jid.split("#")[0] + f"#issue:{args['issue_number']}"

        data = {
            "type": "github_comment",
            "chatJid": target_jid,
            "text": args["text"],
            "groupFolder": group_folder,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_ipc_file(MESSAGES_DIR, data)
        return {"content": [{"type": "text", "text": "Comment posted."}]}

    @tool(
        "github_review",
        "Submit a review on a pull request (approve, request changes, or comment).",
        {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Overall review comment",
                },
                "event": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                    "description": "Review action",
                },
                "pr_number": {
                    "type": "integer",
                    "description": "PR number. Defaults to current thread.",
                },
                "comments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "File path relative to repo root",
                            },
                            "line": {
                                "type": "integer",
                                "description": "Line number in the diff",
                            },
                            "body": {
                                "type": "string",
                                "description": "Inline comment text",
                            },
                        },
                        "required": ["path", "line", "body"],
                    },
                    "description": "Optional inline comments on specific lines",
                },
            },
            "required": ["body", "event"],
        },
    )
    async def github_review(args: dict[str, Any]) -> dict[str, Any]:
        target_jid = chat_jid
        if args.get("pr_number"):
            target_jid = chat_jid.split("#")[0] + f"#pr:{args['pr_number']}"

        data = {
            "type": "github_review",
            "chatJid": target_jid,
            "body": args["body"],
            "event": args["event"],
            "comments": args.get("comments"),
            "groupFolder": group_folder,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_ipc_file(MESSAGES_DIR, data)
        return {
            "content": [
                {"type": "text", "text": f"Review submitted ({args['event']})."}
            ]
        }

    @tool(
        "github_create_pr",
        "Create a new pull request. Make sure to commit and push changes to a branch first using git/gh CLI.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "body": {
                    "type": "string",
                    "description": "PR description (Markdown supported)",
                },
                "head": {"type": "string", "description": "Source branch name"},
                "base": {
                    "type": "string",
                    "description": "Target branch (default: main)",
                    "default": "main",
                },
            },
            "required": ["title", "body", "head"],
        },
    )
    async def github_create_pr(args: dict[str, Any]) -> dict[str, Any]:
        data = {
            "type": "github_create_pr",
            "chatJid": chat_jid,
            "title": args["title"],
            "body": args["body"],
            "head": args["head"],
            "base": args.get("base", "main"),
            "groupFolder": group_folder,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_ipc_file(MESSAGES_DIR, data)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f'PR creation requested: "{args["title"]}" ({args["head"]} -> {args.get("base", "main")})',
                }
            ]
        }

    return create_sdk_mcp_server(
        name="clawcode",
        tools=[
            send_message,
            schedule_task,
            list_tasks,
            pause_task,
            resume_task,
            cancel_task,
            register_group,
            github_comment,
            github_review,
            github_create_pr,
        ],
    )
