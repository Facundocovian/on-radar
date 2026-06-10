"""
Reporte de consola para el MVP de ON Radar.
"""

import pandas as pd


def print_report(df: pd.DataFrame, top_n: int = 20) -> None:
    if df.empty:
        print("\n[!] DataFrame vacío — no hay datos para reportar.\n")
        return

    sep = "=" * 70

    print(f"\n{sep}")
    print("  RADAR ONs — BYMA OPEN DATA")
    print(sep)

    # --- Resumen general ---
    total = len(df)
    con_trade = int((df["ultimo_precio"] > 0).sum()) if "ultimo_precio" in df.columns else 0
    con_bid_ask = int(
        ((df.get("precio_compra", pd.Series(0)) > 0) &
         (df.get("precio_venta", pd.Series(0)) > 0)).sum()
    )

    print(f"\n  Total ONs descargadas  : {total:>6}")
    print(f"  Con trade > 0          : {con_trade:>6}")
    print(f"  Con bid y ask          : {con_bid_ask:>6}")

    # --- Distribución por moneda ---
    if "moneda" in df.columns:
        print("\n--- Distribución por moneda ---")
        dist = df["moneda"].value_counts()
        for moneda, count in dist.items():
            pct = count / total * 100
            print(f"  {moneda:<10} {count:>5}  ({pct:.1f}%)")

    # --- Top N por liquidity_score ---
    if "liquidity_score" in df.columns:
        cols_liq = [c for c in ["ticker", "moneda", "tipo_liquidacion", "monto_operado",
                                "spread_pct", "years_to_maturity", "liquidity_score"] if c in df.columns]
        top_liq = (
            df[cols_liq]
            .query("monto_operado > 0")
            .sort_values("liquidity_score", ascending=False)
            .head(top_n)
        )
        if not top_liq.empty:
            print(f"\n--- Top {top_n} por liquidity_score ---")
            print(top_liq.to_string(index=False))

    # --- Top N por monto operado ---
    if "monto_operado" in df.columns:
        cols_vol = [c for c in ["ticker", "moneda", "tipo_liquidacion", "ultimo_precio",
                                "monto_operado", "years_to_maturity"] if c in df.columns]
        top_vol = (
            df[cols_vol]
            .query("monto_operado > 0")
            .sort_values("monto_operado", ascending=False)
            .head(top_n)
        )
        if not top_vol.empty:
            print(f"\n--- Top {top_n} por monto operado ---")
            print(top_vol.to_string(index=False))

    # --- Top N menor spread bid-ask (más líquidas) ---
    if "spread_abs" in df.columns:
        cols_sp = [c for c in ["ticker", "moneda", "tipo_liquidacion", "precio_compra",
                               "precio_venta", "spread_abs", "spread_pct",
                               "years_to_maturity"] if c in df.columns]
        df_spread = df[cols_sp].dropna(subset=["spread_abs"])

        top_tight = df_spread.sort_values("spread_abs").head(top_n)
        if not top_tight.empty:
            print(f"\n--- Top {top_n} menor spread bid-ask (más líquidas) ---")
            print(top_tight.to_string(index=False))

        top_wide = df_spread.sort_values("spread_abs", ascending=False).head(top_n)
        if not top_wide.empty:
            print(f"\n--- Top {top_n} mayor spread bid-ask ---")
            print(top_wide.to_string(index=False))

    print(f"\n{sep}\n")
