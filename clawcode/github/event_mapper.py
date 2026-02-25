"""GitHub Event Mapper.

Converts webhook payloads into normalized GitHubEvent objects
for the ClawCode message pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clawcode.models import GitHubEventMetadata
from clawcode.router import escape_xml


@dataclass
class GitHubEvent:
    event_type: str
    action: str
    installation_id: int
    repo_full_name: str
    repo_jid: str  # 'gh:owner/repo'
    thread_jid: str  # 'gh:owner/repo#issue:42' or 'gh:owner/repo#pr:17'
    sender: str  # GitHub username
    content: str  # Formatted XML prompt for the agent
    metadata: GitHubEventMetadata


def repo_jid_from_thread_jid(thread_jid: str) -> str:
    """Extract repo-level JID from a thread JID."""
    return thread_jid.split("#")[0]


def parse_repo_from_jid(jid: str) -> tuple[str, str]:
    """Parse owner/repo from a JID like 'gh:owner/repo' or 'gh:owner/repo#issue:42'.

    Returns (owner, repo).
    """
    repo_jid = repo_jid_from_thread_jid(jid)
    repo_path = repo_jid.replace("gh:", "")
    parts = repo_path.split("/")
    return parts[0], parts[1]


def map_webhook_to_event(
    event_name: str,
    payload: dict,
    app_slug: str,
) -> GitHubEvent | None:
    """Map a GitHub webhook event to a GitHubEvent, or None if we don't handle it."""
    installation = payload.get("installation")
    if not installation:
        return None

    repo = payload.get("repository")
    if not repo:
        return None

    sender = payload.get("sender")
    if not sender:
        return None

    # Bot loop prevention
    if sender.get("type") == "Bot" or sender.get("login") == f"{app_slug}[bot]":
        return None

    action = payload.get("action", "")
    repo_jid = f"gh:{repo['full_name']}"
    installation_id = installation["id"]
    repo_full_name = repo["full_name"]
    sender_login = sender["login"]

    handlers = {
        "issues": _map_issue_event,
        "issue_comment": _map_issue_comment_event,
        "pull_request": _map_pull_request_event,
        "pull_request_review": _map_pr_review_event,
        "pull_request_review_comment": _map_pr_review_comment_event,
    }

    handler = handlers.get(event_name)
    if handler is None:
        return None

    if event_name in ("issue_comment", "pull_request_review", "pull_request_review_comment"):
        return handler(action, payload, repo_jid, repo_full_name, installation_id, sender_login, app_slug)
    else:
        return handler(action, payload, repo_jid, repo_full_name, installation_id, sender_login)


def _map_issue_event(
    action: str,
    payload: dict,
    repo_jid: str,
    repo_full_name: str,
    installation_id: int,
    sender: str,
) -> GitHubEvent | None:
    if action not in ("opened", "assigned"):
        return None

    issue = payload["issue"]
    thread_jid = f"{repo_jid}#issue:{issue['number']}"
    content = (
        f'<github_event type="issue_{action}" repo="{escape_xml(repo_full_name)}" '
        f'issue="#{issue["number"]}" sender="{escape_xml(sender)}">\n'
        f"  <issue_title>{escape_xml(issue['title'])}</issue_title>\n"
        f"  <issue_body>{escape_xml(issue.get('body') or '')}</issue_body>\n"
        f"</github_event>"
    )

    return GitHubEvent(
        event_type="issues",
        action=action,
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        repo_jid=repo_jid,
        thread_jid=thread_jid,
        sender=sender,
        content=content,
        metadata=GitHubEventMetadata(issue_number=issue["number"]),
    )


def _map_issue_comment_event(
    action: str,
    payload: dict,
    repo_jid: str,
    repo_full_name: str,
    installation_id: int,
    sender: str,
    app_slug: str,
) -> GitHubEvent | None:
    if action != "created":
        return None

    issue = payload["issue"]
    comment = payload["comment"]

    # Detect if this is on a PR (GitHub sends issue_comment for PR comments too)
    is_pr = "pull_request" in issue and issue["pull_request"] is not None
    thread_jid = (
        f"{repo_jid}#pr:{issue['number']}" if is_pr else f"{repo_jid}#issue:{issue['number']}"
    )

    mention_pattern = re.compile(rf"@{re.escape(app_slug)}\b", re.IGNORECASE)
    has_mention = bool(mention_pattern.search(comment["body"]))

    event_type = "pr_comment" if is_pr else "issue_comment"

    content = (
        f'<github_event type="{event_type}" repo="{escape_xml(repo_full_name)}" '
        f'issue="#{issue["number"]}" sender="{escape_xml(sender)}" mentioned="{has_mention}">\n'
        f"  <issue_title>{escape_xml(issue['title'])}</issue_title>\n"
        f"  <comment>{escape_xml(comment['body'])}</comment>\n"
        f"</github_event>"
    )

    return GitHubEvent(
        event_type="issue_comment",
        action=action,
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        repo_jid=repo_jid,
        thread_jid=thread_jid,
        sender=sender,
        content=content,
        metadata=GitHubEventMetadata(
            issue_number=None if is_pr else issue["number"],
            pr_number=issue["number"] if is_pr else None,
            comment_id=comment["id"],
        ),
    )


def _map_pull_request_event(
    action: str,
    payload: dict,
    repo_jid: str,
    repo_full_name: str,
    installation_id: int,
    sender: str,
) -> GitHubEvent | None:
    if action not in ("opened", "synchronize"):
        return None

    pr = payload["pull_request"]
    thread_jid = f"{repo_jid}#pr:{pr['number']}"
    content = (
        f'<github_event type="pull_request_{action}" repo="{escape_xml(repo_full_name)}" '
        f'pr="#{pr["number"]}" sender="{escape_xml(sender)}">\n'
        f"  <pr_title>{escape_xml(pr['title'])}</pr_title>\n"
        f"  <pr_body>{escape_xml(pr.get('body') or '')}</pr_body>\n"
        f'  <stats additions="{pr["additions"]}" deletions="{pr["deletions"]}" changed_files="{pr["changed_files"]}" />\n'
        f"  <head_sha>{pr['head']['sha']}</head_sha>\n"
        f"</github_event>"
    )

    return GitHubEvent(
        event_type="pull_request",
        action=action,
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        repo_jid=repo_jid,
        thread_jid=thread_jid,
        sender=sender,
        content=content,
        metadata=GitHubEventMetadata(pr_number=pr["number"], sha=pr["head"]["sha"]),
    )


def _map_pr_review_event(
    action: str,
    payload: dict,
    repo_jid: str,
    repo_full_name: str,
    installation_id: int,
    sender: str,
    app_slug: str,
) -> GitHubEvent | None:
    if action != "submitted":
        return None

    pr = payload["pull_request"]
    review = payload["review"]

    mention_pattern = re.compile(rf"@{re.escape(app_slug)}\b", re.IGNORECASE)
    has_mention = bool(mention_pattern.search(review.get("body") or ""))
    if not has_mention:
        return None

    thread_jid = f"{repo_jid}#pr:{pr['number']}"
    content = (
        f'<github_event type="pull_request_review" repo="{escape_xml(repo_full_name)}" '
        f'pr="#{pr["number"]}" sender="{escape_xml(sender)}" review_state="{escape_xml(review["state"])}">\n'
        f"  <pr_title>{escape_xml(pr['title'])}</pr_title>\n"
        f"  <review_body>{escape_xml(review.get('body') or '')}</review_body>\n"
        f"</github_event>"
    )

    return GitHubEvent(
        event_type="pull_request_review",
        action=action,
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        repo_jid=repo_jid,
        thread_jid=thread_jid,
        sender=sender,
        content=content,
        metadata=GitHubEventMetadata(pr_number=pr["number"], review_id=review["id"]),
    )


def _map_pr_review_comment_event(
    action: str,
    payload: dict,
    repo_jid: str,
    repo_full_name: str,
    installation_id: int,
    sender: str,
    app_slug: str,
) -> GitHubEvent | None:
    if action != "created":
        return None

    pr = payload["pull_request"]
    comment = payload["comment"]

    mention_pattern = re.compile(rf"@{re.escape(app_slug)}\b", re.IGNORECASE)
    has_mention = bool(mention_pattern.search(comment["body"]))
    if not has_mention and not comment.get("in_reply_to_id"):
        return None

    thread_jid = f"{repo_jid}#pr:{pr['number']}"
    content = (
        f'<github_event type="pull_request_review_comment" repo="{escape_xml(repo_full_name)}" '
        f'pr="#{pr["number"]}" sender="{escape_xml(sender)}" path="{escape_xml(comment["path"])}">\n'
        f"  <pr_title>{escape_xml(pr['title'])}</pr_title>\n"
        f'  <comment line="{comment.get("line") or 0}">{escape_xml(comment["body"])}</comment>\n'
        f"</github_event>"
    )

    return GitHubEvent(
        event_type="pull_request_review_comment",
        action=action,
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        repo_jid=repo_jid,
        thread_jid=thread_jid,
        sender=sender,
        content=content,
        metadata=GitHubEventMetadata(
            pr_number=pr["number"],
            comment_id=comment["id"],
            is_review_comment=True,
            path=comment["path"],
            line=comment.get("line"),
        ),
    )
