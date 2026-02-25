/**
 * Webhook Server
 * HTTP server for receiving GitHub webhooks.
 * Uses Node.js built-in http module (no framework).
 */
import http from 'http';
import crypto from 'crypto';

import { logger } from './logger.js';

export interface WebhookServerOpts {
  port: number;
  webhookSecret: string;
  onEvent: (eventName: string, deliveryId: string, payload: Record<string, unknown>) => void;
}

export function startWebhookServer(opts: WebhookServerOpts): http.Server {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url || '/', `http://${req.headers.host}`);

    // Health check
    if (req.method === 'GET' && url.pathname === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok' }));
      return;
    }

    // Webhook endpoint
    if (req.method === 'POST' && url.pathname === '/github/webhooks') {
      const chunks: Buffer[] = [];
      req.on('data', (chunk: Buffer) => chunks.push(chunk));
      req.on('end', () => {
        const rawBody = Buffer.concat(chunks);
        const signature = req.headers['x-hub-signature-256'] as string;
        const eventName = req.headers['x-github-event'] as string;
        const deliveryId = req.headers['x-github-delivery'] as string;

        if (!signature || !eventName || !deliveryId) {
          res.writeHead(400, { 'Content-Type': 'text/plain' });
          res.end('Missing required headers');
          return;
        }

        // Verify webhook signature
        if (!verifySignature(rawBody, signature, opts.webhookSecret)) {
          logger.warn({ deliveryId }, 'Invalid webhook signature');
          res.writeHead(401, { 'Content-Type': 'text/plain' });
          res.end('Invalid signature');
          return;
        }

        try {
          const payload = JSON.parse(rawBody.toString()) as Record<string, unknown>;
          // Respond immediately, process asynchronously
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ received: true }));

          opts.onEvent(eventName, deliveryId, payload);
        } catch (err) {
          logger.error({ err, deliveryId }, 'Failed to parse webhook payload');
          res.writeHead(400, { 'Content-Type': 'text/plain' });
          res.end('Invalid JSON');
        }
      });
      return;
    }

    // 404 for everything else
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
  });

  server.listen(opts.port, () => {
    logger.info({ port: opts.port }, 'Webhook server listening');
  });

  return server;
}

function verifySignature(rawBody: Buffer, signature: string, secret: string): boolean {
  const expected = 'sha256=' + crypto
    .createHmac('sha256', secret)
    .update(rawBody)
    .digest('hex');
  const sigBuf = Buffer.from(signature);
  const expBuf = Buffer.from(expected);
  if (sigBuf.length !== expBuf.length) return false;
  return crypto.timingSafeEqual(sigBuf, expBuf);
}
