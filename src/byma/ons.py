"""
Descarga y normalización de ONs desde BYMA Open Data.

Endpoint principal: POST /negociable-obligations
Devuelve ~2100 registros (todos los instrumentos de tipo CORP),
incluyendo duplicados por tipo de liquidación:
  settlementType '1' = T+1 (48hs)
  settlementType '2' = CI (contado inmediato)
"""

import logging
import pandas as pd
from .client import BYMAClient

logger = logging.getLogger(__name__)

ENDPOINT = "negociable-obligations"

# Campos del endpoint real → nombres internos del proyecto
FIELD_MAP = {
    "symbol":                   "ticker",
    "denominationCcy":          "moneda",
    "settlementType":           "tipo_liquidacion",
    "trade":                    "ultimo_precio",
    "bidPrice":                 "precio_compra",
    "offerPrice":               "precio_venta",
    "quantityBid":              "cantidad_compra",
    "quantityOffer":            "cantidad_venta",
    "volumeAmount":             "monto_operado",
    "tradeVolume":              "volumen_nominal",
    "vwap":                     "precio_promedio",
    "openingPrice":             "apertura",
    "tradingHighPrice":         "maximo",
    "tradingLowPrice":          "minimo",
    "previousClosingPrice":     "precio_cierre_anterior",
    "previousSettlementPrice":  "precio_settlement_anterior",
    "maturityDate":             "fecha_vencimiento",
    "daysToMaturity":           "dias_al_vencimiento",
    "numberOfOrders":           "cantidad_operaciones",
    "tradeHour":                "hora_ultimo_trade",
    "market":                   "mercado",
    "securityType":             "tipo_instrumento",
}


def fetch_ons_raw(client: BYMAClient) -> list:
    logger.info("Descargando ONs desde BYMA Open Data...")
    data = client.post(ENDPOINT)
    if not isinstance(data, list):
        raise ValueError(f"Respuesta inesperada del endpoint: {type(data)}")
    logger.info(f"  {len(data)} registros recibidos")
    return data


def normalize_ons(records: list) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    rename = {k: v for k, v in FIELD_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)

    # Tipos numéricos
    num_cols = [
        "ultimo_precio", "precio_compra", "precio_venta",
        "cantidad_compra", "cantidad_venta",
        "monto_operado", "volumen_nominal", "precio_promedio",
        "apertura", "maximo", "minimo",
        "precio_cierre_anterior", "precio_settlement_anterior",
        "dias_al_vencimiento", "cantidad_operaciones",
    ]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "fecha_vencimiento" in df.columns:
        df["fecha_vencimiento"] = pd.to_datetime(df["fecha_vencimiento"], errors="coerce")

    return df


def get_ons(client: BYMAClient) -> pd.DataFrame:
    records = fetch_ons_raw(client)
    return normalize_ons(records)
