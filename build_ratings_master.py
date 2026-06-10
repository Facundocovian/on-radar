"""
Construye data/ratings_master.csv con calificaciones crediticias de FIX SCR.

Uso:
    python build_ratings_master.py                  # descarga + match + export
    python build_ratings_master.py --cached         # reusar data/raw/fixscr_raw.csv
    python build_ratings_master.py --report-only    # solo reporte (requiere ratings_master.csv)
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from src.ratings.fetcher import fetch_all
from src.ratings.matcher import match_ratings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ISSUER_MASTER  = Path("data/issuer_master.csv")
ENRICHED_CSV   = Path("outputs/ons_enriched.csv")
FIXSCR_RAW     = Path("data/raw/fixscr_raw.csv")
RATINGS_MASTER = Path("data/ratings_master.csv")
OUTPUT_CSV     = Path("outputs/issuer_ratings.csv")


# ─────────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────────

def load_issuer_master() -> pd.DataFrame:
    if not ISSUER_MASTER.exists():
        logger.error(f"No existe {ISSUER_MASTER}")
        sys.exit(1)
    df = pd.read_csv(ISSUER_MASTER, comment="#", dtype=str).fillna("")
    if "cuit_emisor" in df.columns:
        df = df.rename(columns={"cuit_emisor": "cuit"})
    # Deduplicar por CUIT: preferir entradas manuales (nombres en title case)
    # Ordenar: manual_confirmado primero, luego afip_oficial
    df["_sort"] = df.get("fuente", "").apply(lambda f: 0 if "manual" in str(f) else 1)
    df = (
        df[df["cuit"].str.len() >= 10]
        .sort_values(["_sort", "prefijo"])
        .drop_duplicates(subset=["cuit"])
        .drop(columns=["_sort"])
        .reset_index(drop=True)
    )
    logger.info(f"Emisores únicos por CUIT: {len(df)}")
    return df


def get_fixscr(use_cache: bool = False) -> pd.DataFrame:
    if use_cache and FIXSCR_RAW.exists():
        df = pd.read_csv(FIXSCR_RAW)
        df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
        logger.info(f"FIX SCR desde caché: {len(df)} filas ({FIXSCR_RAW})")
        return df

    logger.info("Descargando ratings de FIX SCR...")
    df = fetch_all()
    if df.empty:
        logger.error("No se obtuvieron datos de FIX SCR — verificar conexión")
        return df

    FIXSCR_RAW.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(FIXSCR_RAW, index=False)
    logger.info(f"FIX SCR raw guardado: {FIXSCR_RAW} ({len(df)} filas)")
    return df


def load_enriched() -> Optional[pd.DataFrame]:
    if not ENRICHED_CSV.exists():
        logger.warning(f"Sin {ENRICHED_CSV} — sin datos de volumen (correr python main.py primero)")
        return None
    df = pd.read_csv(ENRICHED_CSV, dtype={"cuit": str})
    df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def ratings_report(
    ratings_df: pd.DataFrame,
    enriched_df: Optional[pd.DataFrame],
) -> None:
    has_rating = (
        ratings_df["rating_lp"].notna() & (ratings_df["rating_lp"] != "")
    ) | (
        ratings_df["rating_cp"].notna() & (ratings_df["rating_cp"] != "")
    )
    n_rated = int(has_rating.sum())
    n_total = len(ratings_df)

    sep = "=" * 68
    print(f"\n{sep}")
    print(f"  COBERTURA RATINGS — FIX SCR")
    print(sep)
    print(f"  Emisores únicos en master     : {n_total:>5}")
    print(f"  Con rating FIX SCR            : {n_rated:>5}  ({n_rated/n_total*100:.1f}%)")
    print(f"  Sin rating                    : {n_total-n_rated:>5}  ({(n_total-n_rated)/n_total*100:.1f}%)")

    if enriched_df is not None and not enriched_df.empty and "cuit" in enriched_df.columns:
        vol_by_cuit = (
            enriched_df.groupby("cuit")["volume"]
            .sum()
            .reset_index()
            .rename(columns={"volume": "vol_total"})
        )
        merged = ratings_df.merge(vol_by_cuit, on="cuit", how="left")
        merged["vol_total"] = merged["vol_total"].fillna(0)
        vol_total = merged["vol_total"].sum()
        if vol_total > 0:
            vol_rated = merged.loc[has_rating.values, "vol_total"].sum()
            print(f"  Volumen cubierto por rating   : {vol_rated/vol_total*100:.1f}%")

    print(f"\n{'─'*68}")
    print(f"  Top emisores con rating (por outlook + fecha):")
    print(f"{'─'*68}")
    rated_display = (
        ratings_df[has_rating][
            ["razon_social", "rating_lp", "rating_cp", "outlook", "rating_date", "fixscr_entity"]
        ]
        .sort_values("rating_date", ascending=False)
        .head(30)
    )
    print(rated_display.to_string(index=False))

    print(f"\n{'─'*68}")
    print(f"  Emisores SIN rating FIX SCR ({n_total - n_rated} total — primeros 30):")
    print(f"{'─'*68}")
    unrated_display = (
        ratings_df[~has_rating][["razon_social", "cuit", "sector"]]
        .head(30)
    )
    print(unrated_display.to_string(index=False))
    print(f"\n{sep}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Issuer ratings output
# ─────────────────────────────────────────────────────────────────────────────

def build_issuer_ratings(
    ratings_df: pd.DataFrame,
    enriched_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """
    Genera outputs/issuer_ratings.csv: ratings + volumen BYMA por CUIT.
    """
    out = ratings_df.copy()

    if enriched_df is not None and not enriched_df.empty and "cuit" in enriched_df.columns:
        vol = (
            enriched_df.groupby("cuit")
            .agg(vol_total=("volume", "sum"), n_ons=("symbol", "nunique"))
            .reset_index()
        )
        out = out.merge(vol, on="cuit", how="left")
        out["vol_total"] = pd.to_numeric(out.get("vol_total"), errors="coerce").fillna(0)
        out["n_ons"]     = pd.to_numeric(out.get("n_ons"),     errors="coerce").fillna(0).astype(int)
        out = out.sort_values("vol_total", ascending=False)
    else:
        out["vol_total"] = 0
        out["n_ons"]     = 0

    cols = [
        "razon_social", "cuit", "sector",
        "rating_lp", "rating_cp", "outlook",
        "rating_date", "fixscr_entity", "source",
        "vol_total", "n_ons",
    ]
    cols = [c for c in cols if c in out.columns]
    return out[cols].reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Construir ratings_master desde FIX SCR")
    ap.add_argument("--cached",      action="store_true", help="Usar caché FIX SCR raw")
    ap.add_argument("--report-only", action="store_true", help="Solo mostrar reporte")
    args = ap.parse_args()

    enriched_df = load_enriched()

    if args.report_only:
        if RATINGS_MASTER.exists():
            ratings_df = pd.read_csv(RATINGS_MASTER, dtype=str)
            ratings_report(ratings_df, enriched_df)
        else:
            print(f"No existe {RATINGS_MASTER}. Correr sin --report-only primero.")
        return

    # ── 1. Datos ─────────────────────────────────────────────────────────────
    issuer_df = load_issuer_master()
    fixscr_df = get_fixscr(use_cache=args.cached)

    if fixscr_df.empty:
        logger.error("Sin datos de FIX SCR — abortando")
        sys.exit(1)

    # ── 2. Match y export ratings_master ─────────────────────────────────────
    ratings_df = match_ratings(issuer_df, fixscr_df)

    RATINGS_MASTER.parent.mkdir(parents=True, exist_ok=True)
    ratings_df.to_csv(RATINGS_MASTER, index=False)
    logger.info(f"Exportado: {RATINGS_MASTER}  ({len(ratings_df)} emisores)")

    # ── 3. Reporte ───────────────────────────────────────────────────────────
    ratings_report(ratings_df, enriched_df)

    # ── 4. issuer_ratings.csv ────────────────────────────────────────────────
    ir = build_issuer_ratings(ratings_df, enriched_df)
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    ir.to_csv(OUTPUT_CSV, index=False)
    logger.info(f"Exportado: {OUTPUT_CSV}  ({len(ir)} filas)")


if __name__ == "__main__":
    main()
