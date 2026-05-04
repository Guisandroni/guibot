#!/usr/bin/env bash
# Arranca em paralelo: bot Python (landing FastAPI + Kick) + Vite (:3000 com proxy /api).
# Pré-requisitos: config.yaml com bot.landing.enabled, .env com credenciais e LANDING_API_SECRET;
# em web/: npm ci (ou npm install).
#
# O alvo do proxy é VITE_DEV_API_PROXY (por defeito http://127.0.0.1:8844 — igual ao vite.config.ts).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$ROOT/web"

cleanup() {
  if [[ -n "${BOT_PID:-}" ]] && kill -0 "$BOT_PID" 2>/dev/null; then
    echo ""
    echo "[dev-panel] A encerrar o bot (PID $BOT_PID)…"
    kill "$BOT_PID" 2>/dev/null || true
    wait "$BOT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

API_PROXY="${VITE_DEV_API_PROXY:-http://127.0.0.1:8844}"

if [[ ! -d "$WEB/node_modules" ]]; then
  echo "[dev-panel] Aviso: não há web/node_modules — corre npm ci em web/ primeiro." >&2
fi

echo "[dev-panel] A arrancar o bot em $ROOT (landing → ${API_PROXY})…"
(cd "$ROOT" && exec python3 bot.py) &
BOT_PID=$!

sleep 1

if ! kill -0 "$BOT_PID" 2>/dev/null; then
  echo "[dev-panel] Erro: o bot terminou ao iniciar. Confirma bot.landing no config.yaml e o .env." >&2
  exit 1
fi

echo "[dev-panel] A arrancar Vite — http://localhost:3000 (POST/GET /api → proxy para a landing)."
(cd "$WEB" && exec npm run dev)
