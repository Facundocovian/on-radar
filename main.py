"""
Entry point del ON Radar.

Uso normal (sin credenciales):
    python main.py

Modo demo (datos de muestra, sin conexión):
    python main.py --demo
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from src.byma.client import BYMAClient
from src.byma.ons import get_ons, normalize_ons
from src.byma.demo_data import SAMPLE_ONS
from src.analytics.spreads import enrich
from src.analytics.issuer import load_issuer_map, enrich_issuers, build_top_issuers
from src.analytics.exports import (
    export_top_liquid,
    export_top_spreads,
    export_maturity_buckets,
    export_ons_enriched,
    export_top_issuers,
    export_excel,
)
from src.analytics.report import print_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Outputs
OUTPUT_MASTER   = Path("outputs/ons_master.csv")
OUTPUT_ENRICHED = Path("outputs/ons_enriched.csv")
OUTPUT_ISSUERS  = Path("outputs/top_issuers.csv")
OUTPUT_LIQUID   = Path("outputs/top_liquid.csv")
OUTPUT_SPREADS  = Path("outputs/top_spreads.csv")
OUTPUT_BUCKETS  = Path("outputs/maturity_buckets.csv")
OUTPUT_EXCEL    = Path("outputs/ons_summary.xlsx")

ISSUER_MAP_CSV  = Path("data/issuer_master.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ON Radar")
    parser.add_argument("--demo", action="store_true", help="Usar datos de muestra sin conexión")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # 1. Descarga / demo
    if args.demo:
        logger.info("Modo DEMO — usando datos de muestra")
        df = normalize_ons(SAMPLE_ONS)
    else:
        client = BYMAClient()
        df = get_ons(client)

    if df.empty:
        logger.error("No se obtuvieron datos.")
        sys.exit(1)

    # 2. Métricas de mercado
    df = enrich(df)

    # 3. Enriquecimiento de emisores
    issuer_map = load_issuer_map(ISSUER_MAP_CSV)
    df = enrich_issuers(df, issuer_map)
    df_issuers = build_top_issuers(df)

    # 4. Exports CSV
    Path("outputs").mkdir(exist_ok=True)

    df_master = df.sort_values(
        ["monto_operado", "spread_pct", "years_to_maturity"],
        ascending=[False, True, True],
        na_position="last",
    )
    df_master.to_csv(OUTPUT_MASTER, index=False, encoding="utf-8-sig")
    logger.info(f"Exportado: {OUTPUT_MASTER}  ({len(df_master)} filas)")

    export_ons_enriched(df, OUTPUT_ENRICHED)
    export_top_issuers(df_issuers, OUTPUT_ISSUERS)
    export_top_liquid(df, OUTPUT_LIQUID)
    export_top_spreads(df, OUTPUT_SPREADS)
    export_maturity_buckets(df, OUTPUT_BUCKETS)
    export_excel(df, OUTPUT_EXCEL)

    # 5. Reporte consola
    print_report(df)
    _print_issuers_report(df_issuers)


def _print_issuers_report(df: pd.DataFrame) -> None:
    sep = "=" * 70
    print(f"\n{sep}")
    print("  TOP EMISORES — DOMINANCIA DEL MERCADO")
    print(sep)

    mapped   = df[df["is_mapped"]]
    unmapped = df[~df["is_mapped"]]
    total_vol = df["volumen_total"].sum()

    print(f"\n  Emisores identificados : {len(mapped)}")
    print(f"  Grupos sin mapear      : {len(unmapped)}")
    vol_mapped_pct = mapped["volumen_total"].sum() / total_vol * 100 if total_vol else 0
    print(f"  Volumen cubierto       : {vol_mapped_pct:.1f}%\n")

    cols = [c for c in [
        "issuer", "sector", "cantidad_ons", "con_trade",
        "volumen_total", "liquidity_score_promedio", "spread_pct_median",
    ] if c in df.columns]

    print(f"{'EMISOR':<45} {'SECTOR':<25} {'ONs':>4} {'Trades':>6} {'Vol Total':>14} {'Liq':>6} {'Sprd%':>6}")
    print("-" * 110)
    for _, row in df.head(30).iterrows():
        print(
            f"{str(row.get('issuer','')):<45} "
            f"{str(row.get('sector','')):<25} "
            f"{int(row.get('cantidad_ons',0)):>4} "
            f"{int(row.get('con_trade',0)):>6} "
            f"{row.get('volumen_total',0):>14,.0f} "
            f"{row.get('liquidity_score_promedio',0):>6.1f} "
            f"{row.get('spread_pct_median',0):>6.2f}"
        )
    print(f"\n{sep}\n")


if __name__ == "__main__":
    main()
