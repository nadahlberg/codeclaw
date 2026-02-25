/**
 * Setup Handler
 * Generates HTML pages for the GitHub App Manifest setup flow.
 */
import fs from 'fs';
import os from 'os';
import path from 'path';

import { buildAppManifest } from './app-manifest.js';
import { logger } from '../logger.js';
import { readEnvFile } from '../env.js';

/**
 * Generate the setup page HTML with the manifest form.
 * Returns null if setup is already complete.
 */
export function getSetupPageHtml(webhookUrl: string): string | null {
  // Check if already configured
  const env = readEnvFile(['GITHUB_APP_ID']);
  if (env.GITHUB_APP_ID) return null;

  const manifest = buildAppManifest(webhookUrl);
  const manifestJson = JSON.stringify(manifest);

  const isLocalhost = /^https?:\/\/(localhost|127\.0\.0\.1|::1)(:|\/|$)/.test(webhookUrl);
  const localhostNote = isLocalhost
    ? `<div class="note">
        <strong>Running on localhost</strong> &mdash; the GitHub App will be created
        without a webhook URL since GitHub cannot reach localhost. After creating
        the App, configure a public URL (e.g. via <code>ngrok</code>,
        Tailscale Funnel, or a cloud deploy) and set the <code>WEBHOOK_URL</code>
        environment variable, then update the webhook URL in your
        <a href="https://github.com/settings/apps">GitHub App settings</a>.
      </div>`
    : '';

  return `<!DOCTYPE html>
<html>
<head><title>CodeClaw Setup</title>
<style>
  body { font-family: system-ui; max-width: 600px; margin: 40px auto; padding: 0 20px; }
  h1 { color: #333; }
  .btn { background: #2ea44f; color: white; border: none; padding: 12px 24px; font-size: 16px; border-radius: 6px; cursor: pointer; }
  .btn:hover { background: #2c974b; }
  code { background: #f6f8fa; padding: 2px 6px; border-radius: 3px; }
  .note { background: #fff8c5; border: 1px solid #d4a72c; border-radius: 6px; padding: 12px 16px; margin: 16px 0; font-size: 14px; }
</style>
</head>
<body>
  <h1>CodeClaw Setup</h1>
  <p>Click below to create a GitHub App with the correct permissions and webhook configuration.</p>
  ${localhostNote}
  <form action="https://github.com/settings/apps/new" method="post">
    <input type="hidden" name="manifest" value='${manifestJson.replace(/'/g, '&#39;')}'>
    <button type="submit" class="btn">Create GitHub App</button>
  </form>
  <p><small>This will redirect you to GitHub to approve the app creation.</small></p>
</body>
</html>`;
}

/**
 * Handle the callback after GitHub creates the app from the manifest.
 * Exchanges the temp code for app credentials and stores them.
 */
export async function handleManifestCallback(code: string): Promise<string> {
  // Exchange code for app credentials
  const response = await fetch(`https://api.github.com/app-manifests/${code}/conversions`, {
    method: 'POST',
    headers: { Accept: 'application/vnd.github+json' },
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`GitHub API error: ${response.status} ${body}`);
  }

  const data = await response.json() as {
    id: number;
    slug: string;
    pem: string;
    webhook_secret: string;
    client_id: string;
    client_secret: string;
    html_url: string;
  };

  // Store private key
  const configDir = path.join(os.homedir(), '.config', 'codeclaw');
  fs.mkdirSync(configDir, { recursive: true });
  const pemPath = path.join(configDir, 'github-app.pem');
  fs.writeFileSync(pemPath, data.pem, { mode: 0o600 });
  logger.info({ pemPath }, 'GitHub App private key saved');

  // Append to .env
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

  const installUrl = `${data.html_url}/installations/new`;
  return `<!DOCTYPE html>
<html>
<head><title>CodeClaw Setup Complete</title>
<style>
  body { font-family: system-ui; max-width: 600px; margin: 40px auto; padding: 0 20px; }
  h1 { color: #2ea44f; }
  .btn { background: #2ea44f; color: white; border: none; padding: 12px 24px; font-size: 16px; border-radius: 6px; cursor: pointer; text-decoration: none; display: inline-block; }
  code { background: #f6f8fa; padding: 2px 6px; border-radius: 3px; }
</style>
</head>
<body>
  <h1>Setup Complete!</h1>
  <p>GitHub App <strong>${data.slug}</strong> has been created.</p>
  <p>Now install it on the repositories you want the bot to monitor:</p>
  <a href="${installUrl}" class="btn">Install on Repositories</a>
  <p><small>After installing, restart CodeClaw to load the new credentials.</small></p>
</body>
</html>`;
}
