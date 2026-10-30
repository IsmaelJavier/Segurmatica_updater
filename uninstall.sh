#!/usr/bin/env bash
#
# Limpieza / desinstalación de Segurmática Updater para Ubuntu 24.04
#
# Uso:
#   sudo ./uninstall.sh                       # limpia servicio, timer, script y logs
#   sudo ./uninstall.sh /srv/segurmatica_update   # además borra la carpeta de descargas
#   sudo ./uninstall.sh --purge /srv/segurmatica_update  # + usuario y carpeta destino
#
# Opciones:
#   --purge     Elimina también el usuario 'segurmatica' y la carpeta destino.
#   --dry-run   Muestra lo que haría sin ejecutar nada.
#   -h, --help  Muestra esta ayuda.
#
set -uo pipefail   # sin -e: queremos continuar aunque algo no exista

INSTALL_DIR="/opt/segurmatica"
LOG_DIR="/var/log/segurmatica"
SERVICE_USER="segurmatica"
SERVICE_NAME="segurmatica-update"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

PURGE=0
DRY_RUN=0
DEST_DIR=""

# ----------------------- Parseo de argumentos -----------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge)   PURGE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*)
            error "Opción desconocida: $1"; exit 1 ;;
        *)
            DEST_DIR="$1"; shift ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    error "Este script debe ejecutarse como root (usa sudo)."; exit 1
fi

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

# ----------------------- 1. Parar y desactivar -----------------------
info "Deteniendo y desactivando el timer y el servicio..."
if systemctl list-unit-files --no-legend --no-pager 2>/dev/null | grep -q "^${SERVICE_NAME}.timer"; then
    run systemctl disable --now "${SERVICE_NAME}.timer" 2>/dev/null || warn "No se pudo desactivar el timer."
else
    warn "El timer ${SERVICE_NAME}.timer no existe."
fi

if systemctl list-unit-files --no-legend --no-pager 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    run systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || warn "No se pudo parar el servicio."
else
    warn "El servicio ${SERVICE_NAME}.service no existe."
fi

# ----------------------- 2. Matar procesos colgados -----------------------
info "Buscando procesos de segurmatica_updater colgados..."
PIDS="$(pgrep -f 'segurmatica_updater\.py' 2>/dev/null || true)"
if [[ -n "$PIDS" ]]; then
    warn "Procesos encontrados: $PIDS. Enviando SIGTERM..."
    run kill $PIDS 2>/dev/null || true
    sleep 2
    PIDS2="$(pgrep -f 'segurmatica_updater\.py' 2>/dev/null || true)"
    if [[ -n "$PIDS2" ]]; then
        warn "Aún vivos: $PIDS2. Enviando SIGKILL..."
        run kill -9 $PIDS2 2>/dev/null || true
    fi
else
    info "No hay procesos colgados."
fi

# ----------------------- 3. Borrar unidades systemd -----------------------
info "Eliminando unidades systemd..."
for unit in \
    "/etc/systemd/system/${SERVICE_NAME}.service" \
    "/etc/systemd/system/${SERVICE_NAME}.timer" \
    "/etc/systemd/system/${SERVICE_NAME}.service.d" \
    "/etc/systemd/system/${SERVICE_NAME}.timer.d"; do
    if [[ -e "$unit" ]]; then
        run rm -rf "$unit"
        info "  Borrado: $unit"
    fi
done

run systemctl daemon-reload || true
run systemctl reset-failed 2>/dev/null || true

# ----------------------- 4. Borrar archivos instalados -----------------------
info "Eliminando archivos del programa..."
for path in \
    "$INSTALL_DIR/segurmatica_updater.py" \
    "$INSTALL_DIR/segurmatica-ca.pem"; do
    if [[ -e "$path" ]]; then
        run rm -f "$path"
        info "  Borrado: $path"
    fi
done

# Borrar el directorio de instalación si queda vacío
if [[ -d "$INSTALL_DIR" ]] && [[ -z "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    run rmdir "$INSTALL_DIR" 2>/dev/null && info "  Borrado directorio vacío: $INSTALL_DIR"
fi

# ----------------------- 5. Logs -----------------------
info "Eliminando logs..."
if [[ -d "$LOG_DIR" ]]; then
    run rm -rf "$LOG_DIR"
    info "  Borrado: $LOG_DIR"
fi

# ----------------------- 6. Carpeta destino (opcional) -----------------------
if [[ -n "$DEST_DIR" ]]; then
    DEST_DIR="$(realpath -m "$DEST_DIR")"
    if [[ -d "$DEST_DIR" ]]; then
        warn "Se eliminará la carpeta de descargas: $DEST_DIR"
        run rm -rf "$DEST_DIR"
        info "  Borrado: $DEST_DIR"
    else
        warn "La carpeta destino no existe: $DEST_DIR"
    fi
else
    info "No se borró ninguna carpeta de descargas (pásala como argumento si quieres eliminarla)."
fi

# ----------------------- 7. Usuario (solo con --purge) -----------------------
if [[ $PURGE -eq 1 ]]; then
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        info "Eliminando usuario de sistema: $SERVICE_USER"
        # Borra el grupo también si existe
        if getent group "$SERVICE_USER" >/dev/null 2>&1; then
            run userdel "$SERVICE_USER" 2>/dev/null || warn "No se pudo borrar el usuario."
            run groupdel "$SERVICE_USER" 2>/dev/null || true
        else
            run userdel "$SERVICE_USER" 2>/dev/null || warn "No se pudo borrar el usuario."
        fi
    else
        warn "El usuario $SERVICE_USER no existe."
    fi
else
    info "Usuario $SERVICE_USER conservado (usa --purge para eliminarlo)."
fi

# ----------------------- 8. Verificación final -----------------------
echo
info "==================== LIMPIEZA COMPLETA ===================="
if [[ $DRY_RUN -eq 1 ]]; then
    warn "Modo --dry-run: no se ha modificado nada."
fi

REMAIN=0
for check in \
    "/etc/systemd/system/${SERVICE_NAME}.service" \
    "/etc/systemd/system/${SERVICE_NAME}.timer" \
    "$INSTALL_DIR/segurmatica_updater.py" \
    "$INSTALL_DIR/segurmatica-ca.pem"; do
    if [[ -e "$check" ]]; then
        warn "Aún existe: $check"
        REMAIN=1
    fi
done

if pgrep -f 'segurmatica_updater\.py' >/dev/null 2>&1; then
    warn "Aún hay procesos de segurmatica_updater.py en ejecución."
    REMAIN=1
fi

if [[ $REMAIN -eq 0 ]]; then
    info "No quedan restos del servicio. Todo limpio."
else
    warn "Quedaron restos. Revisa los avisos anteriores."
fi

echo
echo "Para reinstalar:"
echo "  sudo ./install.sh /ruta/de/descargas"
echo "==========================================================="