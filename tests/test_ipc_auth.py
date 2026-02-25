"""Tests for IPC task authorization."""

from __future__ import annotations

import pytest

from codeclaw.db import (
    create_task,
    get_all_tasks,
    get_registered_group,
    get_task_by_id,
    set_registered_group,
)
from codeclaw.ipc import IpcDeps, process_task_ipc
from codeclaw.models import RegisteredGroup, ScheduledTask

MAIN_GROUP = RegisteredGroup(
    name="Main", folder="main", trigger="always", added_at="2024-01-01T00:00:00.000Z"
)
OTHER_GROUP = RegisteredGroup(
    name="Other", folder="other-group", trigger="@Andy", added_at="2024-01-01T00:00:00.000Z"
)
THIRD_GROUP = RegisteredGroup(
    name="Third", folder="third-group", trigger="@Andy", added_at="2024-01-01T00:00:00.000Z"
)


async def _noop_async(*args, **kwargs):
    pass


@pytest.fixture()
def setup_groups():
    groups = {
        "main@g.us": MAIN_GROUP,
        "other@g.us": OTHER_GROUP,
        "third@g.us": THIRD_GROUP,
    }
    for jid, g in groups.items():
        set_registered_group(jid, g)

    deps = IpcDeps(
        send_message=_noop_async,
        send_structured_message=None,
        registered_groups=lambda: groups,
        register_group=lambda jid, group: (
            groups.__setitem__(jid, group),
            set_registered_group(jid, group),
        ),
        sync_group_metadata=_noop_async,
        get_available_groups=lambda: [],
        write_groups_snapshot=lambda *a: None,
    )
    return groups, deps


# ---------------------------------------------------------------------------
# schedule_task authorization
# ---------------------------------------------------------------------------


class TestScheduleTaskAuth:
    @pytest.mark.asyncio
    async def test_main_can_schedule_for_other(self, setup_groups):
        _, deps = setup_groups
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "do something",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )
        tasks = get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "other-group"

    @pytest.mark.asyncio
    async def test_non_main_can_schedule_for_self(self, setup_groups):
        _, deps = setup_groups
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "self task",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "other-group",
            False,
            deps,
        )
        tasks = get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "other-group"

    @pytest.mark.asyncio
    async def test_non_main_cannot_schedule_for_other(self, setup_groups):
        _, deps = setup_groups
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "unauthorized",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "main@g.us",
            },
            "other-group",
            False,
            deps,
        )
        assert len(get_all_tasks()) == 0

    @pytest.mark.asyncio
    async def test_rejects_unregistered_target(self, setup_groups):
        _, deps = setup_groups
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "no target",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "unknown@g.us",
            },
            "main",
            True,
            deps,
        )
        assert len(get_all_tasks()) == 0


# ---------------------------------------------------------------------------
# pause_task authorization
# ---------------------------------------------------------------------------


class TestPauseTaskAuth:
    @pytest.fixture(autouse=True)
    def _create_tasks(self):
        create_task(ScheduledTask(
            id="task-main", group_folder="main", chat_jid="main@g.us",
            prompt="main task", schedule_type="once",
            schedule_value="2025-06-01T00:00:00.000Z", context_mode="isolated",
            next_run="2025-06-01T00:00:00.000Z", status="active",
            created_at="2024-01-01T00:00:00.000Z",
        ))
        create_task(ScheduledTask(
            id="task-other", group_folder="other-group", chat_jid="other@g.us",
            prompt="other task", schedule_type="once",
            schedule_value="2025-06-01T00:00:00.000Z", context_mode="isolated",
            next_run="2025-06-01T00:00:00.000Z", status="active",
            created_at="2024-01-01T00:00:00.000Z",
        ))

    @pytest.mark.asyncio
    async def test_main_can_pause_any_task(self, setup_groups):
        _, deps = setup_groups
        await process_task_ipc({"type": "pause_task", "taskId": "task-other"}, "main", True, deps)
        assert get_task_by_id("task-other").status == "paused"

    @pytest.mark.asyncio
    async def test_non_main_can_pause_own_task(self, setup_groups):
        _, deps = setup_groups
        await process_task_ipc({"type": "pause_task", "taskId": "task-other"}, "other-group", False, deps)
        assert get_task_by_id("task-other").status == "paused"

    @pytest.mark.asyncio
    async def test_non_main_cannot_pause_other_group(self, setup_groups):
        _, deps = setup_groups
        await process_task_ipc({"type": "pause_task", "taskId": "task-main"}, "other-group", False, deps)
        assert get_task_by_id("task-main").status == "active"


# ---------------------------------------------------------------------------
# cancel_task authorization
# ---------------------------------------------------------------------------


class TestCancelTaskAuth:
    @pytest.mark.asyncio
    async def test_main_can_cancel_any_task(self, setup_groups):
        _, deps = setup_groups
        create_task(ScheduledTask(
            id="task-to-cancel", group_folder="other-group", chat_jid="other@g.us",
            prompt="cancel me", schedule_type="once",
            schedule_value="2025-06-01T00:00:00.000Z", context_mode="isolated",
            status="active", created_at="2024-01-01T00:00:00.000Z",
        ))
        await process_task_ipc({"type": "cancel_task", "taskId": "task-to-cancel"}, "main", True, deps)
        assert get_task_by_id("task-to-cancel") is None

    @pytest.mark.asyncio
    async def test_non_main_cannot_cancel_other_group(self, setup_groups):
        _, deps = setup_groups
        create_task(ScheduledTask(
            id="task-foreign", group_folder="main", chat_jid="main@g.us",
            prompt="not yours", schedule_type="once",
            schedule_value="2025-06-01T00:00:00.000Z", context_mode="isolated",
            status="active", created_at="2024-01-01T00:00:00.000Z",
        ))
        await process_task_ipc({"type": "cancel_task", "taskId": "task-foreign"}, "other-group", False, deps)
        assert get_task_by_id("task-foreign") is not None


# ---------------------------------------------------------------------------
# register_group authorization
# ---------------------------------------------------------------------------


class TestRegisterGroupAuth:
    @pytest.mark.asyncio
    async def test_non_main_cannot_register(self, setup_groups):
        groups, deps = setup_groups
        await process_task_ipc(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@Andy",
            },
            "other-group",
            False,
            deps,
        )
        assert "new@g.us" not in groups

    @pytest.mark.asyncio
    async def test_main_cannot_register_unsafe_folder(self, setup_groups):
        groups, deps = setup_groups
        await process_task_ipc(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "../../outside",
                "trigger": "@Andy",
            },
            "main",
            True,
            deps,
        )
        assert "new@g.us" not in groups

    @pytest.mark.asyncio
    async def test_main_can_register_new_group(self, setup_groups):
        _, deps = setup_groups
        await process_task_ipc(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@Andy",
            },
            "main",
            True,
            deps,
        )
        group = get_registered_group("new@g.us")
        assert group is not None
        assert group.name == "New Group"


# ---------------------------------------------------------------------------
# IPC message authorization (logic from ipc watcher)
# ---------------------------------------------------------------------------


class TestIpcMessageAuth:
    @staticmethod
    def _is_authorized(source_group, is_main, target_jid, registered_groups):
        target_group = registered_groups.get(target_jid)
        return is_main or (target_group is not None and target_group.folder == source_group)

    def test_main_can_send_to_any(self, setup_groups):
        groups, _ = setup_groups
        assert self._is_authorized("main", True, "other@g.us", groups)
        assert self._is_authorized("main", True, "third@g.us", groups)

    def test_non_main_can_send_to_own(self, setup_groups):
        groups, _ = setup_groups
        assert self._is_authorized("other-group", False, "other@g.us", groups)

    def test_non_main_cannot_send_to_other(self, setup_groups):
        groups, _ = setup_groups
        assert not self._is_authorized("other-group", False, "main@g.us", groups)

    def test_non_main_cannot_send_to_unregistered(self, setup_groups):
        groups, _ = setup_groups
        assert not self._is_authorized("other-group", False, "unknown@g.us", groups)

    def test_main_can_send_to_unregistered(self, setup_groups):
        groups, _ = setup_groups
        assert self._is_authorized("main", True, "unknown@g.us", groups)
