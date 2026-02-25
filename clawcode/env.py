from pathlib import Path

from clawcode.logger import logger


def read_env_file(keys: list[str], env_path: Path | None = None) -> dict[str, str]:
    """Parse the .env file and return values for the requested keys.

    Does NOT load anything into os.environ — callers decide what to
    do with the values. This keeps secrets out of the process environment
    so they don't leak to child processes.
    """
    if env_path is None:
        env_path = Path.cwd() / ".env"

    try:
        content = env_path.read_text()
    except FileNotFoundError:
        logger.debug(".env file not found, using defaults", path=str(env_path))
        return {}

    result: dict[str, str] = {}
    wanted = set(keys)

    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        eq_idx = trimmed.find("=")
        if eq_idx == -1:
            continue
        key = trimmed[:eq_idx].strip()
        if key not in wanted:
            continue
        value = trimmed[eq_idx + 1 :].strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if value:
            result[key] = value

    return result
