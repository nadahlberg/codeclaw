import os
from pathlib import Path

from codeclaw.env import read_env_file

# Read config values from .env (falls back to os.environ).
# Secrets are NOT read here — they stay on disk and are loaded only
# where needed (container_runner.py) to avoid leaking to child processes.
_env_config = read_env_file(["ASSISTANT_NAME"])

ASSISTANT_NAME: str = os.environ.get("ASSISTANT_NAME") or _env_config.get("ASSISTANT_NAME", "CodeClaw")
SCHEDULER_POLL_INTERVAL: int = 60_000  # ms
RECONCILIATION_INTERVAL: int = 60_000  # ms

# Absolute paths needed for container mounts
PROJECT_ROOT: Path = Path.cwd()
HOME_DIR: Path = Path(os.environ.get("HOME", str(Path.home())))

# Mount security: allowlist stored OUTSIDE project root, never mounted into containers
MOUNT_ALLOWLIST_PATH: Path = HOME_DIR / ".config" / "codeclaw" / "mount-allowlist.json"
STORE_DIR: Path = (PROJECT_ROOT / "store").resolve()
GROUPS_DIR: Path = (PROJECT_ROOT / "groups").resolve()
DATA_DIR: Path = (PROJECT_ROOT / "data").resolve()
MAIN_GROUP_FOLDER: str = "main"

CONTAINER_IMAGE: str = os.environ.get("CONTAINER_IMAGE", "codeclaw-agent:latest")
CONTAINER_TIMEOUT: int = int(os.environ.get("CONTAINER_TIMEOUT", "1800000"))
CONTAINER_MAX_OUTPUT_SIZE: int = int(os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760"))
IPC_POLL_INTERVAL: int = 1000  # ms
IDLE_TIMEOUT: int = int(os.environ.get("IDLE_TIMEOUT", "1800000"))
MAX_CONCURRENT_CONTAINERS: int = max(1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5")))

# HTTP server port for webhooks
PORT: int = int(os.environ.get("PORT", "3000"))

# Timezone for scheduled tasks
TIMEZONE: str = os.environ.get("TZ", "UTC")
