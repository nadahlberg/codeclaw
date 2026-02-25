# Plan: Python `setup/github_app.py` CLI Module

## What we're building

A Python equivalent of the deleted `setup/github-app.ts` — a CLI script invoked as `python -m setup.github_app --webhook-url <URL>` that creates a GitHub App via the [manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest) and saves credentials locally.

## How the manifest flow works

1. A local page serves an HTML form with a JSON manifest as a hidden field, posting to `https://github.com/settings/apps/new`
2. The user approves on GitHub, which redirects back to a local callback URL with a `?code=` parameter
3. We exchange the code via `POST https://api.github.com/app-manifests/{code}/conversions` to get the app ID, slug, PEM key, and webhook secret
4. We save those credentials and show a success page with an "Install on Repositories" link

## Files to create

### 1. `setup/__init__.py`
Empty file to make `setup/` a package (required for `python -m setup.github_app`).

### 2. `setup/__main__.py`
Thin entry point that allows `python -m setup` usage. Parses `--step` if we want parity with the old TS multi-step runner, or we can keep it simple and just call `github_app.run()` directly. Recommendation: **keep it simple** — just have `python -m setup.github_app` work as a standalone module.

### 3. `setup/github_app.py`
The main module. Contains:

#### `build_app_manifest(webhook_url: str) -> dict`
Port of `src/github/app-manifest.ts:buildAppManifest`. Returns the manifest dict with:
- `name`: "ClawCode AI"
- `url`: project URL
- `redirect_url`: local callback URL
- `public`: false
- `default_permissions`: issues write, pull_requests write, contents write, checks write, metadata read, members read
- `default_events`: issues, issue_comment, pull_request, pull_request_review, pull_request_review_comment
- `hook_attributes`: only included when webhook URL is publicly reachable (not localhost/127.0.0.1/::1)

#### `exchange_code(code: str) -> dict`
POST to `https://api.github.com/app-manifests/{code}/conversions`. Returns `{id, slug, pem, webhook_secret, html_url}`. Uses `httpx` (already a project dependency).

#### `save_credentials(data: dict) -> None`
- Write PEM to `~/.config/clawcode/github-app.pem` with mode 0o600
- Append `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY_PATH` to `.env`
- This matches what `load_github_app_config()` in `clawcode/github/auth.py` already expects

#### `open_browser(url: str) -> None`
Platform-aware browser open using `webbrowser.open()` (stdlib — simpler than the TS version's subprocess calls).

#### `run_setup_server(webhook_url: str) -> None`
Spins up a temporary `http.server`-based server on `127.0.0.1:23847` with two routes:
- `GET /setup` — serves the HTML form page
- `GET /callback?code=...` — exchanges the code, saves credentials, serves success page, shuts down

Uses `asyncio` + a lightweight approach. Two options:
- **Option A**: Use `aiohttp` — but that's a new dependency.
- **Option B**: Use stdlib `http.server.HTTPServer` in a thread with an `Event` for shutdown — zero new dependencies.
- **Recommendation**: **Option B** (stdlib `http.server`). It's a one-shot CLI tool, not a production server. The TS version used Node's `http` stdlib, so this is a direct parallel.

Timeout: 5 minutes, then exit with an error.

#### `main()` (module `__main__` block)
Argument parsing with `argparse`:
- `--webhook-url` (required): the public URL
- Early-exit if `GITHUB_APP_ID` already exists in `.env` (already configured)
- Validate the URL
- Start the local server and open the browser
- Block until callback completes or timeout

## Existing code we reuse
- `clawcode.env.read_env_file()` — to check if already configured
- `clawcode.logger.logger` — for structured logging
- `httpx` — for the GitHub API call (already a dependency)

## Existing code alignment
- `clawcode/github/auth.py:load_github_app_config()` reads from `.env` and `~/.config/clawcode/github-app.pem` — our `save_credentials` writes to exactly those locations
- The webhook endpoint is `/github/webhooks` (from `webhook_server.py`) — our manifest's `hook_attributes.url` uses that path

## HTML templates
Port the two HTML pages (setup form + success) inline as f-strings, same as the TS version. Keep styling minimal.

## No new dependencies
Everything we need is already available:
- `httpx` — HTTP client (in pyproject.toml)
- `http.server` — local server (stdlib)
- `webbrowser` — browser open (stdlib)
- `argparse` — CLI args (stdlib)
- `json`, `pathlib`, `threading` — stdlib

## Summary of changes

| File | Action |
|------|--------|
| `setup/__init__.py` | Create (empty) |
| `setup/github_app.py` | Create (main module, ~200 lines) |
