"""Shared PDF-generation helpers for the app's "Export PDF" buttons, mirroring
the existing openpyxl-based "Export Excel" endpoints (see app/api/v1/counters.py,
accounting.py, inventory.py) — same data, same branch/period filters, just a
printable table layout instead of a spreadsheet.
"""
from io import BytesIO
from pathlib import Path

import re

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core import clock

# Myanmar text: Helvetica has no Myanmar glyphs (prints ■■■). Burmese runs are
# switched to Noto Sans Myanmar; with uharfbuzz installed ReportLab shapes them.
MM_FONT = "NotoMM"
_MM_RUN = re.compile(r"([\u1000-\u109f\uaa60-\uaa7f\ua9e0-\ua9ff\u200c\u200d]+(?:\s+[\u1000-\u109f\uaa60-\uaa7f\ua9e0-\ua9ff\u200c\u200d]+)*)")
try:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont(MM_FONT, str(Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansMyanmar.ttf")))
    _HAS_MM = True
except Exception:  # noqa: BLE001 — a missing font must not break the export
    _HAS_MM = False


def has_myanmar(text) -> bool:
    return isinstance(text, str) and bool(_MM_RUN.search(text))


def mm_markup(text: str) -> str:
    """Escape for Paragraph markup and put Burmese runs in the Myanmar font."""
    t = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MM_RUN.sub(lambda m: f'<font name="{MM_FONT}">{m.group(1)}</font>', t) if _HAS_MM else t

# Taken from the letterhead itself so tables match its green/yellow bands.
BRAND_COLOR = colors.HexColor("#148d00")
ACCENT_COLOR = colors.HexColor("#f3dd31")
STRIPE_COLOR = colors.HexColor("#f2f8ef")
GRID_COLOR = colors.HexColor("#d9e6d4")
TEXT_COLOR = colors.HexColor("#1f2937")
MUTED_COLOR = colors.HexColor("#6b7280")
_NUMBER = re.compile(r"^-?[\d,]+(\.\d+)?( MMK)?$")
# Same letterhead the Lab/X-ray result slips print (frontend/src/assets/letterhead.png).
LETTERHEAD = Path(__file__).resolve().parent.parent / "assets" / "letterhead.png"
LETTERHEAD_RATIO = 412 / 1600


def _footer(canvas, doc) -> None:
    canvas.saveState()
    w, _ = doc.pagesize
    y = doc.bottomMargin - 6 * mm
    canvas.setStrokeColor(BRAND_COLOR)
    canvas.setLineWidth(0.6)
    canvas.line(doc.leftMargin, y + 4 * mm, w - doc.rightMargin, y + 4 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED_COLOR)
    canvas.drawString(doc.leftMargin, y, f"Printed {clock.now().strftime('%d %b %Y, %I:%M %p')}")
    canvas.drawRightString(w - doc.rightMargin, y, f"Page {doc.page}")
    canvas.restoreState()


class _Doc(SimpleDocTemplate):
    def build(self, flowables, **kw):
        kw.setdefault("onFirstPage", _footer)
        kw.setdefault("onLaterPages", _footer)
        return super().build(flowables, **kw)


def new_pdf_document(buf: BytesIO, *, landscape_mode: bool = False) -> SimpleDocTemplate:
    page_size = landscape(A4) if landscape_mode else A4
    return _Doc(
        buf,
        pagesize=page_size,
        topMargin=12 * mm,
        bottomMargin=16 * mm,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        title="Hospital",
    )


def header_elements(title: str, meta: list[tuple[str, str]], width: float = A4[0] - 24 * mm) -> list:
    styles = getSampleStyleSheet()
    if LETTERHEAD.exists():
        w = min(width, 150 * mm)
        elements = [Image(str(LETTERHEAD), width=w, height=w * LETTERHEAD_RATIO), Spacer(1, 6)]
    else:
        elements = [Paragraph("Hospital", styles["Heading2"])]
    title_style = ParagraphStyle("ReportTitle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=BRAND_COLOR)
    meta_style = ParagraphStyle("ReportMeta", parent=styles["Normal"], fontSize=8.5, leading=12, textColor=MUTED_COLOR)
    elements.append(Paragraph(title, title_style))
    if meta:
        line = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(f"{label}: <font color='#1f2937'><b>{mm_markup(value)}</b></font>" for label, value in meta)
        elements.append(Paragraph(line, meta_style))
    elements.append(Spacer(1, 3))
    elements.append(HRFlowable(width="100%", thickness=1.2, color=ACCENT_COLOR, spaceBefore=2, spaceAfter=8))
    return elements


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}" if abs(value) >= 10000 else str(int(value))
    return str(value)


def styled_table(
    columns: list[str],
    rows: list[list],
    *,
    col_widths: list[float] | None = None,
    width: float | None = None,
    total: list | None = None,
    highlight: dict | None = None,
) -> Table:
    body = [[_cell(c) for c in row] for row in rows]
    if not body:
        body = [["No data"] + [""] * (len(columns) - 1)]
    data = [columns] + body + ([[_cell(c) for c in total]] if total else [])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_COLOR),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, ACCENT_COLOR),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TEXTCOLOR", (0, 1), (-1, -1), TEXT_COLOR),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, GRID_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1 if not total else -2), [colors.white, STRIPE_COLOR]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    if total:
        style += [
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e8f3e3")),
            ("LINEABOVE", (0, -1), (-1, -1), 1, BRAND_COLOR),
        ]
    # Right-align every column except the first when it holds numbers / money / counts.
    for i in range(1, len(columns)):
        vals = [r[i] for r in body if i < len(r) and r[i] not in ("", "—", "-")]
        if vals and all(_NUMBER.match(v) or v.endswith("%") or "·" in v or " / " in v for v in vals):
            style.append(("ALIGN", (i, 0), (i, -1), "RIGHT"))
    if width and not col_widths:
        # Fill the given width: first column takes the slack, numeric columns keep their natural width.
        probe = Table(data)
        probe.wrap(0, 0)
        nat = list(probe._colWidths)
        total_nat = sum(nat)
        if total_nat < width:
            # Give the spare room to the widest text column (name / item), not to dates or numbers.
            num_cols = {c[1][0] for c in style if c[0] == "ALIGN"}
            text_cols = [i for i in range(len(nat)) if i not in num_cols] or [0]
            grow = max(text_cols, key=lambda i: nat[i])
            nat[grow] += width - total_nat
            col_widths = nat
        else:
            col_widths = [w * width / total_nat for w in nat]
    if any(has_myanmar(c) for row in data for c in row):
        base = getSampleStyleSheet()["Normal"]
        cell_st = ParagraphStyle("MMCell", parent=base, fontSize=8.5, leading=12, textColor=TEXT_COLOR)
        head_st = ParagraphStyle("MMHead", parent=cell_st, fontName="Helvetica-Bold", textColor=colors.white)
        data = [[Paragraph(mm_markup(c), head_st if r == 0 else cell_st) if has_myanmar(c) else c for c in row] for r, row in enumerate(data)]
    for ri, color in (highlight or {}).items():
        style.append(("TEXTCOLOR", (0, ri + 1), (-1, ri + 1), color))
    table = Table(data, repeatRows=1, colWidths=col_widths, hAlign="LEFT")
    table.setStyle(TableStyle(style))
    return table


def section(title: str, table: Table) -> KeepTogether:
    """A heading kept on the same page as its table."""
    st = ParagraphStyle("Sec", parent=getSampleStyleSheet()["Normal"], fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=BRAND_COLOR, spaceBefore=4, spaceAfter=4)
    return KeepTogether([Paragraph(mm_markup(title), st), table, Spacer(1, 10)])


def side_by_side(left, right, *, width: float, gap: float = 8) -> Table:
    """Two small tables next to each other instead of stacked down the page."""
    half = (width - gap) / 2
    left = left._content if isinstance(left, KeepTogether) else left
    right = right._content if isinstance(right, KeepTogether) else right
    t = Table([[left, "", right]], colWidths=[half, gap, half], hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t


def kpi_grid(items: list[tuple[str, str]], *, width: float, per_row: int = 4) -> Table:
    """Summary numbers as cards: small label on top, big value underneath."""
    base = getSampleStyleSheet()["Normal"]
    lab = ParagraphStyle("KL", parent=base, fontSize=7.5, leading=10, textColor=MUTED_COLOR)
    val = ParagraphStyle("KV", parent=base, fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=TEXT_COLOR)
    cells = [[Paragraph(mm_markup(label), lab), Paragraph(mm_markup(value), val)] for label, value in items]
    rows = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
    while len(rows[-1]) < per_row:
        rows[-1].append("")
    cw = width / per_row
    t = Table(rows, colWidths=[cw] * per_row, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, GRID_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, GRID_COLOR),
        ("BACKGROUND", (0, 0), (-1, -1), STRIPE_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([t, Spacer(1, 12)])


def build_table_pdf(
    *,
    title: str,
    meta: list[tuple[str, str]],
    columns: list[str],
    rows: list[list],
    landscape_mode: bool = False,
    col_widths: list[float] | None = None,
    total: list | None = None,
    kpis: list[tuple[str, str]] | None = None,
    highlight: dict | None = None,
) -> BytesIO:
    """Title + metadata + (optional) summary cards + one data table with a total row."""
    buf = BytesIO()
    doc = new_pdf_document(buf, landscape_mode=landscape_mode)
    elements = header_elements(title, meta, width=doc.width)
    if kpis:
        elements.append(kpi_grid(kpis, width=doc.width, per_row=min(len(kpis), 4)))
    elements.append(styled_table(columns, rows, col_widths=col_widths, width=doc.width, total=total, highlight=highlight))
    doc.build(elements)
    buf.seek(0)
    return buf
