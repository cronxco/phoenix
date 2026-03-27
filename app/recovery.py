import asyncio
import logging
import os
import socket
import time

import httpx
import paramiko

from .notifier import Notifier
from .state import RecoveryState

logger = logging.getLogger("phoenix.recovery")

# ── Config from environment ────────────────────────────────────────────────────
LINODE_TOKEN        = os.environ.get("LINODE_TOKEN", "")
LINODE_INSTANCE_ID  = os.environ.get("LINODE_INSTANCE_ID", "")       # numeric ID of jupiter

JUPITER_TAILSCALE_HOST = os.environ.get("JUPITER_TAILSCALE_HOST", "jupiter")  # tailscale hostname
JUPITER_SSH_USER       = os.environ.get("JUPITER_SSH_USER", "root")
JUPITER_SSH_KEY_PATH   = os.environ.get("JUPITER_SSH_KEY_PATH", "/secrets/id_ed25519")
JUPITER_SSH_PORT       = int(os.environ.get("JUPITER_SSH_PORT", "22"))

KOMODO_API_URL         = os.environ.get("KOMODO_API_URL", "")         # e.g. https://komodo.cronx.co
KOMODO_API_KEY         = os.environ.get("KOMODO_API_KEY", "")
KOMODO_API_SECRET      = os.environ.get("KOMODO_API_SECRET", "")
KOMODO_ACTION_ID       = os.environ.get("KOMODO_ACTION_ID", "")       # ID or name of the action

SWAG_CONTAINER_NAME    = os.environ.get("SWAG_CONTAINER_NAME", "swag")

POLL_INTERVAL_SECS     = int(os.environ.get("POLL_INTERVAL_SECS", "15"))
WAIT_TIMEOUT_SECS      = int(os.environ.get("WAIT_TIMEOUT_SECS", "600"))  # 10 min max wait per stage

_REQUIRED_ENV_VARS = [
    "LINODE_TOKEN", "LINODE_INSTANCE_ID",
    "KOMODO_API_URL", "KOMODO_API_KEY", "KOMODO_API_SECRET", "KOMODO_ACTION_ID",
]


def validate_env():
    """Check that all required environment variables are set. Call on startup."""
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


class RecoveryOrchestrator:
    def __init__(self, state: RecoveryState):
        self.state = state
        self.notifier = Notifier()

    # ── Public entry point ─────────────────────────────────────────────────────

    async def run(self):
        self.state.is_active = True
        try:
            await self._notify("🔥 *Phoenix recovery initiated* for Jupiter.")

            await self._stage("restarting_linode", self._restart_linode())
            await self._stage("waiting_for_ssh", self._wait_for_ssh())
            await self._stage("waiting_for_docker", self._wait_for_docker())

            containers_healthy = await self._stage_returning(
                "checking_containers", self._check_containers()
            )
            if not containers_healthy:
                await self._stage("restarting_docker", self._restart_docker())
                await self._stage("waiting_for_docker", self._wait_for_docker())
                await self._stage("waiting_for_komodo", self._wait_for_komodo())
                await self._stage("triggering_komodo", self._trigger_komodo())

            await self._stage("restarting_horizon", self._restart_horizon())

            self.state.set_stage("complete")
            await self._notify("✅ *Phoenix recovery complete.* Jupiter and Spark should be back online.")

        except RecoveryStepFailed as e:
            logger.error(f"Recovery step failed: {e}")
            await self._notify(
                f"❌ *Phoenix recovery failed* at stage `{self.state.stage}`:\n>{e}\n"
                "Manual intervention required."
            )
        except Exception as e:
            logger.exception("Unexpected error during recovery")
            await self._notify(
                f"❌ *Phoenix unexpected error* at stage `{self.state.stage}`:\n>{e}"
            )
        finally:
            self.state.is_active = False

    # ── Stages ─────────────────────────────────────────────────────────────────

    async def _restart_linode(self):
        logger.info(f"Rebooting Linode instance {LINODE_INSTANCE_ID}...")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.linode.com/v4/linode/instances/{LINODE_INSTANCE_ID}/reboot",
                headers={
                    "Authorization": f"Bearer {LINODE_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code not in (200, 204):
            raise RecoveryStepFailed(
                f"Linode reboot API returned {resp.status_code}: {resp.text}"
            )
        logger.info("Linode reboot triggered successfully")
        await self._notify("🔄 Linode reboot triggered. Waiting for Jupiter to come back up...")
        # Brief pause to let the reboot actually initiate before we start polling
        await asyncio.sleep(30)

    async def _wait_for_ssh(self):
        logger.info(f"Polling SSH on {JUPITER_TAILSCALE_HOST}:{JUPITER_SSH_PORT}...")
        await self._poll_until(
            self._ssh_port_open,
            label="SSH port open",
            failure_msg="Jupiter SSH did not become available within timeout",
        )
        await self._notify(f"🟢 Jupiter SSH is up ({JUPITER_TAILSCALE_HOST})")

    async def _check_containers(self) -> bool:
        """Poll until swag and redis-spark are both Up, or one is definitively stuck.

        Returns True if both containers are healthy (skip restart path).
        Returns False if one or more are stuck (trigger restart path).
        """
        targets = {"swag", "redis-spark"}
        cmd = (
            'docker ps -a --format "{{.Names}}\\t{{.Status}}"'
            ' --filter "name=swag" --filter "name=redis-spark"'
        )
        deadline = time.monotonic() + WAIT_TIMEOUT_SECS
        while time.monotonic() < deadline:
            stdout, _ = await self._ssh_run(cmd)
            statuses: dict[str, str] = {}
            for line in stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    statuses[parts[0].strip()] = parts[1].strip()

            logger.info(f"Container statuses: {statuses}")

            stuck = {
                name: status
                for name, status in statuses.items()
                if status.startswith("Restarting") or status.startswith("Exited")
            }
            if stuck:
                details = ", ".join(f"{n} ({s})" for n, s in stuck.items())
                await self._notify(
                    f"⚠️ Container(s) stuck: {details}. Restarting Docker and triggering Komodo..."
                )
                return False

            healthy = {name for name, status in statuses.items() if status.startswith("Up")}
            if targets <= healthy:
                await self._notify("✅ swag and redis-spark are running — skipping Docker restart and Komodo.")
                return True

            missing = targets - set(statuses.keys())
            starting = targets - healthy - missing
            logger.info(f"  … waiting for containers (healthy={healthy}, starting={starting}, missing={missing})")
            await asyncio.sleep(POLL_INTERVAL_SECS)

        # Timeout: treat as stuck
        await self._notify("⚠️ Container check timed out — assuming stuck. Restarting Docker and triggering Komodo...")
        return False

    async def _restart_docker(self):
        logger.info("Restarting Docker on Jupiter via SSH...")
        stdout, stderr = await self._ssh_run("sudo systemctl restart docker")
        logger.info(f"Docker restart stdout: {stdout!r} stderr: {stderr!r}")
        await self._notify("🐳 Docker restarted on Jupiter. Waiting for daemon to become healthy...")
        await asyncio.sleep(10)  # give systemd a moment

    async def _wait_for_docker(self):
        logger.info("Polling Docker daemon health...")
        await self._poll_until(
            self._docker_healthy,
            label="Docker daemon healthy",
            failure_msg="Docker daemon did not become healthy within timeout",
        )
        await self._notify("🐳 Docker daemon is healthy")

    async def _wait_for_komodo(self):
        logger.info(f"Polling Komodo API at {KOMODO_API_URL}...")
        await self._poll_until(
            self._komodo_reachable,
            label="Komodo API reachable",
            failure_msg="Komodo API did not become reachable within timeout",
        )
        await self._notify("⚙️ Komodo is reachable")

    async def _trigger_komodo(self):
        logger.info(f"Triggering Komodo action: {KOMODO_ACTION_ID}")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{KOMODO_API_URL}/execute",
                headers={
                    "X-Api-Key": KOMODO_API_KEY,
                    "X-Api-Secret": KOMODO_API_SECRET,
                    "Content-Type": "application/json",
                },
                json={"type": "RunAction", "params": {"action": KOMODO_ACTION_ID}},
                timeout=30,
            )
        if resp.status_code not in (200, 201, 202):
            raise RecoveryStepFailed(
                f"Komodo action returned {resp.status_code}: {resp.text}"
            )
        logger.info("Komodo action triggered")
        await self._notify("⚙️ Komodo action triggered. Waiting 30s for containers to settle...")
        await asyncio.sleep(30)

    async def _restart_horizon(self):
        logger.info("Restarting Laravel Horizon inside SWAG container...")

        workdir = "/srv/web/sites/spark-dev/current"

        # Step 1: clear queued jobs
        stdout, stderr = await self._ssh_run(
            f"docker exec -w {workdir} {SWAG_CONTAINER_NAME} php artisan horizon:clear"
        )
        logger.info(f"horizon:clear → {stdout!r}")
        if stderr and "error" in stderr.lower():
            raise RecoveryStepFailed(f"horizon:clear failed: {stderr}")

        await asyncio.sleep(3)

        # Step 2: start Horizon detached
        stdout, stderr = await self._ssh_run(
            f"docker exec -d -t -w {workdir} {SWAG_CONTAINER_NAME} php artisan horizon"
        )
        logger.info(f"horizon start → stdout={stdout!r} stderr={stderr!r}")

        # Step 3: check for stuck schedule mutexes
        stdout, _ = await self._ssh_run(
            f"docker exec -w {workdir} {SWAG_CONTAINER_NAME} php artisan schedule:list"
        )
        if "Has Mutex" in stdout:
            logger.info("Found stuck mutexes — running schedule:clear-cache")
            clear_out, _ = await self._ssh_run(
                f"docker exec -w {workdir} {SWAG_CONTAINER_NAME} php artisan schedule:clear-cache"
            )
            logger.info(f"schedule:clear-cache → {clear_out!r}")
        else:
            logger.info("No stuck mutexes — skipping schedule:clear-cache")

        # Step 4: verify Horizon is running
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            stdout, _ = await self._ssh_run(
                f"docker exec -w {workdir} {SWAG_CONTAINER_NAME} php artisan horizon:status"
            )
            logger.info(f"horizon:status → {stdout!r}")
            if "Horizon is running" in stdout:
                break
            await asyncio.sleep(5)
        else:
            raise RecoveryStepFailed("Horizon did not start within timeout")

        await self._notify("🌅 Horizon cleared and restarted — status confirmed running.")

    # ── SSH helpers ────────────────────────────────────────────────────────────

    def _make_ssh_client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=JUPITER_TAILSCALE_HOST,
            port=JUPITER_SSH_PORT,
            username=JUPITER_SSH_USER,
            key_filename=JUPITER_SSH_KEY_PATH,
            timeout=15,
        )
        return client

    async def _ssh_run(self, command: str) -> tuple[str, str]:
        """Run a command on Jupiter via SSH; returns (stdout, stderr)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._ssh_run_sync, command)

    def _ssh_run_sync(self, command: str) -> tuple[str, str]:
        client = self._make_ssh_client()
        try:
            _, stdout, stderr = client.exec_command(command, timeout=60)
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            return out, err
        finally:
            client.close()

    def _ssh_port_open(self) -> bool:
        try:
            with socket.create_connection(
                (JUPITER_TAILSCALE_HOST, JUPITER_SSH_PORT), timeout=5
            ):
                return True
        except OSError:
            return False

    def _komodo_reachable(self) -> bool:
        try:
            import urllib.request
            urllib.request.urlopen(KOMODO_API_URL, timeout=5)
            return True
        except Exception:
            return False

    def _docker_healthy(self) -> bool:
        try:
            stdout, stderr = self._ssh_run_sync("docker info --format '{{.ServerVersion}}'")
            return bool(stdout.strip())
        except Exception:
            return False

    # ── Polling helper ─────────────────────────────────────────────────────────

    async def _poll_until(self, check_fn, label: str, failure_msg: str):
        loop = asyncio.get_event_loop()
        deadline = time.monotonic() + WAIT_TIMEOUT_SECS
        while time.monotonic() < deadline:
            ok = await loop.run_in_executor(None, check_fn)
            if ok:
                logger.info(f"✓ {label}")
                return
            logger.info(f"  … waiting for {label}")
            await asyncio.sleep(POLL_INTERVAL_SECS)
        raise RecoveryStepFailed(failure_msg)

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _stage(self, name: str, coro):
        logger.info(f"─── Stage: {name} ───")
        self.state.set_stage(name)
        await coro

    async def _stage_returning(self, name: str, coro):
        logger.info(f"─── Stage: {name} ───")
        self.state.set_stage(name)
        return await coro

    async def _notify(self, msg: str):
        logger.info(f"[notify] {msg}")
        await self.notifier.send(msg)


class RecoveryStepFailed(Exception):
    pass
