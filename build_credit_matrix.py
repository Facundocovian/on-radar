"""
Fase 4: Matriz de riesgo y liquidez del mercado de ONs.

Combina:
  - outputs/ons_enriched.csv   (métricas por ON individual)
  - data/ratings_master.csv    (rating LP/CP, outlook por CUIT)

Genera:
  outputs/credit_matrix.csv    (una fila por emisor)

Reportes:
  1. Top emisores por liquidez
  2. Top emisores por volumen
  3. Liquidez y spread promedio por rating
  4. Distribución por outlook (emisores + volumen)
  5. Concentración de volumen (Pareto + HHI)

Uso:
    python build_credit_matrix.py
    python build_credit_matrix.py --report-only
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ENRICHED_CSV = Path("outputs/ons_enriched.csv")
RATINGS_CSV  = Path("data/ratings_master.csv")
OUTPUT_CSV   = Path("outputs/credit_matrix.csv")

# Cap de spread_pct antes de promediar — excluye outliers tipo Mastellone (15M%)
SPREAD_CAP = 200.0

# Escala local de FIX SCR / Fitch, de mayor a menor calidad crediticia
RATING_ORDER = [
    "AAA(arg)", "AA+(arg)", "AA(arg)", "AA-(arg)",
    "A+(arg)",  "A(arg)",   "A-(arg)",
    "BBB+(arg)", "BBB(arg)", "BBB-(arg)",
    "BB+(arg)", "BB(arg)",  "BB-(arg)",
    "B+(arg)",  "B(arg)",   "B-(arg)",
    "CCC(arg)", "CC(arg)", "C(arg)", "D(arg)",
]


def _rank(rating: Optional[str]) -> int:
    if not rating or pd.isna(rating):
        return len(RATING_ORDER)
    try:
        return RATING_ORDER.index(str(rating).strip())
    except ValueError:
        return len(RATING_ORDER)


# ─────────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────────

def load_enriched() -> pd.DataFrame:
    if not ENRICHED_CSV.exists():
        logger.error(f"No existe {ENRICHED_CSV} — correr python main.py primero")
        sys.exit(1)
    df = pd.read_csv(ENRICHED_CSV, dtype={"cuit": str})
    for col in ("volume", "spread_pct", "years_to_maturity", "liquidity_score"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    logger.info(f"ons_enriched: {len(df)} ONs  |  {df['issuer'].nunique()} emisores")
    return df


def load_ratings() -> pd.DataFrame:
    if not RATINGS_CSV.exists():
        logger.warning(f"No existe {RATINGS_CSV} — matriz sin ratings")
        return pd.DataFrame(columns=["cuit", "rating_lp", "rating_cp", "outlook"])
    df = pd.read_csv(RATINGS_CSV, dtype={"cuit": str}).fillna("")
    n_rated = df["rating_lp"].ne("").sum()
    logger.info(f"ratings_master: {len(df)} emisores  |  {n_rated} con rating LP")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def build_matrix(enriched: pd.DataFrame, ratings: pd.DataFrame) -> pd.DataFrame:
    df = enriched.copy()
    df["spread_pct_w"] = df["spread_pct"].clip(upper=SPREAD_CAP)

    agg = (
        df.groupby("issuer", sort=False)
        .agg(
            cuit                  = ("cuit", "first"),
            sector                = ("sector", "first"),
            n_ons                 = ("symbol", "nunique"),
            total_volume          = ("volume", "sum"),
            avg_liquidity_score   = ("liquidity_score", "mean"),
            avg_spread_pct        = ("spread_pct_w", "mean"),
            avg_years_to_maturity = ("years_to_maturity", "mean"),
        )
        .round(4)
        .reset_index()
    )

    # Ratings: un registro por CUIT
    r = ratings[["cuit", "rating_lp", "rating_cp", "outlook"]].copy()
    r = r[r["cuit"].str.len() >= 10].drop_duplicates(subset=["cuit"])
    # "" → NaN para columnas de rating
    for col in ("rating_lp", "rating_cp", "outlook"):
        r[col] = r[col].replace("", pd.NA)

    matrix = agg.merge(r, on="cuit", how="left")

    matrix = matrix.sort_values("total_volume", ascending=False).reset_index(drop=True)

    return matrix[[
        "issuer", "cuit", "sector",
        "rating_lp", "rating_cp", "outlook",
        "n_ons", "total_volume",
        "avg_liquidity_score", "avg_spread_pct", "avg_years_to_maturity",
    ]]


# ─────────────────────────────────────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────────────────────────────────────

_W = 74


def _h(title: str) -> None:
    print(f"\n{'=' * _W}")
    print(f"  {title}")
    print(f"{'=' * _W}")


def _fmt_vol(v: float) -> str:
    if pd.isna(v):
        return "—"
    if v >= 1e9:
        return f"{v/1e9:,.2f}B"
    if v >= 1e6:
        return f"{v/1e6:,.1f}M"
    return f"{v:,.0f}"


def _fmt_f(v, fmt=".2f") -> str:
    return format(v, fmt) if pd.notna(v) else "—"


# 1. Top por liquidez
def rep_liquidez(m: pd.DataFrame, n: int = 20) -> None:
    _h("TOP EMISORES POR LIQUIDEZ PROMEDIO")
    sub = (
        m[m["avg_liquidity_score"].notna()]
        .nlargest(n, "avg_liquidity_score")
        [["issuer", "rating_lp", "avg_liquidity_score", "avg_spread_pct", "n_ons", "total_volume"]]
        .copy()
    )
    sub["avg_liquidity_score"] = sub["avg_liquidity_score"].map(lambda x: _fmt_f(x, ".2f"))
    sub["avg_spread_pct"]      = sub["avg_spread_pct"].map(lambda x: _fmt_f(x, ".2f") + "%" if pd.notna(x) else "—")
    sub["total_volume"]        = sub["total_volume"].map(_fmt_vol)
    sub["rating_lp"]           = sub["rating_lp"].fillna("—")
    print(sub.to_string(index=False))


# 2. Top por volumen
def rep_volumen(m: pd.DataFrame, n: int = 20) -> None:
    _h("TOP EMISORES POR VOLUMEN (ARS)")
    sub = (
        m.nlargest(n, "total_volume")
        [["issuer", "rating_lp", "outlook", "total_volume", "n_ons", "avg_liquidity_score"]]
        .copy()
    )
    sub["total_volume"]        = sub["total_volume"].map(_fmt_vol)
    sub["avg_liquidity_score"] = sub["avg_liquidity_score"].map(lambda x: _fmt_f(x, ".1f"))
    sub["rating_lp"]           = sub["rating_lp"].fillna("—")
    sub["outlook"]             = sub["outlook"].fillna("—")
    print(sub.to_string(index=False))


# 3. Liquidez y spread por rating
def rep_por_rating(m: pd.DataFrame) -> None:
    _h("LIQUIDEZ Y SPREAD POR RATING (LP)")
    rated = m[m["rating_lp"].notna()]
    if rated.empty:
        print("  (sin datos)")
        return
    by_r = (
        rated.groupby("rating_lp")
        .agg(
            n_emisores = ("issuer",               "count"),
            vol_ARS_M  = ("total_volume",          "sum"),
            avg_liq    = ("avg_liquidity_score",   "mean"),
            avg_spread = ("avg_spread_pct",        "mean"),
            avg_ytm    = ("avg_years_to_maturity", "mean"),
        )
        .round(2)
        .reset_index()
    )
    by_r["_rank"]    = by_r["rating_lp"].apply(_rank)
    by_r             = by_r.sort_values("_rank").drop(columns="_rank")
    by_r["vol_ARS_M"] = (by_r["vol_ARS_M"] / 1e6).map("{:,.0f}M".format)
    print(by_r.to_string(index=False))


# 4. Outlook
def rep_outlook(m: pd.DataFrame) -> None:
    _h("DISTRIBUCIÓN POR OUTLOOK")
    vol_total = m["total_volume"].sum()
    m2 = m.copy()
    m2["_out"] = m2["outlook"].fillna("Sin rating").replace("", "Sin rating")
    out = (
        m2.groupby("_out")
        .agg(
            emisores = ("issuer",       "count"),
            ons      = ("n_ons",        "sum"),
            volumen  = ("total_volume", "sum"),
        )
        .reset_index()
        .rename(columns={"_out": "outlook"})
        .sort_values("volumen", ascending=False)
    )
    out["vol_%"] = (out["volumen"] / vol_total * 100).map("{:.1f}%".format)
    out["volumen"] = out["volumen"].map(_fmt_vol)
    print(out.to_string(index=False))


# 5. Concentración
def rep_concentracion(m: pd.DataFrame, n: int = 15) -> None:
    _h("CONCENTRACIÓN DE VOLUMEN (PARETO + HHI)")
    vol_total = m["total_volume"].sum()
    top = m.nlargest(n, "total_volume")[["issuer", "rating_lp", "total_volume"]].copy()
    top["vol_%"]      = (top["total_volume"] / vol_total * 100).map("{:.2f}%".format)
    top["vol_acum_%"] = (
        top["total_volume"].cumsum() / vol_total * 100
    ).map("{:.1f}%".format)
    top["total_volume"] = top["total_volume"].map(_fmt_vol)
    top["rating_lp"]    = top["rating_lp"].fillna("—")
    print(top.to_string(index=False))
    print("─" * _W)
    shares = m["total_volume"] / vol_total
    hhi = int((shares ** 2).sum() * 10_000)
    nivel = "alta" if hhi > 2_500 else "moderada" if hhi > 1_500 else "baja"
    print(f"  HHI: {hhi:,}  ({nivel} concentración)  |  "
          f"Top-1 emis: {(m['total_volume'].max()/vol_total*100):.1f}% del mercado")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def print_all(m: pd.DataFrame) -> None:
    rep_liquidez(m)
    rep_volumen(m)
    rep_por_rating(m)
    rep_outlook(m)
    rep_concentracion(m)
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true",
                    help="Solo mostrar reporte (requiere credit_matrix.csv)")
    args = ap.parse_args()

    if args.report_only:
        if OUTPUT_CSV.exists():
            m = pd.read_csv(OUTPUT_CSV, dtype={"cuit": str})
            print_all(m)
        else:
            print(f"No existe {OUTPUT_CSV} — correr sin --report-only primero")
        return

    enriched = load_enriched()
    ratings  = load_ratings()
    matrix   = build_matrix(enriched, ratings)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    matrix.to_csv(OUTPUT_CSV, index=False)
    logger.info(f"Exportado: {OUTPUT_CSV}  ({len(matrix)} emisores)")

    print_all(matrix)


if __name__ == "__main__":
    main()
