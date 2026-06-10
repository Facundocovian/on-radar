"""
Construye data/issuer_master.csv desde el PDF oficial de AFIP/ARCA.

Uso:
    python build_issuer_master.py                    # descarga + parsea
    python build_issuer_master.py --only-manual      # solo usa CSV base
    python build_issuer_master.py --pdf <ruta.pdf>   # PDF ya descargado
    python build_issuer_master.py --report-only      # solo reporte de cobertura
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from src.afip.downloader import download, _is_valid_pdf
from src.afip.parser import parse_pdf, to_issuer_master

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PDF_PATH      = Path("data/raw/afip_ons_exentas_2025.pdf")
MANUAL_CSV    = Path("data/issuer_master.csv")
OUTPUT_CSV    = Path("data/issuer_master.csv")
BYMA_MASTER   = Path("outputs/ons_master.csv")
RAW_EXPORT    = Path("data/processed/afip_ons_raw.csv")

CURRENCY_SUFFIXES = set("ODCPXYZLB")
UNMAPPED_CSV      = Path("outputs/unmapped_prefixes.csv")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_prefix(ticker: str) -> str:
    t = ticker.strip().upper()
    if t and t[-1] in CURRENCY_SUFFIXES:
        t = t[:-1]
    return t[:3] if len(t) >= 3 else t


def load_manual_base() -> pd.DataFrame:
    if not MANUAL_CSV.exists():
        return pd.DataFrame(columns=["prefijo", "cuit_emisor", "razon_social", "sector", "fuente"])
    df = pd.read_csv(MANUAL_CSV, comment="#", dtype=str).fillna("")
    logger.info(f"Base manual cargada: {len(df)} entradas desde {MANUAL_CSV}")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def build_from_pdf(pdf_path: Path) -> pd.DataFrame:
    """Parsea el PDF y construye el DataFrame de prefijos."""
    df_raw = parse_pdf(pdf_path)
    if df_raw.empty:
        logger.warning("Parser retornó DataFrame vacío")
        return pd.DataFrame()

    # Exportar raw para auditoría
    RAW_EXPORT.parent.mkdir(parents=True, exist_ok=True)
    df_raw.to_csv(RAW_EXPORT, index=False, encoding="utf-8-sig")
    logger.info(f"Raw exportado: {RAW_EXPORT} ({len(df_raw)} filas)")

    return to_issuer_master(df_raw)


# ─────────────────────────────────────────────────────────────────────────────
# Coverage report
# ─────────────────────────────────────────────────────────────────────────────

def coverage_report(master: pd.DataFrame, byma_df: pd.DataFrame, top_n: int = 50, export: bool = True) -> None:
    if byma_df.empty or "ticker" not in byma_df.columns:
        print("  Sin datos de BYMA para comparar.")
        return

    # Prefijos del master ordenados por longitud (longest-first)
    prefixes_sorted = sorted(
        master["prefijo"].str.upper().dropna().tolist(),
        key=len, reverse=True,
    )

    def is_mapped(ticker: str) -> bool:
        t = ticker.strip().upper()
        if t and t[-1] in CURRENCY_SUFFIXES:
            t = t[:-1]
        return any(t.startswith(p) for p in prefixes_sorted)

    byma = byma_df.copy()
    byma["prefijo3"]  = byma["ticker"].apply(get_prefix)
    byma["is_mapped"] = byma["ticker"].apply(is_mapped)
    byma["monto_operado"] = pd.to_numeric(byma.get("monto_operado", 0), errors="coerce").fillna(0)

    total     = len(byma)
    n_mapped  = byma["is_mapped"].sum()
    vol_total = byma["monto_operado"].sum()
    vol_map   = byma.loc[byma["is_mapped"], "monto_operado"].sum()

    sep = "=" * 65
    print(f"\n{sep}")
    print(f"  COBERTURA issuer_master.csv  ({len(master)} prefijos)")
    print(sep)
    print(f"  ONs en BYMA master        : {total:>6,}")
    print(f"  ONs identificadas         : {n_mapped:>6,}  ({n_mapped/total*100:.1f}%)")
    print(f"  ONs sin identificar       : {total-n_mapped:>6,}  ({(total-n_mapped)/total*100:.1f}%)")
    if vol_total > 0:
        print(f"  Volumen cubierto          : {vol_map/vol_total*100:.1f}%")
        print(f"  Volumen sin cubrir        : {(vol_total-vol_map)/vol_total*100:.1f}%")

    # ── Top N tickers sin mapear por volumen ──────────────────────────────
    unmapped_tickers = (
        byma[~byma["is_mapped"]]
        [["ticker", "moneda", "tipo_liquidacion", "monto_operado", "prefijo3"]]
        .sort_values("monto_operado", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    ) if "moneda" in byma.columns else (
        byma[~byma["is_mapped"]][["ticker", "monto_operado"]]
        .sort_values("monto_operado", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )

    print(f"\n{'─'*65}")
    print(f"  Top {top_n} tickers sin identificar (por volumen):")
    print(f"{'─'*65}")
    if unmapped_tickers.empty:
        print("  (todos los tickers están identificados)")
    else:
        print(unmapped_tickers.to_string(index=True))

    # ── Top N prefijos sin mapear por volumen ─────────────────────────────
    unmapped_prefixes = (
        byma[~byma["is_mapped"]]
        .groupby("prefijo3")
        .agg(
            n_tickers=("ticker", "nunique"),
            vol_total=("monto_operado", "sum"),
        )
        .sort_values("vol_total", ascending=False)
        .reset_index()
    )

    # ── Export unmapped prefixes CSV ──────────────────────────────────────────
    if export and not unmapped_prefixes.empty:
        UNMAPPED_CSV.parent.mkdir(parents=True, exist_ok=True)
        unmapped_prefixes.to_csv(UNMAPPED_CSV, index=False)
        logger.info(f"Exportado: {UNMAPPED_CSV} ({len(unmapped_prefixes)} prefijos sin mapear)")

    print(f"\n{'─'*65}")
    print(f"  Top {top_n} prefijos sin identificar (por volumen):")
    print(f"{'─'*65}")
    if unmapped_prefixes.empty:
        print("  (todos los prefijos están identificados)")
    else:
        display = unmapped_prefixes.head(top_n).copy()
        display["vol_total"] = display["vol_total"].map("{:,.0f}".format)
        print(display.to_string(index=False))

    print(f"\n{sep}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Construir issuer_master desde AFIP")
    parser.add_argument("--pdf",         help="Ruta al PDF descargado manualmente")
    parser.add_argument("--only-manual", action="store_true", help="No intentar descarga")
    parser.add_argument("--report-only", action="store_true", help="Solo mostrar reporte")
    args = parser.parse_args()

    manual_df = load_manual_base()

    if args.report_only:
        if BYMA_MASTER.exists():
            coverage_report(manual_df, pd.read_csv(BYMA_MASTER))
        else:
            print(f"  No existe {BYMA_MASTER}. Correr primero: python main.py")
        return

    # ── 1. Obtener el PDF ─────────────────────────────────────────────────
    pdf_path = Path(args.pdf) if args.pdf else PDF_PATH
    official_df = pd.DataFrame()

    if not args.only_manual:
        pdf_ok = _is_valid_pdf(pdf_path) or download(pdf_path)
        if pdf_ok:
            official_df = build_from_pdf(pdf_path)

    # ── 2. Combinar: oficial tiene prioridad ──────────────────────────────
    if not official_df.empty:
        covered = set(official_df["prefijo"].str.upper())
        extra_manual = manual_df[~manual_df["prefijo"].str.upper().isin(covered)]
        combined = pd.concat([official_df, extra_manual], ignore_index=True)
        logger.info(
            f"Combinado: {len(official_df)} prefijos del PDF "
            f"+ {len(extra_manual)} entradas manuales adicionales"
        )
    else:
        combined = manual_df
        logger.info(f"Usando solo base manual: {len(combined)} entradas")

    combined = (
        combined
        .sort_values("prefijo")
        .drop_duplicates(subset=["prefijo"])
        .reset_index(drop=True)
    )

    # ── 3. Exportar CSV ───────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", encoding="utf-8") as f:
        f.write(
            "# Tabla maestra de emisores de ONs — ON Radar\n"
            "# Generado por build_issuer_master.py\n"
            "# prefijo,cuit_emisor,razon_social,sector,fuente\n"
        )
        combined.to_csv(f, index=False)
    logger.info(f"Exportado: {OUTPUT_CSV}  ({len(combined)} prefijos)")

    # ── 4. Reporte de cobertura ───────────────────────────────────────────
    if BYMA_MASTER.exists():
        coverage_report(combined, pd.read_csv(BYMA_MASTER))
    else:
        logger.info(f"Sin {BYMA_MASTER} — correr 'python main.py' para generarlo")


if __name__ == "__main__":
    main()
