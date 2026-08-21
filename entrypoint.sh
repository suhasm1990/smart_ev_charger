#!/bin/bash
set -e

REPO_URL="${REPO_URL:-https://github.com/suhasm1990/smart_ev_charger.git}"
BRANCH="${GIT_BRANCH:-main}"

# 0. Dynamically configure OS timezone from TZ environment variable (e.g. America/New_York)
TARGET_TZ="${TZ:-America/Los_Angeles}"
if [ -f "/usr/share/zoneinfo/${TARGET_TZ}" ]; then
    ln -snf "/usr/share/zoneinfo/${TARGET_TZ}" /etc/localtime
    echo "${TARGET_TZ}" > /etc/timezone
    export TZ="${TARGET_TZ}"
fi

# 1. Configure Git identity and GitHub CLI
git config --global --add safe.directory '*' || true

if [ -n "$GITHUB_TOKEN" ]; then
    export GH_TOKEN="$GITHUB_TOKEN"
    gh auth setup-git || true
    git config --global user.name "${GIT_AUTHOR_NAME:-suhasm1990}"
    git config --global user.email "${GIT_AUTHOR_EMAIL:-suhasm1990@users.noreply.github.com}"
fi

# 2. Clone or pull latest codebase into /app
cd /app

if [ ! -d "/app/.git" ]; then
    echo "📥 Cloning repository from ${REPO_URL} (branch: ${BRANCH})..."
    git clone --branch "${BRANCH}" "${REPO_URL}" /tmp/repo
    cp -a /tmp/repo/. /app/
    rm -rf /tmp/repo
else
    echo "🔄 Pulling latest changes from origin/${BRANCH}..."
    git checkout "${BRANCH}" || true
    git pull origin "${BRANCH}" || true
fi

# 3. Ensure any updated requirements are installed
if [ -f "/app/requirements.txt" ]; then
    pip install --no-cache-dir -r /app/requirements.txt || true
fi

mkdir -p /app/logs

# 4. Start the application
echo "🚀 Starting application..."
exec "$@"
