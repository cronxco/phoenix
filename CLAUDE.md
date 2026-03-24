# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Phoenix is an automated recovery service for the Jupiter VPS. It runs on Sol, listens for Sentry outage webhooks, waits a configurable grace period, then orchestrates a multi-stage recovery: Linode reboot → SSH wait → Docker restart → Komodo procedure → Horizon restart. Notifications go to Slack and/or ntfy at each stage.

## Running locally

```bash
cp .env.example .env   # fill in values
docker compose up -d
docker compose logs -f phoenix
```

The app is a FastAPI service run via uvicorn (port 8000). Dependencies are in `requirements.txt`.

## Project layout

- `app/main.py` — FastAPI app, webhook endpoint (`/webhook/sentry`), health endpoint (`/health`), grace period logic
- `app/recovery.py` — `RecoveryOrchestrator` with the 6-stage recovery pipeline; `RecoveryStepFailed` exception
- `app/notifier.py` — `Notifier` class sending to Slack and/or ntfy concurrently
- `app/state.py` — `RecoveryState` dataclass tracking active recovery stage
- `scripts/entrypoint.sh` — starts tailscaled, authenticates, then launches uvicorn

## Architecture notes

- **Single recovery at a time**: duplicate Sentry webhooks are ignored while a recovery or grace period is active. Global `_recovery_task` in `main.py` gates this.
- **Grace period**: on a `triggered` webhook, Phoenix waits `GRACE_PERIOD_MINUTES` (default 10) before starting recovery. A `resolved` webhook cancels the asyncio task.
- **SSH via Tailscale**: all SSH to Jupiter goes over the cronx.net tailnet (MagicDNS hostname `jupiter`). The container runs tailscaled and authenticates with `TS_AUTHKEY`.
- **Blocking SSH calls**: paramiko is synchronous — `_ssh_run` wraps it in `run_in_executor` to avoid blocking the event loop.
- **No persistence**: if Phoenix restarts mid-recovery, the task is lost. A new Sentry alert is needed to re-trigger. This is intentional to prevent runaway loops.

## Key environment variables

Required: `LINODE_TOKEN`, `LINODE_INSTANCE_ID`, `KOMODO_API_URL`, `KOMODO_API_KEY`, `KOMODO_PROCEDURE_ID`, `TS_AUTHKEY`

Optional: `WEBHOOK_SECRET`, `SLACK_WEBHOOK_URL`, `NTFY_TOPIC_URL`, `GRACE_PERIOD_MINUTES`, `POLL_INTERVAL_SECS`, `WAIT_TIMEOUT_SECS`, `SWAG_CONTAINER_NAME`
