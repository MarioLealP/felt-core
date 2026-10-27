"""New mail for Felt (WP-43): HA's IMAP integration fires `imap_content` for
each new unread inbox mail; this turns one into what felt-core's
/v1/ha/email takes. Felt's own HA user isn't an admin and can't listen for
the event itself, so this integration does. No Home Assistant imports, so it
is testable on its own."""

from __future__ import annotations

from typing import Any

EVENT = "imap_content"


def _header(headers: dict[str, Any], name: str) -> str | None:
    """HA gives each header as a tuple of its values; names in any case."""
    for key, value in headers.items():
        if key.lower() == name:
            if isinstance(value, (list, tuple)):
                return str(value[0]) if value else None
            return str(value)
    return None


def payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """felt-core's body for one event, None when there's nothing to forward."""
    sender = data.get("sender")
    if not sender:
        return None
    headers = data.get("headers") or {}
    key = _header(headers, "message-id") or (
        f"{data.get('username')}:{data.get('folder')}:{data.get('uid')}"
    )
    return {
        "key": key.strip(),
        "sender": str(sender),
        "subject": str(data.get("subject") or ""),
        "text": str(data.get("text") or ""),
        "bulk": _header(headers, "list-unsubscribe") is not None,
    }
