"""
Descarga calificaciones de FIX SCR (afiliada de Fitch Ratings en Argentina).

Endpoint usado:
  /calificaciones?estado=1&sort=entidades_name
  → devuelve TODOS los ratings activos, ~37 páginas × 50 filas.

Este endpoint cubre corporativas, financieras, SGR, fondos, etc.
El filtrado por pais/area/tipo se hace en matcher.py.

Estrategia anti-ciclo: FIX SCR repite página 1 cuando se pide N > total.
Detectamos esto comparando la primera entidad de cada página con la de p1.
"""

import io
import logging
import time
from typing import List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE_URL  = "https://www.fixscr.com"
HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
DELAY_SEC = 0.7

# Columnas de /calificaciones (10 columnas — incluye AREA antes de SECTOR)
# /finanzas-corporativas tiene 9 (sin AREA), pero ya no se usa ese endpoint
COL_NAMES = [
    "entidad",
    "fecha",
    "pais",
    "area",       # ← extra: "Finanzas Corporativas", "Entidades Financieras", etc.
    "sector",     # ← "Energía", "Telecomunicaciones", etc.
    "tipo",       # ← "Emisor", "ON Clase X", "Acciones", etc.
    "rating_cp",
    "rating_lp",
    "perspectiva",
    "estado",
]

# URL del listado global con estado=activo, ordenado por nombre (estable)
CALIFICACIONES_URL = (
    f"{BASE_URL}/calificaciones"
    "?estado=1"
    "&sort=entidades_name"
    "&dp-1-per-page=50"
    "&dp-1-page={page}"
)


def _parse_table(html: str) -> Optional[pd.DataFrame]:
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return None
    if not tables:
        return None

    df = tables[0].copy()
    if df.empty:
        return None

    # FIX SCR usa 2 filas de <thead>: nombres + opciones de filtro → MultiIndex.
    # Aplanamos a nivel-0 antes de renombrar por posición.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.iloc[:, : len(COL_NAMES)].copy()
    df.columns = list(COL_NAMES)
    return df


def _fetch_url(url: str) -> Optional[pd.DataFrame]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"HTTP error [{url}]: {e}")
        return None
    return _parse_table(resp.text)


def fetch_all(max_pages: int = 50) -> pd.DataFrame:
    """
    Descarga todos los ratings activos de FIX SCR Argentina.

    Usa el endpoint /calificaciones con estado=1 (activos) y detección
    de ciclo para no re-descargar páginas repetidas.

    Devuelve DataFrame con columnas: entidad, fecha, pais, sector, tipo,
    rating_cp, rating_lp, perspectiva, estado.
    """
    frames: List[pd.DataFrame] = []
    prev_last: Optional[str] = None

    logger.info("Descargando /calificaciones (FIX SCR)...")

    for page in range(1, max_pages + 1):
        url = CALIFICACIONES_URL.format(page=page)
        df  = _fetch_url(url)

        if df is None or df.empty:
            logger.info(f"  p{page}: vacía → stop")
            break

        # Detección de ciclo: FIX SCR repite la última página cuando se sobrepasa.
        # Con sort=entidades_name, la última entidad de cada página sube por orden
        # alfabético. Si la última entidad coincide con la de la página anterior,
        # hemos entrado en ciclo.
        current_last = str(df.iloc[-1]["entidad"])
        if prev_last is not None and current_last == prev_last:
            logger.info(f"  p{page}: ciclo detectado → stop  (total real: {page-1} páginas)")
            break
        prev_last = current_last

        frames.append(df)
        logger.info(f"  p{page}: {len(df)} filas  (última: {current_last!r})")
        time.sleep(DELAY_SEC)

    if not frames:
        logger.warning("No se obtuvieron datos de FIX SCR")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    # Normalizar celdas vacías / placeholder "—"
    for col in ("rating_lp", "rating_cp", "perspectiva"):
        if col in combined.columns:
            combined[col] = (
                combined[col]
                .astype(str)
                .str.strip()
                .replace({"—": None, "-": None, "nan": None, "": None})
            )

    combined["fecha"] = pd.to_datetime(combined["fecha"], errors="coerce")

    logger.info(
        f"FIX SCR descargado: {len(combined)} filas | "
        f"Argentina: {combined['pais'].eq('Argentina').sum()} | "
        f"Emisor: {combined['tipo'].eq('Emisor').sum()}"
    )
    return combined
