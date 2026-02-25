"""IPC Watcher.

Polls per-group IPC directories for messages and tasks written by container agents.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from croniter import croniter

from codeclaw.config import DATA_DIR, IPC_POLL_INTERVAL, MAIN_GROUP_FOLDER, TIMEZONE
from codeclaw.db import create_task, delete_task, get_task_by_id, update_task
from codeclaw.group_folder import is_valid_group_folder
from codeclaw.logger import logger
from codeclaw.models import RegisteredGroup, ScheduledTask


class IpcDeps:
    def __init__(
        self,
        send_message: Callable[[str, str], Coroutine],
        send_structured_message: Callable[[str, str, Any], Coroutine] | None,
        registered_groups: Callable[[], dict[str, RegisteredGroup]],
        register_group: Callable[[str, RegisteredGroup], None],
        sync_group_metadata: Callable[[bool], Coroutine],
        get_available_groups: Callable[[], list[dict]],
        write_groups_snapshot: Callable[[str, bool, list[dict], set[str]], None],
    ) -> None:
        self.send_message = send_message
        self.send_structured_message = send_structured_message
        self.registered_groups = registered_groups
        self.register_group = register_group
        self.sync_group_metadata = sync_group_metadata
        self.get_available_groups = get_available_groups
        self.write_groups_snapshot = write_groups_snapshot


_ipc_watcher_running = False


async def start_ipc_watcher(deps: IpcDeps) -> None:
    global _ipc_watcher_running
    if _ipc_watcher_running:
        logger.debug("IPC watcher already running, skipping duplicate start")
        return
    _ipc_watcher_running = True

    ipc_base_dir = DATA_DIR / "ipc"
    ipc_base_dir.mkdir(parents=True, exist_ok=True)

    async def process_ipc_files():
        while True:
            try:
                group_folders = [
                    f.name
                    for f in ipc_base_dir.iterdir()
                    if f.is_dir() and f.name != "errors"
                ]
            except Exception as err:
                logger.error("Error reading IPC base directory", error=str(err))
                await asyncio.sleep(IPC_POLL_INTERVAL / 1000)
                continue

            registered_groups = deps.registered_groups()

            for source_group in group_folders:
                is_main = source_group == MAIN_GROUP_FOLDER
                messages_dir = ipc_base_dir / source_group / "messages"
                tasks_dir = ipc_base_dir / source_group / "tasks"

                # Process messages
                try:
                    if messages_dir.exists():
                        for file_path in sorted(messages_dir.glob("*.json")):
                            try:
                                data = json.loads(file_path.read_text())
                                chat_jid = data.get("chatJid")
                                if chat_jid:
                                    repo_jid = chat_jid.split("#")[0] if chat_jid.startswith("gh:") else chat_jid
                                    target_group = registered_groups.get(repo_jid) or registered_groups.get(chat_jid)
                                    authorized = is_main or (target_group and target_group.folder == source_group)

                                    if not authorized:
                                        logger.warning("Unauthorized IPC message attempt blocked", chat_jid=chat_jid, source_group=source_group)
                                    elif data.get("type") == "message" and data.get("text"):
                                        await deps.send_message(chat_jid, data["text"])
                                    elif data.get("type") == "github_comment" and data.get("text") and deps.send_structured_message:
                                        target = {
                                            "type": "pr_comment" if "#pr:" in chat_jid else "issue_comment",
                                            "issue_number": data.get("issueNumber"),
                                            "pr_number": data.get("prNumber"),
                                        }
                                        await deps.send_structured_message(chat_jid, data["text"], target)
                                    elif data.get("type") == "github_review" and data.get("body") and deps.send_structured_message:
                                        target = {
                                            "type": "pr_review",
                                            "pr_number": data.get("prNumber"),
                                            "review_action": data.get("event"),
                                            "review_comments": data.get("comments"),
                                        }
                                        await deps.send_structured_message(chat_jid, data["body"], target)
                                    elif data.get("type") == "github_create_pr" and data.get("title") and deps.send_structured_message:
                                        target = {
                                            "type": "new_pr",
                                            "title": data.get("title"),
                                            "head": data.get("head"),
                                            "base": data.get("base"),
                                        }
                                        await deps.send_structured_message(chat_jid, data.get("body", ""), target)

                                file_path.unlink()
                            except Exception as err:
                                logger.error("Error processing IPC message", file=file_path.name, source_group=source_group, error=str(err))
                                error_dir = ipc_base_dir / "errors"
                                error_dir.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(file_path), str(error_dir / f"{source_group}-{file_path.name}"))
                except Exception as err:
                    logger.error("Error reading IPC messages directory", source_group=source_group, error=str(err))

                # Process tasks
                try:
                    if tasks_dir.exists():
                        for file_path in sorted(tasks_dir.glob("*.json")):
                            try:
                                data = json.loads(file_path.read_text())
                                await process_task_ipc(data, source_group, is_main, deps)
                                file_path.unlink()
                            except Exception as err:
                                logger.error("Error processing IPC task", file=file_path.name, source_group=source_group, error=str(err))
                                error_dir = ipc_base_dir / "errors"
                                error_dir.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(file_path), str(error_dir / f"{source_group}-{file_path.name}"))
                except Exception as err:
                    logger.error("Error reading IPC tasks directory", source_group=source_group, error=str(err))

            await asyncio.sleep(IPC_POLL_INTERVAL / 1000)

    asyncio.create_task(process_ipc_files())
    logger.info("IPC watcher started (per-group namespaces)")


async def process_task_ipc(
    data: dict,
    source_group: str,
    is_main: bool,
    deps: IpcDeps,
) -> None:
    registered_groups = deps.registered_groups()

    if data["type"] == "schedule_task":
        if data.get("prompt") and data.get("schedule_type") and data.get("schedule_value") and data.get("targetJid"):
            target_jid = data["targetJid"]
            target_group_entry = registered_groups.get(target_jid)
            if not target_group_entry:
                logger.warning("Cannot schedule task: target group not registered", target_jid=target_jid)
                return

            target_folder = target_group_entry.folder
            if not is_main and target_folder != source_group:
                logger.warning("Unauthorized schedule_task attempt blocked", source_group=source_group, target_folder=target_folder)
                return

            schedule_type = data["schedule_type"]
            next_run: str | None = None

            if schedule_type == "cron":
                try:
                    cron = croniter(data["schedule_value"])
                    next_run = cron.get_next(datetime).isoformat()
                except Exception:
                    logger.warning("Invalid cron expression", schedule_value=data["schedule_value"])
                    return
            elif schedule_type == "interval":
                ms = int(data["schedule_value"])
                if ms <= 0:
                    logger.warning("Invalid interval", schedule_value=data["schedule_value"])
                    return
                next_run = datetime.fromtimestamp(time.time() + ms / 1000, tz=timezone.utc).isoformat()
            elif schedule_type == "once":
                next_run = data["schedule_value"]

            task_id = f"task-{int(time.time() * 1000)}-{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))}"
            context_mode = data.get("context_mode", "isolated")
            if context_mode not in ("group", "isolated"):
                context_mode = "isolated"

            create_task(ScheduledTask(
                id=task_id,
                group_folder=target_folder,
                chat_jid=target_jid,
                prompt=data["prompt"],
                schedule_type=schedule_type,
                schedule_value=data["schedule_value"],
                context_mode=context_mode,
                next_run=next_run,
                status="active",
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            logger.info("Task created via IPC", task_id=task_id, source_group=source_group, target_folder=target_folder)

    elif data["type"] == "pause_task":
        if data.get("taskId"):
            task = get_task_by_id(data["taskId"])
            if task and (is_main or task.group_folder == source_group):
                update_task(data["taskId"], status="paused")
                logger.info("Task paused via IPC", task_id=data["taskId"], source_group=source_group)
            else:
                logger.warning("Unauthorized task pause attempt", task_id=data["taskId"], source_group=source_group)

    elif data["type"] == "resume_task":
        if data.get("taskId"):
            task = get_task_by_id(data["taskId"])
            if task and (is_main or task.group_folder == source_group):
                update_task(data["taskId"], status="active")
                logger.info("Task resumed via IPC", task_id=data["taskId"], source_group=source_group)
            else:
                logger.warning("Unauthorized task resume attempt", task_id=data["taskId"], source_group=source_group)

    elif data["type"] == "cancel_task":
        if data.get("taskId"):
            task = get_task_by_id(data["taskId"])
            if task and (is_main or task.group_folder == source_group):
                delete_task(data["taskId"])
                logger.info("Task cancelled via IPC", task_id=data["taskId"], source_group=source_group)
            else:
                logger.warning("Unauthorized task cancel attempt", task_id=data["taskId"], source_group=source_group)

    elif data["type"] == "refresh_groups":
        if is_main:
            logger.info("Group metadata refresh requested via IPC", source_group=source_group)
            await deps.sync_group_metadata(True)
            available_groups = deps.get_available_groups()
            deps.write_groups_snapshot(source_group, True, available_groups, set(registered_groups.keys()))
        else:
            logger.warning("Unauthorized refresh_groups attempt blocked", source_group=source_group)

    elif data["type"] == "register_group":
        if not is_main:
            logger.warning("Unauthorized register_group attempt blocked", source_group=source_group)
            return
        if data.get("jid") and data.get("name") and data.get("folder") and data.get("trigger"):
            if not is_valid_group_folder(data["folder"]):
                logger.warning("Invalid register_group request - unsafe folder name", source_group=source_group, folder=data["folder"])
                return
            deps.register_group(data["jid"], RegisteredGroup(
                name=data["name"],
                folder=data["folder"],
                trigger=data["trigger"],
                added_at=datetime.now(timezone.utc).isoformat(),
                container_config=data.get("containerConfig"),
                requires_trigger=data.get("requiresTrigger"),
            ))
    else:
        logger.warning("Unknown IPC task type", type=data["type"])
