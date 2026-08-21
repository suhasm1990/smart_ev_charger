#!/bin/bash
set -e

echo "📦 Syncing Google Pro credentials (~/.gemini) for Docker build..."
rm -rf .gemini_auth
mkdir -p .gemini_auth/antigravity-cli .gemini_auth/config

if [ -d "$HOME/.gemini" ]; then
    # Copy only necessary CLI configs and auth tokens (skips heavy browser profiles & IDE runtimes)
    cp -R "$HOME/.gemini/antigravity-cli/"* .gemini_auth/antigravity-cli/ 2>/dev/null || true
    cp -R "$HOME/.gemini/config/"* .gemini_auth/config/ 2>/dev/null || true
    cp "$HOME/.gemini/"*.json .gemini_auth/ 2>/dev/null || true
    cp "$HOME/.gemini/installation_id" .gemini_auth/ 2>/dev/null || true
    cp "$HOME/.gemini/history" .gemini_auth/ 2>/dev/null || true
    echo "✅ Successfully synced CLI credentials (~30MB total)."
else
    echo "⚠️ Warning: ~/.gemini not found on host. Make sure agy is logged in."
fi

IMAGE_TAG="${1:-smart_ev_charger:latest}"
PLATFORM="${2:-linux/amd64}"

echo "🔨 Building Docker image ($IMAGE_TAG for $PLATFORM)..."
docker buildx build --platform "$PLATFORM" -t "$IMAGE_TAG" .

echo "🎉 Build completed!"
