# Fuentes de cashflow para ONs argentinas — Validación Fase 5

> Objetivo: determinar si es posible obtener automáticamente la estructura financiera
> completa de una ON (cupón, tipo de tasa, fechas de pago, amortizaciones, cashflows).
> No se calculó TIR. Solo validación de fuentes.

Fecha de validación: 2026-06-02  
Tickers de prueba: YMCJO · DNC7O · PLC4O · PNCXO · TLC5O

---

## Tabla comparativa de fuentes

| Fuente | Cupón | Tipo tasa | Fechas pago | Amortizaciones | Cashflows (JSON) | ISIN | Automatizable | Login | Costo |
|--------|:-----:|:---------:|:-----------:|:--------------:|:----------------:|:----:|:-------------:|:-----:|:-----:|
| **MAE API free** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | $0 |
| **BYMA Open free** | ⚠️ texto | ⚠️ texto | ✅ | ⚠️ texto | ❌ | ✅ | ✅ | ❌ | $0 |
| CNV / AIF2 | ? | ? | ? | ? | ? | ✅ | ❌ | ✅ inst. | n/d |
| BYMA Comercial | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | Pago |
| MAE Comercial | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | Pago |
| IOL / PPI | — | — | — | — | — | — | ❌ | OAuth | n/d |
| Caja de Valores | — | — | — | — | — | — | ❌ | ✅ | n/d |

**Leyenda:** ✅ = disponible y estructurado · ⚠️ = disponible pero solo como texto libre del prospecto · ❌ = no disponible / bloqueado · ? = no verificado

---

## 1. MAE API free — fuente recomendada

**Base URL:** `https://api.marketdata.mae.com.ar/api`  
**Autenticación:** ninguna  
**Rate limit:** no detectado  
**Documentación comercial:** https://marketdata.mae.com.ar/swagger/api-documentacion.html

### Endpoints útiles

```
GET /emisiones/flujofondos/{TICKER}
    → Cashflow completo en JSON: cupón por cupón, fecha, VR, renta%, amortización%, cashFlow
    → Cobertura: ONs activamente cargadas (ver caveat de cobertura)

GET /emisiones/on/Todos
    → Lista las 226 ONs en el sistema con ticker, emisor, fecha de alta
    → Permite resolver ticker → id + codigo para otros endpoints

GET /emisiones/on/{id}/{codigo}
    → Detalle de la emisión con links a documentos del prospecto (SharePoint MAE)
```

### Esquema de respuesta de `/flujofondos/{ticker}`

```json
{
  "especie":           "YMCJO",
  "numeroCuponActual": "012",
  "renta":             3.5,
  "amortizacion":      0.0,
  "moneda":            "USD",
  "descripcion":       "ON.YPF CLASE 18EMISOR FREQUENT",
  "detalle": [
    {
      "fechaPago":    "2026-09-30T00:00:00",
      "numeroCupon":  "012",
      "vr":           100.0,
      "vrCartera":    100.0,
      "cashFlow":     3.5,
      "renta":        3.5,
      "amortizacion": 0.0,
      "amasR":        3.5
    }
  ]
}
```

**Campos por cupón:**
- `fechaPago` — fecha exacta del pago
- `numeroCupon` — número de secuencia del cupón
- `vr` — valor residual al momento del pago (sobre VN 100)
- `renta` — pago de interés como % del VN original
- `amortizacion` — pago de capital como % del VN original
- `cashFlow` — suma renta + amortizacion (sobre VN original)

### Caveat de cobertura

MAE solo tiene cashflows cargados para ONs que el sistema tiene activas en su módulo de renta fija.
Bonds recientes (emitidos < 12 meses) o con pocas operaciones pueden devolver `detalle: []`.
Ver tabla de resultados por ticker.

---

## 2. BYMA Open free — fuente complementaria para metadata estática

**Base URL:** `https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free`  
**Autenticación:** ninguna  
**Método:** POST con `Content-Type: application/json`

### Endpoint útil

```
POST /bnown/fichatecnica/especies/general
Body: {"symbol": "YMCJO"}

→ Respuesta anidada en data[0]
```

### Campos disponibles en `data[0]`

```
codigoIsin         — ISIN completo (ej: USP989MJBT72)
denominacion       — Clase/serie (ej: "CLASE XVIII")
interes            — Texto libre del prospecto con tasa (ej: "9,75%" o texto largo)
formaAmortizacion  — Texto libre (ej: "EN TRES PAGOS,DOS DE 33,33% Y EL ULTIMO DE 33,34%")
fechaEmision       — Fecha de emisión
fechaVencimiento   — Fecha de vencimiento final
moneda             — "Dólares", "Pesos", etc.
montoNominal       — Monto total emitido (VN)
montoResidual      — VN residual actual
tipoGarantia       — "Con garantía común", etc.
```

**Limitación:** `interes` y `formaAmortizacion` son strings de texto libre extraídos del prospecto,
no estructuras numéricas. Requieren parsing ad-hoc para cada emisor.

**No devuelve:** fechas individuales de cada cupón, tasa exacta por período (ej: step-up),
schedule de amortización como array de fechas+montos.

### BYMA Comercial (upgrade required)

```
POST /bnown/analiticos/rentafija
→ Responde con: {"upgrade": true}  — requiere contrato pago
```

---

## 3. CNV / AIF2 — no automatizable públicamente

**AIF2:** `https://aif2.cnv.gov.ar/`  
Redirige a `cnvfs.cnv.gov.ar/adfs/ls/` — login institucional ADFS/WS-Federation.  
No tiene endpoint de datos públicos sin autenticación.

La CNV publica los prospectos completos de cada ON en PDF (accesibles desde el
buscador web en `cnv.gov.ar/SitioWeb/Financiamiento/BusquedaPublica`),
pero sin API estructurada.

---

## 4. MAE Comercial — requiere API key

**Base URL:** `https://api.mae.com.ar/MarketData/v1`  
**Header requerido:** `x-api-key: {API_KEY}`  
**Respuesta sin key:** HTTP 403

Documentado en Swagger. Incluye endpoints para cotizaciones, cashflows y analíticos.
Presumiblemente sin restricciones de cobertura (cubre todo el universo MAE).

---

## Resultados por ticker

### YMCJO — YPF S.A. ON Clase XVIII (USD step-up, 2021–2033)

| Campo | Valor |
|-------|-------|
| ISIN | USP989MJBT72 |
| Moneda | USD |
| Tasa | Fija 7% anual (step-up histórico: 1.5% → 3.5% → 7%) |
| Amortización | 4 cuotas anuales de 25% desde sep 2030 |
| Vencimiento | 2033-09-30 |
| MAE cashflow | **✅ 15 cupones** (#012 al #026, sep 2026 → sep 2033) |
| BYMA ficha | ✅ ISIN + texto descriptivo |

**Cashflow completo (MAE):**

| Fecha | Cupón | VR | Renta% | Amort% | CashFlow% |
|-------|-------|----|--------|--------|-----------|
| 2026-09-30 | 012 | 100.00 | 3.50 | 0.00 | 3.50 |
| 2027-03-30 | 013 | 100.00 | 3.50 | 0.00 | 3.50 |
| 2027-09-30 | 014 | 100.00 | 3.50 | 0.00 | 3.50 |
| 2028-03-30 | 015 | 100.00 | 3.50 | 0.00 | 3.50 |
| 2028-09-30 | 016 | 100.00 | 3.50 | 0.00 | 3.50 |
| 2029-03-30 | 017 | 100.00 | 3.50 | 0.00 | 3.50 |
| 2029-09-30 | 018 | 100.00 | 3.50 | 0.00 | 3.50 |
| 2030-03-30 | 019 | 100.00 | 3.50 | 0.00 | 3.50 |
| 2030-09-30 | 020 | 100.00 | 3.50 | **25.00** | **28.50** |
| 2031-03-30 | 021 | 75.00 | 2.62 | 0.00 | 2.62 |
| 2031-09-30 | 022 | 75.00 | 2.62 | **25.00** | **27.62** |
| 2032-03-30 | 023 | 50.00 | 1.75 | 0.00 | 1.75 |
| 2032-09-30 | 024 | 50.00 | 1.75 | **25.00** | **26.75** |
| 2033-03-30 | 025 | 25.00 | 0.88 | 0.00 | 0.88 |
| 2033-09-30 | 026 | 25.00 | 0.88 | **25.00** | **25.88** |

---

### DNC7O — Edenor S.A. ON Clase 7 (USD 9.75%, 2024–2030)

| Campo | Valor |
|-------|-------|
| ISIN | USP3710FAU86 |
| Moneda | USD |
| Tasa | Fija 9.75% anual |
| Amortización | 3 cuotas: 33.33% + 33.33% + 33.34% (oct 2028, 2029, 2030) |
| Vencimiento | 2030-10-24 |
| MAE cashflow | ⚠️ `detalle: []` — bond reciente (oct 2024), sin cashflow cargado |
| BYMA ficha | ✅ ISIN + tasa + esquema amortización en texto |

**Estado:** La ON existe en el sistema MAE pero sin schedule de cupones cargado.
Reconstruible desde BYMA ficha técnica + parámetros de emisión.

---

### PLC4O — Pluspetrol S.A. ON Clase 4 (USD 8.5% bullet, 2025–2032)

| Campo | Valor |
|-------|-------|
| ISIN | USP7924AAA62 |
| Moneda | USD |
| Tasa | Fija 8.50% anual |
| Amortización | Bullet — 100% al vencimiento (2032-05-30) |
| Vencimiento | 2032-05-30 |
| MAE cashflow | ⚠️ `detalle: []` — bond muy reciente (may 2025) |
| BYMA ficha | ✅ ISIN + tasa + "AL VENCIMIENTO" |

**Estado:** Emitida en mayo 2025. MAE aún no tiene el schedule cargado.
Estructura deducible: cupones semestrales al 4.25% + bullet al vencimiento.

---

### PNCXO — ticker inválido

**Este ticker no existe en BYMA ni en MAE.**

El ticker correcto para Pan American Energy ON Clase 31 es **PNXCO** (letras C y N
en distinto orden). PNCXO devuelve null en todos los endpoints.

**Usando PNXCO (correcto):**

| Campo | Valor |
|-------|-------|
| ISIN | USE7S78BAC65 |
| Moneda | USD |
| Tasa | Fija 8.50% anual |
| Amortización | 3 cuotas: 33% + 33% + 34% (abr 2030, 2031, 2032) |
| Vencimiento | 2032-04-30 |
| MAE cashflow | **✅ 12 cupones** (#005 al #016, oct 2026 → abr 2032) |

**Cashflow completo (PNXCO vía MAE):**

| Fecha | Cupón | VR | Renta% | Amort% | CashFlow% |
|-------|-------|----|--------|--------|-----------|
| 2026-10-30 | 005 | 100.00 | 4.25 | 0.00 | 4.25 |
| 2027-04-30 | 006 | 100.00 | 4.25 | 0.00 | 4.25 |
| 2027-10-30 | 007 | 100.00 | 4.25 | 0.00 | 4.25 |
| 2028-04-30 | 008 | 100.00 | 4.25 | 0.00 | 4.25 |
| 2028-10-30 | 009 | 100.00 | 4.25 | 0.00 | 4.25 |
| 2029-04-30 | 010 | 100.00 | 4.25 | 0.00 | 4.25 |
| 2029-10-30 | 011 | 100.00 | 4.25 | 0.00 | 4.25 |
| 2030-04-30 | 012 | 100.00 | 4.25 | **33.00** | **37.25** |
| 2030-10-30 | 013 | 67.00 | 2.85 | 0.00 | 2.85 |
| 2031-04-30 | 014 | 67.00 | 2.85 | **33.00** | **35.85** |
| 2031-10-30 | 015 | 34.00 | 1.44 | 0.00 | 1.44 |
| 2032-04-30 | 016 | 34.00 | 1.44 | **34.00** | **35.44** |

---

### TLC5O — Telecom Argentina ON Clase 5 — VENCIDA

**Este ticker venció el 6 de agosto de 2025.** Todos los endpoints devuelven vacío.

**Alternativa vigente — TLCMO (Telecom ON Clase 21, USD 9.5%, 2024–2031):**

| Campo | Valor |
|-------|-------|
| Moneda | USD |
| Tasa | Fija 9.5% anual |
| Amortización | 3 cuotas: 33% + 33% + 34% (jul 2029, 2030, 2031) |
| MAE cashflow | **✅ 11 cupones** (#004 al #014, jul 2026 → jul 2031) |

**Cashflow completo (TLCMO vía MAE):**

| Fecha | Cupón | VR | Renta% | Amort% | CashFlow% |
|-------|-------|----|--------|--------|-----------|
| 2026-07-18 | 004 | 100.00 | 4.75 | 0.00 | 4.75 |
| 2027-01-18 | 005 | 100.00 | 4.75 | 0.00 | 4.75 |
| 2027-07-18 | 006 | 100.00 | 4.75 | 0.00 | 4.75 |
| 2028-01-18 | 007 | 100.00 | 4.75 | 0.00 | 4.75 |
| 2028-07-18 | 008 | 100.00 | 4.75 | 0.00 | 4.75 |
| 2029-01-18 | 009 | 100.00 | 4.75 | 0.00 | 4.75 |
| 2029-07-18 | 010 | 100.00 | 4.75 | **33.00** | **37.75** |
| 2030-01-18 | 011 | 67.00 | 3.18 | 0.00 | 3.18 |
| 2030-07-18 | 012 | 67.00 | 3.18 | **33.00** | **36.18** |
| 2031-01-18 | 013 | 34.00 | 1.62 | 0.00 | 1.62 |
| 2031-07-18 | 014 | 34.00 | 1.62 | **34.00** | **35.62** |

---

## Resumen de cobertura por ticker

| Ticker | MAE cashflow | BYMA ficha | ISIN | Nota |
|--------|:------------:|:----------:|:----:|------|
| YMCJO | ✅ 15 cupones | ✅ | USP989MJBT72 | Completo |
| DNC7O | ⚠️ vacío | ✅ | USP3710FAU86 | Bond oct 2024, sin schedule MAE |
| PLC4O | ⚠️ vacío | ✅ | USP7924AAA62 | Bond may 2025, sin schedule MAE |
| **PNCXO** | ❌ no existe | ❌ | — | **Ticker incorrecto** — usar PNXCO |
| PNXCO | ✅ 12 cupones | ✅ | USE7S78BAC65 | Ticker correcto PAE Clase 31 |
| **TLC5O** | ❌ vencida | ❌ | — | **Venció ago-2025** — usar TLCMO |
| TLCMO | ✅ 11 cupones | — | — | Alternativa Telecom vigente |

---

## Conclusiones

### 1. MAE API es la fuente primaria para cashflows automatizables

```
GET https://api.marketdata.mae.com.ar/api/emisiones/flujofondos/{TICKER}
```

- Sin autenticación, sin costo, JSON estructurado
- Devuelve: cupón#, fecha exacta, VR, renta%, amortización%, cashFlow%
- Suficiente para calcular TIR, Duration y DV01
- **Limitación de cobertura**: bonds emitidos recientemente o con bajo volumen MAE
  pueden devolver `detalle: []`

### 2. BYMA Open es la fuente complementaria para metadata

```
POST https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free/bnown/fichatecnica/especies/general
Body: {"symbol": "{TICKER}"}
```

- Sin autenticación, sin costo
- Devuelve: ISIN, fechas emisión/vencimiento, monto nominal, tipo de tasa (texto), esquema amortización (texto)
- Útil para confirmar ISIN y como fallback cuando MAE no tiene cashflows
- Los campos `interes` y `formaAmortizacion` requieren parsing ad-hoc

### 3. Estrategia recomendada para Fase 6 (TIR / Duration)

```python
# Orden de prioridad para obtener cashflows:
1. MAE flujofondos/{ticker}   → si detalle no vacío → usar directo
2. Fallback: reconstruir desde BYMA fichatecnica + parámetros de emisión
3. Fallback manual: prospecto PDF desde MAE /emisiones/on/{id}/{codigo}
```

### 4. Tickers inválidos detectados

- **PNCXO** no existe → ticker correcto es **PNXCO** (ON PAE Clase 31)
- **TLC5O** venció en agosto 2025 → usar **TLCMO** (Telecom Clase 21) u otro ticker vigente
- Verificar siempre contra `GET /emisiones/on/Todos` antes de asumir que un ticker es válido

---

## Appendix: snippet de integración (Python)

```python
import requests

MAE_BASE = "https://api.marketdata.mae.com.ar/api"
BYMA_BASE = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"

def get_cashflows_mae(ticker: str) -> dict:
    r = requests.get(f"{MAE_BASE}/emisiones/flujofondos/{ticker}", timeout=10)
    r.raise_for_status()
    return r.json()

def get_ficha_byma(ticker: str) -> dict:
    r = requests.post(
        f"{BYMA_BASE}/bnown/fichatecnica/especies/general",
        json={"symbol": ticker},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("data", [])
    return items[0] if items else {}

# Uso
cf = get_cashflows_mae("YMCJO")
for row in cf["detalle"]:
    print(row["fechaPago"][:10], row["renta"], row["amortizacion"], row["cashFlow"])
```
