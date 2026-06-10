"""
Módulo de calidad de señales RV para ON Radar.

Implementa tres scores independientes que responden preguntas distintas:

  cashflow_quality_score   → ¿Conozco bien lo que estoy comprando?      (0–100)
  signal_confidence_score  → ¿El RV signal es confiable?                 (0–100)
  execution_score          → ¿Puedo ejecutar esta operación hoy?         (0–100)

Más dos outputs accionables:
  signal_label    → texto para mostrar en dashboard
  alert_flags     → lista de alertas activas (máx. 3, las más relevantes)

Todos los scores son 0–100 donde 100 = mejor calidad.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Detección de estructura de cashflows
# ─────────────────────────────────────────────────────────────────────────────

def infer_structure_type(cashflows: list) -> str:
    """
    Infiere el tipo de estructura de una ON desde su schedule de cashflows.

    Returns uno de: bullet | amortizing | step_up | unknown
    """
    if not cashflows:
        return "unknown"

    amorts  = [cf.get("amortization", 0) for cf in cashflows]
    coupons = [cf.get("coupon", 0)       for cf in cashflows]

    nonzero_amorts = [a for a in amorts if a > 0.01]

    if len(nonzero_amorts) == 0:
        return "unknown"

    if len(nonzero_amorts) == 1:
        return "bullet"

    # Cupones crecientes en el tiempo → step-up (>10% diferencia entre el primero y el último)
    nonzero_coupons = [c for c in coupons if c > 0.01]
    if len(nonzero_coupons) >= 4:
        first_avg = sum(nonzero_coupons[:2]) / 2
        last_avg  = sum(nonzero_coupons[-2:]) / 2
        if last_avg > first_avg * 1.10:
            return "step_up"

    return "amortizing"


# ─────────────────────────────────────────────────────────────────────────────
# Cashflow Quality Score
# ─────────────────────────────────────────────────────────────────────────────

_CONFIDENCE_BASE: Dict[str, float] = {
    "confirmed": 100.0,
    "mae":        70.0,
    "partial":    55.0,
    "estimated":  35.0,
}

_STRUCTURE_SCORE: Dict[str, float] = {
    "bullet":    100.0,
    "amortizing": 90.0,
    "step_up":    70.0,
    "floating":   55.0,
    "unknown":    30.0,
}


def cashflow_quality_score(
    confidence: Optional[str],
    structure_type: Optional[str],
    n_cashflows: Optional[int],
    has_maturity_date: bool = True,
) -> float:
    """
    Score 0–100 que responde: ¿Conozco bien lo que estoy comprando?

    Pesos:
      50% confianza de los cashflows (fuente y verificación)
      20% tipo de estructura (bullet conocido → más predecible)
      20% cantidad de cashflows conocidos (más = mejor cobertura)
      10% confirmación de fecha de vencimiento
    """
    base  = _CONFIDENCE_BASE.get(str(confidence).lower() if confidence else "", 50.0)
    struc = _STRUCTURE_SCORE.get(str(structure_type).lower() if structure_type else "", 50.0)

    # n_cashflows: 1=8pts, 6=48pts, 12=96pts, 13+=100pts
    n    = int(n_cashflows) if n_cashflows and not pd.isna(n_cashflows) else 0
    cf_s = min(100.0, n * 8.0)

    mat_s = 100.0 if has_maturity_date else 0.0

    score = base * 0.50 + struc * 0.20 + cf_s * 0.20 + mat_s * 0.10
    return round(score, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Signal Confidence Score
# ─────────────────────────────────────────────────────────────────────────────

def signal_confidence_score(
    cf_quality: float,
    peer_count: Optional[int],
    peer_unique_issuers: Optional[int],
    signal_type: Optional[str],
    rv_score: Optional[float] = None,
) -> float:
    """
    Score 0–100 que responde: ¿Es confiable esta señal de RV?

    Pesos:
      50% calidad de cashflows (base de los cálculos de YTM)
      25% tamaño y calidad del grupo de peers
      15% tipo de señal (cross-credit > intra-curve > fallback)
      10% penalización por rv_score extremo sin respaldo de calidad
    """
    # ── Peer quality ─────────────────────────────────────────────────────────
    n_peers = int(peer_count) if peer_count and not pd.isna(peer_count) else 0
    n_uni   = int(peer_unique_issuers) if peer_unique_issuers and not pd.isna(peer_unique_issuers) else 0

    # peer_count: 0=0, 2=50, 4=80, 6+=100
    peer_size_s = min(100.0, n_peers / 6.0 * 100.0)
    # diversidad: 1 emisor=50, 2=75, 3+=100
    peer_div_s  = min(100.0, max(0.0, (n_uni - 1) / 2.0 * 100.0)) if n_uni > 0 else 0.0
    peer_s      = peer_size_s * 0.60 + peer_div_s * 0.40

    # ── Tipo de señal ─────────────────────────────────────────────────────────
    _ST_SCORE = {"cross_credit": 100.0, "intra_curve": 65.0, "fallback": 30.0}
    sig_s = _ST_SCORE.get(str(signal_type).lower() if signal_type else "", 50.0)

    # ── Penalización por rv_score extremo (>4) sin calidad alta ───────────────
    rv_penalty = 0.0
    if rv_score is not None and not pd.isna(rv_score):
        rv_abs = abs(float(rv_score))
        if rv_abs > 4.0:
            # Amplía la desconfianza si la base de cashflows es baja
            cf_gap = max(0.0, 80.0 - cf_quality) / 80.0   # 0 si cf_quality≥80, 1 si cf_quality=0
            rv_penalty = min(20.0, (rv_abs - 4.0) * 3.0 * cf_gap)

    score = cf_quality * 0.50 + peer_s * 0.25 + sig_s * 0.15 - rv_penalty * 0.10
    return round(max(0.0, min(100.0, score)), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Execution Score
# ─────────────────────────────────────────────────────────────────────────────

def execution_score(
    days_stale: Optional[int],
    spread_bps: Optional[float],
    liquidity_pct: Optional[float],
    days_traded_30: Optional[int] = None,
) -> float:
    """
    Score 0–100 que responde: ¿Puedo operar esto hoy a un precio razonable?

    Pesos:
      30% antigüedad del precio (precio viejo = no ejecutable)
      30% spread bid/ask (costo de entrada directo)
      25% actividad reciente (¿hay mercado?)
      15% volumen relativo (percentil en el universo)

    Para campos sin datos disponibles usa valores neutros conservadores.
    """
    # ── Antigüedad del precio ─────────────────────────────────────────────────
    if days_stale is not None and not pd.isna(days_stale):
        age_s = max(0.0, 100.0 - int(days_stale) * 5.0)
    else:
        age_s = 50.0   # neutro conservador

    # ── Spread bid/ask ────────────────────────────────────────────────────────
    if spread_bps is not None and not pd.isna(spread_bps) and float(spread_bps) >= 0:
        spread_s = max(0.0, 100.0 - float(spread_bps) * 0.4)
        # 0bps=100, 50bps=80, 150bps=40, 250bps=0
    else:
        spread_s = 40.0   # sin oferta visible = penaliza

    # ── Actividad reciente ────────────────────────────────────────────────────
    if days_traded_30 is not None and not pd.isna(days_traded_30):
        act_s = min(100.0, int(days_traded_30) / 20.0 * 100.0)
        # 20 ruedas activas = 100, 10 = 50, 0 = 0
    else:
        act_s = 35.0   # sin historia = conservador

    # ── Volumen relativo (percentil en el universo) ───────────────────────────
    if liquidity_pct is not None and not pd.isna(liquidity_pct):
        vol_s = float(liquidity_pct)   # ya es 0–100
    else:
        vol_s = 40.0

    score = age_s * 0.30 + spread_s * 0.30 + act_s * 0.25 + vol_s * 0.15
    return round(max(0.0, min(100.0, score)), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Liquidity Percentile Rank
# ─────────────────────────────────────────────────────────────────────────────

def liquidity_percentile_rank(vol_value: float, vol_series: pd.Series) -> float:
    """
    Retorna el percentil (0–100) del volumen de un bono dentro del universo dado.

    Usa percentil relativo para evitar que umbrales absolutos excluyan
    emisores pequeños pero líquidos dentro de su segmento.
    """
    if pd.isna(vol_value) or vol_series.empty:
        return 50.0
    clean = vol_series.dropna()
    if len(clean) == 0:
        return 50.0
    n_below = (clean < vol_value).sum()
    return round(float(n_below / len(clean) * 100), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Alert Flags
# ─────────────────────────────────────────────────────────────────────────────

_ALERT_DEFS = [
    {
        "id":       "iliquidez",
        "severity": "warning",
        "mensaje":  "Posible distorsión por iliquidez — precio puede no ser ejecutable",
        "check":    lambda r: (
            _get(r, "rv_score", 0) > 3 and
            _get(r, "execution_score", 100) < 40
        ),
    },
    {
        "id":       "no_ejecutable",
        "severity": "danger",
        "mensaje":  "Señal posiblemente no ejecutable — validar precio y liquidez antes de operar",
        "check":    lambda r: (
            abs(_get(r, "rv_score", 0)) > 4 and
            _get(r, "execution_score", 100) < 35
        ),
    },
    {
        "id":       "precio_viejo",
        "severity": "warning",
        "mensaje":  "Precio con {days_stale} días de antigüedad — puede no reflejar mercado actual",
        "check":    lambda r: (
            abs(_get(r, "rv_score", 0)) > 1.5 and
            _get(r, "days_stale", 0) > 7
        ),
    },
    {
        "id":       "cashflow_estimado",
        "severity": "danger",
        "mensaje":  "Cashflows no verificados (~) — el YTM calculado puede ser incorrecto",
        "check":    lambda r: (
            abs(_get(r, "rv_score", 0)) > 2.0 and
            str(_get(r, "cashflow_confidence", "")) == "estimated"
        ),
    },
    {
        "id":       "pocos_peers",
        "severity": "warning",
        "mensaje":  "Grupo de comparación pequeño — señal menos representativa del mercado",
        "check":    lambda r: (
            abs(_get(r, "rv_score", 0)) > 2.0 and
            _get(r, "peer_count", 99) < 3
        ),
    },
    {
        "id":       "intra_curva",
        "severity": "info",
        "mensaje":  "Peers mayormente del mismo emisor — anomalía de curva propia, no cross-credit",
        "check":    lambda r: (
            abs(_get(r, "rv_score", 0)) > 2.0 and
            str(_get(r, "signal_type", "")) == "intra_curve"
        ),
    },
]


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Helper para leer desde dict o pd.Series con fallback."""
    try:
        val = row[key] if isinstance(row, dict) else row.get(key, default)
        return default if pd.isna(val) else val
    except (KeyError, TypeError):
        return default


def compute_alerts(row: Any) -> List[Dict]:
    """
    Evalúa las reglas de alerta para un bono y retorna la lista de alertas activas.

    row puede ser dict o pd.Series.
    Retorna lista de dicts {id, severity, mensaje}.
    """
    active = []
    for rule in _ALERT_DEFS:
        try:
            if rule["check"](row):
                msg = rule["mensaje"]
                # Interpolación simple de {days_stale}
                if "{days_stale}" in msg:
                    msg = msg.replace("{days_stale}", str(int(_get(row, "days_stale", 0))))
                active.append({"id": rule["id"], "severity": rule["severity"], "mensaje": msg})
        except Exception:
            pass
    return active


# ─────────────────────────────────────────────────────────────────────────────
# Signal Label & Category
# ─────────────────────────────────────────────────────────────────────────────

def signal_category(
    rv_score: Optional[float],
    signal_type: Optional[str],
    signal_confidence: Optional[float],
    exec_score: Optional[float],
) -> str:
    """
    Clasifica la señal en 4 categorías mutuamente excluyentes.

    POTENCIALMENTE_BARATO       → señal fuerte + confianza alta
    POSIBLE_OPORTUNIDAD         → señal fuerte + confianza media (requiere validación)
    ANOMALIA_CURVA_PROPIA       → señal fuerte pero peers mayormente mismo emisor
    NEUTRO                      → señal débil o insuficiente
    """
    if rv_score is None or pd.isna(rv_score):
        return "NEUTRO"

    rv    = float(rv_score)
    conf  = float(signal_confidence) if signal_confidence and not pd.isna(signal_confidence) else 50.0
    exe   = float(exec_score)        if exec_score        and not pd.isna(exec_score)        else 50.0
    stype = str(signal_type or "")

    if abs(rv) < 1.5:
        return "NEUTRO"

    if stype == "intra_curve":
        return "ANOMALIA_CURVA_PROPIA"

    if conf >= 60 and exe >= 45:
        return "POTENCIALMENTE_BARATO" if rv > 0 else "POTENCIALMENTE_CARO"

    return "POSIBLE_OPORTUNIDAD"


_CATEGORY_LABEL: Dict[str, str] = {
    "POTENCIALMENTE_BARATO":  "POTENCIALMENTE BARATO",
    "POTENCIALMENTE_CARO":    "POTENCIALMENTE CARO",
    "POSIBLE_OPORTUNIDAD":    "POSIBLE OPORTUNIDAD — REQUIERE VALIDACIÓN",
    "ANOMALIA_CURVA_PROPIA":  "ANOMALÍA DE CURVA PROPIA",
    "NEUTRO":                 "",
}


def signal_label(category: str) -> str:
    """Texto para mostrar en el dashboard."""
    return _CATEGORY_LABEL.get(category, "")


# ─────────────────────────────────────────────────────────────────────────────
# Compute all scores for a single row (convenience wrapper)
# ─────────────────────────────────────────────────────────────────────────────

def score_row(row: Any, universe_vol_series: pd.Series) -> Dict:
    """
    Calcula todos los scores de calidad para una fila del RV DataFrame.

    row               : pd.Series o dict con campos del relative_value.csv
    universe_vol_series: pd.Series con vol_30d de todos los bonos (para percentil)

    Returns dict con los campos de signal_quality.csv.
    """
    cf_conf  = _get(row, "cashflow_confidence")
    struc    = _get(row, "structure_type")
    n_cfs    = _get(row, "n_cashflows")
    mat_date = _get(row, "maturity_date")
    has_mat  = mat_date is not None and str(mat_date) not in ("", "nan", "None")

    cf_q = cashflow_quality_score(cf_conf, struc, n_cfs, has_mat)

    sig_c = signal_confidence_score(
        cf_quality          = cf_q,
        peer_count          = _get(row, "peer_count"),
        peer_unique_issuers = _get(row, "peer_unique_issuers"),
        signal_type         = _get(row, "signal_type"),
        rv_score            = _get(row, "rv_score"),
    )

    # Percentil de volumen dentro del universo
    vol = _get(row, "volume", None)
    if vol is not None:
        liq_pct = liquidity_percentile_rank(float(vol), universe_vol_series)
    else:
        liq_pct = None

    exe = execution_score(
        days_stale     = _get(row, "days_stale"),
        spread_bps     = _get(row, "spread_bps_current"),
        liquidity_pct  = liq_pct,
        days_traded_30 = _get(row, "days_traded_30"),
    )

    cat   = signal_category(
        rv_score          = _get(row, "rv_score"),
        signal_type       = _get(row, "signal_type"),
        signal_confidence = sig_c,
        exec_score        = exe,
    )
    label  = signal_label(cat)
    alerts = compute_alerts({
        **{k: _get(row, k) for k in [
            "rv_score", "signal_type", "cashflow_confidence",
            "peer_count", "days_stale", "execution_score",
        ]},
        "execution_score": exe,
    })

    return {
        "cashflow_quality_score":  cf_q,
        "signal_confidence_score": sig_c,
        "execution_score":         exe,
        "liquidity_percentile":    liq_pct,
        "signal_category":         cat,
        "signal_label":            label,
        "alerts":                  "|".join(a["id"] for a in alerts),
        "alert_count":             len(alerts),
    }
