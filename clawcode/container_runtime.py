"""Container runtime abstraction for ClawCode.

All runtime-specific logic lives here so swapping runtimes means changing one file.
"""

from __future__ import annotations

import subprocess

from clawcode.logger import logger

CONTAINER_RUNTIME_BIN = "docker"


def readonly_mount_args(host_path: str, container_path: str) -> list[str]:
    """Returns CLI args for a readonly bind mount."""
    return ["-v", f"{host_path}:{container_path}:ro"]


def stop_container_cmd(name: str) -> str:
    """Returns the shell command to stop a container by name."""
    return f"{CONTAINER_RUNTIME_BIN} stop {name}"


def ensure_container_runtime_running() -> None:
    """Ensure the container runtime is running, starting it if needed."""
    try:
        subprocess.run(
            [CONTAINER_RUNTIME_BIN, "info"],
            capture_output=True,
            timeout=10,
            check=True,
        )
        logger.debug("Container runtime already running")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as err:
        logger.error("Failed to reach container runtime", error=str(err))
        print(
            "\n"
            "╔════════════════════════════════════════════════════════════════╗\n"
            "║  FATAL: Container runtime failed to start                      ║\n"
            "║                                                                ║\n"
            "║  Agents cannot run without a container runtime. To fix:        ║\n"
            "║  1. Ensure Docker is installed and running                     ║\n"
            "║  2. Run: docker info                                           ║\n"
            "║  3. Restart ClawCode                                           ║\n"
            "╚════════════════════════════════════════════════════════════════╝\n",
            flush=True,
        )
        raise RuntimeError("Container runtime is required but failed to start") from err


def cleanup_orphans() -> None:
    """Kill orphaned ClawCode containers from previous runs."""
    try:
        result = subprocess.run(
            [CONTAINER_RUNTIME_BIN, "ps", "--filter", "name=clawcode-", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        orphans = [name for name in result.stdout.strip().split("\n") if name]
        for name in orphans:
            try:
                subprocess.run(
                    [CONTAINER_RUNTIME_BIN, "stop", name],
                    capture_output=True,
                    timeout=15,
                )
            except Exception:
                pass  # already stopped
        if orphans:
            logger.info("Stopped orphaned containers", count=len(orphans), names=orphans)
    except Exception as err:
        logger.warning("Failed to clean up orphaned containers", error=str(err))
