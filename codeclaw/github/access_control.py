"""GitHub Access Control.

Permission checking and rate limiting for webhook events.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from codeclaw.logger import logger

PermissionLevel = str  # 'admin' | 'maintain' | 'write' | 'triage' | 'read' | 'none'

PERMISSION_RANK: dict[str, int] = {
    "admin": 5,
    "maintain": 4,
    "write": 3,
    "triage": 2,
    "read": 1,
    "none": 0,
}


@dataclass
class AccessPolicy:
    min_permission: PermissionLevel = "triage"
    allow_external_contributors: bool = False
    rate_limit_per_user: int = 10
    rate_limit_window_ms: int = 3_600_000  # 1 hour


DEFAULT_ACCESS_POLICY = AccessPolicy()


async def check_permission(
    headers: dict[str, str],
    owner: str,
    repo: str,
    username: str,
    policy: AccessPolicy,
) -> tuple[bool, str | None]:
    """Check if a user has sufficient permission to trigger the bot.

    Returns (allowed, reason).
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/collaborators/{username}/permission",
                headers=headers,
            )

            if resp.status_code == 404:
                if policy.allow_external_contributors:
                    return True, None
                return False, "Not a collaborator"

            resp.raise_for_status()
            data = resp.json()

            user_level = data.get("permission", "none")
            user_rank = PERMISSION_RANK.get(user_level, 0)
            required_rank = PERMISSION_RANK.get(policy.min_permission, 0)

            if user_rank >= required_rank:
                return True, None

            if policy.allow_external_contributors:
                return True, None

            return False, f"Insufficient permissions: {user_level} < {policy.min_permission}"

    except httpx.HTTPStatusError as err:
        logger.error(
            "Failed to check permission",
            owner=owner,
            repo=repo,
            username=username,
            status=err.response.status_code,
        )
        return False, "Permission check failed"
    except Exception as err:
        logger.error("Failed to check permission", owner=owner, repo=repo, username=username, error=str(err))
        return False, "Permission check failed"


class RateLimiter:
    """Simple in-memory rate limiter.

    Tracks invocation timestamps per user-repo pair.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, list[float]] = {}

    def check(self, user: str, repo_jid: str, policy: AccessPolicy) -> tuple[bool, int | None]:
        """Check rate limit. Returns (allowed, retry_after_ms)."""
        key = f"{user}:{repo_jid}"
        now = time.time() * 1000  # ms
        window = policy.rate_limit_window_ms

        timestamps = self._buckets.get(key, [])
        # Prune expired entries
        timestamps = [t for t in timestamps if now - t < window]

        if len(timestamps) >= policy.rate_limit_per_user:
            oldest = timestamps[0]
            retry_after_ms = int(window - (now - oldest))
            return False, retry_after_ms

        timestamps.append(now)
        self._buckets[key] = timestamps
        return True, None

    def cleanup(self, max_age_ms: int = 7_200_000) -> None:
        """Periodic cleanup of stale entries."""
        now = time.time() * 1000
        keys_to_delete = []
        for key, timestamps in self._buckets.items():
            fresh = [t for t in timestamps if now - t < max_age_ms]
            if not fresh:
                keys_to_delete.append(key)
            else:
                self._buckets[key] = fresh
        for key in keys_to_delete:
            del self._buckets[key]
