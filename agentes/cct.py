from typing import Callable, List, Dict, Any, Optional
from pathlib import Path
import json

import chromadb
from chromadb.config import Settings

from utils.prompt_loader import carregar_prompt
from utils.config import get_llm

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"
RULES_INDEX_PATH = BASE_DIR / "base_conhecimento" / "rules_index.json"


def criar_agente_cct(ufs: Optional[List[str]] = None, sindicatos: Optional[List[str]] = None) -> Callable[[str], str]:
    """
    Agente Analista de CCT: realiza retrieval na base ChromaDB populada por ingest_ccts.py
    e responde via LLM usando o prompt unificado `prompts/analista_cct.md`.
    """
    prompt = carregar_prompt("analista_cct")
    llm = get_llm()

    client = chromadb.PersistentClient(path=str(CHROMA_DIR), settings=Settings(allow_reset=False))
    collection = client.get_or_create_collection("ccts")

    def executar(pergunta: str) -> str:
        where = None
        if ufs or sindicatos:
            where = {}
            if ufs:
                where["uf"] = {"$in": [u.upper() for u in ufs]}
            if sindicatos:
                where["sindicato"] = {"$in": [s.upper() for s in sindicatos]}
        res = collection.query(query_texts=[pergunta], n_results=4, where=where)
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        contexto = []
        for i, d in enumerate(docs):
            origem = metas[i].get("arquivo") if i < len(metas) else None
            uf = metas[i].get("uf") if i < len(metas) else None
            sindicato = metas[i].get("sindicato") if i < len(metas) else None
            contexto.append(f"Fonte: {origem} | UF: {uf} | Sindicato: {sindicato}\nTrecho:\n{d}")
        contexto_txt = "\n\n".join(contexto)

        mensagem = (
            f"{prompt}\n\nPERGUNTA:\n{pergunta}\n\nTRECHOS RECUPERADOS:\n{contexto_txt}\n\n"
            "Instrução: Responda somente com base nos trechos. Se faltar informação, diga que não foi encontrado."
        )
        resp = llm.invoke(mensagem)
        conteudo = getattr(resp, "content", str(resp))
        return conteudo

    return executar


def _normalize_key(s: str | None) -> str:
    return (s or "DESCONHECIDO").strip().upper()


def _consolidate_rules(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Consolida por (UF, Sindicato) escolhendo melhor fonte disponível
    # Critério atualizado: valores > cláusulas > dias > preferência por 'diário'
    agrup: Dict[tuple, Dict[str, Any]] = {}
    for it in items:
        uf = _normalize_key(it.get("uf"))
        sind = _normalize_key(it.get("sindicato"))
        key = (uf, sind)
        atual = agrup.get(key)

        # Extrai campos padronizados
        vr = it.get("vr_valor") or it.get("vr")
        va = it.get("va_valor") or it.get("va")
        dias = it.get("dias")
        periodicidade = it.get("periodicidade")
        origem = it.get("origem") or it.get("arquivo")
        tem_vr = bool(it.get("tem_clausula_vr"))
        tem_va = bool(it.get("tem_clausula_va"))

        candidato = {
            "uf": uf,
            "sindicato": sind,
            "vr_valor": vr,
            "va_valor": va,
            "dias": dias,
            "periodicidade": periodicidade,
            "tem_clausula_vr": tem_vr,
            "tem_clausula_va": tem_va,
            "origem": origem,
        }

        def _score(rec: Dict[str, Any]) -> tuple:
            # Mais alto é melhor: valores presentes > cláusulas presentes > dias presentes > preferir 'diário'
            periodicidade = str(rec.get("periodicidade") or "").lower()
            pref_diario = 1 if "diar" in periodicidade else 0
            return (
                int(bool(rec.get("vr_valor"))) + int(bool(rec.get("va_valor"))),
                int(bool(rec.get("tem_clausula_vr"))) + int(bool(rec.get("tem_clausula_va"))),
                int(rec.get("dias") is not None),
                pref_diario,
            )

        if atual is None or _score(candidato) > _score(atual):
            agrup[key] = candidato

    # Marcar pendências
    saida: List[Dict[str, Any]] = []
    for (uf, sind), rec in sorted(agrup.items(), key=lambda x: (x[0][0], x[0][1])):
        rec = dict(rec)
        rec["pendencia_valores"] = not (bool(rec.get("vr_valor")) and bool(rec.get("va_valor")))
        rec["pendencia_dias"] = rec.get("dias") is None
        rec["pendencia_clausulas"] = not (bool(rec.get("tem_clausula_vr")) and bool(rec.get("tem_clausula_va")))
        saida.append(rec)
    return saida


def criar_agente_coletor_cct() -> Callable[[str], str]:
    """
    Agente Coletor CCT: lê rules_index.json e consolida informações por UF/Sindicato,
    marcando pendências (valores, dias, cláusulas). Determinístico (não invoca LLM).

    Retorna SEMPRE um array JSON (orient=records) com os campos:
    uf, sindicato, vr_valor, va_valor, dias, periodicidade, tem_clausula_vr, tem_clausula_va,
    origem, pendencia_valores, pendencia_dias, pendencia_clausulas
    """
    # Carrega a persona unificada (sem uso de LLM aqui)
    _ = carregar_prompt("analista_cct")

    def executar(_: str) -> str:
        try:
            if RULES_INDEX_PATH.exists():
                items = json.loads(RULES_INDEX_PATH.read_text(encoding="utf-8"))
            else:
                items = []
        except Exception:
            items = []
        consol = _consolidate_rules(items)
        return json.dumps(consol, ensure_ascii=False)

    return executar
