#!/bin/bash
set -e

IMAGE_TAG="${1:-smart_ev_charger:latest}"
PLATFORM="${2:-linux/amd64}"

echo "🔨 Building Docker image ($IMAGE_TAG for $PLATFORM)..."
docker buildx build --platform "$PLATFORM" -t "$IMAGE_TAG" .

echo "🎉 Build completed!"
