"""
Fase 8: Signal Quality Engine.

Calcula scores de calidad para cada señal RV:

  cashflow_quality_score   → ¿Conozco bien lo que estoy comprando?      (0–100)
  signal_confidence_score  → ¿Es confiable esta señal de RV?             (0–100)
  execution_score          → ¿Puedo ejecutar esta operación hoy?         (0–100)
  liquidity_percentile     → Percentil de volumen dentro del universo    (0–100)
  signal_category          → clasificación final de la señal
  signal_label             → texto para dashboard
  alerts                   → alertas activas separadas por |

Entradas:
  outputs/relative_value.csv   métricas RV + peer groups + peer diversity
  outputs/ons_master.csv       snapshot de mercado (spread bid/ask, días sin operación)
  data/processed/liquidity_history.csv  (opcional, si existe — para datos históricos)

Salida:
  outputs/signal_quality.csv

Uso:
    python build_signal_quality.py
    python build_signal_quality.py --verbose
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.analytics.signal_quality import score_row, market_quality_score, _get

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

RV_CSV          = Path("outputs/relative_value.csv")
MASTER_CSV      = Path("outputs/ons_master.csv")
METRICS_CSV     = Path("outputs/fixed_income_metrics.csv")
LIQ_HISTORY_CSV = Path("data/processed/liquidity_history.csv")
OUTPUT_CSV      = Path("outputs/signal_quality.csv")

_W = 74


# ─────────────────────────────────────────────────────────────────────────────
# Carga de datos
# ─────────────────────────────────────────────────────────────────────────────

def load_rv() -> pd.DataFrame:
    if not RV_CSV.exists():
        raise FileNotFoundError(f"{RV_CSV} no existe — correr build_relative_value.py primero")
    df = pd.read_csv(RV_CSV)
    for col in ("ytm", "rv_score", "macaulay_duration", "modified_duration",
                "peer_count", "peer_unique_issuers", "peer_same_issuer_ratio",
                "upside_pct", "carry_advantage_pct", "theoretical_price"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    logger.info(f"relative_value.csv: {len(df)} bonos")
    return df


def load_master() -> pd.DataFrame:
    if not MASTER_CSV.exists():
        logger.warning(f"{MASTER_CSV} no existe — sin datos de ejecución")
        return pd.DataFrame()
    df = pd.read_csv(MASTER_CSV)
    for col in ("spread_abs", "spread_pct", "monto_operado", "precio_compra",
                "precio_venta", "cantidad_operaciones", "ultimo_precio"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_metrics() -> pd.DataFrame:
    if not METRICS_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(METRICS_CSV)


def load_liq_history() -> pd.DataFrame:
    if not LIQ_HISTORY_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(LIQ_HISTORY_CSV, parse_dates=["date"])
    logger.info(f"liquidity_history.csv: {len(df)} filas ({df['date'].min().date()} → {df['date'].max().date()})")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Enriquecimiento con datos de mercado
# ─────────────────────────────────────────────────────────────────────────────

def _base_ticker(symbol: str) -> str:
    """CACBO → CACB, YMCJO → YMCJ (quita el sufijo de moneda/liquidación)."""
    return symbol[:-1] if len(symbol) >= 4 else symbol


def enrich_execution_data(rv: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega al DataFrame RV los campos necesarios para Execution Score:
      spread_bps_current : spread bid/ask del snapshot actual
      days_stale         : días desde la última operación (aprox)
    """
    rv = rv.copy()

    if master.empty:
        rv["spread_bps_current"] = None
        rv["days_stale"]         = None
        return rv

    # Usar CI (tipo_liquidacion=2) como preferido para spread
    m2 = master[master["tipo_liquidacion"] == 2].copy() if "tipo_liquidacion" in master.columns else master.copy()

    # spread en pesos → convertir a bps vs precio
    # spread_pct ya está en % del precio; convertir a bps
    if "spread_pct" in m2.columns and "ticker" in m2.columns:
        spread_map = (
            m2[m2["spread_pct"].notna() & (m2["spread_pct"] > 0)]
            .drop_duplicates("ticker")
            .set_index("ticker")["spread_pct"]
            * 100   # % → bps
        )
    else:
        spread_map = pd.Series(dtype=float)

    # days_stale: si monto_operado=0 en el snapshot, el precio viene de otro día.
    # Proxy: si precio_compra=0 Y precio_venta=0 → precio sin oferta actual
    # Sin historial real usamos 0 para bonds con monto>0, None para sin monto
    if "monto_operado" in m2.columns and "ticker" in m2.columns:
        vol_map = (
            m2.drop_duplicates("ticker")
            .set_index("ticker")["monto_operado"]
        )
    else:
        vol_map = pd.Series(dtype=float)

    def _spread(sym):
        val = spread_map.get(sym)
        if val is None or pd.isna(val):
            # Intentar con ticker sin último carácter (sufijo moneda)
            val = spread_map.get(_base_ticker(sym) + "O")
        return val if (val is not None and not pd.isna(val)) else None

    def _days_stale(sym):
        vol = vol_map.get(sym)
        if vol is None or pd.isna(vol):
            return None
        # Con un solo snapshot no podemos saber los días reales.
        # Si operó en el snapshot actual → 0 días. Si no → desconocido.
        return 0 if vol > 0 else None

    rv["spread_bps_current"] = rv["symbol"].apply(_spread)
    rv["days_stale"]         = rv["symbol"].apply(_days_stale)

    return rv


def compute_market_quality(
    rv: pd.DataFrame,
    metrics: pd.DataFrame,
) -> pd.DataFrame:
    """
    Agrega market_quality_score (0-100) por bono usando datos de la especie primaria.

    Requiere que fixed_income_metrics.csv tenga las columnas:
      monto_primary, qty_ops_primary, spread_pct_primary
    generadas por la versión actualizada de build_fixed_income_metrics.py.
    """
    rv = rv.copy()
    has_data = (
        not metrics.empty
        and all(c in metrics.columns for c in ("monto_primary", "qty_ops_primary"))
    )
    if not has_data:
        rv["market_quality_score"] = None
        return rv

    met = metrics.set_index("symbol")

    scores = []
    for _, row in rv.iterrows():
        sym = row["symbol"]
        if sym in met.index:
            m_row     = met.loc[sym]
            monto     = float(m_row.get("monto_primary")    or 0)
            qty_ops   = int(float(m_row.get("qty_ops_primary")  or 0))
            spread_p  = m_row.get("spread_pct_primary")
            spread_p  = float(spread_p) if (spread_p is not None and not pd.isna(spread_p)) else None
        else:
            monto, qty_ops, spread_p = 0.0, 0, None

        days_stale = _get(row, "days_stale")
        score = market_quality_score(
            monto_operado        = monto,
            cantidad_operaciones = qty_ops,
            spread_pct           = spread_p,
            days_stale           = int(days_stale) if days_stale is not None else None,
        )
        scores.append(score)

    rv["market_quality_score"] = scores
    return rv


def enrich_history_data(rv: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """
    Cuando existe liquidity_history.csv, calcula:
      vol_30d         : volumen operado últimos 30 días
      days_traded_30  : ruedas con actividad en 30 días
      days_stale_real : días desde la última operación
    """
    if history.empty:
        rv["vol_30d"]         = None
        rv["days_traded_30"]  = None
        return rv

    cutoff_30 = pd.Timestamp(date.today() - timedelta(days=30))
    h30 = history[history["date"] >= cutoff_30].copy()

    if h30.empty:
        rv["vol_30d"]        = None
        rv["days_traded_30"] = None
        return rv

    agg = (
        h30.groupby("symbol")
        .agg(
            vol_30d        = ("monto_operado", "sum"),
            days_traded_30 = ("monto_operado", lambda x: (x > 0).sum()),
            last_traded    = ("date", "max"),
        )
        .reset_index()
    )
    agg["days_stale_real"] = (pd.Timestamp(date.today()) - agg["last_traded"]).dt.days

    rv = rv.merge(
        agg[["symbol", "vol_30d", "days_traded_30", "days_stale_real"]],
        on="symbol", how="left",
    )

    # Sobreescribir days_stale con el valor real si está disponible
    mask = rv["days_stale_real"].notna()
    rv.loc[mask, "days_stale"] = rv.loc[mask, "days_stale_real"]
    rv = rv.drop(columns=["days_stale_real"])

    return rv


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ─────────────────────────────────────────────────────────────────────────────

def build_signal_quality(rv: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula todos los scores de calidad para cada bono del DataFrame RV.
    """
    # Universo de volúmenes para calcular percentiles relativos
    vol_series = pd.to_numeric(rv["volume"], errors="coerce").dropna()

    rows = []
    for _, row in rv.iterrows():
        scored = score_row(row, vol_series)
        rows.append({
            "symbol": row["symbol"],
            **scored,
        })

    sq = pd.DataFrame(rows)
    return rv.merge(sq, on="symbol", how="left")


# ─────────────────────────────────────────────────────────────────────────────
# Reporte de consola
# ─────────────────────────────────────────────────────────────────────────────

def _h(title: str) -> None:
    print(f"\n{'═' * _W}")
    print(f"  {title}")
    print(f"{'═' * _W}")


def print_signal_report(df: pd.DataFrame) -> None:
    """Resumen de señales activas con sus scores de calidad."""
    _h("SEÑALES ACTIVAS")

    signals = df[
        df["signal_category"].notna() &
        (df["signal_category"] != "NEUTRO") &
        (~df["is_outlier"].fillna(False))
    ].copy()

    if signals.empty:
        print("  (sin señales activas)")
        return

    cat_order = [
        "POTENCIALMENTE_BARATO", "POTENCIALMENTE_CARO",
        "POSIBLE_OPORTUNIDAD", "ANOMALIA_CURVA_PROPIA",
    ]

    for cat in cat_order:
        sub = signals[signals["signal_category"] == cat]
        if sub.empty:
            continue

        label_display = {
            "POTENCIALMENTE_BARATO":  "▲ POTENCIALMENTE BARATOS",
            "POTENCIALMENTE_CARO":    "▼ POTENCIALMENTE CAROS",
            "POSIBLE_OPORTUNIDAD":    "◆ POSIBLES OPORTUNIDADES (requieren validación)",
            "ANOMALIA_CURVA_PROPIA":  "↺ ANOMALÍAS DE CURVA PROPIA",
        }.get(cat, cat)

        print(f"\n  {label_display}")
        print(f"  {'─'*70}")

        hdr = (f"  {'Ticker':<8}  {'YTM':>7}  {'Spread':>8}  {'RV':>6}  "
               f"{'Upside':>7}  {'CFQ':>4}  {'SigC':>4}  {'Exe':>4}  Alertas")
        print(hdr)

        for _, r in sub.sort_values("rv_score", ascending=(cat == "POTENCIALMENTE_CARO")).iterrows():
            upside = r.get("upside_pct")
            upside_s = f"{upside:+.1f}%" if pd.notna(upside) else "   —  "
            alerts_s = str(r.get("alerts", "")).replace("|", ", ")[:30] if r.get("alerts") else "—"

            cfq  = f"{r['cashflow_quality_score']:.0f}"  if pd.notna(r.get("cashflow_quality_score"))  else "—"
            sigc = f"{r['signal_confidence_score']:.0f}" if pd.notna(r.get("signal_confidence_score")) else "—"
            exe  = f"{r['execution_score']:.0f}"          if pd.notna(r.get("execution_score"))          else "—"

            ytm_s    = f"{r['ytm']*100:.2f}%"  if pd.notna(r.get("ytm"))       else "—"
            spread_s = f"{r['spread_bps']:+.0f}bp" if pd.notna(r.get("spread_bps")) else "—"
            rv_s     = f"{r['rv_score']:+.2f}"   if pd.notna(r.get("rv_score"))    else "—"

            print(f"  {r['symbol']:<8}  {ytm_s:>7}  {spread_s:>8}  {rv_s:>6}  "
                  f"{upside_s:>7}  {cfq:>4}  {sigc:>4}  {exe:>4}  {alerts_s}")

    # ── Resumen de scores ────────────────────────────────────────────────────
    _h("DISTRIBUCIÓN DE SCORES (bonos no-outlier con señal)")
    usable = df[~df["is_outlier"].fillna(False) & df["rv_score"].notna()]

    for col, label in [
        ("cashflow_quality_score",  "Cashflow Quality"),
        ("signal_confidence_score", "Signal Confidence"),
        ("execution_score",         "Execution Score"),
    ]:
        if col not in usable.columns:
            continue
        s = usable[col].dropna()
        if s.empty:
            continue
        print(f"\n  {label}:")
        print(f"    Media {s.mean():.1f}  |  P25 {s.quantile(0.25):.1f}  "
              f"|  P75 {s.quantile(0.75):.1f}  |  Min {s.min():.1f}  Max {s.max():.1f}")

    # ── Confianza de cashflows ────────────────────────────────────────────────
    _h("CONFIANZA DE CASHFLOWS (universo completo)")
    cf = usable["cashflow_confidence"].fillna("mae") if "cashflow_confidence" in usable.columns else pd.Series()
    for lbl, cnt in cf.value_counts().items():
        note = {
            "mae":       " → datos API MAE",
            "confirmed": " → verificado manualmente",
            "estimated": " ~ → ESTIMADO — verificar antes de operar",
        }.get(str(lbl), "")
        print(f"  {str(lbl):<12} {cnt:3d} bonos{note}")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Fase 8 — Signal Quality Engine")
    ap.add_argument("--verbose", action="store_true", help="Logging detallado")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Cargar datos ─────────────────────────────────────────────────────────
    rv      = load_rv()
    master  = load_master()
    history = load_liq_history()

    # ── Enriquecer con datos de ejecución ────────────────────────────────────
    rv = enrich_execution_data(rv, master)
    rv = enrich_history_data(rv, history)

    # ── Agregar columnas de metrics que no vienen desde RV ───────────────────
    metrics = load_metrics()
    if not metrics.empty:
        met = metrics.set_index("symbol")
        # structure_type (preexistente)
        if "structure_type" in met.columns and "structure_type" not in rv.columns:
            rv["structure_type"] = rv["symbol"].map(met["structure_type"])
        # Nuevas columnas de especie y calidad
        for col in ("primary_species", "tir_dispersion_bp", "has_desarbitraje",
                    "monto_primary", "qty_ops_primary", "spread_pct_primary"):
            if col in met.columns and col not in rv.columns:
                rv[col] = rv["symbol"].map(met[col])

    # ── Market Quality Score (requiere days_stale + datos de métricas) ───────
    rv = compute_market_quality(rv, metrics)

    # ── Calcular scores ──────────────────────────────────────────────────────
    logger.info("Calculando signal quality scores...")
    df = build_signal_quality(rv)

    # ── Exportar ─────────────────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    out_cols = [
        "symbol", "issuer", "rating_lp", "rating_bucket", "sector", "currency",
        "price", "ytm", "macaulay_duration",
        "rv_score", "spread_bps", "peer_count", "peer_unique_issuers",
        "peer_same_issuer_ratio", "signal_type",
        "theoretical_price", "upside_pct", "carry_advantage_pct",
        "cashflow_confidence", "structure_type", "n_cashflows",
        "cashflow_quality_score",
        "signal_confidence_score",
        "execution_score",
        "liquidity_percentile",
        "market_quality_score",
        "days_stale", "spread_bps_current",
        "primary_species", "tir_dispersion_bp", "has_desarbitraje",
        "signal_category", "signal_label", "alerts", "alert_count",
        "is_outlier", "maturity_date",
    ]
    out_cols = [c for c in out_cols if c in df.columns]
    df[out_cols].to_csv(OUTPUT_CSV, index=False)
    logger.info(f"Exportado: {OUTPUT_CSV}  ({len(df)} filas)")

    # ── Reporte ──────────────────────────────────────────────────────────────
    print_signal_report(df)


if __name__ == "__main__":
    main()
