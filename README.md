# Phoenix 🔥

Automated recovery service for Jupiter VPS. Runs on Sol, responds to Sentry outage webhooks, and orchestrates a full recovery sequence if the outage isn't self-resolving.

## Recovery sequence

```
Sentry webhook (triggered)
        │
        ▼
  10-minute grace period
  (cancelled immediately if Sentry sends "resolved")
        │
        ▼
  1. Linode API → reboot Jupiter
        │
        ▼
  2. Poll SSH until port 22 open
        │
        ▼
  3. SSH → systemctl restart docker
        │
        ▼
  4. Poll SSH → docker info until healthy
        │
        ▼
  5. Komodo API → trigger procedure
        │
        ▼
  6. SSH → docker exec swag horizon:clear
         → docker exec swag horizon:start
        │
        ▼
  Notify: recovery complete ✅
```

Each stage sends a notification (Slack and/or ntfy) so you have a full audit trail.

## Networking

Phoenix uses **Tailscale** to communicate with Jupiter. Both are on the `cronx.net` tailnet. Phoenix joins as hostname `phoenix` and reaches Jupiter via MagicDNS (`jupiter`).

This means:
- No SSH keys or ports need to be exposed publicly
- The Komodo API call goes over the tailnet
- Even if Jupiter's public IP changes after reboot, recovery still works

## Setup

### 1. SSH key for Phoenix → Jupiter

Generate a dedicated key (don't reuse your personal key):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/phoenix_ed25519 -C "phoenix@sol" -N ""
```

Add the public key to Jupiter's `authorized_keys`:

```bash
ssh-copy-id -i ~/.ssh/phoenix_ed25519.pub root@jupiter
```

Update `docker-compose.yml` to point at this key path.

### 2. Tailscale auth key

1. Go to https://login.tailscale.com/admin/settings/keys
2. Create a reusable, pre-authorised key
3. Optionally tag it: `tag:phoenix`
4. Set `TS_AUTHKEY` in `.env`

### 3. Linode token & instance ID

```bash
# Find Jupiter's instance ID
linode-cli linodes list

# Or via API
curl -H "Authorization: Bearer $LINODE_TOKEN" \
  https://api.linode.com/v4/linode/instances
```

### 4. Komodo procedure

In Komodo, create a procedure that brings up Jupiter's core stack (databases, etc.) post-reboot. Note its ID and set `KOMODO_PROCEDURE_ID`.

The Komodo API key lives in your Komodo user settings.

### 5. Sentry webhook

In Sentry (your self-hosted instance):

1. Go to **Settings → Integrations → Webhooks**
2. Add a webhook pointing to `http://phoenix:8000/webhook/sentry`
   (reachable internally on Sol's Docker network — no public exposure needed)
3. Under webhook settings, add a custom header: `X-Sentry-Hook-Signature: <your WEBHOOK_SECRET>`
4. Create an **Alert Rule** for your Spark project:
   - Condition: `Number of errors > threshold`
   - Action: Send to your webhook integration
   - Ensure both `triggered` and `resolved` events are sent

### 6. Configure .env and deploy

```bash
cp .env.example .env
# Edit .env with your values

docker compose up -d
docker compose logs -f phoenix
```

## Notifications

Phoenix supports two channels simultaneously:

| Channel | Config var | Notes |
|---------|-----------|-------|
| Slack   | `SLACK_WEBHOOK_URL` | Incoming webhook from Slack app |
| ntfy    | `NTFY_TOPIC_URL` | Great for mobile — use a private topic |

You'll receive notifications at:
- Grace period start (outage detected)
- Grace period cancelled (self-resolved)
- Each recovery stage
- Completion or failure

## Health endpoint

```
GET http://phoenix:8000/health
```

Returns current recovery state — useful for monitoring from Komodo or an uptime checker.

## Tuning

| Env var | Default | Notes |
|---------|---------|-------|
| `GRACE_PERIOD_MINUTES` | `10` | Wait before triggering recovery |
| `POLL_INTERVAL_SECS` | `15` | How often to check SSH/Docker |
| `WAIT_TIMEOUT_SECS` | `600` | Max wait per polling stage |
| `SWAG_CONTAINER_NAME` | `swag` | Docker container name for Spark |

## Notes

- Phoenix only runs **one recovery at a time** — duplicate Sentry webhooks are ignored while a recovery is in progress or a grace period is active.
- If Phoenix itself restarts mid-recovery, the task is lost — a new Sentry alert would be needed to re-trigger. This is intentional to avoid runaway loops.
- The Linode reboot waits 30 seconds after triggering before polling SSH, to give the hypervisor time to actually shut the instance down first.
