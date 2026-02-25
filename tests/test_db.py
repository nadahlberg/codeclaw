"""Tests for database CRUD operations."""

from __future__ import annotations

from clawcode.db import (
    create_task,
    delete_task,
    get_all_chats,
    get_messages_since,
    get_task_by_id,
    store_chat_metadata,
    store_message,
    update_task,
)
from clawcode.models import NewMessage, ScheduledTask


# ---------------------------------------------------------------------------
# store_message
# ---------------------------------------------------------------------------


class TestStoreMessage:
    def test_stores_and_retrieves(self):
        store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        store_message(NewMessage(
            id="msg-1", chat_jid="group@g.us", sender="123@s.whatsapp.net",
            sender_name="Alice", content="hello world", timestamp="2024-01-01T00:00:01.000Z",
        ))
        messages = get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z", "Andy")
        assert len(messages) == 1
        assert messages[0].id == "msg-1"
        assert messages[0].sender == "123@s.whatsapp.net"
        assert messages[0].sender_name == "Alice"
        assert messages[0].content == "hello world"

    def test_filters_empty_content(self):
        store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        store_message(NewMessage(
            id="msg-2", chat_jid="group@g.us", sender="111@s.whatsapp.net",
            sender_name="Dave", content="", timestamp="2024-01-01T00:00:04.000Z",
        ))
        messages = get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z", "Andy")
        assert len(messages) == 0

    def test_upserts_on_duplicate(self):
        store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        store_message(NewMessage(
            id="msg-dup", chat_jid="group@g.us", sender="123@s.whatsapp.net",
            sender_name="Alice", content="original", timestamp="2024-01-01T00:00:01.000Z",
        ))
        store_message(NewMessage(
            id="msg-dup", chat_jid="group@g.us", sender="123@s.whatsapp.net",
            sender_name="Alice", content="updated", timestamp="2024-01-01T00:00:01.000Z",
        ))
        messages = get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z", "Andy")
        assert len(messages) == 1
        assert messages[0].content == "updated"


# ---------------------------------------------------------------------------
# get_messages_since
# ---------------------------------------------------------------------------


class TestGetMessagesSince:
    def _setup_messages(self):
        store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        store_message(NewMessage(
            id="m1", chat_jid="group@g.us", sender="Alice@s.whatsapp.net",
            sender_name="Alice", content="first", timestamp="2024-01-01T00:00:01.000Z",
        ))
        store_message(NewMessage(
            id="m2", chat_jid="group@g.us", sender="Bob@s.whatsapp.net",
            sender_name="Bob", content="second", timestamp="2024-01-01T00:00:02.000Z",
        ))
        store_message(NewMessage(
            id="m3", chat_jid="group@g.us", sender="Bot@s.whatsapp.net",
            sender_name="Bot", content="bot reply", timestamp="2024-01-01T00:00:03.000Z",
            is_bot_message=True,
        ))
        store_message(NewMessage(
            id="m4", chat_jid="group@g.us", sender="Carol@s.whatsapp.net",
            sender_name="Carol", content="third", timestamp="2024-01-01T00:00:04.000Z",
        ))

    def test_returns_messages_after_timestamp(self):
        self._setup_messages()
        msgs = get_messages_since("group@g.us", "2024-01-01T00:00:02.000Z", "Andy")
        assert len(msgs) == 1
        assert msgs[0].content == "third"

    def test_excludes_bot_messages(self):
        self._setup_messages()
        msgs = get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z", "Andy")
        assert not any(m.content == "bot reply" for m in msgs)

    def test_returns_all_non_bot_when_empty_timestamp(self):
        self._setup_messages()
        msgs = get_messages_since("group@g.us", "", "Andy")
        assert len(msgs) == 3

    def test_filters_pre_migration_bot_via_content_prefix(self):
        self._setup_messages()
        store_message(NewMessage(
            id="m5", chat_jid="group@g.us", sender="Bot@s.whatsapp.net",
            sender_name="Bot", content="Andy: old bot reply",
            timestamp="2024-01-01T00:00:05.000Z",
        ))
        msgs = get_messages_since("group@g.us", "2024-01-01T00:00:04.000Z", "Andy")
        assert len(msgs) == 0


# ---------------------------------------------------------------------------
# store_chat_metadata
# ---------------------------------------------------------------------------


class TestStoreChatMetadata:
    def test_stores_with_default_name(self):
        store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        chats = get_all_chats()
        assert len(chats) == 1
        assert chats[0]["jid"] == "group@g.us"
        assert chats[0]["name"] == "group@g.us"

    def test_stores_with_explicit_name(self):
        store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z", name="My Group")
        chats = get_all_chats()
        assert chats[0]["name"] == "My Group"

    def test_updates_name_on_subsequent_call(self):
        store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        store_chat_metadata("group@g.us", "2024-01-01T00:00:01.000Z", name="Updated Name")
        chats = get_all_chats()
        assert len(chats) == 1
        assert chats[0]["name"] == "Updated Name"

    def test_preserves_newer_timestamp(self):
        store_chat_metadata("group@g.us", "2024-01-01T00:00:05.000Z")
        store_chat_metadata("group@g.us", "2024-01-01T00:00:01.000Z")
        chats = get_all_chats()
        assert chats[0]["last_message_time"] == "2024-01-01T00:00:05.000Z"


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


class TestTaskCrud:
    def test_creates_and_retrieves(self):
        create_task(ScheduledTask(
            id="task-1", group_folder="main", chat_jid="group@g.us",
            prompt="do something", schedule_type="once",
            schedule_value="2024-06-01T00:00:00.000Z", context_mode="isolated",
            next_run="2024-06-01T00:00:00.000Z", status="active",
            created_at="2024-01-01T00:00:00.000Z",
        ))
        task = get_task_by_id("task-1")
        assert task is not None
        assert task.prompt == "do something"
        assert task.status == "active"

    def test_updates_status(self):
        create_task(ScheduledTask(
            id="task-2", group_folder="main", chat_jid="group@g.us",
            prompt="test", schedule_type="once",
            schedule_value="2024-06-01T00:00:00.000Z", context_mode="isolated",
            status="active", created_at="2024-01-01T00:00:00.000Z",
        ))
        update_task("task-2", status="paused")
        assert get_task_by_id("task-2").status == "paused"

    def test_deletes_task(self):
        create_task(ScheduledTask(
            id="task-3", group_folder="main", chat_jid="group@g.us",
            prompt="delete me", schedule_type="once",
            schedule_value="2024-06-01T00:00:00.000Z", context_mode="isolated",
            status="active", created_at="2024-01-01T00:00:00.000Z",
        ))
        delete_task("task-3")
        assert get_task_by_id("task-3") is None
