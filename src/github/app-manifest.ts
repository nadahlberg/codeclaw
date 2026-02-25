/**
 * GitHub App Manifest
 * Defines the App's permissions and events for the one-click setup flow.
 *
 * Note: `installation_repositories` is NOT listed in default_events because
 * GitHub delivers it automatically to all GitHub Apps — it is not a
 * subscribable webhook event and the manifest flow rejects it.
 *
 * When the webhook URL is localhost (or any non-public address), we omit
 * `hook_attributes` from the server-side manifest. The setup page prompts
 * the user for a public tunnel URL and injects `hook_attributes` via
 * client-side JS before the form is submitted to GitHub.
 */

function isPublicUrl(url: string): boolean {
  try {
    const { hostname } = new URL(url);
    return hostname !== 'localhost' && hostname !== '127.0.0.1' && hostname !== '::1';
  } catch {
    return false;
  }
}

export function buildAppManifest(webhookUrl: string, appName?: string): object {
  const manifest: Record<string, unknown> = {
    name: appName || 'CodeClaw AI',
    url: 'https://github.com/nadahlberg/codeclaw',
    redirect_url: `${webhookUrl}/github/callback`,
    public: false,
    default_permissions: {
      issues: 'write',
      pull_requests: 'write',
      contents: 'write',
      checks: 'write',
      metadata: 'read',
      members: 'read',
    },
    default_events: [
      'issues',
      'issue_comment',
      'pull_request',
      'pull_request_review',
      'pull_request_review_comment',
    ],
  };

  // Only include hook_attributes when the URL is publicly reachable.
  // GitHub rejects localhost/private URLs during manifest creation.
  if (isPublicUrl(webhookUrl)) {
    manifest.hook_attributes = {
      url: `${webhookUrl}/github/webhooks`,
      active: true,
    };
  }

  return manifest;
}
