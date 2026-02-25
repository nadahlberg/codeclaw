"""CodeClaw Main Orchestrator.

Webhook handling, repo checkout, agent invocation, and message routing.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from codeclaw.channels.github import GitHubChannel, GitHubResponseTarget
from codeclaw.config import (
    ASSISTANT_NAME,
    DATA_DIR,
    IDLE_TIMEOUT,
    MAIN_GROUP_FOLDER,
    PORT,
    RECONCILIATION_INTERVAL,
)
from codeclaw.container_runner import (
    ContainerInput,
    ContainerOutput,
    add_github_token,
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from codeclaw.container_runtime import cleanup_orphans, ensure_container_runtime_running
from codeclaw.db import (
    cleanup_processed_events,
    get_all_chats,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_messages_since,
    get_router_state,
    init_database,
    is_event_processed,
    mark_event_processed,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
)
from codeclaw.github.access_control import DEFAULT_ACCESS_POLICY, RateLimiter, check_permission
from codeclaw.github.auth import GitHubTokenManager, load_github_app_config
from codeclaw.github.event_mapper import (
    GitHubEvent,
    map_webhook_to_event,
    parse_repo_from_jid,
    repo_jid_from_thread_jid,
)
from codeclaw.group_folder import resolve_group_folder_path
from codeclaw.group_queue import GroupQueue
from codeclaw.ipc import IpcDeps, start_ipc_watcher
from codeclaw.logger import logger
from codeclaw.models import NewMessage, RegisteredGroup
from codeclaw.router import find_channel, format_messages, format_outbound
from codeclaw.task_scheduler import SchedulerDependencies, start_scheduler_loop
from codeclaw.webhook_server import create_app

# Module-level state
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_token_manager: GitHubTokenManager | None = None
_channels: list = []
_queue = GroupQueue()
_rate_limiter = RateLimiter()


def _load_state() -> None:
    global _sessions, _registered_groups, _last_agent_timestamp
    agent_ts = get_router_state("last_agent_timestamp")
    try:
        _last_agent_timestamp = json.loads(agent_ts) if agent_ts else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted last_agent_timestamp in DB, resetting")
        _last_agent_timestamp = {}
    _sessions = get_all_sessions()
    _registered_groups = get_all_registered_groups()
    logger.info("State loaded", group_count=len(_registered_groups))


def _save_state() -> None:
    set_router_state("last_agent_timestamp", json.dumps(_last_agent_timestamp))


def _register_group(jid: str, group: RegisteredGroup) -> None:
    try:
        group_dir = resolve_group_folder_path(group.folder)
    except Exception as err:
        logger.warning("Rejecting group registration with invalid folder", jid=jid, folder=group.folder, error=str(err))
        return

    _registered_groups[jid] = group
    set_registered_group(jid, group)
    Path(group_dir, "logs").mkdir(parents=True, exist_ok=True)
    logger.info("Group registered", jid=jid, name=group.name, folder=group.folder)


def _get_available_groups() -> list[dict]:
    chats = get_all_chats()
    registered_jids = set(_registered_groups.keys())
    return [
        {
            "jid": c["jid"],
            "name": c["name"],
            "lastActivity": c["last_message_time"],
            "isRegistered": c["jid"] in registered_jids,
        }
        for c in chats
        if c["jid"] != "__group_sync__" and c.get("is_group")
    ]


# --- GitHub webhook event handling ---


async def _handle_webhook_event(event_name: str, delivery_id: str, payload: dict) -> None:
    if is_event_processed(delivery_id):
        logger.debug("Duplicate event, skipping", delivery_id=delivery_id)
        return
    mark_event_processed(delivery_id)

    if event_name == "installation_repositories":
        await _handle_installation_event(payload)
        return

    if not _token_manager:
        return

    app_slug = await _token_manager.get_app_slug()
    event = map_webhook_to_event(event_name, payload, app_slug)
    if not event:
        logger.debug("Event not handled", event_name=event_name, delivery_id=delivery_id)
        return

    group = _registered_groups.get(event.repo_jid)
    if not group:
        logger.debug("Event for unregistered repo, skipping", repo_jid=event.repo_jid)
        return

    # Access control
    try:
        owner, repo = parse_repo_from_jid(event.repo_jid)
        headers = await _token_manager.get_headers_for_repo(owner, repo)
        allowed, reason = await check_permission(headers, owner, repo, event.sender, DEFAULT_ACCESS_POLICY)
        if not allowed:
            logger.info("Event rejected: insufficient permissions", sender=event.sender, repo_jid=event.repo_jid, reason=reason)
            return

        rate_allowed, retry_after = _rate_limiter.check(event.sender, event.repo_jid, DEFAULT_ACCESS_POLICY)
        if not rate_allowed:
            logger.info("Event rejected: rate limited", sender=event.sender, repo_jid=event.repo_jid, retry_after_ms=retry_after)
            return
    except Exception as err:
        logger.error("Access control check failed", repo_jid=event.repo_jid, error=str(err))
        return

    # Store message
    message = NewMessage(
        id=delivery_id,
        chat_jid=event.thread_jid,
        sender=event.sender,
        sender_name=event.sender,
        content=event.content,
        timestamp=datetime.now(timezone.utc).isoformat(),
        is_from_me=False,
        is_bot_message=False,
        github_metadata=event.metadata,
    )

    store_message(message)
    store_chat_metadata(event.repo_jid, message.timestamp, event.repo_full_name, "github", True)
    store_chat_metadata(event.thread_jid, message.timestamp, None, "github", True)

    logger.info(
        "GitHub event stored",
        event_type=event.event_type,
        repo_jid=event.repo_jid,
        thread_jid=event.thread_jid,
        sender=event.sender,
    )

    formatted = format_messages([message])
    if _queue.send_message(event.thread_jid, formatted):
        logger.debug("Piped event to active container", thread_jid=event.thread_jid)
        _last_agent_timestamp[event.thread_jid] = message.timestamp
        _save_state()
    else:
        _queue.enqueue_message_check(event.thread_jid)


async def _handle_installation_event(payload: dict) -> None:
    installation = payload.get("installation")
    if not installation:
        return

    added_repos = payload.get("repositories_added", [])
    for repo_data in added_repos:
        repo_jid = f"gh:{repo_data['full_name']}"
        if repo_jid in _registered_groups:
            continue
        folder = repo_data["full_name"].replace("/", "--")
        _register_group(repo_jid, RegisteredGroup(
            name=repo_data["full_name"],
            folder=folder,
            trigger=f"@{installation.get('app_slug', 'codeclaw')}",
            added_at=datetime.now(timezone.utc).isoformat(),
            requires_trigger=True,
        ))
        logger.info("Auto-registered repo from installation", repo_jid=repo_jid, folder=folder)

    for repo_data in payload.get("repositories_removed", []):
        repo_jid = f"gh:{repo_data['full_name']}"
        if repo_jid in _registered_groups:
            logger.info("Repo removed from installation (group preserved)", repo_jid=repo_jid)


async def _prepare_repo_checkout(owner: str, repo: str, token: str) -> str:
    repo_dir = str(DATA_DIR / "repos" / f"{owner}--{repo}")
    clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"

    git_dir = Path(repo_dir) / ".git"
    if not git_dir.exists():
        Path(repo_dir).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "50", clone_url, repo_dir], capture_output=True, timeout=120, check=True)
        logger.info("Repo cloned", owner=owner, repo=repo)
    else:
        try:
            subprocess.run(["git", "-C", repo_dir, "remote", "set-url", "origin", clone_url], capture_output=True, timeout=10, check=True)
            subprocess.run(["git", "-C", repo_dir, "fetch", "--depth", "50", "origin"], capture_output=True, timeout=60, check=True)
            subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "origin/HEAD"], capture_output=True, timeout=10, check=True)
        except Exception as err:
            logger.warning("Failed to fetch repo, using existing checkout", owner=owner, repo=repo, error=str(err))

    try:
        subprocess.run(["git", "-C", repo_dir, "config", "user.name", "CodeClaw AI"], capture_output=True)
        subprocess.run(["git", "-C", repo_dir, "config", "user.email", "codeclaw[bot]@users.noreply.github.com"], capture_output=True)
    except Exception:
        pass

    return repo_dir


async def _process_group_messages(chat_jid: str) -> bool:
    repo_jid = repo_jid_from_thread_jid(chat_jid) if chat_jid.startswith("gh:") else chat_jid
    group = _registered_groups.get(repo_jid) or _registered_groups.get(chat_jid)
    if not group:
        return True

    channel = find_channel(_channels, chat_jid)
    if not channel:
        logger.warning("No channel owns JID, skipping", chat_jid=chat_jid)
        return True

    since_timestamp = _last_agent_timestamp.get(chat_jid, "")
    missed_messages = get_messages_since(chat_jid, since_timestamp, ASSISTANT_NAME)
    if not missed_messages:
        return True

    prompt = format_messages(missed_messages)
    previous_cursor = _last_agent_timestamp.get(chat_jid, "")
    _last_agent_timestamp[chat_jid] = missed_messages[-1].timestamp
    _save_state()

    logger.info("Processing messages", group=group.name, chat_jid=chat_jid, message_count=len(missed_messages))

    # Prepare GitHub context
    repo_checkout_path: str | None = None
    github_token: str | None = None

    if chat_jid.startswith("gh:") and _token_manager:
        try:
            owner, repo = parse_repo_from_jid(chat_jid)
            checkout_token = await _token_manager.get_token_for_repo(owner, repo)
            repo_checkout_path = await _prepare_repo_checkout(owner, repo, checkout_token)
            github_token = await _token_manager.get_scoped_token_for_repo(owner, repo)
        except Exception as err:
            logger.error("Failed to prepare GitHub context", chat_jid=chat_jid, error=str(err))

    idle_handle: asyncio.TimerHandle | None = None

    def reset_idle_timer():
        nonlocal idle_handle
        if idle_handle:
            idle_handle.cancel()
        loop = asyncio.get_event_loop()
        idle_handle = loop.call_later(IDLE_TIMEOUT / 1000, lambda: _queue.close_stdin(chat_jid))

    had_error = False
    output_sent = False

    result = await _run_agent(group, prompt, chat_jid, repo_checkout_path, github_token, channel, reset_idle_timer)

    if idle_handle:
        idle_handle.cancel()

    if result == "error":
        if output_sent:
            logger.warning("Agent error after output was sent", group=group.name)
            return True
        _last_agent_timestamp[chat_jid] = previous_cursor
        _save_state()
        logger.warning("Agent error, rolled back message cursor for retry", group=group.name)
        return False

    return True


async def _run_agent(
    group: RegisteredGroup,
    prompt: str,
    chat_jid: str,
    repo_checkout_path: str | None,
    github_token: str | None,
    channel,
    reset_idle_timer,
) -> str:
    is_main = group.folder == MAIN_GROUP_FOLDER
    session_id = _sessions.get(group.folder)

    all_tasks = get_all_tasks()
    write_tasks_snapshot(
        group.folder,
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

    available_groups = _get_available_groups()
    write_groups_snapshot(group.folder, is_main, available_groups, set(_registered_groups.keys()))

    async def wrapped_on_output(output: ContainerOutput):
        if output.new_session_id:
            _sessions[group.folder] = output.new_session_id
            set_session(group.folder, output.new_session_id)

        if output.result:
            raw = output.result if isinstance(output.result, str) else json.dumps(output.result)
            import re
            text = re.sub(r"<internal>[\s\S]*?</internal>", "", raw).strip()
            if text:
                await channel.send_message(chat_jid, text)
            reset_idle_timer()

        if output.status == "success":
            _queue.notify_idle(chat_jid)

    try:
        secrets_dict: dict[str, str] = {}
        if github_token:
            add_github_token(secrets_dict, github_token)

        output = await run_container_agent(
            group,
            ContainerInput(
                prompt=prompt,
                session_id=session_id,
                group_folder=group.folder,
                chat_jid=chat_jid,
                is_main=is_main,
                assistant_name=ASSISTANT_NAME,
                repo_checkout_path=repo_checkout_path,
                secrets=secrets_dict,
            ),
            lambda proc, name: _queue.register_process(chat_jid, proc, name, group.folder),
            wrapped_on_output,
        )

        if output.new_session_id:
            _sessions[group.folder] = output.new_session_id
            set_session(group.folder, output.new_session_id)

        if output.status == "error":
            logger.error("Container agent error", group=group.name, error=output.error)
            return "error"

        return "success"
    except Exception as err:
        logger.error("Agent error", group=group.name, error=str(err))
        return "error"


def _recover_pending_messages() -> None:
    for chat_jid, group in _registered_groups.items():
        since = _last_agent_timestamp.get(chat_jid, "")
        pending = get_messages_since(chat_jid, since, ASSISTANT_NAME)
        if pending:
            logger.info("Recovery: found unprocessed messages", group=group.name, pending_count=len(pending))
            _queue.enqueue_message_check(chat_jid)


async def _reconciliation_loop() -> None:
    while True:
        try:
            cleanup_processed_events()
            _rate_limiter.cleanup()
        except Exception as err:
            logger.error("Reconciliation loop error", error=str(err))
        await asyncio.sleep(RECONCILIATION_INTERVAL / 1000)


async def _async_main() -> None:
    ensure_container_runtime_running()
    init_database()
    logger.info("Database initialized")
    _load_state()

    # Load GitHub App config
    app_config = load_github_app_config()
    global _token_manager

    if app_config:
        _token_manager = GitHubTokenManager(app_config)
        app_slug = await _token_manager.get_app_slug()
        logger.info("GitHub App authenticated", app_slug=app_slug)

        github = GitHubChannel(_token_manager)
        _channels.append(github)
        await github.connect()

        webhook_secret = app_config.webhook_secret
    else:
        logger.warning("GitHub App not configured, starting in setup mode")
        webhook_secret = secrets.token_hex(32)

    # Create FastAPI app
    def on_event(event_name: str, delivery_id: str, payload: dict):
        asyncio.ensure_future(_handle_webhook_event(event_name, delivery_id, payload))

    app = create_app(webhook_secret, on_event)

    # Start subsystems
    await start_scheduler_loop(SchedulerDependencies(
        registered_groups=lambda: _registered_groups,
        get_sessions=lambda: _sessions,
        queue=_queue,
        on_process=lambda jid, proc, name, folder: _queue.register_process(jid, proc, name, folder),
        send_message=lambda jid, raw: _send_message(jid, raw),
    ))

    await start_ipc_watcher(IpcDeps(
        send_message=lambda jid, text: _ipc_send_message(jid, text),
        send_structured_message=lambda jid, text, target: _ipc_send_structured(jid, text, target),
        registered_groups=lambda: _registered_groups,
        register_group=_register_group,
        sync_group_metadata=lambda force: asyncio.sleep(0),  # No-op for GitHub
        get_available_groups=_get_available_groups,
        write_groups_snapshot=lambda gf, im, ag, rj: write_groups_snapshot(gf, im, ag, rj),
    ))

    _queue.set_process_messages_fn(_process_group_messages)
    _recover_pending_messages()
    asyncio.create_task(_reconciliation_loop())

    logger.info("CodeClaw running (GitHub webhook mode)", port=PORT)

    # Run uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


async def _send_message(jid: str, raw_text: str) -> None:
    channel = find_channel(_channels, jid)
    if not channel:
        logger.warning("No channel owns JID, cannot send message", jid=jid)
        return
    text = format_outbound(raw_text)
    if text:
        await channel.send_message(jid, text)


async def _ipc_send_message(jid: str, text: str) -> None:
    channel = find_channel(_channels, jid)
    if not channel:
        raise RuntimeError(f"No channel for JID: {jid}")
    await channel.send_message(jid, text)


async def _ipc_send_structured(jid: str, text: str, target: dict) -> None:
    github = next((c for c in _channels if c.name == "github"), None)
    if not github:
        raise RuntimeError(f"No GitHub channel for JID: {jid}")
    await github.send_structured_message(jid, text, GitHubResponseTarget(**target))


def main() -> None:
    """Entry point for the codeclaw command."""
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.info("Shutdown signal received")
    except Exception as err:
        logger.error("Failed to start CodeClaw", error=str(err))
        sys.exit(1)


if __name__ == "__main__":
    main()
