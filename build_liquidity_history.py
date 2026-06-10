"""
Fase 8b: Acumulador de historial de liquidez.

Ejecutar una vez por día hábil (idealmente al cierre del mercado ~18:00 ARS).
Descarga el snapshot actual de BYMA y lo agrega como una fila por bono
al archivo data/processed/liquidity_history.csv.

Con 20+ días de historia se habilitan:
  - days_stale real (días desde la última operación)
  - vol_30d, days_traded_30 para Execution Score completo
  - Percentiles históricos de liquidez

Uso:
    python build_liquidity_history.py
    python build_liquidity_history.py --dry-run    # imprime sin guardar
    python build_liquidity_history.py --from-cache # usa ons_master.csv local
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MASTER_CSV  = Path("outputs/ons_master.csv")
OUTPUT_CSV  = Path("data/processed/liquidity_history.csv")
HISTORY_DIR = Path("data/processed")

# Columnas que guardamos en el historial
HISTORY_COLS = [
    "date", "symbol",
    "monto_operado", "volumen_nominal", "cantidad_operaciones",
    "ultimo_precio", "precio_compra", "precio_venta",
    "spread_abs", "spread_pct",
    "moneda", "tipo_liquidacion",
]


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot desde BYMA en vivo
# ─────────────────────────────────────────────────────────────────────────────

def fetch_live_snapshot() -> pd.DataFrame:
    """Descarga snapshot actual de BYMA."""
    from src.byma.client import BYMAClient
    from src.byma.ons import get_ons

    client = BYMAClient()
    df = get_ons(client)
    if df.empty:
        raise ValueError("BYMA API retornó vacío")
    logger.info(f"  Snapshot BYMA: {len(df)} filas")
    return df


def load_from_cache() -> pd.DataFrame:
    """Carga snapshot desde ons_master.csv (ya calculado en el pipeline)."""
    if not MASTER_CSV.exists():
        raise FileNotFoundError(f"{MASTER_CSV} no existe — correr el pipeline completo primero")
    df = pd.read_csv(MASTER_CSV)
    logger.info(f"  Cargado desde cache: {len(df)} filas")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Normalización y guardado
# ─────────────────────────────────────────────────────────────────────────────

def normalize_snapshot(df: pd.DataFrame, today: date) -> pd.DataFrame:
    """
    Normaliza el snapshot para guardarlo en el historial.
    La columna 'symbol' unifica el nombre del ticker (renombra 'ticker' si aplica).
    """
    df = df.copy()

    if "ticker" in df.columns and "symbol" not in df.columns:
        df = df.rename(columns={"ticker": "symbol"})

    df["date"] = pd.Timestamp(today)

    num_cols = ["monto_operado", "volumen_nominal", "cantidad_operaciones",
                "ultimo_precio", "precio_compra", "precio_venta",
                "spread_abs", "spread_pct"]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    keep = [c for c in HISTORY_COLS if c in df.columns]
    return df[keep]


def append_to_history(snapshot: pd.DataFrame, dry_run: bool = False) -> None:
    """
    Agrega el snapshot al CSV de historial.
    Si el historial ya tiene datos para la fecha de hoy, los omite (idempotente).
    """
    today_ts = snapshot["date"].iloc[0]

    if OUTPUT_CSV.exists():
        existing = pd.read_csv(OUTPUT_CSV, parse_dates=["date"])
        # Verificar si ya existe data de hoy
        if not existing.empty and (existing["date"] == today_ts).any():
            logger.info(f"Historial ya contiene datos para {today_ts.date()} — skip (idempotente)")
            return
        combined = pd.concat([existing, snapshot], ignore_index=True)
    else:
        combined = snapshot

    n_days  = combined["date"].nunique()
    n_bonds = combined["symbol"].nunique()

    if dry_run:
        logger.info(f"[DRY RUN] Se agregarían {len(snapshot)} filas → historial total: {len(combined)} filas")
        logger.info(f"[DRY RUN] Cobertura: {n_days} días, {n_bonds} bonos")
        return

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_csv(OUTPUT_CSV, index=False)
    logger.info(f"Historial actualizado: {OUTPUT_CSV}")
    logger.info(f"  Total: {len(combined)} filas | {n_days} días | {n_bonds} bonos")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Acumulador de historial de liquidez")
    ap.add_argument("--dry-run",    action="store_true", help="Imprime sin guardar")
    ap.add_argument("--from-cache", action="store_true",
                    help="Usa ons_master.csv en lugar de llamar a BYMA en vivo")
    args = ap.parse_args()

    today = date.today()
    logger.info(f"Snapshot de liquidez: {today}")

    if args.from_cache:
        raw = load_from_cache()
    else:
        raw = fetch_live_snapshot()

    snapshot = normalize_snapshot(raw, today)
    logger.info(f"  Filas normalizadas: {len(snapshot)}")

    append_to_history(snapshot, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
