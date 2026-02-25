"""GitHub App Authentication.

Manages JWT generation and installation token caching.
The private key lives at ~/.config/codeclaw/github-app.pem (outside project root).
Containers only receive short-lived installation tokens, never the private key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jwt

from codeclaw.env import read_env_file
from codeclaw.logger import logger


@dataclass
class GitHubAppConfig:
    app_id: str
    private_key: str
    webhook_secret: str
    app_slug: str | None = None


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # Unix timestamp


class GitHubTokenManager:
    def __init__(self, config: GitHubAppConfig) -> None:
        self.config = config
        self._token_cache: dict[int, _CachedToken] = {}
        self._installation_for_repo: dict[str, int] = {}

    def _generate_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + (10 * 60),  # 10 minutes max
            "iss": self.config.app_id,
        }
        return jwt.encode(payload, self.config.private_key, algorithm="RS256")

    def _app_headers(self) -> dict[str, str]:
        token = self._generate_jwt()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_app_slug(self) -> str:
        """Get the app slug (login name like 'codeclaw-ai[bot]')."""
        if self.config.app_slug:
            return self.config.app_slug
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.github.com/app",
                headers=self._app_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            slug = data.get("slug", f"app-{self.config.app_id}")
            self.config.app_slug = slug
            return slug

    async def get_installation_token(self, installation_id: int) -> str:
        """Get an installation token, cached with auto-refresh 5 min before expiry."""
        cached = self._token_cache.get(installation_id)
        if cached and cached.expires_at - time.time() > 5 * 60:
            return cached.token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers=self._app_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            token = data["token"]
            # Parse ISO 8601 expiry to timestamp
            from datetime import datetime, timezone

            expires_at_str = data.get("expires_at", "")
            if expires_at_str:
                dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                expires_at = dt.timestamp()
            else:
                expires_at = time.time() + 3600  # fallback: 1 hour

            self._token_cache[installation_id] = _CachedToken(token=token, expires_at=expires_at)
            return token

    async def _resolve_installation_id(self, owner: str, repo: str) -> int:
        key = f"{owner}/{repo}"
        cached = self._installation_for_repo.get(key)
        if cached is not None:
            return cached

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/installation",
                headers=self._app_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            installation_id = data["id"]
            self._installation_for_repo[key] = installation_id
            return installation_id

    async def get_token_for_repo(self, owner: str, repo: str) -> str:
        """Get an installation token for a specific repo."""
        installation_id = await self._resolve_installation_id(owner, repo)
        return await self.get_installation_token(installation_id)

    async def get_scoped_token_for_repo(self, owner: str, repo: str) -> str:
        """Get a token scoped to a single repo with minimal permissions.

        Use this for tokens passed into agent containers — limits blast radius
        if the token is exfiltrated via prompt injection.
        """
        installation_id = await self._resolve_installation_id(owner, repo)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers=self._app_headers(),
                json={
                    "repositories": [repo],
                    "permissions": {
                        "contents": "write",
                        "pull_requests": "write",
                        "issues": "write",
                        "metadata": "read",
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["token"]

    async def get_headers_for_repo(self, owner: str, repo: str) -> dict[str, str]:
        """Get auth headers for a specific repo."""
        token = await self.get_token_for_repo(owner, repo)
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def webhook_secret(self) -> str:
        return self.config.webhook_secret


def load_github_app_config() -> GitHubAppConfig | None:
    """Load GitHub App config from .env and private key file.

    Returns None if GitHub App is not configured.
    """
    env = read_env_file([
        "GITHUB_APP_ID",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_PRIVATE_KEY_PATH",
        "GITHUB_PRIVATE_KEY",
    ])

    app_id = env.get("GITHUB_APP_ID")
    webhook_secret = env.get("GITHUB_WEBHOOK_SECRET")

    if not app_id or not webhook_secret:
        return None

    # Private key: either inline in env or read from file
    private_key = env.get("GITHUB_PRIVATE_KEY")
    if not private_key:
        key_path = env.get(
            "GITHUB_PRIVATE_KEY_PATH",
            str(Path.home() / ".config" / "codeclaw" / "github-app.pem"),
        )
        try:
            private_key = Path(key_path).read_text()
        except FileNotFoundError:
            logger.error("GitHub App private key not found", key_path=key_path)
            return None

    return GitHubAppConfig(app_id=app_id, private_key=private_key, webhook_secret=webhook_secret)
