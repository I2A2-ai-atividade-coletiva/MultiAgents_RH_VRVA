from __future__ import annotations

from pathlib import Path
import re
from typing import Dict, Optional

# Docling
try:
    from docling.document_converter import DocumentConverter
except Exception as e:  # pragma: no cover
    DocumentConverter = None  # type: ignore


VR_LABELS = re.compile(
    r"(?:\bVR\b|"
    r"vale[-\s]?refei[cç][aã]o|aux[ií]lio[-\s]?refei[cç][aã]o|refei[cç][aã]o|tele[-\s]?refei[cç][aã]o|"
    r"ticket[-\s]?refei[cç][aã]o|cart[aã]o[-\s]?refei[cç][aã]o|benef[ií]cio[-\s]?refei[cç][aã]o|refei[cç][aã]o[-\s]?conv[eê]nio"
    r")",
    re.IGNORECASE,
)
VA_LABELS = re.compile(
    r"(?:\bVA\b|"
    r"vale[-\s]?alimenta[cç][aã]o|aux[ií]lio[-\s]?alimenta[cç][aã]o|alimenta[cç][aã]o|"
    r"ticket[-\s]?alimenta[cç][aã]o|cart[aã]o[-\s]?alimenta[cç][aã]o|cesta[-\s]?b[aá]sica|aux[ií]lio[-\s]?cesta|alimenta[cç][aã]o[-\s]?conv[eê]nio"
    r")",
    re.IGNORECASE,
)

# R$ 1.234,56 | 123,45 | 1000,00 etc.
BRL_VALUE = r"R?\$?\s*\d{1,3}(?:\.\d{3})*,\d{2}|\d+(?:,\d{2})?"
PCT_VALUE = r"\d{1,3}(?:,\d+)?\s*%"
BRL_RE = re.compile(BRL_VALUE)
PCT_RE = re.compile(PCT_VALUE)


def _norm_brl_to_float(s: str | None) -> Optional[float]:
    if not s:
        return None
    # reject percentage-like values
    if PCT_RE.fullmatch(s.strip()):
        return None
    # Ensure we keep only the first value-like token
    m = BRL_RE.search(s)
    if not m:
        return None
    v = m.group(0)
    v = v.replace("R$", "").replace("$", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(v)
    except Exception:
        return None


def _search_kv_nearby(text: str, label_regex: re.Pattern) -> Optional[str]:
    # Look for monetary first, else percentage
    for rx in (BRL_RE, PCT_RE):
        for m in rx.finditer(text):
            # scan around value for label presence
            start = max(0, m.start() - 80)
            end = min(len(text), m.end() + 80)
            window = text[start:end]
            if label_regex.search(window):
                return m.group(0)
    return None


def _parse_markdown_tables(md: str) -> Dict[str, Optional[str]]:
    """Scan Markdown for table blocks and try to extract VR/VA from rows."""
    vr_val: Optional[str] = None
    va_val: Optional[str] = None

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # naive detection of a markdown table: a header line containing '|' and the next line with dashes
        if ("|" in line) and (i + 1 < len(lines)) and re.search(r"\|\s*:?[-]{3,}", lines[i + 1]):
            # accumulate table until a blank line or a non-pipe line
            tbl = [line]
            j = i + 1
            while j < len(lines) and ("|" in lines[j]) and lines[j].strip():
                tbl.append(lines[j])
                j += 1
            # inspect rows (skip header and separator)
            for row in tbl[2:]:
                # split cells
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                row_text = " | ".join(cells)
                if vr_val is None and VR_LABELS.search(row_text):
                    # prefer BRL, else percentage
                    m = BRL_RE.search(row_text) or PCT_RE.search(row_text)
                    if m:
                        vr_val = m.group(0)
                if va_val is None and VA_LABELS.search(row_text):
                    m = BRL_RE.search(row_text) or PCT_RE.search(row_text)
                    if m:
                        va_val = m.group(0)
                if vr_val and va_val:
                    break
            i = j
        else:
            i += 1

    return {"vr": vr_val, "va": va_val}


def extrair_vr_va_docling(pdf_path: str | Path) -> Dict[str, Optional[str]]:
    """
    Extract VR/VA from a CCT PDF using Docling.
    Priority:
    1) Tables in the Docling output (preserved structure).
    2) Preserved text (Markdown) proximity search.

    Returns dict: { vr, va, vr_float, va_float, origem, periodicidade, condicao }
    origem in { 'docling_table', 'docling_text', 'docling_error:<msg>' }
    Values may be monetary (R$ ...) or percentage (12%). Floats only for monetary values.
    """
    pdf_path = str(pdf_path)

    if DocumentConverter is None:
        return {
            "vr": None,
            "va": None,
            "vr_float": None,
            "va_float": None,
            "origem": "docling_error:import",
        }

    try:
        conv = DocumentConverter()
        result = conv.convert(pdf_path)
        doc = result.document
        md = doc.export_to_markdown()
    except Exception as e:  # pragma: no cover
        msg = str(e)
        msg = (msg[:40] + "...") if len(msg) > 40 else msg
        return {
            "vr": None,
            "va": None,
            "vr_float": None,
            "va_float": None,
            "origem": f"docling_error:{msg}",
        }

    # 1) Tables
    table_hit = _parse_markdown_tables(md)
    vr, va = table_hit.get("vr"), table_hit.get("va")
    origem: str = ""
    if vr or va:
        origem = "docling_table"
    else:
        # 2) Text search with preserved order
        vr = _search_kv_nearby(md, VR_LABELS)
        va = _search_kv_nearby(md, VA_LABELS)
        if vr or va:
            origem = "docling_text"

    vr_float = _norm_brl_to_float(vr)
    va_float = _norm_brl_to_float(va)

    # Heuristics for periodicidade and condicao (global scan on md)
    periodicidade: Optional[str]
    if re.search(r"\b(por\s+dia|di[aá]rio|ao\s+dia)\b", md, flags=re.IGNORECASE):
        periodicidade = "diário"
    elif re.search(r"\b(mensal|por\s+m[eê]s|ao\s+m[eê]s)\b", md, flags=re.IGNORECASE):
        periodicidade = "mensal"
    else:
        periodicidade = None

    condicao: Optional[str]
    if re.search(r"comunicad[oa].{0,20}at[eé]\s+o\s+dia\s*15", md, flags=re.IGNORECASE):
        condicao = "comunicado <= 15"
    else:
        condicao = None

    return {
        "vr": vr,
        "va": va,
        "vr_float": vr_float,
        "va_float": va_float,
        "origem": origem or "docling_text",  # default to text if anything was found; else will be empty
        "periodicidade": periodicidade,
        "condicao": condicao,
    }
