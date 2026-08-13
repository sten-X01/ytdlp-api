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
    --socket=/tmp/tailscaled.sock \
    --socks5-server=127.0.0.1:1055 \
    --outbound-http-proxy-listen=127.0.0.1:1055 \
    --state=/tmp/tailscaled.state \
    --statedir=/tmp \
    &

  # tailscaled ka socket file banne tak wait karo (max ~15s), fixed
  # sleep se better hai kyunki startup time thoda vary karta hai
  for i in $(seq 1 15); do
    [ -S /tmp/tailscaled.sock ] && break
    sleep 1
  done

  echo "[start.sh] Connecting to tailnet..."
  # NOTE: --exit-node yahan jaan-boojh kar NAHI use kiya — Render jaise
  # userspace-networking containers me "exit node" feature Tailscale ka
  # ek known/unfixed limitation hai (traffic actually route nahi hota,
  # chahe status "connected" dikhaye). Iske bajaye hum seedha phone ke
  # apne proxy server (Termux/pproxy, TS_PHONE_PROXY_PORT par) tak ek
  # do-hop chain banate hain neeche.
  ./tailscale --socket=/tmp/tailscaled.sock up \
    --authkey="$TS_AUTHKEY" \
    --hostname="ytdlp-render" \
    --accept-dns=false \
    --timeout=30s \
    || echo "[start.sh] WARNING: tailscale up fail hua — proxy ke bina chal raha hai, cookies pe hi depend karega."

  # Phone khud Termux me ek pproxy SOCKS5 server chala raha hai
  # (0.0.0.0:${TS_PHONE_PROXY_PORT:-1080}) apne Tailscale IP par.
  # Render seedha us IP tak nahi pahunch sakta (koi TUN/route nahi),
  # isliye ek chained pproxy banate hain: pehle tailscaled ke apne
  # local socks5 (127.0.0.1:1055) se tailnet me jao, phir wahan se
  # jump karke phone ke proxy tak pahuncho. Final result ek naya
  # local proxy 127.0.0.1:1090 hai jise main.py use karta hai.
  if [ -n "$TS_EXIT_NODE_IP" ]; then
    echo "[start.sh] Starting proxy chain to phone (${TS_EXIT_NODE_IP})..."
    python3 run_pproxy.py \
      -l "socks5://127.0.0.1:1090" \
      -r "socks5://127.0.0.1:1055__socks5://${TS_EXIT_NODE_IP}:${TS_PHONE_PROXY_PORT:-1080}" \
      &
  fi
else
  echo "[start.sh] TS_AUTHKEY set nahi hai — Tailscale skip, direct connection use hoga."
fi

echo "[start.sh] Starting API server..."
exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
