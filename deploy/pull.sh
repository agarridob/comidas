#!/usr/bin/env bash
# Despliegue por git-pull del panel comidas (en el servidor de Mealie).
# Se ejecuta EN el servidor como root:
#   ssh <servidor> /opt/comidas/deploy/pull.sh
#
# /opt/comidas es un checkout git de este repo. El .env solo vive en el servidor
# (gitignoreado) y NO se toca.
set -euo pipefail

# Todo va dentro de { … } para que bash lea el script entero antes de ejecutarlo:
# el `git pull` puede reescribir este mismo fichero a mitad de ejecución. Eso
# significa que se ejecuta la versión de ANTES del pull: si el pull trae un
# pull.sh nuevo, se relanza para que sus cambios (unidades nuevas…) se apliquen ya.
{

REPO=/opt/comidas
cd "$REPO"

echo "==> git pull"
git config --global --add safe.directory "$REPO" 2>/dev/null || true
ANTES=$(git rev-parse --short HEAD)
git pull --ff-only origin main
DESPUES=$(git rev-parse --short HEAD)
echo "    $ANTES -> $DESPUES"
if [ -z "${COMIDAS_RELANZADO:-}" ] && ! git diff --quiet "$ANTES" "$DESPUES" -- deploy/pull.sh; then
  echo "==> pull.sh ha cambiado: se relanza la versión nueva"
  COMIDAS_RELANZADO=1 exec "$REPO/deploy/pull.sh"
fi

echo "==> venv + dependencias (editable: el venv apunta al checkout)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -e . >/dev/null

echo "==> servicio + timers (borrador semanal y avisos)"
install -m 644 deploy/comidas-panel.service deploy/comidas-borrador.service deploy/comidas-borrador.timer \
  deploy/comidas-avisos.service deploy/comidas-avisos.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q comidas-panel
systemctl enable -q --now comidas-borrador.timer comidas-avisos.timer

echo "==> permisos"
chown -R www-data:www-data "$REPO"
chmod 600 "$REPO/.env"

echo "==> reiniciar servicio"
systemctl restart comidas-panel
sleep 3
systemctl is-active comidas-panel
curl -s --retry 3 --retry-delay 1 -o /dev/null -w "    panel: HTTP %{http_code}\n" http://127.0.0.1:8000/api/health
echo "==> Hecho."
exit
}
