/**
 * GitHub App Setup (CLI)
 *
 * Creates a GitHub App via the manifest flow without exposing the setup
 * on the main webhook server. Runs a temporary local HTTP server to
 * serve the manifest form and receive the OAuth callback, then writes
 * credentials to .env and exits.
 *
 * Usage: npx tsx setup/index.ts --step github-app [-- --webhook-url URL]
 */
import { execSync } from 'child_process';
import fs from 'fs';
import http from 'http';
import os from 'os';
import path from 'path';

import { buildAppManifest } from '../src/github/app-manifest.js';
import { logger } from '../src/logger.js';
import { readEnvFile } from '../src/env.js';
import { emitStatus } from './status.js';

const SETUP_PORT = 23847; // Arbitrary high port unlikely to conflict

function openBrowser(url: string): void {
  const platform = os.platform();
  try {
    if (platform === 'darwin') {
      execSync(`open ${JSON.stringify(url)}`);
    } else if (platform === 'win32') {
      execSync(`start "" ${JSON.stringify(url)}`);
    } else {
      execSync(`xdg-open ${JSON.stringify(url)} 2>/dev/null || sensible-browser ${JSON.stringify(url)} 2>/dev/null`);
    }
  } catch {
    // Browser open failed — user will need to navigate manually
  }
}

async function exchangeCode(code: string): Promise<{
  id: number;
  slug: string;
  pem: string;
  webhook_secret: string;
  html_url: string;
}> {
  const response = await fetch(`https://api.github.com/app-manifests/${code}/conversions`, {
    method: 'POST',
    headers: { Accept: 'application/vnd.github+json' },
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`GitHub API error: ${response.status} ${body}`);
  }

  return response.json() as Promise<{
    id: number;
    slug: string;
    pem: string;
    webhook_secret: string;
    html_url: string;
  }>;
}

function saveCredentials(data: {
  id: number;
  slug: string;
  pem: string;
  webhook_secret: string;
}): void {
  const configDir = path.join(os.homedir(), '.config', 'codeclaw');
  fs.mkdirSync(configDir, { recursive: true });
  const pemPath = path.join(configDir, 'github-app.pem');
  fs.writeFileSync(pemPath, data.pem, { mode: 0o600 });
  logger.info({ pemPath }, 'GitHub App private key saved');

  const envPath = path.join(process.cwd(), '.env');
  const envLines = [
    '',
    '# GitHub App (auto-configured)',
    `GITHUB_APP_ID=${data.id}`,
    `GITHUB_WEBHOOK_SECRET=${data.webhook_secret}`,
    `GITHUB_PRIVATE_KEY_PATH=${pemPath}`,
    '',
  ].join('\n');

  fs.appendFileSync(envPath, envLines);
  logger.info({ appId: data.id, slug: data.slug }, 'GitHub App credentials saved to .env');
}

function buildSetupPageHtml(webhookUrl: string): string {
  const callbackUrl = `http://localhost:${SETUP_PORT}/callback`;
  const manifest = buildAppManifest(webhookUrl);
  // Override redirect_url to point to our local setup server
  (manifest as Record<string, unknown>).redirect_url = callbackUrl;
  const manifestJson = JSON.stringify(manifest);

  return `<!DOCTYPE html>
<html>
<head><title>CodeClaw &mdash; Create GitHub App</title>
<style>
  body { font-family: system-ui; max-width: 600px; margin: 40px auto; padding: 0 20px; }
  h1 { color: #333; }
  .btn { background: #2ea44f; color: white; border: none; padding: 12px 24px;
         font-size: 16px; border-radius: 6px; cursor: pointer; }
  .btn:hover { background: #2c974b; }
  code { background: #f6f8fa; padding: 2px 6px; border-radius: 3px; }
  .note { background: #fff8c5; border: 1px solid #d4a72c; border-radius: 6px;
          padding: 12px 16px; margin: 16px 0; font-size: 14px; }
</style>
</head>
<body>
  <h1>CodeClaw &mdash; Create GitHub App</h1>
  <p>Click below to create a GitHub App with the correct permissions.</p>
  <p>Webhook URL: <code>${webhookUrl}/github/webhooks</code></p>
  <form action="https://github.com/settings/apps/new" method="post">
    <input type="hidden" name="manifest" value='${manifestJson.replace(/'/g, '&#39;')}'>
    <button type="submit" class="btn">Create GitHub App</button>
  </form>
  <p><small>This will redirect you to GitHub to approve the app creation.</small></p>
</body>
</html>`;
}

function buildSuccessHtml(slug: string, installUrl: string): string {
  return `<!DOCTYPE html>
<html>
<head><title>CodeClaw &mdash; Setup Complete</title>
<style>
  body { font-family: system-ui; max-width: 600px; margin: 40px auto; padding: 0 20px; }
  h1 { color: #2ea44f; }
  .btn { background: #2ea44f; color: white; border: none; padding: 12px 24px;
         font-size: 16px; border-radius: 6px; cursor: pointer; text-decoration: none;
         display: inline-block; }
  code { background: #f6f8fa; padding: 2px 6px; border-radius: 3px; }
</style>
</head>
<body>
  <h1>Setup Complete!</h1>
  <p>GitHub App <strong>${slug}</strong> has been created. Credentials saved to <code>.env</code>.</p>
  <p>Now install it on the repositories you want the bot to monitor:</p>
  <a href="${installUrl}" class="btn">Install on Repositories</a>
  <p><small>You can close this tab after installing. Then restart CodeClaw to load the new credentials.</small></p>
</body>
</html>`;
}

export async function run(args: string[]): Promise<void> {
  // Check if already configured
  const env = readEnvFile(['GITHUB_APP_ID']);
  if (env.GITHUB_APP_ID) {
    emitStatus('GITHUB_APP', {
      STATUS: 'already_configured',
      APP_ID: env.GITHUB_APP_ID,
    });
    return;
  }

  // Get webhook URL from args or fail
  const urlIdx = args.indexOf('--webhook-url');
  const webhookUrl = urlIdx !== -1 ? args[urlIdx + 1] : undefined;

  if (!webhookUrl) {
    emitStatus('GITHUB_APP', {
      STATUS: 'failed',
      ERROR: 'Missing --webhook-url. Provide the public URL where GitHub will send webhooks (e.g. https://abc123.ngrok-free.app).',
    });
    process.exit(1);
  }

  // Validate URL
  try {
    new URL(webhookUrl);
  } catch {
    emitStatus('GITHUB_APP', {
      STATUS: 'failed',
      ERROR: `Invalid URL: ${webhookUrl}`,
    });
    process.exit(1);
  }

  // Start temporary local server
  const html = buildSetupPageHtml(webhookUrl);

  await new Promise<void>((resolve, reject) => {
    const server = http.createServer(async (req, res) => {
      const url = new URL(req.url || '/', `http://localhost:${SETUP_PORT}`);

      if (url.pathname === '/' || url.pathname === '/setup') {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(html);
        return;
      }

      if (url.pathname === '/callback') {
        const code = url.searchParams.get('code');
        if (!code) {
          res.writeHead(400, { 'Content-Type': 'text/plain' });
          res.end('Missing code parameter');
          return;
        }

        try {
          const data = await exchangeCode(code);
          saveCredentials(data);

          const installUrl = `${data.html_url}/installations/new`;
          res.writeHead(200, { 'Content-Type': 'text/html' });
          res.end(buildSuccessHtml(data.slug, installUrl));

          emitStatus('GITHUB_APP', {
            STATUS: 'ok',
            APP_ID: data.id,
            SLUG: data.slug,
            INSTALL_URL: installUrl,
          });

          // Give the browser a moment to receive the response, then shut down
          setTimeout(() => {
            server.close();
            resolve();
          }, 1000);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          logger.error({ err }, 'GitHub App manifest callback failed');
          res.writeHead(500, { 'Content-Type': 'text/plain' });
          res.end(`Setup failed: ${message}`);
          server.close();
          reject(err);
        }
        return;
      }

      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not found');
    });

    server.listen(SETUP_PORT, '127.0.0.1', () => {
      const setupUrl = `http://localhost:${SETUP_PORT}/setup`;
      console.log(`\nOpening browser to create GitHub App...\n  ${setupUrl}\n`);
      openBrowser(setupUrl);
    });

    // Timeout after 5 minutes
    setTimeout(() => {
      server.close();
      reject(new Error('Setup timed out — no callback received within 5 minutes.'));
    }, 5 * 60 * 1000);
  });
}
