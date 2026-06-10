"""
Parsea el PDF oficial de ARCA/AFIP: OBLIGACIONES-NEGOCIABLES-EXENTAS-*.pdf

Estructura del PDF (verificada 2026-05-30 en versión 2025):
  Columnas por índice:
    0  Cuit Emisor
    1  Denominación Emisor
    2  Código Caja de Valores
    3  Código BYMA
    4  Denominación Especie
    5  Tipo de título
    6  Fecha Alta (aammdd)
    7  Fecha Vto (aammdd)
    8  Moneda

  - 7 páginas, ~350-400 registros
  - El encabezado se repite en cada página
  - Primera fila por página: texto descriptivo ("Información suministrada...")
  - Filas válidas: primer campo contiene un CUIT (11 dígitos)
"""

import logging
import pandas as pd
from pathlib import Path

try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    _HAS_PDFPLUMBER = False

logger = logging.getLogger(__name__)

# Nombres canónicos de las 9 columnas, en orden de aparición
COLUMNS = [
    "cuit_emisor",
    "razon_social",
    "codigo_caja_valores",
    "codigo_byma",
    "denominacion_especie",
    "tipo_titulo",
    "fecha_alta",
    "fecha_vencimiento",
    "moneda",
]

CURRENCY_SUFFIXES = set("ODCPXYZLB")


def _is_valid_cuit(value: str) -> bool:
    """CUIT válido: 11 dígitos, opcionalmente con guiones."""
    digits = value.replace("-", "").replace(" ", "")
    return digits.isdigit() and len(digits) == 11


def _get_prefix(codigo_byma: str) -> str:
    """Extrae el prefijo de 2-4 chars del código BYMA."""
    c = codigo_byma.strip().upper()
    if c and c[-1] in CURRENCY_SUFFIXES:
        c = c[:-1]
    # El prefijo son los chars alfabéticos iniciales
    prefix = ""
    for ch in c:
        if ch.isalpha():
            prefix += ch
        else:
            break
    return prefix[:4] if prefix else c[:3]


def parse_pdf(pdf_path: Path) -> pd.DataFrame:
    """
    Extrae la tabla completa del PDF.
    Retorna DataFrame con columnas de COLUMNS.
    """
    if not _HAS_PDFPLUMBER:
        logger.error("pdfplumber no instalado: pip install pdfplumber")
        return pd.DataFrame()

    if not pdf_path.exists():
        logger.error(f"PDF no encontrado: {pdf_path}")
        return pd.DataFrame()

    rows = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        logger.info(f"Procesando {len(pdf.pages)} páginas de {pdf_path.name}")
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            if not tables:
                continue
            for table in tables:
                for row in table:
                    if not row:
                        continue
                    # Solo procesar filas cuyo primer campo es un CUIT válido
                    first = str(row[0]).strip().replace("-", "") if row[0] else ""
                    if not _is_valid_cuit(str(row[0]).strip()):
                        continue
                    # Tomar los 9 primeros campos
                    record = [str(c).strip() if c else "" for c in row[:9]]
                    # Padding si la fila tiene menos de 9 columnas
                    while len(record) < 9:
                        record.append("")
                    rows.append(record)

    if not rows:
        logger.warning("No se encontraron filas válidas en el PDF")
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=COLUMNS)

    # Normalizar CUIT: quitar guiones y espacios, formatear con guiones
    df["cuit_emisor"] = df["cuit_emisor"].str.replace(r"[\s-]", "", regex=True)

    # Calcular prefijo BYMA
    df["prefijo"] = df["codigo_byma"].apply(
        lambda x: _get_prefix(x) if x.strip() else ""
    )
    df = df[df["prefijo"].str.len() >= 2]

    logger.info(f"PDF parseado: {len(df)} registros, {df['cuit_emisor'].nunique()} emisores únicos")
    return df.reset_index(drop=True)


def to_issuer_master(df_pdf: pd.DataFrame) -> pd.DataFrame:
    """
    Convierte el DataFrame del PDF al formato issuer_master.csv:
    prefijo | cuit_emisor | razon_social | sector | fuente
    """
    if df_pdf.empty or "prefijo" not in df_pdf.columns:
        return pd.DataFrame()

    records = []
    for _, row in df_pdf.iterrows():
        prefijo = row["prefijo"].strip().upper()
        if len(prefijo) < 2:
            continue
        records.append({
            "prefijo":      prefijo,
            "cuit_emisor":  row.get("cuit_emisor", ""),
            "razon_social": row.get("razon_social", ""),
            "sector":       "",
            "fuente":       "afip_oficial",
        })

    if not records:
        return pd.DataFrame()

    master = (
        pd.DataFrame(records)
        .drop_duplicates(subset=["prefijo"])
        .sort_values("prefijo")
        .reset_index(drop=True)
    )
    logger.info(f"issuer_master desde PDF: {len(master)} prefijos únicos")
    return master
