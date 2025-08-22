from typing import Callable, List

from langchain.tools import BaseTool
from utils.prompt_loader import carregar_prompt
from utils.config import get_llm
import json
from ferramentas.persistencia_db import salvar_dataframe_db
from ferramentas.leitor_arquivos import normalizar_nomes_sindicatos


def criar_agente_dados(ferramentas: List[BaseTool] | None = None) -> Callable[[str], str]:
    """
    Agente especialista em dados: lê arquivos, saneia e unifica. Usa ferramentas de leitura.
    """
    prompt = carregar_prompt("especialista_dados")
    ferramentas = ferramentas or []
    llm = get_llm()

    def executar(instrucoes: str) -> str:
        # Contextualiza a presença de ferramentas/DB para o LLM
        mensagem = (
            f"{prompt}\n\nINSTRUÇÕES:\n{instrucoes}\n\n"
            "Diretriz: Retorne um array JSON de registros tabulares ao final."
        )
        resp = llm.invoke(mensagem)
        conteudo = getattr(resp, "content", str(resp))

        # Tenta extrair primeiro array JSON e persistir
        try:
            s = str(conteudo)
            i = s.find('[')
            j = s.rfind(']')
            if i != -1 and j != -1 and j > i:
                arr = json.loads(s[i:j+1])
                if isinstance(arr, list):
                    dfjson = json.dumps(arr, ensure_ascii=False)
                    salvar_dataframe_db(dfjson, "dados_consolidados")
                    try:
                        dfjson_norm = normalizar_nomes_sindicatos(dfjson)
                        salvar_dataframe_db(dfjson_norm, "dados_consolidados_norm")
                    except Exception:
                        pass
        except Exception:
            pass

        return conteudo

    return executar
