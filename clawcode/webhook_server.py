"""Webhook Server.

FastAPI server for receiving GitHub webhooks.

The app is created immediately so uvicorn can bind the port and respond to
health checks while the rest of the system initializes.  Call
``mark_ready(app, webhook_secret, on_event)`` once initialization is complete
to enable webhook processing.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from fastapi import FastAPI, Request, Response

from clawcode.logger import logger

OnEventCallback = Callable[[str, str, dict], None]


def create_app() -> FastAPI:
    """Create the FastAPI app.

    The ``/health`` endpoint responds immediately.  The ``/github/webhooks``
    endpoint returns 503 until ``mark_ready`` is called.
    """

    app = FastAPI(docs_url=None, redoc_url=None)
    app.state.ready = False
    app.state.webhook_secret = ""
    app.state.on_event: OnEventCallback | None = None

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/github/webhooks")
    async def github_webhook(request: Request):
        if not app.state.ready:
            return Response(content="Server initializing", status_code=503)

        raw_body = await request.body()
        signature = request.headers.get("x-hub-signature-256")
        event_name = request.headers.get("x-github-event")
        delivery_id = request.headers.get("x-github-delivery")

        if not signature or not event_name or not delivery_id:
            return Response(content="Missing required headers", status_code=400)

        if not _verify_signature(raw_body, signature, app.state.webhook_secret):
            logger.warning("Invalid webhook signature", delivery_id=delivery_id)
            return Response(content="Invalid signature", status_code=401)

        try:
            import json

            payload = json.loads(raw_body)
        except Exception:
            logger.error("Failed to parse webhook payload", delivery_id=delivery_id)
            return Response(content="Invalid JSON", status_code=400)

        # Respond immediately, process asynchronously
        app.state.on_event(event_name, delivery_id, payload)
        return {"received": True}

    return app


def mark_ready(app: FastAPI, webhook_secret: str, on_event: OnEventCallback) -> None:
    """Enable webhook processing after initialization is complete."""
    app.state.webhook_secret = webhook_secret
    app.state.on_event = on_event
    app.state.ready = True


def _verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)
