"""
Generador de cashflows manuales para ONs clase A.

Lee parámetros de cada bono (tasa, frecuencia, esquema de amortización),
genera un schedule completo en formato MAE y lo guarda en
data/raw/mae_cashflows/{base}O.json — solo si el archivo actual está vacío/null.

El pipeline existente (build_fixed_income_metrics.py --all) los levanta
automáticamente en el siguiente run.

Uso:
    python build_cashflows_manual.py              # genera todos los clase A
    python build_cashflows_manual.py --dry-run    # muestra sin escribir
    python build_cashflows_manual.py --force      # sobreescribe aunque haya MAE real
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MAE_CACHE_DIR = Path("data/raw/mae_cashflows")
ENRICHED_CSV  = Path("outputs/ons_enriched.csv")
RATINGS_CSV   = Path("data/ratings_master.csv")
RECOVERY_CSV  = Path("outputs/cashflow_recovery_plan.csv")


# ─────────────────────────────────────────────────────────────────────────────
# Parámetros de bonos clase A
#
# amort_type:
#   "bullet"  — 100% al vencimiento
#   "equal_n" — N amortizaciones iguales (100/N % cada una), una por período,
#               tomando los últimos N pagos del schedule (hacia maturity)
#
# current_vr: % del par original vigente hoy (default 100.0).
#   Para CACB: 3 de 8 cuotas de amortización ya pagadas → VR = 62.5%.
#   Se almacena en el JSON para que build_fixed_income_metrics ajuste el precio.
#
# coupon_confidence: "confirmed" | "estimated"
# ─────────────────────────────────────────────────────────────────────────────

BOND_PARAMS: Dict[str, Dict] = {
    # ── Telecom Argentina ──────────────────────────────────────────────────
    "TLCP": {
        "issuer":     "Telecom Argentina S.A.",
        "currency":   "USD",
        "coupon":     0.0700,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.23 local, 7.00%, bullet. Fuente: aviso resultados.",
    },
    "TLCW": {
        "issuer":     "Telecom Argentina S.A.",
        "currency":   "USD",
        "coupon":     0.0925,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "estimated",
        "notes":      "Cl.24 intl 9.25% — amort schedule pendiente confirmación; usando bullet.",
    },
    # ── Pluspetrol ─────────────────────────────────────────────────────────
    "PLC4": {
        "issuer":     "Pluspetrol S.A.",
        "currency":   "USD",
        "coupon":     0.0850,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.4, US$650M, 8.50% semi-annual bullet. Confirmado.",
    },
    "PLC3": {
        "issuer":     "Pluspetrol S.A.",
        "currency":   "USD",
        "coupon":     0.0600,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "estimated",
        "notes":      "Cl.3, ~6%, semi-annual bullet. Tasa estimada.",
    },
    # ── Tarjeta Naranja ─────────────────────────────────────────────────────
    "T671": {
        "issuer":     "Tarjeta Naranja S.A.U.",
        "currency":   "USD",
        "coupon":     0.0790,
        "frequency":  4,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.LXIV Ser.I, 7.90% quarterly bullet. Confirmado.",
    },
    # ── Pan American Energy ─────────────────────────────────────────────────
    "PNEC": {
        "issuer":     "Pan American Energy LLC (PAE Sucursal)",
        "currency":   "USD",
        "coupon":     0.0850,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "estimated",
        "notes":      "8.50% semi-annual. Amort schedule incierto → usando bullet al vto.",
    },
    # ── Tecpetrol ──────────────────────────────────────────────────────────
    "TTCE": {
        "issuer":     "Tecpetrol S.A.",
        "currency":   "USD",
        "coupon":     0.0500,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "estimated",
        "notes":      "~5.00% semi-annual bullet. Mapeo clase incierto (diff=3).",
    },
    "TTC9": {
        "issuer":     "Tecpetrol S.A.",
        "currency":   "USD",
        "coupon":     0.0680,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.9, AR0482976963, 6.80% semi-annual bullet. Confirmado.",
    },
    # ── Petroquímica Comodoro Rivadavia ─────────────────────────────────────
    "PQCS": {
        "issuer":     "Petroquímica Comodoro Rivadavia S.A.",
        "currency":   "USD",
        "coupon":     0.0800,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.S, US$65M, 8.00% semi-annual bullet. Confirmado.",
    },
    # ── YPF ────────────────────────────────────────────────────────────────
    "YM39": {
        "issuer":     "YPF S.A.",
        "currency":   "USD",
        "coupon":     0.0875,
        "frequency":  4,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.XXXIX, 8.75% TNA quarterly bullet. Confirmado.",
    },
    "YM38": {
        "issuer":     "YPF S.A.",
        "currency":   "USD",
        "coupon":     0.0750,
        "frequency":  4,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.XXXVIII, US$100M, 7.50% quarterly bullet. Confirmado.",
    },
    "YM37": {
        "issuer":     "YPF S.A.",
        "currency":   "USD",
        "coupon":     0.0700,
        "frequency":  4,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.XXXVII, US$139.5M, 7.00% quarterly bullet. Confirmado.",
    },
    "YMCY": {
        "issuer":     "YPF S.A.",
        "currency":   "USD",
        "coupon":     0.0650,
        "frequency":  4,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.XXXV (est.), 6.50% quarterly bullet. Confirmado via market.",
    },
    # ── MSU Energy ─────────────────────────────────────────────────────────
    "MSSF": {
        "issuer":     "MSU S.A.",
        "currency":   "USD",
        "coupon":     0.0750,
        "frequency":  4,
        "amort_type": "equal_n",
        "amort_n":    2,
        "current_vr": 100.0,
        "confidence": "confirmed",
        "notes":      "Cl.XIII, 7.50% quarterly, 2×50%: últimas 2 fechas (abr-2027, jul-2027).",
    },
    # ── EDEMSA ─────────────────────────────────────────────────────────────
    "OZC3": {
        "issuer":     "EDEMSA",
        "currency":   "USD",
        "coupon":     0.0800,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.3, 8.00% semi-annual bullet. Confirmado.",
    },
    # ── Capex ──────────────────────────────────────────────────────────────
    "CACB": {
        "issuer":     "Capex S.A.",
        "currency":   "USD",
        "coupon":     0.0925,
        "frequency":  2,
        "amort_type": "equal_n",
        "amort_n":    8,
        "current_vr": 62.5,  # 3 de 8 cuotas ya pagadas (dic-24, jun-25, dic-25)
        "confidence": "confirmed",
        "notes":      "Cl.V, 9.25%, 8×12.5% semi-annual. VR actual 62.5%. Precio BYMA en % VR residual.",
    },
    # ── Cresud ─────────────────────────────────────────────────────────────
    "CS48": {
        "issuer":     "Cresud S.A.C.I.F. y A.",
        "currency":   "USD",
        "coupon":     0.0800,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.XLVIII, 8.00% semi-annual bullet. Confirmado.",
    },
    "CS45": {
        "issuer":     "Cresud S.A.C.I.F. y A.",
        "currency":   "USD",
        "coupon":     0.0600,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.XLV, US$10.2M, 6.00% semi-annual bullet. Confirmado.",
    },
    "CS49": {
        "issuer":     "Cresud S.A.C.I.F. y A.",
        "currency":   "USD",
        "coupon":     0.0725,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.XLIX, 7.25% semi-annual bullet. Confirmado.",
    },
    # ── Pampa Energía ───────────────────────────────────────────────────────
    "MGCQ": {
        "issuer":     "Pampa Energía S.A.",
        "currency":   "USD",
        "coupon":     0.0725,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.25, AR0519406398, 7.25% bullet. Confirmado.",
    },
    # ── ARCOR ──────────────────────────────────────────────────────────────
    "RC2C": {
        "issuer":     "ARCOR S.A.I.C.",
        "currency":   "USD",
        "coupon":     0.0590,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.2, 5.90% TNA semi-annual bullet. Confirmado.",
    },
    # ── John Deere Credit ───────────────────────────────────────────────────
    "HJCK": {
        "issuer":     "John Deere Credit Cía. Financiera S.A.",
        "currency":   "USD",
        "coupon":     0.0600,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "estimated",
        "notes":      "Cl.XIV (est.), ~6.00% semi-annual bullet. Tasa estimada.",
    },
    "HJCJ": {
        "issuer":     "John Deere Credit Cía. Financiera S.A.",
        "currency":   "USD",
        "coupon":     0.0650,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "estimated",
        "notes":      "Cl.XV (est.), ~6.50% semi-annual bullet. Tasa estimada.",
    },
    # ── Banco de Galicia ────────────────────────────────────────────────────
    "BYCW": {
        "issuer":     "Banco de Galicia y Buenos Aires S.A.",
        "currency":   "USD",
        "coupon":     0.0625,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.XXIX, US$110.9M, 6.25% semi-annual bullet. Confirmado.",
    },
    # ── Balanz Capital ──────────────────────────────────────────────────────
    "NBS1": {
        "issuer":     "Balanz Capital Valores S.A.",
        "currency":   "USD",
        "coupon":     0.0500,
        "frequency":  2,
        "amort_type": "bullet",
        "confidence": "confirmed",
        "notes":      "Cl.I, AR0340052312, 5.00% semi-annual bullet. Próximo a vencer.",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de fecha
# ─────────────────────────────────────────────────────────────────────────────

def add_months(d: date, n: int) -> date:
    """Suma n meses a d, clampea al último día del mes si es necesario."""
    month = d.month + n
    year  = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, max_day))


def payment_dates_from_maturity(maturity: date, frequency: int, since: date) -> List[date]:
    """
    Genera fechas de pago desde maturity hacia atrás en pasos de 12/frequency meses,
    devolviendo todas las fechas estrictamente mayores a since, ordenadas asc.
    """
    months_per_period = 12 // frequency
    dates: List[date] = []
    d = maturity
    while d > since:
        dates.append(d)
        d = add_months(d, -months_per_period)
    return sorted(dates)


# ─────────────────────────────────────────────────────────────────────────────
# Generadores de cashflow
# ─────────────────────────────────────────────────────────────────────────────

def _build_detalle(
    dates: List[date],
    coupon: float,
    frequency: int,
    amort_at: Dict[date, float],  # date → % of original par
    starting_vr: float = 100.0,
) -> List[Dict]:
    """
    Construye detalle[] en formato MAE.

    dates       : fechas de pago (ascendente)
    coupon      : tasa anual (decimal, 0.0700 para 7%)
    frequency   : periodos por año
    amort_at    : dict fecha → amortización (% del par original)
    starting_vr : VR al inicio de la serie (% del par original)
    """
    coupon_per_period = coupon / frequency * 100  # como % del par original
    vr = starting_vr
    detalle = []
    for i, pmt_date in enumerate(dates):
        amort = amort_at.get(pmt_date, 0.0)
        renta = coupon_per_period * vr / 100.0  # proporcional al VR vigente
        cf    = renta + amort
        detalle.append({
            "fechaPago":    pmt_date.isoformat() + "T00:00:00",
            "numeroCupon":  f"{i + 1:03d}",
            "vr":           round(vr, 6),
            "vrCartera":    round(vr, 6),
            "cashFlow":     round(cf,    6),
            "renta":        round(renta, 6),
            "amortizacion": round(amort, 6),
            "amasR":        round(cf,    6),
        })
        vr = round(vr - amort, 6)
    return detalle


def generate_cashflows(base: str, params: Dict, settlement: date) -> Optional[Dict]:
    """
    Genera el dict completo en formato MAE para un bono.

    Devuelve None si no hay cashflows futuros (bono vencido).
    """
    coupon     = params["coupon"]
    frequency  = params["frequency"]
    currency   = params["currency"]
    amort_type = params["amort_type"]
    current_vr = params.get("current_vr", 100.0)

    # Obtener maturity desde ons_enriched
    maturity = _get_maturity(base)
    if maturity is None:
        logger.warning(f"  {base}: vencimiento no encontrado en ons_enriched — skip")
        return None
    if maturity <= settlement:
        logger.warning(f"  {base}: ya vencido ({maturity}) — skip")
        return None

    # Fechas de pago futuras (ancladas en maturity, hacia atrás)
    dates = payment_dates_from_maturity(maturity, frequency, settlement)
    if not dates:
        logger.warning(f"  {base}: sin fechas futuras — skip")
        return None

    # Construir mapa de amortizaciones
    if amort_type == "bullet":
        amort_at: Dict[date, float] = {maturity: 100.0}
    elif amort_type == "equal_n":
        n = params["amort_n"]
        installment = round(100.0 / n, 6)
        # Las últimas n fechas del schedule COMPLETO son las fechas de amort
        all_dates = payment_dates_from_maturity(maturity, frequency, date(1900, 1, 1))
        amort_dates_all = all_dates[-n:]
        amort_at = {}
        for ad in amort_dates_all:
            amort_at[ad] = installment
        # Ajustar la última para que la suma sea exactamente 100.0
        if amort_dates_all:
            amort_at[amort_dates_all[-1]] = round(100.0 - installment * (n - 1), 6)
    else:
        raise ValueError(f"amort_type desconocido: {amort_type!r}")

    # Para bonos parcialmente amortizados, reconstruir VR al inicio de la serie futura
    # (current_vr ya refleja el estado actual; las amortizaciones futuras en amort_at
    # están expresadas como % del par ORIGINAL, lo cual es la convención MAE)
    detalle = _build_detalle(dates, coupon, frequency, amort_at, starting_vr=current_vr)

    mae_json = {
        "especie":           base + "O",
        "numeroCuponActual": detalle[0]["numeroCupon"] if detalle else "001",
        "renta":             detalle[0]["renta"] if detalle else 0,
        "amortizacion":      0,
        "amasR":             detalle[0]["renta"] if detalle else 0,
        "moneda":            currency,
        "descripcion":       f"ON.{params['issuer'].upper()[:30]}",
        "precio":            0,
        "tir":               0,
        "md":                0,
        # Campos de auditoría (no parte del MAE estándar, usados por el pipeline)
        "_manual":           True,
        "_coupon_annual":    coupon,
        "_frequency":        frequency,
        "_confidence":       params.get("confidence", "estimated"),
        "_notes":            params.get("notes", ""),
    }
    # Para bonos con VR < 100%: el precio BYMA viene en % de VR residual →
    # el pipeline necesita escalarlo a % del par original para el cálculo de YTM.
    if abs(current_vr - 100.0) > 0.01:
        mae_json["_vrActual"] = current_vr

    mae_json["detalle"] = detalle
    return mae_json


# ─────────────────────────────────────────────────────────────────────────────
# Metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

_enriched_cache: Optional[pd.DataFrame] = None

def _get_enriched() -> pd.DataFrame:
    global _enriched_cache
    if _enriched_cache is None:
        _enriched_cache = pd.read_csv(ENRICHED_CSV, dtype={"cuit": str})
        _enriched_cache["base"] = _enriched_cache["symbol"].str[:-1]
    return _enriched_cache


def _get_maturity(base: str) -> Optional[date]:
    df = _get_enriched()
    rows = df[(df["base"] == base) & df["maturity_date"].notna()]
    if rows.empty:
        return None
    raw = rows.iloc[0]["maturity_date"]
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def _is_cache_empty(path: Path) -> bool:
    """Devuelve True si el archivo no existe o contiene null/sin detalle."""
    if not path.exists():
        return True
    with open(path) as fh:
        try:
            data = json.load(fh)
        except Exception:
            return True
    if data is None:
        return True
    detalle = data.get("detalle")
    return not detalle


def main() -> None:
    ap = argparse.ArgumentParser(description="Generador de cashflows manuales — Clase A")
    ap.add_argument("--dry-run", action="store_true",
                    help="Muestra sin escribir archivos")
    ap.add_argument("--force",   action="store_true",
                    help="Sobreescribe aunque el archivo MAE tenga datos reales")
    ap.add_argument("bases",     nargs="*",
                    help="Bases específicas a procesar (default: todas las clase A)")
    args = ap.parse_args()

    settlement = date.today() + timedelta(days=2)
    logger.info(f"Fecha de liquidación: {settlement}")

    bases_to_process = args.bases if args.bases else list(BOND_PARAMS.keys())

    results = []
    skipped_has_data = []
    skipped_no_maturity = []
    skipped_expired = []

    for base in bases_to_process:
        params = BOND_PARAMS.get(base)
        if params is None:
            logger.warning(f"  {base}: no en BOND_PARAMS — ignorado")
            continue

        cache_path = MAE_CACHE_DIR / f"{base}O.json"

        # No sobreescribir datos MAE reales
        if not args.force and not _is_cache_empty(cache_path):
            logger.info(f"  {base}: ya tiene datos MAE reales — skip (--force para sobreescribir)")
            skipped_has_data.append(base)
            continue

        mae_json = generate_cashflows(base, params, settlement)
        if mae_json is None:
            skipped_no_maturity.append(base)
            continue
        if not mae_json.get("detalle"):
            skipped_expired.append(base)
            continue

        n = len(mae_json["detalle"])
        first = mae_json["detalle"][0]
        last  = mae_json["detalle"][-1]
        logger.info(
            f"  {base}O  {params['currency']}  "
            f"{params['coupon']*100:.2f}% / f={params['frequency']}  "
            f"[{params.get('confidence','?')}]  "
            f"{n} cashflows: {first['fechaPago'][:10]} → {last['fechaPago'][:10]}"
        )

        if not args.dry_run:
            MAE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as fh:
                json.dump(mae_json, fh, indent=2, default=str)

        results.append({
            "base":       base,
            "ticker":     base + "O",
            "issuer":     params["issuer"],
            "currency":   params["currency"],
            "coupon":     params["coupon"],
            "frequency":  params["frequency"],
            "amort_type": params["amort_type"],
            "current_vr": params.get("current_vr", 100.0),
            "confidence": params.get("confidence", "estimated"),
            "n_cashflows": n,
            "first_date": first["fechaPago"][:10],
            "last_date":  last["fechaPago"][:10],
            "written":    not args.dry_run,
        })

    # ── Resumen ───────────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print(f"  CASHFLOWS MANUALES — {'DRY RUN' if args.dry_run else 'GENERADOS'}")
    print("=" * 65)
    print(f"  Generados:                   {len(results)}")
    print(f"  Ya tenían datos MAE reales:  {len(skipped_has_data)}")
    print(f"  Sin vencimiento en enriched: {len(skipped_no_maturity)}")
    print(f"  Ya vencidos:                 {len(skipped_expired)}")
    print()

    if results:
        print(f"  {'Base':<6}  {'Tasa':>7}  {'Frec':>4}  {'Amort':<10}  "
              f"{'VR%':>5}  {'CFs':>4}  {'Hasta':<12}  {'Conf'}")
        print("  " + "-" * 62)
        for r in results:
            print(
                f"  {r['base']:<6}  {r['coupon']*100:>6.2f}%  {r['frequency']:>4}  "
                f"{r['amort_type']:<10}  {r['current_vr']:>5.1f}  {r['n_cashflows']:>4}  "
                f"{r['last_date']:<12}  {r['confidence']}"
            )

    if skipped_has_data:
        print(f"\n  Skip (MAE real): {skipped_has_data}")

    if not args.dry_run and results:
        print(f"\n  → {len(results)} archivos escritos en {MAE_CACHE_DIR}/")
        print(f"  → Correr: python build_fixed_income_metrics.py --all")
        print(f"           para recalcular métricas con el universo ampliado.")


if __name__ == "__main__":
    main()
