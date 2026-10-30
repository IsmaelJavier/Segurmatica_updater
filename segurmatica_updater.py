#!/usr/bin/env python3
"""
Descargador de actualizaciones de Segurmática.

Sustituye a SegurmaticaScript.py / SegurmaticaScriptV1.py. Consulta la página
de descargas, detecta la actualización más reciente de cada producto, la
descarga de forma atómica, verifica el archivo y conserva las últimas copias.

Documentación completa: README.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

__version__ = "2.0.0"

LOG = logging.getLogger("segurmatica")

DEFAULT_PAGE = "https://www.segurmatica.cu/descargar"
DEFAULT_HOST = "www.segurmatica.cu"
ALLOWED_HOSTS = {"www.segurmatica.cu", "segurmatica.cu"}
USER_AGENT = f"SegurmaticaUpdater/{__version__} (+Linux; descarga de actualizaciones)"
RETRY_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
SPACE_MARGIN = 50 * 1024 * 1024  # 50 MB de margen
PAGE_MAX_BYTES = 5 * 1024 * 1024
STAMP_FORMAT = "%Y%m%d%H%M%S"

# Directorio publicado -> producto lógico.
PRODUCTS: dict[str, "Product"] = {}


@dataclass(frozen=True)
class Product:
    key: str
    directory: str
    ext: str
    label: str


def _register(key: str, directory: str, ext: str, label: str) -> None:
    PRODUCTS[key] = Product(key=key, directory=directory, ext=ext, label=label)


_register("segav", "segavzip", ".zip", "Segurmática Antivirus")
_register("segek", "segavek14zip", ".zip", 'Segurmática Antivirus "Ed. Kaspersky"')
_register("savunix", "savunixzip", ".tar.gz", "SavUnix")
_register("segmovil", "segandroidzip", ".zip", "Seguridad Móvil")

PRODUCT_ALIASES = {
    "segurmatica": "segav",
    "segurmática": "segav",
    "antivirus": "segav",
    "segav": "segav",
    "kaspersky": "segek",
    "ek": "segek",
    "segek": "segek",
    "unix": "savunix",
    "savunix": "savunix",
    "android": "segmovil",
    "movil": "segmovil",
    "móvil": "segmovil",
    "segmovil": "segmovil",
}

LINK_RE = re.compile(
    r"""href\s*=\s*["'](?P<url>[^"']*?/descargas/actualizaciones/"""
    r"""(?P<dir>[A-Za-z0-9_-]+)/(?P<stamp>\d{14})\.(?P<ext>zip|tar\.gz))["']""",
    re.IGNORECASE,
)



class UpdaterError(Exception):
    """Error controlado del programa."""


class PageFormatError(UpdaterError):
    """La página no contiene enlaces reconocibles."""


class LockBusyError(UpdaterError):
    """Ya hay otra instancia descargando."""


@dataclass(frozen=True)
class RemoteUpdate:
    product: Product
    timestamp: str
    url: str

    @property
    def filename(self) -> str:
        return f"{self.timestamp}{self.product.ext}"

    @property
    def display_date(self) -> str:
        return dt.datetime.strptime(self.timestamp, STAMP_FORMAT).strftime(
            "%Y-%m-%d %H:%M:%S"
        )


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------


def human_bytes(value: int | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def setup_logging(verbose: bool, quiet: bool, log_file: Path | None) -> None:
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    """Evita dos ejecuciones simultáneas mediante flock (no bloqueante)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusyError(
                f"Otra instancia está en ejecución (bloqueo: {lock_path})"
            ) from exc
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} inicio={dt.datetime.now().isoformat()}\n")
            fh.flush()
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# HTTP/TLS
# ---------------------------------------------------------------------------


class HttpClient:
    """Cliente HTTP con reintentos, TLS configurable y degradación controlada.

    Modos TLS:
      verify   -> exige certificado válido (usa --ca-bundle o el almacén del sistema).
      auto     -> intenta verificar; si el certificado no es válido, reintenta sin
                  verificar y avisa. Si HTTPS no responde, prueba HTTP.
      insecure -> no verifica el certificado (equivalente al script V1).
    """

    def __init__(
        self,
        *,
        tls_mode: str = "auto",
        ca_bundle: Path | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        if tls_mode not in {"auto", "verify", "insecure"}:
            raise ValueError(f"Modo TLS desconocido: {tls_mode}")
        self.tls_mode = tls_mode
        self.ca_bundle = ca_bundle
        self.timeout = timeout
        self.retries = max(1, retries)
        self.retry_delay = max(0.0, retry_delay)
        self._openers: dict[bool, urllib.request.OpenerDirector] = {}
        self._insecure_hosts: set[str] = set()
        self._warned_insecure = False

    # -- construcción de openers -------------------------------------------------

    def _opener(self, verify: bool) -> urllib.request.OpenerDirector:
        if verify not in self._openers:
            if verify:
                # Almacén del sistema + bundle propio (si se indicó).
                context = ssl.create_default_context()
                if self.ca_bundle:
                    context.load_verify_locations(cafile=str(self.ca_bundle))
            else:
                context = ssl._create_unverified_context()
            self._openers[verify] = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=context)
            )
        return self._openers[verify]

    def _request(
        self,
        url: str,
        headers: dict[str, str] | None,
        method: str,
        verify: bool,
    ) -> urllib.response.addinfourl:
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers, method=method)
        return self._opener(verify).open(request, timeout=self.timeout)

    # -- reintentos --------------------------------------------------------------

    def _request_with_retries(
        self,
        url: str,
        headers: dict[str, str] | None,
        method: str,
        verify: bool,
    ) -> urllib.response.addinfourl:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return self._request(url, headers, method, verify)
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY_HTTP_CODES:
                    raise
                last_error = exc
                exc.close()
                if attempt >= self.retries:
                    raise
            except urllib.error.URLError as exc:
                reason = exc.reason
                # Un certificado inválido o un fallo TLS no se arregla reintentando.
                if isinstance(reason, (ssl.SSLCertVerificationError, ssl.SSLError)):
                    raise
                last_error = exc
                if attempt >= self.retries:
                    raise
            except (TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise

            delay = self.retry_delay * (2 ** (attempt - 1))
            LOG.warning(
                "Fallo de red en %s (intento %d/%d): %s. Reintento en %.0f s",
                url,
                attempt,
                self.retries,
                last_error,
                delay,
            )
            time.sleep(delay)
        raise last_error or UpdaterError(f"No se pudo abrir {url}")

    # -- API pública -------------------------------------------------------------

    def _candidates(self, url: str) -> list[str]:
        parsed = urllib.parse.urlsplit(url)
        candidates = [url]
        if self.tls_mode == "auto" and parsed.scheme == "https":
            candidates.append(
                urllib.parse.urlunsplit(
                    ("http", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
                )
            )
        return candidates

    def open(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "GET",
    ) -> urllib.response.addinfourl:
        last_error: Exception | None = None

        for candidate in self._candidates(url):
            host = urllib.parse.urlsplit(candidate).hostname or ""
            if self.tls_mode == "insecure":
                verify_modes = [False]
            elif self.tls_mode == "auto":
                verify_modes = [False] if host in self._insecure_hosts else [True, False]
            else:
                verify_modes = [True]

            for verify in verify_modes:
                try:
                    return self._request_with_retries(
                        candidate, headers, method, verify
                    )
                except urllib.error.HTTPError:
                    # Una respuesta HTTP (404, 403...) es definitiva: no se
                    # degrada a HTTP ni se reintenta sin verificar.
                    raise
                except urllib.error.URLError as exc:
                    last_error = exc
                    reason = exc.reason
                    if isinstance(reason, ssl.SSLCertVerificationError):
                        if self.tls_mode == "auto" and verify:
                            self._insecure_hosts.add(host)
                            if not self._warned_insecure:
                                self._warned_insecure = True
                                LOG.warning(
                                    "El certificado TLS de %s no es verificable (%s). "
                                    "Se continúa SIN verificar el certificado. "
                                    "Ejecuta --export-ca o instala la CA para evitarlo.",
                                    host,
                                    reason,
                                )
                            continue
                        raise
                    if candidate.startswith("https") and self.tls_mode == "auto":
                        LOG.warning(
                            "HTTPS no disponible en %s (%s). Se intenta por HTTP.",
                            host,
                            reason,
                        )
                        break  # probar el siguiente candidato (http)
                    raise
                except (TimeoutError, ConnectionError, ssl.SSLError) as exc:
                    last_error = exc
                    if candidate.startswith("https") and self.tls_mode == "auto":
                        LOG.warning(
                            "HTTPS no disponible en %s (%s). Se intenta por HTTP.",
                            host,
                            exc,
                        )
                        break
                    raise

        raise last_error or UpdaterError(f"No se pudo conectar con {url}")


# ---------------------------------------------------------------------------
# Página de descargas y detección de versiones
# ---------------------------------------------------------------------------


def normalize_url(url: str, *, prefer_https: bool = True) -> str:
    """Normaliza y valida una URL de actualización."""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://" + DEFAULT_HOST + url

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UpdaterError(f"Esquema no permitido: {url}")
    if parsed.username or parsed.password:
        raise UpdaterError(f"URL con credenciales no permitida: {url}")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise UpdaterError(f"Host no permitido: {parsed.hostname} ({url})")
    if not re.fullmatch(
        r"/descargas/actualizaciones/[A-Za-z0-9_-]+/\d{14}\.(?:zip|tar\.gz)",
        parsed.path,
    ):
        raise UpdaterError(f"Ruta de actualización no permitida: {parsed.path}")

    if prefer_https and parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return urllib.parse.urlunsplit(parsed)


def parse_updates(
    html_text: str,
    *,
    base_url: str = DEFAULT_PAGE,
    prefer_https: bool = True,
) -> dict[str, RemoteUpdate]:
    """Extrae la actualización más reciente de cada producto conocido."""
    found: dict[str, RemoteUpdate] = {}

    for match in LINK_RE.finditer(html_text):
        directory = match.group("dir").lower()
        product = next(
            (p for p in PRODUCTS.values() if p.directory.lower() == directory), None
        )
        if product is None:
            LOG.debug("Producto desconocido en la página: %s", directory)
            continue

        ext = "." + match.group("ext").lower()
        if ext != product.ext:
            LOG.debug(
                "Extensión inesperada para %s: %s (se esperaba %s)",
                product.key,
                ext,
                product.ext,
            )
            continue

        stamp = match.group("stamp")
        try:
            dt.datetime.strptime(stamp, STAMP_FORMAT)
        except ValueError:
            LOG.debug("Timestamp inválido ignorado: %s", stamp)
            continue

        raw_url = urllib.parse.urljoin(base_url, match.group("url"))
        try:
            url = normalize_url(raw_url, prefer_https=prefer_https)
        except UpdaterError as exc:
            LOG.warning("Enlace ignorado: %s", exc)
            continue

        current = found.get(product.key)
        if current is None or (stamp, url) > (current.timestamp, current.url):
            found[product.key] = RemoteUpdate(product, stamp, url)

    return found


def fetch_page(client: HttpClient, page: str) -> str:
    """Lee la página desde HTTP(S) o desde un archivo local (útil para pruebas)."""
    parsed = urllib.parse.urlsplit(page)
    if parsed.scheme == "file":
        path = Path(urllib.parse.unquote(parsed.path))
        return path.read_text(encoding="utf-8", errors="replace")

    local_path = Path(page).expanduser()
    if local_path.is_file():
        LOG.debug("Usando página local: %s", local_path)
        return local_path.read_text(encoding="utf-8", errors="replace")

    with client.open(page) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        data = response.read(PAGE_MAX_BYTES + 1)
    if len(data) > PAGE_MAX_BYTES:
        raise PageFormatError(
            f"La página supera el tamaño máximo esperado ({human_bytes(PAGE_MAX_BYTES)})"
        )
    return data.decode(charset, errors="replace")


# ---------------------------------------------------------------------------
# Estado local
# ---------------------------------------------------------------------------


def product_dir(dest: Path, product: Product, flat: bool) -> Path:
    return dest if flat else dest / product.key


def check_flat_product(dest: Path, product: Product, *, write: bool) -> None:
    """Impide mezclar productos distintos en una carpeta --flat.

    Los ZIP de segav y segek comparten el mismo patrón de nombre; sin esta
    marca, la limpieza de uno podría borrar los del otro.
    """
    marker = dest / ".segurmatica-product"
    if marker.is_file():
        current = marker.read_text(encoding="utf-8", errors="replace").strip()
        if current and current != product.key:
            raise UpdaterError(
                f"La carpeta {dest} ya está marcada para el producto {current!r}; "
                f"no la reutilices con {product.key!r}. Usa otra carpeta o borra "
                f"{marker.name} si estás seguro."
            )
    elif write:
        marker.write_text(f"{product.key}\n", encoding="utf-8")


def local_versions(directory: Path, ext: str) -> list[str]:
    """Timestamps locales, del más nuevo al más viejo."""
    if not directory.is_dir():
        return []
    pattern = re.compile(r"^(\d{14})" + re.escape(ext) + r"$")
    stamps: list[str] = []
    for path in directory.iterdir():
        match = pattern.match(path.name)
        if not path.is_file() or match is None:
            continue
        stamp = match.group(1)
        try:
            dt.datetime.strptime(stamp, STAMP_FORMAT)
        except ValueError:
            LOG.debug("Nombre con timestamp inválido ignorado: %s", path.name)
            continue
        stamps.append(stamp)
    return sorted(stamps, reverse=True)


def prune_old(directory: Path, ext: str, keep: int) -> None:
    """Conserva las `keep` copias más recientes (0 = no borrar nada)."""
    if keep <= 0:
        return
    pattern = re.compile(r"^(\d{14})" + re.escape(ext) + r"$")
    files: list[tuple[str, Path]] = []
    for path in directory.iterdir():
        match = pattern.match(path.name)
        if not path.is_file() or match is None:
            continue
        stamp = match.group(1)
        try:
            dt.datetime.strptime(stamp, STAMP_FORMAT)
        except ValueError:
            continue
        files.append((stamp, path))
    files.sort(key=lambda item: item[0], reverse=True)
    for _, path in files[keep:]:
        try:
            path.unlink()
            sidecar = path.with_name(path.name + ".sha256")
            sidecar.unlink(missing_ok=True)
            LOG.info("Copia antigua eliminada: %s", path.name)
        except OSError as exc:
            LOG.warning("No se pudo eliminar %s: %s", path.name, exc)


# ---------------------------------------------------------------------------
# Descarga y verificación
# ---------------------------------------------------------------------------


def verify_archive(path: Path, ext: str) -> None:
    """Comprueba que el archivo sea un ZIP/TAR.GZ íntegro y no vacío."""
    try:
        if ext == ".zip":
            with zipfile.ZipFile(path) as archive:
                if not archive.namelist():
                    raise UpdaterError("El ZIP está vacío")
                bad = archive.testzip()
                if bad is not None:
                    raise UpdaterError(f"CRC incorrecto en {bad}")
        elif ext == ".tar.gz":
            with tarfile.open(path, "r:gz") as archive:
                members = 0
                for member in archive:
                    members += 1
                    if member.isfile():
                        source = archive.extractfile(member)
                        if source is not None:
                            while source.read(1024 * 1024):
                                pass
                if members == 0:
                    raise UpdaterError("El TAR.GZ está vacío")
        else:
            raise UpdaterError(f"Extensión no verificable: {ext}")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as exc:
        raise UpdaterError(f"Archivo corrupto ({path.name}): {exc}") from exc


def _download_to_part(
    client: HttpClient,
    url: str,
    part: Path,
    *,
    resume: bool,
) -> int | None:
    """Descarga a `part`, reanudando si es posible. Devuelve el tamaño total."""
    offset = part.stat().st_size if resume and part.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else None

    try:
        response = client.open(url, headers=headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            LOG.warning("El archivo parcial no se puede reanudar; se reinicia")
            part.unlink(missing_ok=True)
            return _download_to_part(client, url, part, resume=False)
        raise

    with response:
        status = getattr(response, "status", None) or response.getcode()
        if offset and status != 206:
            LOG.warning("El servidor no admite reanudación; se reinicia la descarga")
            offset = 0
        mode = "ab" if offset else "wb"

        total: int | None = None
        if status == 206:
            content_range = response.headers.get("Content-Range", "")
            match = re.match(r"bytes\s+\d+-\d+/(\d+)", content_range)
            if match:
                total = int(match.group(1))
        else:
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                total = int(content_length)

        LOG.info(
            "Descargando %s (%s%s)",
            Path(urllib.parse.urlsplit(response.geturl()).path).name,
            human_bytes(total),
            f", reanudando en {human_bytes(offset)}" if offset else "",
        )

        done = offset
        last_log = time.monotonic()
        with part.open(mode) as fh:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last_log >= 10:
                    if total:
                        LOG.info(
                            "  %5.1f %%  %s / %s",
                            done * 100 / total,
                            human_bytes(done),
                            human_bytes(total),
                        )
                    else:
                        LOG.info("  %s descargados", human_bytes(done))
                    last_log = now
            fh.flush()
            os.fsync(fh.fileno())

    size = part.stat().st_size
    if total is not None and size != total:
        raise UpdaterError(
            f"Descarga incompleta: {human_bytes(size)} de {human_bytes(total)}"
        )
    return total


def write_sidecar(path: Path, digest: str) -> None:
    sidecar = path.with_name(path.name + ".sha256")
    temp = sidecar.with_name(sidecar.name + ".tmp")
    try:
        temp.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
        os.replace(temp, sidecar)
    except OSError as exc:
        LOG.warning("No se pudo escribir %s: %s", sidecar.name, exc)
        temp.unlink(missing_ok=True)


def download_update(
    client: HttpClient,
    update: RemoteUpdate,
    directory: Path,
    *,
    resume: bool,
    attempts: int,
    force: bool = False,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / update.filename
    part = directory / (update.filename + ".part")

    if force:
        # --force implica volver a descargar, no reutilizar el archivo final.
        part.unlink(missing_ok=True)
    elif final.is_file() and final.stat().st_size > 0:
        LOG.info("[%s] Ya existe %s", update.product.key, final.name)
        return final

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _download_to_part(client, update.url, part, resume=resume)
            try:
                verify_archive(part, update.product.ext)
            except UpdaterError:
                part.unlink(missing_ok=True)
                raise

            digest = sha256_file(part)
            os.replace(part, final)
            write_sidecar(final, digest)
            LOG.info(
                "[%s] Descargado y verificado: %s (%s, sha256=%s)",
                update.product.key,
                final,
                human_bytes(final.stat().st_size),
                digest,
            )
            return final
        except Exception as exc:  # noqa: BLE001 - se registra y se decide reintento
            last_error = exc
            if attempt >= attempts:
                break
            LOG.warning(
                "[%s] Intento %d/%d fallido: %s",
                update.product.key,
                attempt,
                attempts,
                exc,
            )
            # Un fallo de red deja el .part para reanudar; uno de verificación
            # ya lo ha borrado antes de llegar aquí.
            time.sleep(min(30, 2 * attempt))

    raise UpdaterError(f"No se pudo descargar {update.filename}: {last_error}")


def remote_size(client: HttpClient, url: str) -> int | None:
    """Tamaño publicado, si el servidor responde a HEAD."""
    try:
        with client.open(url, method="HEAD") as response:
            value = response.headers.get("Content-Length")
            return int(value) if value and value.isdigit() else None
    except urllib.error.HTTPError as exc:
        if exc.code in {405, 501}:
            return None
        LOG.debug("HEAD falló para %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - HEAD es solo informativo
        LOG.debug("HEAD falló para %s: %s", url, exc)
        return None


def check_space(directory: Path, needed: int | None) -> None:
    try:
        free = shutil.disk_usage(directory).free
    except OSError as exc:
        LOG.warning("No se pudo comprobar el espacio libre en %s: %s", directory, exc)
        return
    LOG.debug("Espacio libre en %s: %s", directory, human_bytes(free))
    if needed is not None and free < needed + SPACE_MARGIN:
        raise UpdaterError(
            f"Espacio insuficiente en {directory}: libres {human_bytes(free)}, "
            f"necesarios {human_bytes(needed + SPACE_MARGIN)}"
        )


# ---------------------------------------------------------------------------
# Certificados
# ---------------------------------------------------------------------------


def _chain_from_openssl(host: str) -> bytes:
    result = subprocess.run(
        [
            "openssl",
            "s_client",
            "-connect",
            f"{host}:443",
            "-servername",
            host,
            "-showcerts",
        ],
        input=b"",
        capture_output=True,
        timeout=25,
        check=False,
    )
    certs = re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        result.stdout,
        re.DOTALL,
    )
    if not certs:
        raise UpdaterError(
            "OpenSSL no devolvió certificados. Detalle: "
            + result.stderr.decode("utf-8", "replace")[:500]
        )
    return b"\n".join(certs) + b"\n"


def _chain_from_python(host: str) -> bytes:
    context = ssl._create_unverified_context()
    with socket.create_connection((host, 443), timeout=20) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            ssl_object = getattr(tls, "_sslobj", None)
            chain = (
                ssl_object.get_unverified_chain()
                if ssl_object is not None
                and hasattr(ssl_object, "get_unverified_chain")
                else []
            )
            if not chain:
                raise UpdaterError(
                    "Esta versión de Python no expone la cadena TLS completa. "
                    "Instala openssl o exporta la CA manualmente (ver README)."
                )
            encoding = getattr(ssl._ssl, "ENCODING_PEM", 3)
            return b"\n".join(
                cert.public_bytes(encoding) for cert in chain
            ) + b"\n"


def _ca_only(pem: bytes) -> bytes:
    """Quita el certificado hoja: un CA bundle debe contener solo CAs."""
    certs = re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.DOTALL
    )
    if len(certs) > 1:
        certs = certs[1:]
    if not certs:
        raise UpdaterError("No se encontraron certificados en la cadena TLS")
    return b"\n".join(certs) + b"\n"


def export_ca(path: Path, host: str = DEFAULT_HOST) -> None:
    try:
        pem = _chain_from_openssl(host)
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.debug("openssl no disponible (%s); usando Python", exc)
        pem = _chain_from_python(host)
    pem = _ca_only(pem)
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    count = pem.count(b"BEGIN CERTIFICATE")
    LOG.info(
        "Certificados de CA de %s guardados en %s (%d certificados)",
        host,
        path,
        count,
    )


# ---------------------------------------------------------------------------
# CLI y flujo principal
# ---------------------------------------------------------------------------


def select_products(values: Sequence[str] | None, all_products: bool) -> list[Product]:
    if all_products:
        return list(PRODUCTS.values())
    if not values:
        return [PRODUCTS["segav"]]

    selected: list[Product] = []
    for value in values:
        for item in value.split(","):
            key = item.strip().lower()
            if not key:
                continue
            if key == "all":
                return list(PRODUCTS.values())
            key = PRODUCT_ALIASES.get(key, key)
            product = PRODUCTS.get(key)
            if product is None:
                raise UpdaterError(
                    f"Producto desconocido: {item!r}. "
                    f"Válidos: {', '.join(PRODUCTS)} (o 'all')"
                )
            if product not in selected:
                selected.append(product)
    if not selected:
        raise UpdaterError("No se seleccionó ningún producto")
    return selected


def build_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    default_dest = os.environ.get(
        "SEGURMATICA_DEST", str(script_dir / "actualizaciones")
    )
    default_page = os.environ.get("SEGURMATICA_PAGE", DEFAULT_PAGE)
    bundled_ca = script_dir / "segurmatica-ca.pem"
    default_ca = os.environ.get(
        "SEGURMATICA_CA", str(bundled_ca) if bundled_ca.is_file() else ""
    )

    parser = argparse.ArgumentParser(
        prog="segurmatica_updater",
        description=(
            "Descarga las actualizaciones publicadas por Segurmática "
            "de forma atómica y verificada."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  %(prog)s --check\n"
            "  %(prog)s --product segav --dest /srv/segurmatica_update\n"
            "  %(prog)s --product all --keep 3\n"
            "  %(prog)s --export-ca segurmatica-ca.pem\n"
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--dest",
        default=default_dest,
        help="Carpeta destino (por defecto: %(default)s)",
    )
    parser.add_argument(
        "--page",
        default=default_page,
        help="Página de descargas o archivo HTML local (por defecto: %(default)s)",
    )
    parser.add_argument(
        "--product",
        "-p",
        action="append",
        metavar="NOMBRE",
        help="Producto a descargar (repetible o separado por comas). Por defecto: segav",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Descarga todos los productos conocidos",
    )
    parser.add_argument(
        "--check",
        "--dry-run",
        dest="check",
        action="store_true",
        help="Solo muestra el estado, sin descargar",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Descarga aunque la copia local sea igual o más reciente",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=2,
        metavar="N",
        help="Copias a conservar por producto; 0 = conservar todas (por defecto: 2)",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Guardar los archivos directamente en --dest (un solo producto)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="No reanudar descargas parciales (.part)",
    )
    parser.add_argument(
        "--no-https",
        action="store_true",
        help="No reescribir los enlaces publicados a HTTPS",
    )
    parser.add_argument(
        "--tls",
        choices=("auto", "verify", "insecure"),
        default="auto",
        help=(
            "auto: verifica y degrada con aviso; verify: exige certificado válido; "
            "insecure: no verifica (por defecto: auto)"
        ),
    )
    parser.add_argument(
        "--ca-bundle",
        default=default_ca,
        metavar="PEM",
        help="CA o cadena PEM para verificar el servidor (por defecto: segurmatica-ca.pem)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SEG",
        help="Timeout de red en segundos (por defecto: 30)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        metavar="N",
        help="Reintentos por petición HTTP (por defecto: 3)",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        metavar="SEG",
        help="Espera inicial entre reintentos (por defecto: 2)",
    )
    parser.add_argument(
        "--download-attempts",
        type=int,
        default=2,
        metavar="N",
        help="Intentos completos de descarga+verificación (por defecto: 2)",
    )
    parser.add_argument(
        "--export-ca",
        metavar="PEM",
        help="Exporta los certificados de CA del servidor a un archivo y termina",
    )
    parser.add_argument(
        "--list-products",
        action="store_true",
        help="Lista los productos soportados y termina",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        metavar="RUTA",
        help="Además de stderr, escribe el registro en este archivo",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Salida JSON para --check (útil para monitorización)",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true", help="Registro detallado"
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true", help="Solo advertencias y errores"
    )
    return parser


def print_check_table(rows: list[dict[str, str]]) -> None:
    headers = ("Producto", "Remoto", "Local", "Estado")
    widths = [max(len(str(row[h])) for row in rows + [dict(zip(headers, headers))]) for h in headers]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(row[h]).ljust(w) for h, w in zip(headers, widths)))


def run(args: argparse.Namespace) -> int:
    if args.list_products:
        for product in PRODUCTS.values():
            print(
                f"{product.key:10} {product.directory:16} {product.ext:8} {product.label}"
            )
        return 0

    if args.export_ca:
        export_ca(Path(args.export_ca))
        return 0

    if args.keep < 0:
        raise UpdaterError("--keep no puede ser negativo")
    if args.retries < 1:
        raise UpdaterError("--retries debe ser al menos 1")
    if args.download_attempts < 1:
        raise UpdaterError("--download-attempts debe ser al menos 1")
    if args.timeout <= 0:
        raise UpdaterError("--timeout debe ser mayor que 0")

    products = select_products(args.product, args.all)
    if args.flat and len(products) > 1:
        raise UpdaterError(
            "--flat solo se puede usar con un producto; usa una carpeta por producto"
        )

    dest = Path(args.dest).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if args.flat:
        check_flat_product(dest, products[0], write=not args.check)

    ca_bundle = Path(args.ca_bundle).expanduser() if args.ca_bundle else None
    if ca_bundle is not None and not ca_bundle.is_file():
        LOG.warning("No existe el CA indicado (%s); se usará el almacén del sistema", ca_bundle)
        ca_bundle = None

    client = HttpClient(
        tls_mode=args.tls,
        ca_bundle=ca_bundle,
        timeout=args.timeout,
        retries=args.retries,
        retry_delay=args.retry_delay,
    )

    page = args.page
    if not args.no_https and urllib.parse.urlsplit(page).scheme == "http":
        page = urllib.parse.urlunsplit(
            urllib.parse.urlsplit(page)._replace(scheme="https")
        )

    try:
        html_text = fetch_page(client, page)
    except Exception as exc:  # noqa: BLE001
        raise UpdaterError(f"No se pudo leer la página de descargas: {exc}") from exc

    updates = parse_updates(
        html_text,
        base_url=DEFAULT_PAGE,
        prefer_https=not args.no_https,
    )
    if not updates:
        raise PageFormatError(
            "No se encontraron enlaces de actualización. "
            "La estructura de la página puede haber cambiado."
        )

    missing = [p.key for p in products if p.key not in updates]
    if missing:
        LOG.error("No se encontraron enlaces para: %s", ", ".join(missing))

    rows: list[dict[str, str]] = []
    exit_code = 4 if missing else 0

    lock = nullcontext() if args.check else single_instance(dest / ".segurmatica.lock")
    with lock:
        for product in products:
            update = updates.get(product.key)
            if update is None:
                rows.append(
                    {
                        "Producto": product.label,
                        "Remoto": "—",
                        "Local": "—",
                        "Estado": "sin enlace",
                    }
                )
                continue

            directory = product_dir(dest, product, args.flat)
            versions = local_versions(directory, product.ext)
            local = versions[0] if versions else None

            if local is None:
                state = "sin copia local"
            elif update.timestamp > local:
                state = "actualizable"
            elif update.timestamp == local:
                state = "actualizado"
            else:
                state = "local más reciente"

            rows.append(
                {
                    "Producto": product.label,
                    "Remoto": update.display_date,
                    "Local": (
                        dt.datetime.strptime(local, STAMP_FORMAT).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        if local
                        else "—"
                    ),
                    "Estado": state,
                }
            )

            if args.check:
                continue

            if local is not None and update.timestamp <= local and not args.force:
                LOG.info(
                    "[%s] Sin cambios (remoto %s, local %s)",
                    product.key,
                    update.timestamp,
                    local,
                )
                prune_old(directory, product.ext, args.keep)
                continue

            if local is not None and update.timestamp < local:
                LOG.warning(
                    "[%s] El servidor publica una versión más antigua (%s) que la local (%s); "
                    "se descarga porque se usó --force",
                    product.key,
                    update.timestamp,
                    local,
                )

            try:
                directory.mkdir(parents=True, exist_ok=True)
                size = remote_size(client, update.url)
                check_space(directory, size)
                download_update(
                    client,
                    update,
                    directory,
                    resume=not args.no_resume,
                    attempts=args.download_attempts,
                    force=args.force,
                )
                prune_old(directory, product.ext, args.keep)
            except Exception as exc:  # noqa: BLE001 - continuar con el resto
                LOG.error("[%s] %s", product.key, exc)
                exit_code = 1

    if args.check:
        if args.json:
            import json

            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print_check_table(rows)

    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        setup_logging(args.verbose, args.quiet, args.log_file)
        return run(args)
    except LockBusyError as exc:
        LOG.error("%s", exc)
        return 3
    except PageFormatError as exc:
        LOG.error("%s", exc)
        return 4
    except UpdaterError as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrumpido por el usuario")
        return 130
    except Exception as exc:  # noqa: BLE001 - última barrera
        LOG.exception("Error inesperado: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
