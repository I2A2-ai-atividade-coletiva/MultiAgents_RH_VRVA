from typing import Callable

from utils.prompt_loader import carregar_prompt
from utils.config import get_llm
import json
from ferramentas.persistencia_db import carregar_dataframe_db, salvar_dataframe_db


def criar_agente_compliance() -> Callable[[str], str]:
    """
    Agente que verifica regras internas e conformidade.
    """
    prompt = carregar_prompt("especialista_compliance")
    llm = get_llm()

    def executar(instrucoes: str) -> str:
        # Carrega base consolidada do DB para o contexto
        df_json_norm = carregar_dataframe_db("dados_consolidados_norm")
        try:
            parsed_norm = json.loads(df_json_norm)
        except Exception:
            parsed_norm = []
        if not parsed_norm:
            df_json_norm = carregar_dataframe_db("dados_consolidados")

        mensagem = (
            f"{prompt}\n\nINSTRUÇÕES:\n{instrucoes}\n\n"
            "Contexto (dados consolidados - JSON orient=records):\n"
            f"{df_json_norm}\n\n"
            "Diretriz: Retorne ao final apenas um array JSON com os registros elegíveis após compliance."
        )
        resp = llm.invoke(mensagem)
        conteudo = getattr(resp, "content", str(resp))

        # Persiste primeiro array JSON identificado
        try:
            s = str(conteudo)
            i = s.find('[')
            j = s.rfind(']')
            if i != -1 and j != -1 and j > i:
                arr = json.loads(s[i:j+1])
                if isinstance(arr, list):
                    dfjson = json.dumps(arr, ensure_ascii=False)
                    salvar_dataframe_db(dfjson, "dados_compliance_ok")
        except Exception:
            pass

        return conteudo

    return executar
