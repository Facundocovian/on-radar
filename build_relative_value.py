"""
Fase 7: Relative Value Engine.

Detecta ONs que rinden significativamente más o menos que sus comparables,
sin emitir recomendaciones de inversión.

Entradas:
  outputs/fixed_income_metrics.csv   TIR, Duration, precio
  data/ratings_master.csv            Rating LP por CUIT
  outputs/credit_matrix.csv          Métricas agregadas por emisor
  outputs/ons_enriched.csv           Volumen, liquidez, CUIT

Metodología de peer groups:
  Nivel 1: misma moneda + mismo rating_bucket + duración ±1.5 años  (≥3 peers)
  Nivel 2: misma moneda + mismo rating_bucket                         (≥2 peers)
  Nivel 3: misma moneda + duración ±1.5 años                          (≥2 peers)
  Nivel 4: misma moneda                                                (fallback)

Métricas calculadas:
  spread_vs_peers  =  YTM - YTM_media_grupo  [bps]
  rv_score         =  z-score dentro del grupo  (+ = cheap, - = expensive)
  percentile       =  percentil del YTM dentro del grupo [0–100]

Salida:
  outputs/relative_value.csv

Uso:
    python build_relative_value.py
    python build_relative_value.py --rebuild-metrics   # re-corre Fase 6 antes
    python build_relative_value.py --min-duration 0.5  # cambiar umbral de duración
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

METRICS_CSV  = Path("outputs/fixed_income_metrics.csv")
RATINGS_CSV  = Path("data/ratings_master.csv")
MATRIX_CSV   = Path("outputs/credit_matrix.csv")
ENRICHED_CSV = Path("outputs/ons_enriched.csv")
OUTPUT_CSV   = Path("outputs/relative_value.csv")

# ─── Umbrales de filtrado ────────────────────────────────────────────────────
YTM_MIN       = -0.30   # -30%: bonds muy por encima del par pueden tener YTM levemente negativa
YTM_MAX       =  0.50   # +50%: por encima → precio/cashflows anómalos
MIN_DURATION  =  0.50   # años: excluye bonds casi vencidos donde la YTM anualizada no es comparable
MIN_PEERS_L1  =  3      # mínimo de peers para grupo Nivel 1
MIN_PEERS_L2  =  2      # mínimo de peers para grupos de fallback

# ─── Rating buckets ──────────────────────────────────────────────────────────
# Mapeo escala local FIX SCR / Fitch → bucket comparable
RATING_BUCKET: Dict[str, str] = {
    "AAA(arg)":  "AAA",
    "AA+(arg)":  "AA",
    "AA(arg)":   "AA",
    "AA-(arg)":  "AA",
    "A+(arg)":   "A",
    "A(arg)":    "A",
    "A-(arg)":   "A",
    "BBB+(arg)": "BBB",
    "BBB(arg)":  "BBB",
    "BBB-(arg)": "BBB",
    "BB+(arg)":  "HY",
    "BB(arg)":   "HY",
    "BB-(arg)":  "HY",
    "B+(arg)":   "HY",
    "B(arg)":    "HY",
    "B-(arg)":   "HY",
    "CCC(arg)":  "HY",
    "CC(arg)":   "HY",
    "C(arg)":    "HY",
    "D(arg)":    "HY",
}

_W = 74


# ─────────────────────────────────────────────────────────────────────────────
# Carga de datos
# ─────────────────────────────────────────────────────────────────────────────

def load_metrics() -> pd.DataFrame:
    if not METRICS_CSV.exists():
        logger.error(f"{METRICS_CSV} no existe — correr build_fixed_income_metrics.py --all primero")
        sys.exit(1)
    df = pd.read_csv(METRICS_CSV)
    for col in ("ytm", "macaulay_duration", "modified_duration", "current_yield"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    logger.info(f"fixed_income_metrics: {len(df)} bonos")
    return df


def load_enriched() -> pd.DataFrame:
    if not ENRICHED_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(ENRICHED_CSV, dtype={"cuit": str})
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    df["liquidity_score"] = pd.to_numeric(df["liquidity_score"], errors="coerce")
    return df


def load_ratings() -> pd.DataFrame:
    if not RATINGS_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(RATINGS_CSV, dtype={"cuit": str}).fillna("")


def load_matrix() -> pd.DataFrame:
    if not MATRIX_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(MATRIX_CSV, dtype={"cuit": str})


# ─────────────────────────────────────────────────────────────────────────────
# Enriquecimiento
# ─────────────────────────────────────────────────────────────────────────────

def enrich(
    metrics: pd.DataFrame,
    enriched: pd.DataFrame,
    ratings: pd.DataFrame,
    matrix: pd.DataFrame,
) -> pd.DataFrame:
    """
    Agrega al DataFrame de métricas:
      - cuit (desde ons_enriched, para lookup de ratings)
      - rating_lp si falta en metrics
      - rating_bucket
      - volume, liquidity_score (desde ons_enriched)
      - sector (desde credit_matrix via cuit)
    """
    df = metrics.copy()

    # ── CUIT desde enriched ──────────────────────────────────────────────────
    if not enriched.empty:
        cuit_map = (
            enriched[enriched["cuit"].notna()][["symbol", "cuit"]]
            .drop_duplicates("symbol")
            .set_index("symbol")["cuit"]
        )
        df["cuit"] = df["symbol"].map(cuit_map)

        # Volume / liquidity del bond individual (settlement=2 preferred)
        en2 = enriched[enriched["settlement"] == 2].copy()
        vol_map = en2.drop_duplicates("symbol").set_index("symbol")["volume"]
        liq_map = en2.drop_duplicates("symbol").set_index("symbol")["liquidity_score"]
        df["volume"]          = df["symbol"].map(vol_map)
        df["liquidity_score"] = df["symbol"].map(liq_map)
    else:
        df["cuit"] = None
        df["volume"] = None
        df["liquidity_score"] = None

    # ── Rating LP (rellenar vacíos desde ratings_master vía CUIT) ────────────
    if not ratings.empty and "cuit" in df.columns:
        r = (
            ratings[ratings["cuit"].str.len().ge(10)][["cuit", "rating_lp"]]
            .drop_duplicates("cuit")
            .set_index("cuit")["rating_lp"]
        )
        r = r.replace("", pd.NA)
        mask_missing = df["rating_lp"].isna() & df["cuit"].notna()
        df.loc[mask_missing, "rating_lp"] = df.loc[mask_missing, "cuit"].map(r)

    # ── Rating bucket ────────────────────────────────────────────────────────
    df["rating_bucket"] = df["rating_lp"].map(RATING_BUCKET).fillna("Unrated")

    # ── Sector desde credit_matrix ───────────────────────────────────────────
    if not matrix.empty and "cuit" in df.columns and "sector" in matrix.columns:
        sect_map = (
            matrix[["cuit", "sector"]].drop_duplicates("cuit").set_index("cuit")["sector"]
        )
        df["sector"] = df["cuit"].map(sect_map)
    else:
        df["sector"] = None

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Filtrado de outliers
# ─────────────────────────────────────────────────────────────────────────────

def flag_outliers(df: pd.DataFrame, min_duration: float) -> pd.DataFrame:
    """
    Marca bonos con datos anómalos como is_outlier=True.
    Se excluyen del cálculo de peers pero se mantienen en el output.
    """
    df = df.copy()
    df["is_outlier"] = (
        df["ytm"].isna() |
        df["macaulay_duration"].isna() |
        (df["ytm"] < YTM_MIN) |
        (df["ytm"] > YTM_MAX) |
        (df["macaulay_duration"] < min_duration)
    )
    n = df["is_outlier"].sum()
    if n:
        outs = df[df["is_outlier"]]["symbol"].tolist()
        logger.info(f"Outliers excluidos de peer groups ({n}): {outs}")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Cálculo de peer groups y métricas RV
# ─────────────────────────────────────────────────────────────────────────────

def _percentile_in_group(ytm: float, peer_ytms: pd.Series) -> float:
    """Percentil del YTM del bono dentro del grupo (0–100, sin scipy)."""
    if len(peer_ytms) == 0:
        return 50.0
    n_below = (peer_ytms < ytm).sum()
    return round(float(n_below / len(peer_ytms) * 100), 1)


def _rv_score(spread: float, std: float) -> float:
    """Z-score del spread vs peers; 0 si std es trivialmente pequeño."""
    if pd.isna(spread):
        return float("nan")
    if std < 1e-6:
        # Todos los peers tienen el mismo yield → no hay dispersión
        return 0.0
    return round(float(spread / std), 3)


def find_peers(
    row: pd.Series,
    pool: pd.DataFrame,
    dur_window: float = 1.5,
) -> Tuple[pd.Series, pd.Series, str]:
    """
    Devuelve (peer_ytms_series, peer_issuers_series, descripcion_del_grupo).

    Cascada de fallbacks:
      L1: currency + rating_bucket + dur ±1.5 años  (≥3 peers)
      L2: currency + rating_bucket                   (≥2 peers)
      L3: currency + dur ±1.5 años                   (≥2 peers)
      L4: currency                                    (fallback)
    """
    ccy     = row["currency"]
    bucket  = row["rating_bucket"]
    dur     = row["macaulay_duration"]

    others  = pool[pool["symbol"] != row["symbol"]]
    ccy_m   = others["currency"] == ccy
    bkt_m   = others["rating_bucket"] == bucket
    dur_m   = (others["macaulay_duration"] - dur).abs() <= dur_window

    for label, mask, min_n in [
        (f"{ccy}/{bucket}/dur±{dur_window}y",  ccy_m & bkt_m & dur_m,  MIN_PEERS_L1),
        (f"{ccy}/{bucket}",                    ccy_m & bkt_m,           MIN_PEERS_L2),
        (f"{ccy}/dur±{dur_window}y",           ccy_m & dur_m,           MIN_PEERS_L2),
        (f"{ccy}",                             ccy_m,                   1),
    ]:
        sub = others[mask].dropna(subset=["ytm"])
        if len(sub) >= min_n:
            issuers = sub["issuer"].fillna("") if "issuer" in sub.columns else pd.Series(dtype=str)
            return sub["ytm"], issuers, label

    return pd.Series(dtype=float), pd.Series(dtype=str), "sin peers"


def _signal_type(same_ratio: float, group_desc: str) -> str:
    """
    Clasifica el tipo de señal según diversidad de emisores en el grupo de peers.

    cross_credit : peers de emisores distintos → señal de valor relativo genuino
    intra_curve  : mayoría de peers son del mismo emisor → anomalía de curva propia
    fallback     : grupo de peers muy amplio (fallback L4) o sin peers
    """
    if group_desc in ("sin peers",) or pd.isna(same_ratio):
        return "fallback"
    if same_ratio >= 0.6:
        return "intra_curve"
    # Grupo L4 (solo moneda, sin rating ni duración) = señal débil
    parts = group_desc.split("/")
    if len(parts) == 1:
        return "fallback"
    return "cross_credit"


def _theoretical_price(
    price: float,
    modified_duration: float,
    ytm: float,
    peer_avg_ytm: float,
) -> float:
    """
    Precio teórico si el bono cotizara a peer_avg_ytm.

    Usa aproximación de primer orden (duración modificada):
        ΔP ≈ -ModD × ΔYield × P

    Precisa para variaciones de yield < 200bps y duration < 3 años.
    Para mayor precisión se requieren los cashflows completos.
    """
    delta_yield = peer_avg_ytm - ytm          # negativo si bono es "caro" vs peers
    d_price     = -modified_duration * delta_yield * price
    return round(price + d_price, 4)


def compute_rv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula columnas de relative value para cada bono.

    pool = bonos no-outlier → se usan como universo de comparación.
    """
    pool = df[~df["is_outlier"]].copy()

    results = []
    for _, row in df.iterrows():
        rec: Dict = {
            "symbol":                 row["symbol"],
            "peer_count":             None,
            "peer_avg_ytm":           None,
            "peer_std_ytm":           None,
            "spread_vs_peers":        None,
            "spread_bps":             None,
            "percentile":             None,
            "rv_score":               None,
            "peer_group":             None,
            "peer_unique_issuers":    None,
            "peer_same_issuer_count": None,
            "peer_same_issuer_ratio": None,
            "signal_type":            None,
            "theoretical_price":      None,
            "upside_pct":             None,
            "carry_advantage_pct":    None,
        }

        if row["is_outlier"] or pd.isna(row["ytm"]):
            results.append(rec)
            continue

        peer_ytms, peer_issuers, group_desc = find_peers(row, pool)
        rec["peer_count"]  = len(peer_ytms)
        rec["peer_group"]  = group_desc

        if len(peer_ytms) == 0:
            rec["signal_type"] = "fallback"
            results.append(rec)
            continue

        mean_ytm = float(peer_ytms.mean())
        std_ytm  = float(peer_ytms.std()) if len(peer_ytms) > 1 else 0.0
        spread   = row["ytm"] - mean_ytm

        rec["peer_avg_ytm"]    = round(mean_ytm, 6)
        rec["peer_std_ytm"]    = round(std_ytm, 6)
        rec["spread_vs_peers"] = round(spread, 6)
        rec["spread_bps"]      = round(spread * 10_000, 1)
        rec["percentile"]      = _percentile_in_group(row["ytm"], peer_ytms)
        rec["rv_score"]        = _rv_score(spread, std_ytm)

        # ── Diversidad de emisores en el grupo ───────────────────────────────
        this_issuer = str(row.get("issuer", "")) if pd.notna(row.get("issuer")) else ""
        if not peer_issuers.empty:
            unique_issuers    = int(peer_issuers.nunique())
            same_issuer_count = int((peer_issuers == this_issuer).sum()) if this_issuer else 0
            same_ratio        = round(same_issuer_count / len(peer_ytms), 3)
        else:
            unique_issuers    = 0
            same_issuer_count = 0
            same_ratio        = 0.0

        rec["peer_unique_issuers"]    = unique_issuers
        rec["peer_same_issuer_count"] = same_issuer_count
        rec["peer_same_issuer_ratio"] = same_ratio
        rec["signal_type"]            = _signal_type(same_ratio, group_desc)

        # ── Precio teórico y upside por convergencia ─────────────────────────
        mod_dur = row.get("modified_duration")
        mac_dur = row.get("macaulay_duration")
        price   = row.get("price")
        if pd.notna(mod_dur) and pd.notna(price) and price > 0:
            theo = _theoretical_price(float(price), float(mod_dur), row["ytm"], mean_ytm)
            rec["theoretical_price"] = theo
            rec["upside_pct"]        = round((theo - price) / price * 100, 2)
            # Carry advantage: retorno anual extra vs peers (por unidad de duración)
            if pd.notna(mac_dur):
                rec["carry_advantage_pct"] = round((row["ytm"] - mean_ytm) * float(mac_dur) * 100, 2)

        results.append(rec)

    rv_df = pd.DataFrame(results)
    return df.merge(rv_df, on="symbol", how="left")


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def _h(title: str) -> None:
    print(f"\n{'═' * _W}")
    print(f"  {title}")
    print(f"{'═' * _W}")


def _fmt_ytm(v) -> str:
    return f"{v*100:.2f}%" if pd.notna(v) else "—"


def _fmt_bps(v) -> str:
    if pd.isna(v):
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.0f}bp"


def _fmt_f(v, fmt=".2f") -> str:
    return format(v, fmt) if pd.notna(v) else "—"


def _confidence_flag(conf) -> str:
    """Returns a short flag for non-MAE cashflow confidence levels."""
    if pd.isna(conf) or conf == "mae":
        return ""
    if conf == "estimated":
        return " ~"   # tilde = estimado
    return ""


_SIGNAL_TYPE_LABEL = {
    "cross_credit": "✦ cross",
    "intra_curve":  "↺ curva",
    "fallback":     "~ broad",
}


def _print_rv_table(sub: pd.DataFrame, rv_full: pd.DataFrame) -> None:
    """Shared table printer for cheap/expensive rankings."""
    conf_map  = rv_full.set_index("symbol")["cashflow_confidence"] if "cashflow_confidence" in rv_full.columns else {}
    sig_map   = rv_full.set_index("symbol")["signal_type"]          if "signal_type"          in rv_full.columns else {}
    upsid_map = rv_full.set_index("symbol")["upside_pct"]           if "upside_pct"           in rv_full.columns else {}

    hdr = (f"{'Ticker':<10}  {'Emisor':<28}  {'Rat':>9}  {'YTM':>7}  {'MacD':>5}  "
           f"{'PeerYTM':>7}  {'Spread':>8}  {'RV':>6}  {'Upside':>7}  {'Tipo':<9}  Peers")
    print(hdr)
    print("─" * len(hdr))
    for _, r in sub.iterrows():
        rv_val    = r.get("rating_lp")
        conf      = conf_map.get(r["symbol"], "") if isinstance(conf_map, pd.Series) else ""
        sig_type  = sig_map.get(r["symbol"], "")  if isinstance(sig_map,  pd.Series) else ""
        upside    = upsid_map.get(r["symbol"])     if isinstance(upsid_map, pd.Series) else None
        flag      = _confidence_flag(conf)
        ticker    = f"{r['symbol']}{flag}"
        issuer    = (str(r.get("issuer")) if pd.notna(r.get("issuer")) else "—")[:28]
        rating    = (str(rv_val) if pd.notna(rv_val) else "—")[:9]
        stype_lbl = _SIGNAL_TYPE_LABEL.get(str(sig_type), "")[:9]
        peers_lbl = ""
        if pd.notna(r.get("peer_count")) and pd.notna(r.get("peer_unique_issuers")):
            peers_lbl = f"{int(r['peer_count'])}p/{int(r['peer_unique_issuers'])}e"
        upside_str = f"{upside:+.1f}%" if pd.notna(upside) else "  —  "
        print(
            f"{ticker:<10}  {issuer:<28}  {rating:>9}  "
            f"{_fmt_ytm(r['ytm']):>7}  {_fmt_f(r['macaulay_duration']):>5}  "
            f"{_fmt_ytm(r['peer_avg_ytm']):>7}  {_fmt_bps(r['spread_bps']):>8}  "
            f"{_fmt_f(r['rv_score']):>6}  {upside_str:>7}  {stype_lbl:<9}  {peers_lbl}"
        )
    print("  ~ = cashflows estimados  |  ✦ cross = peers de otros emisores  |  ↺ curva = peers del mismo emisor")


def print_top_cheap(rv: pd.DataFrame, n: int = 25) -> None:
    """Top N bonos con mayor rv_score (rinden más que sus comparables)."""
    _h(f"TOP {n}  APARENTEMENTE BARATOS  (rv_score más alto = mayor yield vs peers)")
    sub = (
        rv[rv["rv_score"].notna() & ~rv["is_outlier"]]
        .nlargest(n, "rv_score")
        [["symbol", "issuer", "rating_lp", "ytm", "macaulay_duration",
          "peer_avg_ytm", "spread_bps", "percentile", "rv_score", "peer_group"]]
        .copy()
    )
    if sub.empty:
        print("  (sin datos)")
        return
    _print_rv_table(sub, rv)


def print_top_expensive(rv: pd.DataFrame, n: int = 25) -> None:
    """Top N bonos con menor rv_score (rinden menos que sus comparables)."""
    _h(f"TOP {n}  APARENTEMENTE CAROS  (rv_score más bajo = menor yield vs peers)")
    sub = (
        rv[rv["rv_score"].notna() & ~rv["is_outlier"]]
        .nsmallest(n, "rv_score")
        [["symbol", "issuer", "rating_lp", "ytm", "macaulay_duration",
          "peer_avg_ytm", "spread_bps", "percentile", "rv_score", "peer_group"]]
        .copy()
    )
    if sub.empty:
        print("  (sin datos)")
        return
    _print_rv_table(sub, rv)


def print_coverage(rv: pd.DataFrame) -> None:
    """Estadísticas del universo analizado."""
    _h("COBERTURA DEL UNIVERSO")
    total  = len(rv)
    valid  = (~rv["is_outlier"] & rv["ytm"].notna()).sum()
    rated  = (~rv["is_outlier"] & rv["rating_lp"].notna()).sum()
    w_peers = (~rv["is_outlier"] & rv["peer_count"].notna() & rv["peer_count"].gt(0)).sum()

    print(f"  Bonos en fixed_income_metrics:   {total}")
    print(f"  Bonos usables (no-outlier):      {valid}")
    print(f"  Con rating LP:                   {rated}")
    print(f"  Con al menos 1 peer:             {w_peers}")

    print()
    print("  Por rating bucket:")
    bc = rv[~rv["is_outlier"]].groupby("rating_bucket", dropna=False)["symbol"].count()
    for bkt, cnt in bc.sort_values(ascending=False).items():
        print(f"    {str(bkt):<12} {cnt:3d} bonos")

    if "cashflow_confidence" in rv.columns:
        print()
        print("  Confianza de cashflows:")
        cf = rv[~rv["is_outlier"]]["cashflow_confidence"].fillna("mae")
        for lbl, cnt in cf.value_counts().items():
            note = {
                "mae":       " (datos API MAE)",
                "confirmed": " (verificado)",
                "estimated": " (~ estimado — verificar antes de operar)",
            }.get(lbl, "")
            print(f"    {str(lbl):<12} {cnt:3d} bonos{note}")

    print()
    print("  Distribución de rv_score (bonos con peers):")
    rs = rv[rv["rv_score"].notna() & ~rv["is_outlier"]]["rv_score"]
    if not rs.empty:
        print(f"    Media:    {rs.mean():.3f}")
        print(f"    Mediana:  {rs.median():.3f}")
        print(f"    Std:      {rs.std():.3f}")
        print(f"    Min/Max:  {rs.min():.3f} / {rs.max():.3f}")
        print(f"    |rv|>1.5: {(rs.abs()>1.5).sum()} bonos  "
              f"({(rs>1.5).sum()} caros  /  {(rs<-1.5).sum()} baratos)")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Fase 7 — Relative Value Engine")
    ap.add_argument(
        "--min-duration", type=float, default=MIN_DURATION,
        help=f"Duration mínima para incluir un bono (default: {MIN_DURATION} años)",
    )
    ap.add_argument(
        "--rebuild-metrics", action="store_true",
        help="Re-ejecutar build_fixed_income_metrics.py --all antes de correr el RV",
    )
    ap.add_argument(
        "--top", type=int, default=25,
        help="Cantidad de bonos a mostrar en cada tabla (default: 25)",
    )
    args = ap.parse_args()

    if args.rebuild_metrics:
        import subprocess
        logger.info("Re-construyendo fixed_income_metrics.csv...")
        subprocess.run(
            [sys.executable, "build_fixed_income_metrics.py", "--all"],
            check=True,
        )

    # ── Cargar datos ─────────────────────────────────────────────────────────
    metrics  = load_metrics()
    enriched = load_enriched()
    ratings  = load_ratings()
    matrix   = load_matrix()

    # ── Enriquecer ───────────────────────────────────────────────────────────
    df = enrich(metrics, enriched, ratings, matrix)

    # ── Filtrar outliers ─────────────────────────────────────────────────────
    df = flag_outliers(df, min_duration=args.min_duration)

    # ── Calcular RV ──────────────────────────────────────────────────────────
    logger.info(f"Calculando RV sobre {(~df['is_outlier']).sum()} bonos usables...")
    df = compute_rv(df)

    # ── Exportar ─────────────────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out_cols = [
        "symbol", "issuer", "rating_lp", "rating_bucket", "sector",
        "currency", "price", "ytm", "macaulay_duration", "modified_duration",
        "current_yield", "volume", "liquidity_score",
        "peer_count", "peer_avg_ytm", "peer_std_ytm",
        "spread_vs_peers", "spread_bps",
        "percentile", "rv_score",
        "peer_group", "peer_unique_issuers", "peer_same_issuer_count", "peer_same_issuer_ratio",
        "signal_type",
        "theoretical_price", "upside_pct", "carry_advantage_pct",
        "is_outlier",
        "maturity_date", "n_cashflows", "cashflow_confidence",
    ]
    # solo incluir columnas que existen
    out_cols = [c for c in out_cols if c in df.columns]
    df[out_cols].to_csv(OUTPUT_CSV, index=False)
    logger.info(f"Exportado: {OUTPUT_CSV}  ({len(df)} filas)")

    # ── Reportes ─────────────────────────────────────────────────────────────
    print_coverage(df)
    print_top_cheap(df, n=args.top)
    print_top_expensive(df, n=args.top)
    print()


if __name__ == "__main__":
    main()
