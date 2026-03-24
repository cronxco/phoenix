#!/bin/bash
set -e

# ── Tailscale ──────────────────────────────────────────────────────────────────
# Start tailscaled in the background
tailscaled --state=/var/lib/tailscale/tailscaled.state &
TAILSCALED_PID=$!

# Authenticate — TS_AUTHKEY must be set in environment
# Use --accept-routes so we can reach Jupiter via cronx.net MagicDNS
tailscale up \
  --authkey="${TS_AUTHKEY}" \
  --hostname="phoenix" \
  --accept-dns=true \
  --accept-routes=true \
  --reset

echo "Tailscale up — hostname: $(tailscale status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["Self"]["HostName"])')"

# ── Start Phoenix ──────────────────────────────────────────────────────────────
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level info \
  --no-access-log
