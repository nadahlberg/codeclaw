"""Group Queue.

Manages per-group concurrency, message queuing, and container lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Coroutine

from codeclaw.config import DATA_DIR, MAX_CONCURRENT_CONTAINERS
from codeclaw.logger import logger

MAX_RETRIES = 5
BASE_RETRY_MS = 5000


@dataclass
class QueuedTask:
    id: str
    group_jid: str
    fn: Callable[[], Coroutine]


@dataclass
class GroupState:
    active: bool = False
    idle_waiting: bool = False
    is_task_container: bool = False
    pending_messages: bool = False
    pending_tasks: list[QueuedTask] = field(default_factory=list)
    process: object | None = None
    container_name: str | None = None
    group_folder: str | None = None
    retry_count: int = 0


class GroupQueue:
    def __init__(self) -> None:
        self._groups: dict[str, GroupState] = {}
        self._active_count = 0
        self._waiting_groups: list[str] = []
        self._process_messages_fn: Callable[[str], Coroutine[None, None, bool]] | None = None
        self._shutting_down = False

    def _get_group(self, group_jid: str) -> GroupState:
        if group_jid not in self._groups:
            self._groups[group_jid] = GroupState()
        return self._groups[group_jid]

    def set_process_messages_fn(self, fn: Callable[[str], Coroutine[None, None, bool]]) -> None:
        self._process_messages_fn = fn

    def enqueue_message_check(self, group_jid: str) -> None:
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        if state.active:
            state.pending_messages = True
            logger.debug("Container active, message queued", group_jid=group_jid)
            return

        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_messages = True
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug("At concurrency limit, message queued", group_jid=group_jid, active_count=self._active_count)
            return

        self._activate(state)
        asyncio.ensure_future(self._run_for_group(group_jid, "messages"))

    def enqueue_task(self, group_jid: str, task_id: str, fn: Callable[[], Coroutine]) -> None:
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        if any(t.id == task_id for t in state.pending_tasks):
            logger.debug("Task already queued, skipping", group_jid=group_jid, task_id=task_id)
            return

        if state.active:
            state.pending_tasks.append(QueuedTask(id=task_id, group_jid=group_jid, fn=fn))
            if state.idle_waiting:
                self.close_stdin(group_jid)
            logger.debug("Container active, task queued", group_jid=group_jid, task_id=task_id)
            return

        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_tasks.append(QueuedTask(id=task_id, group_jid=group_jid, fn=fn))
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug("At concurrency limit, task queued", group_jid=group_jid, task_id=task_id, active_count=self._active_count)
            return

        self._activate(state)
        asyncio.ensure_future(self._run_task(group_jid, QueuedTask(id=task_id, group_jid=group_jid, fn=fn)))

    def _activate(self, state: GroupState) -> None:
        """Mark a group slot as active synchronously before launching a future."""
        state.active = True
        self._active_count += 1

    def register_process(self, group_jid: str, proc: object, container_name: str, group_folder: str | None = None) -> None:
        state = self._get_group(group_jid)
        state.process = proc
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder

    def notify_idle(self, group_jid: str) -> None:
        state = self._get_group(group_jid)
        state.idle_waiting = True
        if state.pending_tasks:
            self.close_stdin(group_jid)

    def send_message(self, group_jid: str, text: str) -> bool:
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder or state.is_task_container:
            return False
        state.idle_waiting = False

        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{int(time.time() * 1000)}-{os.urandom(3).hex()}.json"
            filepath = input_dir / filename
            temp_path = filepath.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps({"type": "message", "text": text}))
            temp_path.rename(filepath)
            return True
        except Exception:
            return False

    def close_stdin(self, group_jid: str) -> None:
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder:
            return

        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / "_close").write_text("")
        except Exception:
            pass

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        state = self._get_group(group_jid)
        state.idle_waiting = False
        state.is_task_container = False
        state.pending_messages = False

        logger.debug("Starting container for group", group_jid=group_jid, reason=reason, active_count=self._active_count)

        try:
            if self._process_messages_fn:
                success = await self._process_messages_fn(group_jid)
                if success:
                    state.retry_count = 0
                else:
                    self._schedule_retry(group_jid, state)
        except Exception as err:
            logger.error("Error processing messages for group", group_jid=group_jid, error=str(err))
            self._schedule_retry(group_jid, state)
        finally:
            state.active = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: QueuedTask) -> None:
        state = self._get_group(group_jid)
        state.idle_waiting = False
        state.is_task_container = True

        logger.debug("Running queued task", group_jid=group_jid, task_id=task.id, active_count=self._active_count)

        try:
            await task.fn()
        except Exception as err:
            logger.error("Error running task", group_jid=group_jid, task_id=task.id, error=str(err))
        finally:
            state.active = False
            state.is_task_container = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    def _schedule_retry(self, group_jid: str, state: GroupState) -> None:
        state.retry_count += 1
        if state.retry_count > MAX_RETRIES:
            logger.error("Max retries exceeded, dropping messages", group_jid=group_jid, retry_count=state.retry_count)
            state.retry_count = 0
            return

        delay_ms = BASE_RETRY_MS * (2 ** (state.retry_count - 1))
        logger.info("Scheduling retry with backoff", group_jid=group_jid, retry_count=state.retry_count, delay_ms=delay_ms)

        loop = asyncio.get_event_loop()
        loop.call_later(delay_ms / 1000, lambda: self.enqueue_message_check(group_jid) if not self._shutting_down else None)

    def _drain_group(self, group_jid: str) -> None:
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            self._activate(state)
            asyncio.ensure_future(self._run_task(group_jid, task))
            return

        if state.pending_messages:
            self._activate(state)
            asyncio.ensure_future(self._run_for_group(group_jid, "drain"))
            return

        self._drain_waiting()

    def _drain_waiting(self) -> None:
        while self._waiting_groups and self._active_count < MAX_CONCURRENT_CONTAINERS:
            next_jid = self._waiting_groups.pop(0)
            state = self._get_group(next_jid)

            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                self._activate(state)
                asyncio.ensure_future(self._run_task(next_jid, task))
            elif state.pending_messages:
                self._activate(state)
                asyncio.ensure_future(self._run_for_group(next_jid, "drain"))

    async def shutdown(self, grace_period_ms: int) -> None:
        self._shutting_down = True
        active_containers = [
            state.container_name
            for state in self._groups.values()
            if state.process and state.container_name
        ]
        logger.info(
            "GroupQueue shutting down (containers detached, not killed)",
            active_count=self._active_count,
            detached_containers=active_containers,
        )
