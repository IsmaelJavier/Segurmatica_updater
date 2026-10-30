# Segurmática Updater

Descargador automático de actualizaciones de **Segurmática Antivirus** para servidores Linux (Ubuntu 24.04 y compatibles).

El script `segurmatica_updater.py` consulta la página de descargas de [segurmatica.cu](https://www.segurmatica.cu/descargar), detecta la última versión publicada, la descarga de forma atómica, verifica su integridad y conserva las copias más recientes. Incluye un instalador `install.sh` que lo configura como servicio systemd con ejecución **cada 2 horas** y persistencia al encender el servidor.

- Solo usa la **biblioteca estándar de Python** (3.10+). Sin dependencias externas.
- Verifica TLS con la CA propia de Segurmática (`segurmatica-ca.pem`).
- Verifica el ZIP (CRC) y genera un `sha256` junto a cada archivo.
- Descarga reanudable (`.part`) y publicación atómica.
- Pensado para publicar la carpeta por Samba/NFS a los equipos de la red.

---

## Índice

- [Requisitos](#requisitos)
- [Contenido del repositorio](#contenido-del-repositorio)
- [Instalación rápida](#instalación-rápida)
- [Uso manual del script](#uso-manual-del-script)
  - [Comprobar estado](#comprobar-estado)
  - [Descargar Segurmática Antivirus](#descargar-segurmática-antivirus)
  - [Refrescar la cadena TLS](#refrescar-la-cadena-tls)
- [Automatización con systemd](#automatización-con-systemd)
- [Opciones de línea de comandos](#opciones-de-línea-de-comandos)
- [Variables de entorno](#variables-de-entorno)
- [Códigos de salida](#códigos-de-salida)
- [Carpeta compartida y verificación](#carpeta-compartida-y-verificación)
- [Solución de problemas](#solución-de-problemas)
- [Seguridad](#seguridad)

---

## Requisitos

- **Linux** (probado en Ubuntu 24.04). Usa `fcntl` para el bloqueo entre ejecuciones, por lo que **no funciona en Windows**.
- **Python 3.10 o superior** (probado con 3.12).
- `openssl` es opcional; solo se usa para refrescar la CA con `--export-ca`.

Comprobar la versión de Python:

```bash
python3 --version
```

---

## Contenido del repositorio

| Archivo | Descripción |
|---|---|
| `segurmatica_updater.py` | Script principal. Descarga, verifica y conserva versiones. |
| `install.sh` | Instalador automático para Ubuntu 24.04: crea usuario, carpetas, servicio y timer systemd. |
| `segurmatica-ca.pem` | Cadena de CA de `www.segurmatica.cu` para verificar HTTPS sin desactivar la validación. |

---

## Instalación rápida

1. Clona el repositorio (o copia los tres archivos a una misma carpeta):

   ```bash
   git clone https://github.com/TU_USUARIO/segurmatica-updater.git
   cd segurmatica-updater
   ```

2. Da permisos al instalador y ejecútalo indicando **dónde quieres las descargas**:

   ```bash
   chmod +x install.sh
   sudo ./install.sh /srv/segurmatica_update
   ```

   Cambia `/srv/segurmatica_update` por la ruta que prefieras. El instalador:

   - Verifica que Python ≥ 3.10 esté disponible.
   - Crea el usuario de sistema `segurmatica` (sin login).
   - Crea `/opt/segurmatica`, `/var/log/segurmatica` y la carpeta de descargas.
   - Copia el script y el certificado a `/opt/segurmatica`.
   - Instala y activa el servicio `segurmatica-update.service` y el timer `segurmatica-update.timer`.
   - Programa la ejecución **cada 2 horas** con `Persistent=true` (si el servidor estaba apagado, se ejecuta al encender).
   - Descarga **solo** el producto `segav` (Segurmática Antivirus).

3. Comprobar que quedó activo:

   ```bash
   systemctl list-timers segurmatica-update.timer
   journalctl -u segurmatica-update.service -n 50 --no-pager
   ls -la /srv/segurmatica_update/segav/
   ```

Los archivos quedarán así:

```
/srv/segurmatica_update/
└── segav/
    ├── 20261029155001.zip
    └── 20261029155001.zip.sha256
```

---

## Uso manual del script

También puedes ejecutar el script directamente sin instalar nada, por ejemplo para probarlo o para integrarlo en tu propio cron.

### Comprobar estado

Muestra qué versión hay publicada y qué tienes en local. **No descarga nada.**

```bash
python3 segurmatica_updater.py --check
```

Salida de ejemplo:

```text
Producto                 Remoto               Local                Estado
-----------------------  -------------------  -------------------  ---------------
Segurmática Antivirus    2026-10-29 15:50:01  2026-10-29 15:50:01  actualizado
```

Para monitorización, con `--json`:

```bash
python3 segurmatica_updater.py --check --json
```

### Descargar Segurmática Antivirus

```bash
python3 segurmatica_updater.py --product segav --dest /srv/segurmatica_update
```

- `--product segav` selecciona solo Segurmática Antivirus.
- `--dest` indica la carpeta destino (se crea si no existe).
- `--keep N` (por defecto `2`) conserva las N copias más recientes y borra las anteriores.

Ejemplo conservando las 3 últimas:

```bash
python3 segurmatica_updater.py --product segav --dest /srv/segurmatica_update --keep 3
```

### Refrescar la cadena TLS

Si el certificado del servidor cambia y la verificación falla:

```bash
python3 segurmatica_updater.py --export-ca segurmatica-ca.pem
python3 segurmatica_updater.py --product segav --tls verify --check
```

---

## Automatización con systemd

El instalador ya deja todo configurado. Esta sección es solo para referencia o para reinstalar a mano.

### Archivos que crea `install.sh`

`/etc/systemd/system/segurmatica-update.service`:

```ini
[Unit]
Description=Descargar actualizaciones de Segurmática Antivirus
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=segurmatica
Group=segurmatica
ExecStart=/usr/bin/python3 /opt/segurmatica/segurmatica_updater.py \
    --product segav \
    --dest /srv/segurmatica_update \
    --keep 3 \
    --tls auto \
    --log-file /var/log/segurmatica/updater.log
Nice=10
IOSchedulingClass=idle
```

`/etc/systemd/system/segurmatica-update.timer`:

```ini
[Unit]
Description=Ejecutar descarga de Segurmática Antivirus cada 2 horas

[Timer]
OnCalendar=*-*-* */2:00:00
RandomizedDelaySec=5m
Persistent=true
Unit=segurmatica-update.service

[Install]
WantedBy=timers.target
```

### Cambiar la ruta o el intervalo

Edita el servicio o el timer y recarga:

```bash
sudo systemctl edit --full segurmatica-update.service
sudo systemctl edit --full segurmatica-update.timer
sudo systemctl daemon-reload
sudo systemctl restart segurmatica-update.timer
```

### Ejecutar una vez a mano

```bash
sudo systemctl start segurmatica-update.service
journalctl -u segurmatica-update.service -n 100 --no-pager
```

---

## Opciones de línea de comandos

| Opción | Por defecto | Descripción |
|---|---|---|
| `--dest DIR` | `./actualizaciones` junto al script | Carpeta destino. |
| `--page URL\|ARCHIVO` | `https://www.segurmatica.cu/descargar` | Página o HTML local (útil para pruebas sin red). |
| `--product, -p NOMBRE` | `segav` | Producto; repetible o separado por comas. |
| `--check`, `--dry-run` | — | Solo muestra estado, sin descargar. |
| `--json` | — | Salida JSON con `--check`. |
| `--force` | — | Vuelve a descargar aunque la copia local sea igual o más nueva. |
| `--keep N` | `2` | Copias por producto; `0` = no borrar. |
| `--flat` | — | Archivos directamente en `--dest` (un solo producto). |
| `--no-resume` | — | No reanudar `.part`; empieza de cero. |
| `--no-https` | — | No reescribir enlaces publicados a HTTPS. |
| `--tls {auto,verify,insecure}` | `auto` | Política TLS. |
| `--ca-bundle PEM` | `segurmatica-ca.pem` si existe | CA/cadena para verificar. |
| `--timeout SEG` | `30` | Timeout de red. |
| `--retries N` | `3` | Reintentos por petición HTTP. |
| `--retry-delay SEG` | `2` | Espera inicial entre reintentos. |
| `--download-attempts N` | `2` | Intentos completos de descarga + verificación. |
| `--export-ca PEM` | — | Exporta los certificados de CA y termina. |
| `--list-products` | — | Lista productos soportados y termina. |
| `--log-file RUTA` | — | Además de stderr, escribe el registro. |
| `-v`, `--verbose` | — | Registro detallado. |
| `-q`, `--quiet` | — | Solo advertencias y errores. |

> Aunque el script soporta varios productos (`segav`, `segek`, `savunix`, `segmovil`, `all`), el instalador `install.sh` está configurado para descargar **solo `segav`**.

---

## Variables de entorno

| Variable | Efecto |
|---|---|
| `SEGURMATICA_DEST` | Carpeta destino por defecto. |
| `SEGURMATICA_PAGE` | Página o HTML local por defecto. |
| `SEGURMATICA_CA` | Ruta al PEM de la CA por defecto. |
| `http_proxy` / `https_proxy` | `urllib` los respeta automáticamente. |

---

## Códigos de salida

| Código | Significado |
|---|---|
| `0` | Todo correcto, actualizado o descargado. |
| `1` | Error de red, descarga, verificación, permisos o espacio. |
| `2` | Error de uso de la línea de comandos. |
| `3` | Otra instancia está en ejecución (bloqueo). |
| `4` | No se pudo interpretar la página o falta el producto seleccionado. |
| `130` | Interrumpido con Ctrl+C. |

Para monitorización, trata como fallo cualquier código distinto de `0`.

---

## Carpeta compartida y verificación

El script escribe archivos normales, por lo que puedes publicar la carpeta por Samba, NFS o un directorio sincronizado.

Recomendaciones:

- El usuario `segurmatica` debe ser el dueño de la carpeta.
- Permisos típicos: `0750` para el directorio y `0640` para los archivos (o `0644` si los clientes solo necesitan lectura).
- No compartas los archivos `.part` mientras se descargan; publica preferiblemente la carpeta `segav/` o espera a que termine la descarga.

Los clientes pueden verificar la integridad con:

```bash
cd /srv/segurmatica_update/segav
sha256sum -c 20261029155001.zip.sha256
```

---

## Solución de problemas

### «No se encontraron enlaces de actualización»

La página cambió o el bloque de actualizaciones ya no está. Ejecuta con `-v` para ver detalles:

```bash
python3 segurmatica_updater.py --check -v
```

### Error de certificado TLS (`CERTIFICATE_VERIFY_FAILED`)

Refresca la cadena de CA:

```bash
python3 segurmatica_updater.py --export-ca segurmatica-ca.pem
python3 segurmatica_updater.py --product segav --tls verify --check
```

Si persiste, revisa que `segurmatica-ca.pem` esté en la misma carpeta que el script o pásalo con `--ca-bundle`.

### `SSL: WRONG_VERSION_NUMBER`

El puerto 443 no está hablando TLS en ese momento (servidor inestable). El modo `--tls auto` (por defecto) prueba HTTP y sigue, dejando un aviso en el log. Si quieres que falle en lugar de degradar, usa `--tls verify`.

### «Espacio insuficiente»

El script reserva el tamaño del archivo más 50 MB de margen. Libera espacio o baja `--keep`. Cada copia del antivirus ronda los 80 MB.

### «Otra instancia está en ejecución» (código 3)

Hay un proceso descargando o quedó un proceso colgado. Comprueba:

```bash
ps aux | grep segurmatica_updater
```

El bloqueo `flock` se libera al terminar el proceso; el archivo `.segurmatica.lock` puede quedarse, pero no impide ejecuciones futuras.

### Descarga lenta o cortada

- El `.part` se conserva y se reanuda automáticamente.
- En redes lentas, aumenta `--timeout 120 --retries 5`.
- Para empezar de cero: `--no-resume` o borra el `.part`.

### «Local más reciente» y no descarga

El servidor publica una versión más antigua que la que ya tienes. Es una protección contra rollbacks. Usa `--force` solo si estás seguro.

---

## Seguridad

- **Validación de URL:** solo se aceptan hosts de Segurmática y rutas `/descargas/actualizaciones/...`.
- **Sin extracción:** el ZIP/TAR.GZ no se descomprime, así que no hay riesgo de zip-slip.
- **Escritura atómica:** el archivo final solo aparece tras verificar y hacer `os.replace`.
- **Verificación de integridad:** CRC/estructura del archivo y SHA-256 en un sidecar.
- **TLS:** `--tls verify` es lo recomendado. `--tls auto` puede degradar a HTTP y avisa. `--tls insecure` desactiva la validación y solo debe usarse temporalmente.
- **Permisos:** el servicio corre como usuario `segurmatica` sin privilegios. No ejecutes el script como root.

---

## Licencia

Este proyecto se distribuye con fines educativos y de administración de sistemas. Respeta las condiciones de uso del sitio de Segurmática al descargar sus actualizaciones.