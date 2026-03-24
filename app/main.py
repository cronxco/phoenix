import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .recovery import RecoveryOrchestrator, validate_env
from .notifier import Notifier
from .state import RecoveryState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("phoenix")

# Global state — one active recovery at a time
_recovery_task: asyncio.Task | None = None
_state = RecoveryState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_env()
    logger.info("🔥 Phoenix starting up")
    await Notifier().send("🔥 *Phoenix is online* and listening for Sentry alerts.")
    yield
    logger.info("🔥 Phoenix shutting down")
    if _recovery_task and not _recovery_task.done():
        _recovery_task.cancel()


app = FastAPI(title="Phoenix", lifespan=lifespan)

SENTRY_CLIENT_SECRET = os.environ.get("SENTRY_CLIENT_SECRET", "")


def _verify_signature(body: bytes, header: str | None) -> None:
    """Verify Sentry's HMAC-SHA256 signature over the raw request body."""
    if not SENTRY_CLIENT_SECRET:
        return
    if not header:
        raise HTTPException(status_code=401, detail="Missing sentry-hook-signature")
    expected = hmac.new(SENTRY_CLIENT_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "recovery_active": _state.is_active,
        "recovery_stage": _state.stage,
        "last_trigger": _state.last_trigger.isoformat() if _state.last_trigger else None,
    }


@app.post("/webhook/sentry")
async def sentry_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    sentry_hook_signature: str | None = Header(default=None),
):
    global _recovery_task

    body = await request.body()
    _verify_signature(body, sentry_hook_signature)

    try:
        payload = json.loads(body)
    except Exception:
        logger.warning("Received webhook with invalid JSON body")
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    logger.info(f"Received Sentry webhook: action={payload.get('action')}")

    action = payload.get("action")

    # Sentry alert resolved — cancel any pending recovery
    if action == "resolved":
        if _recovery_task and not _recovery_task.done():
            logger.info("Outage resolved by Sentry — cancelling recovery countdown")
            _recovery_task.cancel()
            _state.clear()
            notifier = Notifier()
            await notifier.send("✅ *Phoenix*: Outage resolved by Sentry before recovery triggered. No action taken.")
        return JSONResponse({"status": "resolved, recovery cancelled"})

    # Sentry alert triggered
    if action == "triggered":
        if _recovery_task and not _recovery_task.done():
            logger.info("Recovery already scheduled — ignoring duplicate webhook")
            return JSONResponse({"status": "recovery already pending"})

        logger.info("Outage detected — starting 10-minute grace period")
        _state.set_triggered()

        notifier = Notifier()
        await notifier.send(
            "⚠️ *Phoenix*: Sentry outage alert received for Jupiter/Spark. "
            "Recovery will trigger in 10 minutes if not resolved."
        )

        orchestrator = RecoveryOrchestrator(state=_state)
        _recovery_task = asyncio.create_task(
            _grace_then_recover(orchestrator),
            name="phoenix-recovery",
        )
        return JSONResponse({"status": "grace period started"})

    return JSONResponse({"status": "ignored", "action": action})


async def _grace_then_recover(orchestrator: RecoveryOrchestrator):
    grace_minutes = int(os.environ.get("GRACE_PERIOD_MINUTES", "10"))
    try:
        logger.info(f"Grace period: waiting {grace_minutes} minutes...")
        await asyncio.sleep(grace_minutes * 60)
        logger.info("Grace period elapsed — beginning recovery")
        await orchestrator.run()
    except asyncio.CancelledError:
        logger.info("Recovery task cancelled (outage resolved)")
    finally:
        _state.clear()
