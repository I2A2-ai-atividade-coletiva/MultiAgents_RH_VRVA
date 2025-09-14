from __future__ import annotations

from typing import Callable, Dict, Any, List, Optional, Tuple
import sqlite3
import json
from pathlib import Path

from ferramentas.persistencia_db import DB_PATH


def _peso_origem(origem: Optional[str]) -> float:
    if not origem:
        return 0.4
    o = str(origem).lower()
    if "docling_table" in o:
        return 1.0
    if "docling_text" in o:
        return 0.9
    if "text_fallback" in o:
        return 0.6
    if "sqlite::" in o:
        return 0.7
    if "ocr_index" in o:
        return 0.8
    if "llm_extract" in o:
        return 0.5
    return 0.5


def _parse_float_brl(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        s = str(v)
        # handle percentage strings -> ignore here
        if "%" in s:
            return None
        s = s.replace("R$", "").replace("$", "").replace(" ", "").replace(".", "").replace(",", ".")
        return float(s)
    except Exception:
        return None


def _resolve_valor_diario(valor_str: Optional[str], periodicidade: Optional[str], dias: Optional[int]) -> Optional[float]:
    v = _parse_float_brl(valor_str)
    if v is None:
        return None
    per = (periodicidade or "").strip().lower()
    if per.startswith("mes") or per == "mensal":
        if dias and int(dias) > 0:
            return round(v / int(dias), 2)
        return None
    # default daily
    return round(v, 2)


def _upsert_resolvidas(rows: List[Dict[str, Any]]) -> None:
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regras_cct_vrva_resolvidas (
                uf TEXT,
                sindicato TEXT,
                vr_valor TEXT,
                va_valor TEXT,
                periodicidade TEXT,
                dias INTEGER,
                condicao TEXT,
                origem TEXT,
                confidence REAL,
                PRIMARY KEY (uf, sindicato)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vrva_res_uf_sind ON regras_cct_vrva_resolvidas(uf, sindicato);")
        for r in rows:
            conn.execute(
                """
                INSERT INTO regras_cct_vrva_resolvidas
                (uf, sindicato, vr_valor, va_valor, periodicidade, dias, condicao, origem, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uf, sindicato) DO UPDATE SET
                    vr_valor=excluded.vr_valor,
                    va_valor=excluded.va_valor,
                    periodicidade=excluded.periodicidade,
                    dias=excluded.dias,
                    condicao=excluded.condicao,
                    origem=excluded.origem,
                    confidence=excluded.confidence
                """,
                (
                    r.get("uf"), r.get("sindicato"), r.get("vr_valor"), r.get("va_valor"),
                    r.get("periodicidade"), r.get("dias"), r.get("condicao"), r.get("origem"), r.get("confidence", 0.0)
                ),
            )


def criar_agente_vrva() -> Callable[[str], str]:
    """
    Agente que consolida seleção de VR/VA por (UF, Sindicato) com confiança e resolução de conflitos.
    Lê a tabela regras_cct (produzida pela ingestão Docling/fallback) e decide valores finais.
    Persistirá em regras_cct_vrva_resolvidas e retorna um array JSON dos registros resolvidos.
    """

    def executar(instrucoes: str) -> str:
        # Carregar todos os registros de regras_cct
        with sqlite3.connect(str(DB_PATH)) as conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT arquivo, uf, sindicato, vr, vr_float, va, va_float, origem, periodicidade, condicao FROM regras_cct")
                rows = cur.fetchall()
            except Exception as e:
                return json.dumps({"erro": f"Falha ao ler regras_cct: {e}"}, ensure_ascii=False)

        # Agrupar por (UF, Sindicato)
        grupos: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        cols = ["arquivo","uf","sindicato","vr","vr_float","va","va_float","origem","periodicidade","condicao"]
        for tup in rows:
            rec = {cols[i]: tup[i] for i in range(len(cols))}
            key = (str(rec.get("uf") or "").upper(), str(rec.get("sindicato") or "").strip())
            grupos.setdefault(key, []).append(rec)

        resolvidas: List[Dict[str, Any]] = []

        for (uf, sind), itens in grupos.items():
            # seleciona VR
            melhor_vr: Optional[str] = None
            melhor_va: Optional[str] = None
            melhor_per: Optional[str] = None
            melhor_dias: Optional[int] = None
            melhor_cond: Optional[str] = None
            best_score_vr = -1.0
            best_score_va = -1.0
            origem_vr = None
            origem_va = None

            # agregação: escolhe item de maior peso/origem; quando empatar, preferir com *_float não nulo
            for rec in itens:
                peso = _peso_origem(rec.get("origem"))
                per = rec.get("periodicidade")
                dias = rec.get("dias") if isinstance(rec.get("dias"), int) else None
                cond = rec.get("condicao")
                # VR
                vr_txt = rec.get("vr")
                vr_f = rec.get("vr_float")
                score_vr = peso + (0.05 if vr_f is not None else 0)
                if vr_txt and score_vr > best_score_vr:
                    melhor_vr = vr_txt
                    origem_vr = rec.get("origem")
                    best_score_vr = score_vr
                    melhor_per = per or melhor_per
                    melhor_dias = dias or melhor_dias
                    melhor_cond = cond or melhor_cond
                # VA
                va_txt = rec.get("va")
                va_f = rec.get("va_float")
                score_va = peso + (0.05 if va_f is not None else 0)
                if va_txt and score_va > best_score_va:
                    melhor_va = va_txt
                    origem_va = rec.get("origem")
                    best_score_va = score_va
                    melhor_per = per or melhor_per
                    melhor_dias = dias or melhor_dias
                    melhor_cond = cond or melhor_cond

            # montar saída
            # periodicidade: se conflitante, mantemos a mais "forte" (docling_*) que apareceu
            # já tratada na var melhor_per pelo processo acima
            out = {
                "uf": uf,
                "sindicato": sind,
                "vr_valor": melhor_vr,
                "va_valor": melhor_va,
                "periodicidade": melhor_per,
                "dias": melhor_dias,
                "condicao": melhor_cond,
                "origem": f"vr:{origem_vr or 'NA'};va:{origem_va or 'NA'}",
                "confidence": round(max(best_score_vr, best_score_va, 0.0), 3),
            }
            resolvidas.append(out)

        # Persistir na tabela resolvida
        try:
            _upsert_resolvidas(resolvidas)
        except Exception:
            pass

        return json.dumps(resolvidas, ensure_ascii=False)

    return executar
