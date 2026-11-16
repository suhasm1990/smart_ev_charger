#!/bin/bash
# Build the image on this Mac and replace the charger container on the NAS.
#
#   ./deploy_nas.sh
#
# Needs passwordless ssh to the NAS (ssh-copy-id) and the NAS user in the
# docker group (sudo usermod -aG docker <user>). The image is streamed over
# ssh, so no .tar file is left on either side. .env is baked into the image,
# so run this from the repo folder that holds your .env.
set -euo pipefail

NAS="${NAS:-suhas_mallesh@10.0.0.218}"
IMAGE="smart_ev_charger"
CONTAINER="smart_ev_charger"
PORT="${DASHBOARD_PORT:-8080}"

cd "$(dirname "$0")"
[ -f .env ] || { echo "❌ No .env here: the image would start without secrets."; exit 1; }

echo "🔨 Building $IMAGE (linux/amd64)..."
docker build --platform linux/amd64 -t "$IMAGE" .

echo "📦 Sending the image to $NAS..."
docker save "$IMAGE" | gzip | ssh "$NAS" 'gunzip | docker load'

echo "🔁 Replacing the container..."
ssh "$NAS" "docker rm -f $CONTAINER >/dev/null 2>&1 || true
docker run -d --name $CONTAINER --restart unless-stopped \
  -p $PORT:$PORT --stop-timeout 20 \
  --log-opt max-size=10m --log-opt max-file=3 \
  $IMAGE >/dev/null
docker image prune -f >/dev/null
docker ps --filter name=$CONTAINER --format '✅ {{.Names}}: {{.Status}}'"
