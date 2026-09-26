#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# SmartGreenAI · instalación en la Raspberry Pi 5 (backend + gateway)
#
# Uso (como tu usuario normal, NO con sudo; el script pide sudo cuando toca):
#     cd ~/Arduino
#     SMARTGREENAI_SEED_PASSWORD='Rabano2026!' bash deploy/pi5/install.sh
#
# Se puede correr las veces que quieras (por ejemplo después de un git pull):
# actualiza dependencias y reinicia los servicios, pero NO toca los .env ni
# la base de datos que ya existan.
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY="$REPO/deploy/pi5"
USER_NAME="$(id -un)"
SERVICES=(smartgreenai-backend smartgreenai-gateway)
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

paso() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
aviso() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
falla() { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

# ── Revisiones previas ────────────────────────────────────────────────
[ "$(id -u)" -ne 0 ] || falla "No lo corras con sudo ni como root: los servicios deben correr con tu usuario."
[ -f "$REPO/backend/app/main.py" ] && [ -f "$REPO/gateway/__main__.py" ] \
    || falla "No encuentro backend/ y gateway/ en $REPO. ¿Clonaste el repo completo?"
case "$REPO" in *'|'*|*' '*) falla "La ruta del repo no puede tener espacios ni '|': $REPO" ;; esac

paso "1/7 Paquetes del sistema (python3-venv, git, sqlite3)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git sqlite3

python3 - <<'PY' || falla "Se necesita Python 3.10 o más nuevo."
import sys
print(f"Python {sys.version.split()[0]}")
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

paso "2/7 Permiso para leer el Arduino (grupo dialout)"
if id -nG "$USER_NAME" | grep -qw dialout; then
    echo "$USER_NAME ya está en dialout."
else
    sudo usermod -aG dialout "$USER_NAME"
    aviso "Se agregó $USER_NAME a dialout. Para correr el gateway A MANO, cierra sesión y vuelve a entrar (el servicio no lo necesita)."
fi

paso "3/7 Backend: entorno virtual y dependencias"
[ -x "$REPO/backend/.venv/bin/python" ] || python3 -m venv "$REPO/backend/.venv"
"$REPO/backend/.venv/bin/python" -m pip install -q --upgrade pip
"$REPO/backend/.venv/bin/python" -m pip install -q -r "$REPO/backend/requirements.txt"

paso "4/7 Gateway: entorno virtual y dependencias (solo pyserial)"
[ -x "$REPO/gateway/.venv/bin/python" ] || python3 -m venv "$REPO/gateway/.venv"
"$REPO/gateway/.venv/bin/python" -m pip install -q --upgrade pip
"$REPO/gateway/.venv/bin/python" -m pip install -q -r "$REPO/gateway/requirements.txt"

paso "5/7 backend/.env (secreto JWT fijo y ruta de la base de datos)"
BACKEND_ENV="$REPO/backend/.env"
if [ -f "$BACKEND_ENV" ]; then
    echo "Ya existe; no se modifica."
else
    SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    umask 077
    cat > "$BACKEND_ENV" <<ENV
# Creado por deploy/pi5/install.sh. No se sube a git.
# Secreto fijo: las sesiones sobreviven a reinicios del servicio.
SMARTGREENAI_JWT_SECRET=$SECRET
SMARTGREENAI_DB=$REPO/backend/smartgreenai.db
ENV
    umask 022
    echo "Creado $BACKEND_ENV"
fi

paso "6/7 Datos de demo (seed) y gateway/.env"
GATEWAY_ENV="$REPO/gateway/.env"
SEED_OUT="$(mktemp)"
trap 'rm -f "$SEED_OUT"' EXIT
(
    cd "$REPO/backend"
    set -a
    # shellcheck source=/dev/null
    . "$BACKEND_ENV"
    set +a
    "$REPO/backend/.venv/bin/python" -m app.seed
) | tee "$SEED_OUT"
API_KEY="$(awk '/API Key/ {getline; gsub(/^[ \t]+|[ \t\r]+$/, ""); print; exit}' "$SEED_OUT")"

if [ ! -f "$GATEWAY_ENV" ]; then
    cp "$REPO/gateway/.env.example" "$GATEWAY_ENV"
    chmod 600 "$GATEWAY_ENV"
    echo "Creado $GATEWAY_ENV a partir de .env.example"
fi
if grep -Eq '^SG_API_KEY=.+' "$GATEWAY_ENV"; then
    echo "gateway/.env ya tiene SG_API_KEY; no se modifica."
elif [ -n "$API_KEY" ]; then
    sed -i "s|^SG_API_KEY=.*|SG_API_KEY=$API_KEY|" "$GATEWAY_ENV"
    echo "La API key del seed se guardó en gateway/.env."
else
    aviso "gateway/.env no tiene SG_API_KEY y el gateway ya existía en la BD (el seed no vuelve a mostrar la key)."
    aviso "Pega la key que guardaste en gateway/.env, o sigue 'Perdí la API key' en deploy/pi5/README.md."
fi

paso "7/7 Servicios systemd (arrancan solos al prender la Pi)"
for svc in "${SERVICES[@]}"; do
    sed -e "s|@USER@|$USER_NAME|g" -e "s|@REPO@|$REPO|g" "$DEPLOY/$svc.service" \
        | sudo tee "$SYSTEMD_DIR/$svc.service" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICES[@]}"
sudo systemctl restart "${SERVICES[@]}"
sleep 3
for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$svc"; then
        echo "  $svc: activo"
    else
        aviso "$svc no arrancó. Revisa: journalctl -u $svc -n 50"
    fi
done

# ── Resumen ───────────────────────────────────────────────────────────
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "════════════════════════════════════════════════════════════════"
echo " Listo. Abre desde la laptop (misma red):"
echo "   http://${IP:-<ip-de-la-pi>}:8000        o   http://$(hostname).local:8000"
echo
ARDUINO="$(find /dev/serial/by-id/ -mindepth 1 -print -quit 2>/dev/null || true)"
if [ -n "$ARDUINO" ]; then
    echo " Arduino detectado: $ARDUINO"
else
    aviso "No veo el Arduino en /dev/serial/by-id/. Conéctalo por USB; el gateway lo buscará solo."
fi
if timedatectl show --property=NTPSynchronized --value 2>/dev/null | grep -qx yes; then
    echo " Reloj sincronizado por internet: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
else
    aviso "El reloj aún NO está sincronizado (¿hay internet?). Las lecturas esperan su hora en el buffer."
fi
echo
echo " Estado del gateway:  cd $REPO && gateway/.venv/bin/python -m gateway status"
echo " Logs en vivo:        journalctl -u smartgreenai-gateway -f"
echo "════════════════════════════════════════════════════════════════"
