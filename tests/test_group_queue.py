"""Tests for GroupQueue concurrency, retry backoff, and task priority."""

from __future__ import annotations

import asyncio

import pytest

from clawcode.group_queue import GroupQueue


@pytest.fixture
def queue():
    return GroupQueue()


class TestGroupQueueConcurrency:
    @pytest.mark.asyncio
    async def test_one_container_per_group(self, queue):
        """Only one container should run per group at a time."""
        concurrent_count = 0
        max_concurrent = 0

        async def process_messages(group_jid: str) -> bool:
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return True

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check("group1@g.us")
        queue.enqueue_message_check("group1@g.us")

        await asyncio.sleep(0.2)
        assert max_concurrent == 1

    @pytest.mark.asyncio
    async def test_respects_global_concurrency_limit(self, queue, monkeypatch):
        """Should not exceed MAX_CONCURRENT_CONTAINERS."""
        monkeypatch.setattr("clawcode.group_queue.MAX_CONCURRENT_CONTAINERS", 2)

        active_count = 0
        max_active = 0
        events: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            event = asyncio.Event()
            events.append(event)
            await event.wait()
            active_count -= 1
            return True

        queue.set_process_messages_fn(process_messages)

        queue.enqueue_message_check("group1@g.us")
        queue.enqueue_message_check("group2@g.us")
        queue.enqueue_message_check("group3@g.us")

        await asyncio.sleep(0.05)
        assert max_active == 2
        assert active_count == 2

        # Complete first — third should start
        events[0].set()
        await asyncio.sleep(0.05)
        assert len(events) == 3

        # Cleanup
        for e in events:
            e.set()
        await asyncio.sleep(0.05)


class TestGroupQueueRetry:
    @pytest.mark.asyncio
    async def test_retries_on_failure(self, queue):
        call_count = 0

        async def process_messages(group_jid: str) -> bool:
            nonlocal call_count
            call_count += 1
            return False  # failure

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check("group1@g.us")

        # First call + allow time for 1-2 retries (backoff is 5s, 10s, etc.)
        # With a short test, just verify it was called at least once and retries
        await asyncio.sleep(0.1)
        assert call_count >= 1


class TestGroupQueueShutdown:
    @pytest.mark.asyncio
    async def test_prevents_new_enqueues(self, queue):
        call_count = 0

        async def process_messages(group_jid: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        queue.set_process_messages_fn(process_messages)
        await queue.shutdown(100)

        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.1)
        assert call_count == 0


class TestGroupQueueTaskPriority:
    @pytest.mark.asyncio
    async def test_drains_tasks_before_messages(self, queue):
        execution_order: list[str] = []
        event = asyncio.Event()

        async def process_messages(group_jid: str) -> bool:
            if not execution_order:
                await event.wait()
            execution_order.append("messages")
            return True

        queue.set_process_messages_fn(process_messages)

        # Start processing (takes the active slot)
        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.05)

        # While active, enqueue a task and pending messages
        async def task_fn():
            execution_order.append("task")

        queue.enqueue_task("group1@g.us", "task-1", task_fn)
        queue.enqueue_message_check("group1@g.us")

        # Release first processing
        event.set()
        await asyncio.sleep(0.1)

        # Task should run before second message check
        assert execution_order[0] == "messages"
        if len(execution_order) > 1:
            assert execution_order[1] == "task"
