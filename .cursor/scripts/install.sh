#!/usr/bin/env bash
# Idempotent Cloud Agent install for Lakshmimata server + frontend.
set -euo pipefail

SERVER_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SERVER_ROOT"

echo "[install] server root: $SERVER_ROOT"
python3 -m pip install --user -r requirements.txt

find_frontend() {
  local candidates=(
    "${SERVER_ROOT}/../Lakshmimata"
    "${SERVER_ROOT}/Lakshmimata"
    "/workspace/Lakshmimata"
    "/opt/cursor/lakshmimata-stack/Lakshmimata"
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
    frontend="${SERVER_ROOT}/../Lakshmimata"
    echo "[install] cloning frontend into $frontend" >&2
    mkdir -p "$(dirname "$frontend")"
    if [[ -d "$frontend/.git" ]]; then
      git -C "$frontend" fetch --depth 1 origin main
      git -C "$frontend" checkout -f main
      git -C "$frontend" reset --hard origin/main
    else
      git clone --depth 1 https://github.com/rangaarnaik-lab/Lakshmimata.git "$frontend"
    fi
    frontend="$(cd "$frontend" && pwd)"
  fi
  printf '%s' "$frontend"
}

write_frontend_env() {
  local frontend="$1"
  # Prefer Cloud Agent secrets when present; otherwise leave existing .env alone.
  if [[ -z "${VITE_SUPABASE_URL:-}" || -z "${VITE_SUPABASE_ANON_KEY:-}" ]]; then
    echo "[install] VITE_SUPABASE_* secrets not set; skipping .env rewrite" >&2
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
  cd "$frontend"
  # Repo may ship a non-executable node_modules/.bin; always reinstall cleanly.
  rm -rf node_modules
  if [[ -f package-lock.json ]]; then
    npm ci
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
