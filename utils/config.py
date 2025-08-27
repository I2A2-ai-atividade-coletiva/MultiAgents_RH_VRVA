from __future__ import annotations

import os
from typing import Optional, Dict

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq


# Carrega variáveis de ambiente do arquivo .env se existir
load_dotenv()


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(name, default)


def get_llm():
    provider = (get_env("LLM_PROVIDER", "google") or "google").lower()
    temperature = float(get_env("GENAI_TEMPERATURE", "0.2"))
    if provider == "groq":
        # Default para um modelo suportado e estável na Groq
        model = get_env("GROQ_MODEL", "llama-3.1-8b-instant")
        # langchain_groq usa GROQ_API_KEY do ambiente
        return ChatGroq(model=model, temperature=temperature)
    else:
        model = get_env("GENAI_MODEL", "gemini-1.5-pro")
        # A lib usa GOOGLE_API_KEY do ambiente
        return ChatGoogleGenerativeAI(model=model, temperature=temperature)


# ---------------------------------------------------------------------------
# Parametrização de regras de negócio (customizável por .env se desejar)
# ---------------------------------------------------------------------------

# Dias fixos por UF (ex.: SP=22, RJ=21). Se uma UF não estiver aqui, usa dias úteis do mês
DIAS_FIXOS_UF: Dict[str, int] = {
    "SP": int(get_env("DIAS_FIXOS_SP", "22")),
    "RJ": int(get_env("DIAS_FIXOS_RJ", "21")),
}

# Jornada padrão (horas/dia) — disponível para cálculos futuros que dependam de jornada
JORNADA_PADRAO_HORAS: int = int(get_env("JORNADA_PADRAO_HORAS", "8"))

# Valores padrão opcionais quando não houver CCT/estado (pode ser preenchido no .env ou código)
# Formato: { "UF": {"VR": 0.0, "VA": 0.0}, "SINDICATO_CANONICO": {"VR": 0.0, "VA": 0.0} }
VALOR_PADRAO: Dict[str, Dict[str, float]] = {}
