from typing import Callable

from utils.prompt_loader import carregar_prompt
from utils.config import get_llm
import json
from ferramentas.persistencia_db import carregar_dataframe_db, salvar_dataframe_db


def criar_agente_calculo() -> Callable[[str], str]:
    """
    Agente de cálculo: aplica regras de VR/VA e gera valores finais.
    """
    prompt = carregar_prompt("especialista_calculo")
    llm = get_llm()

    def executar(instrucoes: str) -> str:
        # Carrega base de compliance aprovada
        df_json_ok = carregar_dataframe_db("dados_compliance_ok")
        try:
            parsed_ok = json.loads(df_json_ok)
        except Exception:
            parsed_ok = []

        mensagem = (
            f"{prompt}\n\nINSTRUÇÕES:\n{instrucoes}\n\n"
            "Contexto (dados aprovados em compliance - JSON orient=records):\n"
            f"{df_json_ok}\n\n"
            "Diretriz: Retorne ao final apenas um array JSON com os registros e colunas finais calculadas."
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
                    salvar_dataframe_db(dfjson, "dados_calculo_final")
        except Exception:
            pass

        return conteudo

    return executar
