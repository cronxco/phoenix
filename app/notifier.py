import logging
import os

import httpx

logger = logging.getLogger("phoenix.notifier")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
NTFY_TOPIC_URL    = os.environ.get("NTFY_TOPIC_URL", "")    # e.g. https://ntfy.sh/your-topic


class Notifier:
    """
    Sends audit trail notifications.

    Supports:
      - Slack incoming webhook  (set SLACK_WEBHOOK_URL)
      - ntfy.sh push            (set NTFY_TOPIC_URL) — great for mobile
      - Both simultaneously

    If neither is configured, messages are logged only.
    """

    async def send(self, message: str):
        tasks = []
        if SLACK_WEBHOOK_URL:
            tasks.append(self._send_slack(message))
        if NTFY_TOPIC_URL:
            tasks.append(self._send_ntfy(message))

        if not tasks:
            logger.warning("No notification targets configured — logging only")
            return

        import asyncio
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Notification failed: {r}")

    async def _send_slack(self, message: str):
        # Strip markdown bold (*text*) — Slack uses *text* natively, so this is fine
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                SLACK_WEBHOOK_URL,
                json={"text": message},
                timeout=10,
            )
            resp.raise_for_status()

    async def _send_ntfy(self, message: str):
        # ntfy.sh: plain text POST, title from first line
        title = message.split("\n")[0][:50].strip("*_ ")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                NTFY_TOPIC_URL,
                content=message,
                headers={
                    "Title": title,
                    "Priority": "high",
                    "Tags": "phoenix,recovery",
                },
                timeout=10,
            )
            resp.raise_for_status()
