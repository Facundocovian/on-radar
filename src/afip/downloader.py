"""
Descarga del PDF oficial de ONs exentas de ARCA/AFIP.

URL oficial (verificada en bienes-exentos.asp 2026-05-30):
  https://www.afip.gob.ar/gananciasYBienes/bienes-personales/conceptos-
  basicos/documentos/OBLIGACIONES-NEGOCIABLES-EXENTAS-2025.pdf

ACCESO: disponible sin restricción de IP.
"""

import logging
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE = "https://www.afip.gob.ar/gananciasYBienes/bienes-personales/conceptos-basicos/documentos"
_REFERER = "https://www.afip.gob.ar/gananciasybienes/bienes-personales/conceptos-basicos/bienes-exentos.asp"

URLS_CANDIDATAS = [
    f"{_BASE}/OBLIGACIONES-NEGOCIABLES-EXENTAS-2025.pdf",
    f"{_BASE}/Obligaciones-Negociables-Exentas-2025.pdf",
    f"{_BASE}/Obligaciones-Negociables-Exentas-31-12-24.pdf",
    f"{_BASE}/OBLIGACIONES-NEGOCIABLES-EXENTAS-31-12-24.pdf",
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": _REFERER,
    "Accept": "application/pdf,*/*",
}


def _is_valid_pdf(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 50_000:
        return False
    return path.read_bytes()[:4] == b"%PDF"


def download(dest: Path, timeout: int = 30) -> bool:
    """
    Descarga el PDF oficial de AFIP/ARCA.

    Returns:
        True  — PDF válido disponible en dest
        False — todos los intentos fallaron
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    if _is_valid_pdf(dest):
        logger.info(f"PDF ya disponible: {dest} ({dest.stat().st_size:,} bytes)")
        return True

    session = requests.Session()
    # Obtener cookie de sesión
    try:
        session.get(_REFERER, headers=_HEADERS, timeout=timeout)
    except Exception:
        pass

    for url in URLS_CANDIDATAS:
        try:
            logger.info(f"Descargando: {url}")
            resp = session.get(url, headers=_HEADERS, timeout=timeout, stream=True)
            content_type = resp.headers.get("Content-Type", "")

            if resp.status_code == 200 and "pdf" in content_type.lower():
                content = resp.content
                if content[:4] == b"%PDF":
                    dest.write_bytes(content)
                    logger.info(f"PDF descargado: {dest} ({len(content):,} bytes)")
                    return True

            logger.warning(f"  HTTP {resp.status_code} | Content-Type: {content_type}")

        except Exception as e:
            logger.warning(f"  Error: {e}")

    _print_manual_instructions(dest)
    return False


def _print_manual_instructions(dest: Path) -> None:
    url = URLS_CANDIDATAS[0]
    logger.warning(
        f"\n"
        f"  No se pudo descargar el PDF automáticamente.\n"
        f"  Descargá manualmente desde:\n"
        f"  {url}\n"
        f"  y guardalo en: {dest}\n"
        f"  Luego volvé a correr: python build_issuer_master.py\n"
    )
