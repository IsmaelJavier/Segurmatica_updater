#!/usr/bin/env bash
#
# Instalador de Segurmática Updater (Opción A: permisos abiertos)
#
# - /opt/segurmatica y la carpeta de descargas: 0777 (todos leen/escriben/ejecutan)
# - Archivos de datos (ZIP, .sha256): 0666
# - Logs: SOLO root, vía journald (sin --log-file)
# - Timer: cada 2 horas con persistencia al encender
# - Producto: solo segav (Segurmática Antivirus)
#
# Uso:
#   sudo ./install.sh /ruta/donde/quieres/las/descargas
#
# Ejemplo:
#   sudo ./install.sh /srv/segurmatica_update
#
set -euo pipefail

# ----------------------- CONFIGURACIÓN EDITABLE -----------------------
INSTALL_DIR="/opt/segurmatica"
LOG_DIR="/var/log/segurmatica"      # solo se crea 0700 root:root; el log va al journal
SERVICE_USER="segurmatica"
SERVICE_GROUP="segurmatica"
PRODUCT="segav"                     # solo Segurmática Antivirus
TIMER_HOURS="0/2"                   # cada 2 horas (systemd: 0/2, no */2)
KEEP_COPIES="3"                     # copias a conservar por producto
# ----------------------------------------------------------------------

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ----------------------- Validaciones -----------------------
if [[ $EUID -ne 0 ]]; then
    error "Este script debe ejecutarse como root (usa sudo)."; exit 1
fi
if [[ $# -lt 1 ]]; then
    error "Falta la ruta de descargas."
    echo "Uso: sudo $0 /ruta/de/descargas"
    exit 1
fi

DEST_DIR="$(realpath -m "$1")"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
SCRIPT_SRC="$SCRIPT_DIR/segurmatica_updater.py"
CA_SRC="$SCRIPT_DIR/segurmatica-ca.pem"

if [[ ! -f "$SCRIPT_SRC" ]]; then
    error "No se encuentra segurmatica_updater.py junto a este instalador."
    error "Coloca install.sh, segurmatica_updater.py y segurmatica-ca.pem en la misma carpeta."
    exit 1
fi
if [[ ! -f "$CA_SRC" ]]; then
    warn "No se encuentra segurmatica-ca.pem; se usará el almacén del sistema."
    CA_SRC=""
fi

info "Ruta destino de descargas: $DEST_DIR"
info "Producto a descargar: $PRODUCT"
info "Permisos: 0777/0666 en programa y descargas; logs solo root (journald)"

# ----------------------- Python -----------------------
PYTHON_BIN="/usr/bin/python3"
[[ -x "$PYTHON_BIN" ]] || { error "No se encontró $PYTHON_BIN"; exit 1; }
PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VER%%.*}"; PY_MINOR="${PY_VER##*.}"
if (( PY_MAJOR < 3 || (PY_MAJOR == 3 && PY_MINOR < 10) )); then
    error "Se requiere Python 3.10+. Detectado: $PY_VER"; exit 1
fi
info "Python detectado: $PY_VER"

# ----------------------- Usuario -----------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    info "Creando usuario de sistema: $SERVICE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
    info "Usuario $SERVICE_USER ya existe."
fi

# ----------------------- Carpetas -----------------------
info "Creando carpetas con permisos abiertos..."
install -d -o root -g root -m 0777 "$INSTALL_DIR"
install -d -o root -g root -m 0700 "$LOG_DIR"          # logs: solo root
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0777 "$DEST_DIR"

# ----------------------- Copiar archivos -----------------------
info "Copiando segurmatica_updater.py (0777)"
install -o root -g root -m 0777 "$SCRIPT_SRC" "$INSTALL_DIR/segurmatica_updater.py"

if [[ -n "$CA_SRC" ]]; then
    info "Copiando segurmatica-ca.pem (0666)"
    install -o root -g root -m 0666 "$CA_SRC" "$INSTALL_DIR/segurmatica-ca.pem"
fi

# ----------------------- Servicio (log vía journald) -----------------------
info "Creando segurmatica-update.service (sin --log-file, log al journal)"
cat > /etc/systemd/system/segurmatica-update.service <<EOF
[Unit]
Description=Descargar actualizaciones de Segurmática Antivirus
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
UMask=0000
ExecStart=$PYTHON_BIN $INSTALL_DIR/segurmatica_updater.py \\
    --product $PRODUCT \\
    --dest $DEST_DIR \\
    --keep $KEEP_COPIES \\
    --tls auto
StandardOutput=journal
StandardError=journal
Nice=10
IOSchedulingClass=idle
EOF

# ----------------------- Timer -----------------------
info "Creando segurmatica-update.timer (cada 2 horas, persistente)"
cat > /etc/systemd/system/segurmatica-update.timer <<EOF
[Unit]
Description=Ejecutar descarga de Segurmática Antivirus cada 2 horas

[Timer]
OnCalendar=*-*-* ${TIMER_HOURS}:00:00
RandomizedDelaySec=5m
Persistent=true
Unit=segurmatica-update.service

[Install]
WantedBy=timers.target
EOF

# ----------------------- Ajustes de permisos en caliente -----------------------
info "Aplicando 0777 a la carpeta de descargas y su contenido..."
chmod 0777 "$DEST_DIR"
chmod 0777 "$DEST_DIR/segav" 2>/dev/null || true
find "$DEST_DIR" -type f -exec chmod 0666 {} + 2>/dev/null || true

# ----------------------- Activar -----------------------
systemctl daemon-reload
systemctl enable --now segurmatica-update.timer

# ----------------------- Prueba -----------------------
info "Comprobando instalación (--check)..."
if sudo -u "$SERVICE_USER" "$PYTHON_BIN" "$INSTALL_DIR/segurmatica_updater.py" \
        --product "$PRODUCT" --check; then
    info "Comprobación inicial correcta."
else
    warn "La comprobación devolvió código distinto de 0."
    warn "Revisa: sudo journalctl -u segurmatica-update.service -n 50"
fi

echo
info "==================== INSTALACIÓN COMPLETA ===================="
echo -e "  Script:        ${GREEN}$INSTALL_DIR/segurmatica_updater.py${NC}  (0777)"
echo -e "  CA:            ${GREEN}$INSTALL_DIR/segurmatica-ca.pem${NC}  (0666)"
echo -e "  Descargas en:  ${GREEN}$DEST_DIR${NC}  (0777)"
echo -e "  Logs:          ${GREEN}journalctl -u segurmatica-update.service${NC}  (solo root)"
echo -e "  Timer:         ${GREEN}segurmatica-update.timer${NC} (cada 2 horas)"
echo
echo "Comandos útiles:"
echo "  systemctl list-timers segurmatica-update.timer"
echo "  systemctl status segurmatica-update.service"
echo "  sudo journalctl -u segurmatica-update.service -n 100 --no-pager"
echo "  ls -la $DEST_DIR/segav/"
echo "==============================================================="