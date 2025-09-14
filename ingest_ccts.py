from pathlib import Path
from typing import List, Optional, Tuple, Dict
import io
import re
import hashlib

import fitz  # PyMuPDF
import chromadb
from chromadb.config import Settings
from PIL import Image
import pytesseract
import pdfplumber
import cv2
import numpy as np
import sqlite3

from ferramentas.persistencia_db import DB_PATH

# Docling extractor (new)
try:
    from ferramentas.extracao_cct_docling import extrair_vr_va_docling
except Exception:
    extrair_vr_va_docling = None  # type: ignore

BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR / "base_conhecimento" / "ccts_pdfs"
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"
RULES_INDEX_ROOT = BASE_DIR / "base_conhecimento" / "rules_index.json"

# ... (rest of the code remains the same)

def preprocess_for_ocr(pil_img: Image.Image) -> Image.Image:
    """OpenCV pipeline: grayscale, denoise, threshold (Otsu), deskew, slight upscale."""
    img = np.array(pil_img)
    if img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    # CLAHE for contrast
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    # Threshold
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Denoise light
    th = cv2.medianBlur(th, 3)
    # Deskew (estimate angle)
    coords = np.column_stack(np.where(th < 255))
    angle = 0.0
    if coords.size > 0:
        rect = cv2.minAreaRect(coords)
        angle = rect[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
    (h, w) = th.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(th, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    # Upscale
    up = cv2.resize(rotated, None, fx=1.3, fy=1.3, interpolation=cv2.INTER_CUBIC)
    return Image.fromarray(up)

def ocr_with_tesseract(pil_img: Image.Image) -> str:
    # Try a few PSM modes
    for psm in (6, 4, 11):
        cfg = f"--oem 1 --psm {psm}"
        try:
            txt = pytesseract.image_to_string(pil_img, lang="por+eng", config=cfg)
            if txt and len(txt.strip()) >= 10:
                return txt
        except Exception:
            continue
    return ""

def extract_text_from_pdf(pdf_path: Path) -> tuple[str, int, int]:
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    ocr_pages = 0
    texts: List[str] = []
    # Try pdfplumber alongside PyMuPDF for better layout/tables
    try:
        plumber = pdfplumber.open(str(pdf_path))
    except Exception:
        plumber = None
    for i, page in enumerate(doc):
        # 1) PyMuPDF plain text
        txt = page.get_text("text")
        # 2) If weak, try pdfplumber text (and append tables text)
        if (not txt) or (len(txt.strip()) < 30):
            try:
                if plumber:
                    p2 = plumber.pages[i]
                    t2 = p2.extract_text() or ""
                    tbl_texts = []
                    try:
                        tables = p2.extract_tables() or []
                        for tbl in tables:
                            # join rows with tabs
                            for row in tbl:
                                tbl_texts.append("\t".join([c or "" for c in row]))
                    except Exception:
                        pass
                    combo = (t2 + "\n" + "\n".join(tbl_texts)).strip()
                    if len(combo) > len(txt or ""):
                        txt = combo
            except Exception:
                pass
        # 3) If still weak, OCR fallback with preprocessing
        if (not txt) or (len(txt.strip()) < 30):
            try:
                pix = page.get_pixmap(dpi=300)
                img_bytes = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_bytes))
                img_prep = preprocess_for_ocr(img)
                txt = ocr_with_tesseract(img_prep)
                if txt:
                    ocr_pages += 1
                else:
                    txt = ""
            except Exception:
                txt = ""
        texts.append(txt)
    doc.close()
    try:
        if plumber:
            plumber.close()
    except Exception:
        pass
    return "\n".join(texts), ocr_pages, total_pages

def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 150) -> List[str]:
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        chunks.append(chunk)
        start = end - overlap
        if start < 0:
            start = 0
        if end == len(text):
            break
    return chunks

def extract_rules_from_text(text: str) -> dict:
    """Heurística simples para VR/VA por dia/mês e dias. Retorna dicionário com campos quando encontrados."""
    out: dict = {}
    s = " ".join(text.split())  # normalize spaces
    # moeda BR
    money = r"R\$\s?\d{1,3}(?:\.\d{3})*(,\d{2})"
    # Sinonímias e variações: VR/refeição/vale refeição/auxílio refeição
    kw_ref = r"(?:\bVR\b|refei[cç][aã]o|vale[\s\-_]*refei[cç][aã]o|aux[ií]lio[\s\-_]*refei[cç][aã]o)"
    # VA/alimentação/vale alimentação/auxílio alimentação
    kw_ali = r"(?:\bVA\b|alimenta[cç][aã]o|vale[\s\-_]*alimenta[cç][aã]o|aux[ií]lio[\s\-_]*alimenta[cç][aã]o)"
    # Proximidade em qualquer ordem (keyword perto de valor)
    prox = 80
    pat_vr = rf"(?:{kw_ref}[^\n\r]{{0,{prox}}}({money})|({money})[^\n\r]{{0,{prox}}}{kw_ref})"
    pat_va = rf"(?:{kw_ali}[^\n\r]{{0,{prox}}}({money})|({money})[^\n\r]{{0,{prox}}}{kw_ali})"
    m_vr = re.search(pat_vr, s, flags=re.IGNORECASE)
    m_va = re.search(pat_va, s, flags=re.IGNORECASE)
    if m_vr:
        out["vr_valor"] = next(g for g in m_vr.groups() if g and g.startswith("R$"))
    if m_va:
        out["va_valor"] = next(g for g in m_va.groups() if g and g.startswith("R$"))
    # Dias (ex.: 22 dias, 22 dias úteis)
    dias = re.search(r"(\b\d{1,2}\b)\s+dias(\s+\b[uú]teis\b)?", s, flags=re.IGNORECASE)
    if dias:
        out["dias"] = int(dias.group(1))
        if dias.group(2):
            out["dias_tipo"] = "uteis"
    # Periodicidade: por dia, diário, ao dia -> 'diário'; por mês, mensal, ao mês -> 'mensal'
    if re.search(r"\b(por\s+dia|di[aá]rio|ao\s+dia)\b", s, flags=re.IGNORECASE):
        out["periodicidade"] = "diário"
    elif re.search(r"\b(mensal|por\s+m[eê]s|ao\s+m[eê]s)\b", s, flags=re.IGNORECASE):
        out["periodicidade"] = "mensal"
    else:
        out["periodicidade"] = None
    # Condição: comunicado até o dia 15
    if re.search(r"comunicad[oa].{0,20}at[eé]\s+o\s+dia\s*15", s, flags=re.IGNORECASE):
        out["condicao"] = "comunicado <= 15"
    else:
        out["condicao"] = None
    # Normalização simples: se valor diário e dias presentes, estima mensal
    def parse_brl(v: str) -> Optional[float]:
        try:
            return float(v.replace("R$", "").replace(" ", "").replace(".", "").replace(",", "."))
        except Exception:
            return None
    if "vr_valor" in out and "dias" in out:
        v = parse_brl(out["vr_valor"]) or 0.0
        out["vr_estimado_mes"] = round(v * out["dias"], 2)
    if "va_valor" in out and "dias" in out:
        v = parse_brl(out["va_valor"]) or 0.0
        out["va_estimado_mes"] = round(v * out["dias"], 2)
    return out

# -- Robust fallback regex for VR/VA (currency and percentage) --
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
BRL_VALUE = r"R?\$?\s*\d{1,3}(?:\.\d{3})*,\d{2}|\d+(?:,\d{2})?"
PCT_VALUE = r"\d{1,3}(?:,\d+)?\s*%"
BRL_RE = re.compile(BRL_VALUE)
PCT_RE = re.compile(PCT_VALUE)

def _norm_brl_to_float(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    if PCT_RE.fullmatch(v.strip()):
        return None
    m = BRL_RE.search(v)
    if not m:
        return None
    s = m.group(0)
    s = s.replace("R$", "").replace("$", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def _fallback_vr_va_from_text(text: str) -> Tuple[Optional[str], Optional[str], str]:
    s = " ".join(text.split())
    origem = ""
    vr = None
    va = None
    # Search values, prefer currency, then percentage; ensure labels close by
    for rx in (BRL_RE, PCT_RE):
        for m in rx.finditer(s):
            start = max(0, m.start() - 80)
            end = min(len(s), m.end() + 80)
            win = s[start:end]
            if vr is None and VR_LABELS.search(win):
                vr = m.group(0)
            if va is None and VA_LABELS.search(win):
                va = m.group(0)
            if vr and va:
                break
        if vr and va:
            break
    if vr or va:
        origem = "text_fallback"
    return vr, va, origem

def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(CHROMA_DIR), settings=Settings(allow_reset=False))
    collection = client.get_or_create_collection("ccts")

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"Nenhum PDF encontrado em {PDF_DIR}. Coloque as CCTs aqui e rode novamente.")
        return

    docs = []
    ids = []
    metadatas = []
    rules_index: List[dict] = []

    # Padrão de UF (siglas BR)
    ufs = {"AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO"}

    # Mapa de nomes de estados (PT-BR, sem acento) -> UF
    STATE_NAME_TO_UF = {
        "acre": "AC",
        "alagoas": "AL",
        "amapa": "AP",
        "amazonas": "AM",
        "bahia": "BA",
        "ceara": "CE",
        "distrito federal": "DF",
        "espirito santo": "ES",
        "goias": "GO",
        "maranhao": "MA",
        "mato grosso": "MT",
        "mato grosso do sul": "MS",
        "minas gerais": "MG",
        "para": "PA",
        "paraiba": "PB",
        "parana": "PR",
        "pernambuco": "PE",
        "piaui": "PI",
        "rio de janeiro": "RJ",
        "rio grande do norte": "RN",
        "rio grande do sul": "RS",
        "rondonia": "RO",
        "roraima": "RR",
        "santa catarina": "SC",
        "sao paulo": "SP",
        "sergipe": "SE",
        "tocantins": "TO",
    }

    def infer_uf_from_filename(name: str) -> Optional[str]:
        parts = re.split(r"\W+", name.upper())
        for p in parts:
            if p in ufs:
                return p
        # tentar por nome de estado no próprio nome do arquivo
        try:
            import unicodedata as _ud
            tnorm = _ud.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii").lower()
        except Exception:
            tnorm = str(name).lower()
        for nome, sigla in STATE_NAME_TO_UF.items():
            # match por palavra inteira
            if re.search(rf"\b{re.escape(nome)}\b", tnorm):
                return sigla
        return None

    def _detect_ufs_in_text(text: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        try:
            import unicodedata as _ud
            tnorm = _ud.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower()
        except Exception:
            tnorm = str(text).lower()
        # Contar ocorrências por nome do estado (palavra inteira)
        for nome, sigla in STATE_NAME_TO_UF.items():
            try:
                rx = re.compile(rf"\b{re.escape(nome)}\b")
                c = len(rx.findall(tnorm))
            except Exception:
                c = 0
            if c:
                counts[sigla] = counts.get(sigla, 0) + c
        # Contar ocorrências por siglas isoladas
        for m in re.finditer(r"\b([A-Z]{2})\b", str(text)):
            sg = m.group(1)
            if sg in ufs:
                counts[sg] = counts.get(sg, 0) + 1
        return counts

    def infer_uf_from_text(text: str) -> Optional[str]:
        counts = _detect_ufs_in_text(text)
        if not counts:
            return None
        # retorna a UF com maior contagem
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    def infer_uf_from_sindicato_name(s: Optional[str]) -> Optional[str]:
        if not s:
            return None
        s_up = str(s).upper()
        # Tenta siglas primeiro em sindicato
        m = re.search(r"\b([A-Z]{2})\b", s_up)
        if m and m.group(1) in ufs:
            return m.group(1)
        # Depois tenta por nome do estado (tolerante a acentos)
        try:
            import unicodedata as _ud
            tnorm = _ud.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii").lower()
            for nome, sigla in STATE_NAME_TO_UF.items():
                if nome in tnorm:
                    return sigla
        except Exception:
            pass
        return None

    def infer_sindicato_from_text(text: str) -> Optional[str]:
        # Heurística simples: captura linha que começa ou contém "SINDICATO ..."
        # Ex.: "SINDICATO DOS EMPREGADOS NO COMÉRCIO DE SÃO PAULO"
        m = re.search(r"(SINDICATO[^\n]{10,150})", text.upper())
        if m:
            return m.group(1).strip()
        # Alternativa: "FEDERAÇÃO" pode indicar entidade superior; mantemos só se não achar sindicato
        m2 = re.search(r"(FEDERAÇÃO[^\n]{10,150})", text.upper())
        if m2:
            return m2.group(1).strip()
        return None

    def infer_sindicato_from_filename(name: str) -> Optional[str]:
        # tenta extrair algo como "SINDICATO_..." no nome do arquivo
        m = re.search(r"(SINDICATO[^_\-]{3,})", name.upper())
        if m:
            return m.group(1).replace("_", " ").strip()
        return None

    ocr_summary = []
    sindicatos_set = set()
    ufs_set = set()
    # Prepare DB table for extracted rules
    try:
        create_sql = (
            """
            CREATE TABLE IF NOT EXISTS regras_cct (
                arquivo TEXT PRIMARY KEY,
                doc_sha1 TEXT,
                uf TEXT,
                sindicato TEXT,
                vr TEXT,
                vr_float REAL,
                va TEXT,
                va_float REAL,
                origem TEXT,
                periodicidade TEXT,
                condicao TEXT
            );
            """
        )
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(create_sql)
            # Indexes (idempotent)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regras_cct_arquivo ON regras_cct(arquivo);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regras_cct_uf ON regras_cct(uf);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regras_cct_sindicato ON regras_cct(sindicato);")
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_regras_cct_uf_sind ON regras_cct(uf, sindicato);")
            except Exception:
                pass
            # Migration: add column doc_sha1 if table existed without it
            try:
                cur = conn.execute("PRAGMA table_info(regras_cct)")
                cols = [r[1] for r in cur.fetchall()]
                if 'doc_sha1' not in cols:
                    conn.execute("ALTER TABLE regras_cct ADD COLUMN doc_sha1 TEXT")
            except Exception:
                pass
    except Exception as e:
        print(f"Aviso: falha ao criar/indixar regras_cct: {e}. SQL=\n{create_sql}")

    for pdf in pdfs:
        # Compute SHA1 of the PDF for deduplication
        try:
            data = pdf.read_bytes()
            doc_sha1 = hashlib.sha1(data).hexdigest()
        except Exception:
            doc_sha1 = None
        # 0) Try Docling first for VR/VA (tables/text)
        docling_res: Dict[str, Optional[str]] = {
            "vr": None, "va": None, "vr_float": None, "va_float": None, "origem": None
        }
        if extrair_vr_va_docling is not None:
            try:
                docling_res = extrair_vr_va_docling(str(pdf))  # type: ignore
            except Exception as e:
                msg = str(e)
                docling_res = {
                    "vr": None, "va": None, "vr_float": None, "va_float": None, "origem": f"docling_error:{msg[:40]}"
                }

        # 1) Extract text for indexing and possible fallback
        texto, ocr_pages, total_pages = extract_text_from_pdf(pdf)
        if ocr_pages:
            ocr_summary.append((pdf.name, ocr_pages, total_pages))
        parts = chunk_text(texto)
        # Primeiro extraímos o sindicato, pois ele pode conter o estado correto (ex.: '... EST PARANA')
        sindicato = (
            infer_sindicato_from_text(texto)
            or infer_sindicato_from_filename(pdf.stem)
            or "DESCONHECIDO"
        )
        # Em seguida inferimos a UF priorizando: nome do arquivo (sigla) -> sindicato -> texto
        uf = (
            infer_uf_from_filename(pdf.stem)
            or infer_uf_from_sindicato_name(sindicato)
            or infer_uf_from_text(texto)
            or "DESCONHECIDO"
        )
        # Resolve VR/VA: prefer Docling results; if empty, fallback to robust regex over extracted text
        vr = docling_res.get("vr")
        va = docling_res.get("va")
        origem = (docling_res.get("origem") or "").strip()
        vr_f = docling_res.get("vr_float")  # type: ignore
        va_f = docling_res.get("va_float")  # type: ignore

        if not (vr or va):
            fb_vr, fb_va, fb_origin = _fallback_vr_va_from_text(texto)
            vr = vr or fb_vr
            va = va or fb_va
            origem = origem or fb_origin
            vr_f = _norm_brl_to_float(vr) if vr_f is None else vr_f
            va_f = _norm_brl_to_float(va) if va_f is None else va_f

        # Parse other simple rules from text (dias, periodicidade, estimativas) e flags de cláusula
        regras = extract_rules_from_text(texto)
        try:
            txt_up = texto.upper()
            tem_clausula_vr = bool(re.search(r"CL[AÁ]USULA[\s\S]{0,120}" + VR_LABELS.pattern, txt_up, flags=re.IGNORECASE))
            tem_clausula_va = bool(re.search(r"CL[AÁ]USULA[\s\S]{0,120}" + VA_LABELS.pattern, txt_up, flags=re.IGNORECASE))
            regras["tem_clausula_vr"] = tem_clausula_vr
            regras["tem_clausula_va"] = tem_clausula_va
        except Exception:
            pass
        # Prefer Docling's periodicidade/condicao when available
        try:
            if docling_res.get("periodicidade") is not None:
                regras["periodicidade"] = docling_res.get("periodicidade")
            if docling_res.get("condicao") is not None:
                regras["condicao"] = docling_res.get("condicao")
        except Exception:
            pass
        if vr:
            regras["vr_valor"] = vr
        if va:
            regras["va_valor"] = va
        # Add normalized floats and origin metadata
        if vr_f is None:
            vr_f = _norm_brl_to_float(regras.get("vr_valor"))
        if va_f is None:
            va_f = _norm_brl_to_float(regras.get("va_valor"))
        if origem:
            regras["origem"] = origem
        if vr_f is not None:
            regras["vr_float"] = vr_f
        if va_f is not None:
            regras["va_float"] = va_f
        if regras:
            rules_index.append({
                "arquivo": pdf.name,
                "uf": uf,
                "sindicato": sindicato,
                **regras,
            })
            # Log concise source origin
            try:
                src = regras.get("origem") or ""
                if src:
                    print(f"[origem] {pdf.name}: {src} | VR={regras.get('vr_valor')} | VA={regras.get('va_valor')}")
            except Exception:
                pass
        if uf:
            ufs_set.add(uf)
        if sindicato:
            sindicatos_set.add(sindicato)
        for i, p in enumerate(parts):
            docs.append(p)
            ids.append(f"{pdf.stem}-{i}")
            md = {"arquivo": pdf.name, "parte": i, "uf": uf, "sindicato": sindicato}
            if regras:
                # Inclui algumas chaves úteis, sem inflar muito o metadata
                if "vr_valor" in regras:
                    md["vr_valor"] = regras["vr_valor"]
                if "va_valor" in regras:
                    md["va_valor"] = regras["va_valor"]
                if "dias" in regras:
                    md["dias"] = regras["dias"]
            metadatas.append(md)

        # Persist row into regras_cct (upsert by arquivo)
        try:
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("DELETE FROM regras_cct WHERE arquivo = ?", (pdf.name,))
                conn.execute(
                    """
                    INSERT INTO regras_cct (
                        arquivo, doc_sha1, uf, sindicato, vr, vr_float, va, va_float, origem, periodicidade, condicao
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pdf.name,
                        doc_sha1,
                        uf,
                        sindicato,
                        vr,
                        vr_f,
                        va,
                        va_f,
                        origem or None,
                        regras.get("periodicidade"),
                        regras.get("condicao"),
                    ),
                )
        except Exception as e:
            print(f"Aviso: falha ao salvar regras_cct para {pdf.name}: {e}")

    if docs:
        # Para simplicidade, fazemos reset de IDs existentes se houver conflito
        # (Chroma pode lançar erro de ID duplicado). Aqui usamos add, que atualizará/ignora conforme backend.
        collection.add(documents=docs, ids=ids, metadatas=metadatas)
        print(f"Ingestão concluída. Documentos adicionados: {len(docs)}")
        if ocr_summary:
            print("Resumo OCR (arquivo: páginas_OCR/total):")
            for name, ocr_p, tot in ocr_summary:
                print(f" - {name}: {ocr_p}/{tot}")
        if ufs_set:
            print("UFs detectadas nas CCTs:", ", ".join(sorted(ufs_set)))
        if sindicatos_set:
            print("Sindicatos detectados nas CCTs (amostra):")
            for s in list(sorted(sindicatos_set))[:15]:
                print(" -", s)
        # Persiste índice simples de regras extraídas
        try:
            # Unified target
            RULES_INDEX_ROOT.parent.mkdir(parents=True, exist_ok=True)
            import json as _json
            RULES_INDEX_ROOT.write_text(_json.dumps(rules_index, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Regras extraídas salvas em: {RULES_INDEX_ROOT}")
            # Legacy copy for backward compatibility (optional)
            try:
                CHROMA_DIR.mkdir(parents=True, exist_ok=True)
                legacy_path = CHROMA_DIR / "rules_index.json"
                legacy_path.write_text(_json.dumps(rules_index, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"Cópia legada salva em: {legacy_path}")
            except Exception as le:
                print(f"Aviso: falha ao salvar cópia legada em chromadb: {le}")
        except Exception as e:
            print("Falha ao salvar rules_index.json:", e)
    else:
        print("Nenhum conteúdo foi gerado a partir dos PDFs.")

if __name__ == "__main__":
    main()
