"""
Diagnóstico: tabla de precios Radar vs mercado para los 50 bonos más líquidos.
Calcula TIR implícita del ask y diferencia absoluta vs TIR Radar.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from src.analytics.fixed_income import calculate_ytm

MASTER   = Path("outputs/ons_master.csv")
METRICS  = Path("outputs/fixed_income_metrics.csv")
CF_DIR   = Path("data/raw/mae_cashflows")
SETTLE   = pd.Timestamp("today") + pd.Timedelta(days=2)   # aprox. fecha liquidación


def load_master() -> pd.DataFrame:
    df = pd.read_csv(MASTER)
    if df.columns[0].startswith("﻿"):
        df = df.rename(columns={df.columns[0]: "volumen_nominal"})
    # Normalizar ticker/symbol
    if "ticker" not in df.columns and "symbol" in df.columns:
        df = df.rename(columns={"symbol": "ticker"})
    df["precio_compra"]  = pd.to_numeric(df.get("precio_compra",  0), errors="coerce").fillna(0)
    df["precio_venta"]   = pd.to_numeric(df.get("precio_venta",   0), errors="coerce").fillna(0)
    df["ultimo_precio"]  = pd.to_numeric(df.get("ultimo_precio",  0), errors="coerce").fillna(0)
    df["monto_operado"]  = pd.to_numeric(df.get("monto_operado",  0), errors="coerce").fillna(0)
    df["moneda"]         = df.get("moneda", "ARS").fillna("ARS")
    df["tipo_liquidacion"] = pd.to_numeric(df.get("tipo_liquidacion", 2), errors="coerce").fillna(2)
    return df


def ytm_at_price(mae_ticker: str, price_pct: float):
    """Calcula YTM para un bono a un precio dado (% par)."""
    cf_file = CF_DIR / f"{mae_ticker}.json"
    if not cf_file.exists():
        return None
    try:
        data = json.loads(cf_file.read_text())
        cashflows = data.get("cashflows") or data.get("flujos") or []
        if not cashflows:
            return None
        cfs = []
        for cf in cashflows:
            fecha = cf.get("fecha") or cf.get("date") or cf.get("payment_date")
            monto = cf.get("monto") or cf.get("amount") or cf.get("flujo") or 0
            if fecha and monto:
                cfs.append({"date": str(fecha), "amount": float(monto)})
        if not cfs:
            return None
        ytm = calculate_ytm(price_pct, cfs, SETTLE.date())
        return ytm
    except Exception:
        return None


def build_table() -> pd.DataFrame:
    master  = load_master()
    metrics = pd.read_csv(METRICS)

    # --- Top 50 bonos USD más líquidos (en métricas, que tienen cashflows válidos) ---
    # Tomamos todos en métricas y los rankeamos por monto_operado en el ticker fuente
    metrics = metrics.copy()
    metrics["symbol"] = metrics["symbol"].str.strip()
    metrics["price_source"] = metrics["price_source"].str.strip()

    # Para cada bono en métricas, buscar en master el ticker fuente
    rows = []
    for _, m in metrics.iterrows():
        mae  = m["symbol"]        # e.g. CACBO
        src  = m["price_source"]  # e.g. "CACBD (USD)"  o "YM38C (EXT)"
        base = mae[:-1]           # e.g. CACB, YM38

        radar_price = m["price"]
        radar_ytm   = m["ytm"]
        mod_dur     = m["modified_duration"]

        # ── Ticker fuente: extraer sólo el ticker (antes del espacio) ──────────
        src_ticker = src.split()[0] if src and src != "—" else None

        # ── Buscar en master todos los tickers con esa base ────────────────────
        variants = master[master["ticker"].str.startswith(base, na=False)].copy()

        # Filtrar: preferir CI (tipo_liquidacion=2) con precio > 0 para bid/ask
        usd_variants = variants[variants["moneda"].isin(["USD", "EXT"])].copy()

        # Mejor ticker por liquidez (monto_operado más alto, precio > 0)
        liquid_usd = (
            usd_variants[usd_variants["ultimo_precio"] > 0]
            .sort_values("monto_operado", ascending=False)
        )

        # Precio del ticker fuente en master
        src_row = master[master["ticker"] == src_ticker] if src_ticker else pd.DataFrame()
        src_last  = float(src_row["ultimo_precio"].iloc[0])  if not src_row.empty else np.nan
        src_hora  = str(src_row["hora_ultimo_trade"].iloc[0]) if not src_row.empty else "—"
        src_bid   = float(src_row["precio_compra"].iloc[0])   if not src_row.empty else np.nan
        src_ask   = float(src_row["precio_venta"].iloc[0])    if not src_row.empty else np.nan
        src_monto = float(src_row["monto_operado"].iloc[0])   if not src_row.empty else 0.0

        # Ticker más líquido (puede diferir del fuente)
        best_ticker = liquid_usd["ticker"].iloc[0] if not liquid_usd.empty else src_ticker
        best_row    = master[master["ticker"] == best_ticker] if best_ticker else pd.DataFrame()
        best_last   = float(best_row["ultimo_precio"].iloc[0]) if not best_row.empty else np.nan
        best_hora   = str(best_row["hora_ultimo_trade"].iloc[0]) if not best_row.empty else "—"
        best_bid    = float(best_row["precio_compra"].iloc[0])  if not best_row.empty else np.nan
        best_ask    = float(best_row["precio_venta"].iloc[0])   if not best_row.empty else np.nan
        best_monto  = float(best_row["monto_operado"].iloc[0])  if not best_row.empty else 0.0

        # Monto operado total (todas las variantes USD)
        total_monto_usd = usd_variants["monto_operado"].sum()

        # ── TIR implícita del ask (fuente) ────────────────────────────────────
        ytm_ask_src = None
        if src_ask and src_ask > 0:
            ytm_ask_src = ytm_at_price(mae, src_ask)
            if ytm_ask_src is None and mod_dur > 0:
                # fallback: aproximación por duración
                ytm_ask_src = radar_ytm - (src_ask - radar_price) / (mod_dur * radar_price)

        # ── TIR implícita del ask del ticker más líquido ─────────────────────
        ytm_ask_best = None
        if best_ask and best_ask > 0 and best_ticker != src_ticker:
            ytm_ask_best = ytm_at_price(mae, best_ask)
            if ytm_ask_best is None and mod_dur > 0:
                ytm_ask_best = radar_ytm - (best_ask - radar_price) / (mod_dur * radar_price)

        # Diferencia principal: TIR Radar vs TIR ask del ticker fuente
        diff_bp = None
        if ytm_ask_src is not None:
            diff_bp = abs(radar_ytm - ytm_ask_src) * 10_000

        rows.append({
            "Ticker":            mae,
            "Emisor":            str(m.get("issuer", ""))[:30],
            "Fuente Radar":      src_ticker or "—",
            "Precio Radar":      radar_price,
            "Hora precio":       src_hora,
            "Último BYMA":       src_last,
            "Bid":               src_bid if src_bid else np.nan,
            "Ask":               src_ask if src_ask else np.nan,
            "TIR Radar %":       radar_ytm * 100,
            "TIR Ask %":         ytm_ask_src * 100 if ytm_ask_src is not None else np.nan,
            "Δ TIR (bp)":        diff_bp,
            # Info de diagnóstico
            "Ticker más líquido": best_ticker,
            "Monto USD fuente":  src_monto,
            "Monto USD líquido": best_monto,
            "Nota":              "⚠ usar " + str(best_ticker) if best_ticker and best_ticker != src_ticker and best_monto > src_monto * 2 else "",
        })

    df = pd.DataFrame(rows)

    # Ordenar por Δ TIR descendente (mayores diferencias primero)
    df = df.sort_values("Δ TIR (bp)", ascending=False, na_position="last")

    return df


def print_table(df: pd.DataFrame) -> None:
    pd.set_option("display.max_rows", 60)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda x: f"{x:.2f}")

    display_cols = [
        "Ticker", "Fuente Radar", "Precio Radar", "Hora precio",
        "Último BYMA", "Bid", "Ask",
        "TIR Radar %", "TIR Ask %", "Δ TIR (bp)",
        "Ticker más líquido", "Monto USD fuente", "Monto USD líquido", "Nota",
    ]
    print("\n" + "═" * 160)
    print("  DIAGNÓSTICO: Precio Radar vs Mercado")
    print("═" * 160)
    print(df[display_cols].to_string(index=False))
    print("\n")

    # ── Highlight casos especiales ────────────────────────────────────────────
    print("─" * 80)
    print("  CASOS NOTABLES")
    print("─" * 80)
    for ticker in ["MSSFO", "CACBO", "YM38O"]:
        row = df[df["Ticker"] == ticker]
        if row.empty:
            continue
        r = row.iloc[0]
        print(f"\n  {ticker}:")
        print(f"    Fuente Radar:      {r['Fuente Radar']} @ {r['Precio Radar']:.4f}  (hora {r['Hora precio']})")
        print(f"    Último BYMA:       {r['Último BYMA']:.4f}  |  Bid: {r['Bid']:.2f}  Ask: {r['Ask']:.2f}" if pd.notna(r['Último BYMA']) else "    Sin datos bid/ask")
        print(f"    TIR Radar:         {r['TIR Radar %']:.2f}%")
        print(f"    TIR implícita ask: {r['TIR Ask %']:.2f}%" if pd.notna(r['TIR Ask %']) else "    TIR ask: n/d")
        print(f"    Δ TIR:             {r['Δ TIR (bp)']:.0f} bp" if pd.notna(r['Δ TIR (bp)']) else "    Δ TIR: n/d")
        print(f"    Ticker más líquido: {r['Ticker más líquido']}  (monto {r['Monto USD líquido']:,.0f} USD vs fuente {r['Monto USD fuente']:,.0f} USD)")
        if r["Nota"]:
            print(f"    ⚠  {r['Nota']}")


if __name__ == "__main__":
    df = build_table()
    print_table(df)
    df.to_csv("outputs/diagnostico_precios.csv", index=False)
    print(f"  → Exportado: outputs/diagnostico_precios.csv")
