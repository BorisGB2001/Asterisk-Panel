"""Server-side SVG charts (no JS libraries, strict CSP). Colours come from CSS classes (light/dark theme)."""
from __future__ import annotations

import math
from html import escape

from .fmt import WEEKDAYS
from .i18n import t

W, H, PAD_L, PAD_B, PAD_T = 720, 180, 40, 22, 8


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * p:
            return max(m * p, 1)
    return 10 * p


def bars(points: list[tuple[str, list[float]]], series: list[str]) -> str:
    """Stacked columns: points = [(label, [value per series])]; series = legend names (s1, s2, s3 colours)."""
    if not points or not any(sum(v) for _, v in points):
        return f'<p class="muted small">{t("No calls in this period.")}</p>'
    n = len(points)
    top = _nice_max(max(sum(v) for _, v in points))
    pw, ph = W - PAD_L - 4, H - PAD_B - PAD_T
    bw = pw / n
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" preserveAspectRatio="none">']
    for i in range(5):
        y = PAD_T + ph * i / 4
        val = top * (4 - i) / 4
        out.append(f'<line class="grid" x1="{PAD_L}" x2="{W}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="axis" x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{val:g}</text>')
    step = max(1, math.ceil(n / 12))
    for i, (label, vals) in enumerate(points):
        x = PAD_L + i * bw
        y = PAD_T + ph
        tip = escape(f"{label}: " + ", ".join(f"{s} {int(v)}" for s, v in zip(series, vals)))
        parts = [f'<g><title>{tip}</title>']
        for j, v in enumerate(vals):
            h = ph * v / top
            y -= h
            parts.append(f'<rect class="s{j + 1}" x="{x + 0.5:.1f}" y="{y:.1f}" width="{max(bw - 1, 0.5):.1f}" height="{h:.1f}"/>')
        parts.append("</g>")
        out.append("".join(parts))
        if i % step == 0:
            out.append(f'<text class="axis" x="{x + bw / 2:.1f}" y="{H - 6}" text-anchor="middle">{escape(label)}</text>')
    out.append("</svg>")
    out.append('<div class="legend">' + "".join(f'<span class="sw s{j + 1}"></span>{escape(s)} ' for j, s in enumerate(series))
               + "</div>")
    return "".join(out)


def heatmap(cells: dict[tuple[int, int], float]) -> str:
    """Calls by weekday (rows, Mon…Sun) and hour (columns 0…23)."""
    days = [t(d) for d in WEEKDAYS]
    top = max(cells.values(), default=0) or 1
    cw, chh, left, topp = 26, 18, 28, 16
    out = [f'<svg class="heat" viewBox="0 0 {left + 24 * cw} {topp + 7 * chh + 2}" role="img">']
    for h in range(0, 24, 3):
        out.append(f'<text class="axis" x="{left + h * cw + cw / 2}" y="11" text-anchor="middle">{h:02d}</text>')
    for d in range(7):
        out.append(f'<text class="axis" x="{left - 6}" y="{topp + d * chh + 13}" text-anchor="end">{days[d]}</text>')
        for h in range(24):
            v = cells.get((d, h), 0)
            op = 0.08 + 0.92 * (v / top) ** 0.5 if v else 0.04
            out.append(f'<rect class="hc" x="{left + h * cw + 1}" y="{topp + d * chh + 1}" width="{cw - 2}" height="{chh - 2}" '
                       f'rx="2" fill-opacity="{op:.2f}"><title>{days[d]} {h:02d}:00 — {int(v)}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def hbars(rows: list[tuple[str, float, str]], fmt=lambda v: f"{int(v)}") -> str:
    """Horizontal bars «label — value»: rows = (label, value, link or "")."""
    if not rows:
        return f'<p class="muted small">{t("No data.")}</p>'
    top = max(v for _, v, _ in rows) or 1
    rh, w, left, right = 24, 560, 200, 70
    out = [f'<svg class="hbars" viewBox="0 0 {w} {len(rows) * rh + 4}" role="img">']
    for i, (label, v, href) in enumerate(rows):
        y = i * rh + 2
        bw = max((w - left - right) * v / top, 1)
        name = label if len(label) <= 28 else label[:27] + "…"
        inner = (f'<text class="axis lbl" x="{left - 8}" y="{y + 16}" text-anchor="end">{escape(name)}<title>{escape(label)}</title></text>'
                 f'<rect class="s1" x="{left}" y="{y + 4}" width="{bw:.1f}" height="{rh - 8}" rx="2"/>'
                 f'<text class="axis" x="{left + bw + 6:.1f}" y="{y + 16}">{escape(fmt(v))}</text>')
        out.append(f'<a href="{escape(href)}">{inner}</a>' if href else f"<g>{inner}</g>")
    out.append("</svg>")
    return "".join(out)
