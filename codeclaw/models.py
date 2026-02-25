from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class AdditionalMount(BaseModel):
    host_path: str  # Absolute path on host (supports ~ for home)
    container_path: str | None = None  # Defaults to basename of host_path, mounted at /workspace/extra/{value}
    readonly: bool = True


class AllowedRoot(BaseModel):
    path: str  # Absolute path or ~ for home
    allow_read_write: bool = False
    description: str | None = None


class MountAllowlist(BaseModel):
    allowed_roots: list[AllowedRoot] = []
    blocked_patterns: list[str] = []
    non_main_read_only: bool = True


class ContainerConfig(BaseModel):
    additional_mounts: list[AdditionalMount] | None = None
    timeout: int | None = None  # Default: 300000 (5 minutes)


class RegisteredGroup(BaseModel):
    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool | None = None  # Default: True for groups, False for solo chats


class GitHubEventMetadata(BaseModel):
    issue_number: int | None = None
    pr_number: int | None = None
    comment_id: int | None = None
    review_id: int | None = None
    is_review_comment: bool | None = None
    sha: str | None = None
    path: str | None = None
    line: int | None = None


class NewMessage(BaseModel):
    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False
    is_bot_message: bool = False
    github_metadata: GitHubEventMetadata | None = None


class ScheduledTask(BaseModel):
    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: str  # 'cron' | 'interval' | 'once'
    schedule_value: str
    context_mode: str = "isolated"  # 'group' | 'isolated'
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: str = "active"  # 'active' | 'paused' | 'completed'
    created_at: str = ""


class TaskRunLog(BaseModel):
    task_id: str
    run_at: str
    duration_ms: int
    status: str  # 'success' | 'error'
    result: str | None = None
    error: str | None = None


class Channel(Protocol):
    """Channel abstraction for posting messages to GitHub (or other platforms)."""

    name: str

    async def connect(self) -> None: ...
    async def send_message(self, jid: str, text: str) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
    async def disconnect(self) -> None: ...
