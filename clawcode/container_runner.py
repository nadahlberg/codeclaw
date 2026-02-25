"""Container Runner for ClawCode.

Spawns agent execution in containers and handles IPC.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from clawcode.config import (
    CONTAINER_IMAGE,
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_TIMEOUT,
    DATA_DIR,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    TIMEZONE,
)
from clawcode.container_runtime import CONTAINER_RUNTIME_BIN, readonly_mount_args, stop_container_cmd
from clawcode.env import read_env_file
from clawcode.group_folder import resolve_group_folder_path, resolve_group_ipc_path
from clawcode.logger import logger
from clawcode.models import RegisteredGroup
from clawcode.mount_security import validate_additional_mounts

OUTPUT_START_MARKER = "---CLAWCODE_OUTPUT_START---"
OUTPUT_END_MARKER = "---CLAWCODE_OUTPUT_END---"


@dataclass
class ContainerInput:
    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None
    secrets: dict[str, str] | None = None
    repo_checkout_path: str | None = None


@dataclass
class ContainerOutput:
    status: str  # 'success' | 'error'
    result: str | None
    new_session_id: str | None = None
    error: str | None = None


def _build_volume_mounts(
    group: RegisteredGroup,
    is_main: bool,
    repo_checkout_path: str | None = None,
) -> list[dict]:
    mounts: list[dict] = []
    project_root = Path.cwd()
    group_dir = resolve_group_folder_path(group.folder)

    if repo_checkout_path:
        mounts.append({"host_path": repo_checkout_path, "container_path": "/workspace/repo", "readonly": False})

    mounts.append({"host_path": group_dir, "container_path": "/workspace/group", "readonly": False})

    if is_main:
        store_dir = project_root / "store"
        if store_dir.exists():
            mounts.append({"host_path": str(store_dir), "container_path": "/workspace/store", "readonly": True})

        data_dir = project_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        mounts.append({"host_path": str(data_dir), "container_path": "/workspace/data", "readonly": False})

        mounts.append({"host_path": str(GROUPS_DIR), "container_path": "/workspace/groups", "readonly": False})
    else:
        global_dir = GROUPS_DIR / "global"
        if global_dir.exists():
            mounts.append({"host_path": str(global_dir), "container_path": "/workspace/global", "readonly": True})

    # Per-group Claude sessions directory
    group_sessions_dir = DATA_DIR / "sessions" / group.folder / ".claude"
    group_sessions_dir.mkdir(parents=True, exist_ok=True)
    settings_file = group_sessions_dir / "settings.json"
    if not settings_file.exists():
        settings_file.write_text(json.dumps({
            "env": {
                "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
            },
        }, indent=2) + "\n")

    # Sync skills
    skills_src = Path.cwd() / "container" / "skills"
    skills_dst = group_sessions_dir / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if skill_dir.is_dir():
                dst_dir = skills_dst / skill_dir.name
                shutil.copytree(str(skill_dir), str(dst_dir), dirs_exist_ok=True)

    mounts.append({"host_path": str(group_sessions_dir), "container_path": "/home/node/.claude", "readonly": False})

    # Per-group IPC namespace
    group_ipc_dir = resolve_group_ipc_path(group.folder)
    for subdir in ("messages", "tasks", "input"):
        Path(group_ipc_dir, subdir).mkdir(parents=True, exist_ok=True)
    mounts.append({"host_path": group_ipc_dir, "container_path": "/workspace/ipc", "readonly": False})

    # Additional mounts
    if group.container_config and group.container_config.additional_mounts:
        validated = validate_additional_mounts(
            group.container_config.additional_mounts, group.name, is_main
        )
        mounts.extend(validated)

    return mounts


def _read_secrets() -> dict[str, str]:
    return read_env_file(["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"])


def add_github_token(secrets: dict[str, str], token: str) -> None:
    secrets["GITHUB_TOKEN"] = token


def _build_container_args(mounts: list[dict], container_name: str) -> list[str]:
    args = ["run", "-i", "--rm", "--name", container_name]

    # Container hardening
    args.extend([
        "--cap-drop=ALL",
        "--cap-add=SYS_ADMIN",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        "--add-host=metadata.google.internal:0.0.0.0",
        "--add-host=169.254.169.254:0.0.0.0",
    ])

    args.extend(["-e", f"TZ={TIMEZONE}"])

    host_uid = os.getuid()
    host_gid = os.getgid()
    if host_uid != 0 and host_uid != 1000:
        args.extend(["--user", f"{host_uid}:{host_gid}"])
        args.extend(["-e", "HOME=/home/node"])

    for mount in mounts:
        if mount["readonly"]:
            args.extend(readonly_mount_args(mount["host_path"], mount["container_path"]))
        else:
            args.extend(["-v", f"{mount['host_path']}:{mount['container_path']}"])

    args.append(CONTAINER_IMAGE)
    return args


async def run_container_agent(
    group: RegisteredGroup,
    input_data: ContainerInput,
    on_process: callable,
    on_output: callable | None = None,
) -> ContainerOutput:
    """Spawn an agent container and stream results."""
    start_time = time.time()

    group_dir = resolve_group_folder_path(group.folder)
    Path(group_dir).mkdir(parents=True, exist_ok=True)

    mounts = _build_volume_mounts(group, input_data.is_main, input_data.repo_checkout_path)
    safe_name = group.folder.replace("/", "-").replace("\\", "-")
    container_name = f"clawcode-{safe_name}-{int(time.time() * 1000)}"
    container_args = _build_container_args(mounts, container_name)

    logger.info(
        "Spawning container agent",
        group=group.name,
        container_name=container_name,
        mount_count=len(mounts),
        is_main=input_data.is_main,
    )

    logs_dir = Path(group_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Spawn the container process
    process = await asyncio.create_subprocess_exec(
        CONTAINER_RUNTIME_BIN,
        *container_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    on_process(process, container_name)

    # Pass secrets via stdin
    secrets = {**_read_secrets(), **(input_data.secrets or {})}
    stdin_data = json.dumps({
        "prompt": input_data.prompt,
        "sessionId": input_data.session_id,
        "groupFolder": input_data.group_folder,
        "chatJid": input_data.chat_jid,
        "isMain": input_data.is_main,
        "isScheduledTask": input_data.is_scheduled_task,
        "assistantName": input_data.assistant_name,
        "secrets": secrets,
    }).encode()
    process.stdin.write(stdin_data)
    process.stdin.close()

    # Track output
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_truncated = False
    stderr_truncated = False
    parse_buffer = ""
    new_session_id: str | None = None
    had_streaming_output = False
    timed_out = False

    config_timeout = (group.container_config.timeout if group.container_config and group.container_config.timeout else CONTAINER_TIMEOUT) / 1000
    timeout_secs = max(config_timeout, (IDLE_TIMEOUT + 30_000) / 1000)

    # Timeout handling
    timeout_handle: asyncio.TimerHandle | None = None

    def kill_on_timeout():
        nonlocal timed_out
        timed_out = True
        logger.error("Container timeout, stopping gracefully", group=group.name, container_name=container_name)
        asyncio.create_task(_stop_container(container_name, process))

    def reset_timeout():
        nonlocal timeout_handle
        if timeout_handle:
            timeout_handle.cancel()
        loop = asyncio.get_event_loop()
        timeout_handle = loop.call_later(timeout_secs, kill_on_timeout)

    reset_timeout()

    # Read stdout
    async def read_stdout():
        nonlocal stdout_buf, stdout_truncated, parse_buffer, new_session_id, had_streaming_output
        while True:
            chunk = await process.stdout.read(8192)
            if not chunk:
                break
            text = chunk.decode(errors="replace")

            if not stdout_truncated:
                remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stdout_buf)
                if len(chunk) > remaining:
                    stdout_buf.extend(chunk[:remaining])
                    stdout_truncated = True
                    logger.warning("Container stdout truncated", group=group.name, size=len(stdout_buf))
                else:
                    stdout_buf.extend(chunk)

            if on_output:
                parse_buffer += text
                while OUTPUT_START_MARKER in parse_buffer:
                    start_idx = parse_buffer.index(OUTPUT_START_MARKER)
                    end_idx = parse_buffer.find(OUTPUT_END_MARKER, start_idx)
                    if end_idx == -1:
                        break
                    json_str = parse_buffer[start_idx + len(OUTPUT_START_MARKER):end_idx].strip()
                    parse_buffer = parse_buffer[end_idx + len(OUTPUT_END_MARKER):]
                    try:
                        parsed = json.loads(json_str)
                        output = ContainerOutput(
                            status=parsed.get("status", "success"),
                            result=parsed.get("result"),
                            new_session_id=parsed.get("newSessionId"),
                            error=parsed.get("error"),
                        )
                        if output.new_session_id:
                            new_session_id = output.new_session_id
                        had_streaming_output = True
                        reset_timeout()
                        await on_output(output)
                    except json.JSONDecodeError as err:
                        logger.warning("Failed to parse streamed output chunk", group=group.name, error=str(err))

    # Read stderr
    async def read_stderr():
        nonlocal stderr_buf, stderr_truncated
        while True:
            chunk = await process.stderr.read(8192)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            for line in text.strip().split("\n"):
                if line:
                    logger.debug(line, container=group.folder)
            if not stderr_truncated:
                remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stderr_buf)
                if len(chunk) > remaining:
                    stderr_buf.extend(chunk[:remaining])
                    stderr_truncated = True
                else:
                    stderr_buf.extend(chunk)

    await asyncio.gather(read_stdout(), read_stderr())
    return_code = await process.wait()

    if timeout_handle:
        timeout_handle.cancel()

    duration = time.time() - start_time

    if timed_out:
        if had_streaming_output:
            logger.info("Container timed out after output (idle cleanup)", group=group.name, duration=f"{duration:.1f}s")
            return ContainerOutput(status="success", result=None, new_session_id=new_session_id)
        logger.error("Container timed out with no output", group=group.name, duration=f"{duration:.1f}s")
        return ContainerOutput(status="error", result=None, error=f"Container timed out after {config_timeout}s")

    if return_code != 0:
        stderr_text = stderr_buf.decode(errors="replace")
        logger.error("Container exited with error", group=group.name, code=return_code, duration=f"{duration:.1f}s")
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container exited with code {return_code}: {stderr_text[-200:]}",
        )

    if on_output:
        logger.info("Container completed (streaming mode)", group=group.name, duration=f"{duration:.1f}s", new_session_id=new_session_id)
        return ContainerOutput(status="success", result=None, new_session_id=new_session_id)

    # Legacy mode: parse last output marker pair
    stdout_text = stdout_buf.decode(errors="replace")
    try:
        start_idx = stdout_text.find(OUTPUT_START_MARKER)
        end_idx = stdout_text.find(OUTPUT_END_MARKER)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_line = stdout_text[start_idx + len(OUTPUT_START_MARKER):end_idx].strip()
        else:
            lines = stdout_text.strip().split("\n")
            json_line = lines[-1]
        parsed = json.loads(json_line)
        return ContainerOutput(
            status=parsed.get("status", "success"),
            result=parsed.get("result"),
            new_session_id=parsed.get("newSessionId"),
            error=parsed.get("error"),
        )
    except Exception as err:
        return ContainerOutput(status="error", result=None, error=f"Failed to parse container output: {err}")


async def _stop_container(container_name: str, process: asyncio.subprocess.Process) -> None:
    try:
        stop_proc = await asyncio.create_subprocess_shell(
            stop_container_cmd(container_name),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(stop_proc.wait(), timeout=15)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def write_tasks_snapshot(group_folder: str, is_main: bool, tasks: list[dict]) -> None:
    """Write filtered tasks to the group's IPC directory."""
    group_ipc_dir = resolve_group_ipc_path(group_folder)
    Path(group_ipc_dir).mkdir(parents=True, exist_ok=True)
    filtered = tasks if is_main else [t for t in tasks if t.get("groupFolder") == group_folder]
    tasks_file = Path(group_ipc_dir) / "current_tasks.json"
    tasks_file.write_text(json.dumps(filtered, indent=2))


def write_groups_snapshot(
    group_folder: str,
    is_main: bool,
    groups: list[dict],
    registered_jids: set[str],
) -> None:
    """Write available groups snapshot for the container to read."""
    group_ipc_dir = resolve_group_ipc_path(group_folder)
    Path(group_ipc_dir).mkdir(parents=True, exist_ok=True)
    visible_groups = groups if is_main else []
    groups_file = Path(group_ipc_dir) / "available_groups.json"
    groups_file.write_text(json.dumps({
        "groups": visible_groups,
        "lastSync": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))
