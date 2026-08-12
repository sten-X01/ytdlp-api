#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
#   Render startup script
#   ─────────────────────────────────────────────────────────────────
#   1. tailscaled ko "userspace-networking" mode me chalata hai — isse
#      root/TUN device ki zaroorat nahi padti (Render free container
#      me wo permission nahi milti). Ye localhost:1055 pe ek local
#      SOCKS5 proxy expose kar deta hai.
#   2. tailscale up se is Render instance ko tailnet me jodta hai, aur
#      phone (jo exit-node hai) ke through route set kar deta hai.
#   3. Phir uvicorn start hota hai — yt-dlp ke saare requests ab
#      127.0.0.1:1055 -> tailnet -> phone -> internet, is route se
#      jaate hain (main.py me PROXY_URL isi ko point karta hai).
#
#   Zaroori env vars (Render dashboard -> Environment tab):
#     TS_AUTHKEY        — Tailscale admin console -> Settings -> Keys
#                          se banaya hua auth key. Reusable + Ephemeral
#                          + Pre-authorized flags ON rakhna (taaki har
#                          restart pe naya manual approval na maangna
#                          pade aur purane dead nodes apne aap hat jayein).
#     TS_EXIT_NODE_IP   — phone ka Tailscale IP (100.x.y.z), Tailscale
#                          admin console (login.tailscale.com/admin/machines)
#                          me phone ke naam ke niche dikhega.
# ══════════════════════════════════════════════════════════════════════
set -e

if [ -n "$TS_AUTHKEY" ]; then
  echo "[start.sh] Starting tailscaled (userspace networking)..."
  ./tailscaled \
    --tun=userspace-networking \
    --socks5-server=127.0.0.1:1055 \
    --outbound-http-proxy-listen=127.0.0.1:1055 \
    --state=/tmp/tailscaled.state \
    --statedir=/tmp \
    &

  # tailscaled ko socket banane ke liye thoda time do
  sleep 3

  echo "[start.sh] Connecting to tailnet..."
  ./tailscale up \
    --authkey="$TS_AUTHKEY" \
    --hostname="ytdlp-render" \
    --exit-node="${TS_EXIT_NODE_IP:-}" \
    --exit-node-allow-lan-access=false \
    --accept-dns=false \
    --timeout=30s \
    || echo "[start.sh] WARNING: tailscale up fail hua — proxy ke bina chal raha hai, cookies pe hi depend karega."
else
  echo "[start.sh] TS_AUTHKEY set nahi hai — Tailscale skip, direct connection use hoga."
fi

echo "[start.sh] Starting API server..."
exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
