#!/bin/bash
# Render dashboard ka "Build Command" field single-line hai — multi-line
# command paste karne se saari lines bina separator ke chipak jaati hain
# (isi wajah se pehle "no such option: -o" wala error aaya tha). Isliye
# ye poora build logic ek script file me daal diya — Build Command me ab
# sirf "chmod +x build.sh && ./build.sh" likhna hai.
set -e

pip install -r requirements.txt

TS_TARBALL=$(curl -fsSL "https://pkgs.tailscale.com/stable/?mode=json" | python3 -c "import sys,json; print(json.load(sys.stdin)['Tarballs']['amd64'])")
curl -fsSL "https://pkgs.tailscale.com/stable/${TS_TARBALL}" -o ts.tgz
tar -xzf ts.tgz
mv tailscale_*_amd64/tailscale tailscale_*_amd64/tailscaled .
chmod +x tailscale tailscaled
chmod +x start.sh

echo "[build.sh] Build complete."
