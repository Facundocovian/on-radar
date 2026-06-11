"""
Fase 6: Motor de métricas de renta fija.

Para cada ticker:
  1. Descarga cashflows de MAE /emisiones/flujofondos/{ticker}
  2. Obtiene precio de mercado desde ons_enriched.csv o BYMA (--live)
  3. Calcula YTM, Macaulay Duration, Modified Duration, Current Yield
  4. Exporta outputs/fixed_income_metrics.csv

Estrategia de precios:
  - Para ONs USD, el precio input debe estar en % del par en USD.
  - Prioridad: EXT (C suffix) > USD (D suffix) > ARS (O suffix) / CCL implícito
  - CCL implícito se estima de pares donde existen precios C y O simultáneamente.

Uso:
    python build_fixed_income_metrics.py
    python build_fixed_income_metrics.py --tickers YMCJO PNXCO TLCMO
    python build_fixed_income_metrics.py --live          # fetch fresco de BYMA
    python build_fixed_income_metrics.py --report-only   # solo mostrar CSV existente
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from src.analytics.fixed_income import (
    calculate_current_yield,
    calculate_macaulay_duration,
    calculate_modified_duration,
    calculate_ytm,
    normalize_cashflows,
)
from src.analytics.signal_quality import infer_structure_type
from src.byma.client import BYMAClient
from src.byma.ons import get_ons

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MAE_URL       = "https://api.marketdata.mae.com.ar/api/emisiones/flujofondos/{ticker}"
ENRICHED_CSV  = Path("outputs/ons_enriched.csv")
MASTER_CSV    = Path("outputs/ons_master.csv")
RATINGS_CSV   = Path("data/ratings_master.csv")
OUTPUT_CSV    = Path("outputs/fixed_income_metrics.csv")
MAE_CACHE_DIR = Path("data/raw/mae_cashflows")

# Tickers de prueba default — se pueden sobreescribir con --tickers
DEFAULT_TICKERS = ["YMCJO", "PNXCO", "TLCMO"]


# ─────────────────────────────────────────────────────────────────────────────
# Fecha de liquidación
# ─────────────────────────────────────────────────────────────────────────────

def settlement_date() -> date:
    """T+2 días calendario (simplificación estándar para cálculos)."""
    return date.today() + timedelta(days=2)


# ─────────────────────────────────────────────────────────────────────────────
# Fuentes de precios
# ─────────────────────────────────────────────────────────────────────────────

def load_all_prices_from_master() -> pd.DataFrame:
    """
    Lee TODOS los instrumentos de ons_master.csv (variantes C/D/Z/O por bono)
    con monto_operado, para seleccionar precio por liquidez real.
    """
    if not MASTER_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(MASTER_CSV)
    # Strip BOM del primer encabezado si existe
    if df.columns[0].startswith("﻿"):
        df = df.rename(columns={df.columns[0]: df.columns[0].lstrip("﻿")})
    rename = {}
    if "ticker"           in df.columns: rename["ticker"]           = "symbol"
    if "ultimo_precio"    in df.columns: rename["ultimo_precio"]    = "price"
    if "tipo_liquidacion" in df.columns: rename["tipo_liquidacion"] = "settlement"
    df = df.rename(columns=rename)
    df["price"]         = pd.to_numeric(df.get("price",         pd.Series(dtype=float)), errors="coerce").fillna(0)
    df["settlement"]    = pd.to_numeric(df.get("settlement",    pd.Series(dtype=float)), errors="coerce").fillna(2)
    df["monto_operado"] = pd.to_numeric(df.get("monto_operado", pd.Series(dtype=float)), errors="coerce").fillna(0)
    return df


def load_prices_from_enriched() -> pd.DataFrame:
    if not ENRICHED_CSV.exists():
        logger.warning(f"{ENRICHED_CSV} no existe — sin precios cacheados")
        return pd.DataFrame()
    df = pd.read_csv(ENRICHED_CSV, dtype={"cuit": str})
    df["price"] = pd.to_numeric(df["price"], errors="coerce").fillna(0)
    if "settlement" not in df.columns:
        df["settlement"] = 2
    else:
        df["settlement"] = pd.to_numeric(df["settlement"], errors="coerce").fillna(2)
    return df


def fetch_prices_live() -> pd.DataFrame:
    logger.info("Descargando precios frescos desde BYMA Open Data...")
    try:
        client = BYMAClient()
        df = get_ons(client)
    except Exception as e:
        logger.error(f"Error BYMA: {e}")
        return pd.DataFrame()

    # Normalizar nombres de columnas a lo que espera el resto del script
    rename = {}
    if "ticker" in df.columns:
        rename["ticker"] = "symbol"
    if "ultimo_precio" in df.columns:
        rename["ultimo_precio"] = "price"
    if "tipo_liquidacion" in df.columns:
        rename["tipo_liquidacion"] = "settlement"
    if "moneda" in df.columns:
        rename["moneda"] = "currency"
    if "fecha_vencimiento" in df.columns:
        rename["fecha_vencimiento"] = "maturity_date"
    df = df.rename(columns=rename)
    df["price"] = pd.to_numeric(df.get("price", pd.Series(dtype=float)), errors="coerce").fillna(0)
    df["settlement"] = pd.to_numeric(df.get("settlement", pd.Series(dtype=float)), errors="coerce").fillna(2)
    logger.info(f"  BYMA: {len(df)} instrumentos recibidos")
    return df


def estimate_ccl(prices_df: pd.DataFrame) -> Optional[float]:
    """
    Estima CCL implícito (ARS/USD) a partir de pares de tickers donde existen
    cotizaciones tanto EXT (sufijo C, precio en USD % par) como ARS (sufijo O).

    Para ONs USD: ARS_price = USD_price × CCL  →  CCL = ARS_price / USD_price
    """
    if prices_df.empty or "symbol" not in prices_df.columns:
        return None

    ext = (
        prices_df[prices_df["symbol"].str.endswith("C") & prices_df["price"].gt(1)]
        .copy()
    )
    ext["base"] = ext["symbol"].str[:-1]

    ars = (
        prices_df[
            prices_df["symbol"].str.endswith("O") &
            prices_df["price"].gt(100) &
            (prices_df["settlement"] == 2)
        ]
        .copy()
    )
    ars["base"] = ars["symbol"].str[:-1]

    merged = ext.merge(
        ars[["base", "price"]],
        on="base",
        suffixes=("_ext", "_ars"),
    )
    if merged.empty:
        return None

    merged["ccl"] = merged["price_ars"] / merged["price_ext"]
    valid = merged["ccl"][(merged["ccl"] > 500) & (merged["ccl"] < 5_000)]
    if valid.empty:
        return None

    ccl = float(valid.median())
    logger.info(
        f"CCL implícito: {ccl:,.1f} ARS/USD  "
        f"({len(valid)} pares de referencia: {list(merged['base'].head(5))}...)"
    )
    return ccl


def get_best_price(
    mae_ticker: str,
    prices_df: pd.DataFrame,
    ccl: Optional[float],
) -> Tuple[Optional[float], str, str]:
    """
    Busca el mejor precio USD para un ticker MAE eligiendo el variante
    (C=EXT/CI, D=USD/48hs, Z=cable) con mayor monto_operado.

    Prioridad:
      1. Variante USD/EXT con mayor monto_operado (C, D o Z)
      2. ARS (O suffix) / CCL implícito como fallback

    Returns (price_as_pct_par, price_ticker_usado, price_currency)
    """
    base = mae_ticker[:-1]  # CACBO → CACB

    monto_col = next(
        (c for c in ("monto_operado", "volume") if c in prices_df.columns),
        None,
    )

    candidates = []
    for suffix, label in (("C", "EXT"), ("D", "USD"), ("Z", "USD")):
        symbol = base + suffix
        rows = prices_df[(prices_df["symbol"] == symbol) & prices_df["price"].gt(1)]
        if rows.empty:
            continue
        ci = rows[rows["settlement"] == 2]
        use = ci if not ci.empty else rows
        best_row = (
            use.sort_values(monto_col, ascending=False).iloc[0]
            if monto_col else use.sort_values("price", ascending=False).iloc[0]
        )
        monto = float(best_row[monto_col]) if monto_col else 0.0
        candidates.append({
            "symbol": symbol,
            "label":  label,
            "price":  float(best_row["price"]),
            "monto":  monto,
        })

    if candidates:
        best = max(candidates, key=lambda x: x["monto"])
        return best["price"], best["symbol"], best["label"]

    # Fallback: ARS con conversión CCL
    candidate_o = base + "O"
    rows_o = prices_df[(prices_df["symbol"] == candidate_o) & prices_df["price"].gt(100)]
    if not rows_o.empty and ccl and ccl > 0:
        ci_o = rows_o[rows_o["settlement"] == 2]
        use_o = ci_o if not ci_o.empty else rows_o
        p_ars = float(use_o.sort_values("price", ascending=False).iloc[0]["price"])
        p_usd = p_ars / ccl
        logger.info(
            f"  Precio {candidate_o}: {p_ars:,.0f} ARS / CCL {ccl:,.0f} "
            f"→ {p_usd:.2f} USD (% par)"
        )
        return p_usd, candidate_o, "ARS/CCL"

    return None, mae_ticker, "—"


# ─────────────────────────────────────────────────────────────────────────────
# MAE cashflows (con caché en disco)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_mae_cashflows(ticker: str, use_cache: bool = True) -> Optional[dict]:
    """
    Descarga cashflows de MAE con caché local en data/raw/mae_cashflows/{ticker}.json.

    Si use_cache=True y el archivo existe con detalle no vacío, lo devuelve
    sin hacer HTTP. Útil para correr --all sin re-fetchar cada vez.
    """
    cache_file = MAE_CACHE_DIR / f"{ticker.upper()}.json"

    if use_cache and cache_file.exists():
        with open(cache_file) as fh:
            data = json.load(fh)
        if data.get("detalle"):
            logger.debug(f"  MAE [{ticker}]: caché hit")
            return data
        # detalle vacío en caché → no reintentar
        return None

    url = MAE_URL.format(ticker=ticker.upper())
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"  MAE fetch error [{ticker}]: {e}")
        return None

    # Guardar en caché (tanto con detalle como sin él, para evitar re-fetch de vacíos)
    MAE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w") as fh:
        json.dump(data, fh, indent=2, default=str)

    if not data.get("detalle"):
        logger.debug(f"  MAE [{ticker}]: detalle vacío — bono muy nuevo o vencido")
        return None
    return data


def get_unique_mae_tickers(prices_df: pd.DataFrame) -> List[str]:
    """
    Extrae los tickers MAE únicos desde ons_enriched.

    Convención: el ticker MAE es base + "O" (sufijo de liquidación ARS).
    Filtra únicamente las bases con al menos un precio > 0 en cualquier variante.
    """
    if prices_df.empty or "symbol" not in prices_df.columns:
        return []

    # Bases con precio válido en cualquier sufijo
    liquid = prices_df[prices_df["price"].gt(0)]["symbol"].str[:-1].unique()
    mae_tickers = sorted({b + "O" for b in liquid})
    logger.info(f"Bases con precio > 0: {len(liquid)}  →  {len(mae_tickers)} tickers MAE únicos")
    return mae_tickers


# ─────────────────────────────────────────────────────────────────────────────
# Metadata (emisor, rating)
# ─────────────────────────────────────────────────────────────────────────────

def get_metadata(
    mae_ticker: str,
    enriched: pd.DataFrame,
    ratings: pd.DataFrame,
) -> Dict:
    row = enriched[enriched["symbol"] == mae_ticker] if not enriched.empty else pd.DataFrame()
    issuer = str(row.iloc[0]["issuer"]) if not row.empty else None
    cuit   = str(row.iloc[0]["cuit"])   if not row.empty and "cuit" in row.columns else None

    rating_lp = None
    if cuit and not ratings.empty and "cuit" in ratings.columns:
        r = ratings[ratings["cuit"].astype(str).str.strip() == cuit.strip()]
        if not r.empty:
            rv = r.iloc[0].get("rating_lp")
            rating_lp = None if (not rv or str(rv) in ("nan", "")) else str(rv)

    return {"issuer": issuer, "cuit": cuit, "rating_lp": rating_lp}


# ─────────────────────────────────────────────────────────────────────────────
# Procesamiento de un ticker
# ─────────────────────────────────────────────────────────────────────────────

def _infer_frequency(future_cashflows: list) -> int:
    """Infiere frecuencia de pagos a partir del intervalo entre los primeros dos cashflows."""
    if len(future_cashflows) < 2:
        return 2
    gap_days = (future_cashflows[1]["date"] - future_cashflows[0]["date"]).days
    if gap_days < 100:
        return 4   # ~91 días → quarterly
    if gap_days < 200:
        return 2   # ~182 días → semi-annual
    return 1       # ~365 días → annual


def process_ticker(
    mae_ticker: str,
    prices_df: pd.DataFrame,
    enriched: pd.DataFrame,
    ratings: pd.DataFrame,
    ccl: Optional[float],
    settle: date,
    mae_fetcher=None,
) -> Optional[Dict]:
    logger.info(f"  → {mae_ticker}")

    _fetch = mae_fetcher if mae_fetcher is not None else fetch_mae_cashflows
    mae_data = _fetch(mae_ticker)
    if mae_data is None:
        return None
    cashflows = normalize_cashflows(mae_data)
    if not cashflows:
        return None

    # Skip MAE bonds where all cashflows carry zero coupon — MAE API is returning
    # only the final bullet without the coupon schedule (incomplete data).
    if not mae_data.get("_manual"):
        total_coupon = sum(cf["coupon"] for cf in cashflows)
        if total_coupon == 0.0 and len(cashflows) <= 2:
            logger.warning(f"  {mae_ticker}: datos MAE incompletos (sin cupones) → skip")
            return None

    price, price_ticker, price_label = get_best_price(mae_ticker, prices_df, ccl)
    if price is None:
        logger.warning(f"  {mae_ticker}: sin precio válido — skip")
        return None

    # Para bonos parcialmente amortizados (generados manualmente), el precio BYMA
    # está en % del VR residual. Escalar a % del par original para YTM consistente.
    vr_actual = mae_data.get("_vrActual")
    if vr_actual is not None and vr_actual < 100.0:
        price = price * (vr_actual / 100.0)
        logger.info(f"    Ajuste VR residual: precio × {vr_actual/100:.4f} → {price:.4f}")

    future = [cf for cf in cashflows if cf["date"] > settle]
    if not future:
        logger.warning(f"  {mae_ticker}: no quedan cashflows futuros — skip")
        return None

    logger.info(
        f"    Precio: {price:.4f} ({price_label}, {price_ticker})  |  "
        f"Cashflows: {len(future)}  ({future[0]['date']} → {future[-1]['date']})"
    )

    ytm = calculate_ytm(price, cashflows, settle)
    if ytm is None:
        return None

    mac_dur = calculate_macaulay_duration(price, cashflows, ytm, settle)
    mod_dur = (
        calculate_modified_duration(mac_dur, ytm)
        if mac_dur is not None else None
    )

    # Cupón anual: primer cupón futuro × frecuencia (2=semi-annual, 4=quarterly).
    # Para cashflows manuales, la frecuencia viene en _frequency; para MAE se infiere
    # del intervalo entre los primeros dos pagos o se asume 2.
    freq = mae_data.get("_frequency") or _infer_frequency(future)
    annual_coupon_pct = future[0]["coupon"] * freq
    current_yield = calculate_current_yield(annual_coupon_pct, price)

    meta = get_metadata(mae_ticker, enriched, ratings)
    # Fallback de emisor: descripción MAE cuando el ticker no está en enriched
    if not meta["issuer"] and mae_data.get("descripcion"):
        meta["issuer"] = mae_data["descripcion"]

    # Cashflow confidence: "confirmed", "estimated", or "mae" for API-sourced data
    cf_confidence = mae_data.get("_confidence") or ("mae" if not mae_data.get("_manual") else "confirmed")

    structure = infer_structure_type(cashflows)

    return {
        "symbol":               mae_ticker,
        "issuer":               meta["issuer"],
        "rating_lp":            meta["rating_lp"],
        "price":                round(price, 4),
        "price_source":         f"{price_ticker} ({price_label})",
        "currency":             mae_data.get("moneda", "USD"),
        "maturity_date":        str(future[-1]["date"]),
        "ytm":                  round(ytm, 6),
        "macaulay_duration":    round(mac_dur, 4) if mac_dur is not None else None,
        "modified_duration":    round(mod_dur, 4) if mod_dur is not None else None,
        "current_yield":        round(current_yield, 6) if current_yield is not None else None,
        "n_cashflows":          len(future),
        "cashflow_confidence":  cf_confidence,
        "structure_type":       structure,
        "next_payment_date":    str(future[0]["date"]),
        "final_payment_date":   str(future[-1]["date"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

_W = 70


def _h(t: str) -> None:
    print(f"\n{'═' * _W}")
    print(f"  {t}")
    print(f"{'═' * _W}")


def print_ticker_detail(r: Dict, settle: date) -> None:
    """Muestra detalle completo de un ticker."""
    _h(f"{r['symbol']}  —  {r.get('issuer') or '(emisor desconocido)'}")

    ytm_pct       = r["ytm"] * 100
    cy_pct        = (r["current_yield"] or 0) * 100
    rating        = r.get("rating_lp") or "—"

    print(f"  Rating LP:           {rating}")
    print(f"  Precio:              {r['price']:.4f}  [{r['price_source']}]")
    print(f"  Moneda cashflows:    {r['currency']}")
    print(f"  Vencimiento:         {r['maturity_date']}")
    print(f"  Cashflows futuros:   {r['n_cashflows']}")
    print(f"    Próximo pago:      {r['next_payment_date']}")
    print(f"    Último pago:       {r['final_payment_date']}")
    print()
    print(f"  TIR (YTM):           {ytm_pct:.4f}%   (anual nominal, f=2)")
    print(f"  Duration Macaulay:   {r['macaulay_duration']:.4f} años")
    print(f"  Duration Modificada: {r['modified_duration']:.4f} años")
    print(f"  Current Yield:       {cy_pct:.4f}%")
    print(f"  Fecha liquidación:   {settle}")


def print_summary(results: List[Dict]) -> None:
    """Tabla resumen con todos los tickers procesados."""
    _h("RESUMEN  —  MÉTRICAS DE RENTA FIJA")
    if not results:
        print("  (sin resultados)")
        return

    header = f"{'Ticker':<8}  {'Emisor':<30}  {'Rating':<10}  {'Precio':>8}  {'TIR':>7}  {'MacD':>6}  {'ModD':>6}  {'CY':>6}"
    print(header)
    print("─" * len(header))
    for r in results:
        ytm_s  = f"{r['ytm']*100:.2f}%"
        cy_s   = f"{r['current_yield']*100:.2f}%" if r["current_yield"] else "—"
        mac_s  = f"{r['macaulay_duration']:.2f}" if r["macaulay_duration"] else "—"
        mod_s  = f"{r['modified_duration']:.2f}" if r["modified_duration"] else "—"
        issuer = (r.get("issuer") or "—")[:30]
        rating = (r.get("rating_lp") or "—")[:10]
        print(
            f"{r['symbol']:<8}  {issuer:<30}  {rating:<10}  "
            f"{r['price']:>8.4f}  {ytm_s:>7}  {mac_s:>6}  {mod_s:>6}  {cy_s:>6}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Fase 6 — Motor de renta fija")
    ap.add_argument(
        "--tickers", nargs="+", default=None,
        metavar="TICKER",
        help="Tickers MAE a procesar (default: YMCJO PNXCO TLCMO)",
    )
    ap.add_argument(
        "--all", action="store_true",
        help="Procesar todos los tickers con precio disponible en ons_enriched",
    )
    ap.add_argument(
        "--live", action="store_true",
        help="Descarga precios frescos desde BYMA (ignora ons_enriched.csv)",
    )
    ap.add_argument(
        "--no-cache", action="store_true",
        help="Ignorar caché MAE y re-fetchar todos los cashflows",
    )
    ap.add_argument(
        "--report-only", action="store_true",
        help="Solo mostrar CSV existente sin recalcular",
    )
    args = ap.parse_args()

    if args.report_only:
        if OUTPUT_CSV.exists():
            df = pd.read_csv(OUTPUT_CSV)
            print_summary(df.to_dict("records"))
        else:
            print(f"No existe {OUTPUT_CSV} — correr sin --report-only primero")
        return

    settle = settlement_date()
    logger.info(f"Fecha de liquidación: {settle}")

    # ── Precios ──────────────────────────────────────────────────────────────
    if args.live:
        prices_df = fetch_prices_live()
    else:
        # ons_master tiene TODOS los variantes (C/D/Z/O) con monto_operado
        # para que get_best_price elija el más líquido
        prices_df = load_all_prices_from_master()
        if prices_df.empty:
            prices_df = load_prices_from_enriched()
        logger.info(f"Precios cacheados: {len(prices_df)} instrumentos")

    ccl = estimate_ccl(prices_df)
    if ccl is None:
        logger.warning("No se pudo estimar CCL implícito — precios ARS no convertibles")

    # ── Datos de referencia ──────────────────────────────────────────────────
    # enriched siempre desde ons_enriched.csv para metadata (issuer, cuit)
    enriched = load_prices_from_enriched()
    ratings: pd.DataFrame
    if RATINGS_CSV.exists():
        ratings = pd.read_csv(RATINGS_CSV, dtype={"cuit": str}).fillna("")
    else:
        logger.warning(f"{RATINGS_CSV} no existe — sin ratings")
        ratings = pd.DataFrame()

    # ── Lista de tickers ─────────────────────────────────────────────────────
    use_cache = not args.no_cache
    if getattr(args, "all"):
        tickers = get_unique_mae_tickers(prices_df)
        verbose = False  # en modo bulk, solo resumen final
    else:
        tickers = args.tickers if args.tickers else DEFAULT_TICKERS
        verbose = True

    # ── Procesamiento ────────────────────────────────────────────────────────
    logger.info(f"\nProcesando {len(tickers)} tickers  (caché={'on' if use_cache else 'off'})")
    results: List[Dict] = []

    def _make_fetcher(uc: bool):
        def _f(t: str) -> Optional[dict]:
            return fetch_mae_cashflows(t, use_cache=uc)
        return _f

    mae_fetcher = _make_fetcher(use_cache)

    for i, ticker in enumerate(tickers, 1):
        result = process_ticker(
            ticker, prices_df, enriched, ratings, ccl, settle,
            mae_fetcher=mae_fetcher,
        )
        if result is not None:
            if verbose:
                print_ticker_detail(result, settle)
            results.append(result)

        if getattr(args, "all") and i % 20 == 0:
            logger.info(f"  Progreso: {i}/{len(tickers)}  ({len(results)} con métricas)")

        delay = 0.05 if use_cache else 0.35
        time.sleep(delay)

    # ── Output ───────────────────────────────────────────────────────────────
    print_summary(results)

    if results:
        OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        out_df = pd.DataFrame(results)[
            [
                "symbol", "issuer", "rating_lp",
                "price", "price_source", "currency",
                "maturity_date",
                "ytm", "macaulay_duration", "modified_duration", "current_yield",
                "n_cashflows", "cashflow_confidence", "structure_type",
                "next_payment_date", "final_payment_date",
            ]
        ]
        out_df.to_csv(OUTPUT_CSV, index=False)
        logger.info(f"\nExportado: {OUTPUT_CSV}  ({len(out_df)} filas)")
    else:
        logger.warning("Sin resultados — CSV no generado")
        sys.exit(1)


if __name__ == "__main__":
    main()
