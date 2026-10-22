#!/usr/bin/env bash
# Start a built image the way compose.yaml runs it (read-only, no capabilities, non-root user) on empty
# throwaway volumes, then check that it answers, reports the expected version and can write its database.
# Used by CI before an image is published. Never touches a running ChemoTracker or its data.
#
#   scripts/smoke_image.sh IMAGE [VERSION]
set -euo pipefail

image=${1:?usage: scripts/smoke_image.sh IMAGE [VERSION]}
want=${2:-}
name="chemo-smoke-$$"
data="chemo-smoke-data-$$"
backups="chemo-smoke-backups-$$"

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$data" "$backups" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$name" \
  --read-only --tmpfs /tmp \
  --security-opt no-new-privileges:true --cap-drop ALL \
  --user 10001:10001 \
  -v "$data:/data" -v "$backups:/backups" \
  -p 127.0.0.1::8000 \
  "$image" >/dev/null

port=$(docker port "$name" 8000/tcp | head -n 1 | sed 's/.*://')
base="http://127.0.0.1:$port"

health=""
for _ in $(seq 1 60); do
  if health=$(curl -fsS "$base/healthz" 2>/dev/null); then
    break
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != "true" ]; then
    break
  fi
  sleep 1
done
if [ -z "$health" ]; then
  echo "The container didn't answer /healthz. Its log:" >&2
  docker logs "$name" >&2 || true
  exit 1
fi
echo "healthz: $health"

if [ -n "$want" ]; then
  got=$(printf '%s' "$health" | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])')
  if [ "$got" != "$want" ]; then
    echo "Expected version $want, the image says $got." >&2
    exit 1
  fi
fi

# First run: the app answers with the setup pages, which needs the templates and static files in the image.
page=$(curl -fsSL "$base/")
if ! printf '%s' "$page" | grep -q "ChemoTracker"; then
  echo "The first page didn't render." >&2
  exit 1
fi

user=$(docker exec "$name" id -u)
if [ "$user" = "0" ]; then
  echo "The app runs as root." >&2
  exit 1
fi
docker exec "$name" sh -c 'test -s /data/chemotracker.sqlite3' || {
  echo "No database was written to /data." >&2
  exit 1
}

echo "Smoke test passed: $image"
