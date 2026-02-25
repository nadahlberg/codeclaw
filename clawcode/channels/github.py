"""GitHub Channel.

Implements the Channel interface for GitHub (issues, PRs, comments, reviews).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from clawcode.github.auth import GitHubTokenManager
from clawcode.github.event_mapper import parse_repo_from_jid
from clawcode.logger import logger


@dataclass
class GitHubResponseTarget:
    type: str  # 'issue_comment' | 'pr_comment' | 'pr_review' | 'new_pr'
    issue_number: int | None = None
    pr_number: int | None = None
    review_action: str | None = None  # 'APPROVE' | 'REQUEST_CHANGES' | 'COMMENT'
    review_comments: list[dict] | None = None
    head: str | None = None
    base: str | None = None
    title: str | None = None


class GitHubChannel:
    name = "github"

    def __init__(self, token_manager: GitHubTokenManager) -> None:
        self._token_manager = token_manager
        self._connected = False

    async def connect(self) -> None:
        """Validate credentials by fetching app info."""
        try:
            await self._token_manager.get_app_slug()
            self._connected = True
            logger.info("GitHub channel connected")
        except Exception as err:
            logger.error("Failed to connect GitHub channel", error=str(err))
            raise

    async def send_message(self, jid: str, text: str) -> None:
        """Send a message (comment) to a GitHub thread.

        JID format: 'gh:owner/repo#issue:42' or 'gh:owner/repo#pr:17'
        """
        owner, repo = parse_repo_from_jid(jid)
        thread_part = jid.split("#")[1] if "#" in jid else None
        if not thread_part:
            logger.warning("Cannot send message: no thread specified", jid=jid)
            return

        headers = await self._token_manager.get_headers_for_repo(owner, repo)
        type_str, number_str = thread_part.split(":")
        try:
            number = int(number_str)
        except ValueError:
            logger.warning("Invalid thread number", jid=jid, thread_part=thread_part)
            return

        # Both issues and PRs use the issues API for comments
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
                headers=headers,
                json={"body": text},
            )
            resp.raise_for_status()

        logger.info("GitHub comment posted", jid=jid, type=type_str, number=number, length=len(text))

    async def send_structured_message(
        self, jid: str, text: str, target: GitHubResponseTarget
    ) -> None:
        """Send a structured response (review, new PR, etc.)."""
        owner, repo = parse_repo_from_jid(jid)
        headers = await self._token_manager.get_headers_for_repo(owner, repo)

        async with httpx.AsyncClient() as client:
            if target.type == "issue_comment":
                await client.post(
                    f"https://api.github.com/repos/{owner}/{repo}/issues/{target.issue_number}/comments",
                    headers=headers,
                    json={"body": text},
                )
            elif target.type == "pr_comment":
                await client.post(
                    f"https://api.github.com/repos/{owner}/{repo}/issues/{target.pr_number}/comments",
                    headers=headers,
                    json={"body": text},
                )
            elif target.type == "pr_review":
                review_payload: dict = {
                    "body": text,
                    "event": target.review_action or "COMMENT",
                }
                if target.review_comments:
                    review_payload["comments"] = [
                        {"path": c["path"], "line": c["line"], "body": c["body"]}
                        for c in target.review_comments
                    ]
                await client.post(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{target.pr_number}/reviews",
                    headers=headers,
                    json=review_payload,
                )
            elif target.type == "new_pr":
                await client.post(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls",
                    headers=headers,
                    json={
                        "title": target.title or "New PR",
                        "body": text,
                        "head": target.head,
                        "base": target.base or "main",
                    },
                )

        logger.info("GitHub structured message sent", jid=jid, target_type=target.type)

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("gh:")

    async def disconnect(self) -> None:
        self._connected = False

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        """Typing indicator: best-effort, currently a no-op for GitHub."""
        thread_part = jid.split("#")[1] if "#" in jid else None
        if not thread_part or not thread_part.startswith("pr:"):
            return
        logger.debug("Typing indicator (GitHub check runs) not yet implemented", jid=jid)
