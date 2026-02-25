"""Task Scheduler.

Runs scheduled tasks by polling the database for due tasks.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Coroutine

from croniter import croniter

from codeclaw.config import ASSISTANT_NAME, MAIN_GROUP_FOLDER, SCHEDULER_POLL_INTERVAL, TIMEZONE
from codeclaw.container_runner import ContainerInput, ContainerOutput, run_container_agent, write_tasks_snapshot
from codeclaw.db import get_all_tasks, get_due_tasks, get_task_by_id, log_task_run, update_task, update_task_after_run
from codeclaw.group_folder import resolve_group_folder_path
from codeclaw.group_queue import GroupQueue
from codeclaw.logger import logger
from codeclaw.models import RegisteredGroup, ScheduledTask, TaskRunLog


class SchedulerDependencies:
    def __init__(
        self,
        registered_groups: Callable[[], dict[str, RegisteredGroup]],
        get_sessions: Callable[[], dict[str, str]],
        queue: GroupQueue,
        on_process: Callable,
        send_message: Callable[[str, str], Coroutine],
    ) -> None:
        self.registered_groups = registered_groups
        self.get_sessions = get_sessions
        self.queue = queue
        self.on_process = on_process
        self.send_message = send_message


async def _run_task(task: ScheduledTask, deps: SchedulerDependencies) -> None:
    start_time = time.time()

    try:
        group_dir = resolve_group_folder_path(task.group_folder)
    except Exception as err:
        update_task(task.id, status="paused")
        logger.error("Task has invalid group folder", task_id=task.id, group_folder=task.group_folder, error=str(err))
        log_task_run(TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=int((time.time() - start_time) * 1000),
            status="error",
            error=str(err),
        ))
        return

    Path(group_dir).mkdir(parents=True, exist_ok=True)
    logger.info("Running scheduled task", task_id=task.id, group=task.group_folder)

    groups = deps.registered_groups()
    group = next((g for g in groups.values() if g.folder == task.group_folder), None)

    if not group:
        logger.error("Group not found for task", task_id=task.id, group_folder=task.group_folder)
        log_task_run(TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=int((time.time() - start_time) * 1000),
            status="error",
            error=f"Group not found: {task.group_folder}",
        ))
        return

    is_main = task.group_folder == MAIN_GROUP_FOLDER
    all_tasks = get_all_tasks()
    write_tasks_snapshot(
        task.group_folder,
        is_main,
        [
            {
                "id": t.id,
                "groupFolder": t.group_folder,
                "prompt": t.prompt,
                "schedule_type": t.schedule_type,
                "schedule_value": t.schedule_value,
                "status": t.status,
                "next_run": t.next_run,
            }
            for t in all_tasks
        ],
    )

    result: str | None = None
    error: str | None = None

    sessions = deps.get_sessions()
    session_id = sessions.get(task.group_folder) if task.context_mode == "group" else None

    TASK_CLOSE_DELAY = 10.0  # seconds
    close_handle: asyncio.TimerHandle | None = None

    def schedule_close():
        nonlocal close_handle
        if close_handle:
            return
        loop = asyncio.get_event_loop()
        close_handle = loop.call_later(TASK_CLOSE_DELAY, lambda: deps.queue.close_stdin(task.chat_jid))

    try:
        async def on_output(streamed_output: ContainerOutput):
            nonlocal result, error
            if streamed_output.result:
                result = streamed_output.result
                await deps.send_message(task.chat_jid, streamed_output.result)
                schedule_close()
            if streamed_output.status == "success":
                deps.queue.notify_idle(task.chat_jid)
            if streamed_output.status == "error":
                error = streamed_output.error or "Unknown error"

        output = await run_container_agent(
            group,
            ContainerInput(
                prompt=task.prompt,
                session_id=session_id,
                group_folder=task.group_folder,
                chat_jid=task.chat_jid,
                is_main=is_main,
                is_scheduled_task=True,
                assistant_name=ASSISTANT_NAME,
            ),
            lambda proc, name: deps.on_process(task.chat_jid, proc, name, task.group_folder),
            on_output,
        )

        if close_handle:
            close_handle.cancel()

        if output.status == "error":
            error = output.error or "Unknown error"
        elif output.result:
            result = output.result

        logger.info("Task completed", task_id=task.id, duration_ms=int((time.time() - start_time) * 1000))
    except Exception as err:
        if close_handle:
            close_handle.cancel()
        error = str(err)
        logger.error("Task failed", task_id=task.id, error=error)

    duration_ms = int((time.time() - start_time) * 1000)

    log_task_run(TaskRunLog(
        task_id=task.id,
        run_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=duration_ms,
        status="error" if error else "success",
        result=result,
        error=error,
    ))

    next_run: str | None = None
    if task.schedule_type == "cron":
        cron = croniter(task.schedule_value)
        next_run = cron.get_next(datetime).isoformat()
    elif task.schedule_type == "interval":
        ms = int(task.schedule_value)
        next_run = datetime.fromtimestamp(time.time() + ms / 1000, tz=timezone.utc).isoformat()

    result_summary = f"Error: {error}" if error else (result[:200] if result else "Completed")
    update_task_after_run(task.id, next_run, result_summary)


_scheduler_running = False


async def start_scheduler_loop(deps: SchedulerDependencies) -> None:
    global _scheduler_running
    if _scheduler_running:
        logger.debug("Scheduler loop already running, skipping duplicate start")
        return
    _scheduler_running = True
    logger.info("Scheduler loop started")

    async def loop():
        while True:
            try:
                due_tasks = get_due_tasks()
                if due_tasks:
                    logger.info("Found due tasks", count=len(due_tasks))

                for task in due_tasks:
                    current_task = get_task_by_id(task.id)
                    if not current_task or current_task.status != "active":
                        continue

                    deps.queue.enqueue_task(
                        current_task.chat_jid,
                        current_task.id,
                        lambda t=current_task: _run_task(t, deps),
                    )
            except Exception as err:
                logger.error("Error in scheduler loop", error=str(err))

            await asyncio.sleep(SCHEDULER_POLL_INTERVAL / 1000)

    asyncio.create_task(loop())
