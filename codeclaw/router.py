from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeclaw.models import Channel, NewMessage


def escape_xml(s: str) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def format_messages(messages: list[NewMessage]) -> str:
    lines = [
        f'<message sender="{escape_xml(m.sender_name)}" time="{m.timestamp}">{escape_xml(m.content)}</message>'
        for m in messages
    ]
    return f"<messages>\n" + "\n".join(lines) + "\n</messages>"


def strip_internal_tags(text: str) -> str:
    return re.sub(r"<internal>[\s\S]*?</internal>", "", text).strip()


def format_outbound(raw_text: str) -> str:
    text = strip_internal_tags(raw_text)
    return text if text else ""


def find_channel(channels: list[Channel], jid: str) -> Channel | None:
    for c in channels:
        if c.owns_jid(jid):
            return c
    return None
