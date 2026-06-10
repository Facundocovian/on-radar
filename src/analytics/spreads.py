"""
Cálculo de métricas de relative value para ONs.

MVP 1:
  - spread_abs: offerPrice - bidPrice  (spread bid-ask en puntos de precio)
  - spread_pct: spread_abs / bidPrice * 100  (spread como % del bid)
  - years_to_maturity: daysToMaturity / 365.25

Nota: el endpoint público de BYMA no incluye tasa de cupón.
Los spreads de rendimiento (Z-spread, ASW) quedan para Fase 2 con datos de MAE/TIR.

Stubs preparados para fases futuras: calc_tir, calc_duration, calc_dv01.
"""

import logging
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)


def calc_years_to_maturity(df: pd.DataFrame) -> pd.Series:
    """Años al vencimiento usando daysToMaturity de BYMA."""
    if "dias_al_vencimiento" in df.columns:
        return (df["dias_al_vencimiento"] / 365.25).round(4)
    # Fallback: calcular desde fecha_vencimiento
    if "fecha_vencimiento" in df.columns:
        today = pd.Timestamp.today().normalize()
        delta = df["fecha_vencimiento"] - today
        return (delta.dt.days / 365.25).round(4)
    return pd.Series(pd.NA, index=df.index, dtype="Float64")


def calc_spread_abs(df: pd.DataFrame) -> pd.Series:
    """
    Spread bid-ask absoluto en puntos de precio.
    Solo se calcula cuando ambos bid y ask son positivos.
    """
    if "precio_compra" not in df.columns or "precio_venta" not in df.columns:
        return pd.Series(pd.NA, index=df.index, dtype="Float64")

    mask = (df["precio_compra"] > 0) & (df["precio_venta"] > 0)
    result = pd.Series(pd.NA, index=df.index, dtype="Float64")
    result[mask] = (df.loc[mask, "precio_venta"] - df.loc[mask, "precio_compra"]).round(4)
    return result


def calc_spread_pct(df: pd.DataFrame) -> pd.Series:
    """
    Spread bid-ask como porcentaje del precio de compra.
    spread_pct = (ask - bid) / bid * 100
    """
    if "precio_compra" not in df.columns or "precio_venta" not in df.columns:
        return pd.Series(pd.NA, index=df.index, dtype="Float64")

    mask = (df["precio_compra"] > 0) & (df["precio_venta"] > 0)
    result = pd.Series(pd.NA, index=df.index, dtype="Float64")
    result[mask] = (
        (df.loc[mask, "precio_venta"] - df.loc[mask, "precio_compra"])
        / df.loc[mask, "precio_compra"]
        * 100
    ).round(4)
    return result


def calc_liquidity_score(df: pd.DataFrame) -> pd.Series:
    """
    Score compuesto 0-100 que combina tres señales de liquidez:
      - monto_operado      → 50% (volumen en ARS/USD)
      - spread_pct inverso → 35% (spread menor = más líquido)
      - cantidad_operaciones → 15% (frecuencia de trades)

    Se usa percentile rank para que sea comparable entre monedas.
    Registros sin bid/ask reciben 0 en el componente de spread.
    """
    weights = {"vol": 0.50, "spread": 0.35, "ops": 0.15}
    score = pd.Series(0.0, index=df.index)
    actual_weight = 0.0

    if "monto_operado" in df.columns:
        vol = df["monto_operado"].fillna(0)
        score += vol.rank(pct=True) * weights["vol"]
        actual_weight += weights["vol"]

    if "spread_pct" in df.columns:
        max_sp = df["spread_pct"].max(skipna=True)
        fill_val = (max_sp * 2) if pd.notna(max_sp) else 100.0
        sp = df["spread_pct"].fillna(fill_val)
        score += (1 - sp.rank(pct=True)) * weights["spread"]
        actual_weight += weights["spread"]

    if "cantidad_operaciones" in df.columns:
        ops = df["cantidad_operaciones"].fillna(0)
        score += ops.rank(pct=True) * weights["ops"]
        actual_weight += weights["ops"]

    if actual_weight > 0:
        score = (score / actual_weight * 100).round(2)

    return score


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    df["years_to_maturity"] = calc_years_to_maturity(df)
    df["spread_abs"] = calc_spread_abs(df)
    df["spread_pct"] = calc_spread_pct(df)
    df["liquidity_score"] = calc_liquidity_score(df)

    logger.info("Métricas calculadas: years_to_maturity, spread_abs, spread_pct, liquidity_score")
    return df


# ---------------------------------------------------------------------------
# Stubs Fase 2
# ---------------------------------------------------------------------------

def calc_tir(df: pd.DataFrame) -> pd.Series:
    """[TODO Fase 2] TIR / yield to maturity usando flujo de fondos real."""
    raise NotImplementedError

def calc_duration(df: pd.DataFrame) -> pd.Series:
    """[TODO Fase 2] Duration modificada."""
    raise NotImplementedError

def calc_dv01(df: pd.DataFrame) -> pd.Series:
    """[TODO Fase 2] DV01 (dollar value of 1bp)."""
    raise NotImplementedError
