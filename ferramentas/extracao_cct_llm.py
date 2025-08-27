from langchain.tools import tool
import json
import re
from utils.config import get_llm


@tool("extrair_regras_da_cct")
def extrair_regras_da_cct(texto_cct: str) -> str:
    """
    Analisa o texto completo de uma CCT e extrai os valores de VR, VA e a
    quantidade de dias úteis. Retorna um JSON com as chaves:
      - valor_vr: float | null
      - valor_va: float | null
      - dias_uteis: int | null

    Usa LLM via get_llm() para uma extração direcionada mais precisa.
    """
    llm = get_llm()

    prompt = f"""
    Você é um extrator preciso de informações em convenções coletivas (CCT). Extraia:
    1) Valor numérico diário do Vale Refeição (VR) ou Auxílio Refeição.
    2) Valor numérico diário do Vale Alimentação (VA) ou Auxílio Alimentação.
    3) Quantidade de dias úteis a considerar para cálculo no mês.

    Responda APENAS com um JSON válido, sem comentários, no formato:
    {{
      "valor_vr": <float ou null>,
      "valor_va": <float ou null>,
      "dias_uteis": <int ou null>
    }}

    Texto:
    ---
    {texto_cct}
    ---
    """

    try:
        resp = llm.invoke(prompt)
        content = getattr(resp, "content", resp)
        if not isinstance(content, str):
            content = str(content)
        # Tenta isolar somente o JSON
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            payload = m.group(0)
        else:
            payload = content.strip()
        # valida minimamente o JSON
        try:
            obj = json.loads(payload)
            # normaliza tipos
            def _to_float(x):
                try:
                    return float(x)
                except Exception:
                    return None
            def _to_int(x):
                try:
                    xi = int(float(x))
                    return xi
                except Exception:
                    return None
            out = {
                "valor_vr": _to_float(obj.get("valor_vr")) if obj.get("valor_vr") is not None else None,
                "valor_va": _to_float(obj.get("valor_va")) if obj.get("valor_va") is not None else None,
                "dias_uteis": _to_int(obj.get("dias_uteis")) if obj.get("dias_uteis") is not None else None,
            }
            return json.dumps(out, ensure_ascii=False)
        except Exception:
            # fallback mínimo se não for JSON válido
            return json.dumps({"valor_vr": None, "valor_va": None, "dias_uteis": None}, ensure_ascii=False)
    except Exception:
        return json.dumps({"valor_vr": None, "valor_va": None, "dias_uteis": None}, ensure_ascii=False)
