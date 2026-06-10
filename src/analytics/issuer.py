"""
Enriquecimiento de ONs con datos de emisor.

Fuente de verdad: data/issuer_master.csv
  - Generado por build_issuer_master.py desde el PDF oficial de AFIP/ARCA
  - Fallback manual en data/issuer_master.csv (entradas curadas)

Estrategia de matching:
  - El ticker BYMA tiene la forma [PREFIJO][SERIE][SUFIJO_MONEDA].
  - Se prueba contra cada prefijo del CSV de mayor a menor longitud
    (longest-first evita conflictos entre YM y YMC si ambos existieran).
  - Tickers sin match quedan agrupados por su prefijo de 3 chars
    con razon_social = "[VSC]" etc., visibles en top_issuers.

Cobertura esperada en MVP 2: ~30% de registros, ~55% del volumen
(los 5-6 emisores confirmados concentran mucho del mercado).
"""

import logging
import pandas as pd
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ISSUER_CSV = Path("data/issuer_master.csv")
CURRENCY_SUFFIXES = {"O", "D", "C", "P"}


def _strip_suffix(ticker: str) -> str:
    """Quita el sufijo de moneda (último caracter si es O/D/C/P)."""
    t = ticker.strip().upper()
    if t and t[-1] in CURRENCY_SUFFIXES:
        return t[:-1]
    return t


def extract_base_code(ticker: str) -> str:
    """
    Prefijo de 3 chars para agrupar tickers sin mapeo conocido.
    Ej: VSCXO → VSC,  YM34O → YM3,  TLCPO → TLC
    """
    return _strip_suffix(ticker)[:3]


def load_issuer_map(path: Path = DEFAULT_ISSUER_CSV) -> pd.DataFrame:
    """
    Carga el CSV de emisores y lo ordena de prefijo más largo a más corto
    para garantizar matching correcto (longest-first).
    """
    if not path.exists():
        logger.warning(f"Archivo de emisores no encontrado: {path}  — sin enriquecimiento")
        return pd.DataFrame(columns=["prefijo", "razon_social", "cuit", "sector"])

    df = pd.read_csv(path, comment="#", dtype=str).fillna("")
    if "cuit_emisor" in df.columns:
        df = df.rename(columns={"cuit_emisor": "cuit"})
    df["prefijo"] = df["prefijo"].str.upper().str.strip()
    df = df.sort_values("prefijo", key=lambda s: s.str.len(), ascending=False).reset_index(drop=True)
    logger.info(f"Mapa de emisores cargado: {len(df)} entradas  ({path})")
    return df


def _match_row(ticker: str, issuer_map: pd.DataFrame) -> dict:
    t = _strip_suffix(ticker)
    for _, row in issuer_map.iterrows():
        if t.startswith(row["prefijo"]):
            return row.to_dict()
    # Fallback: prefijo de 3 chars, razon_social entre corchetes
    base = t[:3]
    return {"prefijo": base, "razon_social": f"[{base}]", "cuit": "", "sector": "Sin mapear"}


def enrich_issuers(df: pd.DataFrame, issuer_map: pd.DataFrame) -> pd.DataFrame:
    """Agrega columnas issuer / cuit / sector / is_mapped al DataFrame."""
    if df.empty:
        return df

    rows = df["ticker"].apply(lambda t: _match_row(t, issuer_map))
    matched = pd.DataFrame(list(rows), index=df.index)

    out = df.copy()
    out["issuer"]    = matched["razon_social"]
    out["cuit"]      = matched["cuit"]
    out["sector"]    = matched["sector"]
    out["is_mapped"] = ~matched["razon_social"].str.startswith("[")

    mapped_records = out["is_mapped"].sum()
    total_vol      = out["monto_operado"].sum()
    mapped_vol     = out.loc[out["is_mapped"], "monto_operado"].sum()

    logger.info(
        f"Cobertura emisores: {mapped_records}/{len(out)} registros "
        f"({mapped_records/len(out)*100:.1f}%)  |  "
        f"Volumen cubierto: {mapped_vol/total_vol*100:.1f}%"
    )
    return out


def build_top_issuers(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ranking de emisores por volumen total operado.
    Incluye tanto emisores mapeados como grupos [PREFIX] sin mapear,
    para que el usuario vea la foto completa del mercado.
    """
    if "issuer" not in df.columns:
        raise ValueError("Ejecutar enrich_issuers() antes de build_top_issuers()")

    agg = (
        df.groupby(["issuer", "cuit", "sector", "is_mapped"], observed=True)
        .agg(
            cantidad_ons      = ("ticker", "nunique"),
            con_trade         = ("ultimo_precio", lambda s: (s > 0).sum()),
            volumen_total     = ("monto_operado", "sum"),
            liquidity_score_promedio = ("liquidity_score", "mean"),
            spread_pct_promedio      = ("spread_pct", "mean"),
            spread_pct_median        = ("spread_pct", "median"),
            years_to_maturity_promedio = ("years_to_maturity", "mean"),
        )
        .round(4)
        .reset_index()
        .sort_values("volumen_total", ascending=False)
    )
    return agg
