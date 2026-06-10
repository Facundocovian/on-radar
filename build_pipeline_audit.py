"""
Pipeline Audit — ON Radar
Traza cada bono desde BYMA hasta el universo final de RV.
Genera: outputs/pipeline_audit.csv y muestra embudo en consola.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

# ─── Paths ────────────────────────────────────────────────────────────────────
BYMA_FILE    = Path("outputs/ons_enriched.csv")
RATINGS_FILE = Path("data/ratings_master.csv")
MAE_CACHE    = Path("data/raw/mae_cashflows")
FIM_FILE     = Path("outputs/fixed_income_metrics.csv")
RV_FILE      = Path("outputs/relative_value.csv")
OUT_FILE     = Path("outputs/pipeline_audit.csv")

CCL_MIN, CCL_MAX = 500, 5_000


# ─── Load sources ─────────────────────────────────────────────────────────────

def load_byma() -> pd.DataFrame:
    df = pd.read_csv(BYMA_FILE, dtype={"cuit": str})
    df["base"]   = df["symbol"].str[:-1]
    df["suffix"] = df["symbol"].str[-1]
    return df


def load_ratings() -> dict[str, str]:
    df = pd.read_csv(RATINGS_FILE, comment="#", dtype=str)
    return dict(zip(df["prefijo"].str.strip(), df["rating_lp"].str.strip()))


def load_mae_cache() -> dict[str, dict]:
    cache: dict[str, dict] = {}
    for f in MAE_CACHE.glob("*.json"):
        ticker = f.stem.upper()
        with open(f) as fh:
            try:
                data = json.load(fh)
            except Exception:
                data = {}
        cache[ticker] = data or {}
    return cache


def load_fim() -> dict[str, dict]:
    df = pd.read_csv(FIM_FILE)
    return {row["symbol"]: row.to_dict() for _, row in df.iterrows()}


def load_rv() -> dict[str, dict]:
    df = pd.read_csv(RV_FILE)
    return {row["symbol"]: row.to_dict() for _, row in df.iterrows()}


# ─── CCL estimation ───────────────────────────────────────────────────────────

def estimate_ccl(byma: pd.DataFrame) -> float | None:
    ext = byma[(byma["suffix"] == "C") & byma["price"].gt(1)].copy()
    ext["base_"] = ext["base"]
    ars = byma[
        (byma["suffix"] == "O") & byma["price"].gt(100) & (byma["settlement"] == 2)
    ].copy()
    ars["base_"] = ars["base"]
    merged = ext.merge(ars[["base_", "price"]], on="base_", suffixes=("_ext", "_ars"))
    merged["ccl"] = merged["price_ars"] / merged["price_ext"]
    valid = merged["ccl"][(merged["ccl"] > CCL_MIN) & (merged["ccl"] < CCL_MAX)]
    return float(valid.median()) if not valid.empty else None


# ─── Best price lookup ────────────────────────────────────────────────────────

def best_price(base: str, byma: pd.DataFrame, ccl: float | None) -> tuple[float | None, str]:
    def _lookup(symbol: str) -> float | None:
        rows = byma[(byma["symbol"] == symbol) & byma["price"].gt(0)]
        if rows.empty:
            return None
        ci = rows[rows["settlement"] == 2]
        best = ci if not ci.empty else rows
        return float(best.sort_values("price", ascending=False).iloc[0]["price"])

    for suffix, label in (("C", "EXT"), ("D", "USD")):
        p = _lookup(base + suffix)
        if p and p > 1:
            return p, label

    p_ars = _lookup(base + "O")
    if p_ars and p_ars > 100 and ccl and ccl > 0:
        return p_ars / ccl, "ARS/CCL"

    return None, "—"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    byma    = load_byma()
    ratings = load_ratings()
    mae     = load_mae_cache()
    fim     = load_fim()
    rv      = load_rv()
    ccl     = estimate_ccl(byma)

    print(f"CCL estimado: {ccl:.2f} ARS/USD" if ccl else "CCL: no disponible")

    # Universo base: todas las bases únicas en BYMA con al menos un registro
    all_bases = sorted(byma["base"].unique())

    rows = []
    for base in all_bases:
        sub = byma[byma["base"] == base]

        # ── Stage 1: en BYMA (siempre True por construcción)
        in_byma = True
        byma_symbols = sorted(sub["symbol"].unique().tolist())
        currencies   = sorted(sub["currency"].unique().tolist())

        # ── Stage 2: tiene precio USD válido (via C, D, o O/CCL)
        price_usd, price_src = best_price(base, byma, ccl)
        has_price = price_usd is not None

        # ── Stage 3: tiene emisor mapeado
        mapped_rows = sub[sub["issuer"].notna() & (sub["issuer"] != "")]
        has_issuer  = not mapped_rows.empty
        issuer      = mapped_rows.iloc[0]["issuer"] if has_issuer else ""
        cuit        = mapped_rows.iloc[0]["cuit"]   if has_issuer else ""
        sector      = mapped_rows.iloc[0]["sector"] if has_issuer else ""

        # ── Stage 4: tiene rating
        prefix      = base[:3].upper()
        rating_val  = ratings.get(prefix)
        has_rating  = rating_val is not None

        # ── Stage 5: tiene cashflows MAE
        mae_ticker  = base + "O"
        mae_data    = mae.get(mae_ticker, {})
        n_cashflows = len((mae_data.get("detalle") or []))
        has_mae_cf  = n_cashflows > 0
        mae_currency = mae_data.get("moneda", "") if mae_data else ""

        # ── Stage 6: tiene YTM calculada (en fixed_income_metrics)
        # MAE tickers sin sufijo extra — los símbolos en FIM son BYMA O-suffix
        fim_symbol = mae_ticker  # e.g. YMCJO → "YMCJO"
        fim_row    = fim.get(fim_symbol)
        has_ytm    = fim_row is not None and pd.notna(fim_row.get("ytm"))
        ytm        = fim_row["ytm"] if has_ytm else None

        # ── Stage 7: en universo RV (no outlier)
        rv_row     = rv.get(fim_symbol)
        in_rv      = rv_row is not None
        is_outlier = bool(rv_row.get("is_outlier")) if in_rv else None
        rv_score   = rv_row.get("rv_score") if in_rv else None

        # ── Motivo de exclusión (primera razón que falla)
        reason = ""
        if not has_price:
            reason = "sin precio USD válido en BYMA"
        elif not has_mae_cf:
            mae_exists = mae_ticker in mae
            if mae_exists:
                reason = "MAE devuelve respuesta vacía (sin detalle)"
            else:
                reason = "no buscado en MAE (sin precio en fecha de corrida)"
        elif not has_ytm:
            reason = "error en cálculo de YTM"
        elif is_outlier:
            reason = "outlier (YTM o duration fuera de rango)"
        # else: en universo final

        rows.append({
            "base":          base,
            "issuer":        issuer,
            "cuit":          cuit,
            "sector":        sector,
            "byma_symbols":  " ".join(byma_symbols),
            "currencies":    " ".join(currencies),
            "in_byma":       in_byma,
            "has_issuer":    has_issuer,
            "has_rating":    has_rating,
            "rating":        rating_val or "",
            "has_price":     has_price,
            "price_usd":     round(price_usd, 4) if price_usd else None,
            "price_src":     price_src,
            "has_mae_cf":    has_mae_cf,
            "n_cashflows":   n_cashflows if has_mae_cf else 0,
            "mae_currency":  mae_currency,
            "has_ytm":       has_ytm,
            "ytm_pct":       round(ytm * 100, 3) if has_ytm else None,
            "in_rv":         in_rv,
            "is_outlier":    is_outlier,
            "rv_score":      rv_score,
            "exclusion_reason": reason,
        })

    audit = pd.DataFrame(rows)
    audit.to_csv(OUT_FILE, index=False)
    print(f"\nAudit guardado → {OUT_FILE}  ({len(audit)} bases)\n")

    # ─── Embudo ───────────────────────────────────────────────────────────────
    n_byma     = len(audit)
    n_price    = audit["has_price"].sum()
    n_mae_cf   = audit["has_mae_cf"].sum()
    n_ytm      = audit["has_ytm"].sum()
    n_rv_total = audit["in_rv"].sum()
    n_rv_clean = audit[audit["in_rv"] & ~audit["is_outlier"].astype(object).fillna(False)].shape[0]

    print("=" * 60)
    print("EMBUDO DEL PIPELINE — ON Radar")
    print("=" * 60)
    print(f"  BYMA total (bases únicas)         : {n_byma:>4}")
    print(f"  ↓ con precio USD válido            : {n_price:>4}  (-{n_byma - n_price})")
    print(f"  ↓ con cashflows MAE                : {n_mae_cf:>4}  (-{n_price - n_mae_cf})")
    print(f"  ↓ con YTM calculada                : {n_ytm:>4}  (-{n_mae_cf - n_ytm})")
    print(f"  ↓ en relative_value.csv            : {n_rv_total:>4}  (-{n_ytm - n_rv_total})")
    print(f"  ↓ no outlier (universo final RV)   : {n_rv_clean:>4}  (-{n_rv_total - n_rv_clean})")
    print("=" * 60)

    # ─── Pérdida detallada por etapa ──────────────────────────────────────────
    no_price   = audit[~audit["has_price"]]
    no_mae_cf  = audit[audit["has_price"] & ~audit["has_mae_cf"]]
    no_ytm_row = audit[audit["has_mae_cf"] & ~audit["has_ytm"]]

    print(f"\n--- Pérdida etapa 2 (sin precio): {len(no_price)} bases ---")
    print("  (bonos en BYMA pero solo con precio=0 en todas las variantes)")
    # Mostrar distribución de sufijos disponibles
    no_price_suffixes = byma[byma["base"].isin(no_price["base"])].groupby("suffix")["base"].count()
    print("  Sufijos disponibles (precio=0):")
    for suf, cnt in no_price_suffixes.items():
        print(f"    {suf}: {cnt}")

    print(f"\n--- Pérdida etapa 3 (MAE vacío): {len(no_mae_cf)} bases ---")
    # Sample con emisor para mostrar cuáles son conocidos
    sample_mae = no_mae_cf[["base", "issuer", "mae_currency", "exclusion_reason"]].copy()
    # Mostrar los que tienen emisor (conocidos)
    known = sample_mae[sample_mae["issuer"] != ""]
    unknown = sample_mae[sample_mae["issuer"] == ""]
    print(f"  Con emisor identificado: {len(known)}")
    print(f"  Sin mapeo de emisor:     {len(unknown)}")
    print()
    if len(known) > 0:
        print("  Muestra — bonos conocidos sin cashflows MAE:")
        for _, r in known.head(20).iterrows():
            print(f"    {r['base']:8s}  {r['issuer'][:35]:35s}  {r['exclusion_reason']}")

    if len(no_ytm_row) > 0:
        print(f"\n--- Pérdida etapa 4 (sin YTM): {len(no_ytm_row)} ---")
        for _, r in no_ytm_row.iterrows():
            print(f"    {r['base']:8s}  {r['exclusion_reason']}")

    # ─── Cobertura de rating en universo final ────────────────────────────────
    final = audit[audit["in_rv"] & ~audit["is_outlier"].astype(object).fillna(False)]
    print(f"\n--- Universo final RV ({len(final)} bonos) ---")
    print("  Con rating:    ", final["has_rating"].sum())
    print("  Sin rating:    ", (~final["has_rating"]).sum())
    print()
    print("  Ratings presentes:")
    for r in sorted(final[final["has_rating"]]["rating"].dropna().unique(), key=str):
        n = (final["rating"] == r).sum()
        print(f"    {r}: {n}")

    # ─── Inventario de MAE vacíos por emisor (diagnóstico) ───────────────────
    print(f"\n--- Top emisores afectados por MAE vacío ---")
    mae_empty_by_issuer = (
        no_mae_cf[no_mae_cf["issuer"] != ""]
        .groupby("issuer")
        .size()
        .sort_values(ascending=False)
        .head(15)
    )
    for iss, cnt in mae_empty_by_issuer.items():
        print(f"  {cnt:>3}  {iss}")


if __name__ == "__main__":
    main()
