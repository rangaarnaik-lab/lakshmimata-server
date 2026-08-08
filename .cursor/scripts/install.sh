#!/usr/bin/env bash
# Idempotent Cloud Agent install for Lakshmimata server + frontend.
set -euo pipefail

SERVER_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SERVER_ROOT"

echo "[install] server root: $SERVER_ROOT"
python3 -m pip install --user -r requirements.txt

find_frontend() {
  # Prefer inside the writable workspace. Sibling ../Lakshmimata only works
  # on a laptop checkout layout — Cloud Agents cannot create /Lakshmimata.
  local candidates=(
    "${SERVER_ROOT}/Lakshmimata"
    "/workspace/Lakshmimata"
    "/opt/cursor/lakshmimata-stack/Lakshmimata"
    "${SERVER_ROOT}/../Lakshmimata"
  )
  local path
  for path in "${candidates[@]}"; do
    if [[ -f "${path}/package.json" ]]; then
      printf '%s' "$(cd "$path" && pwd)"
      return 0
    fi
  done
  return 1
}

ensure_frontend() {
  local frontend
  if frontend="$(find_frontend)"; then
    echo "[install] frontend found: $frontend" >&2
  else
    frontend="${SERVER_ROOT}/Lakshmimata"
    echo "[install] cloning frontend into $frontend" >&2
    mkdir -p "$(dirname "$frontend")"
    if [[ -d "$frontend/.git" ]]; then
      git -C "$frontend" fetch --depth 1 origin main
      git -C "$frontend" checkout -f main
      git -C "$frontend" reset --hard origin/main
    else
      git clone --depth 1 https://github.com/rangaarnaik-lab/Lakshmimata.git "$frontend" \
        || { echo "[install] frontend clone failed: $frontend" >&2; return 1; }
    fi
    if [[ ! -f "${frontend}/package.json" ]]; then
      echo "[install] frontend missing package.json at $frontend" >&2
      return 1
    fi
    frontend="$(cd "$frontend" && pwd)"
  fi
  printf '%s' "$frontend"
}

write_frontend_env() {
  local frontend="$1"
  # Lakshmimata already commits a working .env (Supabase anon + owner token).
  # Only override when Cloud Agent secrets are explicitly provided.
  if [[ -z "${VITE_SUPABASE_URL:-}" || -z "${VITE_SUPABASE_ANON_KEY:-}" ]]; then
    if [[ -f "${frontend}/.env" ]]; then
      echo "[install] using committed frontend .env (no Cloud Agent VITE_* secrets needed)" >&2
    else
      echo "[install] warning: frontend .env missing and no VITE_* secrets set" >&2
    fi
    return 0
  fi
  umask 077
  {
    printf 'VITE_SUPABASE_URL=%s\n' "$VITE_SUPABASE_URL"
    printf 'VITE_SUPABASE_ANON_KEY=%s\n' "$VITE_SUPABASE_ANON_KEY"
    if [[ -n "${VITE_OWNER_UPSTOX_TOKEN:-}" ]]; then
      printf 'VITE_OWNER_UPSTOX_TOKEN=%s\n' "$VITE_OWNER_UPSTOX_TOKEN"
    fi
  } > "${frontend}/.env.local"
  echo "[install] wrote ${frontend}/.env.local from secrets" >&2
}

install_frontend() {
  local frontend="$1"
  if [[ -z "$frontend" || ! -f "${frontend}/package.json" ]]; then
    echo "[install] refuse npm: invalid frontend root '$frontend'" >&2
    return 1
  fi
  cd "$frontend"
  # Repo may ship a non-executable node_modules/.bin; always reinstall cleanly.
  rm -rf node_modules
  # Prefer npm ci; fall back to npm install when lockfile drifts from package.json
  # (common across Cloud Agent vs laptop npm versions / partial lock updates).
  if [[ -f package-lock.json ]]; then
    npm ci || {
      echo "[install] npm ci failed (lock drift?) — falling back to npm install" >&2
      npm install
    }
  else
    npm install
  fi
  # Belt-and-suspenders for executable bins after odd filesystem restores.
  if [[ -d node_modules/.bin ]]; then
    chmod -R u+x node_modules/.bin || true
  fi
  write_frontend_env "$frontend"
  echo "[install] frontend dependencies ready" >&2
}

FRONTEND_ROOT="$(ensure_frontend)"
install_frontend "$FRONTEND_ROOT"
cd "$SERVER_ROOT"
echo "[install] complete (server + frontend)"
