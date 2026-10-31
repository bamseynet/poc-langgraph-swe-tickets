#!/bin/sh
# Clones the checkout the investigator reads, then starts the API.
set -eu

if [ -n "${TARGET_REPO_URL:-}" ]; then
    TARGET_REPO_PATH="$(mktemp -d)/target"
    git clone --quiet --depth 1 "$TARGET_REPO_URL" "$TARGET_REPO_PATH"
    export TARGET_REPO_PATH
fi

exec uvicorn ticketgate.api:create_app --factory --host 0.0.0.0 --port 8000
