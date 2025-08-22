from pathlib import Path
from typing import List, Optional
import io
import re

import fitz  # PyMuPDF
import chromadb
from chromadb.config import Settings
from PIL import Image
import pytesseract
import pdfplumber
import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR / "base_conhecimento" / "ccts_pdfs"
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"


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
    # Indícios de periodicidade perto do valor: por dia/diário vs mensal
    # Vamos capturar contexto de até 80 chars ao redor do match, mas de forma simples procure palavras-chave no texto completo
    if re.search(r"\bpor\s+dia\b|di[aá]rio", s, flags=re.IGNORECASE):
        out.setdefault("periodicidade", "dia")
    if re.search(r"\bmensal\b|por\s+m[eê]s", s, flags=re.IGNORECASE):
        out.setdefault("periodicidade", "mes")
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

    def infer_uf_from_filename(name: str) -> Optional[str]:
        parts = re.split(r"\W+", name.upper())
        for p in parts:
            if p in ufs:
                return p
        return None

    def infer_uf_from_text(text: str) -> Optional[str]:
        # procura por "Estado de SP", "São Paulo - SP", etc.
        m = re.search(r"\b([A-Z]{2})\b", text)
        if m and m.group(1) in ufs:
            return m.group(1)
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
    for pdf in pdfs:
        texto, ocr_pages, total_pages = extract_text_from_pdf(pdf)
        if ocr_pages:
            ocr_summary.append((pdf.name, ocr_pages, total_pages))
        parts = chunk_text(texto)
        uf = infer_uf_from_filename(pdf.stem) or infer_uf_from_text(texto) or "DESCONHECIDO"
        sindicato = (
            infer_sindicato_from_text(texto)
            or infer_sindicato_from_filename(pdf.stem)
            or "DESCONHECIDO"
        )
        # Parse simples de regras/valores
        regras = extract_rules_from_text(texto)
        if regras:
            rules_index.append({
                "arquivo": pdf.name,
                "uf": uf,
                "sindicato": sindicato,
                **regras,
            })
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
            rules_path = CHROMA_DIR / "rules_index.json"
            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            import json as _json
            rules_path.write_text(_json.dumps(rules_index, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Regras extraídas salvas em: {rules_path}")
        except Exception as e:
            print("Falha ao salvar rules_index.json:", e)
    else:
        print("Nenhum conteúdo foi gerado a partir dos PDFs.")


if __name__ == "__main__":
    main()
