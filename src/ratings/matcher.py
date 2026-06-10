"""
Matching de nombres: issuer_master ↔ entidades FIX SCR.

Estrategia:
  1. Normalización de nombre (sin tildes, sin sufijos legales, minúsculas)
  2. Exact match sobre nombre normalizado
  3. SequenceMatcher fuzzy (umbral MATCH_THRESHOLD)
  4. KNOWN_ALIASES para casos donde el nombre comercial difiere del legal
"""

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.80

# Tipos de calificación de FIX SCR que representan al emisor (no instrumentos)
EMISOR_TIPOS = {"Emisor", "Endeudamiento de Largo Plazo"}

# Alias explícitos: razon_social normalizada → substring del nombre FIX SCR normalizado
# Necesario cuando FIX SCR usa nombre de marca y no razón social legal
KNOWN_ALIASES: Dict[str, str] = {
    "tarjeta naranja":   "naranja digital compania financiera",
    "john deere credit": "john deere credit compania financiera",
}

# Sufijos legales a eliminar antes de comparar.
# El grupo no incluye el punto final — consumimos el "." con \.? DESPUÉS del grupo,
# para evitar que \b impida capturar el punto trailing (dots are non-word chars).
_LEGAL_RE = re.compile(
    r"\b("
    r"s\.a\.u"
    r"|s\.a\.c\.i\.f\. y a"
    r"|s\.a\.c\.i\.f\.ya"
    r"|s\.a\.c\.i\. y f"
    r"|s\.a\.c\.i\.f"
    r"|s\.a\.c\.i"
    r"|s\.a\.c"
    r"|s\.a"
    r"|s\.r\.l"
    r"|l\.l\.c"
    r"|llc"
    r"|s\.a\.s"
    r"|s\.e\.m"
    r"|sucursal argentina"
    r"|sucursal"
    r")\.?",
    flags=re.IGNORECASE,
)


def _norm(name: str) -> str:
    """Normaliza un nombre de empresa para comparación."""
    if not name:
        return ""
    # Quitar tildes / marcas de combinación unicode
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().strip()
    # Quitar paréntesis y su contenido
    name = re.sub(r"\([^)]*\)", "", name)
    # Quitar sufijos legales
    name = _LEGAL_RE.sub("", name)
    # Colapsar espacios y limpiar puntuación al final
    name = re.sub(r"\s+", " ", name).strip().rstrip(".,")
    return name


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _find_match(query_norm: str, emisor_df: pd.DataFrame) -> Optional[pd.Series]:
    """
    Busca el mejor match en emisor_df para query_norm.
    Primero exact match, luego fuzzy, luego aliases.
    """
    if not query_norm:
        return None

    # 1. Exact match
    exact = emisor_df[emisor_df["_name_norm"] == query_norm]
    if not exact.empty:
        return exact.iloc[0]

    # 2. Alias lookup
    alias = KNOWN_ALIASES.get(query_norm)
    if alias:
        alias_match = emisor_df[emisor_df["_name_norm"].str.contains(alias, na=False)]
        if not alias_match.empty:
            logger.debug(f"  Alias: {query_norm!r} → {alias_match.iloc[0]['entidad']!r}")
            return alias_match.iloc[0]

    # 3. Fuzzy match
    best_ratio = 0.0
    best_row: Optional[pd.Series] = None
    for _, row in emisor_df.iterrows():
        r = _ratio(query_norm, row["_name_norm"])
        if r > best_ratio:
            best_ratio = r
            best_row = row

    if best_ratio >= MATCH_THRESHOLD and best_row is not None:
        logger.debug(f"  Fuzzy ({best_ratio:.2f}): {query_norm!r} → {best_row['entidad']!r}")
        return best_row

    if best_ratio >= 0.50 and best_row is not None:
        logger.info(f"  Near-miss ({best_ratio:.2f}): {query_norm!r} ↔ {best_row['entidad']!r}")

    return None


def match_ratings(issuer_df: pd.DataFrame, fixscr_df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega columnas de rating a issuer_df uniendo con fixscr_df por nombre.

    Columnas añadidas:
      rating_lp, rating_cp, outlook, rating_date, fixscr_entity, source
    """
    RATING_COLS = ["rating_lp", "rating_cp", "outlook", "rating_date", "fixscr_entity", "source"]

    result = issuer_df.copy()
    for col in RATING_COLS:
        result[col] = None

    if fixscr_df.empty:
        logger.warning("fixscr_df vacío — sin ratings")
        return result

    # Filtrar: solo Argentina + tipo relevante para el emisor (no instrumentos)
    pais_col = fixscr_df["pais"].fillna("") if "pais" in fixscr_df.columns else pd.Series("", index=fixscr_df.index)
    tipo_col = fixscr_df["tipo"].fillna("") if "tipo" in fixscr_df.columns else pd.Series("", index=fixscr_df.index)
    mask = (
        pais_col.str.contains("Argentina", case=False)
        & tipo_col.isin(EMISOR_TIPOS)
    )
    emisor = fixscr_df[mask].copy()

    if emisor.empty:
        logger.warning("No hay registros tipo 'Emisor' de Argentina en fixscr_df")
        return result

    # Un registro por entidad: la calificación más reciente
    emisor = (
        emisor.sort_values("fecha", ascending=False)
        .drop_duplicates(subset=["entidad"])
        .reset_index(drop=True)
    )
    emisor["_name_norm"] = emisor["entidad"].apply(_norm)
    logger.info(f"Emisores FIX SCR Argentina (tipo Emisor): {len(emisor)}")

    # Match
    matched = 0
    for idx, row in result.iterrows():
        query = _norm(row.get("razon_social", ""))
        hit = _find_match(query, emisor)
        if hit is not None:
            result.at[idx, "rating_lp"]     = hit.get("rating_lp")
            result.at[idx, "rating_cp"]     = hit.get("rating_cp")
            result.at[idx, "outlook"]       = hit.get("perspectiva")
            result.at[idx, "rating_date"]   = hit.get("fecha")
            result.at[idx, "fixscr_entity"] = hit.get("entidad")
            result.at[idx, "source"]        = "fixscr"
            matched += 1

    logger.info(f"Emisores con rating: {matched}/{len(result)}")
    return result
