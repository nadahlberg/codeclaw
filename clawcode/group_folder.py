import re

from clawcode.config import DATA_DIR, GROUPS_DIR

# Allow owner--repo format for GitHub repos (e.g., "octocat--hello-world")
_GROUP_FOLDER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_RESERVED_FOLDERS = {"global"}


def is_valid_group_folder(folder: str) -> bool:
    if not folder:
        return False
    if folder != folder.strip():
        return False
    if not _GROUP_FOLDER_PATTERN.match(folder):
        return False
    if "/" in folder or "\\" in folder:
        return False
    if ".." in folder:
        return False
    if folder.lower() in _RESERVED_FOLDERS:
        return False
    return True


def assert_valid_group_folder(folder: str) -> None:
    if not is_valid_group_folder(folder):
        raise ValueError(f'Invalid group folder "{folder}"')


def _ensure_within_base(base_dir: str, resolved_path: str) -> None:
    from pathlib import Path

    rel = Path(resolved_path).relative_to(base_dir)
    # relative_to raises ValueError if not within base, but double-check
    if str(rel).startswith(".."):
        raise ValueError(f"Path escapes base directory: {resolved_path}")


def resolve_group_folder_path(folder: str) -> str:
    assert_valid_group_folder(folder)
    group_path = str((GROUPS_DIR / folder).resolve())
    _ensure_within_base(str(GROUPS_DIR), group_path)
    return group_path


def resolve_group_ipc_path(folder: str) -> str:
    assert_valid_group_folder(folder)
    ipc_base_dir = (DATA_DIR / "ipc").resolve()
    ipc_path = str((ipc_base_dir / folder).resolve())
    _ensure_within_base(str(ipc_base_dir), ipc_path)
    return ipc_path
