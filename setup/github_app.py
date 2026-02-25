"""GitHub App Setup (CLI).

Creates a GitHub App via the manifest flow. Runs a temporary local HTTP
server to serve the manifest form and receive the OAuth callback, then
writes credentials to .env and exits.

Usage: python -m setup.github_app --webhook-url <URL>
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from clawcode.env import read_env_file
from clawcode.logger import logger

SETUP_PORT = 23847  # Arbitrary high port unlikely to conflict
TIMEOUT_SECONDS = 5 * 60  # 5 minutes


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _is_public_url(url: str) -> bool:
    try:
        hostname = urlparse(url).hostname
        return hostname not in ("localhost", "127.0.0.1", "::1")
    except Exception:
        return False


def build_app_manifest(webhook_url: str) -> dict:
    """Build the GitHub App manifest for the one-click creation flow.

    When the webhook URL is localhost we omit hook_attributes — the setup
    page prompts the user for a public tunnel URL instead.
    """
    callback_url = f"http://localhost:{SETUP_PORT}/callback"
    manifest: dict = {
        "name": "ClawCode AI",
        "url": "https://github.com/nadahlberg/clawcode",
        "redirect_url": callback_url,
        "public": False,
        "default_permissions": {
            "issues": "write",
            "pull_requests": "write",
            "contents": "write",
            "checks": "write",
            "metadata": "read",
            "members": "read",
        },
        "default_events": [
            "issues",
            "issue_comment",
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
        ],
    }

    if _is_public_url(webhook_url):
        manifest["hook_attributes"] = {
            "url": f"{webhook_url}/github/webhooks",
            "active": True,
        }

    return manifest


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def exchange_code(code: str) -> dict:
    """Exchange a manifest creation code for app credentials."""
    resp = httpx.post(
        f"https://api.github.com/app-manifests/{code}/conversions",
        headers={"Accept": "application/vnd.github+json"},
    )
    if not resp.is_success:
        raise RuntimeError(f"GitHub API error: {resp.status_code} {resp.text}")
    return resp.json()


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------

def save_credentials(data: dict) -> None:
    """Save app credentials to ~/.config/clawcode/ and .env."""
    config_dir = Path.home() / ".config" / "clawcode"
    config_dir.mkdir(parents=True, exist_ok=True)

    pem_path = config_dir / "github-app.pem"
    pem_path.write_text(data["pem"])
    pem_path.chmod(0o600)
    logger.info("GitHub App private key saved", pem_path=str(pem_path))

    env_path = Path.cwd() / ".env"
    env_lines = (
        "\n"
        "# GitHub App (auto-configured)\n"
        f"GITHUB_APP_ID={data['id']}\n"
        f"GITHUB_WEBHOOK_SECRET={data['webhook_secret']}\n"
        f"GITHUB_PRIVATE_KEY_PATH={pem_path}\n"
    )
    with open(env_path, "a") as f:
        f.write(env_lines)

    logger.info(
        "GitHub App credentials saved to .env",
        app_id=data["id"],
        slug=data.get("slug"),
    )


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

def _setup_page_html(webhook_url: str) -> str:
    manifest = build_app_manifest(webhook_url)
    manifest_json = json.dumps(manifest).replace("'", "&#39;")

    return f"""\
<!DOCTYPE html>
<html>
<head><title>ClawCode &mdash; Create GitHub App</title>
<style>
  body {{ font-family: system-ui; max-width: 600px; margin: 40px auto; padding: 0 20px; }}
  h1 {{ color: #333; }}
  .btn {{ background: #2ea44f; color: white; border: none; padding: 12px 24px;
         font-size: 16px; border-radius: 6px; cursor: pointer; }}
  .btn:hover {{ background: #2c974b; }}
  code {{ background: #f6f8fa; padding: 2px 6px; border-radius: 3px; }}
</style>
</head>
<body>
  <h1>ClawCode &mdash; Create GitHub App</h1>
  <p>Click below to create a GitHub App with the correct permissions.</p>
  <p>Webhook URL: <code>{webhook_url}/github/webhooks</code></p>
  <form action="https://github.com/settings/apps/new" method="post">
    <input type="hidden" name="manifest" value='{manifest_json}'>
    <button type="submit" class="btn">Create GitHub App</button>
  </form>
  <p><small>This will redirect you to GitHub to approve the app creation.</small></p>
</body>
</html>"""


def _success_page_html(slug: str, install_url: str) -> str:
    return f"""\
<!DOCTYPE html>
<html>
<head><title>ClawCode &mdash; Setup Complete</title>
<style>
  body {{ font-family: system-ui; max-width: 600px; margin: 40px auto; padding: 0 20px; }}
  h1 {{ color: #2ea44f; }}
  .btn {{ background: #2ea44f; color: white; border: none; padding: 12px 24px;
         font-size: 16px; border-radius: 6px; cursor: pointer; text-decoration: none;
         display: inline-block; }}
  code {{ background: #f6f8fa; padding: 2px 6px; border-radius: 3px; }}
</style>
</head>
<body>
  <h1>Setup Complete!</h1>
  <p>GitHub App <strong>{slug}</strong> has been created. Credentials saved to <code>.env</code>.</p>
  <p>Now install it on the repositories you want the bot to monitor:</p>
  <a href="{install_url}" class="btn">Install on Repositories</a>
  <p><small>You can close this tab after installing. Then restart ClawCode to load the new credentials.</small></p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Local HTTP server
# ---------------------------------------------------------------------------

class _SetupHandler(BaseHTTPRequestHandler):
    """Handles /setup and /callback for the one-shot manifest flow."""

    def __init__(self, *args, webhook_url: str, done_event: threading.Event, **kwargs):
        self.webhook_url = webhook_url
        self.done_event = done_event
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # noqa: A002
        # Silence default stderr logging from BaseHTTPRequestHandler
        pass

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/setup"):
            self._serve_setup_page()
        elif parsed.path == "/callback":
            self._handle_callback(parsed.query)
        else:
            self.send_error(404)

    def _serve_setup_page(self):
        html = _setup_page_html(self.webhook_url)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _handle_callback(self, query: str):
        params = parse_qs(query)
        code = params.get("code", [None])[0]
        if not code:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Missing code parameter")
            return

        try:
            data = exchange_code(code)
            save_credentials(data)

            install_url = f"{data['html_url']}/installations/new"
            slug = data.get("slug", f"app-{data['id']}")

            html = _success_page_html(slug, install_url)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

            logger.info(
                "GitHub App created",
                app_id=data["id"],
                slug=slug,
                install_url=install_url,
            )
            self.done_event.set()

        except Exception as exc:
            logger.error("GitHub App manifest callback failed", error=str(exc))
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Setup failed: {exc}".encode())
            self.done_event.set()


def run_setup_server(webhook_url: str) -> None:
    """Start a temporary local server, open the browser, and block until done."""
    done = threading.Event()
    handler = partial(_SetupHandler, webhook_url=webhook_url, done_event=done)
    server = HTTPServer(("127.0.0.1", SETUP_PORT), handler)

    setup_url = f"http://localhost:{SETUP_PORT}/setup"
    print(f"\nOpening browser to create GitHub App...\n  {setup_url}\n")
    webbrowser.open(setup_url)

    # Serve in a background thread so we can enforce the timeout
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    if not done.wait(timeout=TIMEOUT_SECONDS):
        logger.error("Setup timed out — no callback received within 5 minutes.")
        sys.exit(1)

    server.shutdown()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Create a GitHub App via the manifest flow")
    parser.add_argument(
        "--webhook-url",
        required=True,
        help="Public URL where GitHub will send webhooks (e.g. https://abc123.ngrok-free.app)",
    )
    args = parser.parse_args()

    # Already configured?
    env = read_env_file(["GITHUB_APP_ID"])
    if env.get("GITHUB_APP_ID"):
        logger.info(
            "GitHub App already configured",
            app_id=env["GITHUB_APP_ID"],
        )
        return

    # Validate URL
    parsed = urlparse(args.webhook_url)
    if not parsed.scheme or not parsed.hostname:
        logger.error("Invalid URL", url=args.webhook_url)
        sys.exit(1)

    run_setup_server(args.webhook_url)


if __name__ == "__main__":
    main()
