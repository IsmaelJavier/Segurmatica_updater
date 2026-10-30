# Segurmática Updater

Descargador automático de actualizaciones de **Segurmática Antivirus** para servidores Linux (Ubuntu 24.04 y compatibles).

El script `segurmatica_updater.py` consulta la página de descargas de [segurmatica.cu](https://www.segurmatica.cu/descargar), detecta la última versión publicada, la descarga de forma atómica, verifica su integridad y conserva las copias más recientes. Incluye un instalador `install.sh` que lo configura como servicio systemd con ejecución **cada 2 horas** y persistencia al encender el servidor.

- Solo usa la **biblioteca estándar de Python** (3.10+). Sin dependencias externas.
- Verifica TLS con la CA propia de Segurmática (`segurmatica-ca.pem`).
- Verifica el ZIP (CRC) y genera un `sha256` junto a cada archivo.
- Descarga reanudable (`.part`) y publicación atómica.
- **Permisos abiertos (0777/0666)** en programa y descargas para que cualquier usuario pueda acceder.
- **Logs restringidos a root** vía journald.

---

## Índice

- [Requisitos](#requisitos)
- [Contenido del repositorio](#contenido-del-repositorio)
- [Instalación rápida](#instalación-rápida)
- [Uso manual del script](#uso-manual-del-script)
- [Automatización con systemd](#automatización-con-systemd)
- [Permisos](#permisos)
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

---

## Contenido del repositorio

| Archivo | Descripción |
|---|---|
| `segurmatica_updater.py` | Script principal. Descarga, verifica y conserva versiones. |
| `install.sh` | Instalador para Ubuntu 24.04: crea usuario, carpetas, servicio y timer systemd. |
| `uninstall.sh` | Limpieza / desinstalación idempotente. |
| `segurmatica-ca.pem` | Cadena de CA de `www.segurmatica.cu` para verificar HTTPS. |

---

## Instalación rápida

1. Clona el repositorio:

   ```bash
   git clone https://github.com/TU_USUARIO/segurmatica-updater.git
   cd segurmatica-updater
   ```

2. Da permisos y ejecuta el instalador indicando **dónde quieres las descargas**:

   ```bash
   chmod +x install.sh uninstall.sh
   sudo ./install.sh /srv/segurmatica_update
   ```

   El instalador:

   - Verifica Python ≥ 3.10.
   - Crea el usuario de sistema `segurmatica`.
   - Crea `/opt/segurmatica` (0777), `/var/log/segurmatica` (0700 root:root) y la carpeta de descargas (0777).
   - Copia el script (0777) y el certificado (0666).
   - Instala y activa `segurmatica-update.service` y `segurmatica-update.timer`.
   - Programa la ejecución **cada 2 horas** con `Persistent=true` (se recupera al encender).
   - Descarga **solo** el producto `segav`.
   - Envía los logs al **journal**, legibles solo por root.

3. Comprobar:

   ```bash
   systemctl list-timers segurmatica-update.timer
   sudo journalctl -u segurmatica-update.service -n 50 --no-pager
   ls -la /srv/segurmatica_update/segav/
   ```

Resultado:

```text
/srv/segurmatica_update/
└── segav/
    ├── 20261029155001.zip          (0666)
    └── 20261029155001.zip.sha256   (0666)
```

---

## Uso manual del script

### Comprobar estado

```bash
python3 segurmatica_updater.py --check
```

### Descargar Segurmática Antivirus

```bash
python3 segurmatica_updater.py --product segav --dest /srv/segurmatica_update
```

### Refrescar la cadena TLS

```bash
python3 segurmatica_updater.py --export-ca segurmatica-ca.pem
python3 segurmatica_updater.py --product segav --tls verify --check
```

---

## Automatización con systemd

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
UMask=0000
ExecStart=/usr/bin/python3 /opt/segurmatica/segurmatica_updater.py \
    --product segav \
    --dest /srv/segurmatica_update \
    --keep 3 \
    --tls auto
StandardOutput=journal
StandardError=journal
Nice=10
IOSchedulingClass=idle
```

`/etc/systemd/system/segurmatica-update.timer`:

```ini
[Unit]
Description=Ejecutar descarga de Segurmática Antivirus cada 2 horas

[Timer]
OnCalendar=*-*-* 0/2:00:00
RandomizedDelaySec=5m
Persistent=true
Unit=segurmatica-update.service

[Install]
WantedBy=timers.target
```

### Cambiar la ruta o el intervalo

```bash
sudo systemctl edit --full segurmatica-update.service
sudo systemctl edit --full segurmatica-update.timer
sudo systemctl daemon-reload
sudo systemctl restart segurmatica-update.timer
```

### Ejecutar una vez a mano

```bash
sudo systemctl start segurmatica-update.service
sudo journalctl -u segurmatica-update.service -n 100 --no-pager
```

---

## Permisos

El instalador aplica **permisos abiertos** en el programa y en las descargas, y **restringe los logs a root**:

| Ruta | Dueño:Grupo | Modo | Acceso |
|---|---|---|---|
| `/opt/segurmatica` | `root:root` | `0777` | todos leen/escriben/ejecutan |
| `/opt/segurmatica/segurmatica_updater.py` | `root:root` | `0777` | todos |
| `/opt/segurmatica/segurmatica-ca.pem` | `root:root` | `0666` | todos |
| `$DEST_DIR` | `segurmatica:segurmatica` | `0777` | todos |
| `$DEST_DIR/segav` | `segurmatica:segurmatica` | `0777` | todos |
| ZIP / `.sha256` | `segurmatica:segurmatica` | `0666` | todos |
| `/var/log/segurmatica` | `root:root` | `0700` | **solo root** |
| Log del servicio | journal | — | **solo root** |

El servicio lleva `UMask=0000` para que los archivos nuevos nazcan con `0666` y los directorios con `0777`.

> ⚠️ Con `0777` en el script y en las descargas, **cualquier usuario local** puede modificar el script (que se ejecuta como `segurmatica`) o falsificar los ZIP. Úsalo solo en servidores de confianza.

### Si ya instalaste y solo quieres cambiar permisos

```bash
sudo chmod 0777 /opt/segurmatica
sudo chmod 0777 /opt/segurmatica/segurmatica_updater.py
sudo chmod 0666 /opt/segurmatica/segurmatica-ca.pem
sudo chown -R segurmatica:segurmatica /srv/segurmatica_update
sudo chmod -R 0777 /srv/segurmatica_update
sudo find /srv/segurmatica_update -type f -exec chmod 0666 {} +
sudo chown root:root /var/log/segurmatica
sudo chmod 0700 /var/log/segurmatica
sudo systemctl edit --full segurmatica-update.service   # añade UMask=0000, quita --log-file
sudo systemctl daemon-reload
sudo systemctl restart segurmatica-update.timer
```

---

## Logs

Los logs del servicio van al **journal** (no a archivo). Solo root puede leerlos:

```bash
sudo journalctl -u segurmatica-update.service
sudo journalctl -u segurmatica-update.service -f          # seguir en vivo
sudo journalctl -u segurmatica-update.service --since today
```

La carpeta `/var/log/segurmatica` se crea `0700 root:root` y queda vacía; existe solo para que `uninstall.sh` la limpie y por si en el futuro quieres volver a `--log-file`.

Si alguna vez quieres volver a archivo **restringido a root**, añade al servicio:

```ini
StandardOutput=append:/var/log/segurmatica/updater.log
StandardError=append:/var/log/segurmatica/updater.log
```

systemd, al correr como root, puede escribir ahí aunque la carpeta sea `0700 root:root`.

---

## Opciones de línea de comandos

| Opción | Por defecto | Descripción |
|---|---|---|
| `--dest DIR` | `./actualizaciones` junto al script | Carpeta destino. |
| `--page URL\|ARCHIVO` | `https://www.segurmatica.cu/descargar` | Página o HTML local. |
| `--product, -p NOMBRE` | `segav` | Producto; repetible o separado por comas. |
| `--check`, `--dry-run` | — | Solo estado, sin descargar. |
| `--json` | — | Salida JSON con `--check`. |
| `--force` | — | Vuelve a descargar aunque la copia local sea igual o más nueva. |
| `--keep N` | `2` | Copias por producto; `0` = no borrar. |
| `--flat` | — | Archivos directamente en `--dest` (un solo producto). |
| `--no-resume` | — | No reanudar `.part`. |
| `--no-https` | — | No reescribir enlaces a HTTPS. |
| `--tls {auto,verify,insecure}` | `auto` | Política TLS. |
| `--ca-bundle PEM` | `segurmatica-ca.pem` si existe | CA/cadena. |
| `--timeout SEG` | `30` | Timeout de red. |
| `--retries N` | `3` | Reintentos por petición. |
| `--retry-delay SEG` | `2` | Espera inicial entre reintentos. |
| `--download-attempts N` | `2` | Intentos completos de descarga + verificación. |
| `--export-ca PEM` | — | Exporta los certificados de CA y termina. |
| `--list-products` | — | Lista productos soportados. |
| `--log-file RUTA` | — | Además de stderr, escribe registro (no usado por el servicio). |
| `-v`, `--verbose` | — | Registro detallado. |
| `-q`, `--quiet` | — | Solo advertencias y errores. |

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

---

## Carpeta compartida y verificación

Con permisos `0777`/`0666`, cualquier usuario o servicio (Samba, NFS, otros usuarios) puede leer y escribir. Verificación de integridad en el cliente:

```bash
cd /srv/segurmatica_update/segav
sha256sum -c 20261029155001.zip.sha256
```

---

## Solución de problemas

### «No se encontraron enlaces de actualización»

```bash
python3 segurmatica_updater.py --check -v
```

### Error de certificado TLS

```bash
python3 segurmatica_updater.py --export-ca segurmatica-ca.pem
python3 segurmatica_updater.py --product segav --tls verify --check
```

### `SSL: WRONG_VERSION_NUMBER`

El servidor es inestable en 443. `--tls auto` prueba HTTP y avisa. Para que falle en lugar de degradar: `--tls verify`.

### «Espacio insuficiente»

Reserva el tamaño + 50 MB. Cada copia del antivirus ronda 80 MB.

### «Otra instancia está en ejecución» (código 3)

```bash
ps aux | grep segurmatica_updater
```

### Descarga lenta o cortada

El `.part` se reanuda. Aumenta `--timeout 120 --retries 5`. Para reiniciar: `--no-resume` o borra el `.part`.

### «Local más reciente» y no descarga

Protección contra rollbacks. Usa `--force` solo si estás seguro.

### No puedo leer los logs

Es intencionado: solo root. Usa `sudo journalctl -u segurmatica-update.service`.

---

## Seguridad

- **Permisos abiertos (0777/0666)** en programa y descargas: cualquiera puede modificarlos. Aceptable solo en servidores de confianza.
- **Logs solo root** vía journald.
- **Validación de URL:** solo hosts de Segurmática y rutas de actualizaciones.
- **Sin extracción:** no hay zip-slip.
- **Escritura atómica** y **verificación CRC + SHA-256**.
- **TLS:** `--tls verify` recomendado; `auto` degrada con aviso; `insecure` solo temporal.

---

## Licencia

Distribuido con fines educativos y de administración de sistemas. Respeta las condiciones de uso del sitio de Segurmática al descargar sus actualizaciones.