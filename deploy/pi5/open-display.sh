#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# SmartGreenAI · abre el dashboard en modo kiosko para el display de la Pi
#
# Espera a que el backend responda en http://127.0.0.1:8000/health, lee
# SMARTGREENAI_DISPLAY_TOKEN de backend/.env y lanza Chromium en pantalla
# completa apuntando a display.html sin pedir inicio de sesión.
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_ENV="$REPO/backend/.env"

TOKEN=""
if [ -f "$BACKEND_ENV" ]; then
    TOKEN="$(sed -n 's/^SMARTGREENAI_DISPLAY_TOKEN=//p' "$BACKEND_ENV" | tr -d '[:space:]\r' | head -n1)"
fi

if [ -z "$TOKEN" ]; then
    echo "Falta SMARTGREENAI_DISPLAY_TOKEN en $BACKEND_ENV. Corre bash deploy/pi5/install.sh primero." >&2
    exit 1
fi

for _ in $(seq 1 60); do
    if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

BROWSER=""
for candidate in chromium chromium-browser google-chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
        BROWSER="$candidate"
        break
    fi
done

if [ -z "$BROWSER" ]; then
    echo "No se encontró Chromium en la Raspberry Pi." >&2
    exit 1
fi

URL="http://localhost:8000/display.html#kiosk=${TOKEN}"
exec "$BROWSER" \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --check-for-update-interval=31536000 \
    "$URL"
