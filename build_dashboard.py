"""
Fase 9: Dashboard interactivo — ON Radar.

Lee signal_quality.csv (salida de build_signal_quality.py).
Si no existe, cae back a relative_value.csv.

Uso:
    python build_dashboard.py
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

SQ_CSV   = Path("outputs/signal_quality.csv")
RV_CSV   = Path("outputs/relative_value.csv")
OUT_HTML = Path("outputs/dashboard.html")

BUCKET_COLOR = {
    "AAA":     "#1565C0",
    "AA":      "#2E7D32",
    "A":       "#E65100",
    "BBB":     "#6A1B9A",
    "HY":      "#B71C1C",
    "Unrated": "#757575",
}

# Bonos excluidos de los gráficos visuales por distorsión extrema.
# La lógica de señales (cards, tabla) los trata correctamente vía signal_category.
EXCLUIR_GRAFICOS = {"SNABO"}


def load_data() -> pd.DataFrame:
    path = SQ_CSV if SQ_CSV.exists() else RV_CSV
    df = pd.read_csv(path)
    num_cols = [
        "ytm", "macaulay_duration", "rv_score", "spread_bps",
        "volume", "liquidity_score", "peer_avg_ytm",
        "upside_pct", "carry_advantage_pct", "theoretical_price",
        "cashflow_quality_score", "signal_confidence_score", "execution_score",
        "liquidity_percentile", "peer_count", "peer_unique_issuers",
        "peer_same_issuer_ratio",
    ]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _score_badge(label: str, value, abbrev: str = "", thresholds=(60, 75)) -> str:
    """Renders a small score badge with color coding."""
    if pd.isna(value):
        return ""
    v    = float(value)
    text = abbrev if abbrev else label[:2].upper()
    if v >= thresholds[1]:
        color, bg = "#1B5E20", "#E8F5E9"
    elif v >= thresholds[0]:
        color, bg = "#E65100", "#FFF3E0"
    else:
        color, bg = "#B71C1C", "#FFEBEE"
    return (f'<span title="{label}" style="display:inline-block;background:{bg};'
            f'color:{color};border-radius:4px;padding:1px 6px;font-size:0.72rem;'
            f'font-weight:600;margin-right:4px">{text} {v:.0f}</span>')


def _alert_html(alerts_str) -> str:
    """Renders active alerts as small warning items."""
    if not alerts_str or pd.isna(alerts_str) or str(alerts_str).strip() == "":
        return ""
    ids = [a.strip() for a in str(alerts_str).split("|") if a.strip()]
    if not ids:
        return ""
    _ALERT_LABELS = {
        "iliquidez":        ("⚠", "#E65100", "Posible distorsión por iliquidez"),
        "no_ejecutable":    ("🚫", "#B71C1C", "Señal posiblemente no ejecutable"),
        "precio_viejo":     ("⏱", "#7B1FA2", "Precio posiblemente desactualizado"),
        "cashflow_estimado":("~", "#E65100", "Cashflows no verificados"),
        "pocos_peers":      ("◌", "#1565C0", "Grupo de comparación pequeño"),
        "intra_curva":      ("↺", "#555", "Anomalía de curva propia del emisor"),
    }
    parts = []
    for aid in ids[:3]:   # máximo 3 alertas visibles
        icon, color, txt = _ALERT_LABELS.get(aid, ("•", "#555", aid))
        parts.append(
            f'<span style="color:{color};font-size:0.72rem;margin-right:8px">'
            f'{icon} {txt}</span>'
        )
    return '<div style="margin-top:6px">' + "".join(parts) + "</div>"


# ─── Sección 1: Cards de señales ─────────────────────────────────────────────

def cards_html(df: pd.DataFrame) -> str:
    """
    Cards de señales activas usando signal_category del signal_quality engine.
    Muestra: label, YTM, spread, upside, carry, scores de calidad, alertas.
    """
    has_sq = "signal_category" in df.columns

    if has_sq:
        baratos = df[
            df["signal_category"].isin(["POTENCIALMENTE_BARATO", "POSIBLE_OPORTUNIDAD"]) &
            (df["rv_score"] > 0) &
            ~df["is_outlier"].fillna(False)
        ].nlargest(5, "rv_score")

        caros = df[
            df["signal_category"].isin(["POTENCIALMENTE_CARO", "POSIBLE_OPORTUNIDAD"]) &
            (df["rv_score"] < 0) &
            ~df["is_outlier"].fillna(False)
        ].nsmallest(4, "rv_score")
    else:
        usable = df[~df["is_outlier"].fillna(False) & df["rv_score"].notna()]
        baratos = usable.nlargest(5, "rv_score")
        caros   = usable.nsmallest(4, "rv_score")

    def card(row, tipo: str) -> str:
        is_cheap  = tipo == "barato"
        cat       = str(row.get("signal_category", "")) if has_sq else ""

        # Colores y badge principal
        if cat == "POSIBLE_OPORTUNIDAD":
            main_color, bg_color = "#E65100", "#FFF8F0"
            border_color         = "#FF9800"
            cat_label            = "◆ POSIBLE OPORTUNIDAD — REQUIERE VALIDACIÓN"
        elif cat == "ANOMALIA_CURVA_PROPIA":
            main_color, bg_color = "#1565C0", "#E3F2FD"
            border_color         = "#1565C0"
            cat_label            = "↺ ANOMALÍA DE CURVA PROPIA"
        elif is_cheap:
            main_color, bg_color = "#1B5E20", "#F1F8E9"
            border_color         = "#2E7D32"
            cat_label            = "▲ POTENCIALMENTE BARATO"
        else:
            main_color, bg_color = "#B71C1C", "#FFEBEE"
            border_color         = "#C62828"
            cat_label            = "▼ POTENCIALMENTE CARO"

        symbol   = row["symbol"]
        issuer   = str(row.get("issuer", "—"))
        issuer   = issuer[:45] + ("…" if len(issuer) > 45 else "")
        rating   = str(row.get("rating_lp", "Sin rating")) if pd.notna(row.get("rating_lp")) else "Sin rating"
        currency = str(row.get("currency", "USD"))
        mat_date = str(row.get("maturity_date", ""))[:10]

        ytm    = f"{row['ytm']*100:.1f}%"     if pd.notna(row.get("ytm"))       else "—"
        spread = f"{row['spread_bps']:+.0f}bp" if pd.notna(row.get("spread_bps")) else "—"
        rv     = f"{row['rv_score']:+.2f}"     if pd.notna(row.get("rv_score"))  else "—"

        # Ganancia por convergencia / rendimiento extra
        upside_html = ""
        if pd.notna(row.get("upside_pct")):
            up   = float(row["upside_pct"])
            uclr = main_color
            upside_html = (
                f'<span style="font-size:0.82rem;font-weight:700;color:{uclr}">'
                f'Ganancia por convergencia: {up:+.1f}%</span>'
            )
        carry_html = ""
        if pd.notna(row.get("carry_advantage_pct")):
            ca = float(row["carry_advantage_pct"])
            carry_html = (
                f'<span style="font-size:0.75rem;color:#666;margin-left:10px">'
                f'Rendimiento extra vs pares: {ca:+.1f}% anual</span>'
            )

        # Badges de scores
        cfq_badge  = _score_badge("Calidad de cashflows", row.get("cashflow_quality_score"),  abbrev="CF")
        sigc_badge = _score_badge("Confianza de la señal", row.get("signal_confidence_score"), abbrev="SC")
        exe_badge  = _score_badge("Ejecución",             row.get("execution_score"),          abbrev="EJ")

        # Alertas
        alert_section = _alert_html(row.get("alerts", ""))

        # Peers info
        peers_info = ""
        if pd.notna(row.get("peer_count")) and pd.notna(row.get("peer_unique_issuers")):
            pc = int(row["peer_count"])
            pe = int(row["peer_unique_issuers"])
            peers_info = (
                f'<span style="font-size:0.72rem;color:#777">'
                f'comparado vs {pc} bonos ({pe} emisores distintos)</span>'
            )

        return f"""
        <div style="background:{bg_color};border-left:4px solid {border_color};
                    border-radius:6px;padding:14px 16px;margin-bottom:12px">
          <div style="font-size:0.7rem;font-weight:700;color:{main_color};
                      letter-spacing:0.04em;margin-bottom:4px">{cat_label}</div>
          <div style="display:flex;justify-content:space-between;align-items:flex-start">
            <div>
              <div style="font-size:1.1rem;font-weight:700;color:{main_color}">{symbol}</div>
              <div style="font-size:0.8rem;color:#444;margin-top:1px">{issuer}</div>
              <div style="font-size:0.75rem;color:#777">{rating} · {currency} · vence {mat_date}</div>
            </div>
            <div style="text-align:right;flex-shrink:0;padding-left:12px">
              <div style="font-size:1.25rem;font-weight:700;color:{main_color}">{ytm}</div>
              <div style="font-size:0.78rem;color:#555">{spread} vs comparables</div>
              <div style="font-size:0.75rem;color:#777">RV score {rv}</div>
            </div>
          </div>
          <div style="margin-top:8px">
            {upside_html}{carry_html}
          </div>
          <div style="margin-top:8px;display:flex;align-items:center;flex-wrap:wrap;gap:2px">
            {cfq_badge}{sigc_badge}{exe_badge}
            <span style="margin-left:4px">{peers_info}</span>
          </div>
          {alert_section}
        </div>"""

    baratos_html = "".join(card(r, "barato") for _, r in baratos.iterrows())
    caros_html   = "".join(card(r, "caro")   for _, r in caros.iterrows())

    legend = """
    <div style="font-size:0.72rem;color:#777;margin-top:6px;padding:8px 12px;
                background:#F5F5F5;border-radius:4px">
      Scores de calidad (0–100, mayor = mejor):
      <strong>CF</strong> calidad de cashflows ·
      <strong>SC</strong> confianza de la señal ·
      <strong>EJ</strong> ejecución.
      &nbsp;
      <span style="background:#E8F5E9;color:#1B5E20;border-radius:3px;padding:0 4px">verde ≥75</span>
      <span style="background:#FFF3E0;color:#E65100;border-radius:3px;padding:0 4px">naranja 60–74</span>
      <span style="background:#FFEBEE;color:#B71C1C;border-radius:3px;padding:0 4px">rojo &lt;60</span>
    </div>"""

    return f"""
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:8px">
      <div>
        <h3 style="font-size:0.88rem;font-weight:600;color:#1B5E20;margin-bottom:12px;
                   text-transform:uppercase;letter-spacing:0.05em">
          Baratos / Posibles oportunidades
          <span style="font-weight:400;color:#666;font-size:0.72rem;text-transform:none">
            — rinden más de lo esperado para su riesgo
          </span>
        </h3>
        {baratos_html}
      </div>
      <div>
        <h3 style="font-size:0.88rem;font-weight:600;color:#B71C1C;margin-bottom:12px;
                   text-transform:uppercase;letter-spacing:0.05em">
          Caros / Para revisar posición
          <span style="font-weight:400;color:#666;font-size:0.72rem;text-transform:none">
            — rinden menos de lo esperado para su riesgo
          </span>
        </h3>
        {caros_html}
      </div>
    </div>
    {legend}"""


# ─── Sección 2: Yield Map ─────────────────────────────────────────────────────

def build_yield_map(df: pd.DataFrame) -> go.Figure:
    plot_df = df[
        ~df["is_outlier"].fillna(False) &
        df["ytm"].notna() &
        df["macaulay_duration"].notna() &
        ~df["symbol"].isin(EXCLUIR_GRAFICOS) &
        (df["ytm"] < 0.25)
    ].copy()

    fig = go.Figure()

    for bucket, color in BUCKET_COLOR.items():
        sub = plot_df[plot_df["rating_bucket"] == bucket]
        if sub.empty:
            continue

        hover = []
        for _, r in sub.iterrows():
            conf   = r.get("cashflow_confidence", "mae")
            est    = " [estimado]" if conf == "estimated" else ""
            issuer = str(r.get("issuer", ""))[:38]
            spread = f"{r['spread_bps']:+.0f}bp" if pd.notna(r.get("spread_bps")) else "—"
            rv     = f"{r['rv_score']:+.2f}"      if pd.notna(r.get("rv_score"))  else "—"
            upside = f"{r['upside_pct']:+.1f}%"   if pd.notna(r.get("upside_pct")) else ""
            upside_line = f"<br>Upside potencial: {upside}" if upside else ""
            cat    = str(r.get("signal_label", "")) if pd.notna(r.get("signal_label")) else ""
            cat_line = f"<br><b>{cat}</b>" if cat else ""
            hover.append(
                f"<b>{r['symbol']}</b>{est}{cat_line}<br>"
                f"{issuer}<br>"
                f"YTM: {r['ytm']*100:.2f}%  |  Dur: {r['macaulay_duration']:.1f}a<br>"
                f"Spread vs pares: {spread} · RV: {rv}{upside_line}"
            )

        fig.add_trace(go.Scatter(
            x=sub["macaulay_duration"],
            y=sub["ytm"] * 100,
            mode="markers+text",
            marker=dict(size=11, color=color, opacity=0.85,
                        line=dict(width=1, color="white")),
            text=sub["symbol"],
            textposition="top center",
            textfont=dict(size=8),
            hovertemplate="%{meta}<extra></extra>",
            meta=hover,
            name=bucket,
        ))

    fig.update_layout(
        xaxis=dict(title="Duración (años) →  más largo = más riesgo de tasa",
                   gridcolor="#EEE", tickformat=".1f"),
        yaxis=dict(title="Rendimiento anual (YTM %)",
                   gridcolor="#EEE", ticksuffix="%", tickformat=".1f"),
        legend=dict(title="Calificación", x=1.01, y=1),
        height=480,
        margin=dict(l=60, r=160, t=20, b=60),
        plot_bgcolor="#FAFAFA",
        paper_bgcolor="white",
        hovermode="closest",
    )
    return fig


# ─── Sección 3: RV Score Bar ─────────────────────────────────────────────────

def build_rv_bar(df: pd.DataFrame) -> go.Figure:
    sub = df[
        ~df["is_outlier"].fillna(False) &
        df["rv_score"].notna() &
        ~df["symbol"].isin(EXCLUIR_GRAFICOS) &
        (df["rv_score"].abs() < 15)   # excluir outliers extremos del gráfico
    ].sort_values("rv_score").copy()

    colors = [
        "#B71C1C" if v < -1.5 else
        "#EF9A9A" if v < 0    else
        "#A5D6A7" if v < 1.5  else
        "#1B5E20"
        for v in sub["rv_score"]
    ]

    labels = []
    for _, r in sub.iterrows():
        conf = r.get("cashflow_confidence", "mae")
        labels.append(r["symbol"] + (" ~" if conf == "estimated" else ""))

    fig = go.Figure(go.Bar(
        y=labels,
        x=sub["rv_score"],
        orientation="h",
        marker_color=colors,
        text=[f"{v:+.2f}" for v in sub["rv_score"]],
        textposition="outside",
        textfont=dict(size=8),
        hovertemplate="<b>%{y}</b><br>RV score: %{x:.2f}<extra></extra>",
    ))

    fig.add_vline(x=0,    line_color="#999", line_width=1)
    fig.add_vline(x=1.5,  line_color="#1B5E20", line_dash="dash", line_width=1)
    fig.add_vline(x=-1.5, line_color="#B71C1C", line_dash="dash", line_width=1)

    fig.add_annotation(x=1.5,  y=len(sub)-0.5, text="señal compra", showarrow=False,
                       font=dict(size=9, color="#1B5E20"), xanchor="left")
    fig.add_annotation(x=-1.5, y=len(sub)-0.5, text="señal cara",   showarrow=False,
                       font=dict(size=9, color="#B71C1C"), xanchor="right")

    fig.update_layout(
        xaxis=dict(title="← caro    |    barato →", gridcolor="#EEE"),
        yaxis=dict(tickfont=dict(size=9)),
        height=max(380, len(sub) * 17 + 80),
        margin=dict(l=90, r=70, t=20, b=50),
        plot_bgcolor="#FAFAFA",
        paper_bgcolor="white",
        showlegend=False,
    )
    return fig


# ─── Tabla completa ───────────────────────────────────────────────────────────

def tabla_html(df: pd.DataFrame) -> str:
    has_sq = "signal_category" in df.columns

    usable = df[
        ~df["is_outlier"].fillna(False) &
        df["rv_score"].notna() &
        ~df["symbol"].isin(EXCLUIR_GRAFICOS)
    ].sort_values("rv_score", ascending=False).copy()

    rows = []
    for _, r in usable.iterrows():
        conf    = r.get("cashflow_confidence", "mae")
        est     = " ~" if conf == "estimated" else ""
        rv      = float(r["rv_score"])
        color   = "#1B5E20" if rv > 1.5 else ("#B71C1C" if rv < -1.5 else "#333")
        bg      = "#F1F8E9" if rv > 1.5 else ("#FFEBEE" if rv < -1.5 else "white")
        issuer  = str(r.get("issuer", "—"))[:35]
        rating  = str(r.get("rating_lp", "—")) if pd.notna(r.get("rating_lp")) else "—"
        spread  = f"{r['spread_bps']:+.0f}" if pd.notna(r.get("spread_bps")) else "—"
        peer_y  = f"{r['peer_avg_ytm']*100:.2f}%" if pd.notna(r.get("peer_avg_ytm")) else "—"
        upside  = f"{r['upside_pct']:+.1f}%" if pd.notna(r.get("upside_pct")) else "—"

        # Quality scores (only if signal_quality data available)
        cfq_cell  = f"{r['cashflow_quality_score']:.0f}"  if has_sq and pd.notna(r.get("cashflow_quality_score"))  else "—"
        sigc_cell = f"{r['signal_confidence_score']:.0f}" if has_sq and pd.notna(r.get("signal_confidence_score")) else "—"
        exe_cell  = f"{r['execution_score']:.0f}"          if has_sq and pd.notna(r.get("execution_score"))          else "—"

        # Signal label badge
        cat       = str(r.get("signal_category", "")) if has_sq else ""
        cat_color = {
            "POTENCIALMENTE_BARATO": "#1B5E20",
            "POTENCIALMENTE_CARO":   "#B71C1C",
            "POSIBLE_OPORTUNIDAD":   "#E65100",
            "ANOMALIA_CURVA_PROPIA": "#1565C0",
        }.get(cat, "#555")
        cat_short = {
            "POTENCIALMENTE_BARATO": "▲ Barato",
            "POTENCIALMENTE_CARO":   "▼ Caro",
            "POSIBLE_OPORTUNIDAD":   "◆ Validar",
            "ANOMALIA_CURVA_PROPIA": "↺ Curva",
            "NEUTRO":                "",
        }.get(cat, "")
        cat_cell = (f'<span style="color:{cat_color};font-size:0.75rem;font-weight:600">'
                    f'{cat_short}</span>') if cat_short else ""

        rows.append(f"""
          <tr style="background:{bg}">
            <td style="color:{color};font-weight:600">{r['symbol']}{est}</td>
            <td>{issuer}</td>
            <td>{rating}</td>
            <td style="text-align:right">{r['ytm']*100:.2f}%</td>
            <td style="text-align:right">{peer_y}</td>
            <td style="text-align:right;color:{color};font-weight:600">{spread}bp</td>
            <td style="text-align:right;color:{color};font-weight:700">{rv:+.2f}</td>
            <td style="text-align:right">{upside}</td>
            <td style="text-align:center">{cfq_cell}</td>
            <td style="text-align:center">{sigc_cell}</td>
            <td style="text-align:center">{exe_cell}</td>
            <td>{cat_cell}</td>
          </tr>""")

    rows_html = "\n".join(rows)

    quality_headers = ("""
          <th style="padding:8px 10px;text-align:center">Calidad cashflows</th>
          <th style="padding:8px 10px;text-align:center">Confianza señal</th>
          <th style="padding:8px 10px;text-align:center">Ejecución</th>
          <th style="padding:8px 10px;text-align:left">Señal</th>
    """ if has_sq else "")

    return f"""
    <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:0.82rem">
      <thead>
        <tr style="background:#F5F5F5;border-bottom:2px solid #DDD">
          <th style="padding:8px 10px;text-align:left">Ticker</th>
          <th style="padding:8px 10px;text-align:left">Emisor</th>
          <th style="padding:8px 10px;text-align:left">Rating</th>
          <th style="padding:8px 10px;text-align:right">YTM</th>
          <th style="padding:8px 10px;text-align:right">YTM pares</th>
          <th style="padding:8px 10px;text-align:right">Spread</th>
          <th style="padding:8px 10px;text-align:right">RV score</th>
          <th style="padding:8px 10px;text-align:right">Ganancia convergencia</th>
          {quality_headers}
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    </div>
    <p style="font-size:0.72rem;color:#888;margin-top:8px">
      ~ = cashflows estimados. RV &gt;1.5 = potencialmente barato. RV &lt;-1.5 = potencialmente caro.
      Calidad cashflows / Confianza señal / Ejecución = scores de calidad (0–100, mayor = mejor).
      Ganancia convergencia = variación de precio estimada si el bono cotizara en línea con sus comparables
      (aproximación por duración modificada, primer orden).
    </p>"""


# ─── Build HTML ───────────────────────────────────────────────────────────────

def build_html(cards: str, fig_map: go.Figure, fig_rv: go.Figure,
               tabla: str, out_path: Path, run_date: str, n_usable: int = 0) -> None:

    map_html = fig_map.to_html(full_html=False, include_plotlyjs=False,
                               config={"responsive": True})
    rv_html  = fig_rv.to_html(full_html=False,  include_plotlyjs=False,
                               config={"responsive": True})

    page = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ON Radar — {run_date}</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    * {{ box-sizing:border-box; margin:0; padding:0 }}
    body {{ font-family: Inter,-apple-system,Arial,sans-serif; background:#F0F2F5; color:#1A1A1A }}
    header {{ background:#0D1B2A; color:white; padding:16px 32px; display:flex; align-items:baseline; gap:14px }}
    header h1 {{ font-size:1.3rem; font-weight:700 }}
    header span {{ font-size:0.82rem; color:#90CAF9 }}
    .wrap {{ max-width:1300px; margin:0 auto; padding:28px 24px }}
    .card {{ background:white; border-radius:8px; box-shadow:0 1px 4px rgba(0,0,0,.08);
              padding:24px; margin-bottom:24px }}
    .card h2 {{ font-size:1rem; font-weight:600; margin-bottom:16px; color:#111 }}
    .glosario {{ background:#E3F2FD; border-radius:6px; padding:14px 18px;
                 font-size:0.8rem; color:#1565C0; line-height:1.8 }}
    footer {{ text-align:center; padding:16px; font-size:0.72rem; color:#aaa }}
    td, th {{ padding:7px 10px; border-bottom:1px solid #F0F0F0 }}
  </style>
</head>
<body>
<header>
  <h1>ON Radar</h1>
  <span>Análisis de Valor Relativo · ONs Corporativas USD Argentina · {run_date}</span>
</header>
<div class="wrap">

  <div class="card glosario">
    <strong>Cómo leer esto:</strong>
    El sistema compara el rendimiento de cada ON contra bonos similares (mismo rating, duración parecida).
    Un <strong>RV score &gt;1.5</strong> significa que rinde más de lo esperado para su nivel de riesgo → potencialmente barato.
    <strong>Ganancia por convergencia:</strong> cuánto subiría el precio si el bono cotizara en línea con sus comparables — <em>(precio teórico − precio actual) / precio actual</em>.
    <strong>Rendimiento extra vs pares:</strong> diferencia entre la YTM del bono y la YTM promedio de sus comparables — <em>YTM bono − YTM pares</em>. Indica cuánto rinde de más este bono por año frente a similares, independientemente de si el precio converge o no.
    Los badges de calidad muestran la confiabilidad de los datos y la ejecutabilidad de la operación (0–100).
    Las señales <strong>◆ REQUIERE VALIDACIÓN</strong> tienen algún factor de calidad por debajo del umbral — revisar antes de operar.
  </div>

  <div class="card" style="margin-top:20px">
    <h2>Señales del día</h2>
    {cards}
  </div>

  <div class="card">
    <h2>Mapa de rendimientos — hover para ver detalle de cada bono</h2>
    {map_html}
  </div>

  <div class="card">
    <h2>Ranking de valor relativo — verde = barato, rojo = caro</h2>
    {rv_html}
  </div>

  <div class="card">
    <h2>Tabla completa · {run_date}</h2>
    {tabla}
  </div>

  <div class="card" style="font-size:0.78rem;color:#555;line-height:1.8">
    <h2 style="margin-bottom:10px">Fuentes de datos</h2>
    <table style="border-collapse:collapse;width:100%">
      <tr><td style="padding:3px 12px 3px 0;font-weight:600;color:#333;white-space:nowrap">Precios</td>
          <td>BYMA (Bolsas y Mercados Argentinos) — Open Data API · {run_date}</td></tr>
      <tr><td style="padding:3px 12px 3px 0;font-weight:600;color:#333;white-space:nowrap">Flujos de fondos</td>
          <td>MAE (Mercado Abierto Electrónico) — <code>/emisiones/flujofondos</code>.
          Para bonos sin cobertura MAE: construcción manual verificada contra prospectos CNV.
          Bonos marcados <strong>~</strong> usan parámetros estimados.</td></tr>
      <tr><td style="padding:3px 12px 3px 0;font-weight:600;color:#333;white-space:nowrap">Calificaciones</td>
          <td>FIX SCR / Fitch Ratings Argentina (escala local).</td></tr>
      <tr><td style="padding:3px 12px 3px 0;font-weight:600;color:#333;white-space:nowrap">Metodología</td>
          <td>Z-score del YTM vs grupo de pares (misma moneda + rating + duración ±1.5 años).
          {n_usable} ONs con cashflows válidos en el universo.<br>
          <strong>Ganancia por convergencia</strong> = (precio teórico − precio actual) / precio actual,
          donde precio teórico se calcula descontando los cashflows a la YTM promedio de pares
          (aproximación de primer orden por duración modificada).<br>
          <strong>Rendimiento extra vs pares</strong> = YTM bono − YTM promedio pares.</td></tr>
    </table>
  </div>

</div>
<footer>ON Radar · {run_date} · análisis de valor relativo · no constituye recomendación de inversión</footer>
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    print(f"Dashboard → {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT_HTML)
    args = ap.parse_args()

    df       = load_data()
    run_date = date.today().isoformat()

    n_usable = int((~df["is_outlier"].fillna(False) & ~df["symbol"].isin(EXCLUIR_GRAFICOS)).sum())
    build_html(
        cards    = cards_html(df),
        fig_map  = build_yield_map(df),
        fig_rv   = build_rv_bar(df),
        tabla    = tabla_html(df),
        out_path = args.out,
        run_date = run_date,
        n_usable = n_usable,
    )


if __name__ == "__main__":
    main()
