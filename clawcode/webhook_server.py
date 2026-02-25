"""Webhook Server.

FastAPI server for receiving GitHub webhooks.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from fastapi import FastAPI, Request, Response

from clawcode.logger import logger

OnEventCallback = Callable[[str, str, dict], None]


def create_app(webhook_secret: str, on_event: OnEventCallback) -> FastAPI:
    """Create the FastAPI app with webhook endpoint."""

    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/github/webhooks")
    async def github_webhook(request: Request):
        raw_body = await request.body()
        signature = request.headers.get("x-hub-signature-256")
        event_name = request.headers.get("x-github-event")
        delivery_id = request.headers.get("x-github-delivery")

        if not signature or not event_name or not delivery_id:
            return Response(content="Missing required headers", status_code=400)

        if not _verify_signature(raw_body, signature, webhook_secret):
            logger.warning("Invalid webhook signature", delivery_id=delivery_id)
            return Response(content="Invalid signature", status_code=401)

        try:
            import json

            payload = json.loads(raw_body)
        except Exception:
            logger.error("Failed to parse webhook payload", delivery_id=delivery_id)
            return Response(content="Invalid JSON", status_code=400)

        # Respond immediately, process asynchronously
        on_event(event_name, delivery_id, payload)
        return {"received": True}

    return app


def _verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)
