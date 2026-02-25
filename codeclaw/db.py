from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from codeclaw.config import ASSISTANT_NAME, DATA_DIR, STORE_DIR
from codeclaw.group_folder import is_valid_group_folder
from codeclaw.logger import logger
from codeclaw.models import NewMessage, RegisteredGroup, ScheduledTask, TaskRunLog

_db: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db


def _create_schema(database: sqlite3.Connection) -> None:
    database.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TEXT,
            channel TEXT,
            is_group INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT,
            chat_jid TEXT,
            sender TEXT,
            sender_name TEXT,
            content TEXT,
            timestamp TEXT,
            is_from_me INTEGER,
            is_bot_message INTEGER DEFAULT 0,
            PRIMARY KEY (id, chat_jid),
            FOREIGN KEY (chat_jid) REFERENCES chats(jid)
        );
        CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            group_folder TEXT NOT NULL,
            chat_jid TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            schedule_value TEXT NOT NULL,
            next_run TEXT,
            last_run TEXT,
            last_result TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
        CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);

        CREATE TABLE IF NOT EXISTS task_run_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            error TEXT,
            FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

        CREATE TABLE IF NOT EXISTS router_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            group_folder TEXT PRIMARY KEY,
            session_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS registered_groups (
            jid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder TEXT NOT NULL UNIQUE,
            trigger_pattern TEXT NOT NULL,
            added_at TEXT NOT NULL,
            container_config TEXT,
            requires_trigger INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS processed_events (
            delivery_id TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL
        );
    """)

    # Add context_mode column if it doesn't exist (migration for existing DBs)
    try:
        database.execute("ALTER TABLE scheduled_tasks ADD COLUMN context_mode TEXT DEFAULT 'isolated'")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Add is_bot_message column if it doesn't exist
    try:
        database.execute("ALTER TABLE messages ADD COLUMN is_bot_message INTEGER DEFAULT 0")
        database.execute(
            "UPDATE messages SET is_bot_message = 1 WHERE content LIKE ?",
            (f"{ASSISTANT_NAME}:%",),
        )
    except sqlite3.OperationalError:
        pass

    # Add channel and is_group columns if they don't exist
    try:
        database.execute("ALTER TABLE chats ADD COLUMN channel TEXT")
        database.execute("ALTER TABLE chats ADD COLUMN is_group INTEGER DEFAULT 0")
        database.execute("UPDATE chats SET channel = 'github', is_group = 1 WHERE jid LIKE 'gh:%'")
    except sqlite3.OperationalError:
        pass

    database.commit()


def init_database() -> None:
    global _db
    db_path = STORE_DIR / "messages.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(str(db_path))
    _db.row_factory = sqlite3.Row
    _create_schema(_db)
    _migrate_json_state()


def init_test_database() -> None:
    """For tests only. Creates a fresh in-memory database."""
    global _db
    _db = sqlite3.connect(":memory:")
    _db.row_factory = sqlite3.Row
    _create_schema(_db)


# --- Chat metadata ---


def store_chat_metadata(
    chat_jid: str,
    timestamp: str,
    name: str | None = None,
    channel: str | None = None,
    is_group: bool | None = None,
) -> None:
    db = _get_db()
    group_val = None if is_group is None else (1 if is_group else 0)

    if name:
        db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time, channel, is_group) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                name = excluded.name,
                last_message_time = MAX(last_message_time, excluded.last_message_time),
                channel = COALESCE(excluded.channel, channel),
                is_group = COALESCE(excluded.is_group, is_group)
            """,
            (chat_jid, name, timestamp, channel, group_val),
        )
    else:
        db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time, channel, is_group) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                last_message_time = MAX(last_message_time, excluded.last_message_time),
                channel = COALESCE(excluded.channel, channel),
                is_group = COALESCE(excluded.is_group, is_group)
            """,
            (chat_jid, chat_jid, timestamp, channel, group_val),
        )
    db.commit()


def get_all_chats() -> list[dict]:
    db = _get_db()
    rows = db.execute(
        "SELECT jid, name, last_message_time, channel, is_group FROM chats ORDER BY last_message_time DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# --- Messages ---


def store_message(msg: NewMessage) -> None:
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg.id,
            msg.chat_jid,
            msg.sender,
            msg.sender_name,
            msg.content,
            msg.timestamp,
            1 if msg.is_from_me else 0,
            1 if msg.is_bot_message else 0,
        ),
    )
    db.commit()


def get_messages_since(chat_jid: str, since_timestamp: str, bot_prefix: str) -> list[NewMessage]:
    db = _get_db()
    rows = db.execute(
        """
        SELECT id, chat_jid, sender, sender_name, content, timestamp
        FROM messages
        WHERE chat_jid = ? AND timestamp > ?
            AND is_bot_message = 0 AND content NOT LIKE ?
            AND content != '' AND content IS NOT NULL
        ORDER BY timestamp
        """,
        (chat_jid, since_timestamp, f"{bot_prefix}:%"),
    ).fetchall()
    return [
        NewMessage(
            id=r["id"],
            chat_jid=r["chat_jid"],
            sender=r["sender"],
            sender_name=r["sender_name"],
            content=r["content"],
            timestamp=r["timestamp"],
        )
        for r in rows
    ]


# --- Scheduled tasks ---


def create_task(task: ScheduledTask) -> None:
    db = _get_db()
    db.execute(
        """
        INSERT INTO scheduled_tasks (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, context_mode, next_run, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode or "isolated",
            task.next_run,
            task.status,
            task.created_at,
        ),
    )
    db.commit()


def get_task_by_id(task_id: str) -> ScheduledTask | None:
    db = _get_db()
    row = db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    return ScheduledTask(**dict(row))


def get_all_tasks() -> list[ScheduledTask]:
    db = _get_db()
    rows = db.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC").fetchall()
    return [ScheduledTask(**dict(r)) for r in rows]


def update_task(task_id: str, **updates: str | None) -> None:
    db = _get_db()
    allowed = {"prompt", "schedule_type", "schedule_value", "next_run", "status"}
    fields = []
    values: list = []
    for key, val in updates.items():
        if key in allowed:
            fields.append(f"{key} = ?")
            values.append(val)

    if not fields:
        return

    values.append(task_id)
    db.execute(f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?", values)
    db.commit()


def delete_task(task_id: str) -> None:
    db = _get_db()
    db.execute("DELETE FROM task_run_logs WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    db.commit()


def get_due_tasks() -> list[ScheduledTask]:
    db = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = db.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= ?
        ORDER BY next_run
        """,
        (now,),
    ).fetchall()
    return [ScheduledTask(**dict(r)) for r in rows]


def update_task_after_run(task_id: str, next_run: str | None, last_result: str) -> None:
    db = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        UPDATE scheduled_tasks
        SET next_run = ?, last_run = ?, last_result = ?, status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
        WHERE id = ?
        """,
        (next_run, now, last_result, next_run, task_id),
    )
    db.commit()


def log_task_run(log: TaskRunLog) -> None:
    db = _get_db()
    db.execute(
        """
        INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (log.task_id, log.run_at, log.duration_ms, log.status, log.result, log.error),
    )
    db.commit()


# --- Router state ---


def get_router_state(key: str) -> str | None:
    db = _get_db()
    row = db.execute("SELECT value FROM router_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_router_state(key: str, value: str) -> None:
    db = _get_db()
    db.execute("INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)", (key, value))
    db.commit()


# --- Sessions ---


def get_session(group_folder: str) -> str | None:
    db = _get_db()
    row = db.execute(
        "SELECT session_id FROM sessions WHERE group_folder = ?", (group_folder,)
    ).fetchone()
    return row["session_id"] if row else None


def set_session(group_folder: str, session_id: str) -> None:
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)",
        (group_folder, session_id),
    )
    db.commit()


def get_all_sessions() -> dict[str, str]:
    db = _get_db()
    rows = db.execute("SELECT group_folder, session_id FROM sessions").fetchall()
    return {r["group_folder"]: r["session_id"] for r in rows}


# --- Registered groups ---


def get_registered_group(jid: str) -> RegisteredGroup | None:
    db = _get_db()
    row = db.execute("SELECT * FROM registered_groups WHERE jid = ?", (jid,)).fetchone()
    if not row:
        return None
    row_dict = dict(row)
    if not is_valid_group_folder(row_dict["folder"]):
        logger.warning(
            "Skipping registered group with invalid folder",
            jid=row_dict["jid"],
            folder=row_dict["folder"],
        )
        return None
    container_config = json.loads(row_dict["container_config"]) if row_dict.get("container_config") else None
    requires_trigger = None if row_dict.get("requires_trigger") is None else bool(row_dict["requires_trigger"])
    return RegisteredGroup(
        name=row_dict["name"],
        folder=row_dict["folder"],
        trigger=row_dict["trigger_pattern"],
        added_at=row_dict["added_at"],
        container_config=container_config,
        requires_trigger=requires_trigger,
    )


def set_registered_group(jid: str, group: RegisteredGroup) -> None:
    if not is_valid_group_folder(group.folder):
        raise ValueError(f'Invalid group folder "{group.folder}" for JID {jid}')
    db = _get_db()
    container_config_json = json.dumps(group.container_config.model_dump()) if group.container_config else None
    requires_trigger_val = 1 if group.requires_trigger is None else (1 if group.requires_trigger else 0)
    db.execute(
        """INSERT OR REPLACE INTO registered_groups (jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger)
         VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            jid,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            container_config_json,
            requires_trigger_val,
        ),
    )
    db.commit()


def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    db = _get_db()
    rows = db.execute("SELECT * FROM registered_groups").fetchall()
    result: dict[str, RegisteredGroup] = {}
    for row in rows:
        row_dict = dict(row)
        if not is_valid_group_folder(row_dict["folder"]):
            logger.warning(
                "Skipping registered group with invalid folder",
                jid=row_dict["jid"],
                folder=row_dict["folder"],
            )
            continue
        container_config = json.loads(row_dict["container_config"]) if row_dict.get("container_config") else None
        requires_trigger = None if row_dict.get("requires_trigger") is None else bool(row_dict["requires_trigger"])
        result[row_dict["jid"]] = RegisteredGroup(
            name=row_dict["name"],
            folder=row_dict["folder"],
            trigger=row_dict["trigger_pattern"],
            added_at=row_dict["added_at"],
            container_config=container_config,
            requires_trigger=requires_trigger,
        )
    return result


# --- Processed events (webhook idempotency) ---


def is_event_processed(delivery_id: str) -> bool:
    db = _get_db()
    row = db.execute("SELECT 1 FROM processed_events WHERE delivery_id = ?", (delivery_id,)).fetchone()
    return row is not None


def mark_event_processed(delivery_id: str) -> None:
    db = _get_db()
    db.execute(
        "INSERT OR IGNORE INTO processed_events (delivery_id, processed_at) VALUES (?, ?)",
        (delivery_id, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def cleanup_processed_events(max_age_ms: int = 86_400_000) -> None:
    db = _get_db()
    cutoff = datetime.fromtimestamp(
        (datetime.now(timezone.utc).timestamp() * 1000 - max_age_ms) / 1000, tz=timezone.utc
    ).isoformat()
    db.execute("DELETE FROM processed_events WHERE processed_at < ?", (cutoff,))
    db.commit()


# --- JSON migration ---


def _migrate_json_state() -> None:
    def migrate_file(filename: str):
        file_path = DATA_DIR / filename
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text())
            file_path.rename(f"{file_path}.migrated")
            return data
        except Exception:
            return None

    # Migrate router_state.json
    router_state = migrate_file("router_state.json")
    if router_state:
        if router_state.get("last_timestamp"):
            set_router_state("last_timestamp", router_state["last_timestamp"])
        if router_state.get("last_agent_timestamp"):
            set_router_state("last_agent_timestamp", json.dumps(router_state["last_agent_timestamp"]))

    # Migrate sessions.json
    sessions = migrate_file("sessions.json")
    if sessions:
        for folder, session_id in sessions.items():
            set_session(folder, session_id)

    # Migrate registered_groups.json
    groups = migrate_file("registered_groups.json")
    if groups:
        for jid, group_data in groups.items():
            try:
                group = RegisteredGroup(**group_data)
                set_registered_group(jid, group)
            except Exception as err:
                logger.warning(
                    "Skipping migrated registered group with invalid folder",
                    jid=jid,
                    error=str(err),
                )
