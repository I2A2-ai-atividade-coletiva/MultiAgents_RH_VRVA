from typing import Callable, List, Optional
from pathlib import Path

import chromadb
from chromadb.config import Settings

from utils.prompt_loader import carregar_prompt
from utils.config import get_llm

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"


def criar_agente_cct(ufs: Optional[List[str]] = None, sindicatos: Optional[List[str]] = None) -> Callable[[str], str]:
    """
    Agente de conhecimento: realiza retrieval na base ChromaDB populada pelo ingest_ccts.py.
    """
    prompt = carregar_prompt("analista_cct")
    llm = get_llm()

    client = chromadb.PersistentClient(path=str(CHROMA_DIR), settings=Settings(allow_reset=False))
    collection = client.get_or_create_collection("ccts")

    def executar(pergunta: str) -> str:
        # Retrieval simples
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
