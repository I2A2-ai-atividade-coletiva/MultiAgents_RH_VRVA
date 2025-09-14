from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict, Any
import json
import chromadb
from chromadb.config import Settings
import sqlite3
import pandas as pd
from ferramentas.persistencia_db import DB_PATH
from ferramentas.extracao_cct_llm import extrair_regras_da_cct

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"
RULES_INDEX = BASE_DIR / "base_conhecimento" / "rules_index.json"
RULES_OVERRIDES = CHROMA_DIR / "rules_overrides.json"


def _read_json(path: Path) -> Optional[object]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def resolve_cct_rules(uf: str, sindicato: str) -> Dict[str, Any]:
    """
    Resolve valores de VR/VA para uma combinação (UF, Sindicato).
    Prioridade: overrides -> rules_index (OCR) -> retrieval (Chroma, se houver metadados com valores).

    Retorna um dicionário possivelmente com chaves:
      - vr_valor, va_valor (string BRL, p.ex. "R$ 25,00")
      - dias (int), dias_tipo ("uteis")
      - periodicidade ("dia"|"mes")
      - origem: "override"|"ocr_index"|"retrieval"
    """
    uf_key = (uf or "").upper()
    sind_key = (sindicato or "").strip()

    # 0) Resultado consolidado pelo especialista (tabela resolvida)
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT vr_valor, va_valor, dias, periodicidade, condicao, origem, confidence
                FROM regras_cct_vrva_resolvidas
                WHERE uf = ? AND sindicato = ?
                """,
                (uf_key, sind_key),
            )
            row = cur.fetchone()
            if row:
                out = {}
                if row[0] is not None: out["vr_valor"] = row[0]
                if row[1] is not None: out["va_valor"] = row[1]
                if row[2] is not None:
                    try:
                        out["dias"] = int(row[2])
                    except Exception:
                        pass
                if row[3] is not None: out["periodicidade"] = row[3]
                if row[4] is not None: out["condicao"] = row[4]
                out["origem"] = (row[5] or "") + ";resolver"
                out["confidence"] = row[6]
                return out
    except Exception:
        pass

    # 1) Overrides
    overrides = _read_json(RULES_OVERRIDES) or {}
    k = f"{uf_key}::{sind_key}"
    if k in overrides:
        out = dict(overrides[k])
        out["origem"] = "override"
        return out

    # 2) OCR index
    idx = _read_json(RULES_INDEX) or []
    # Escolhe o primeiro matching por UF/Sindicato
    for item in idx:
        if (item.get("uf", "").upper() == uf_key) and (item.get("sindicato", "").strip() == sind_key):
            out = {
                key: item.get(key)
                for key in ("vr_valor", "va_valor", "dias", "dias_tipo", "periodicidade")
                if item.get(key) is not None
            }
            if out:
                out["origem"] = "ocr_index"
                return out

    # 2.5) LLM extraction a partir do texto das CCTs (Chroma) quando não há match direto no índice
    #      Junta os documentos do UF/Sindicato e extrai {valor_vr, valor_va, dias_uteis}
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR), settings=Settings(allow_reset=False))
        collection = client.get_or_create_collection("ccts")
        where = {"uf": uf_key, "sindicato": sind_key}
        res = collection.query(query_texts=["regras de VR VA dias"], n_results=6, where=where)
        docs = res.get("documents", [[]])[0]
        if docs:
            # limita tamanho para evitar prompt muito grande
            joined = "\n\n".join(docs)
            texto_cct = joined[:20000]
            try:
                payload = extrair_regras_da_cct(texto_cct)
                # ferramenta retorna JSON string
                data = json.loads(payload) if isinstance(payload, str) else payload
                out = {}
                if isinstance(data, dict):
                    vr = data.get("valor_vr")
                    va = data.get("valor_va")
                    dias = data.get("dias_uteis")
                    if vr is not None:
                        out["vr_valor"] = vr
                    if va is not None:
                        out["va_valor"] = va
                    if dias is not None:
                        try:
                            out["dias"] = int(dias)
                        except Exception:
                            pass
                if out:
                    out["origem"] = "llm_extract"
                    return out
            except Exception:
                pass
    except Exception:
        pass

    # 3) Lookup em SQLite (tabelas importadas via Streamlit)
    #    Procuramos tabelas com nomes que contenham 'sindicato' e 'valor' (ex.: base_sindicato_x_valor[_sheet])
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            # lista tabelas
            tbls = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", conn)
            candidatos = [t for t in tbls['name'].tolist() if 'sindicato' in t and 'valor' in t]
            for tname in candidatos:
                try:
                    # filtra por UF/sindicato (best-effort em nomes de colunas)
                    df = pd.read_sql_query(f"SELECT * FROM {tname}", conn)
                    cols_low = {c.lower(): c for c in df.columns}
                    # identificar colunas chaves
                    col_uf = cols_low.get('uf') or cols_low.get('estado')
                    col_sind = cols_low.get('sindicato') or cols_low.get('sindicato_do_colaborador') or cols_low.get('sindicato_colab')
                    if not (col_uf and col_sind):
                        continue
                    df['_uf_key'] = df[col_uf].astype(str).str.upper().str.strip()
                    df['_sind_key'] = df[col_sind].astype(str).str.strip()
                    hit = df[(df['_uf_key'] == uf_key) & (df['_sind_key'] == sind_key)]
                    if hit.empty:
                        continue
                    row = hit.iloc[0]
                    # mapear possíveis nomes de colunas de valores/dias/periodicidade
                    def pick(colnames: list[str]):
                        for name in colnames:
                            c = cols_low.get(name)
                            if c and pd.notna(row.get(c)):
                                return row.get(c)
                        return None
                    vr = pick(['vr_valor','vr','valor_vr','valor_vr_dia','vr_dia'])
                    va = pick(['va_valor','va','valor_va','valor_va_dia','va_dia'])
                    dias = pick(['dias','dias_vr','dias_va'])
                    per = pick(['periodicidade','periodicidade_vr','periodicidade_va'])
                    out = {}
                    if vr is not None: out['vr_valor'] = vr
                    if va is not None: out['va_valor'] = va
                    if dias is not None:
                        try:
                            out['dias'] = int(dias)
                        except Exception:
                            pass
                    if per is not None: out['periodicidade'] = per
                    if out:
                        out['origem'] = f"sqlite::{tname}"
                        return out
                except Exception:
                    continue
    except Exception:
        pass

    # 4) Retrieval no Chroma (busca documentos desse UF/sindicato e tenta ler metadados com valores)
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR), settings=Settings(allow_reset=False))
        collection = client.get_or_create_collection("ccts")
        where = {"uf": uf_key, "sindicato": sind_key}
        res = collection.query(query_texts=["valores VR VA"], n_results=5, where=where)
        metas = res.get("metadatas", [[]])[0]
        for md in metas:
            fields = {}
            for key in ("vr_valor", "va_valor", "dias", "dias_tipo", "periodicidade"):
                if key in md and md[key] is not None:
                    fields[key] = md[key]
            if fields:
                fields["origem"] = "retrieval"
                return fields
    except Exception:
        pass

    # Sem dados
    return {"origem": "nao_encontrado"}
