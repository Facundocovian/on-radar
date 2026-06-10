"""
Motor de renta fija: TIR, Macaulay Duration, Modified Duration, Current Yield.

Convenciones:
  - Precios y cashflows en % de valor par (ej: 102.50 para 102.5% del par)
  - Tiempos en años, base Actual/365
  - Descuento semi-anual (frequency=2) por defecto — típico para ONs argentinas USD
  - TIR expresada como tasa anual nominal
  - Sin dependencias externas (solo stdlib)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Normalización de datos MAE
# ─────────────────────────────────────────────────────────────────────────────

def normalize_cashflows(mae_json: dict) -> List[Dict]:
    """
    Convierte respuesta de MAE /emisiones/flujofondos a lista de dicts.

    Cada dict contiene:
      date         : date — fecha de pago
      cashflow     : float — flujo total como % del par original  (renta + amortización)
      coupon       : float — componente de renta como % del par original
      amortization : float — componente de amortización como % del par original
      vr           : float — valor residual como % del par original en ese cupón
      numero_cupon : str
    """
    result = []
    for item in mae_json.get("detalle", []):
        fecha_str = item.get("fechaPago", "")
        try:
            fecha = datetime.fromisoformat(fecha_str.split("T")[0]).date()
        except (ValueError, AttributeError):
            logger.warning(f"  Fecha inválida en MAE JSON: {fecha_str!r} — skipped")
            continue
        result.append({
            "date":         fecha,
            "cashflow":     float(item.get("cashFlow",     0)),
            "coupon":       float(item.get("renta",        0)),
            "amortization": float(item.get("amortizacion", 0)),
            "vr":           float(item.get("vr",           0)),
            "numero_cupon": str(item.get("numeroCupon",    "")),
        })
    result = sorted(result, key=lambda x: x["date"])

    # MAE sometimes returns coupon-only schedules for bullet bonds (final 100
    # amortization is missing from the API response).  If every cashflow has
    # zero amortization and the last VR is still 100, patch the final entry.
    if result:
        total_amort = sum(cf["amortization"] for cf in result)
        if total_amort == 0.0 and result[-1]["vr"] >= 99.0:
            result[-1]["amortization"] = 100.0
            result[-1]["cashflow"]    += 100.0
            logger.info("  normalize_cashflows: patched missing final amortization (bullet bond)")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────────────────────────────────────

def _future_cashflows(
    cashflows: List[Dict], settlement: date
) -> List[Tuple[float, float]]:
    """
    Filtra cashflows con fecha estrictamente posterior a settlement.
    Retorna lista de (cashflow_pct_par, tiempo_en_años_act365).
    """
    return [
        (cf["cashflow"], (cf["date"] - settlement).days / 365.0)
        for cf in cashflows
        if cf["date"] > settlement
    ]


def _pv_sum(ytm: float, fut: List[Tuple[float, float]], frequency: int) -> float:
    """Suma de valores presentes dado ytm anual nominal y convención de frecuencia."""
    r = ytm / frequency
    return sum(cf / (1.0 + r) ** (frequency * t) for cf, t in fut)


# ─────────────────────────────────────────────────────────────────────────────
# Motor financiero
# ─────────────────────────────────────────────────────────────────────────────

def calculate_ytm(
    price: float,
    cashflows: List[Dict],
    settlement_date: date,
    frequency: int = 2,
) -> Optional[float]:
    """
    Calcula YTM (Yield to Maturity) anual nominal por bisección.

    Resuelve:  P = Σ CF_i / (1 + ytm/f)^(f·t_i)

    price           : precio limpio como % del par (ej: 102.50)
    cashflows       : lista de normalize_cashflows()
    settlement_date : fecha de liquidación (T+2 normalmente)
    frequency       : periodos por año (2 = semi-anual)

    Returns YTM anual nominal como decimal (ej: 0.0682 para 6.82%).
    """
    fut = _future_cashflows(cashflows, settlement_date)
    if not fut:
        logger.warning("  YTM: no hay cashflows futuros")
        return None

    def f(y: float) -> float:
        return _pv_sum(y, fut, frequency) - price

    lo, hi = -0.999, 50.0
    f_lo, f_hi = f(lo), f(hi)

    if f_lo * f_hi > 0:
        logger.warning(
            f"  YTM: la función no cambia de signo en [{lo}, {hi}] "
            f"— f(lo)={f_lo:.4f} f(hi)={f_hi:.4f}"
        )
        return None

    for _ in range(120):
        if abs(hi - lo) < 1e-9:
            break
        mid = (lo + hi) / 2.0
        f_mid = f(mid)
        if f_mid * f_lo <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid

    return (lo + hi) / 2.0


def calculate_macaulay_duration(
    price: float,
    cashflows: List[Dict],
    ytm: float,
    settlement_date: date,
    frequency: int = 2,
) -> Optional[float]:
    """
    Calcula Macaulay Duration en años.

    MD = Σ( t_i · PV_i ) / P

    donde PV_i = CF_i / (1 + ytm/f)^(f·t_i)
    """
    fut = _future_cashflows(cashflows, settlement_date)
    if not fut or price <= 0:
        return None

    r = ytm / frequency
    weighted = sum(
        t * cf / (1.0 + r) ** (frequency * t)
        for cf, t in fut
    )
    return weighted / price


def calculate_modified_duration(
    macaulay_duration: float,
    ytm: float,
    frequency: int = 2,
) -> float:
    """
    Modified Duration = Macaulay Duration / (1 + ytm/f)

    Sensibilidad del precio ante variación de 1pp en YTM:
      ΔP/P ≈ -ModD × Δytm
    """
    return macaulay_duration / (1.0 + ytm / frequency)


def calculate_current_yield(
    annual_coupon_pct: float,
    price: float,
) -> Optional[float]:
    """
    Current Yield = cupón anual (% del par original) / precio (% del par).

    annual_coupon_pct : cupón anual como % del par original (ej: 7.0 para 7%)
    price             : precio como % del par (ej: 102.50)

    Returns yield como decimal (ej: 0.0683 para 6.83%).
    """
    if price <= 0:
        return None
    return annual_coupon_pct / price
